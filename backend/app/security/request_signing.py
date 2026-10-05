"""HMAC-signed server-to-server requests (internal services, bots, workers).

Headers: x-timestamp (ms since epoch), x-nonce (16-128 chars, single use), x-signature =
HMAC_SHA256_hex(key, f"{timestamp}\\n{nonce}\\n{sha256_hex(body)}"). Requests more than 5 minutes
off are rejected and every nonce is burned in the nonce store.
Browser requests never use this: they authenticate with the session cookie + CSRF token.
"""

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from app.security.crypto import hmac_sha256_hex, sha256_hex, timing_safe_equal
from app.security.nonce_store import NonceReplayError, NonceStore, maybe_purge

SKEW_MS = 5 * 60 * 1000
NONCE_TTL_MS = 10 * 60 * 1000


@dataclass(frozen=True)
class HmacResult:
    ok: bool
    status: int
    error: str | None = None
    reason: str = "ok"  # machine-readable, for security logs


def _matches_any(keys: list[str], message: str, signature: str) -> bool:
    matched = False
    for key in keys:  # no early exit: constant work regardless of which key matches (key rotation)
        if timing_safe_equal(hmac_sha256_hex(key, message), signature):
            matched = True
    return matched


async def verify_hmac_request(
    headers: Mapping[str, str],
    body: bytes,
    keys: list[str],
    nonce_store: NonceStore,
    *,
    user_id: str | None = None,  # users.id when a user is involved (request_nonces FK), else None
    now_ms: int | None = None,
) -> HmacResult:
    keys = [k for k in keys if k]
    if not keys:
        return HmacResult(False, 503, "Request signing is not configured", "no_signing_key")
    timestamp = headers.get("x-timestamp") or ""
    signature = headers.get("x-signature") or ""
    nonce = headers.get("x-nonce") or ""
    if not timestamp or not signature or not nonce:
        return HmacResult(False, 401, "Missing request signature", "missing_signature")
    if not timestamp.isascii() or not timestamp.isdigit():
        return HmacResult(False, 401, "Invalid timestamp", "invalid_timestamp")
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if abs(now - int(timestamp)) > SKEW_MS:
        return HmacResult(False, 401, "Request expired", "timestamp_skew")
    if len(nonce) < 16 or len(nonce) > 128:
        return HmacResult(False, 401, "Invalid nonce", "invalid_nonce")
    if not _matches_any(keys, f"{timestamp}\n{nonce}\n{sha256_hex(body)}", signature):
        return HmacResult(False, 401, "Invalid request signature", "bad_signature")
    expires_at = datetime.fromtimestamp((now + NONCE_TTL_MS) / 1000, UTC)
    try:
        await nonce_store.insert(nonce, user_id, expires_at)
    except NonceReplayError:
        return HmacResult(False, 409, "Request replay detected", "replay")
    except Exception:  # noqa: BLE001 - any other failure: fail closed
        return HmacResult(False, 500, "Signature verification failed", "nonce_store_error")
    await maybe_purge(nonce_store)
    return HmacResult(True, 200)


def sign_request(key: str, body: bytes, timestamp_ms: int, nonce: str) -> dict[str, str]:
    """Client-side signer for internal callers and tests."""
    ts = str(timestamp_ms)
    return {
        "x-timestamp": ts,
        "x-nonce": nonce,
        "x-signature": hmac_sha256_hex(key, f"{ts}\n{nonce}\n{sha256_hex(body)}"),
    }
