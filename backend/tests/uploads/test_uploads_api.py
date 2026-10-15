"""Phase 6 donor upload API end to end through the real app (create_app, router, CORS, error handlers) with
in-process fakes for the database-backed parts: creator rows (behind the real PublicService), the nonce
store, the rate limiter and R2. tests/uploads/test_uploads_db.py repeats the key flows on PostgreSQL."""

import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.public.service import PublicService
from app.security.nonce_store import InMemoryNonceStore
from app.security.crypto import hmac_sha256_hex
from app.security.upload_grant import GRANT_TTL_MS, issue_upload_grant
from app.storage.memory import InMemoryObjectStore
from app.storage.r2 import IMMUTABLE
from app.uploads.service import PublicCreators, UploadService
from tests.uploads import webm_builder as b
from tests.uploads.test_media import GIF, JPEG, MP4, PNG, WEBP

P = "/api/v1/public/streamers"
FRONT = "https://app.quicktestspace.site"
ORIGIN = {"origin": FRONT}
GRANT_SECRET = "TEST-ONLY-upload-grant-secret"
NOW = 1_800_000_000_000
REAL_VOICE = (Path(__file__).parent / "fixtures" / "chromium152_mediarecorder_opus_1440ms.webm").read_bytes()
VOICE_URL = re.compile(r"^https://media\.example\.test/voice-messages/([a-z0-9_-]+)_(\d{13})_([0-9a-f]{32})\.webm$")
MEDIA_URL = re.compile(r"^https://media\.example\.test/donation-media/([a-z0-9_-]+)_(\d{13})_([0-9a-f]{32})\.(\w+)$")
LEAKS = re.compile(r"Traceback|File \"|site-packages|botocore|psycopg|SELECT |INSERT |/opt/|/etc/|[A-Z]:\\\\|"
                   r"TEST-ONLY|r2\.cloudflarestorage|x-amz|Signature=", re.I)


class FakePublicRepo:
    """Rows as PublicRepository.active_streamer returns them; only `active` creators are found."""

    def __init__(self) -> None:
        self.rows = {
            "creator-a": dict(id="id-a", streamer_slug="creator-a", status="active", media_upload_enabled=True,
                              media_max_file_size_mb=Decimal("10")),
            "creator-b": dict(id="id-b", streamer_slug="creator-b", status="active", media_upload_enabled=True,
                              media_max_file_size_mb=Decimal("1")),
            "no-media": dict(id="id-n", streamer_slug="no-media", status="active", media_upload_enabled=False,
                             media_max_file_size_mb=Decimal("10")),
            "zero-media": dict(id="id-z", streamer_slug="zero-media", status="active", media_upload_enabled=True,
                               media_max_file_size_mb=Decimal("0")),
            "pending-c": dict(id="id-p", streamer_slug="pending-c", status="pending", media_upload_enabled=True,
                              media_max_file_size_mb=None),
        }
        self.blocks: dict[str, str] = {}
        self.tiers = {"id-a": [(150, 15), (300, 30)]}  # creator-a: longest tier 30 s; others: fallback (15 s)

    async def donation_block(self, streamer_id):
        return self.blocks.get(streamer_id)  # the real rule: public.creator_donation_block (migration 0020)

    async def active_streamer(self, slug):
        row = self.rows.get(slug)
        return dict(row) if row and row["status"] == "active" else None

    async def voice_tiers(self, streamer_id):
        return self.tiers.get(streamer_id, []), []


class FakeLimiter:
    def __init__(self) -> None:
        self.counts: Counter = Counter()
        self.calls: list[tuple] = []

    async def hit_rate_limit(self, key, bucket, limit, window_seconds):
        self.calls.append((key, bucket, limit, window_seconds))
        self.counts[(key, bucket)] += 1
        return self.counts[(key, bucket)] <= limit


class World:
    def __init__(self) -> None:
        self.clock = [NOW]
        self.nonces = InMemoryNonceStore()
        self.limiter = FakeLimiter()
        self.store = InMemoryObjectStore()
        self.repo = FakePublicRepo()

    def service(self, *, secret=GRANT_SECRET, store="default") -> UploadService:
        return UploadService(grant_secret=SecretStr(secret) if secret is not None else None,
                             creators=PublicCreators(PublicService(self.repo)), nonces=self.nonces,
                             limiter=self.limiter, store=self.store if store == "default" else store,
                             now_ms=lambda: self.clock[0])


@pytest.fixture
def world():
    return World()


@pytest.fixture
def client(monkeypatch, world):
    monkeypatch.setenv("HYPERCHAT_DB_ENABLED", "false")
    monkeypatch.setenv("HYPERCHAT_APP_ENV", "test")
    monkeypatch.setenv("HYPERCHAT_FRONTEND_URL", FRONT)
    monkeypatch.setenv("HYPERCHAT_ALLOWED_ORIGINS", "http://localhost:5173")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    bodies: list[str] = []
    with TestClient(create_app(), base_url="https://testserver") as c:
        c.app.state.uploads = world.service()
        c.event_hooks["response"].append(lambda r: bodies.append(r.read().decode("utf-8", "replace")))
        yield c
    get_settings.cache_clear()
    leaked = [x[:200] for x in bodies if LEAKS.search(x)]
    assert not leaked, leaked  # no stack traces, paths, SQL, credentials or signed URLs in any response


def get_grant(client, slug="creator-a", kind="voice", headers=ORIGIN) -> str:
    r = client.post(f"{P}/{slug}/upload-grant", json={"kind": kind}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["grant"]


def upload(client, kind, data, content_type, grant, slug="creator-a", origin=FRONT, **extra):
    headers = {"content-type": content_type, **({"origin": origin} if origin else {}),
               **({"x-upload-grant": grant} if grant is not None else {}), **extra}
    return client.post(f"{P}/{slug}/uploads/{kind}", content=data, headers=headers)


# ── upload-grant ────────────────────────────────────────────────────────────────

def test_grant_issued_creator_and_type_bound(client, world):
    r = client.post(f"{P}/CREATOR-A/upload-grant", json={"kind": "voice"}, headers=ORIGIN)
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    assert set(r.json()) == {"grant", "expires_at"}
    slug, kind, expiry, nonce, sig = r.json()["grant"].split(".")
    assert (slug, kind, int(expiry)) == ("creator-a", "voice", NOW + GRANT_TTL_MS)  # canonical slug
    assert r.json()["expires_at"] == "2027-01-15T08:10:00.000Z"
    assert not world.nonces._seen  # issuing does not burn anything
    assert world.limiter.calls[-1] == ("ip:testclient", "upload:grant", 10, 60)


@pytest.mark.parametrize("origin", [None, "https://evil.invalid", "https://hyperchat.space", "null",
                                    "http://app.quicktestspace.site", "https://app.quicktestspace.site.evil.invalid"])
def test_grant_requires_a_trusted_origin(client, world, origin):
    r = client.post(f"{P}/creator-a/upload-grant", json={"kind": "voice"}, headers={"origin": origin} if origin else {})
    assert (r.status_code, r.json()) == (403, {"error": "Request origin not allowed"})
    assert not world.limiter.calls  # rejected before anything else


def test_grant_from_localhost_dev_origin(client):
    assert get_grant(client, headers={"origin": "http://localhost:5173"})


def test_origin_is_checked_before_the_body(client):
    r = client.post(f"{P}/creator-a/upload-grant", json={"kind": "nope"}, headers={"origin": "https://evil.invalid"})
    assert r.status_code == 403


@pytest.mark.parametrize("body", [{}, {"kind": "video"}, {"kind": "voice", "slug": "creator-b"}, None])
def test_grant_body_validated(client, body):
    r = client.post(f"{P}/creator-a/upload-grant", json=body, headers=ORIGIN)
    assert r.status_code == 422


@pytest.mark.parametrize("slug", ["unknown-x", "pending-c", "Bad%20Slug", "a" * 70, "x'%20OR%20'1'='1"])
def test_grant_unknown_inactive_or_malformed_creator_is_one_404(client, slug):
    r = client.post(f"{P}/{slug}/upload-grant", json={"kind": "voice"}, headers=ORIGIN)
    assert (r.status_code, r.json()) == (404, {"error": "Streamer not found"})


def test_a_creator_that_cannot_accept_donations_gets_no_grant_and_no_upload(client, world):
    """Migration 0020: uploads only for a creator that may accept new donations; a generic 503, never why."""
    grant = get_grant(client)  # issued while the creator could accept donations
    world.repo.blocks["id-a"] = "kyc_required"
    r = client.post(f"{P}/creator-a/upload-grant", json={"kind": "voice"}, headers=ORIGIN)
    assert (r.status_code, r.json()) == (503, {"error": "Donations are temporarily unavailable"})
    r = upload(client, "voice", REAL_VOICE, "audio/webm", grant)
    assert (r.status_code, r.json()) == (503, {"error": "Donations are temporarily unavailable"})
    assert not world.store.objects


@pytest.mark.parametrize("slug", ["no-media", "zero-media"])
def test_media_grant_needs_media_enabled(client, slug):
    r = client.post(f"{P}/{slug}/upload-grant", json={"kind": "media"}, headers=ORIGIN)
    assert (r.status_code, r.json()) == (403, {"error": "Media uploads are not enabled for this creator"})
    assert get_grant(client, slug, "voice")  # voice is unaffected


def test_path_traversal_slug_never_reaches_a_creator(client):
    r = client.post(f"{P}/..%2f..%2fadmin/upload-grant", json={"kind": "voice"}, headers=ORIGIN)
    assert r.status_code == 404


def test_grant_rate_limit_10_per_minute(client, world):
    for _ in range(10):
        get_grant(client)
    r = client.post(f"{P}/creator-a/upload-grant", json={"kind": "voice"}, headers=ORIGIN)
    assert (r.status_code, r.json()) == (429, {"error": "Too many requests. Please try again shortly."})


@pytest.mark.parametrize("setup", ["no_secret", "blank_secret", "no_store", "no_service"])
def test_unconfigured_uploads_answer_503(client, world, setup):
    if setup == "no_service":
        client.app.state.uploads = None
    else:
        client.app.state.uploads = world.service(secret={"no_secret": None, "blank_secret": "  "}.get(setup, GRANT_SECRET),
                                                 store=None if setup == "no_store" else "default")
    for path, kw in ((f"{P}/creator-a/upload-grant", {"json": {"kind": "voice"}}),
                     (f"{P}/creator-a/uploads/voice", {"content": REAL_VOICE}),
                     (f"{P}/creator-a/uploads/media", {"content": JPEG})):
        r = client.post(path, headers=ORIGIN, **kw)
        assert (r.status_code, r.json()) == (503, {"error": "Uploads are not available"})


# ── voice ───────────────────────────────────────────────────────────────────────

def test_voice_upload_real_recording(client, world):
    grant = get_grant(client)
    r = upload(client, "voice", REAL_VOICE, "audio/webm;codecs=opus", grant)
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    assert set(r.json()) == {"voice_message_url"}
    m = VOICE_URL.match(r.json()["voice_message_url"])
    assert m and m.group(1) == "creator-a" and int(m.group(2)) == NOW
    key = r.json()["voice_message_url"].removeprefix("https://media.example.test/")
    stored = world.store.objects[key]
    assert (stored.data, stored.content_type, stored.cache_control) == (REAL_VOICE, "audio/webm", IMMUTABLE)
    assert 1435 <= int(stored.metadata["duration-ms"]) <= 1445 and set(stored.metadata) == {"duration-ms"}
    assert f"upload:{grant.split('.')[3]}" in world.nonces  # burned in the nonce store
    assert world.limiter.calls[-1] == ("ip:testclient", "upload:voice", 5, 60)


def test_voice_object_names_are_unique_random_and_server_chosen(client, world):
    urls = set()
    for _ in range(3):
        r = upload(client, "voice", REAL_VOICE, "audio/webm", get_grant(client),
                   **{"x-upload-key": "../../hypersounds/evil.mp3"})
        urls.add(r.json()["voice_message_url"])
    assert len(urls) == 3 and all(VOICE_URL.match(u) for u in urls)
    assert all(k.startswith("voice-messages/creator-a_") for k in world.store.objects)


def test_voice_grant_is_single_use(client, world):
    grant = get_grant(client)
    assert upload(client, "voice", REAL_VOICE, "audio/webm", grant).status_code == 201
    r = upload(client, "voice", REAL_VOICE, "audio/webm", grant)
    assert (r.status_code, r.json()) == (403, {"error": "Upload grant already used"})
    assert len(world.store.objects) == 1


def test_voice_grant_expiry(client, world):
    grant = get_grant(client)
    world.clock[0] = NOW + GRANT_TTL_MS + 1
    r = upload(client, "voice", REAL_VOICE, "audio/webm", grant)
    assert (r.status_code, r.json()) == (403, {"error": "Upload grant expired"})


def test_cross_creator_isolation(client, world):
    grant_a = get_grant(client, "creator-a")
    r = upload(client, "voice", REAL_VOICE, "audio/webm", grant_a, slug="creator-b")
    assert (r.status_code, r.json()) == (403, {"error": "Upload grant does not match this creator"})
    forged = grant_a.replace("creator-a.", "creator-b.", 1)  # re-labelled grant: signature breaks
    r = upload(client, "voice", REAL_VOICE, "audio/webm", forged, slug="creator-b")
    assert (r.status_code, r.json()) == (403, {"error": "Invalid upload grant"})
    assert not world.store.objects
    assert upload(client, "voice", REAL_VOICE, "audio/webm", grant_a).status_code == 201  # still usable for A


def test_grant_type_binding(client):
    media_grant = get_grant(client, kind="media")
    r = upload(client, "voice", REAL_VOICE, "audio/webm", media_grant)
    assert (r.status_code, r.json()) == (403, {"error": "Upload grant is not valid for this upload type"})
    voice_grant = get_grant(client, kind="voice")
    r = upload(client, "media", JPEG, "image/jpeg", voice_grant)
    assert (r.status_code, r.json()) == (403, {"error": "Upload grant is not valid for this upload type"})


@pytest.mark.parametrize("grant, error", [
    (None, "Upload grant required"), ("", "Upload grant required"), ("a.b.c", "Malformed upload grant"),
    ("creator-a." + "x" * 600, "Malformed upload grant"),
])
def test_missing_or_malformed_grant(client, grant, error):
    r = upload(client, "voice", REAL_VOICE, "audio/webm", grant)
    assert (r.status_code, r.json()) == (403, {"error": error})


def test_legacy_untyped_grant_rejected(client):
    payload = f"creator-a.{NOW + GRANT_TTL_MS}.{'a' * 32}"  # the legacy 4-part form, correctly signed
    legacy = f"{payload}.{hmac_sha256_hex(GRANT_SECRET, payload)}"
    r = upload(client, "voice", REAL_VOICE, "audio/webm", legacy)
    assert (r.status_code, r.json()) == (403, {"error": "Malformed upload grant"})


def test_grant_signed_with_another_secret_rejected(client):
    other, _ = issue_upload_grant("TEST-ONLY-another-secret", "creator-a", kind="voice", now_ms=NOW)
    r = upload(client, "voice", REAL_VOICE, "audio/webm", other)
    assert (r.status_code, r.json()) == (403, {"error": "Invalid upload grant"})


@pytest.mark.parametrize("ct, error", [
    ("video/webm", "Video uploads are not supported"), ("audio/ogg", "Only WebM voice messages are supported"),
    ("audio/mpeg", "Only WebM voice messages are supported"), ("application/octet-stream",
                                                               "Only WebM voice messages are supported"),
])
def test_voice_declared_type(client, ct, error):
    r = upload(client, "voice", REAL_VOICE, ct, get_grant(client))
    assert (r.status_code, r.json()) == (415, {"error": error})


@pytest.mark.parametrize("data, error", [
    (JPEG, "Voice message must be a WebM recording with Opus audio"),
    (b"OggS" + b"\x00" * 100, "Voice message must be a WebM recording with Opus audio"),
    (b.recording(2, track=(1, 2, "A_VORBIS")), "Voice message must be a WebM recording with Opus audio"),
    ((Path(__file__).parent / "fixtures" / "chromium152_mediarecorder_vp8_opus_av.webm").read_bytes(),
     "Video uploads are not supported"),
], ids=["jpeg", "ogg", "vorbis-webm", "real-video-webm"])
def test_voice_bytes_must_be_webm_opus_audio(client, world, data, error):
    grant = get_grant(client)
    r = upload(client, "voice", data, "audio/webm", grant)
    assert (r.status_code, r.json()) == (415, {"error": error})
    assert not world.store.objects
    assert upload(client, "voice", REAL_VOICE, "audio/webm", grant).status_code == 201  # grant not burned


def test_voice_duration_limit_uses_the_creators_longest_tier(client, world):
    # creator-a: tiers up to 30 s (+2 s tolerance); creator-b: platform fallback, 15 s
    assert upload(client, "voice", b.recording(31.5), "audio/webm", get_grant(client)).status_code == 201
    r = upload(client, "voice", b.recording(33), "audio/webm", get_grant(client))
    assert (r.status_code, r.json()) == (422, {"error": "Voice message is too long. Maximum is 30 seconds."})
    r = upload(client, "voice", b.recording(20), "audio/webm", get_grant(client, "creator-b"), slug="creator-b")
    assert (r.status_code, r.json()) == (422, {"error": "Voice message is too long. Maximum is 15 seconds."})


def test_voice_duration_from_content_not_metadata(client):
    # header claims 1 s, timecodes all 0 -> still 40 s of Opus packets
    blocks = [b.block_payload([b.opus(20)]) for _ in range(2000)]
    lying = b.document(b.info(duration=1000.0) + b.tracks() + b.cluster(0, blocks))
    r = upload(client, "voice", lying, "audio/webm", get_grant(client))
    assert r.status_code == 422


def test_voice_size_limit_declared_and_streamed(client, world):
    grant = get_grant(client)
    big = b"\x00" * (5 * 1024 * 1024 + 1)
    r = upload(client, "voice", big, "audio/webm", grant)
    assert (r.status_code, r.json()) == (413, {"error": "Voice message too large. Maximum size is 5MB."})
    chunks = (b"\x00" * (1024 * 1024) for _ in range(6))  # chunked, no Content-Length
    r = upload(client, "voice", chunks, "audio/webm", grant)
    assert r.status_code == 413
    assert upload(client, "voice", REAL_VOICE, "audio/webm", grant).status_code == 201  # never burned


def test_empty_voice_body(client):
    r = upload(client, "voice", b"", "audio/webm", get_grant(client))
    assert (r.status_code, r.json()) == (400, {"error": "Upload body is empty"})


def test_voice_rate_limit_5_per_minute_counts_failures(client, world):
    grant = get_grant(client)
    for _ in range(5):
        assert upload(client, "voice", JPEG, "audio/webm", grant).status_code == 415
    r = upload(client, "voice", REAL_VOICE, "audio/webm", grant)
    assert (r.status_code, r.json()) == (429, {"error": "Too many uploads. Please try again later."})


@pytest.mark.parametrize("origin", [None, "https://evil.invalid", "https://hyperchat.space"])
def test_uploads_require_a_trusted_origin(client, world, origin):
    grant = get_grant(client)
    for kind, data, ct in (("voice", REAL_VOICE, "audio/webm"), ("media", JPEG, "image/jpeg")):
        r = upload(client, kind, data, ct, grant, origin=origin)
        assert (r.status_code, r.json()) == (403, {"error": "Request origin not allowed"})
    assert not world.store.objects


def test_storage_failure_is_a_generic_502(client, world):
    world.store.fail_with = "r2_AccessDenied"
    grant = get_grant(client)
    r = upload(client, "voice", REAL_VOICE, "audio/webm", grant)
    assert (r.status_code, r.json()) == (502, {"error": "Upload storage failed. Please try again."})
    world.store.fail_with = None
    r = upload(client, "voice", REAL_VOICE, "audio/webm", grant)  # burned before the write: get a new grant
    assert r.status_code == 403


def test_nonce_store_failure_is_503_and_nothing_stored(client, world):
    grant = get_grant(client)
    world.nonces.fail_with = RuntimeError("db down")
    r = upload(client, "voice", REAL_VOICE, "audio/webm", grant)
    assert (r.status_code, r.json()) == (503, {"error": "Uploads are not available"})
    assert not world.store.objects


def test_unknown_or_inactive_creator_uploads_are_404(client):
    grant, _ = issue_upload_grant(GRANT_SECRET, "pending-c", kind="voice", now_ms=NOW)
    for slug in ("pending-c", "unknown-x"):
        r = upload(client, "voice", REAL_VOICE, "audio/webm", grant, slug=slug)
        assert (r.status_code, r.json()) == (404, {"error": "Streamer not found"})


# ── media ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("data, ct, ext, media_type", [
    (JPEG, "image/jpeg", "jpg", "image"), (PNG, "image/png", "png", "image"), (WEBP, "image/webp", "webp", "image"),
    (GIF, "image/gif", "gif", "gif"), (JPEG, "image/jpg", "jpg", "image"),
], ids=["jpeg", "png", "webp", "gif", "jpg-alias"])
def test_media_upload_each_type(client, world, data, ct, ext, media_type):
    r = upload(client, "media", data, ct, get_grant(client, kind="media"))
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    assert set(r.json()) == {"media_url", "media_type"} and r.json()["media_type"] == media_type
    m = MEDIA_URL.match(r.json()["media_url"])
    assert m and m.group(1) == "creator-a" and m.group(4) == ext
    stored = world.store.objects[r.json()["media_url"].removeprefix("https://media.example.test/")]
    assert (stored.data, stored.cache_control, stored.metadata) == (data, IMMUTABLE, {})
    assert stored.content_type == ("image/jpeg" if ct == "image/jpg" else ct)
    assert world.limiter.calls[-1] == ("ip:testclient", "upload:media", 3, 60)


@pytest.mark.parametrize("data, ct, error", [
    (JPEG, "image/png", "File content does not match its type"),
    (PNG, "image/gif", "File content does not match its type"),
    (b"<svg/>" + b" " * 64, "image/png", "File content does not match its type"),
    (MP4, "image/jpeg", "Video uploads are not supported"),
    (MP4, "video/mp4", "Video uploads are not supported"),
    (REAL_VOICE, "image/webp", "Video uploads are not supported"),
    (JPEG, "image/svg+xml", "Unsupported file type. Allowed: JPG, PNG, WebP, GIF"),
], ids=["jpeg-as-png", "png-as-gif", "svg-as-png", "mp4-as-jpeg", "mp4-as-video", "webm-as-webp", "svg-type"])
def test_media_type_and_signature(client, world, data, ct, error):
    grant = get_grant(client, kind="media")
    r = upload(client, "media", data, ct, grant)
    assert (r.status_code, r.json()) == (415, {"error": error})
    assert not world.store.objects


def test_media_size_honours_creator_setting(client, world):
    # creator-b allows 1 MB
    big = JPEG + b"\x00" * (1024 * 1024)
    r = upload(client, "media", big, "image/jpeg", get_grant(client, "creator-b", "media"), slug="creator-b")
    assert (r.status_code, r.json()) == (413, {"error": "File too large. Maximum size is 1MB."})
    assert upload(client, "media", big, "image/jpeg", get_grant(client, "creator-a", "media")).status_code == 201


def test_media_platform_cap_10mb(client, world):
    world.repo.rows["creator-a"]["media_max_file_size_mb"] = Decimal("25")
    r = upload(client, "media", JPEG + b"\x00" * (10 * 1024 * 1024), "image/jpeg", get_grant(client, kind="media"))
    assert (r.status_code, r.json()) == (413, {"error": "File too large. Maximum size is 10MB."})


def test_media_disabled_creator_rejects_even_a_valid_grant(client, world):
    grant, _ = issue_upload_grant(GRANT_SECRET, "no-media", kind="media", now_ms=NOW)
    r = upload(client, "media", JPEG, "image/jpeg", grant, slug="no-media")
    assert (r.status_code, r.json()) == (403, {"error": "Media uploads are not enabled for this creator"})


def test_media_rate_limit_3_per_minute(client):
    for _ in range(3):
        upload(client, "media", JPEG, "image/png", "x")
    r = upload(client, "media", JPEG, "image/jpeg", get_grant(client, kind="media"))
    assert (r.status_code, r.json()) == (429, {"error": "Too many uploads. Please try again later."})


# ── browser integration ─────────────────────────────────────────────────────────

def test_cors_preflight_allows_the_grant_header(client):
    r = client.options(f"{P}/creator-a/uploads/voice", headers={
        "origin": FRONT, "access-control-request-method": "POST",
        "access-control-request-headers": "content-type,x-upload-grant"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == FRONT
    assert "x-upload-grant" in r.headers["access-control-allow-headers"]
    evil = client.options(f"{P}/creator-a/uploads/voice", headers={"origin": "https://evil.invalid",
                                                                   "access-control-request-method": "POST"})
    assert "access-control-allow-origin" not in evil.headers


def test_get_on_upload_routes_is_405(client):
    assert client.get(f"{P}/creator-a/uploads/voice", headers=ORIGIN).status_code == 405
    assert client.get(f"{P}/creator-a/upload-grant", headers=ORIGIN).status_code == 405
