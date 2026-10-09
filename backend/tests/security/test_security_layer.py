"""Server-to-server security building blocks: HMAC signing, nonces, upload grants, OBS tokens,
shared secrets / partner keys, CORS and log hygiene. All secrets here are TEST-ONLY values."""

import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.logging import JsonFormatter
from app.security.audit import security_event
from app.security.cors import CorsMiddleware, cors_headers
from app.security.crypto import sha256_hex
from app.security.nonce_store import InMemoryNonceStore
from app.security.obs_token import (
    ObsTokenInfo,
    default_scope_for_obs,
    get_obs_token,
    is_obs_origin_allowed,
    obs_channel_allowed,
    validate_obs_token,
)
from app.security.request_signing import sign_request, verify_hmac_request
from app.security.shared_secret import ERR_PARTNER_KEY, PartnerKey, verify_partner_api_key, verify_shared_secret
from app.security.upload_grant import (
    UploadGrantNotConfigured,
    consume_upload_grant,
    issue_upload_grant,
    verify_upload_grant,
)

KEY = "TEST-ONLY-hmac-key"
GRANT_SECRET = "TEST-ONLY-grant-secret"
BODY = b'{"event":"x"}'
NOW = 1_800_000_000_000


# ── HMAC server-to-server signing ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_signed_request_accepted_once_then_replay_rejected():
    store = InMemoryNonceStore()
    headers = sign_request(KEY, BODY, NOW, "n" * 32)
    assert (await verify_hmac_request(headers, BODY, [KEY], store, now_ms=NOW)).ok
    replay = await verify_hmac_request(headers, BODY, [KEY], store, now_ms=NOW)
    assert (replay.ok, replay.status, replay.reason) == (False, 409, "replay")


@pytest.mark.anyio
async def test_key_rotation_accepts_any_configured_key():
    headers = sign_request("TEST-ONLY-old-key", BODY, NOW, "r" * 32)
    assert (await verify_hmac_request(headers, BODY, [KEY, "TEST-ONLY-old-key"], InMemoryNonceStore(), now_ms=NOW)).ok


@pytest.mark.anyio
@pytest.mark.parametrize("mutate, reason", [
    (lambda h: h.pop("x-signature"), "missing_signature"),
    (lambda h: h.pop("x-nonce"), "missing_signature"),
    (lambda h: h.update({"x-timestamp": "12ab"}), "invalid_timestamp"),
    (lambda h: h.update({"x-timestamp": str(NOW - 5 * 60 * 1000 - 1)}), "timestamp_skew"),
    (lambda h: h.update({"x-nonce": "short"}), "invalid_nonce"),
    (lambda h: h.update({"x-signature": "0" * 64}), "bad_signature"),
])
async def test_bad_signatures_rejected(mutate, reason):
    headers = sign_request(KEY, BODY, NOW, "m" * 32)
    mutate(headers)
    result = await verify_hmac_request(headers, BODY, [KEY], InMemoryNonceStore(), now_ms=NOW)
    assert (result.ok, result.status, result.reason) == (False, 401, reason)


@pytest.mark.anyio
async def test_tampered_body_and_wrong_key_rejected():
    headers = sign_request(KEY, BODY, NOW, "t" * 32)
    assert (await verify_hmac_request(headers, b'{"event":"y"}', [KEY], InMemoryNonceStore(), now_ms=NOW)).reason \
        == "bad_signature"
    assert (await verify_hmac_request(headers, BODY, ["TEST-ONLY-other"], InMemoryNonceStore(), now_ms=NOW)).reason \
        == "bad_signature"


@pytest.mark.anyio
async def test_unconfigured_key_and_store_failure_fail_closed():
    headers = sign_request(KEY, BODY, NOW, "u" * 32)
    assert (await verify_hmac_request(headers, BODY, [], InMemoryNonceStore(), now_ms=NOW)).status == 503
    store = InMemoryNonceStore()
    store.fail_with = RuntimeError("db down")
    result = await verify_hmac_request(headers, BODY, [KEY], store, now_ms=NOW)
    assert (result.ok, result.status) == (False, 500)


# ── upload grants ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_upload_grant_single_use_and_bound_to_creator():
    grant, expires_at = issue_upload_grant(GRANT_SECRET, "creator-a", kind="voice", now_ms=NOW)
    assert expires_at.endswith("Z") and grant.startswith("creator-a.voice.")
    store = InMemoryNonceStore()
    assert (await _use(store, grant, "creator-b", NOW)).error == "Upload grant does not match this creator"
    assert (await _use(store, grant, "creator-a", NOW)).ok
    assert (await _use(store, grant, "creator-a", NOW)).error == "Upload grant already used"


@pytest.mark.anyio
async def test_upload_grant_expiry_tamper_and_malformed():
    grant, _ = issue_upload_grant(GRANT_SECRET, "creator-a", kind="voice", now_ms=NOW)
    store = InMemoryNonceStore()
    assert (await _use(store, grant, "creator-a", NOW + 10 * 60 * 1000 + 1)).error == "Upload grant expired"
    slug, kind, expiry, nonce, sig = grant.split(".")
    forged = f"{slug}.{kind}.{int(expiry) + 60_000}.{nonce}.{sig}"
    assert (await _use(store, forged, "creator-a", NOW)).error == "Invalid upload grant"
    assert (await _use(store, grant, "creator-a", NOW, secret="TEST-ONLY-other")).error == "Invalid upload grant"
    for bad in ("", None, "a.b.c", f"{slug}.{kind}.12x.{nonce}.{sig}", f"{slug}.{kind}.-5.{nonce}.{sig}",
                f"{slug}.{expiry}.{nonce}.{sig}"):  # the last one: the legacy untyped shape
        assert not (await _use(store, bad, "creator-a", NOW)).ok
    assert (await _use(store, grant, "creator-a", NOW)).ok  # none of the refusals burned the real grant


async def _use(store, grant, slug, now_ms, secret=GRANT_SECRET):
    """Verify, then burn: what the upload API does (app.uploads.service)."""
    check = verify_upload_grant(secret, grant, slug, kind="voice", now_ms=now_ms)
    return await consume_upload_grant(store, check) if check.ok else check


def test_upload_grant_requires_secret():
    with pytest.raises(UploadGrantNotConfigured):
        issue_upload_grant(None, "creator-a", kind="voice")


# ── OBS tokens ──────────────────────────────────────────────────────────────────

INFO = ObsTokenInfo("st-1", "creator-a", "Creator A")


class FakeObsTokens:
    """Keyed by SHA-256, like obs_tokens.token_hash: raw tokens never reach the store."""

    def __init__(self, tokens: dict[str, ObsTokenInfo], scopes: dict[str, list[str]] | None = None) -> None:
        self.tokens, self.scopes, self.seen = tokens, scopes or {}, []

    async def validate(self, token_hash):
        self.seen.append(token_hash)
        return self.tokens.get(token_hash)

    async def channel_scope(self, token_hash, streamer_id):
        self.seen.append(token_hash)
        return self.scopes.get(token_hash)


def test_obs_token_precedence():
    assert get_obs_token({"x-obs-token": "h"}, {"token": "q"}, {"obs_token": "b"}) == "h"
    assert get_obs_token({}, {"token": "q"}, {"obs_token": "b"}) == "q"
    assert get_obs_token({}, {}, {"obs_token": "b"}) == "b"
    assert get_obs_token({}, {}, None) == ""


ALLOWED = frozenset({"https://hyperchat.space", "http://localhost:5173"})


@pytest.mark.parametrize("origin, allowed", [
    (None, True), ("", True), ("https://hyperchat.space", True), ("http://localhost:5173", True),
    ("https://www.hyperchat.space", False), ("http://hyperchat.space", False), ("https://hyperchat.space:8443", False),
    ("https://x.lovable.app", False), ("https://hyperchat.space.evil.example", False), ("not a url", False),
])
def test_obs_origin_gate(origin, allowed):
    assert is_obs_origin_allowed(origin, ALLOWED) is allowed


@pytest.mark.anyio
async def test_obs_token_is_looked_up_by_hash_only():
    store = FakeObsTokens({sha256_hex("obs_good_token"): INFO})
    assert (await validate_obs_token(store, "obs_good_token")).token_info == INFO
    assert (await validate_obs_token(store, "obs_bad")).error == "invalid_token"
    assert (await validate_obs_token(store, "")).error == "missing_token"
    assert "obs_good_token" not in store.seen and sha256_hex("obs_good_token") in store.seen


@pytest.mark.anyio
async def test_obs_channel_scope_default_and_override():
    store = FakeObsTokens({}, scopes={sha256_hex("scoped"): ["private-creator-a-alerts"]})
    assert default_scope_for_obs("creator-a")[0] == "private-creator-a-alerts"
    assert await obs_channel_allowed(store, "t", INFO, "private-creator-a-goal")
    assert not await obs_channel_allowed(store, "t", INFO, "private-creator-a-dashboard")
    assert not await obs_channel_allowed(store, "t", INFO, "private-creator-b-alerts")
    assert not await obs_channel_allowed(store, "scoped", INFO, "private-creator-a-goal")


# ── shared secrets / partner keys ───────────────────────────────────────────────


def test_shared_secret_fails_closed():
    assert verify_shared_secret("TEST-ONLY-s3cret", "TEST-ONLY-s3cret")
    assert not verify_shared_secret("wrong", "TEST-ONLY-s3cret")
    assert not verify_shared_secret(None, "TEST-ONLY-s3cret")
    assert not verify_shared_secret("", "") and not verify_shared_secret("anything", None)


class FakePartnerKeys:
    def __init__(self, keys: dict[str, tuple[str, bool]]) -> None:
        self.keys = keys  # key_hash -> (partner_slug, revoked)

    async def find_active_by_hash(self, key_hash):
        entry = self.keys.get(key_hash)
        return None if not entry or entry[1] else PartnerKey(id="pk-1", partner_slug=entry[0])


@pytest.mark.anyio
async def test_partner_api_key():
    good, revoked = "p" * 40, "r" * 40
    store = FakePartnerKeys({sha256_hex(good): ("partner-a", False), sha256_hex(revoked): ("partner-b", True)})
    ok = await verify_partner_api_key(store, good)
    assert ok.ok and ok.partner.partner_slug == "partner-a"
    for key in (revoked, "u" * 40, "short", "x" * 201, None):
        r = await verify_partner_api_key(store, key)
        assert (r.ok, r.status, r.error) == (False, 401, ERR_PARTNER_KEY)


# ── CORS and log hygiene ────────────────────────────────────────────────────────


def _cors_app() -> TestClient:
    app = FastAPI()

    @app.get("/plain")
    async def plain():
        return {"ok": True}

    app.add_middleware(CorsMiddleware, allowed_origins=frozenset({"https://hyperchat.space",
                                                                  "https://preview.example.test"}))
    return TestClient(app)


@pytest.mark.parametrize("origin, allowed", [
    ("https://hyperchat.space", True), ("http://localhost:5173", True), ("https://www.hyperchat.space", False),
    ("https://x.lovable.app", False), ("https://x.lovableproject.com", False), ("https://hyperchat.site", False),
    ("http://hyperchat.space", False), ("https://evil.example", False), ("", False),
])
def test_cors_echoes_only_configured_origins(origin, allowed):
    headers = cors_headers(origin, ALLOWED)
    assert (headers.get("Access-Control-Allow-Origin") == origin) is allowed
    assert headers["Vary"] == "Origin"


def test_cors_preflight_and_response_headers():
    client = _cors_app()
    pre = client.options("/plain", headers={"origin": "https://hyperchat.space",
                                            "access-control-request-method": "POST"})
    assert pre.status_code == 200 and pre.text == "ok"
    assert pre.headers["access-control-allow-origin"] == "https://hyperchat.space"
    allowed = pre.headers["access-control-allow-headers"]
    assert "x-csrf-token" in allowed and "apikey" not in allowed and "x-supabase" not in allowed
    extra = client.get("/plain", headers={"origin": "https://preview.example.test"})
    assert extra.headers["access-control-allow-origin"] == "https://preview.example.test"
    evil = client.get("/plain", headers={"origin": "https://evil.example"})
    assert "access-control-allow-origin" not in evil.headers and evil.headers["vary"] == "Origin"


def test_security_events_drop_unsafe_fields(caplog):
    with caplog.at_level(logging.DEBUG):
        security_event("probe", token="SHOULD-NOT-APPEAR", email="a@example.invalid", reason="unit", status=401)
    formatted = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    event = json.loads(formatted.splitlines()[-1])
    assert event["security_event"] == "probe" and event["reason"] == "unit" and event["status"] == 401
    assert "SHOULD-NOT-APPEAR" not in formatted and "a@example.invalid" not in formatted
