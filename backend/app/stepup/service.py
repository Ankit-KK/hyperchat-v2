"""The API's passkey endpoints (S2): the authenticated administrator and session come from the server-side session
(never the client), every call goes to the isolated verifier over its Unix socket, and the verifier and the database
decide (app.stepup.verifier, migration 0025). This module only forwards, rate-limits and maps errors; it fails
closed: no verifier means no registration, no assurance and no grant."""

import logging

from app.auth.dependencies import CurrentUser
from app.security.audit import security_event
from app.stepup.client import StepupClient, StepupRefused, StepupUnavailable
from app.stepup.grants import StepUpError

log = logging.getLogger("hyperchat.stepup")
# (bucket, limit, window seconds), per administrator and per client IP, counted before calling the verifier (which
# keeps its own per-administrator limits as well). "auth:" prefix: the shared rate_limits housekeeping.
LIMITS = {"user": ("auth:mfa", 60, 600), "ip": ("auth:mfa-ip", 120, 600)}
MESSAGES = {
    "not_admin": "Admin access required", "session_invalid": "Your session is no longer valid",
    "no_active_credential": "No approved passkey: register one and have it approved first",
    "challenge_not_found": "Unknown or expired request: start again", "challenge_used": "Already used: start again",
    "challenge_expired": "Expired: start again", "verification_failed": "The passkey response could not be verified",
    "cross_origin": "Passkey requests from embedded frames are refused", "unknown_credential": "Unknown passkey",
    "credential_pending": "This passkey is not approved yet", "credential_revoked": "This passkey was revoked",
    "already_registered": "This passkey is already registered", "rate_limited": "Too many attempts: try later",
    "bad_action": "Unknown action or malformed request", "malformed_credential": "Malformed passkey response",
}


class MfaService:
    def __init__(self, client: StepupClient | None, repo) -> None:
        self.client = client
        self.repo = repo  # AuthRepository: rate limits and the step-up state (read-only)

    async def _limit(self, current: CurrentUser, ip: str | None) -> None:
        for kind, key in (("user", f"user:{current.user.id}"), ("ip", f"ip:{ip or 'unknown'}")):
            bucket, limit, window = LIMITS[kind]
            if not await self.repo.hit_rate_limit(key, bucket, limit, window):
                security_event("mfa_rate_limited", user_id=current.user.id, client_ip=ip, reason=kind)
                raise StepUpError(429, MESSAGES["rate_limited"], "rate_limited")

    async def _call(self, method: str, current: CurrentUser, ip: str | None, *args):
        if self.client is None:
            raise StepUpError(503, "Passkey verification is not configured", "stepup_not_configured")
        await self._limit(current, ip)
        try:
            return await getattr(self.client, method)(current.user.id, current.session.id, *args)
        except StepupUnavailable:
            security_event("stepup_unavailable", status=503, user_id=current.user.id)
            raise StepUpError(503, "Passkey verification is unavailable: try again shortly", "stepup_unavailable") \
                from None
        except StepupRefused as exc:
            status = exc.status if exc.status in (400, 403, 404, 409, 429) else 502
            raise StepUpError(status, MESSAGES.get(exc.code, "Passkey request refused"), exc.code) from None

    async def status(self, current: CurrentUser) -> dict:
        """The session's assurance and the enforcement state (database), and this admin's passkeys (verifier;
        null with verifier "unavailable" when it cannot be reached: status never fails because of it)."""
        state, aal = await self.repo.stepup_session(current.session.id)
        credentials, verifier = None, "not_configured"
        if self.client is not None:
            try:
                credentials = (await self.client.list_credentials(current.user.id, current.session.id))["credentials"]
                verifier = "ok"
            except (StepupUnavailable, StepupRefused):
                verifier = "unavailable"
        return {"enforcement": state, "aal": aal, "mfa_required": state == "active" and aal < 2,
                "verifier": verifier, "credentials": credentials}

    async def credentials(self, current: CurrentUser, ip: str | None) -> dict:
        return await self._call("list_credentials", current, ip)

    async def registration_options(self, current: CurrentUser, ip: str | None) -> dict:
        return await self._call("registration_options", current, ip)

    async def registration_verify(self, current: CurrentUser, ip: str | None, challenge_id: str,
                                  credential: dict) -> dict:
        result = await self._call("registration_verify", current, ip, challenge_id, credential)
        security_event("passkey_registered", level=logging.INFO, user_id=current.user.id, client_ip=ip)
        return result

    async def assertion_options(self, current: CurrentUser, ip: str | None, action: str, target: str,
                                params: dict) -> dict:
        return await self._call("assertion_options", current, ip, action, target, params)

    async def assertion_verify(self, current: CurrentUser, ip: str | None, challenge_id: str, credential: dict) -> dict:
        result = await self._call("assertion_verify", current, ip, challenge_id, credential)
        security_event("passkey_verified", level=logging.INFO, user_id=current.user.id, client_ip=ip)
        return result
