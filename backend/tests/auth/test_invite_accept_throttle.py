"""Security audit 2026-11-26 NV-2: invite acceptance counts the per-IP limit before Argon2, so random tokens get 429
after the limit and stop costing a password hash. No database: an in-memory limiter. Synthetic data only."""

import asyncio

import pytest

from app.auth import service as svc_mod
from app.auth.service import AuthError, AuthService
from app.core.config import Settings

IP = "203.0.113.77"


class LimitRepo:
    def __init__(self, limit: int) -> None:
        self.limit, self.counts, self.accepts = limit, {}, 0

    async def hit_rate_limit(self, key, bucket, limit, window) -> bool:
        assert (bucket, limit, window) == ("auth:invite-accept", 20, 3600)
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key] <= self.limit

    async def accept_invite(self, token, password_hash, username, row):
        self.accepts += 1
        return None  # every random token is invalid


def test_random_invite_tokens_hit_429_after_the_limit_and_stop_hashing(monkeypatch):
    hashes = []

    async def counting_hash(password):
        hashes.append(password)
        return "argon2-stub"
    monkeypatch.setattr(svc_mod, "_hash", counting_hash)
    repo = LimitRepo(limit=3)
    s = AuthService(Settings(db_enabled=False, auth_secret="test-only-" + "x" * 32), repo, None, None)

    async def attempt(i):
        with pytest.raises(AuthError) as err:
            await s.accept_invite(f"random-{i}", "Invite-Pass-42", None, IP, None)
        return err.value.status, err.value.reason

    results = [asyncio.run(attempt(i)) for i in range(5)]
    assert results[:3] == [(400, "bad_invite_token")] * 3
    assert results[3:] == [(429, "ip_rate_limited")] * 2
    assert len(hashes) == 3 and repo.accepts == 3  # no hash and no lookup once limited
