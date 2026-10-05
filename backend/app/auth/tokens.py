"""Opaque tokens and signed values. Only SHA-256 digests of bearer tokens are ever stored."""

import base64
import hashlib
import hmac
import json
import secrets
import time


def new_token(num_bytes: int = 32) -> str:
    """URL-safe random token (session cookies, CSRF, OAuth state/nonce/PKCE)."""
    return secrets.token_urlsafe(num_bytes)


def new_hex_token(num_bytes: int = 32) -> str:
    """64-hex-char token, same shape as the old reset links (reset and invite tokens)."""
    return secrets.token_hex(num_bytes)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def csrf_token_for(secret: str, session_id: str) -> str:
    """Per-session CSRF token, derived (not stored): stable for the session's lifetime."""
    return hmac.new(secret.encode(), f"hyperchat:csrf:v1:{session_id}".encode(), hashlib.sha256).hexdigest()


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def sign_payload(secret: str, payload: dict, ttl_seconds: int, now: float | None = None) -> str:
    """Compact signed value: base64url(json) + "." + HMAC. Used for the OAuth state cookie."""
    body = dict(payload, exp=int((now or time.time()) + ttl_seconds))
    raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(secret.encode(), f"hyperchat:signed:v1:{raw}".encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def verify_payload(secret: str, value: str | None, now: float | None = None) -> dict | None:
    if not value or "." not in value:
        return None
    raw, sig = value.rsplit(".", 1)
    expected = hmac.new(secret.encode(), f"hyperchat:signed:v1:{raw}".encode(), hashlib.sha256).hexdigest()
    if not constant_time_equal(sig, expected):
        return None
    try:
        body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except ValueError:
        return None
    if not isinstance(body, dict) or body.get("exp", 0) < (now or time.time()):
        return None
    return body
