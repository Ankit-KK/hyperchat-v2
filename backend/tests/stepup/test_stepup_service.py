"""The verifier as a service: its own settings (no application secrets, origins within the RP ID), and the full
ceremony through the API's client over a real Unix socket served by uvicorn (with PostgreSQL)."""

import asyncio
import os
import sys
import tempfile
import threading
import time

import pytest

from app.stepup.client import StepupClient, StepupRefused
from app.stepup.config import ALLOWED_KEYS, StepupSettings, unexpected_keys
from app.stepup.server import create_app
from app.stepup.verifier import StepupError, _client_data, fingerprint
from tests.integration import requires_db
from tests.stepup.authenticator import SoftAuthenticator

RP, ORIGIN = "hyperchat.test", "https://admin.hyperchat.test"


# ── settings (no database) ──────────────────────────────────────────────────────

@pytest.mark.parametrize("origins", ["https://hyperchat.test", "https://admin.hyperchat.test",
                                     "https://a.hyperchat.test,https://b.hyperchat.test"])
def test_origins_within_the_rp_id_are_accepted(origins):
    assert StepupSettings(webauthn_rp_id=RP, webauthn_origins=origins).origins == origins.split(",")


@pytest.mark.parametrize("rp,origins,env", [
    (RP, "https://evil.example", "production"), (RP, "https://hyperchat.test.evil.example", "production"),
    (RP, "https://xhyperchat.test", "production"), (RP, "http://admin.hyperchat.test", "production"),
    (RP, "https://admin.hyperchat.test/path", "production"), (RP, "https://admin.hyperchat.test/", "production"),
    (RP, "https://user@admin.hyperchat.test", "production"), (RP, "", "production"),
    ("localhost", "http://localhost:5173", "production"), ("https://hyperchat.test", ORIGIN, "production"),
])
def test_origins_outside_the_rp_id_or_not_https_are_refused(rp, origins, env):
    with pytest.raises(ValueError):
        StepupSettings(webauthn_rp_id=rp, webauthn_origins=origins, app_env=env)


def test_http_localhost_is_for_development_only():
    assert StepupSettings(webauthn_rp_id="localhost", webauthn_origins="http://localhost:5173").origins


def test_the_verifier_refuses_to_start_with_any_application_secret_in_its_environment():
    settings = StepupSettings(webauthn_rp_id=RP, webauthn_origins=ORIGIN)
    for key in ("HYPERCHAT_AUTH_SECRET", "HYPERCHAT_RAZORPAY_PRIMARY_KEY_SECRET", "HYPERCHAT_DIDIT_API_KEY",
                "HYPERCHAT_TELEGRAM_BOT_TOKEN", "HYPERCHAT_DB_USER", "HYPERCHAT_DB_AUTH_USER", "HYPERCHAT_DB_PASSWORD"):
        assert unexpected_keys({key: "x"}) == [key]
        with pytest.raises(RuntimeError, match=key):
            create_app(settings, environ={key: "x", "HYPERCHAT_WEBAUTHN_RP_ID": RP})
    assert create_app(settings, environ={k: "x" for k in ALLOWED_KEYS}) is not None


def test_fingerprints_are_five_groups_of_four_base32_characters():
    import re
    prints = {fingerprint(os.urandom(77)) for _ in range(50)}
    assert len(prints) == 50 and all(re.fullmatch(r"[A-Z2-7]{4}(-[A-Z2-7]{4}){4}", p) for p in prints)


@pytest.mark.parametrize("extra", [{"crossOrigin": True}, {"topOrigin": "https://evil.example"}])
def test_a_ceremony_in_a_cross_origin_frame_is_refused(extra):
    response = SoftAuthenticator(RP, ORIGIN).register({"challenge": "abc", "user": {"id": "u"}}, client_extra=extra)
    with pytest.raises(StepupError, match="cross_origin"):
        _client_data(response)


# ── over a real Unix socket (database) ──────────────────────────────────────────

@pytest.fixture
def socket_service(env):
    import uvicorn

    from tests.integration import PG_HOST, target
    name, port = target()
    settings = StepupSettings(webauthn_rp_id=RP, webauthn_origins=ORIGIN, db_host=PG_HOST, db_port=int(port),
                              db_name=name, log_level="WARNING")
    path = os.path.join(tempfile.mkdtemp(prefix="stepup-"), "stepup.sock")
    server = uvicorn.Server(uvicorn.Config(create_app(settings, environ={}), uds=path, log_level="warning",
                                           lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    yield path
    server.should_exit = True
    thread.join(10)


@requires_db
@pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets")
def test_the_api_client_runs_a_full_ceremony_over_the_unix_socket(socket_service):
    import httpx

    from tests.integration import DOMAIN, fixture_db
    from tests.stepup.test_stepup_db import admin, approve
    uid, sid = admin("su-socket")
    auth = SoftAuthenticator(RP, ORIGIN)

    async def scenario():
        client = StepupClient(socket_service)
        try:
            health = await httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=socket_service)).get(
                "http://stepup/health")
            assert health.json()["enforcement"] == "draft"
            options = await client.registration_options(uid, sid)
            registered = await client.registration_verify(uid, sid, options["challenge_id"],
                                                          auth.register(options["public_key"]))
            assert approve("su-socket" + DOMAIN, registered["fingerprint"]) == "ok"
            options = await client.assertion_options(uid, sid, "creator.activate", "creator:x", {"expected_status": "pending"})
            assert options["public_key"]["userVerification"] == "required"
            grant = await client.assertion_verify(uid, sid, options["challenge_id"], auth.assert_(options["public_key"]))
            with pytest.raises(StepupRefused) as replay:
                await client.assertion_verify(uid, sid, options["challenge_id"], auth.assert_(options["public_key"]))
            with pytest.raises(StepupRefused) as bad:
                await client.assertion_options(uid, sid, "creator.delete", "creator:x")
            return grant, replay.value, bad.value
        finally:
            await client.aclose()
    grant, replay, bad = asyncio.run(scenario())
    assert grant["action"] == "creator.activate" and (replay.status, replay.code) == (409, "challenge_used")
    assert (bad.status, bad.code) == (400, "bad_action")
    with fixture_db() as conn:
        assert conn.execute("SELECT public.stepup_consume(%s, %s, %s, %s, %s, %s)",
                            (grant["grant_id"], uid, sid, "creator.activate", "creator:x", grant["params_hash"])
                            ).fetchone()[0] == "ok"
