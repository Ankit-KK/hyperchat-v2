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

Several Didit accounts (migration 0040, docs/kyc.md "Didit accounts"): the database assigns each user's account
(public.kyc_assign_account: a new user by rotation, everyone else the account of their first session, never re-routed)
and its workflow; this process holds only the accounts' API keys (HYPERCHAT_DIDIT_API_KEY for 'primary',
HYPERCHAT_DIDIT_EXTRA_API_KEYS for the others). A refused assignment or a missing key answers 503 and raises an
operator alert (kind kyc_account_unavailable).

PAN (migration 0041, docs/kyc.md "PAN evidence"): before a session starts the owner acknowledges PAN_ACK_TEXT
(POST /api/v1/me/kyc/pan-acknowledgement); a start needs an acknowledgement from the last 60 minutes, which is bound
to the session in the same transaction as the session write. The database derives pan_status from the signed
decision's documents and keeps the payout hold for a non-PAN approval; this process only reads both.
"""

import asyncio
import hashlib
import json
import logging

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.auth.repository import User
from app.core.config import Settings
from app.core.errors import ApiError
from app.kyc.didit import CreatedSession, DiditClient, DiditError, as_uuid, canonical, check_statuses
from app.security.audit import security_event
from app.stepup.grants import consume

log = logging.getLogger("hyperchat.kyc")
RATE_START = ("kyc:session", 5, 3600)       # per user
RATE_REFRESH = ("kyc:refresh", 30, 3600)    # per admin
RATE_ACK = ("kyc:pan-ack", 20, 3600)        # per user
# The owner's wording (2026-11-27), shown before the Didit redirect; the stored hash proves which text was accepted.
PAN_ACK_TEXT = ("Only use PAN card for verification, otherwise your payout can be put on hold if found using "
                "different ID")
PAN_ACK_VERSION = "pan-only-2026-11-27"
PAN_ACK_SHA256 = hashlib.sha256(PAN_ACK_TEXT.encode("utf-8")).hexdigest()
PAN_ACK_MINUTES = 60
ACK_REQUIRED = "Please confirm that you will verify with your PAN card first."
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
_SESSION_COLS = """session_id::text, workflow_id::text, workflow_version, environment, account_id, provider_status,
    provider_updated_at, check_statuses, approved_at, created_at, updated_at"""


def _iso(value):
    return value.isoformat() if value is not None else None


def summary(sessions: list[dict], environment: str | None, effective: tuple[bool, str | None]) -> dict:
    """The user's KYC state. `effective` is public.kyc_effective (the one rule): (approved, the CURRENT session, i.e.
    the newest of the configured workflow and its epoch). Sessions of an obsolete workflow never decide the state, and
    an Approved current session the rule does not accept (a later revocation or expiry) needs re-verification."""
    approved, current = effective
    mine = [s for s in sessions if s["environment"] == environment]
    latest = next((s for s in mine if s["session_id"] == current), None)
    if approved:
        state = "approved"
    elif latest is None:
        state = "reverification_required" if mine else "none"  # only obsolete-workflow sessions
    elif latest["provider_status"] == "Approved":
        state = "reverification_required"
    else:
        state = STATE[latest["provider_status"]]
    return {"approved": approved, "state": state,
            "provider_status": latest["provider_status"] if latest else None,
            "updated_at": _iso(latest["updated_at"]) if latest else None}


class _Rollback(Exception):
    """Leaves a transaction with a refusal code (the grant consumed in it is rolled back too)."""


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

    async def effective(self, user_id: str, environment: str) -> tuple[bool, str | None]:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("SELECT approved, session_id::text FROM public.kyc_effective(%s, %s)", (user_id, environment))
            approved, session_id = await cur.fetchone()
            return approved, session_id

    async def assign_account(self, user_id: str) -> tuple[str, str | None, str | None]:
        """(result, account id, workflow id) from public.kyc_assign_account (migration 0040)."""
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("SELECT o_result, o_account, o_workflow::text FROM public.kyc_assign_account(%s)",
                              (user_id,))
            return tuple(await cur.fetchone())

    async def record_session(self, user_id: str, created: CreatedSession, workflow_id: str, environment: str,
                             account: str = "primary") -> dict:
        """Store a session Didit returned (idempotent) and apply the signed webhooks that arrived before it."""
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT public.kyc_create_session(%s, %s, %s, %s, %s, %s) AS r",
                              (user_id, created.session_id, workflow_id, created.workflow_version, environment,
                               account))
            result = (await cur.fetchone())["r"]
            if result != "ok":
                raise DiditError(f"session_{result}")  # never handed out: not this user's, or not this configuration
            # 0041: the PAN acknowledgement this session was started under (a resume keeps its earlier binding)
            await cur.execute(
                "UPDATE public.kyc_pan_acknowledgements SET session_id = %(s)s, bound_at = now() WHERE id = ("
                "SELECT id FROM public.kyc_pan_acknowledgements WHERE user_id = %(u)s AND session_id IS NULL "
                f"AND acknowledged_at > now() - interval '{PAN_ACK_MINUTES} minutes' ORDER BY acknowledged_at DESC "
                "LIMIT 1) AND session_id IS NULL AND NOT EXISTS (SELECT 1 FROM public.kyc_pan_acknowledgements "
                "WHERE user_id = %(u)s AND session_id = %(s)s)", {"s": created.session_id, "u": user_id})
            # (session_id IS NULL on the row itself: a concurrent start that bound it first makes this one skip it)
            await cur.execute("SELECT EXISTS (SELECT 1 FROM public.kyc_pan_acknowledgements WHERE user_id = %s "
                              "AND session_id = %s) AS ok", (user_id, created.session_id))
            if not (await cur.fetchone())["ok"]:
                raise DiditError("pan_acknowledgement_missing")  # rolls the session write back
            await cur.execute(f"SELECT {_SESSION_COLS} FROM public.kyc_sessions WHERE session_id = %s",
                              (created.session_id,))
            return await cur.fetchone()

    async def record_pan_ack(self, user_id: str) -> dict:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("INSERT INTO public.kyc_pan_acknowledgements (user_id, text_version, text_sha256) "
                              "VALUES (%s, %s, %s) RETURNING id::text, acknowledged_at",
                              (user_id, PAN_ACK_VERSION, PAN_ACK_SHA256))
            return await cur.fetchone()

    async def has_recent_pan_ack(self, user_id: str) -> bool:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("SELECT EXISTS (SELECT 1 FROM public.kyc_pan_acknowledgements WHERE user_id = %s "
                              f"AND acknowledged_at > now() - interval '{PAN_ACK_MINUTES} minutes')", (user_id,))
            return (await cur.fetchone())[0]

    async def pan(self, user_id: str, environment: str) -> dict:
        """The database's PAN view (migration 0041): pan_status and the open payout hold."""
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT public.kyc_pan_status(%s, %s) AS pan_status", (user_id, environment))
            status = (await cur.fetchone())["pan_status"]
            await cur.execute("SELECT reason, since FROM public.kyc_payout_hold(%s)", (user_id,))
            hold = await cur.fetchone()
        return {"pan_status": status,
                "payout_hold": {"held": hold is not None, "reason": hold["reason"] if hold else None,
                                "since": _iso(hold["since"]) if hold else None}}

    async def open_hold(self, user_id: str) -> str | None:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("SELECT id::text FROM public.kyc_payout_holds WHERE user_id = %s AND cleared_at IS NULL",
                              (user_id,))
            row = await cur.fetchone()
            return row[0] if row else None

    async def clear_hold(self, hold_id: str, actor_id: str, reason: str, grant) -> str:
        """public.kyc_clear_payout_hold in one transaction with the step-up grant's consumption (SEC-A style)."""
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            await consume(cur, grant)
            await cur.execute("SELECT public.kyc_clear_payout_hold(%s, %s, %s)", (hold_id, actor_id, reason))
            result = (await cur.fetchone())[0]
            if result != "cleared":
                raise _Rollback(result)  # a refused clearance never spends the grant
            return result

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
    def __init__(self, repo: KycRepository, settings: Settings, *, client: DiditClient | None, limiter,
                 clients: dict | None = None, alerts=None) -> None:
        """`client` is the 'primary' account's; `clients` adds the others ({account id: client}, migration 0040)."""
        self.repo, self.settings, self.limiter, self.alerts = repo, settings, limiter, alerts
        self.clients = {**({"primary": client} if client is not None else {}), **(clients or {})}
        self.client = client if client is not None else next(iter(self.clients.values()), None)

    @classmethod
    def from_settings(cls, repo: KycRepository, settings: Settings, limiter, alerts=None) -> "KycService":
        if not settings.didit_environment:  # no environment: KYC is unavailable (503), as before 0040
            return cls(repo, settings, client=None, limiter=limiter, alerts=alerts)
        keys = settings.didit_api_keys()
        primary = None
        if settings.didit_configured:  # the primary's key, workflow and environment; it may be absent (disabled)
            primary = DiditClient(base=settings.didit_api_base, api_key=keys["primary"],
                                  workflow_id=as_uuid(settings.didit_workflow_id.strip()))
        keys.pop("primary", None)
        others = {account: DiditClient(base=settings.didit_api_base, api_key=key) for account, key in keys.items()}
        return cls(repo, settings, client=primary, limiter=limiter, clients=others, alerts=alerts)

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
        return summary(await self.repo.sessions(user_id), self.environment, await self._effective(user_id))

    async def _effective(self, user_id: str) -> tuple[bool, str | None]:
        return await self.repo.effective(user_id, self.environment) if self.environment else (False, None)

    async def _pan(self, user_id: str) -> dict:
        if not self.environment:
            return {"pan_status": "not_approved", "payout_hold": {"held": False, "reason": None, "since": None}}
        return await self.repo.pan(user_id, self.environment)

    async def status(self, user: User) -> dict:
        owns = await self.repo.owns_creator(user.id)
        state = await self._summary(user.id)
        return {**state, "owns_creator": owns, "available": self.client is not None,
                "can_start": owns and self.client is not None and state["state"] not in ("approved", "in_review"),
                **await self._pan(user.id),
                "pan_acknowledgement": {"text": PAN_ACK_TEXT, "text_version": PAN_ACK_VERSION,
                                        "current": await self.repo.has_recent_pan_ack(user.id)}}

    async def acknowledge_pan(self, user: User, ip: str | None) -> dict:
        """The owner ticked the PAN-only notice before the Didit redirect (owner decision 2026-11-27). Kept as
        evidence (who, when, which text, and later the session it was for); valid for a start within 60 minutes."""
        if not await self.repo.owns_creator(user.id):
            raise ApiError(403, "Only a creator's owner can verify their identity here", "not_creator_owner")
        await self._limit(f"user:{user.id}", RATE_ACK)
        row = await self.repo.record_pan_ack(user.id)
        security_event("kyc_pan_acknowledged", level=20, user_id=user.id, client_ip=ip)
        return {"acknowledged_at": _iso(row["acknowledged_at"]), "text_version": PAN_ACK_VERSION, "text": PAN_ACK_TEXT,
                "valid_minutes": PAN_ACK_MINUTES}

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
        if not await self.repo.has_recent_pan_ack(user.id):  # 0041: the PAN notice comes first
            raise ApiError(409, ACK_REQUIRED, "pan_acknowledgement_required")
        callback = f"{self.settings.dashboard_link_url}/kyc/return"  # never from the request
        account, workflow, client = await self._account(user.id, ip)
        try:
            created = await client.create_session(user.id, callback, workflow_id=workflow)
            row = await self.repo.record_session(user.id, created, workflow, self.environment, account)
        except DiditError as exc:
            if exc.reason == "session_account_at_cap":  # Didit made a NEW session although only a resume fits the cap
                raise await self._unavailable("account_at_cap", user.id, ip, account) from None
            if exc.reason == "pan_acknowledgement_missing":  # a NEW session needs a fresh acknowledgement
                raise ApiError(409, ACK_REQUIRED, "pan_acknowledgement_required") from None
            security_event("kyc_session_failed", reason=exc.reason, user_id=user.id, client_ip=ip)
            raise ApiError(503, UNAVAILABLE, "kyc_provider_unavailable") from None
        security_event("kyc_session_started", level=20, user_id=user.id, client_ip=ip)
        return {"session_id": row["session_id"], "status": row["provider_status"], "url": created.url}

    async def _unavailable(self, code: str, user_id: str, ip: str | None, account: str | None) -> ApiError:
        """A user whose Didit account cannot take a session: 503, a security event and an operator alert."""
        security_event("kyc_account_unavailable", reason=code, user_id=user_id, client_ip=ip)
        if self.alerts is not None:
            try:
                await self.alerts.record(kind="kyc_account_unavailable", source="kyc", code=code,
                                         account=account or "", user_id=user_id)
            except Exception as exc:  # noqa: BLE001 - alerting never fails what it reports on (app.alerts)
                log.warning("alert not recorded", extra={"kind": "kyc_account_unavailable",
                                                         "error": type(exc).__name__})
        return ApiError(503, UNAVAILABLE, "kyc_account_unavailable")

    async def _account(self, user_id: str, ip: str | None) -> tuple[str, str, DiditClient]:
        """The user's Didit account (migration 0040: assigned and pinned by the database), its workflow and client."""
        result, account, workflow = await self.repo.assign_account(user_id)
        if result != "ok":
            if result == "not_configured":
                raise ApiError(503, UNAVAILABLE, "kyc_not_configured")
            raise await self._unavailable(result, user_id, ip, account)
        client = self.clients.get(account)
        if client is None:
            raise await self._unavailable("account_key_missing", user_id, ip, account)
        if account == "primary" and client.workflow_id and workflow != client.workflow_id:
            raise await self._unavailable("primary_workflow_mismatch", user_id, ip, account)  # settings vs database
        return account, workflow, client

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
        return {**summary(sessions, self.environment, await self._effective(user_id)), **await self._pan(user_id),
                "sessions": [{**s, **{k: _iso(s[k]) for k in ("provider_updated_at", "approved_at", "created_at",
                                                              "updated_at")}} for s in sessions],
                "events": [{**e, "created_at": _iso(e["created_at"])} for e in await self.repo.events(user_id)]}

    async def admin_clear_payout_hold(self, admin: User, user_id: str, hold_id: str, reason: str | None,
                                      ip: str | None, grant=None) -> dict:
        """Owner decision 2026-11-27 (option C): an administrator resolves a payout hold, with a reason, under the
        step-up action kyc.payout_hold.clear. The database checks the admin, records who, when and why, and claims
        the grant at its own write (migration 0041)."""
        reason = " ".join((reason or "").split())
        if not reason or len(reason) > 500:
            raise ApiError(400, "A reason (at most 500 characters) is required", "reason_required")
        try:
            await self.repo.clear_hold(hold_id, admin.id, reason, grant)
        except _Rollback as refused:
            code = str(refused)
            raise ApiError(409 if code == "not_open" else 403 if code == "not_authorized" else 400,
                           {"not_open": "This payout hold is not open", "not_authorized": "Admins only",
                            "reason_required": "A reason is required"}.get(code, "Refused"), code) from None
        security_event("kyc_payout_hold_cleared", level=20, user_id=admin.id, target_user_id=user_id, client_ip=ip)
        return await self.admin_view(user_id)

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
        client = self.clients.get(sessions[0].get("account_id") or "primary")  # the session's own account (0040)
        if client is None:
            raise ApiError(503, UNAVAILABLE, "kyc_account_unavailable")
        asked_at = await self.repo.clock()  # the refresh describes this moment, however long the call takes
        for attempt in (1, 2):
            try:
                decision = await client.decision(session_id)
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
