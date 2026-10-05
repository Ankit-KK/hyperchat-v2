"""Single-use donor upload grants (donor media uploads to Cloudflare R2).

Grant: "<slug>.<expiryMs>.<nonce32hex>.<hmac>" where hmac = HMAC_SHA256_hex(upload_grant_secret,
"<slug>.<expiryMs>.<nonce>"). Valid 10 minutes; burned by inserting "upload:<nonce>" into the
nonce store, so a grant can be used exactly once.
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from app.security.crypto import hmac_sha256_hex, random_hex, timing_safe_equal
from app.security.nonce_store import NonceStore

GRANT_TTL_MS = 10 * 60 * 1000


@dataclass(frozen=True)
class GrantCheck:
    ok: bool
    error: str | None = None


class UploadGrantNotConfigured(RuntimeError):
    pass


def _sign(secret: str | None, payload: str) -> str:
    if not secret:
        raise UploadGrantNotConfigured("UPLOAD_GRANT_SECRET is not configured")
    return hmac_sha256_hex(secret, payload)


def _iso_ms(ms: int) -> str:
    """ISO 8601 UTC with millisecond precision and a 'Z' suffix."""
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def issue_upload_grant(secret: str | None, streamer_slug: str, *, now_ms: int | None = None,
                       nonce: str | None = None) -> tuple[str, str]:
    """Returns (grant, expiresAt ISO string)."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    expiry = now + GRANT_TTL_MS
    nonce = nonce or random_hex(16)
    payload = f"{streamer_slug}.{expiry}.{nonce}"
    return f"{payload}.{_sign(secret, payload)}", _iso_ms(expiry)


async def verify_and_consume_upload_grant(secret: str | None, nonce_store: NonceStore, grant: object,
                                          streamer_slug: str, *, now_ms: int | None = None) -> GrantCheck:
    if not isinstance(grant, str) or not grant:
        return GrantCheck(False, "Upload grant required")
    parts = grant.split(".")
    if len(parts) != 4:
        return GrantCheck(False, "Malformed upload grant")

    slug, expiry_raw, nonce, signature = parts
    if slug != streamer_slug:
        return GrantCheck(False, "Upload grant does not match this creator")

    now = int(time.time() * 1000) if now_ms is None else now_ms
    if not expiry_raw.isascii() or not expiry_raw.isdigit():
        return GrantCheck(False, "Malformed upload grant")
    expiry = int(expiry_raw)
    if now > expiry:
        return GrantCheck(False, "Upload grant expired")

    expected = _sign(secret, f"{slug}.{expiry_raw}.{nonce}")
    if not timing_safe_equal(expected, signature):
        return GrantCheck(False, "Invalid upload grant")

    try:
        await nonce_store.insert(f"upload:{nonce}", None, datetime.fromtimestamp(expiry / 1000, UTC))
    except Exception:  # noqa: BLE001 - replay or storage failure: never accept the grant twice
        return GrantCheck(False, "Upload grant already used")
    return GrantCheck(True)
