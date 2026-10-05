"""Authentication use-cases. Routers stay thin; every rule lives here."""

import ipaddress
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.auth.email import DisabledSender, EmailSender, ResendSender, render
from app.auth.google import GoogleAuthError, GoogleOAuth
from app.auth.passwords import hash_password, needs_rehash, validate_new_password, verify_password
from app.auth.repository import AuthRepository, InviteConflict, Session, User
from app.auth.tokens import csrf_token_for, new_hex_token, new_token, sha256_hex
from app.core.config import Settings
from app.security.audit import security_event

GENERIC_LOGIN_ERROR = "Invalid email or password"
TOO_MANY = "Too many attempts. Please try again later."


class AuthError(Exception):
    def __init__(self, status: int, error: str, reason: str) -> None:
        super().__init__(reason)
        self.status, self.error, self.reason = status, error, reason


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf_token: str
    session: Session


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
    async def issue_session(self, user: User, ip: str | None, user_agent: str | None) -> IssuedSession:
        secret = self.secret
        token = new_token(32)
        session_id = str(uuid.uuid4())
        csrf = csrf_token_for(secret, session_id)
        s = self.settings
        session = await self.repo.create_session(
            session_id, user.id, sha256_hex(token), sha256_hex(csrf),
            sha256_hex(user_agent) if user_agent else None, _valid_ip(ip), s.session_idle_days,
            s.session_absolute_days)
        return IssuedSession(token=token, csrf_token=csrf, session=session)

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
        if not await self.repo.hit_rate_limit(f"ip:{ip}", "auth:login", 20, 15 * 60):
            security_event("login_rate_limited", reason="ip_limit", client_ip=ip)
            raise AuthError(429, TOO_MANY, "ip_rate_limited")
        email = (email or "").strip()
        user = await self.repo.user_by_email(email) if email else None
        now = datetime.now(UTC)
        if user and user.locked_until and user.locked_until > now:
            verify_password(password or "", None)  # same work as a real attempt
            security_event("login_rejected", reason="locked", user_id=user.id, client_ip=ip)
            raise AuthError(429, TOO_MANY, "locked")
        ok = verify_password(password or "", user.password_hash if user else None)
        if not user or not user.is_active or not ok:
            if user and user.is_active and user.password_hash:
                await self.repo.record_failed_login(user.id, self.settings.login_lockout_threshold,
                                                    self.settings.login_lockout_minutes)
            security_event("login_rejected", reason="bad_credentials" if user else "unknown_account",
                           user_id=user.id if user else None, client_ip=ip)
            raise AuthError(401, GENERIC_LOGIN_ERROR, "bad_credentials")
        new_hash = hash_password(password) if needs_rehash(user.password_hash) else None
        await self.repo.record_successful_login(user.id, new_hash)
        issued = await self.issue_session(user, ip, user_agent)
        security_event("login_succeeded", level=20, reason="password", user_id=user.id, client_ip=ip)
        return user, issued

    async def logout(self, session: Session) -> None:
        await self.repo.revoke_session(session.id)

    async def logout_all(self, user: User) -> int:
        return await self.repo.revoke_all_sessions(user.id)

    # ── password reset / change ────────────────────────────────────────────
    async def request_reset(self, email: str, ip: str | None) -> None:
        """Always succeeds from the caller's point of view (no account enumeration)."""
        email = (email or "").strip()
        if not await self.repo.hit_rate_limit(f"ip:{ip}", "auth:reset", 10, 3600):
            raise AuthError(429, TOO_MANY, "ip_rate_limited")
        if not email or not await self.repo.hit_rate_limit(f"email:{sha256_hex(email.lower())}",
                                                           "auth:reset", 3, 3600):
            return
        user = await self.repo.user_by_email(email)
        if not user or not user.is_active:
            return
        token = new_hex_token(32)
        await self.repo.create_reset_token(user.id, sha256_hex(token), self.settings.reset_token_ttl_minutes)
        url = f"{self.settings.frontend_url.rstrip('/')}/reset-password?token={token}"
        await self.sender.send(render("reset-password", user.email, url))
        security_event("password_reset_requested", level=20, user_id=user.id, client_ip=ip)

    async def reset_password(self, token: str, new_password: str, ip: str | None) -> None:
        self._check_new_password(new_password)
        if not await self.repo.hit_rate_limit(f"ip:{ip}", "auth:reset-confirm", 20, 3600):
            raise AuthError(429, TOO_MANY, "ip_rate_limited")
        user_id = await self.repo.consume_reset_token(sha256_hex(token or ""))
        if not user_id:
            raise AuthError(400, "Invalid or expired reset link. Please request a new one.", "bad_reset_token")
        await self.repo.set_password(user_id, hash_password(new_password))
        revoked = await self.repo.revoke_all_sessions(user_id)
        security_event("password_reset_completed", level=20, user_id=user_id, client_ip=ip,
                       reason=f"sessions_revoked:{revoked}")

    async def change_password(self, user: User, session: Session, current: str, new_password: str) -> None:
        self._check_new_password(new_password)
        if not verify_password(current or "", user.password_hash):
            raise AuthError(400, "Current password is incorrect", "bad_current_password")
        await self.repo.set_password(user.id, hash_password(new_password))
        await self.repo.revoke_all_sessions(user.id, except_session_id=session.id)

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
            user = await self.repo.accept_invite(sha256_hex(token or ""), hash_password(password), username)
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
        await self.repo.link_identity(user.id, "google", identity.subject, identity.email)
        await self.repo.record_successful_login(user.id, None)
        security_event("login_succeeded", level=20, reason="google", user_id=user.id, client_ip=ip)
        return user, await self.issue_session(user, ip, user_agent)
