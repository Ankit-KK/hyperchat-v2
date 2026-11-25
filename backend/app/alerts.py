"""Operator failure alerts (docs/alerting.md): rows in public.system_alerts (migration 0005, columns used as they are).

  source    the subsystem that failed: 'worker' (a job exhausted its attempts) or 'payments' (an account went on hold)
  severity  'error'
  message   a short reason code (app.jobs.retry.reason_code), never free text
  context   {"kind": <alert kind>, <entity ids>} and, after delivery was given up, "delivery_error"
  notified_at  set when the alert reached the operator channel (NULL = recorded, not delivered)

Never a donor name, a message, a token or a key: every context value is a database id or a reason code. Recording is
best effort from the callers' point of view: an alert that cannot be written is logged, and never fails the job or
the checkout it reports on.
"""

import logging
import uuid

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.jobs.retry import reason_code

log = logging.getLogger("hyperchat.alerts")
KINDS = ("job_failed", "payment_on_hold")


class Alerts:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def record(self, *, kind: str, source: str, code: str, **ids: str) -> str:
        """One alert row; `ids` are entity ids (or short codes) and go into the context as they are, cut at 80 chars."""
        assert kind in KINDS
        alert_id = str(uuid.uuid4())  # generated here: the row id is also the delivery dedupe key
        context = {"kind": kind, **{k: str(v)[:80] for k, v in ids.items() if v}}
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute("INSERT INTO public.system_alerts (id, source, severity, message, context) "
                              "VALUES (%s, %s, 'error', %s, %s)", (alert_id, source, reason_code(code), Jsonb(context)))
        return alert_id

    async def _best_effort(self, **fields) -> None:
        try:
            await self.record(**fields)
        except Exception as exc:  # noqa: BLE001 - alerting never fails what it reports on
            log.warning("alert not recorded", extra={"kind": fields["kind"], "error": type(exc).__name__})

    async def job_failed(self, job, reason: str) -> None:
        """Worker hook (app.jobs.runner on_failure): a job finished 'failed' with its attempts exhausted."""
        await self._best_effort(kind="job_failed", source="worker", code=reason, job_id=job.id, job_kind=job.kind,
                                dedupe_key=job.dedupe_key)

    async def payment_on_hold(self, streamer_id: str, status: str) -> None:
        """A creator's payment account went on hold (missing or refused keys); `status` is the health status code."""
        await self._best_effort(kind="payment_on_hold", source="payments", code=status, streamer_id=streamer_id)

    async def list(self, *, kind: str | None, delivered: bool | None, limit: int, offset: int) -> list[dict]:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("""
                SELECT id::text AS id, source, severity, message AS code, context->>'kind' AS kind,
                       context - 'kind' AS context, created_at, notified_at
                FROM public.system_alerts
                WHERE (%(kind)s::text IS NULL OR context->>'kind' = %(kind)s)
                  AND (%(delivered)s::boolean IS NULL OR (notified_at IS NOT NULL) = %(delivered)s)
                ORDER BY created_at DESC, id DESC LIMIT %(limit)s OFFSET %(offset)s
            """, {"kind": kind, "delivered": delivered, "limit": limit, "offset": offset})
            return await cur.fetchall()

    async def delivered(self, alert_id: str) -> None:
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute("UPDATE public.system_alerts SET notified_at = now() WHERE id = %s AND notified_at IS NULL",
                              (alert_id,))

    async def delivery_failed(self, alert_id: str, reason: str) -> None:
        """Record the last delivery error on the alert (a reason code; the alert stays undelivered)."""
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute("UPDATE public.system_alerts SET context = context || jsonb_build_object("
                              "'delivery_error', %s::text) WHERE id = %s", (reason_code(reason), alert_id))


def discord_message(alert: dict, app_env: str) -> dict:
    """One Discord message for one alert: kind, source, reason code and entity ids only; no mention is ever parsed."""
    ids = " ".join(f"{k}={v}" for k, v in sorted(alert["context"].items()) if k not in ("kind", "delivery_error"))
    text = (f"HyperChat {app_env} alert: {alert['kind']} ({alert['source']}, {alert['severity']}) "
            f"code={alert['code']} {ids} at={alert['created_at'].isoformat(timespec='seconds')} id={alert['id']}")
    return {"content": text[:2000], "allowed_mentions": {"parse": []}}
