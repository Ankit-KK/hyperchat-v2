"""Owner identity verification with Didit (KYC PR3; migration 0022). See app.kyc.didit for the provider contract.

KYC belongs to the USER (users.id = Didit vendor_data), never to a creator: a transfer never moves it, and one verified
person keeps it for every creator they own. Only a creator's current owner may start a verification (a linked
moderator or a user without an owned creator may not). This PR only records verification; it never activates a
creator and does not change donation eligibility (PR4 wires user_kyc_approved into activation and enforcement).

The API's database role only reads the KYC tables; every write is a SECURITY DEFINER function (migration 0022):
* webhooks: public.kyc_receive_webhook verifies X-Signature-V2 in the database, with a secret this process never
  holds, and applies only what the signed body says. It is the only way to become approved.
* sessions: public.kyc_create_session (the configured workflow / environment only; applies early webhooks).
* admin refresh: public.kyc_apply_refresh records Didit's decision endpoint, which is not signed, so it can never
  approve; it is dated when Didit was asked, so a slow refresh never outranks a newer webhook.
Provider order is kept by the provider's own update time: a delayed old event never overwrites a newer one, while a
later expiry or decline always replaces an approval.
"""

import asyncio
import json

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.auth.repository import User
from app.core.config import Settings
from app.core.errors import ApiError
from app.kyc.didit import CreatedSession, DiditClient, DiditError, as_uuid, canonical, check_statuses
from app.security.audit import security_event

RATE_START = ("kyc:session", 5, 3600)       # per user
RATE_REFRESH = ("kyc:refresh", 30, 3600)    # per admin
STATE = {"Not Started": "in_progress", "In Progress": "in_progress", "Resubmitted": "in_progress",
         "Awaiting User": "in_progress", "In Review": "in_review", "Declined": "declined", "Kyc Expired": "expired",
         "Expired": "not_completed", "Abandoned": "not_completed", "Approved": "approved"}
UNAVAILABLE = "Identity verification is unavailable right now. Please try again later."
# kyc_receive_webhook refusals -> (HTTP status, message, log code). Didit retries 5xx and 404 only.
REFUSED = {"not_configured": (503, UNAVAILABLE, "kyc_not_configured"),
           "bad_signature": (401, "Invalid signature", "kyc_webhook_bad_signature"),
           "stale_timestamp": (401, "Stale or missing timestamp", "kyc_webhook_stale"),
           "malformed": (400, "Malformed payload", "kyc_webhook_malformed"),
           "wrong_application": (403, "Not this environment's Didit application", "kyc_webhook_wrong_application")}
_SESSION_COLS = """session_id::text, workflow_id::text, workflow_version, environment, provider_status,
    provider_updated_at, check_statuses, approved_at, created_at, updated_at"""


def _iso(value):
    return value.isoformat() if value is not None else None


def summary(sessions: list[dict], environment: str | None, approved: bool) -> dict:
    """The user's KYC state from their sessions in this environment (newest first). `approved` is always
    public.user_kyc_approved (the one rule); an Approved newest session that the rule does not accept (a later
    revocation or expiry elsewhere, or a previous workflow) needs re-verification."""
    mine = [s for s in sessions if s["environment"] == environment]
    latest = mine[0] if mine else None
    if approved:
        state = "approved"
    elif latest is None:
        state = "none"
    elif latest["provider_status"] == "Approved":
        state = "reverification_required"
    else:
        state = STATE[latest["provider_status"]]
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
        """Store a session Didit returned (idempotent) and apply the signed webhooks that arrived before it."""
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT public.kyc_create_session(%s, %s, %s, %s, %s) AS r",
                              (user_id, created.session_id, workflow_id, created.workflow_version, environment))
            result = (await cur.fetchone())["r"]
            if result != "ok":
                raise DiditError(f"session_{result}")  # never handed out: not this user's, or not this configuration
            await cur.execute(f"SELECT {_SESSION_COLS} FROM public.kyc_sessions WHERE session_id = %s",
                              (created.session_id,))
            return await cur.fetchone()

    async def receive_webhook(self, canonical_body: str, signature: str | None, x_timestamp: str | None) -> str:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("SELECT public.kyc_receive_webhook(%s, %s, %s)", (canonical_body, signature, x_timestamp))
            return (await cur.fetchone())[0]

    async def clock(self):
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("SELECT clock_timestamp()")
            return (await cur.fetchone())[0]

    async def refresh(self, session_id: str, status: str, checks: dict, observed_at, actor_id: str) -> str:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("SELECT public.kyc_apply_refresh(%s, %s, %s, %s, %s)",
                              (session_id, status, Jsonb(checks), observed_at, actor_id))
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

    async def _summary(self, user_id: str) -> dict:
        return summary(await self.repo.sessions(user_id), self.environment, await self.kyc_approved(user_id))

    async def status(self, user: User) -> dict:
        owns = await self.repo.owns_creator(user.id)
        state = await self._summary(user.id)
        return {**state, "owns_creator": owns, "available": self.client is not None,
                "can_start": owns and self.client is not None and state["state"] not in ("approved", "in_review")}

    async def start(self, user: User, ip: str | None) -> dict:
        """Start (or resume: Didit returns the open session) the signed-in owner's verification. Answers only the
        hosted URL and the session's safe metadata."""
        self._available()
        if not await self.repo.owns_creator(user.id):
            raise ApiError(403, "Only a creator's owner can verify their identity here", "not_creator_owner")
        await self._limit(f"user:{user.id}", RATE_START)
        state = await self._summary(user.id)
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
        """Canonicalize the body (what Didit signed) and hand it to the database, which verifies and applies it."""
        try:
            body = json.loads(raw)
            text = canonical(body).decode("utf-8") if isinstance(body, dict) else None
        except (ValueError, UnicodeError, RecursionError):
            text = None
        if text is None:
            raise ApiError(400, "Malformed payload", "kyc_webhook_malformed")
        outcome = await self.repo.receive_webhook(text, signature, x_timestamp)
        if outcome in REFUSED:
            code, message, reason = REFUSED[outcome]
            if outcome != "not_configured":
                security_event("kyc_webhook_rejected", reason=outcome, client_ip=ip)
            raise ApiError(code, message, reason)
        if outcome in ("identity_mismatch", "unknown_status", "incomplete"):
            security_event("kyc_webhook_not_applied", reason=outcome, client_ip=ip)
        return {"status": outcome}

    # ── admin ─────────────────────────────────────────────────────────────
    async def admin_view(self, user_id: str) -> dict:
        sessions = await self.repo.sessions(user_id)
        return {**summary(sessions, self.environment, await self.kyc_approved(user_id)),
                "sessions": [{**s, **{k: _iso(s[k]) for k in ("provider_updated_at", "approved_at", "created_at",
                                                              "updated_at")}} for s in sessions],
                "events": [{**e, "created_at": _iso(e["created_at"])} for e in await self.repo.events(user_id)]}

    async def admin_refresh(self, admin: User, user_id: str, ip: str | None) -> dict:
        """Reconcile the user's newest session with Didit's decision endpoint (two attempts at most; no polling).
        The recovery for a webhook that never arrived. The response is not signed, so it never approves: an Approved
        answer is reported as 'approval_needs_webhook' (Didit's own signed webhook, or its retry, approves)."""
        self._available()
        await self._limit(f"admin:{admin.id}", RATE_REFRESH)
        sessions = [s for s in await self.repo.sessions(user_id) if s["environment"] == self.environment]
        if not sessions:
            raise ApiError(404, "This user has no verification session", "kyc_no_session")
        session_id = sessions[0]["session_id"]
        asked_at = await self.repo.clock()  # the refresh describes this moment, however long the call takes
        for attempt in (1, 2):
            try:
                decision = await self.client.decision(session_id)
                break
            except DiditError as exc:
                if attempt == 2 or exc.reason not in ("timeout", "network"):
                    security_event("kyc_refresh_failed", reason=exc.reason, user_id=admin.id, client_ip=ip)
                    raise ApiError(503, UNAVAILABLE, "kyc_provider_unavailable") from None
                await asyncio.sleep(1)
        result = await self.repo.refresh(session_id, decision["status"], check_statuses(decision), asked_at,
                                         admin.id)
        security_event("kyc_refreshed", level=20, user_id=admin.id, target_user_id=user_id, reason=result,
                       client_ip=ip)
        return {**await self.admin_view(user_id), "refresh": result}
