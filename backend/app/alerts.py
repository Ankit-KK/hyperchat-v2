"""Operator failure alerts (docs/alerting.md): rows in public.system_alerts (migration 0005, columns used as they are),
delivered to one Discord channel by a loop in the worker (python -m app.worker), like the realtime outbox publisher.

  source       the subsystem that failed: 'worker' (a job exhausted its attempts) or 'payments' (an account on hold)
  severity     'error'
  message      a short reason code (app.jobs.retry.reason_code), never free text
  context      {"kind": <alert kind>, <entity ids>} plus the delivery state: delivery_attempts, delivery_attempted_at,
               delivery_error (a reason code)
  notified_at  set when the alert reached the operator channel (NULL = recorded, not delivered)

Never a donor name, a message, a token or a key: every context value is a database id or a reason code. Recording is
best effort for the callers: an alert that cannot be written is logged and never fails the job it reports on.

The loop, every HYPERCHAT_ALERT_POLL_SECONDS:
  1. payment holds: a creator whose streamer_payment_health row went on hold (invalid_key / missing_key; never the
     transient network_error) gets one payment_on_hold alert (the payments code itself is not changed during the
     PAY-15 soak, so the hold is read from the row the checkout path writes)
  2. delivery (only with HYPERCHAT_ALERT_DISCORD_CHANNEL_ID and HYPERCHAT_DISCORD_BOT_TOKEN set): each undelivered
     alert is claimed (FOR UPDATE SKIP LOCKED: several workers never send the same one) and sent as one message
       sent              -> notified_at = now()
       Retryable error   -> attempt counted, delivery_error recorded; tried again after interval * 2^(attempts-1)
       Permanent error   -> given up at once (attempts = MAX_ATTEMPTS), delivery_error recorded
     after MAX_ATTEMPTS an alert is never sent again; it stays listed (GET /api/v1/admin/alerts?delivered=false).
Delivery is at-least-once: a message sent whose notified_at write was lost is sent again.
"""

import asyncio
import logging
import uuid

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.jobs.retry import Permanent, Retryable, reason_code

log = logging.getLogger("hyperchat.alerts")
KINDS = ("job_failed", "payment_on_hold")
MAX_ATTEMPTS = 5
HOLD_STATUSES = ("invalid_key", "missing_key")
_COLUMNS = ("id::text AS id, source, severity, message AS code, context->>'kind' AS kind, "
            "context - 'kind' AS context, created_at, notified_at")


class Alerts:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def record(self, *, kind: str, source: str, code: str, **ids: str) -> str:
        """One alert row; `ids` are entity ids (or short codes) and go into the context as they are, cut at 80 chars."""
        assert kind in KINDS
        alert_id = str(uuid.uuid4())
        context = {"kind": kind, **{k: str(v)[:80] for k, v in ids.items() if v}}
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute("INSERT INTO public.system_alerts (id, source, severity, message, context) "
                              "VALUES (%s, %s, 'error', %s, %s)", (alert_id, source, reason_code(code), Jsonb(context)))
        return alert_id

    async def job_failed(self, job, reason: str) -> None:
        """Worker hook (app.jobs.runner on_failure): a job finished 'failed' (attempts exhausted or a permanent error)."""
        try:
            await self.record(kind="job_failed", source="worker", code=reason, job_id=job.id, job_kind=job.kind,
                              dedupe_key=job.dedupe_key)
        except Exception as exc:  # noqa: BLE001 - alerting never fails what it reports on
            log.warning("alert not recorded", extra={"kind": "job_failed", "error": type(exc).__name__})

    async def holds(self) -> list[dict]:
        """Creators on hold now, each with the code of its latest payment_on_hold alert (None: never alerted)."""
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("""
                SELECT h.streamer_id::text AS streamer_id, h.status,
                       (SELECT a.message FROM public.system_alerts a
                        WHERE a.context->>'kind' = 'payment_on_hold' AND a.context->>'streamer_id' = h.streamer_id::text
                        ORDER BY a.created_at DESC LIMIT 1) AS alerted
                FROM public.streamer_payment_health h
                WHERE h.on_hold AND h.status = ANY(%s)
            """, (list(HOLD_STATUSES),))
            return await cur.fetchall()

    async def list(self, *, kind: str | None, delivered: bool | None, limit: int, offset: int) -> list[dict]:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"""
                SELECT {_COLUMNS} FROM public.system_alerts
                WHERE (%(kind)s::text IS NULL OR context->>'kind' = %(kind)s)
                  AND (%(delivered)s::boolean IS NULL OR (notified_at IS NOT NULL) = %(delivered)s)
                ORDER BY created_at DESC, id DESC LIMIT %(limit)s OFFSET %(offset)s
            """, {"kind": kind, "delivered": delivered, "limit": limit, "offset": offset})
            return await cur.fetchall()

    async def deliver_one(self, send, retry_base_seconds: float) -> str | None:
        """Claim the oldest due undelivered alert, `await send(alert)` it and record the outcome in the same
        transaction. Returns 'sent' / 'retry' / 'given_up', or None when nothing is due."""
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"""
                SELECT {_COLUMNS} FROM public.system_alerts
                WHERE notified_at IS NULL AND context ? 'kind'
                  AND COALESCE((context->>'delivery_attempts')::int, 0) < %(max)s
                  AND (context->>'delivery_attempted_at' IS NULL
                       OR (context->>'delivery_attempted_at')::timestamptz + make_interval(
                            secs => %(base)s * power(2, (context->>'delivery_attempts')::int - 1)) <= now())
                ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
            """, {"max": MAX_ATTEMPTS, "base": retry_base_seconds})
            alert = await cur.fetchone()
            if alert is None:
                return None
            attempts = int(alert["context"].get("delivery_attempts", 0)) + 1
            try:
                await send(alert)
            except (Retryable, Permanent) as exc:
                outcome = "given_up" if isinstance(exc, Permanent) or attempts >= MAX_ATTEMPTS else "retry"
                await cur.execute("""
                    UPDATE public.system_alerts SET context = context || jsonb_build_object('delivery_attempts', %s::int,
                        'delivery_attempted_at', now(), 'delivery_error', %s::text) WHERE id = %s
                """, (MAX_ATTEMPTS if isinstance(exc, Permanent) else attempts, exc.reason, alert["id"]))
                log.warning("alert delivery failed", extra={"alert_id": alert["id"], "reason": exc.reason,
                                                            "attempt": attempts, "outcome": outcome})
                return outcome
            await cur.execute("UPDATE public.system_alerts SET notified_at = now(), context = context || "
                              "jsonb_build_object('delivery_attempts', %s::int) WHERE id = %s", (attempts, alert["id"]))
            log.info("alert delivered", extra={"alert_id": alert["id"], "kind": alert["kind"], "attempt": attempts})
            return "sent"


def discord_message(alert: dict, app_env: str) -> dict:
    """One Discord message for one alert: kind, source, reason code and entity ids only; no mention is ever parsed."""
    ids = " ".join(f"{k}={v}" for k, v in sorted(alert["context"].items()) if not k.startswith("delivery_"))
    text = (f"HyperChat {app_env} alert: {alert['kind']} ({alert['source']}, {alert['severity']}) "
            f"code={alert['code']} {ids} at={alert['created_at'].isoformat(timespec='seconds')} id={alert['id']}")
    return {"content": text[:2000], "allowed_mentions": {"parse": []}}


class AlertNotifier:
    """The worker loop (see the module docstring). `discord` is None (or `channel_id` unset) = record only."""

    def __init__(self, alerts: Alerts, *, discord, channel_id: str | None, app_env: str,
                 interval: float = 30.0, batch: int = 20) -> None:
        self.alerts, self.discord, self.channel_id, self.app_env = alerts, discord, channel_id, app_env
        self.interval, self.batch = interval, batch
        self.held: dict[str, str] | None = None  # creator -> hold status at the last scan (None: not scanned yet)
        self.stopping: asyncio.Event | None = None

    @property
    def enabled(self) -> bool:
        return self.discord is not None and bool(self.channel_id)

    async def scan_holds(self) -> int:
        """Alert each creator that went on hold, or whose hold reason changed, since the last scan. On the first scan
        after a start, a hold whose
        latest alert already carries the same code is not alerted again."""
        rows = await self.alerts.holds()
        new = 0
        for row in rows:
            last = self.held.get(row["streamer_id"]) if self.held is not None else row["alerted"]
            seen = last == row["status"]  # the same hold with the same reason: already alerted
            if not seen:
                await self.alerts.record(kind="payment_on_hold", source="payments", code=row["status"],
                                         streamer_id=row["streamer_id"])
                new += 1
        self.held = {r["streamer_id"]: r["status"] for r in rows}
        return new

    async def _send(self, alert: dict) -> None:
        await self.discord.create_message(self.channel_id, discord_message(alert, self.app_env))

    async def run_once(self) -> dict:
        counts = {"recorded": await self.scan_holds(), "sent": 0, "retry": 0, "given_up": 0}
        if self.enabled:
            for _ in range(self.batch):
                if self.stopping is not None and self.stopping.is_set():  # shutting down: the rest waits
                    break
                outcome = await self.alerts.deliver_one(self._send, self.interval)
                if outcome is None:
                    break
                counts[outcome] += 1
        return counts

    async def run(self, stop: asyncio.Event) -> None:
        self.stopping = stop
        log.info("alert notifier started", extra={"delivery_enabled": self.enabled, "interval_s": self.interval})
        while not stop.is_set():
            try:
                counts = await self.run_once()
                if any(counts.values()):
                    log.info("alert notifier cycle", extra=counts)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - e.g. the database is down: try again next cycle
                log.warning("alert notifier cycle failed", extra={"error": type(exc).__name__})
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass
        log.info("alert notifier stopped")
