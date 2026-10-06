"""Phase 6 donor uploads on PostgreSQL: the real app with request_nonces, rate_limits, creator rows and voice
tiers in a scratch database (see tests/integration.py). R2 is replaced by the in-memory store; the grant
secret is a random TEST-ONLY value per test."""

import secrets
from datetime import UTC, datetime, timedelta

import pytest

from app.storage.memory import InMemoryObjectStore
from tests.integration import ORIGIN, db, make_streamer, requires_db, update_streamer
from tests.uploads import webm_builder as b
from tests.uploads.test_media import JPEG

pytestmark = requires_db
P = "/api/v1/public/streamers"


@pytest.fixture
def uploads_env(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_UPLOAD_GRANT_SECRET", "TEST-ONLY-" + secrets.token_hex(16))
    used: list[str] = []
    yield used
    with db() as conn:  # grants carry random nonces: remove exactly the ones these tests burned
        conn.execute("DELETE FROM public.request_nonces WHERE nonce = ANY(%s)", ([f"upload:{n}" for n in used],))


@pytest.fixture
def client(uploads_env, app):
    store = InMemoryObjectStore()
    app.app_state.uploads.store = store
    app.store = store
    yield app


def grant(client, used, slug, kind="voice"):
    r = client.post(f"{P}/{slug}/upload-grant", json={"kind": kind}, headers=ORIGIN)
    assert r.status_code == 201, r.text
    used.append(r.json()["grant"].split(".")[3])
    return r.json()["grant"]


def upload(client, slug, kind, data, ct, g):
    return client.post(f"{P}/{slug}/uploads/{kind}", content=data,
                       headers={**ORIGIN, "content-type": ct, "x-upload-grant": g})


def test_voice_upload_burns_the_grant_in_request_nonces(client, uploads_env):
    make_streamer("authtest-up-voice")
    g = grant(client, uploads_env, "authtest-up-voice")
    r = upload(client, "authtest-up-voice", "voice", b.recording(5), "audio/webm", g)
    assert r.status_code == 201 and r.json()["voice_message_url"].startswith(
        "https://media.example.test/voice-messages/authtest-up-voice_")
    with db() as conn:
        expires = conn.execute("SELECT expires_at FROM public.request_nonces WHERE nonce = %s",
                               (f"upload:{uploads_env[-1]}",)).fetchone()[0]
    assert timedelta(minutes=9) < expires - datetime.now(UTC) <= timedelta(minutes=10, seconds=5)
    again = upload(client, "authtest-up-voice", "voice", b.recording(5), "audio/webm", g)
    assert (again.status_code, again.json()) == (403, {"error": "Upload grant already used"})
    assert len(client.store.objects) == 1


def test_rate_limits_are_recorded_per_ip_and_action(client, uploads_env):
    make_streamer("authtest-up-rate")
    for _ in range(10):
        grant(client, uploads_env, "authtest-up-rate")
    r = client.post(f"{P}/authtest-up-rate/upload-grant", json={"kind": "voice"}, headers=ORIGIN)
    assert (r.status_code, r.json()) == (429, {"error": "Too many requests. Please try again shortly."})
    with db() as conn:
        rows = conn.execute("SELECT ip_address, endpoint, request_count FROM public.rate_limits "
                            "WHERE endpoint LIKE 'upload:%%' ORDER BY endpoint").fetchall()
    assert rows == [("ip:testclient", "upload:grant", 11)]


def test_inactive_creator_and_media_switch_come_from_the_database(client, uploads_env):
    sid = make_streamer("authtest-up-media")
    update_streamer(sid, status="pending")
    r = client.post(f"{P}/authtest-up-media/upload-grant", json={"kind": "voice"}, headers=ORIGIN)
    assert (r.status_code, r.json()) == (404, {"error": "Streamer not found"})
    update_streamer(sid, status="active")  # media_upload_enabled defaults to false
    r = client.post(f"{P}/authtest-up-media/upload-grant", json={"kind": "media"}, headers=ORIGIN)
    assert (r.status_code, r.json()) == (403, {"error": "Media uploads are not enabled for this creator"})
    update_streamer(sid, media_upload_enabled=True, media_max_file_size_mb=1)
    big = JPEG + b"\x00" * (1024 * 1024)
    slug = "authtest-up-media"
    r = upload(client, slug, "media", big, "image/jpeg", grant(client, uploads_env, slug, "media"))
    assert (r.status_code, r.json()) == (413, {"error": "File too large. Maximum size is 1MB."})
    r = upload(client, slug, "media", JPEG, "image/jpeg", grant(client, uploads_env, slug, "media"))
    assert r.status_code == 201 and r.json()["media_type"] == "image"


def test_voice_limit_follows_the_creators_tiers(client, uploads_env):
    sid = make_streamer("authtest-up-tiers")
    with db() as conn:
        conn.execute("INSERT INTO public.streamer_voice_tiers (streamer_id, min_amount_inr, max_duration_seconds) "
                     "VALUES (%s, 150, 6), (%s, 300, 10)", (sid, sid))
    slug = "authtest-up-tiers"
    r = upload(client, slug, "voice", b.recording(12.5), "audio/webm", grant(client, uploads_env, slug))
    assert (r.status_code, r.json()) == (422, {"error": "Voice message is too long. Maximum is 10 seconds."})
    r = upload(client, slug, "voice", b.recording(11), "audio/webm", grant(client, uploads_env, slug))
    assert r.status_code == 201
