"""Soundboard pure rules, MP3 validation, storage key rules and presigning, and the public hypersound library
(no database: the library route runs on the real app with the database disabled)."""

import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest
from botocore.stub import Stubber
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import ApiError
from app.soundboard import library, rules
from app.soundboard.audio import Mp3Error, parse_mp3
from app.soundboard.library import HypersoundLibrary
from app.storage.memory import InMemoryObjectStore
from app.storage.r2 import R2Config, R2Store, StorageError, check_soundboard_key
from tests.soundboard import mp3_builder as m
from tests.uploads.test_media import GIF, JPEG, MP4, PNG

UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"
KEY = f"soundboards/creator-a/{UUID}.mp3"
FULL = {"r2_account_id": "0123456789abcdef0123456789abcdef", "r2_access_key_id": "TEST-ONLY-key-id",
        "r2_secret_access_key": "TEST-ONLY-secret", "r2_bucket": "hyperchat-dev-media",
        "r2_public_base_url": "https://pub-test.r2.dev"}


# ── MP3 ─────────────────────────────────────────────────────────────────────────

def test_mp3_duration_measured_from_frames_and_mutagen():
    info = parse_mp3(m.mp3(7.0))
    assert info.frames == 268 and info.walked_duration_s == pytest.approx(7.0, abs=0.01)
    assert info.mutagen_duration_s == pytest.approx(7.0, abs=0.05)
    assert info.duration_s == max(info.walked_duration_s, info.mutagen_duration_s)
    assert parse_mp3(m.mp3(2.0, id3=True)).duration_s == pytest.approx(2.0, abs=0.03)


def test_vbr_header_trick_cannot_understate_the_length():
    info = parse_mp3(m.vbr_trick(52))
    assert info.mutagen_duration_s < 7 < info.duration_s  # mutagen alone would accept it
    assert info.duration_s == pytest.approx(52, abs=0.1)


def test_junk_between_frames_cannot_hide_audio():
    data = m.frame(320) + (m.frame(32) + b"\x00") * 300  # a decoder resyncs and plays every frame
    assert parse_mp3(data).duration_s > 7.5


@pytest.mark.parametrize("data", [b"", b"\x00" * 4000, JPEG, PNG, GIF, MP4, b"ID3\x04\x00\x00\x00\x00\x7f\x7f",
                                  b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 200, b"OggS" + b"\x00" * 400,
                                  bytes([0xFF, 0xFD, 0x90, 0x44]) * 500],  # MPEG Layer II
                         ids=["empty", "zeros", "jpeg", "png", "gif", "mp4", "id3-only", "wav", "ogg", "layer2"])
def test_non_mp3_rejected(data):
    with pytest.raises(Mp3Error):
        parse_mp3(data)


# ── rules ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [("  Air horn ", "Air horn"), ("x" * 60, "x" * 60), ("Ünïcødé 🎺", "Ünïcødé 🎺")])
def test_display_names(value, expected):
    assert rules.display_name(value) == expected


@pytest.mark.parametrize("value", ["", "   ", "x" * 61, "line\nbreak", "tab\tname", "nul\x00"])
def test_bad_display_names(value):
    with pytest.raises(ValueError):
        rules.display_name(value)


def test_upload_types():
    assert rules.content_type_ext("audio", "audio/mpeg") == ("mp3", "audio/mpeg")
    assert rules.content_type_ext("audio", "AUDIO/MP3") == ("mp3", "audio/mpeg")
    assert rules.content_type_ext("image", "image/jpg") == ("jpg", "image/jpeg")
    for kind, ct in (("audio", "audio/wav"), ("audio", "audio/webm"), ("audio", "image/png"), ("image", "image/svg+xml"),
                     ("image", "video/mp4"), ("image", "audio/mpeg"), ("image", "")):
        with pytest.raises(ApiError) as exc:
            rules.content_type_ext(kind, ct)
        assert exc.value.status == 400


def test_new_keys_are_random_and_owned():
    keys = {rules.new_key("creator-a", "mp3") for _ in range(50)}
    assert len(keys) == 50 and all(rules.owned_key("creator-a", k, "audio") == k for k in keys)


@pytest.mark.parametrize("key, kind", [
    (f"soundboards/creator-b/{UUID}.mp3", "audio"),          # another creator
    (f"soundboards/creator-a/{UUID}.png", "audio"),          # wrong kind
    (f"soundboards/creator-a/{UUID}.mp3", "image"),
    (f"soundboards/creator-a/../creator-b/{UUID}.mp3", "audio"),
    (f"soundboards/creator-a/{UUID}.mp3/../../x.mp3", "audio"),
    ("soundboards/creator-a/not-a-uuid.mp3", "audio"),
    (f"hypersounds/{UUID}.mp3", "audio"), ("hypersound-thumbs/airhorn.gif", "image"),
    ("voice-messages/creator-a_1800000000000_" + "a" * 32 + ".webm", "audio"),
    ("donation-media/creator-a_1800000000000_" + "a" * 32 + ".png", "image"),
    (f"https://pub-legacy.r2.dev/soundboards/creator-a/{UUID}.mp3", "audio"),
    (f"/soundboards/creator-a/{UUID}.mp3", "audio"), (f"soundboards/creator-a/{UUID.upper()}.mp3", "audio"),
    ("", "audio"), (None, "audio"), (42, "audio"),
])
def test_foreign_or_malformed_keys_rejected(key, kind):
    with pytest.raises(ApiError) as exc:
        rules.owned_key("creator-a", key, kind)
    assert exc.value.status == 422


# ── storage: only soundboard keys can be read or deleted; only the library can be listed ──────

@pytest.mark.parametrize("key", ["hypersounds/airhorn.mp3", "hypersound-thumbs/airhorn.gif",
                                 "voice-messages/a_1800000000000_" + "a" * 32 + ".webm",
                                 "donation-media/a_1800000000000_" + "a" * 32 + ".png", "tts-audio/x.mp3",
                                 "../soundboards/a/x.mp3", f"soundboards/a/{UUID}.svg", ""])
def test_store_refuses_to_read_or_delete_anything_but_soundboard_objects(key):
    store = InMemoryObjectStore()
    for call in (store.delete_object(key), store.head_object(key), store.get_object(key, max_bytes=10)):
        with pytest.raises(ValueError):
            asyncio.run(call)
    with pytest.raises(ValueError):
        check_soundboard_key(key)


def test_store_lists_only_the_library_prefixes():
    store = InMemoryObjectStore()
    for prefix in ("soundboards/", "voice-messages/", "", "hypersounds", "hypersounds/../"):
        with pytest.raises(ValueError):
            asyncio.run(store.list_keys(prefix))


def r2() -> R2Store:
    return R2Store(R2Config.from_settings(Settings(**FULL)))


def test_presigned_put_signs_type_length_and_host_for_300_seconds():
    url = r2().presign_put(KEY, content_type="audio/mpeg", content_length=1234)
    parts = urlsplit(url)
    q = parse_qs(parts.query)
    assert parts.scheme == "https" and parts.path == "/hyperchat-dev-media/" + KEY
    assert q["X-Amz-SignedHeaders"] == ["content-length;content-type;host"] and q["X-Amz-Expires"] == ["300"]
    assert "TEST-ONLY-secret" not in url
    with pytest.raises(ValueError):
        r2().presign_put("hypersounds/x.mp3", content_type="audio/mpeg", content_length=1)


def test_r2_head_get_delete_list_requests():
    store = r2()
    stub = Stubber(store._s3())
    stub.add_response("head_object", {"ContentLength": 5, "ContentType": "audio/mpeg"},
                      {"Bucket": "hyperchat-dev-media", "Key": KEY})
    stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
    stub.add_response("delete_object", {}, {"Bucket": "hyperchat-dev-media", "Key": KEY})
    stub.add_response("list_objects_v2", {"Contents": [{"Key": "hypersounds/a.mp3"}], "IsTruncated": True,
                                          "NextContinuationToken": "t"},
                      {"Bucket": "hyperchat-dev-media", "Prefix": "hypersounds/", "MaxKeys": 1000})
    stub.add_response("list_objects_v2", {"Contents": [{"Key": "hypersounds/b.mp3"}], "IsTruncated": False},
                      {"Bucket": "hyperchat-dev-media", "Prefix": "hypersounds/", "MaxKeys": 1000, "ContinuationToken": "t"})
    stub.add_client_error("delete_object", service_error_code="AccessDenied", http_status_code=403)
    with stub:
        head = asyncio.run(store.head_object(KEY))
        assert (head.size, head.content_type) == (5, "audio/mpeg")
        assert asyncio.run(store.head_object(KEY)) is None  # missing object
        asyncio.run(store.delete_object(KEY))
        assert asyncio.run(store.list_keys("hypersounds/")) == ["hypersounds/a.mp3", "hypersounds/b.mp3"]
        with pytest.raises(StorageError) as exc:
            asyncio.run(store.delete_object(KEY))
        assert str(exc.value) == "r2_AccessDenied"
    stub.assert_no_pending_responses()


def test_in_memory_presigned_put_behaves_like_r2():
    now = [1000.0]
    store = InMemoryObjectStore(clock=lambda: now[0])
    store.presign_put(KEY, content_type="audio/mpeg", content_length=3)
    assert store.client_put(KEY, b"abc", "audio/ogg") == 403      # signed content-type
    assert store.client_put(KEY, b"abcd", "audio/mpeg") == 403    # signed content-length
    now[0] += 301
    assert store.client_put(KEY, b"abc", "audio/mpeg") == 403     # expired after 300 s
    now[0] -= 2
    assert store.client_put(KEY, b"abc", "audio/mpeg") == 200


# ── hypersound library ──────────────────────────────────────────────────────────

def test_library_pairs_sounds_with_preferred_thumbnails():
    store = InMemoryObjectStore()
    for key in ("hypersounds/air-horn.mp3", "hypersounds/Boom_Boom.MP3", "hypersounds/no_thumb.mp3",
                "hypersounds/readme.txt", "hypersound-thumbs/air-horn.png", "hypersound-thumbs/AIR-HORN.gif",
                "hypersound-thumbs/boom_boom.jpeg", "hypersound-thumbs/boom_boom.jpg", "hypersound-thumbs/x.webp",
                "hypersounds/sp ace.mp3"):
        store.seed_library(key)
    sounds = asyncio.run(HypersoundLibrary(store).sounds()).model_dump()["sounds"]
    base = "https://media.example.test/"
    assert sounds == [
        {"id": "Boom_Boom", "display_name": "Boom Boom", "audio_url": base + "hypersounds/Boom_Boom.MP3",
         "thumb_url": base + "hypersound-thumbs/boom_boom.jpg"},                       # jpg beats jpeg
        {"id": "air-horn", "display_name": "Air Horn", "audio_url": base + "hypersounds/air-horn.mp3",
         "thumb_url": base + "hypersound-thumbs/AIR-HORN.gif"},                        # gif beats png
        {"id": "no_thumb", "display_name": "No Thumb", "audio_url": base + "hypersounds/no_thumb.mp3", "thumb_url": None},
        {"id": "sp ace", "display_name": "Sp Ace", "audio_url": base + "hypersounds/sp%20ace.mp3", "thumb_url": None},
    ]


def test_library_caches_and_never_caches_failures():
    store, now = InMemoryObjectStore(), [0.0]
    lib = HypersoundLibrary(store, clock=lambda: now[0])
    store.fail_with = "r2_InternalError"
    with pytest.raises(ApiError) as exc:
        asyncio.run(lib.sounds())
    assert (exc.value.status, exc.value.error) == (502, "Sound library unavailable")
    store.fail_with = None
    store.seed_library("hypersounds/a.mp3")
    assert len(asyncio.run(lib.sounds()).sounds) == 1
    store.seed_library("hypersounds/b.mp3")
    assert len(asyncio.run(lib.sounds()).sounds) == 1          # cached for 60 s
    now[0] += library.CACHE_SECONDS + 1
    assert len(asyncio.run(lib.sounds()).sounds) == 2
    with pytest.raises(ApiError) as exc:
        asyncio.run(HypersoundLibrary(None).sounds())
    assert exc.value.status == 503


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_DB_ENABLED", "false")
    monkeypatch.setenv("HYPERCHAT_APP_ENV", "test")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app(), base_url="https://testserver") as c:
        yield c
    get_settings.cache_clear()


def test_public_hypersound_route(client):
    assert client.get("/api/v1/public/hypersounds").json() == {"error": "Sound library unavailable"}  # no R2
    store = InMemoryObjectStore(public_base="https://pub-dev.r2.dev")
    store.seed_library("hypersounds/drum.mp3")
    store.seed_library("hypersound-thumbs/drum.gif")
    client.app.state.hypersounds = HypersoundLibrary(store)
    r = client.get("/api/v1/public/hypersounds")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=60"
    assert r.json() == {"sounds": [{"id": "drum", "display_name": "Drum", "audio_url": "https://pub-dev.r2.dev/hypersounds/drum.mp3",
                                    "thumb_url": "https://pub-dev.r2.dev/hypersound-thumbs/drum.gif"}]}
    assert "set-cookie" not in r.headers
    assert client.post("/api/v1/public/hypersounds").status_code == 405
    assert store.deleted == []
