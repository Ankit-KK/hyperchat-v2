"""Phase 10 units (no database): which URL a claimed donation plays (allowlist, hidden messages), token format and
OBS URLs, the "!vaani" rule, the audio-now-playing / vaani-celebration payloads, the publisher's 30 s limit for
now-playing events, and the player page's headers."""

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.obs.media import choose
from app.obs.tokens import new_token, obs_urls
from app.payments.rules import VAANI_CREATORS, is_vaani_celebration
from app.realtime import outbox
from app.realtime.outbox import Event, OutboxQueue
from app.realtime.payloads import Renderer, Skip, now_playing
from app.security.obs_token import is_obs_origin_allowed, own_origin

BASE = "https://media.example.test"
SID, DID, OTHER = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
SLUG = "ankit"
VOICE = f"{BASE}/voice-messages/{SLUG}_1800000000000_{'a' * 32}.webm"
TTS = f"{BASE}/tts-audio/{SID}/{DID}.mp3"
SILENCE = f"{BASE}/system/silence.mp3"


def row(**kw):
    d = {"id": DID, "streamer_id": SID, "hypersound_url": None, "voice_message_url": None, "tts_audio_url": None,
         "message_visible": True}
    d.update(kw)
    return d


# ── media allowlist (P10-D5) and hidden messages (P10-D6) ───────────────────

@pytest.mark.parametrize("cols, expected", [
    ({"hypersound_url": f"{BASE}/hypersounds/Door_Knock.mp3"}, ("hypersound", f"{BASE}/hypersounds/Door_Knock.mp3")),
    ({"hypersound_url": f"{BASE}/hypersounds/Air%20Horn.mp3"}, ("hypersound", f"{BASE}/hypersounds/Air%20Horn.mp3")),
    ({"voice_message_url": VOICE}, ("voice", VOICE)),
    ({"tts_audio_url": TTS}, ("tts", TTS)),
    ({"tts_audio_url": SILENCE}, ("silence", SILENCE)),
    ({"hypersound_url": f"{BASE}/hypersounds/x.mp3", "voice_message_url": VOICE, "tts_audio_url": TTS},
     ("hypersound", f"{BASE}/hypersounds/x.mp3")),                         # legacy order: hypersound first
    ({"voice_message_url": VOICE, "tts_audio_url": TTS}, ("voice", VOICE)),  # then voice
])
def test_servable(cols, expected):
    assert choose(row(**cols), BASE, SLUG) == expected


@pytest.mark.parametrize("cols", [
    {"hypersound_url": "https://pub-fff13c27bb0d4a1e807dfc596462b7d5.r2.dev/hypersounds/x.mp3"},  # legacy bucket
    {"tts_audio_url": "https://vsevsjvtrshgeiudrnth.supabase.co/storage/v1/object/public/x.mp3"},  # Supabase
    {"tts_audio_url": f"https://media.example.test.evil.com/tts-audio/{SID}/{DID}.mp3"},
    {"tts_audio_url": f"http://media.example.test/tts-audio/{SID}/{DID}.mp3"},
    {"tts_audio_url": f"{BASE}/tts-audio/{OTHER}/{DID}.mp3"},                # another creator's folder
    {"tts_audio_url": f"{BASE}/tts-audio/{SID}/{OTHER}.mp3"},                # another donation's audio
    {"tts_audio_url": f"{TTS}?x=1"}, {"tts_audio_url": f"{TTS}#f"},
    {"tts_audio_url": f"{BASE}/tts-audio/{SID}/../{SID}/{DID}.mp3"},
    {"tts_audio_url": f"{BASE}/system/silence.mp3.exe"},
    {"voice_message_url": f"{BASE}/voice-messages/other_1800000000000_{'a' * 32}.webm"},  # another creator's voice
    {"voice_message_url": f"{BASE}/voice-messages/{SLUG}_1800000000000_{'a' * 32}.mp3"},
    {"hypersound_url": f"{BASE}/hypersounds/../voice-messages/x.mp3"},
    {"hypersound_url": f"{BASE}/hypersounds/a/b.mp3"}, {"hypersound_url": f"{BASE}/hypersounds/a%2Fb.mp3"},
    {"hypersound_url": f"{BASE}/hypersounds/%2e%2e.mp3"}, {"hypersound_url": f"{BASE}/hypersounds/a%5cb.mp3"},
    {"hypersound_url": f"{BASE}/hypersounds/x.wav"}, {"hypersound_url": f"{BASE}\\hypersounds\\x.mp3"},
    {"hypersound_url": "javascript:alert(1)"}, {},
])
def test_never_served(cols):
    assert choose(row(**cols), BASE, SLUG) is None


def test_no_bucket_means_nothing_is_served():
    assert choose(row(tts_audio_url=TTS), None, SLUG) is None


def test_hidden_message_is_never_spoken():
    assert choose(row(tts_audio_url=TTS, message_visible=False), BASE, SLUG) == ("silence", SILENCE)
    assert choose(row(voice_message_url=VOICE, message_visible=False), BASE, SLUG) == ("voice", VOICE)  # donor's own
    assert choose(row(tts_audio_url=SILENCE, message_visible=False), BASE, SLUG) == ("silence", SILENCE)


# ── tokens and URLs (P10-D1) ────────────────────────────────────────────────

def test_token_format_and_urls():
    tokens = {new_token() for _ in range(50)}
    assert len(tokens) == 50 and all(re.fullmatch(r"obs_[0-9a-f]{64}", t) for t in tokens)
    t = new_token()
    urls = obs_urls("https://api.example.test/", t)
    assert urls["player_url"] == f"https://api.example.test/obs/player#token={t}"  # fragment: never sent or logged
    assert urls["media_source_url"] == f"https://api.example.test/api/v1/obs/audio/next?token={t}"


@pytest.mark.parametrize("origin, host, ok", [
    (None, "api.example.test", True), ("https://api.example.test", "api.example.test", True),
    ("https://evil.example", "api.example.test", False), ("http://api.example.test", "api.example.test", False),
    ("https://api.example.test", None, False), ("https://hyperchat.space", "x", True),
])
def test_obs_origin(origin, host, ok):
    assert is_obs_origin_allowed(origin, frozenset({"https://hyperchat.space"}), own_origin(host)) is ok


def test_own_origin_rejects_odd_hosts():
    assert own_origin("evil.com/x") is None and own_origin("a@b") is None and own_origin("") is None


# ── "!vaani" (P10-D13) ──────────────────────────────────────────────────────

@pytest.mark.parametrize("slug, message, voice, hyper, media, expected", [
    ("ankit", "!vaani", False, False, False, True),
    ("captain_vaani", "  !VAANI \n", False, False, False, True),
    ("ankit", "!vaani hi", False, False, False, False),
    ("ankit", "vaani", False, False, False, False),
    ("ankit", "!vaani", True, False, False, False),
    ("ankit", "!vaani", False, True, False, False),
    ("ankit", "!vaani", False, False, True, False),
    ("someone", "!vaani", False, False, False, False),
    ("ankit", None, False, False, False, False),
])
def test_vaani_rule(slug, message, voice, hyper, media, expected):
    assert is_vaani_celebration(slug=slug, message=message, has_voice=voice, has_hypersound=hyper, has_media=media,
                                creators=VAANI_CREATORS) is expected


def test_vaani_creators_setting():
    assert Settings(_env_file=None).vaani_slugs == {"ankit", "captain_vaani"}
    assert Settings(_env_file=None, vaani_creators=" A ,b,, ").vaani_slugs == {"a", "b"}
    assert Settings(_env_file=None, vaani_creators="").vaani_slugs == frozenset()


# ── payloads ────────────────────────────────────────────────────────────────

def donation(**kw):
    d = dict(id=DID, name="Ravi", amount=Decimal("100"), currency="INR", message="secret words", message_visible=True,
             moderation_status="approved", payment_status="success", is_hyperemote=False, voice_message_url=None,
             tts_audio_url=TTS, hypersound_url=None, media_url=None, media_type=None,
             created_at=datetime(2026, 1, 1, tzinfo=UTC))
    d.update(kw)
    return d


DISPLAY_KEYS = {"id", "name", "amount", "currency", "message", "is_hyperemote", "kind", "audio_url",
                "voice_message_url", "hypersound_url", "media_url", "media_type", "created_at"}


def test_now_playing_display():
    shown = now_playing(donation(), "tts", TTS)
    assert set(shown) == DISPLAY_KEYS and shown["message"] == "secret words" and shown["amount"] == 100.0
    assert now_playing(donation(message_visible=False), "silence", SILENCE)["message"] is None


class Repo:
    def __init__(self, d=None, streamer=True):
        self.d, self.s = d, streamer

    async def streamer(self, sid):
        return {"slug": "ankit", "status": "active", "pusher_group": 1} if self.s else None

    async def donation(self, did, sid):
        return self.d


def event(name, facts=None, subject=DID):
    return Event(1, str(uuid.uuid4()), SID, outbox.EVENT_CHANNELS[name], name, subject, facts or {}, 1, 6,
                 datetime.now(UTC), "w", False)


def render(e, d):
    return asyncio.run(Renderer(Repo(d), silence_url=SILENCE).render(e))


def test_audio_now_playing_payload():
    r = render(event("audio-now-playing", {"kind": "tts"}), donation())
    assert r.channel == "private-ankit-alerts" and set(r.data) == {"event_id", "v", "donation"}
    assert set(r.data["donation"]) == DISPLAY_KEYS and r.data["donation"]["audio_url"] == TTS
    silent = render(event("audio-now-playing", {"kind": "silence"}), donation(message_visible=False))
    assert silent.data["donation"]["audio_url"] == SILENCE and silent.data["donation"]["message"] is None
    assert render(event("audio-now-playing", {"kind": "tts"}), donation(moderation_status="rejected")) == \
        Skip("not_approved")
    assert render(event("audio-now-playing", {}), donation()) == Skip("subject_missing")
    # No creator/user id field. Generated TTS keys are tts-audio/<creator id>/<donation id>.mp3 (Phase 8), so the
    # creator id appears only INSIDE the audio URLs, which the player must fetch (private channel only).
    assert "streamer_id" not in json.dumps(r.data)
    without_urls = {k: v for k, v in r.data["donation"].items() if k not in ("audio_url", "tts_audio_url")}
    assert SID not in json.dumps(without_urls)


def test_vaani_payload():
    r = render(event("vaani-celebration"), donation(message="!vaani", tts_audio_url=None))
    assert r.channel == "private-ankit-alerts"
    assert set(r.data) == {"event_id", "v", "donation_id", "name", "amount", "currency", "created_at"}
    assert SID not in json.dumps(r.data) and "streamer_id" not in r.data


def test_now_playing_expires_after_30_seconds():
    calls = []

    class Fake:  # connection, transaction and cursor in one
        rowcount = 0

        def transaction(self):
            return self

        def cursor(self, **kw):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, sql, params):
            calls.append((sql, params))

    asyncio.run(OutboxQueue(Fake).expire())
    ((sql, params),) = calls
    assert "CASE event WHEN 'audio-now-playing' THEN %s ELSE %s END" in sql and params == (30, 600)
    assert outbox.EVENT_CHANNELS["audio-now-playing"] == outbox.EVENT_CHANNELS["vaani-celebration"] == "alerts"


# ── the player page ─────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_DB_ENABLED", "false")
    monkeypatch.setenv("HYPERCHAT_R2_PUBLIC_BASE_URL", BASE)
    get_settings.cache_clear()
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def test_player_page_headers_and_assets(client):
    r = client.get("/obs/player")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    csp = r.headers["content-security-policy"]
    for part in ("default-src 'none'", "script-src 'self'", "connect-src 'self' wss://*.pusher.com",
                 f"media-src {BASE}/", "frame-ancestors 'none'", "form-action 'none'"):
        assert part in csp
    assert "<script src=\"/obs/player.js\"></script>" in r.text and "<script>" not in r.text  # no inline script
    js = client.get("/obs/player.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    assert "history.replaceState" in js.text and 'credentials: "omit"' in js.text and "location.reload" not in js.text
    assert client.get("/obs/player.css").status_code == 200


def test_obs_endpoints_need_the_database(client):
    assert client.post("/api/v1/obs/audio/claim", headers={"x-obs-token": "obs_" + "0" * 64}).status_code == 503
