"""Shared secrets and partner API keys (server-to-server; never used for browsers).

* Internal services / cron jobs present a shared secret header; it is compared in constant time
  and an unconfigured secret never authorizes (fail closed).
* Partner API: x-api-key, looked up by SHA-256 in partner_api_keys and rejected when revoked.
"""

from dataclasses import dataclass
from typing import Protocol

from app.security.crypto import sha256_hex, timing_safe_equal


def verify_shared_secret(presented: str | None, expected: str | None) -> bool:
    if not expected:
        return False
    return timing_safe_equal(presented or "", expected)


@dataclass(frozen=True)
class PartnerKey:
    id: str
    partner_slug: str


class PartnerKeyStore(Protocol):
    async def find_active_by_hash(self, key_hash: str) -> PartnerKey | None:
        """Non-revoked key with this SHA-256, or None."""


@dataclass(frozen=True)
class PartnerAuth:
    ok: bool
    status: int
    partner: PartnerKey | None = None
    error: str | None = None


ERR_PARTNER_KEY = "Invalid or missing API key"


async def verify_partner_api_key(store: PartnerKeyStore, api_key: str | None) -> PartnerAuth:
    """Keys are 20-200 characters; the SHA-256 must match a non-revoked key."""
    if not api_key or len(api_key) < 20 or len(api_key) > 200:
        return PartnerAuth(False, 401, error=ERR_PARTNER_KEY)
    partner = await store.find_active_by_hash(sha256_hex(api_key))
    if partner is None:
        return PartnerAuth(False, 401, error=ERR_PARTNER_KEY)
    return PartnerAuth(True, 200, partner=partner)
