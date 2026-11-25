"""Automatic creator invitations (onboarding Stage 2 Phase A, docs/onboarding.md): a poll loop in the worker next to
the outbox publisher, on the worker's hyperchat_app connection. No migration, no job kind, no new table.

Each pass invites, one transaction per application, every approved application that is due:
  * its creator is unclaimed and it has no open unexpired invitation;
  * it was approved within the last 7 days, and at most 7 days before this worker started (no mass invite of old
    approvals when the setting is first turned on, and in-flight retries survive a restart);
  * no admin invited it by hand (a manual invite takes over for good) and no automatic attempt was skipped or paused;
  * every earlier automatic attempt failed, there were fewer than MAX_ATTEMPTS, and the backoff since the last one
    has passed. An expired automatic invitation is never re-sent (owner decision Q4).
The application row is locked FOR UPDATE SKIP LOCKED inside the transaction that issues the invitation
(ApplicationRepository.issue_auto_invite), so two worker processes never invite the same application twice.

The send happens after commit, through the `onboarding` category, exactly as ApplicationService.invite does. The
outcome is kept as actor-less application_events rows (codes only, never an address, link or provider text):
  invite / note 'auto'                    an automatic attempt (its invitation)
  note 'auto:failed:<code>'               its email failed: the link was revoked, a later pass retries
  note 'auto:skipped:<code>'              account_exists (owner decision Q2: the manual owner-assign path) or similar
  note 'auto:paused:<code>'               the database refused the insert (step-up 'active' before migration 0037)
After the MAX_ATTEMPTS-th failure, or on a pause, an `invite_failed` alert is recorded (app.alerts, source
'auto_invite', ids only) and the application is left to the manual resend route.

shortcut: an invitation committed but never sent (the worker died between commit and send) stays open until it
expires and is never retried automatically; the admin view shows it as pending with no failure. Upgrade with the
Phase B delivery table if that matters.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import psycopg

from app.admin.applications import ApplicationRepository
from app.alerts import Alerts
from app.auth.email import EmailDeliveryError, EmailSender, render
from app.auth.tokens import new_hex_token, sha256_hex

log = logging.getLogger("hyperchat.onboarding")

MAX_ATTEMPTS = 5  # owner decision Q4
# Wait after the n-th failed attempt (seconds): 60 s, 5 min, 30 min, 2 h, 12 h. With MAX_ATTEMPTS = 5 the fifth
# failure ends the automatic attempts, so the 12 h step is never waited for (kept as the owner's schedule).
DELAYS = (60, 300, 1800, 7200, 43200)
WINDOW = timedelta(days=7)
BATCH = 10  # applications per pass at most

_AUTO = "e.signup_id = a.id AND e.action = 'invite' AND e.note = 'auto'"
_OUTCOME = "e.signup_id = a.id AND e.action = 'note' AND e.actor_user_id IS NULL AND e.note LIKE"
# One due application, locked. Run again with its id inside the same transaction: the second statement takes a new
# snapshot, so an invitation another worker committed meanwhile is seen and nothing is sent twice.
PICK = f"""
SELECT a.id::text, (SELECT count(*) FROM public.application_events e WHERE {_AUTO}) AS attempts
FROM public.user_signups a JOIN public.streamers s ON s.id = a.streamer_id
WHERE a.review_status = 'approved' AND (%(id)s::uuid IS NULL OR a.id = %(id)s::uuid)
  AND a.reviewed_at >= %(since)s AND a.reviewed_at >= now() - make_interval(days => %(window_days)s)
  AND s.user_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM public.user_invites i WHERE i.signup_id = a.id
                  AND i.accepted_at IS NULL AND i.revoked_at IS NULL AND i.expires_at > now())
  AND NOT EXISTS (SELECT 1 FROM public.application_events e WHERE e.signup_id = a.id
                  AND e.action = 'invite' AND e.note IS DISTINCT FROM 'auto')
  AND NOT EXISTS (SELECT 1 FROM public.application_events e WHERE {_OUTCOME} 'auto:skipped:%%')
  AND NOT EXISTS (SELECT 1 FROM public.application_events e WHERE {_OUTCOME} 'auto:paused:%%')
  AND (SELECT count(*) FROM public.application_events e WHERE {_AUTO})
      = (SELECT count(*) FROM public.application_events e WHERE {_OUTCOME} 'auto:failed:%%')
  AND (SELECT count(*) FROM public.application_events e WHERE {_AUTO}) < %(max_attempts)s
  AND coalesce((SELECT max(e.created_at) + make_interval(secs => (%(delays)s::int[])[count(*)::int])
                FROM public.application_events e WHERE {_AUTO}), '-infinity') <= now()
ORDER BY a.reviewed_at, a.id
LIMIT 1
FOR UPDATE OF a SKIP LOCKED
"""


def retry_delay(attempts: int) -> int:
    """Seconds to wait after the `attempts`-th failed automatic attempt (the SQL reads the same DELAYS)."""
    return DELAYS[attempts - 1]


def failure_code(exc: Exception) -> str:
    if isinstance(exc, EmailDeliveryError):
        return exc.reason  # refused_auth | rejected | transient
    return {"EmailCategoryInactive": "category_inactive",
            "EmailRecipientNotAllowed": "recipient_not_allowed"}.get(type(exc).__name__, "send_error")


class AutoInviter:
    def __init__(self, repo: ApplicationRepository, sender: EmailSender, alerts: Alerts, *, frontend_url: str,
                 ttl_days: int, poll_seconds: float, started_at: datetime | None = None) -> None:
        self.repo, self.sender, self.alerts = repo, sender, alerts
        self.frontend_url, self.ttl_days, self.poll_seconds = frontend_url.rstrip("/"), ttl_days, poll_seconds
        self.started_at = started_at or datetime.now(UTC)  # the enable time, in memory only

    async def _alert(self, code: str, **ids: str) -> None:
        log.warning("automatic invitation alert", extra={"reason": code, **ids})
        try:
            await self.alerts.record(kind="invite_failed", source="auto_invite", code=code, **ids)
        except Exception as exc:  # noqa: BLE001 - alerting never fails what it reports on (app.alerts)
            log.warning("alert not recorded", extra={"kind": "invite_failed", "error": type(exc).__name__})

    def _params(self, signup_id: str | None) -> dict:
        return {"id": signup_id, "since": self.started_at - WINDOW, "window_days": WINDOW.days,
                "max_attempts": MAX_ATTEMPTS, "delays": list(DELAYS)}

    async def run_once(self) -> list[tuple[str, str]]:
        """One pass; returns (signup id, outcome) per application handled: sent, failed, skipped or paused."""
        handled = []
        for _ in range(BATCH):
            outcome = await self._one()
            if outcome is None:
                break
            handled.append(outcome)
        return handled

    async def _one(self) -> tuple[str, str] | None:
        picked: dict = {}

        async def pick(cur):
            await cur.execute(PICK, self._params(None))
            row = await cur.fetchone()
            if row is not None:
                await cur.execute(PICK, self._params(row["id"]))
                row = await cur.fetchone()
            if row is not None:
                picked.update(id=row["id"], attempt=row["attempts"] + 1)
            return picked.get("id")

        token = new_hex_token(32)
        try:
            result, invite, signup_id = await self.repo.issue_auto_invite(pick, sha256_hex(token), self.ttl_days)
        except psycopg.errors.InsufficientPrivilege as exc:  # a database guard: step-up 'active' (Q13/Q14)
            if "id" not in picked:
                raise
            code = "stepup_refused" if "step-up" in str(exc) else "guard_refused"
            await self.repo.auto_outcome(picked["id"], f"auto:paused:{code}")
            await self._alert(code, signup_id=picked["id"])
            return picked["id"], "paused"
        if result == "none_due":
            return None
        if result != "ok":  # account_exists, creator_claimed: never retried automatically
            await self.repo.auto_outcome(signup_id, f"auto:skipped:{result}")
            log.warning("automatic invitation skipped", extra={"signup_id": signup_id, "reason": result})
            return signup_id, "skipped"
        try:
            await self.sender.send(render("invite", invite["email"], f"{self.frontend_url}/invite?token={token}"))
        except Exception as exc:  # noqa: BLE001 - any provider failure: a code only, never the link or address
            code, attempt = failure_code(exc), picked["attempt"]
            await self.repo.auto_outcome(signup_id, f"auto:failed:{code}", revoke_invite_id=invite["id"])
            log.warning("automatic invitation not delivered", extra={
                "signup_id": signup_id, "invite_id": invite["id"], "reason": code, "attempt": attempt,
                "max_attempts": MAX_ATTEMPTS, "retry_in_s": retry_delay(attempt) if attempt < MAX_ATTEMPTS else None})
            if attempt >= MAX_ATTEMPTS:
                await self._alert(code, signup_id=signup_id, invite_id=invite["id"])
            return signup_id, "failed"
        log.info("automatic invitation sent", extra={"signup_id": signup_id, "invite_id": invite["id"],
                                                     "attempt": picked["attempt"], "count": invite["replaced"]})
        return signup_id, "sent"

    async def run(self, stop: asyncio.Event) -> None:
        log.info("automatic invitations started", extra={"poll_s": self.poll_seconds})
        while not stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - e.g. the database is down: try again next pass
                log.warning("automatic invitation pass failed", extra={
                    "reason": "database_unavailable" if isinstance(exc, psycopg.Error) else "internal_error",
                    "error": type(exc).__name__})
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
        log.info("automatic invitations stopped")
