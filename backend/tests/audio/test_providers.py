"""Provider clients against httpx.MockTransport: request shapes, response validation and the failure
classification (Retryable vs Permanent). No real provider is ever contacted; keys are TEST-ONLY literals."""

import asyncio
import json

import httpx
import pytest

from app.audio.providers import (
    MAX_AUDIO_BYTES,
    ElevenLabsProvider,
    MiniMaxProvider,
    SarvamProvider,
    raise_for_status,
    validate_audio,
)
from app.audio.silence import silent_mp3
from app.jobs.retry import Permanent, Retryable

KEY = "TEST-ONLY-provider-key"
MP3 = silent_mp3()


class Rec:
    def __init__(self, responder):
        self.responder, self.requests = responder, []

    def __call__(self, request):
        self.requests.append(request)
        return self.responder(request)


def run(coro):
    return asyncio.run(coro)


def error_of(coro):
    with pytest.raises((Retryable, Permanent)) as exc:
        run(coro)
    assert KEY not in str(exc.value) and KEY not in repr(exc.value)
    return type(exc.value).__name__, exc.value.reason


def minimax(responder):
    rec = Rec(responder)
    return MiniMaxProvider(KEY, transport=httpx.MockTransport(rec)), rec


def eleven(responder):
    rec = Rec(responder)
    return ElevenLabsProvider(KEY, transport=httpx.MockTransport(rec)), rec


def mm_ok(audio=MP3):
    return httpx.Response(200, json={"base_resp": {"status_code": 0}, "data": {"status": 2, "audio": audio.hex()}})


# ── MiniMax ─────────────────────────────────────────────────────────────────

def test_minimax_request_and_hex_audio():
    p, rec = minimax(lambda r: mm_ok())
    assert run(p.speech("[excited] Ravi sent 40 rupees!", "moss_audio_x", "English")) == MP3
    (req,) = rec.requests
    body = json.loads(req.content)
    assert str(req.url) == "https://api-uw.minimax.io/v1/t2a_v2" and req.headers["authorization"] == f"Bearer {KEY}"
    assert body["model"] == "speech-2.8-hd" and body["text"] == "Ravi sent 40 rupees!"  # tags stripped for MiniMax
    assert body["voice_setting"] == {"voice_id": "moss_audio_x", "speed": 1.1, "vol": 5, "pitch": 0}
    assert body["language_boost"] == "English" and body["output_format"] == "hex"
    run(p.speech("hi", "moss_audio_x", None))
    assert json.loads(rec.requests[-1].content)["language_boost"] == "Hindi"


@pytest.mark.parametrize("code, expected", [(1002, ("Retryable", "provider_rate_limited")),
                                            (1039, ("Retryable", "provider_rate_limited")),
                                            (1000, ("Retryable", "provider_unavailable")),
                                            (1001, ("Retryable", "provider_unavailable")),
                                            (1004, ("Permanent", "provider_auth")),
                                            (1008, ("Permanent", "provider_quota")),
                                            (2013, ("Permanent", "provider_rejected"))])
def test_minimax_base_resp_codes(code, expected):
    p, _ = minimax(lambda r: httpx.Response(200, json={"base_resp": {"status_code": code, "status_msg": KEY}}))
    assert error_of(p.speech("hi", "moss_audio_x", None)) == expected


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"base_resp": {}}',
                                  json.dumps({"base_resp": {"status_code": 0}, "data": {"status": 1}}).encode(),
                                  json.dumps({"base_resp": {"status_code": 0}, "data": {"status": 2, "audio": "zz"}}).encode()])
def test_minimax_malformed_responses_are_retryable(body):
    p, _ = minimax(lambda r: httpx.Response(200, content=body))
    assert error_of(p.speech("hi", "moss_audio_x", None)) == ("Retryable", "bad_response")


@pytest.mark.parametrize("status, headers, expected", [
    (429, {"retry-after": "7"}, ("Retryable", "provider_rate_limited")), (500, {}, ("Retryable", "provider_unavailable")),
    (503, {}, ("Retryable", "provider_unavailable")), (401, {}, ("Permanent", "provider_auth")),
    (403, {}, ("Permanent", "provider_auth")), (402, {}, ("Permanent", "provider_quota")),
    (400, {}, ("Permanent", "provider_rejected")), (422, {}, ("Permanent", "provider_rejected"))])
def test_http_status_classification(status, headers, expected):
    p, _ = minimax(lambda r: httpx.Response(status, headers=headers, text=f"error {KEY}"))
    assert error_of(p.speech("hi", "moss_audio_x", None)) == expected


def test_retry_after_is_honoured_and_capped():
    with pytest.raises(Retryable) as exc:
        raise_for_status(httpx.Response(429, headers={"retry-after": "7"}))
    assert exc.value.retry_after == 7
    with pytest.raises(Retryable) as exc:
        raise_for_status(httpx.Response(429, headers={"retry-after": "9999"}))
    assert exc.value.retry_after == 60
    with pytest.raises(Retryable) as exc:
        raise_for_status(httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}))
    assert exc.value.retry_after is None


@pytest.mark.parametrize("error, reason", [(httpx.ConnectTimeout("t"), "provider_timeout"),
                                           (httpx.ReadTimeout("t"), "provider_timeout"),
                                           (httpx.ConnectError("c"), "provider_network")])
def test_timeouts_and_network_errors_are_retryable(error, reason):
    def boom(request):
        raise error
    p, _ = minimax(boom)
    assert error_of(p.speech("hi", "moss_audio_x", None)) == ("Retryable", reason)


@pytest.mark.parametrize("voice", ["", "../../v1/x", "a/b", "x" * 200, None])
def test_bad_voice_ids_never_reach_the_network(voice):
    p, rec = minimax(lambda r: mm_ok())
    assert error_of(p.speech("hi", voice, None)) == ("Permanent", "invalid_voice")
    e, erec = eleven(lambda r: httpx.Response(200, content=MP3))
    assert error_of(e.speech("hi", voice, None)) == ("Permanent", "invalid_voice")
    assert rec.requests == [] and erec.requests == []


# ── ElevenLabs ──────────────────────────────────────────────────────────────

def test_elevenlabs_v3_first():
    e, rec = eleven(lambda r: httpx.Response(200, content=MP3))
    assert run(e.speech("[excited] hi", "voiceABC", None)) == MP3
    (req,) = rec.requests
    body = json.loads(req.content)
    assert str(req.url) == "https://api.elevenlabs.io/v1/text-to-speech/voiceABC?output_format=mp3_44100_128"
    assert req.headers["xi-api-key"] == KEY and body["model_id"] == "eleven_v3" and body["text"] == "[excited] hi"


def test_elevenlabs_falls_back_to_v2_without_tags():
    def respond(r):
        return httpx.Response(500) if json.loads(r.content)["model_id"] == "eleven_v3" else httpx.Response(200, content=MP3)
    e, rec = eleven(respond)
    assert run(e.speech("[excited] hi there", "voiceABC", None)) == MP3
    assert [json.loads(r.content)["model_id"] for r in rec.requests] == ["eleven_v3", "eleven_multilingual_v2"]
    assert json.loads(rec.requests[1].content)["text"] == "hi there"


def test_elevenlabs_both_models_failing_reports_the_fallback_error():
    e, rec = eleven(lambda r: httpx.Response(429 if json.loads(r.content)["model_id"] == "eleven_v3" else 401))
    assert error_of(e.speech("hi", "voiceABC", None)) == ("Permanent", "provider_auth")
    assert len(rec.requests) == 2


def test_sound_effect_and_music_requests():
    e, rec = eleven(lambda r: httpx.Response(200, content=MP3))
    run(e.sound_effect("door creak"))
    run(e.music("lofi beat"))
    sfx, music = rec.requests
    assert sfx.url.path == "/v1/sound-generation" and json.loads(sfx.content) == {
        "text": "door creak", "duration_seconds": 10, "prompt_influence": 0.3, "loop": False}
    assert music.url.path == "/v1/music" and json.loads(music.content) == {
        "prompt": "lofi beat", "music_length_ms": 10000, "model_id": "music_v2_5"}


@pytest.mark.parametrize("status, expected", [(400, ("Permanent", "bad_prompt")), (422, ("Permanent", "bad_prompt")),
                                              (402, ("Permanent", "provider_quota")),
                                              (429, ("Retryable", "provider_rate_limited"))])
def test_command_errors(status, expected):
    e, _ = eleven(lambda r: httpx.Response(status))
    assert error_of(e.sound_effect("x")) == expected
    assert error_of(e.music("x")) == expected


# ── Sarvam ──────────────────────────────────────────────────────────────────

def test_sarvam_multipart_request_and_transcript():
    rec = Rec(lambda r: httpx.Response(200, json={"transcript": "  namaste bhai  "}))
    s = SarvamProvider(KEY, transport=httpx.MockTransport(rec))
    assert run(s.transcribe(b"webm-bytes")) == "namaste bhai"
    (req,) = rec.requests
    body = req.content
    assert str(req.url) == "https://api.sarvam.ai/speech-to-text-translate" and req.headers["api-subscription-key"] == KEY
    assert b'name="model"' in body and b"saaras:v3" in body and b'name="mode"' in body and b"transcribe" in body
    assert b'filename="audio.webm"' in body and b"webm-bytes" in body


@pytest.mark.parametrize("payload, expected", [({"text": "a"}, "a"), ({"result": "b"}, "b"), ({"transcript": "x" * 6000}, "x" * 5000)])
def test_sarvam_response_fields(payload, expected):
    s = SarvamProvider(KEY, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    assert run(s.transcribe(b"v")) == expected


@pytest.mark.parametrize("response", [httpx.Response(200, json={"nope": 1}), httpx.Response(200, content=b"<html>"),
                                      httpx.Response(200, json=["x"])])
def test_sarvam_malformed(response):
    s = SarvamProvider(KEY, transport=httpx.MockTransport(lambda r: response))
    assert error_of(s.transcribe(b"v")) == ("Retryable", "bad_response")


def test_providers_never_expose_keys_and_require_one():
    for cls in (MiniMaxProvider, ElevenLabsProvider, SarvamProvider):
        assert KEY not in repr(cls(KEY))
        with pytest.raises(ValueError):
            cls("")


# ── audio validation ────────────────────────────────────────────────────────

def test_validate_audio():
    assert 0.9 < validate_audio(MP3) < 1.1
    assert pytest.raises(Retryable, validate_audio, b"").value.reason == "empty_audio"
    assert pytest.raises(Retryable, validate_audio, b"<html>error</html>" * 10).value.reason == "invalid_audio"
    assert pytest.raises(Permanent, validate_audio, b"\x00" * (MAX_AUDIO_BYTES + 1)).value.reason == "audio_too_large"
    assert pytest.raises(Permanent, validate_audio, silent_mp3(frames=2400)).value.reason == "audio_too_long"  # ~63 s
