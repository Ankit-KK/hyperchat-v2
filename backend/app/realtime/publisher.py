"""The outbox publisher: a loop in the worker process (python -m app.worker) next to the job slots.

Each cycle:
  realtime disabled -> every pending row becomes 'skipped' (realtime_disabled); nothing is sent
  otherwise         -> expire rows older than the max age, claim a batch (per-creator FIFO), then per row:
      render from the current database state   Skip           -> 'skipped' (reason)
      resolve the creator's Pusher app          none           -> 'skipped' (pusher_not_configured)
      publish                                   ok             -> 'published'
                                                Retryable      -> pending again after Backoff while attempts remain
                                                Permanent / exhausted -> 'failed'
      database unavailable                      nothing written; the lease (30 s) expires and the row is reclaimed
On shutdown (SIGTERM) the rest of a claimed batch is released at once (the attempt is not counted), so the
worker stops well within systemd's TimeoutStopSec.
Delivery is at-least-once: a row whose publish succeeded but whose 'published' write was lost is sent again
with the same event_id, which consumers deduplicate.

Logs carry event ids, event names, channel suffixes (never the slug-bearing channel), reason codes and
attempts only: never payloads, donor data, tokens or secrets.
"""

import asyncio
import logging
import random
import time

import psycopg

from app.jobs.retry import Backoff, Permanent, Retryable, reason_code
from app.jobs.runner import worker_id
from app.realtime.outbox import Event, OutboxQueue
from app.realtime.payloads import Renderer, Skip
from app.realtime.pusher import PusherApps, PusherClient

log = logging.getLogger("hyperchat.realtime")


class RateLimiter:
    """At most `rate` sends per second (spaced evenly)."""

    def __init__(self, rate: float, clock=time.monotonic, sleep=asyncio.sleep) -> None:
        self.interval, self._clock, self._sleep, self._next = 1.0 / rate, clock, sleep, 0.0

    async def wait(self) -> None:
        now = self._clock()
        if self._next > now:
            await self._sleep(self._next - now)
            now = self._next
        self._next = now + self.interval


class OutboxPublisher:
    backoff = Backoff(base_seconds=2, cap_seconds=30, jitter=0.2)

    def __init__(self, queue: OutboxQueue, renderer: Renderer, client: PusherClient, apps: PusherApps, *,
                 enabled: bool, rate: float = 50.0, batch_size: int = 20, poll_seconds: float = 0.5,
                 ident: str | None = None, rng: random.Random | None = None) -> None:
        self.queue, self.renderer, self.client, self.apps = queue, renderer, client, apps
        self.enabled, self.batch_size, self.poll_seconds = enabled, batch_size, poll_seconds
        self.limiter = RateLimiter(rate)
        self.ident = ident or worker_id()
        self.rng = rng or random.Random()
        self.counts = {"published": 0, "failed": 0, "skipped": 0, "retried": 0, "expired": 0}
        self.stopping: asyncio.Event | None = None

    async def run_once(self) -> int:
        """One cycle; returns the number of rows handled (claimed, skipped or expired)."""
        if not self.enabled:
            skipped = await self.queue.skip_pending("realtime_disabled")
            if skipped:
                self.counts["skipped"] += skipped
                log.info("realtime events skipped", extra={"count": skipped, "reason": "realtime_disabled"})
            return skipped
        expired = await self.queue.expire()
        if expired:
            self.counts["expired"] += expired
            log.info("realtime events expired", extra={"count": expired, "reason": "max_age"})
        events = await self.queue.claim(self.ident, self.batch_size)
        for event in events:
            if self.stopping is not None and self.stopping.is_set():  # shutting down: hand the rest back at once
                await self._release(event)
                continue
            await self._process(event)
        return expired + len(events)

    async def _process(self, event: Event) -> None:
        started = time.monotonic()
        if event.attempts > event.max_attempts:  # reclaimed after its last attempt's lease expired
            await self._finish(event, "failed", "lease_expired" if event.reclaimed else "max_attempts", started)
            return
        try:
            rendered = await self.renderer.render(event)
            if isinstance(rendered, Skip):
                await self._finish(event, "skipped", rendered.reason, started)
                return
            app = self.apps.resolve(rendered.pusher_group)
            if app is None:
                await self._finish(event, "skipped", "pusher_not_configured", started)
                return
            await self.limiter.wait()
            await self.client.publish(app, rendered.channel, rendered.event, rendered.data)
        except asyncio.CancelledError:
            await asyncio.shield(self._release(event))
            raise
        except Retryable as exc:
            if event.attempts < event.max_attempts:
                delay = self.backoff.delay(event.attempts, exc.retry_after, self.rng)
                ok = await self._safe(self.queue.retry(event, delay, exc.reason), event)
                self.counts["retried"] += 1
                self._log("realtime event retry scheduled", event, started, reason=exc.reason, delay_s=delay,
                          fenced=not ok, level=logging.WARNING)
            else:
                await self._finish(event, "failed", exc.reason, started)
            return
        except Permanent as exc:
            await self._finish(event, "failed", exc.reason, started)
            return
        except psycopg.Error as exc:  # rendering could not read: the lease expires and the row is reclaimed
            self._log("realtime event interrupted", event, started, reason="database_unavailable",
                      error=type(exc).__name__, level=logging.WARNING)
            return
        except Exception as exc:  # noqa: BLE001 - an unexpected bug: retried like a transient error
            log.warning("realtime publish error", extra={"event_id": event.event_id, "error": type(exc).__name__})
            if event.attempts < event.max_attempts:
                await self._safe(self.queue.retry(event, self.backoff.delay(event.attempts, None, self.rng),
                                                  "internal_error"), event)
            else:
                await self._finish(event, "failed", "internal_error", started)
            return
        await self._finish(event, "published", None, started)

    async def _finish(self, event: Event, status: str, reason: str | None, started: float) -> None:
        reason = reason_code(reason) if reason else None
        ok = await self._safe(self.queue.finish(event, status, reason), event)
        if ok:
            self.counts[status] += 1
        level = logging.INFO if status != "failed" else logging.WARNING
        self._log(f"realtime event {status}" if ok else "realtime event lease lost", event, started,
                  reason=reason, level=level if ok else logging.WARNING)

    async def _safe(self, call, event: Event) -> bool:
        try:
            return await call
        except psycopg.Error as exc:
            log.warning("realtime state not saved", extra={"event_id": event.event_id,
                                                           "reason": "database_unavailable",
                                                           "error": type(exc).__name__})
            return False

    async def _release(self, event: Event) -> None:
        try:
            await self.queue.release(event)
        except Exception as exc:  # noqa: BLE001 - the lease expires anyway
            log.warning("realtime release failed", extra={"event_id": event.event_id, "error": type(exc).__name__})

    def _log(self, msg: str, event: Event, started: float, level: int = logging.INFO, **fields) -> None:
        log.log(level, msg, extra={"event_id": event.event_id, "event": event.event, "channel": event.channel,
                                   "attempt": event.attempts, "max_attempts": event.max_attempts,
                                   "duration_ms": round((time.monotonic() - started) * 1000),
                                   **{k: v for k, v in fields.items() if v is not None}})

    async def run(self, stop: asyncio.Event) -> None:
        self.stopping = stop
        log.info("realtime publisher started", extra={"worker": self.ident, "enabled": self.enabled,
                                                      "pusher_groups": self.apps.groups})
        while not stop.is_set():
            try:
                handled = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - e.g. the database is down: back off, keep running
                log.warning("realtime claim failed", extra={"reason": "database_unavailable",
                                                            "error": type(exc).__name__})
                handled, idle = 0, self.poll_seconds * 10
            else:
                # Disabled: no hurry, rows are only being marked skipped.
                idle = self.poll_seconds * (self.rng.uniform(0.8, 1.2) if self.enabled else 10)
            if not handled:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=idle)
                except TimeoutError:
                    pass
        log.info("realtime publisher stopped", extra={"worker": self.ident, **self.counts})
