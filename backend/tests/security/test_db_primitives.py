"""Database-backed security primitives through FastAPI dependencies (scratch database).

Test-only routes are mounted on the real app; the secrets are random TEST-ONLY values per test.
"""

import asyncio
import logging
import secrets

import pytest
from fastapi import APIRouter, Depends, Request
from fastapi.testclient import TestClient

from app.auth.tokens import sha256_hex
from app.cli import connection_factory
from app.core.config import Settings
from app.security.dependencies import (
    ObsCaller,
    require_obs_token,
    require_partner_key,
)
from app.security.nonce_store import NonceReplayError
from app.security.obs_token import obs_channel_allowed
from app.security.pg_stores import PostgresNonceStore
from app.security.upload_grant import consume_upload_grant, issue_upload_grant, verify_upload_grant
from tests.integration import FRONT, LogCapture, db, make_streamer, requires_db

pytestmark = requires_db

router = APIRouter(prefix="/_test")


@router.get("/obs")
async def obs(caller: ObsCaller = Depends(require_obs_token)):
    return {"streamer_id": caller.info.streamer_id, "slug": caller.info.streamer_slug}


@router.post("/obs/channel")
async def obs_channel(body: dict, request: Request, caller: ObsCaller = Depends(require_obs_token)):
    return {"allowed": await obs_channel_allowed(request.app.state.security.obs_tokens, caller.token, caller.info,
                                                 body["channel"])}


@router.get("/partner")
async def partner(key=Depends(require_partner_key)):
    return {"partner": key.partner_slug}


@pytest.fixture
def client(env):
    from app.main import create_app

    application = create_app()
    application.include_router(router)
    capture = LogCapture()
    logging.getLogger().addHandler(capture)
    with TestClient(application, base_url="https://testserver") as c:
        c.capture = capture
        yield c
    logging.getLogger().removeHandler(capture)


def _obs_token(streamer_id: str, **cols) -> str:
    token = "obs_" + secrets.token_urlsafe(24)
    values = {"is_active": True, "expires_at": None, "revoked_at": None, "pusher_channel_scope": None, **cols}
    with db() as conn:
        conn.execute("INSERT INTO public.obs_tokens (streamer_id, token_hash, is_active, expires_at, revoked_at, "
                     "pusher_channel_scope) VALUES (%s, %s, %s, %s, %s, %s)",
                     (streamer_id, sha256_hex(token), values["is_active"], values["expires_at"], values["revoked_at"],
                      values["pusher_channel_scope"]))
    return token


# ── OBS tokens ──────────────────────────────────────────────────────────────────


def test_obs_token_valid_by_header_or_query_and_usage_recorded(client):
    sid = make_streamer("authtest-obs")
    token = _obs_token(sid)
    assert client.get("/_test/obs", headers={"x-obs-token": token}).json() == {"streamer_id": sid,
                                                                               "slug": "authtest-obs"}
    assert client.get("/_test/obs", params={"token": token}, headers={"origin": FRONT}).status_code == 200
    with db() as conn:
        count, used = conn.execute("SELECT usage_count, last_used_at IS NOT NULL FROM public.obs_tokens "
                                   "WHERE token_hash = %s", (sha256_hex(token),)).fetchone()
    assert (count, used) == (2, True)


def test_obs_token_rejections(client):
    sid = make_streamer("authtest-obs2")
    good = _obs_token(sid)
    inactive = _obs_token(sid, is_active=False)
    revoked = _obs_token(sid, revoked_at="2026-01-01T00:00:00Z")
    expired = _obs_token(sid, expires_at="2026-01-01T00:00:00Z")
    for token in (inactive, revoked, expired, "obs_unknown", ""):
        r = client.get("/_test/obs", headers={"x-obs-token": token})
        assert r.status_code == 401 and r.json() == {"error": "Invalid or missing OBS token"}, token
    foreign = client.get("/_test/obs", headers={"x-obs-token": good, "origin": "https://evil.invalid"})
    assert foreign.status_code == 403
    with db() as conn:  # raw tokens never reach the database
        assert conn.execute("SELECT count(*) FROM public.obs_tokens WHERE token_hash = %s", (good,)).fetchone()[0] == 0
    assert all(good not in r for r in client.capture.records)


def test_obs_channel_scope_default_and_explicit(client):
    sid = make_streamer("authtest-obs3")
    default = _obs_token(sid)
    scoped = _obs_token(sid, pusher_channel_scope=["private-authtest-obs3-alerts"])

    def allowed(token, channel):
        return client.post("/_test/obs/channel", headers={"x-obs-token": token}, json={"channel": channel}).json()

    assert allowed(default, "private-authtest-obs3-goal") == {"allowed": True}
    assert allowed(default, "private-authtest-other-goal") == {"allowed": False}
    assert allowed(scoped, "private-authtest-obs3-alerts") == {"allowed": True}
    assert allowed(scoped, "private-authtest-obs3-goal") == {"allowed": False}


# ── partner keys ────────────────────────────────────────────────────────────────


def test_partner_keys(client):
    good, revoked = "pk_" + secrets.token_urlsafe(32), "pk_" + secrets.token_urlsafe(32)
    with db() as conn:
        for key, is_revoked in ((good, False), (revoked, True)):
            conn.execute("INSERT INTO public.partner_api_keys (partner_slug, key_hash, key_prefix, revoked) "
                         "VALUES ('authtest-partner', %s, %s, %s)", (sha256_hex(key), key[:8], is_revoked))
    assert client.get("/_test/partner", headers={"x-api-key": good}).json() == {"partner": "authtest-partner"}
    for key in (revoked, "pk_" + secrets.token_urlsafe(32), None):
        r = client.get("/_test/partner", headers={"x-api-key": key} if key else {})
        assert r.status_code == 401 and r.json() == {"error": "Invalid or missing API key"}
    with db() as conn:
        assert conn.execute("SELECT last_used_at IS NOT NULL FROM public.partner_api_keys WHERE key_hash = %s",
                            (sha256_hex(good),)).fetchone()[0] is True


# ── nonce store and upload grants on the real table ─────────────────────────────


def test_nonce_store_and_upload_grant_on_postgres(env):
    store = PostgresNonceStore(connection_factory(Settings()))
    nonce = "authtest-" + secrets.token_hex(8)
    from datetime import UTC, datetime, timedelta

    asyncio.run(store.insert(nonce, None, datetime.now(UTC) + timedelta(minutes=5)))
    with pytest.raises(NonceReplayError):
        asyncio.run(store.insert(nonce, None, datetime.now(UTC) + timedelta(minutes=5)))

    grant, _ = issue_upload_grant("TEST-ONLY-grant", "authtest-up", kind="voice", nonce=secrets.token_hex(16))
    check = verify_upload_grant("TEST-ONLY-grant", grant, "authtest-up", kind="voice")
    first = asyncio.run(consume_upload_grant(store, check))
    second = asyncio.run(consume_upload_grant(store, check))
    assert first.ok and second.error == "Upload grant already used"


def test_purge_removes_only_expired_nonces(strict_scratch_db, env):
    store = PostgresNonceStore(connection_factory(Settings()))
    with db() as conn:
        conn.execute("INSERT INTO public.request_nonces (nonce, expires_at) VALUES "
                     "('authtest-old', now() - interval '1 minute'), ('authtest-new', now() + interval '1 hour')")
    asyncio.run(store.purge_expired())
    with db() as conn:
        left = {r[0] for r in conn.execute("SELECT nonce FROM public.request_nonces WHERE nonce LIKE 'authtest-%'")}
    assert left == {"authtest-new"}
