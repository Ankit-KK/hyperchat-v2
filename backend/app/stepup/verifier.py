"""The step-up verifier: WebAuthn registration and assertion for administrators (docs/admin-stepup.md).

Runs only inside the verifier service (app.stepup.server), as the hyperchat_stepup database role, under its own OS
user. Everything it records is the consequence of a signature it verified itself:
  * a challenge is single-use and expires: verifying locks its row, and a used or expired one is refused, so two
    concurrent verifications of one challenge yield at most one success;
  * the signature must come from an ACTIVE passkey of the same administrator (pending or revoked never count),
    over the challenge the database stored, for the exact relying party ID and one of the exact origins, with
    user verification, outside a cross-origin frame; its sign count may only grow (compare-and-set);
  * a successful 'session' ceremony writes session assurance (aal2, at most 12 hours, never beyond the session);
    a successful 'action' ceremony writes ONE grant bound to administrator, session, passkey, action, target and
    parameter hash, valid for 2 minutes; the database repeats these checks (migration 0025);
  * a failed attempt burns the challenge, and attempts are rate-limited per administrator in the verifier's own
    table, before any signature is checked.
The API (a different OS user) reaches this only through the Unix socket; it supplies the administrator and session
ids, which bind nothing on their own: without the administrator's authenticator signing this exact canonical
action, no grant or assurance can be produced.
"""

import base64
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.security.audit import security_event
from app.stepup import canonical
from app.stepup.config import StepupSettings

log = logging.getLogger("hyperchat.stepup")
ALGORITHMS = [COSEAlgorithmIdentifier.ECDSA_SHA_256, COSEAlgorithmIdentifier.EDDSA,
              COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256]
# (bucket, limit, window seconds) per administrator, counted before any verification.
LIMITS = {"options": (30, 600), "register": (5, 3600), "assert": (10, 600)}


class StepupError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


def fingerprint(public_key: bytes) -> str:
    """What the administrator and the operator compare before approval: 100 bits of SHA-256 over the public key,
    base32 in five groups of four (second-preimage resistant for human comparison)."""
    text = base64.b32encode(hashlib.sha256(public_key).digest()).decode()[:20]
    return "-".join(text[i:i + 4] for i in range(0, 20, 4))


def _client_data(credential: dict) -> dict:
    """The clientDataJSON the browser produced; refuses a ceremony run inside a cross-origin frame."""
    try:
        raw = credential["response"]["clientDataJSON"]
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (KeyError, TypeError, ValueError):
        raise StepupError(400, "malformed_credential") from None
    if data.get("crossOrigin") is True or "topOrigin" in data:
        raise StepupError(400, "cross_origin")
    return data


def _raw_id(credential: dict) -> bytes:
    try:
        raw = credential["rawId"]
        value = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (KeyError, TypeError, ValueError):
        raise StepupError(400, "malformed_credential") from None
    if credential.get("id") != raw or not 16 <= len(value) <= 1023:
        raise StepupError(400, "malformed_credential")
    return value


@dataclass(frozen=True)
class Caller:
    user_id: str
    session_id: str

    @classmethod
    def of(cls, user_id: str, session_id: str) -> "Caller":
        try:
            return cls(str(uuid.UUID(user_id)), str(uuid.UUID(session_id)))
        except (TypeError, ValueError, AttributeError):
            raise StepupError(400, "bad_request") from None


class Verifier:
    def __init__(self, settings: StepupSettings, connect) -> None:
        self.settings = settings
        self._connect = connect  # async context manager yielding an autocommit hyperchat_stepup connection

    # ── shared checks ───────────────────────────────────────────────────────────────────────────────────
    async def _limit(self, caller: Caller, bucket: str) -> None:
        """Count the attempt in its own committed transaction (so neither the count nor the audit event is rolled
        back with a refused request), then refuse when over the limit."""
        limit, window = LIMITS[bucket]
        async with self._connect() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("""
                INSERT INTO public.stepup_rate_limits AS r (key, bucket, window_start, count)
                VALUES (%s, %s, now(), 1)
                ON CONFLICT (key, bucket) DO UPDATE SET
                    count = CASE WHEN r.window_start <= now() - make_interval(secs => %s) THEN 1 ELSE r.count + 1 END,
                    window_start = CASE WHEN r.window_start <= now() - make_interval(secs => %s) THEN now()
                                        ELSE r.window_start END
                RETURNING count
            """, (f"user:{caller.user_id}", f"stepup:{bucket}", window, window))
            over = (await cur.fetchone())["count"] > limit
            if over:
                await self._event(cur, "rate_limited", caller.user_id, detail={"bucket": bucket})
        if over:
            security_event("stepup_rate_limited", user_id=caller.user_id, reason=bucket)
            raise StepupError(429, "rate_limited")

    async def _require_admin_session(self, cur, caller: Caller) -> dict:
        await cur.execute("""
            SELECT u.email::text AS email, public.stepup_admin_ok(u.id) AS admin,
                   (s.revoked_at IS NULL AND s.idle_expires_at > now() AND s.absolute_expires_at > now()) AS live
            FROM public.users u LEFT JOIN public.sessions s ON s.id = %s AND s.user_id = u.id
            WHERE u.id = %s
        """, (caller.session_id, caller.user_id))
        row = await cur.fetchone()
        if row is None or not row["admin"]:
            raise StepupError(403, "not_admin")
        if not row["live"]:
            raise StepupError(403, "session_invalid")
        return row

    @staticmethod
    async def _event(cur, event: str, user_id: str | None, *, credential_id=None, challenge_id=None, grant_id=None,
                     detail: dict | None = None) -> None:
        await cur.execute(
            "INSERT INTO public.stepup_events (event, user_id, credential_id, challenge_id, grant_id, detail) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
            (event, user_id, credential_id, challenge_id, grant_id, json.dumps(detail or {})))

    async def _issue(self, cur, caller: Caller, action: str, target: str, params: dict) -> tuple[str, canonical.CanonicalAction]:
        try:
            built = canonical.build(rp_id=self.settings.webauthn_rp_id, admin_id=caller.user_id,
                                    session_id=caller.session_id, action=action, target=target, params=params)
        except canonical.CanonicalError:
            raise StepupError(400, "bad_action") from None
        await cur.execute("""
            INSERT INTO public.stepup_challenges (user_id, session_id, purpose, action, target, params_hash,
                                                  canonical, challenge, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, to_timestamp(%s))
            RETURNING id::text
        """, (caller.user_id, caller.session_id, built.purpose, built.action, built.target, built.params_hash,
              built.canonical, built.challenge, built.expires_at))
        challenge_id = (await cur.fetchone())["id"]
        await self._event(cur, "challenge_issued", caller.user_id, challenge_id=challenge_id,
                          detail={"action": action, "target": target})
        return challenge_id, built

    async def _take_challenge(self, cur, caller: Caller, challenge_id: str, purposes: tuple[str, ...]) -> dict:
        """Lock the challenge: concurrent verifications of it serialize here, and only the first sees it unused."""
        try:
            challenge_id = str(uuid.UUID(challenge_id))
        except (TypeError, ValueError, AttributeError):
            raise StepupError(404, "challenge_not_found") from None
        await cur.execute("""
            SELECT id::text, purpose, action, target, params_hash, canonical, challenge, used_at,
                   expires_at <= now() AS expired
            FROM public.stepup_challenges
            WHERE id = %s AND user_id = %s AND session_id = %s FOR UPDATE
        """, (challenge_id, caller.user_id, caller.session_id))
        row = await cur.fetchone()
        if row is None or row["purpose"] not in purposes:
            raise StepupError(404, "challenge_not_found")
        if row["used_at"] is not None:
            raise StepupError(409, "challenge_used")
        if row["expired"]:
            raise StepupError(409, "challenge_expired")
        return row

    async def _burn(self, caller: Caller, challenge_id: str, event: str, code: str) -> None:
        """After a failed verification: the challenge can never be tried again, and the failure is recorded."""
        async with self._connect() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("UPDATE public.stepup_challenges SET outcome = 'failed' "
                              "WHERE id = %s AND user_id = %s AND used_at IS NULL", (challenge_id, caller.user_id))
            await self._event(cur, event, caller.user_id, challenge_id=challenge_id, detail={"reason": code})
        security_event(f"stepup_{event}", user_id=caller.user_id, reason=code)

    # ── listing (S2: the API's roles cannot read passkeys; fingerprints and states only, never key material) ──
    async def list_credentials(self, user_id: str, session_id: str) -> dict:
        caller = Caller.of(user_id, session_id)
        async with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            await self._require_admin_session(cur, caller)
            await cur.execute("""
                SELECT fingerprint, status, device_type, backed_up, created_at, approved_at, last_used_at, revoked_at
                FROM public.webauthn_credentials WHERE user_id = %s ORDER BY created_at
            """, (caller.user_id,))
            rows = await cur.fetchall()
        return {"credentials": [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
                                for r in rows]}

    # ── registration ──────────────────────────────────────────────────────────────────────────────────────
    async def registration_options(self, user_id: str, session_id: str) -> dict:
        caller = Caller.of(user_id, session_id)
        await self._limit(caller, "options")
        async with self._connect() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            who = await self._require_admin_session(cur, caller)
            challenge_id, built = await self._issue(cur, caller, "mfa.register", "self", {})
            await cur.execute("SELECT credential_id FROM public.webauthn_credentials "
                              "WHERE user_id = %s AND status <> 'revoked'", (caller.user_id,))
            existing = [PublicKeyCredentialDescriptor(id=bytes(r["credential_id"])) for r in await cur.fetchall()]
        options = generate_registration_options(
            rp_id=self.settings.webauthn_rp_id, rp_name=self.settings.webauthn_rp_name, user_name=who["email"],
            user_id=uuid.UUID(caller.user_id).bytes, user_display_name=who["email"], challenge=built.challenge,
            timeout=canonical.CHALLENGE_TTL * 1000, exclude_credentials=existing, supported_pub_key_algs=ALGORITHMS,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED, user_verification=UserVerificationRequirement.REQUIRED))
        return {"challenge_id": challenge_id, "canonical": built.canonical,
                "public_key": json.loads(options_to_json(options))}

    async def registration_verify(self, user_id: str, session_id: str, challenge_id: str, credential: dict) -> dict:
        caller = Caller.of(user_id, session_id)
        await self._limit(caller, "register")
        async with self._connect() as conn:
            try:
                async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                    await self._require_admin_session(cur, caller)
                    challenge = await self._take_challenge(cur, caller, challenge_id, ("register",))
                    _client_data(credential)
                    try:
                        verified = verify_registration_response(
                            credential=credential, expected_challenge=bytes(challenge["challenge"]),
                            expected_rp_id=self.settings.webauthn_rp_id, expected_origin=self.settings.origins,
                            require_user_presence=True, require_user_verification=True,
                            supported_pub_key_algs=ALGORITHMS)
                    except WebAuthnException:
                        raise StepupError(400, "verification_failed") from None
                    print_ = fingerprint(verified.credential_public_key)
                    try:
                        await cur.execute("""
                            INSERT INTO public.webauthn_credentials (user_id, credential_id, public_key, fingerprint,
                                                                     sign_count, aaguid, device_type, backed_up)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id::text
                        """, (caller.user_id, verified.credential_id, verified.credential_public_key, print_,
                              verified.sign_count, verified.aaguid or None, verified.credential_device_type.value,
                              verified.credential_backed_up))
                    except psycopg.errors.UniqueViolation:
                        raise StepupError(409, "already_registered") from None
                    except psycopg.errors.InsufficientPrivilege:
                        raise StepupError(409, "registration_refused") from None
                    credential_row = (await cur.fetchone())["id"]
                    await cur.execute("UPDATE public.stepup_challenges SET outcome = 'ok' WHERE id = %s",
                                      (challenge["id"],))
                    await self._event(cur, "registration_ok", caller.user_id, credential_id=credential_row,
                                      challenge_id=challenge["id"], detail={"fingerprint": print_})
            except StepupError as exc:
                if exc.code not in ("challenge_not_found", "challenge_used", "not_admin", "session_invalid"):
                    await self._burn(caller, challenge_id, "registration_failed", exc.code)
                raise
        security_event("stepup_registered", level=logging.INFO, user_id=caller.user_id)
        return {"credential": credential_row, "fingerprint": print_, "status": "pending"}

    # ── assertion ─────────────────────────────────────────────────────────────────────────────────────────
    async def assertion_options(self, user_id: str, session_id: str, action: str, target: str, params: dict) -> dict:
        caller = Caller.of(user_id, session_id)
        if canonical.ACTIONS.get(action) not in ("session", "action"):
            raise StepupError(400, "bad_action")
        if canonical.ACTIONS[action] == "session" and (target != "session" or params):
            raise StepupError(400, "bad_action")
        await self._limit(caller, "options")
        async with self._connect() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await self._require_admin_session(cur, caller)
            await cur.execute("SELECT credential_id FROM public.webauthn_credentials "
                              "WHERE user_id = %s AND status = 'active'", (caller.user_id,))
            allowed = [PublicKeyCredentialDescriptor(id=bytes(r["credential_id"])) for r in await cur.fetchall()]
            if not allowed:
                raise StepupError(409, "no_active_credential")
            challenge_id, built = await self._issue(cur, caller, action, target, params)
        options = generate_authentication_options(
            rp_id=self.settings.webauthn_rp_id, challenge=built.challenge, timeout=canonical.CHALLENGE_TTL * 1000,
            allow_credentials=allowed, user_verification=UserVerificationRequirement.REQUIRED)
        return {"challenge_id": challenge_id, "canonical": built.canonical,
                "public_key": json.loads(options_to_json(options))}

    async def assertion_verify(self, user_id: str, session_id: str, challenge_id: str, credential: dict) -> dict:
        caller = Caller.of(user_id, session_id)
        await self._limit(caller, "assert")
        async with self._connect() as conn:
            try:
                async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                    await self._require_admin_session(cur, caller)
                    challenge = await self._take_challenge(cur, caller, challenge_id, ("session", "action"))
                    _client_data(credential)
                    await cur.execute("""
                        SELECT id::text, public_key, sign_count, status FROM public.webauthn_credentials
                        WHERE credential_id = %s AND user_id = %s FOR UPDATE
                    """, (_raw_id(credential), caller.user_id))
                    passkey = await cur.fetchone()
                    if passkey is None:
                        raise StepupError(400, "unknown_credential")
                    if passkey["status"] != "active":
                        raise StepupError(403, f"credential_{passkey['status']}")
                    try:
                        verified = verify_authentication_response(
                            credential=credential, expected_challenge=bytes(challenge["challenge"]),
                            expected_rp_id=self.settings.webauthn_rp_id, expected_origin=self.settings.origins,
                            credential_public_key=bytes(passkey["public_key"]),
                            credential_current_sign_count=passkey["sign_count"], require_user_verification=True)
                    except WebAuthnException:
                        raise StepupError(400, "verification_failed") from None
                    await cur.execute("""
                        UPDATE public.webauthn_credentials SET sign_count = %s, last_used_at = now()
                        WHERE id = %s AND sign_count = %s AND status = 'active'
                    """, (verified.new_sign_count, passkey["id"], passkey["sign_count"]))
                    if cur.rowcount != 1:
                        raise StepupError(409, "concurrent_use")
                    await cur.execute("UPDATE public.stepup_challenges SET outcome = 'ok' WHERE id = %s",
                                      (challenge["id"],))
                    await self._event(cur, "assertion_ok", caller.user_id, credential_id=passkey["id"],
                                      challenge_id=challenge["id"], detail={"action": challenge["action"]})
                    if challenge["purpose"] == "session":
                        await cur.execute("""
                            INSERT INTO public.session_assurance (session_id, user_id, credential_id, challenge_id, aal,
                                                                  expires_at)
                            SELECT s.id, s.user_id, %s, %s, 2,
                                   LEAST(now() + make_interval(secs => %s), s.absolute_expires_at)
                            FROM public.sessions s WHERE s.id = %s AND s.user_id = %s
                            ON CONFLICT (session_id) DO UPDATE SET credential_id = EXCLUDED.credential_id,
                                challenge_id = EXCLUDED.challenge_id, aal = 2, verified_at = now(),
                                expires_at = EXCLUDED.expires_at
                            RETURNING expires_at
                        """, (passkey["id"], challenge["id"], canonical.SESSION_ASSURANCE_TTL, caller.session_id,
                              caller.user_id))
                        expires = (await cur.fetchone())["expires_at"]
                        await self._event(cur, "session_assured", caller.user_id, credential_id=passkey["id"],
                                          challenge_id=challenge["id"])
                        result = {"aal": 2, "expires_at": expires.isoformat()}
                    else:
                        await cur.execute("""
                            INSERT INTO public.stepup_grants (user_id, session_id, credential_id, challenge_id, action,
                                                              target, params_hash, expires_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))
                            RETURNING id::text, expires_at
                        """, (caller.user_id, caller.session_id, passkey["id"], challenge["id"], challenge["action"],
                              challenge["target"], challenge["params_hash"], canonical.GRANT_TTL))
                        grant = await cur.fetchone()
                        await self._event(cur, "grant_issued", caller.user_id, credential_id=passkey["id"],
                                          challenge_id=challenge["id"], grant_id=grant["id"],
                                          detail={"action": challenge["action"], "target": challenge["target"]})
                        result = {"grant_id": grant["id"], "expires_at": grant["expires_at"].isoformat(),
                                  "action": challenge["action"], "target": challenge["target"],
                                  "params_hash": challenge["params_hash"]}
            except StepupError as exc:
                if exc.code not in ("challenge_not_found", "challenge_used", "not_admin", "session_invalid"):
                    await self._burn(caller, challenge_id, "assertion_failed", exc.code)
                raise
        security_event("stepup_assertion_ok", level=logging.INFO, user_id=caller.user_id)
        return result
