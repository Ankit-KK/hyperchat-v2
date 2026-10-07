"""The worker loop: N slots, each claiming one job at a time from PostgreSQL.

Per job: run the kind's handler.
  Done                 -> finish 'succeeded' (+ the handler's effects, same transaction)
  Retryable            -> back to 'queued' after Backoff.delay(attempt) while attempts < max_attempts
  Permanent / exhausted-> finish 'failed' (+ the handler's fallback effects, e.g. the silent audio URL)
  lost lease           -> nothing is written (another worker owns the job now)
  database unavailable -> nothing is written; the lease expires and the job is claimed again
A job claimed again after its lease expired with no attempt left fails immediately with its fallback.

Shutdown (SIGTERM): stop claiming, let running jobs finish for up to `shutdown_grace` seconds, then cancel
them and release their jobs (status queued, the interrupted attempt not counted). Logs carry job ids, kinds,
attempts and reason codes only.
"""

import asyncio
import logging
import os
import random
import socket
import time
import uuid
from typing import Protocol

import psycopg

from app.jobs.queue import Job, JobQueue
from app.jobs.retry import Backoff, Done, Effects, Permanent, Retryable, reason_code

log = logging.getLogger("hyperchat.worker")


class Handler(Protocol):
    kind: str
    backoff: Backoff

    async def run(self, job: Job) -> Done: ...

    async def fallback(self, job: Job, reason: str) -> Effects | None: ...


def worker_id() -> str:
    return f"{socket.gethostname()[:40]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Worker:
    def __init__(self, queue: JobQueue, handlers: dict[str, Handler], *, concurrency: int = 2,
                 poll_seconds: float = 1.0, shutdown_grace: float = 50.0, ident: str | None = None,
                 rng: random.Random | None = None) -> None:
        self.queue, self.handlers = queue, handlers
        self.concurrency, self.poll_seconds, self.shutdown_grace = concurrency, poll_seconds, shutdown_grace
        self.ident = ident or worker_id()
        self.rng = rng or random.Random()
        self.processed = 0

    async def run_once(self) -> Job | None:
        """Claim and process at most one job (used by tests and by each slot)."""
        job = await self.queue.claim(self.ident, list(self.handlers))
        if job is not None:
            await self._process(job)
        return job

    async def _process(self, job: Job) -> None:
        handler = self.handlers[job.kind]
        started = time.monotonic()
        if job.attempts > job.max_attempts:  # reclaimed after its last attempt's lease expired
            await self._fail(job, handler, "lease_expired" if job.reclaimed else "max_attempts", started)
            return
        try:
            done = await handler.run(job)
        except asyncio.CancelledError:
            await asyncio.shield(self._release(job))
            raise
        except Retryable as exc:
            if job.attempts < job.max_attempts:
                delay = handler.backoff.delay(job.attempts, exc.retry_after, self.rng)
                ok = await self._safe(self.queue.retry(job, delay, exc.reason), job)
                self._log("job retry scheduled", job, started, reason=exc.reason, delay_s=delay, fenced=not ok)
            else:
                await self._fail(job, handler, exc.reason, started)
            return
        except Permanent as exc:
            await self._fail(job, handler, exc.reason, started)
            return
        except psycopg.Error as exc:  # the lease expires and another claim retries the job
            self._log("job interrupted", job, started, reason="database_unavailable", error=type(exc).__name__,
                      level=logging.WARNING)
            return
        except Exception as exc:  # noqa: BLE001 - an unexpected bug: retried like a transient error
            reason = "internal_error"
            log.warning("job handler error", extra={"job_id": job.id, "kind": job.kind, "error": type(exc).__name__})
            if job.attempts < job.max_attempts:
                delay = handler.backoff.delay(job.attempts, None, self.rng)
                await self._safe(self.queue.retry(job, delay, reason), job)
            else:
                await self._fail(job, handler, reason, started)
            return
        ok = await self._safe(self.queue.finish(job, "succeeded", result=done.result, last_error=None,
                                                effects=done.effects), job)
        self.processed += 1
        self._log("job succeeded" if ok else "job lease lost", job, started,
                  outcome=str(done.result.get("outcome", ""))[:40], level=logging.INFO if ok else logging.WARNING)

    async def _fail(self, job: Job, handler: Handler, reason: str, started: float) -> None:
        reason = reason_code(reason)
        effects = None
        try:
            effects = await handler.fallback(job, reason)
        except Exception as exc:  # noqa: BLE001 - the job still fails; its fallback is reported
            log.warning("job fallback error", extra={"job_id": job.id, "kind": job.kind, "error": type(exc).__name__})
        ok = await self._safe(self.queue.finish(job, "failed", result={"reason": reason, "fallback": effects is not None},
                                                last_error=reason, effects=effects), job)
        self.processed += 1
        self._log("job failed" if ok else "job lease lost", job, started, reason=reason, fallback=effects is not None,
                  level=logging.WARNING)

    async def _safe(self, call, job: Job) -> bool:
        try:
            return await call
        except psycopg.Error as exc:
            log.warning("job state not saved", extra={"job_id": job.id, "kind": job.kind,
                                                      "reason": "database_unavailable", "error": type(exc).__name__})
            return False

    async def _release(self, job: Job) -> None:
        try:
            released = await self.queue.release(job)
            log.info("job released", extra={"job_id": job.id, "kind": job.kind, "released": released})
        except Exception as exc:  # noqa: BLE001 - the lease expires anyway
            log.warning("job release failed", extra={"job_id": job.id, "error": type(exc).__name__})

    def _log(self, msg: str, job: Job, started: float, level: int = logging.INFO, **fields) -> None:
        log.log(level, msg, extra={"job_id": job.id, "kind": job.kind, "attempt": job.attempts,
                                   "max_attempts": job.max_attempts,
                                   "duration_ms": round((time.monotonic() - started) * 1000), **fields})

    async def _slot(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                job = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - e.g. the database is down: back off, keep running
                log.warning("claim failed", extra={"reason": "database_unavailable", "error": type(exc).__name__})
                job, idle = None, self.poll_seconds * 5
            else:
                idle = self.poll_seconds * self.rng.uniform(0.8, 1.2)
            if job is None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=idle)
                except TimeoutError:
                    pass

    async def run(self, stop: asyncio.Event) -> None:
        log.info("worker started", extra={"worker": self.ident, "concurrency": self.concurrency,
                                          "kinds": sorted(self.handlers)})
        slots = [asyncio.create_task(self._slot(stop)) for _ in range(self.concurrency)]
        await stop.wait()
        log.info("worker stopping", extra={"worker": self.ident})
        _, pending = await asyncio.wait(slots, timeout=self.shutdown_grace)
        for task in pending:  # still processing after the grace period: release their jobs
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        log.info("worker stopped", extra={"worker": self.ident, "processed": self.processed,
                                          "cancelled": len(pending)})
