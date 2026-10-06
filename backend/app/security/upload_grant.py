"""Single-use donor upload grants (donor media uploads to Cloudflare R2).

Grant (v2 API, bound to one upload type):
    "<slug>.<kind>.<expiryMs>.<nonce32hex>.<hmac>", kind = "voice" | "media"
Legacy form (kept for compatibility, no type binding):
    "<slug>.<expiryMs>.<nonce32hex>.<hmac>"
hmac = HMAC_SHA256_hex(upload_grant_secret, everything before the last dot). Valid 10 minutes; burned
by inserting "upload:<nonce>" into the nonce store, so a grant can be used exactly once.

verify_upload_grant checks signature, expiry, creator and type without side effects; consume_upload_grant
burns it. The upload API verifies first, validates the file, and burns the grant right before storing.
"""

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from app.security.crypto import hmac_sha256_hex, random_hex, timing_safe_equal
from app.security.nonce_store import NonceReplayError, NonceStore

GRANT_TTL_MS = 10 * 60 * 1000
GRANT_KINDS = frozenset({"voice", "media"})
MAX_GRANT_LENGTH = 512
_NONCE = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class GrantCheck:
    ok: bool
    error: str | None = None
    nonce: str | None = None
    expires_at: datetime | None = None
    unavailable: bool = False  # nonce storage failed: rejected (fail closed), but not the client's fault


class UploadGrantNotConfigured(RuntimeError):
    pass


def _sign(secret: str | None, payload: str) -> str:
    if not secret:
        raise UploadGrantNotConfigured("UPLOAD_GRANT_SECRET is not configured")
    return hmac_sha256_hex(secret, payload)


def _iso_ms(ms: int) -> str:
    """ISO 8601 UTC with millisecond precision and a 'Z' suffix."""
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def issue_upload_grant(secret: str | None, streamer_slug: str, *, kind: str | None = None,
                       now_ms: int | None = None, nonce: str | None = None) -> tuple[str, str]:
    """Returns (grant, expiresAt ISO string). With `kind` the grant only works for that upload type."""
    if kind is not None and kind not in GRANT_KINDS:
        raise ValueError("unknown upload kind")
    now = int(time.time() * 1000) if now_ms is None else now_ms
    expiry = now + GRANT_TTL_MS
    nonce = nonce or random_hex(16)
    payload = ".".join([streamer_slug, *([kind] if kind else []), str(expiry), nonce])
    return f"{payload}.{_sign(secret, payload)}", _iso_ms(expiry)


def verify_upload_grant(secret: str | None, grant: object, streamer_slug: str, *, kind: str | None = None,
                        now_ms: int | None = None) -> GrantCheck:
    """Signature, expiry, creator and (with `kind`) upload type. Does not burn the grant."""
    if not isinstance(grant, str) or not grant:
        return GrantCheck(False, "Upload grant required")
    parts = grant.split(".")
    if len(grant) > MAX_GRANT_LENGTH or len(parts) != (5 if kind else 4):
        return GrantCheck(False, "Malformed upload grant")

    if kind:
        slug, grant_kind, expiry_raw, nonce, signature = parts
        if not _NONCE.match(nonce):
            return GrantCheck(False, "Malformed upload grant")
    else:
        slug, expiry_raw, nonce, signature = parts
        grant_kind = None
    if slug != streamer_slug:
        return GrantCheck(False, "Upload grant does not match this creator")
    if grant_kind != kind:
        return GrantCheck(False, "Upload grant is not valid for this upload type")

    now = int(time.time() * 1000) if now_ms is None else now_ms
    if not expiry_raw.isascii() or not expiry_raw.isdigit():
        return GrantCheck(False, "Malformed upload grant")
    expiry = int(expiry_raw)
    if now > expiry:
        return GrantCheck(False, "Upload grant expired")

    expected = _sign(secret, ".".join(parts[:-1]))
    if not timing_safe_equal(expected, signature):
        return GrantCheck(False, "Invalid upload grant")
    return GrantCheck(True, nonce=nonce, expires_at=datetime.fromtimestamp(expiry / 1000, UTC))


async def consume_upload_grant(nonce_store: NonceStore, check: GrantCheck) -> GrantCheck:
    """Burn a verified grant: the first caller wins, every later use is a replay."""
    if not check.ok or not check.nonce or check.expires_at is None:
        return GrantCheck(False, check.error or "Invalid upload grant")
    try:
        await nonce_store.insert(f"upload:{check.nonce}", None, check.expires_at)
    except NonceReplayError:
        return GrantCheck(False, "Upload grant already used")
    except Exception:  # noqa: BLE001 - storage failure: never accept the grant (fail closed)
        return GrantCheck(False, "Upload grant could not be verified", unavailable=True)
    return check


async def verify_and_consume_upload_grant(secret: str | None, nonce_store: NonceStore, grant: object,
                                          streamer_slug: str, *, kind: str | None = None,
                                          now_ms: int | None = None) -> GrantCheck:
    check = verify_upload_grant(secret, grant, streamer_slug, kind=kind, now_ms=now_ms)
    if not check.ok:
        return check
    used = await consume_upload_grant(nonce_store, check)
    if not used.ok:  # replay or storage failure: never accept the grant twice
        return GrantCheck(False, "Upload grant already used")
    return used
