"""Single-use nonce storage. Used for HMAC replay protection and upload grants.

The PostgreSQL implementation inserts into public.request_nonces, whose primary key makes a
second insert of the same nonce fail with unique_violation (23505), which means "replay".
"""

import asyncio
import random
from datetime import datetime
from typing import Protocol


class NonceReplayError(Exception):
    """The nonce was already used (unique violation)."""


class NonceStoreError(Exception):
    """Any other storage failure. Callers must fail closed."""


class NonceStore(Protocol):
    async def insert(self, nonce: str, user_id: str | None, expires_at: datetime) -> None: ...

    async def purge_expired(self) -> None: ...


class InMemoryNonceStore:
    """Process-local store for tests. Not for production (no sharing across workers)."""

    def __init__(self) -> None:
        self._seen: dict[str, datetime] = {}
        self._lock = asyncio.Lock()
        self.fail_with: Exception | None = None

    async def insert(self, nonce: str, user_id: str | None, expires_at: datetime) -> None:
        if self.fail_with is not None:
            raise NonceStoreError(str(self.fail_with))
        async with self._lock:
            if nonce in self._seen:
                raise NonceReplayError(nonce)
            self._seen[nonce] = expires_at

    async def purge_expired(self) -> None:
        return None

    def __contains__(self, nonce: str) -> bool:
        return nonce in self._seen


async def maybe_purge(store: NonceStore, probability: float = 0.01) -> None:
    """Best-effort cleanup on ~1% of calls; failures are ignored."""
    if random.random() < probability:
        try:
            await store.purge_expired()
        except Exception:  # noqa: BLE001
            pass
