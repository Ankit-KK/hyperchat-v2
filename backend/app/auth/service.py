"""Authentication use-cases. Routers stay thin; every rule lives here."""

import asyncio
import ipaddress
import uuid
from dataclasses import dataclass

from app.auth.email import DisabledSender, EmailSender, ResendSender, render
from app.auth.google import GoogleAuthError, GoogleOAuth
from app.auth.passwords import hash_password, needs_rehash, validate_new_password, verify_password
from app.auth.repository import AuthRepository, InviteConflict, Session, User
from app.auth.tokens import csrf_token_for, new_hex_token, new_token, sha256_hex
from app.core.config import Settings
from app.security.audit import security_event

# Security audit F2: every failed sign-in, including a throttled one, gets this one answer (status 401), so a response
# never tells whether the account exists, the password was right, or a limit was hit.
GENERIC_LOGIN_ERROR = "Invalid email or password, or too many attempts. Please try again later."
TOO_MANY = "Too many attempts. Please try again later."
# Security audit F2: layered sign-in throttling (no account-wide lockout). (key, bucket, attempts, window seconds).
# Every attempt is counted BEFORE the password check (atomic upsert in rate_limits), so concurrent requests cannot
# exceed a limit and a throttled request reveals nothing about the password. "account_ip" = the normalized email at
# one client IP: a failure from one IP never throttles the same account from another IP. The account/IP buckets
# are cleared by a successful sign-in; all windows expire on their own (rows are deleted by the daily cleanup).
LOGIN_LIMITS = (
    ("ip", "auth:login", 20, 15 * 60),
    ("ip", "auth:login-day", 100, 24 * 3600),
    ("account_ip", "auth:login-account", 5, 15 * 60),
    ("account_ip", "auth:login-account-day", 15, 24 * 3600),
)
PASSWORD_CHANGE_LIMIT = (5, 3600)  # current-password verifications per user per hour (security audit F3)


class AuthError(Exception):
    def __init__(self, status: int, error: str, reason: str) -> None:
        super().__init__(reason)
        self.status, self.error, self.reason = status, error, reason


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf_token: str
    session: Session


async def _verify(password: str, stored_hash: str | None) -> bool:
    """Argon2 / bcrypt (~50 ms) in the default thread pool (bounded), so the event loop keeps serving other requests
    while a password is checked (security audit F9)."""
    return await asyncio.to_thread(verify_password, password, stored_hash)


async def _hash(password: str) -> str:
    return await asyncio.to_thread(hash_password, password)


def _valid_ip(ip: str | None) -> str | None:
    try:
        return str(ipaddress.ip_address(ip)) if ip else None
    except ValueError:
        return None


def public_user(user: User) -> dict:
    return {"id": user.id, "email": user.email, "username": user.username, "role": user.role,
            "streamer_id": user.streamer_id}


class AuthService:
    def __init__(self, settings: Settings, repo: AuthRepository, sender: EmailSender,
                 google: GoogleOAuth | None) -> None:
        self.settings, self.repo, self.sender, self.google = settings, repo, sender, google

    @classmethod
    def from_settings(cls, settings: Settings, repo: AuthRepository) -> "AuthService":
        sender: EmailSender = DisabledSender()
        if settings.email_backend == "resend" and settings.resend_api_key:
            sender = ResendSender(settings.resend_api_key.get_secret_value(), settings.email_from)
        google = None
        if settings.google_enabled:
            google = GoogleOAuth(settings.google_client_id, settings.google_client_secret.get_secret_value(),
                                 settings.google_redirect_uri)
        return cls(settings, repo, sender, google)

    # ── secrets / origins ──────────────────────────────────────────────────
    @property
    def secret(self) -> str:
        if not self.settings.auth_secret:
            raise AuthError(503, "Authentication is not configured", "auth_secret_missing")
        return self.settings.auth_secret.get_secret_value()

    def origin_allowed(self, origin: str | None) -> bool:
        return bool(origin) and origin in self.settings.browser_origins

    def csrf_token(self, session: Session) -> str:
        return csrf_token_for(self.secret, session.id)

    # ── sessions ───────────────────────────────────────────────────────────
    def _new_session(self, user: User, ip: str | None, user_agent: str | None) -> tuple[str, str, tuple]:
        """(token, csrf token, the session row's insert parameters); nothing is stored yet."""
        token, session_id = new_token(32), str(uuid.uuid4())
        csrf = csrf_token_for(self.secret, session_id)
        s = self.settings
        return token, csrf, (session_id, user.id, sha256_hex(token), sha256_hex(csrf),
                             sha256_hex(user_agent) if user_agent else None, _valid_ip(ip), s.session_idle_days,
                             s.session_absolute_days)

    async def issue_session(self, user: User, ip: str | None, user_agent: str | None) -> IssuedSession:
        """Invite acceptance. Password and Google sign-in insert their sessions under the user-row lock instead
        (AuthRepository.password_sign_in / google_sign_in, security audit F10)."""
        token, csrf, row = self._new_session(user, ip, user_agent)
        return IssuedSession(token=token, csrf_token=csrf, session=await self.repo.create_session(*row))

    async def resolve(self, token: str | None) -> tuple[Session, User] | None:
        if not token:
            return None
        session = await self.repo.active_session(sha256_hex(token), self.settings.session_idle_days)
        if session is None:
            return None
        user = await self.repo.user_by_id(session.user_id)
        if user is None or not user.is_active:
            await self.repo.revoke_session(session.id)
            return None
        return session, user

    # ── login ──────────────────────────────────────────────────────────────
    async def login(self, email: str, password: str, ip: str | None, user_agent: str | None
                    ) -> tuple[User, IssuedSession]:
        email = (email or "").strip()
        keys = {"ip": f"ip:{ip}", "account_ip": f"acct:{sha256_hex(email.lower())}:ip:{ip}"}

        async def over(kind: str) -> list[str]:  # every bucket of this kind is counted, none skipped
            return [bucket for k, bucket, limit, window in LOGIN_LIMITS if k == kind
                    and not await self.repo.hit_rate_limit(keys[kind], bucket, limit, window)]
        if exceeded := await over("ip"):
            # Security audit F9: no password hashing once the IP is over its limit, so one client cannot make the
            # server hash without bound. Same answer as every failure; only the latency differs, and that tells
            # nothing about the account (the IP buckets are the same whatever account is tried). Accepted 2026-10-11.
            security_event("login_rate_limited", reason=exceeded[0], client_ip=ip)
            raise AuthError(401, GENERIC_LOGIN_ERROR, "rate_limited")
        if exceeded := await over("account_ip"):
            await _verify(password or "", None)  # account-specific throttling must cost what a real attempt costs
            security_event("login_rate_limited", reason=exceeded[0], client_ip=ip)
            raise AuthError(401, GENERIC_LOGIN_ERROR, "rate_limited")
        user = await self.repo.user_by_email(email) if email else None
        ok = await _verify(password or "", user.password_hash if user else None)
        if not user or not user.is_active or not ok:
            security_event("login_rejected", reason="bad_credentials" if user else "unknown_account",
                           user_id=user.id if user else None, client_ip=ip)
            raise AuthError(401, GENERIC_LOGIN_ERROR, "bad_credentials")
        new_hash = await _hash(password) if needs_rehash(user.password_hash) else None  # Argon2 before the lock
        token, csrf, row = self._new_session(user, ip, user_agent)
        session = await self.repo.password_sign_in(user.id, user.password_hash, new_hash, row)
        if session is None:
            # The password was reset or changed (or the account deactivated) after it was verified: no session.
            security_event("login_rejected", reason="password_changed_meanwhile", user_id=user.id, client_ip=ip)
            raise AuthError(401, GENERIC_LOGIN_ERROR, "bad_credentials")
        await self.repo.clear_rate_limits(keys["account_ip"], [b for k, b, _, _ in LOGIN_LIMITS if k == "account_ip"])
        issued = IssuedSession(token=token, csrf_token=csrf, session=session)
        security_event("login_succeeded", level=20, reason="password", user_id=user.id, client_ip=ip)
        return user, issued

    async def logout(self, session: Session) -> None:
        await self.repo.revoke_session(session.id)

    async def logout_all(self, user: User) -> int:
        return await self.repo.revoke_all_sessions(user.id)

    # ── password reset / change ────────────────────────────────────────────
    async def request_reset(self, email: str, ip: str | None, defer) -> None:
        """Always succeeds from the caller's point of view (no account enumeration). Security audit F11: the
        response path only applies the rate limits, which are the same for every email; the account lookup, the
        token and the email happen in `defer` (the route's BackgroundTasks.add_task), after the response is sent,
        so the response time no longer depends on whether the account exists."""
        email = (email or "").strip()
        if not await self.repo.hit_rate_limit(f"ip:{ip}", "auth:reset", 10, 3600):
            raise AuthError(429, TOO_MANY, "ip_rate_limited")
        if not email or not await self.repo.hit_rate_limit(f"email:{sha256_hex(email.lower())}",
                                                           "auth:reset", 3, 3600):
            return
        defer(self.send_reset_link, email, ip)

    async def send_reset_link(self, email: str, ip: str | None) -> None:
        """After the response: create the token and send the link to an existing active account. Never raises (there
        is no caller left to tell); a failure is logged without the email address, token or link."""
        user = None
        try:
            user = await self.repo.user_by_email(email)
            if not user or not user.is_active:
                return
            token = new_hex_token(32)
            await self.repo.create_reset_token(user.id, sha256_hex(token), self.settings.reset_token_ttl_minutes)
            url = f"{self.settings.frontend_url.rstrip('/')}/reset-password?token={token}"
            await self.sender.send(render("reset-password", user.email, url))
            security_event("password_reset_requested", level=20, user_id=user.id, client_ip=ip)
        except Exception as exc:  # noqa: BLE001 - background work: log and stop, never propagate
            security_event("password_reset_email_failed", level=40, reason=type(exc).__name__,
                           user_id=user.id if user else None, client_ip=ip)

    async def reset_password(self, token: str, new_password: str, ip: str | None) -> None:
        self._check_new_password(new_password)
        if not await self.repo.hit_rate_limit(f"ip:{ip}", "auth:reset-confirm", 20, 3600):
            raise AuthError(429, TOO_MANY, "ip_rate_limited")
        new_hash = await _hash(new_password)  # before any row lock (security audit F10)
        done = await self.repo.reset_password(sha256_hex(token or ""), new_hash)  # also revokes every session
        if not done:
            raise AuthError(400, "Invalid or expired reset link. Please request a new one.", "bad_reset_token")
        user_id, revoked = done
        security_event("password_reset_completed", level=20, user_id=user_id, client_ip=ip,
                       reason=f"sessions_revoked:{revoked}")

    async def change_password(self, user: User, session: Session, current: str, new_password: str,
                              ip: str | None = None) -> None:
        self._check_new_password(new_password)
        # Security audit F3: bound guesses of the current password from a (possibly stolen) session. Every
        # verification counts, so once the limit is reached even the right password answers 429 (no oracle).
        if not await self.repo.hit_rate_limit(f"user:{user.id}", "auth:password-change", *PASSWORD_CHANGE_LIMIT):
            security_event("password_change_rate_limited", reason="user_limit", user_id=user.id, client_ip=ip)
            raise AuthError(429, TOO_MANY, "password_change_rate_limited")
        if not await _verify(current or "", user.password_hash):
            security_event("password_change_rejected", reason="bad_current_password", user_id=user.id, client_ip=ip)
            raise AuthError(400, "Current password is incorrect", "bad_current_password")
        new_hash = await _hash(new_password)  # before any row lock
        revoked = await self.repo.change_password(user.id, user.password_hash, new_hash, keep_session_id=session.id)
        if revoked is None:  # (other sessions are revoked in the same transaction as the password change)
            # The password changed since it was verified (a reset or another change finished first): that one wins.
            security_event("password_change_rejected", reason="password_changed_meanwhile", user_id=user.id,
                           client_ip=ip)
            raise AuthError(400, "Current password is incorrect", "bad_current_password")

    def _check_new_password(self, password: str) -> None:
        err = validate_new_password(password, self.settings.password_min_length, self.settings.password_max_length)
        if err:
            raise AuthError(400, err, "weak_password")

    # ── invites ────────────────────────────────────────────────────────────
    async def create_invite(self, admin: User, email: str, role: str, streamer_id: str | None) -> dict:
        email = (email or "").strip()
        if "@" not in email or len(email) > 254:
            raise AuthError(400, "A valid email is required", "bad_email")
        if role not in ("streamer", "user", "admin"):
            raise AuthError(400, "Invalid role", "bad_role")
        existing = await self.repo.user_by_email(email)
        if existing and existing.password_hash:
            raise AuthError(409, "An account with this email already exists", "account_exists")
        token = new_hex_token(32)
        row = await self.repo.create_invite(email, role, streamer_id, sha256_hex(token), admin.id,
                                            self.settings.invite_ttl_days)
        url = f"{self.settings.frontend_url.rstrip('/')}/invite?token={token}"
        await self.sender.send(render("invite", email, url))
        security_event("invite_created", level=20, user_id=admin.id)
        result = {"invite_id": row["id"], "expires_at": row["expires_at"].isoformat()}
        if self.settings.auth_return_invite_links:
            result["accept_url"] = url
        return result

    async def accept_invite(self, token: str, password: str, username: str | None, ip: str | None,
                            user_agent: str | None) -> tuple[User, IssuedSession]:
        self._check_new_password(password)
        try:
            user = await self.repo.accept_invite(sha256_hex(token or ""), await _hash(password), username)
        except InviteConflict:
            raise AuthError(409, "This account already exists. Please sign in.", "account_exists") from None
        if user is None:
            raise AuthError(400, "Invalid or expired invite link", "bad_invite_token")
        security_event("invite_accepted", level=20, user_id=user.id, client_ip=ip)
        return user, await self.issue_session(user, ip, user_agent)

    # ── Google ─────────────────────────────────────────────────────────────
    async def google_login(self, code: str, code_verifier: str, nonce: str, ip: str | None,
                           user_agent: str | None) -> tuple[User, IssuedSession]:
        if self.google is None:
            raise AuthError(404, "Google sign-in is not enabled", "google_disabled")
        try:
            identity = await self.google.exchange(code, code_verifier, nonce)
        except GoogleAuthError as exc:
            security_event("google_login_rejected", reason=exc.reason, client_ip=ip)
            raise AuthError(400, "Google sign-in failed", exc.reason) from None
        user = await self.repo.user_by_identity("google", identity.subject)
        if user is None:
            user = await self.repo.user_by_email(identity.email)
            if user is None:
                security_event("google_login_rejected", reason="no_account", client_ip=ip)
                raise AuthError(403, "No HyperChat account uses this Google address", "no_account")
        if not user.is_active:
            raise AuthError(403, "This account is disabled", "account_disabled")
        token, csrf, row = self._new_session(user, ip, user_agent)
        session = await self.repo.google_sign_in(user.id, identity.subject, identity.email, row)
        if session is None:  # deactivated after the check above: same answer, nothing written
            security_event("google_login_rejected", reason="account_disabled_meanwhile", user_id=user.id, client_ip=ip)
            raise AuthError(403, "This account is disabled", "account_disabled")
        security_event("login_succeeded", level=20, reason="google", user_id=user.id, client_ip=ip)
        return user, IssuedSession(token=token, csrf_token=csrf, session=session)
