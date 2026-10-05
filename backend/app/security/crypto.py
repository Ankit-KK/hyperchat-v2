"""Hashing and HMAC primitives. All digests are lowercase hex of UTF-8 input."""

import hashlib
import hmac
import secrets


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hmac_sha256_hex(key: str, message: str | bytes) -> str:
    if isinstance(message, str):
        message = message.encode("utf-8")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def timing_safe_equal(a: str, b: str) -> bool:
    """Constant-time string comparison."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def random_hex(num_bytes: int = 16) -> str:
    return secrets.token_hex(num_bytes)
