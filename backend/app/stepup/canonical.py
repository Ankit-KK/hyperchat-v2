"""Canonical step-up actions: the exact bytes a passkey signs (docs/admin-stepup.md).

A WebAuthn challenge here is not a random number: it is SHA-256 over a domain separator and the canonical JSON of
the action being approved, so the authenticator's signature covers WHO (administrator), WHERE (a hash of the
session id), WHAT (action, target, the exact parameters) and WHEN (expiry). The browser recomputes the challenge
from the canonical text it is shown and refuses to sign when it differs from the action the administrator asked
for; the database recomputes it too (migration 0025) before a challenge row can exist.

Canonical JSON (the frontend must reproduce it byte for byte; tests/stepup/canonical_vectors.json):
  * objects only with keys of [a-z0-9_], at most 32 characters; keys sorted (ASCII order); no whitespace;
  * strings without control characters (< U+0020) or unpaired surrogates, written as UTF-8, escaping only '"' and
    '\\'; at most 512 characters;
  * integers within +/- (2**53 - 1) (safe in JavaScript); no floats, no NaN; booleans and null as JSON;
  * nesting at most 4 deep.
"""

import base64
import hashlib
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass

VERSION = 1
DOMAIN = b"hyperchat-stepup-v1\n"
SAFE_INT = 2**53 - 1
GRANT_TTL = 120  # seconds a grant lives (the database caps it at 2 minutes)
CHALLENGE_TTL = 180  # seconds a challenge lives (the database caps it at 5 minutes)
SESSION_ASSURANCE_TTL = 12 * 3600

# purpose per action; S2/S3 bind each action to its route and its database write.
ACTIONS = {
    "session.aal2": "session",
    "mfa.register": "register",
    "creator.activate": "action",
    "creator.reactivate": "action",
    "creator.create": "action",
    "creator.owner.assign": "action",
    "creator.owner.transfer": "action",
    "application.approve": "action",
    "application.invite": "action",
    "admin.invite": "action",
    "admin.user.change": "action",
    "admin.user.email": "action",
    "partner_key.create": "action",
    "mfa.credential.revoke": "action",
}
_KEY = re.compile(r"^[a-z0-9_]{1,32}$")
_TARGET = re.compile(r"^[A-Za-z0-9._:@+\-]{1,200}$")


class CanonicalError(ValueError):
    pass


def _check(value, depth: int = 0):
    if depth > 4:
        raise CanonicalError("nested too deep")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > SAFE_INT:
            raise CanonicalError("integer out of the safe range")
        return
    if isinstance(value, str):
        if len(value) > 512 or any(ord(ch) < 0x20 or 0xD800 <= ord(ch) <= 0xDFFF for ch in value):
            raise CanonicalError("string not allowed")
        return
    if isinstance(value, list):
        if len(value) > 64:
            raise CanonicalError("list too long")
        for item in value:
            _check(item, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 64:
            raise CanonicalError("object too large")
        for key, item in value.items():
            if not isinstance(key, str) or not _KEY.match(key):
                raise CanonicalError(f"key not allowed: {key!r}")
            _check(item, depth + 1)
        return
    raise CanonicalError(f"type not allowed: {type(value).__name__}")


def canonical_json(value) -> str:
    _check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def challenge_bytes(canonical: str) -> bytes:
    return hashlib.sha256(DOMAIN + canonical.encode("utf-8")).digest()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def session_ref(session_id: str) -> str:
    """The session as the browser sees it: a hash of the id (the raw id is never exposed)."""
    return sha256_hex(str(uuid.UUID(session_id)))


@dataclass(frozen=True)
class CanonicalAction:
    canonical: str
    challenge: bytes
    purpose: str
    action: str
    target: str
    params_hash: str
    expires_at: int


def build(*, rp_id: str, admin_id: str, session_id: str, action: str, target: str, params: dict,
          now: int | None = None, nonce: str | None = None, ttl: int = CHALLENGE_TTL) -> CanonicalAction:
    """The canonical action for one ceremony. Unknown actions, malformed targets or parameters are refused."""
    purpose = ACTIONS.get(action)
    if purpose is None:
        raise CanonicalError(f"unknown action: {action!r}")
    if not isinstance(target, str) or not _TARGET.match(target):
        raise CanonicalError("target not allowed")
    if not isinstance(params, dict):
        raise CanonicalError("params must be an object")
    issued = int(time.time()) if now is None else now
    params_hash = sha256_hex(canonical_json(params))
    body = {
        "v": VERSION, "purpose": purpose, "rp_id": rp_id, "admin": str(uuid.UUID(admin_id)),
        "session": session_ref(session_id), "action": action, "target": target, "params": params,
        "params_hash": params_hash, "nonce": nonce or b64url(secrets.token_bytes(32)),
        "issued_at": issued, "expires_at": issued + ttl,
    }
    text = canonical_json(body)
    return CanonicalAction(text, challenge_bytes(text), purpose, action, target, params_hash, issued + ttl)
