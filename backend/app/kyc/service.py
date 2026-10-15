"""Owner identity verification with Didit (KYC PR3; migration 0022). See app.kyc.didit for the provider contract.

KYC belongs to the USER (users.id = Didit vendor_data), never to a creator: a transfer never moves it, and one verified
person keeps it for every creator they own. Only a creator's current owner may start a verification (a linked
moderator or a user without an owned creator may not). This PR only records verification; it never activates a
creator and does not change donation eligibility (PR4 wires user_kyc_approved into activation and enforcement).

Status writes go through public.kyc_record_provider_status (the only writer; the API's database role cannot UPDATE
kyc_sessions): provider order is kept by the provider's own update time, so a delayed old event never overwrites a
newer one, while a later expiry or decline always replaces an approval. Approved is accepted only from a signed
webhook that carries the matching decision, or from Didit's decision endpoint (admin refresh).
"""

import asyncio
import json
import time
from datetime import UTC, datetime

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.auth.repository import User
from app.core.config import Settings
from app.core.errors import ApiError
from app.kyc.didit import (
    STATUSES,
    CreatedSession,
    DiditClient,
    DiditError,
    as_uuid,
    check_statuses,
    fresh,
    signature_valid,
)
from app.security.audit import security_event

RATE_START = ("kyc:session", 5, 3600)       # per user
RATE_REFRESH = ("kyc:refresh", 30, 3600)    # per admin
HANDLED_TYPES = ("status.updated", "data.updated")
STATE = {"Not Started": "in_progress", "In Progress": "in_progress", "Resubmitted": "in_progress",
         "Awaiting User": "in_progress", "In Review": "in_review", "Declined": "declined", "Kyc Expired": "expired",
         "Expired": "not_completed", "Abandoned": "not_completed", "Approved": "approved"}
UNAVAILABLE = "Identity verification is unavailable right now. Please try again later."
_SESSION_COLS = """session_id::text, workflow_id::text, workflow_version, environment, provider_status,
    provider_updated_at, check_statuses, approved_at, created_at, updated_at"""


def _iso(value):
    return value.isoformat() if value is not None else None


def summary(sessions: list[dict], environment: str | None) -> dict:
    """The user's KYC state from their sessions in this environment (newest first). Approved only from Approved."""
    mine = [s for s in sessions if s["environment"] == environment]
    approved = any(s["provider_status"] == "Approved" for s in mine)
    latest = mine[0] if mine else None
    state = "approved" if approved else (STATE[latest["provider_status"]] if latest else "none")
    return {"approved": approved, "state": state,
            "provider_status": latest["provider_status"] if latest else None,
            "updated_at": _iso(latest["updated_at"]) if latest else None}


class KycRepository:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def owns_creator(self, user_id: str) -> bool:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("SELECT EXISTS (SELECT 1 FROM public.streamers WHERE user_id = %s)", (user_id,))
            return (await cur.fetchone())[0]

    async def sessions(self, user_id: str) -> list[dict]:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"SELECT {_SESSION_COLS} FROM public.kyc_sessions WHERE user_id = %s "
                              "ORDER BY created_at DESC, id DESC", (user_id,))
            return await cur.fetchall()

    async def events(self, user_id: str) -> list[dict]:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT session_id::text, action, from_status, to_status, source, actor_user_id::text, "
                              "created_at FROM public.kyc_events WHERE user_id = %s ORDER BY created_at, id",
                              (user_id,))
            return await cur.fetchall()

    async def approved(self, user_id: str, environment: str) -> bool:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("SELECT public.user_kyc_approved(%s, %s)", (user_id, environment))
            return (await cur.fetchone())[0]

    async def record_session(self, user_id: str, created: CreatedSession, workflow_id: str, environment: str) -> dict:
        """Store a session Didit returned (idempotent: a session already stored is kept), then apply any signed
        webhook for it that arrived before this row existed (an early webhook), oldest first."""
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("""
                INSERT INTO public.kyc_sessions (user_id, vendor_data, session_id, workflow_id, workflow_version,
                                                 environment)
                VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (session_id) DO NOTHING RETURNING id
            """, (user_id, user_id, created.session_id, workflow_id, created.workflow_version, environment))
            if await cur.fetchone() is not None:
                await cur.execute("INSERT INTO public.kyc_events (user_id, session_id, action, to_status, source, "
                                  "actor_user_id) VALUES (%s, %s, 'session_created', 'Not Started', "
                                  "'session_create', %s)", (user_id, created.session_id, user_id))
            await cur.execute(f"SELECT user_id::text, {_SESSION_COLS} FROM public.kyc_sessions WHERE session_id = %s",
                              (created.session_id,))
            row = await cur.fetchone()
            if row["user_id"] != user_id:
                raise DiditError("session_owner_mismatch")  # never: vendor_data is this user; refuse to hand it out
            await cur.execute("""
                SELECT id, provider_status, provider_updated_at, vendor_data FROM public.kyc_webhook_events
                WHERE session_id = %s AND outcome = 'unmatched' ORDER BY provider_updated_at, received_at FOR UPDATE
            """, (created.session_id,))
            for early in await cur.fetchall():
                if early["vendor_data"] != user_id:
                    continue  # signed for another user: never applied
                await cur.execute("SELECT public.kyc_record_provider_status(%s, %s, %s, NULL, 'webhook', NULL)",
                                  (created.session_id, early["provider_status"], early["provider_updated_at"]))
                await cur.execute("UPDATE public.kyc_webhook_events SET outcome = 'matched_later' WHERE id = %s",
                                  (early["id"],))
            await cur.execute(f"SELECT {_SESSION_COLS} FROM public.kyc_sessions WHERE session_id = %s",
                              (created.session_id,))
            return await cur.fetchone()

    async def record_webhook(self, event: dict) -> str:
        """Dedupe on event_id, then (unless the outcome is already decided) apply it to its owner-bound session, all
        in one transaction. 'duplicate' when this event id was already received."""
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("""
                INSERT INTO public.kyc_webhook_events (event_id, webhook_type, session_id, vendor_data, provider_status,
                                                       provider_updated_at, environment, outcome)
                VALUES (%(event_id)s, %(webhook_type)s, %(session_id)s, %(vendor_data)s, %(status)s, %(provider_at)s,
                        %(environment)s, %(outcome)s)
                ON CONFLICT (event_id) DO NOTHING RETURNING id
            """, event)
            row = await cur.fetchone()
            if row is None:
                return "duplicate"
            if event["outcome"] != "pending":
                return event["outcome"]
            # No lock needed: a session's identity columns never change (the runtime role cannot UPDATE the table);
            # kyc_record_provider_status locks the row for the status change itself.
            await cur.execute("SELECT user_id::text, workflow_id::text, environment FROM public.kyc_sessions "
                              "WHERE session_id = %s", (event["session_id"],))
            session = await cur.fetchone()
            if session is None:
                outcome = "unmatched"  # an early webhook: applied when the session row is stored
            elif (session["user_id"] != event["vendor_data"] or session["workflow_id"] != event["workflow_id"]
                  or session["environment"] != event["environment"]):
                outcome = "identity_mismatch"
            else:
                await cur.execute("SELECT public.kyc_record_provider_status(%s, %s, %s, %s, 'webhook', NULL) AS r",
                                  (event["session_id"], event["status"], event["provider_at"],
                                   Jsonb(event["checks"]) if event["checks"] is not None else None))
                outcome = "stale" if (await cur.fetchone())["r"] == "stale" else "applied"
            await cur.execute("UPDATE public.kyc_webhook_events SET outcome = %s WHERE id = %s", (outcome, row["id"]))
            return outcome

    async def refresh(self, session_id: str, status: str, checks: dict, actor_id: str) -> str:
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute("SELECT public.kyc_record_provider_status(%s, %s, now(), %s, 'admin_refresh', %s)",
                              (session_id, status, Jsonb(checks), actor_id))
            return (await cur.fetchone())[0]


class KycService:
    def __init__(self, repo: KycRepository, settings: Settings, *, client: DiditClient | None, limiter) -> None:
        self.repo, self.settings, self.client, self.limiter = repo, settings, client, limiter

    @classmethod
    def from_settings(cls, repo: KycRepository, settings: Settings, limiter) -> "KycService":
        client = None
        if settings.didit_configured:
            client = DiditClient(base=settings.didit_api_base, api_key=settings.didit_api_key.get_secret_value().strip(),
                                 workflow_id=as_uuid(settings.didit_workflow_id.strip()))
        return cls(repo, settings, client=client, limiter=limiter)

    @property
    def environment(self) -> str | None:
        return self.settings.didit_environment

    def _available(self) -> None:
        if self.client is None:
            raise ApiError(503, UNAVAILABLE, "kyc_not_configured")

    async def _limit(self, key: str, rate: tuple[str, int, int]) -> None:
        bucket, limit, window = rate
        if not await self.limiter.hit_rate_limit(key, bucket, limit, window):
            raise ApiError(429, "Too many requests. Please try again later.", "kyc_rate_limited")

    async def kyc_approved(self, user_id: str) -> bool:
        """THE rule (for PR4's activation and enforcement): does this user hold an approved Didit verification from
        this environment's Didit application? False when KYC is not configured."""
        return bool(self.environment) and await self.repo.approved(user_id, self.environment)

    async def status(self, user: User) -> dict:
        owns = await self.repo.owns_creator(user.id)
        state = summary(await self.repo.sessions(user.id), self.environment)
        return {**state, "owns_creator": owns, "available": self.client is not None,
                "can_start": owns and self.client is not None and state["state"] not in ("approved", "in_review")}

    async def start(self, user: User, ip: str | None) -> dict:
        """Start (or resume: Didit returns the open session) the signed-in owner's verification. Answers only the
        hosted URL and the session's safe metadata."""
        self._available()
        if not await self.repo.owns_creator(user.id):
            raise ApiError(403, "Only a creator's owner can verify their identity here", "not_creator_owner")
        await self._limit(f"user:{user.id}", RATE_START)
        state = summary(await self.repo.sessions(user.id), self.environment)
        if state["state"] == "approved":
            raise ApiError(409, "Your identity is already verified", "kyc_already_approved")
        if state["state"] == "in_review":
            raise ApiError(409, "Your verification is being reviewed", "kyc_in_review")
        callback = f"{self.settings.frontend_url.rstrip('/')}/kyc/return"  # never from the request
        try:
            created = await self.client.create_session(user.id, callback)
            row = await self.repo.record_session(user.id, created, self.client.workflow_id, self.environment)
        except DiditError as exc:
            security_event("kyc_session_failed", reason=exc.reason, user_id=user.id, client_ip=ip)
            raise ApiError(503, UNAVAILABLE, "kyc_provider_unavailable") from None
        security_event("kyc_session_started", level=20, user_id=user.id, client_ip=ip)
        return {"session_id": row["session_id"], "status": row["provider_status"], "url": created.url}

    # ── webhook ───────────────────────────────────────────────────────────
    async def webhook(self, raw: bytes, signature: str | None, x_timestamp: str | None, ip: str | None) -> dict:
        self._available()
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Malformed payload", "kyc_webhook_malformed") from None
        if not isinstance(body, dict):
            raise ApiError(400, "Malformed payload", "kyc_webhook_malformed")
        secret = self.settings.didit_webhook_secret.get_secret_value().strip()
        if not signature_valid(body, signature, secret):
            security_event("kyc_webhook_rejected", reason="bad_signature" if signature else "no_signature",
                           client_ip=ip)
            raise ApiError(401, "Invalid signature", "kyc_webhook_bad_signature")
        now = time.time()
        header_ts = int(x_timestamp) if x_timestamp and x_timestamp.isascii() and x_timestamp.isdigit() else None
        if not fresh(body.get("timestamp"), now) or not fresh(header_ts, now):
            security_event("kyc_webhook_rejected", reason="stale_or_missing_timestamp", client_ip=ip)
            raise ApiError(401, "Stale or missing timestamp", "kyc_webhook_stale")
        event_id = body.get("event_id")
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 128:
            raise ApiError(400, "Malformed payload", "kyc_webhook_malformed")
        if body.get("environment") != self.environment or (
                self.settings.didit_application_id
                and as_uuid(body.get("application_id")) != as_uuid(self.settings.didit_application_id)):
            security_event("kyc_webhook_rejected", reason="wrong_application_or_environment", client_ip=ip)
            raise ApiError(403, "Not this environment's Didit application", "kyc_webhook_wrong_application")

        status, created_at = body.get("status"), body.get("created_at")
        decision = body.get("decision") if isinstance(body.get("decision"), dict) else None
        event = {"event_id": event_id, "webhook_type": str(body.get("webhook_type"))[:64],
                 "session_id": as_uuid(body.get("session_id")), "vendor_data": as_uuid(body.get("vendor_data")),
                 "workflow_id": as_uuid(body.get("workflow_id")), "environment": self.environment,
                 "status": status if status in STATUSES else None, "checks": None, "outcome": "pending",
                 "provider_at": datetime.fromtimestamp(created_at, UTC) if fresh_int(created_at) else None}
        if event["webhook_type"] not in HANDLED_TYPES:
            event["outcome"] = "ignored"
        elif event["status"] is None:
            event["outcome"] = "unknown_status"  # never applied, never approves
        elif event["session_id"] is None or event["provider_at"] is None:
            event["outcome"] = "incomplete"
        elif event["vendor_data"] is None or event["workflow_id"] != as_uuid(self.settings.didit_workflow_id):
            event["outcome"] = "identity_mismatch"
        elif status == "Approved" and not (decision and as_uuid(decision.get("session_id")) == event["session_id"]
                                           and decision.get("status") == "Approved"):
            event["outcome"] = "incomplete"  # an approval must carry its matching decision
        else:
            event["checks"] = check_statuses(decision) if decision else None
        outcome = await self.repo.record_webhook(event)
        if outcome in ("identity_mismatch", "unknown_status", "incomplete"):
            security_event("kyc_webhook_not_applied", reason=outcome, client_ip=ip)
        return {"status": outcome}

    # ── admin ─────────────────────────────────────────────────────────────
    async def admin_view(self, user_id: str) -> dict:
        sessions = await self.repo.sessions(user_id)
        return {**summary(sessions, self.environment),
                "sessions": [{**s, **{k: _iso(s[k]) for k in ("provider_updated_at", "approved_at", "created_at",
                                                              "updated_at")}} for s in sessions],
                "events": [{**e, "created_at": _iso(e["created_at"])} for e in await self.repo.events(user_id)]}

    async def admin_refresh(self, admin: User, user_id: str, ip: str | None) -> dict:
        """Reconcile the user's newest session with Didit's decision endpoint (two attempts at most; no polling).
        This is the recovery for a webhook that never arrived; it can record any Didit status, Approved included,
        but only what Didit itself reports."""
        self._available()
        await self._limit(f"admin:{admin.id}", RATE_REFRESH)
        sessions = [s for s in await self.repo.sessions(user_id) if s["environment"] == self.environment]
        if not sessions:
            raise ApiError(404, "This user has no verification session", "kyc_no_session")
        session_id = sessions[0]["session_id"]
        for attempt in (1, 2):
            try:
                decision = await self.client.decision(session_id)
                break
            except DiditError as exc:
                if attempt == 2 or exc.reason not in ("timeout", "network"):
                    security_event("kyc_refresh_failed", reason=exc.reason, user_id=admin.id, client_ip=ip)
                    raise ApiError(503, UNAVAILABLE, "kyc_provider_unavailable") from None
                await asyncio.sleep(1)
        result = await self.repo.refresh(session_id, decision["status"], check_statuses(decision), admin.id)
        security_event("kyc_refreshed", level=20, user_id=admin.id, target_user_id=user_id, reason=result,
                       client_ip=ip)
        return await self.admin_view(user_id)


def fresh_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
