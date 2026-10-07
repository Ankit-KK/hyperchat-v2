"""Paid audio providers over httpx (server-side only; keys never logged, returned or put in an exception).

  SpeechProvider        MiniMax t2a_v2 (speech-2.8-hd), ElevenLabs TTS (eleven_v3 -> eleven_multilingual_v2)
  SoundEffectProvider   ElevenLabs sound-generation (10 s)
  MusicProvider         ElevenLabs music (music_v2_5, 10 s)
  TranscriptionProvider Sarvam speech-to-text-translate (saaras:v3, mode transcribe)

Failures raise the job errors with a fixed reason code (never a provider body):
  Retryable  timeout, network, HTTP 429 (Retry-After honoured), 5xx, malformed response
  Permanent  401/403 (credentials), 402 (quota), other 4xx (rejected / bad prompt), invalid audio limits
Generated audio must be a non-empty MP3 of at most 5 MB and 60 s (Phase 6 MP3 parser).
"""

import re
from typing import Protocol

import httpx

from app.audio.text import strip_audio_tags
from app.jobs.retry import Permanent, Retryable
from app.soundboard.audio import Mp3Error, parse_mp3

MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_AUDIO_SECONDS = 60.0
MAX_TRANSCRIPT_CHARS = 5000
RETRY_AFTER_CAP = 60.0
_EL_VOICE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MINIMAX_VOICE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_HEX = re.compile(r"^(?:[0-9a-fA-F]{2})+$")


class SpeechProvider(Protocol):
    name: str

    async def speech(self, text: str, voice_id: str, language_boost: str | None) -> bytes: ...


class SoundEffectProvider(Protocol):
    async def sound_effect(self, prompt: str) -> bytes: ...


class MusicProvider(Protocol):
    async def music(self, prompt: str) -> bytes: ...


class TranscriptionProvider(Protocol):
    model: str

    async def transcribe(self, audio: bytes) -> str: ...


def validate_audio(data: bytes) -> float:
    """Seconds of a provider's MP3, or a job error. Non-MP3 bytes are a malformed (retryable) response."""
    if not data:
        raise Retryable("empty_audio")
    if len(data) > MAX_AUDIO_BYTES:
        raise Permanent("audio_too_large")
    try:
        seconds = parse_mp3(data).duration_s
    except Mp3Error:
        raise Retryable("invalid_audio") from None
    if seconds > MAX_AUDIO_SECONDS:
        raise Permanent("audio_too_long")
    return seconds


def _retry_after(response: httpx.Response) -> float | None:
    value = (response.headers.get("retry-after") or "").strip()
    if value.isdigit():
        return min(float(value), RETRY_AFTER_CAP)
    return None


def raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    if 200 <= status < 300:
        return
    if status == 429:
        raise Retryable("provider_rate_limited", _retry_after(response))
    if status >= 500:
        raise Retryable("provider_unavailable")
    if status in (401, 403):
        raise Permanent("provider_auth")
    if status == 402:
        raise Permanent("provider_quota")
    raise Permanent("provider_rejected")


class _Http:
    def __init__(self, api_key: str, base_url: str, timeout: float, transport: httpx.AsyncBaseTransport | None):
        if not api_key:
            raise ValueError("provider key missing")
        self._key, self._base, self._timeout, self._transport = api_key, base_url, timeout, transport

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    async def _post(self, path: str, *, headers: dict, timeout: float | None = None, **kwargs) -> httpx.Response:
        try:
            async with httpx.AsyncClient(base_url=self._base, transport=self._transport,
                                         timeout=timeout or self._timeout, follow_redirects=False) as client:
                response = await client.post(path, headers=headers, **kwargs)
        except httpx.TimeoutException:
            raise Retryable("provider_timeout") from None
        except httpx.HTTPError:
            raise Retryable("provider_network") from None
        raise_for_status(response)
        return response


class MiniMaxProvider(_Http):
    """MiniMax t2a_v2. Errors arrive as HTTP 200 with base_resp.status_code != 0."""
    name = "minimax"
    MODEL = "speech-2.8-hd"
    _CODES = {1000: Retryable, 1001: Retryable, 1002: Retryable, 1039: Retryable,  # unknown/timeout/rate limits
              1004: Permanent, 1008: Permanent}  # auth / insufficient balance; anything else: rejected
    _REASONS = {1002: "provider_rate_limited", 1039: "provider_rate_limited", 1004: "provider_auth",
                1008: "provider_quota"}

    def __init__(self, api_key: str, *, transport=None, base_url: str = "https://api-uw.minimax.io",
                 timeout: float = 45.0) -> None:
        super().__init__(api_key, base_url, timeout, transport)

    async def speech(self, text: str, voice_id: str, language_boost: str | None) -> bytes:
        if not _MINIMAX_VOICE.match(voice_id or ""):
            raise Permanent("invalid_voice")
        response = await self._post("/v1/t2a_v2", headers={"Authorization": f"Bearer {self._key}"}, json={
            "model": self.MODEL, "text": strip_audio_tags(text), "stream": False,
            "language_boost": language_boost or "Hindi", "output_format": "hex",
            "voice_setting": {"voice_id": voice_id, "speed": 1.1, "vol": 5, "pitch": 0},
            "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1},
        })
        try:
            body = response.json()
        except ValueError:
            raise Retryable("bad_response") from None
        base = body.get("base_resp") if isinstance(body, dict) else None
        code = base.get("status_code") if isinstance(base, dict) else None
        if not isinstance(code, int) or isinstance(code, bool):
            raise Retryable("bad_response")
        if code != 0:
            error = self._CODES.get(code, Permanent)
            raise error(self._REASONS.get(code, "provider_unavailable" if error is Retryable else "provider_rejected"))
        data = body.get("data")
        if not isinstance(data, dict) or data.get("status") != 2 or not isinstance(data.get("audio"), str) \
                or not _HEX.match(data["audio"]):
            raise Retryable("bad_response")
        return bytes.fromhex(data["audio"])


class ElevenLabsProvider(_Http):
    name = "elevenlabs"
    MUSIC_MODEL, MUSIC_LENGTH_MS = "music_v2_5", 10_000
    SFX_SECONDS, SFX_PROMPT_INFLUENCE = 10, 0.3

    def __init__(self, api_key: str, *, transport=None, base_url: str = "https://api.elevenlabs.io",
                 timeout: float = 45.0, music_timeout: float = 90.0, sfx_timeout: float = 60.0) -> None:
        super().__init__(api_key, base_url, timeout, transport)
        self._music_timeout, self._sfx_timeout = music_timeout, sfx_timeout

    def _headers(self) -> dict:
        return {"xi-api-key": self._key, "Accept": "audio/mpeg"}

    async def _tts(self, text: str, voice_id: str, model: str) -> bytes:
        settings = ({"stability": 0.5, "similarity_boost": 0.75, "use_speaker_boost": True} if model == "eleven_v3"
                    else {"stability": 0.5, "similarity_boost": 0.75, "style": 0.3, "use_speaker_boost": True})
        response = await self._post(f"/v1/text-to-speech/{voice_id}", headers=self._headers(),
                                    params={"output_format": "mp3_44100_128"},
                                    json={"text": text, "model_id": model, "voice_settings": settings})
        return response.content

    async def speech(self, text: str, voice_id: str, language_boost: str | None) -> bytes:
        """eleven_v3 performs the hype line's audio tags; on any failure, eleven_multilingual_v2 speaks the
        line without them (legacy), and its error is the one reported."""
        if not _EL_VOICE.match(voice_id or ""):
            raise Permanent("invalid_voice")
        try:
            return await self._tts(text, voice_id, "eleven_v3")
        except (Retryable, Permanent):
            return await self._tts(strip_audio_tags(text), voice_id, "eleven_multilingual_v2")

    async def sound_effect(self, prompt: str) -> bytes:
        try:
            response = await self._post("/v1/sound-generation", headers=self._headers(),
                                        params={"output_format": "mp3_44100_128"}, timeout=self._sfx_timeout,
                                        json={"text": prompt, "duration_seconds": self.SFX_SECONDS,
                                              "prompt_influence": self.SFX_PROMPT_INFLUENCE, "loop": False})
        except Permanent as exc:
            raise Permanent("bad_prompt" if exc.reason == "provider_rejected" else exc.reason) from None
        return response.content

    async def music(self, prompt: str) -> bytes:
        try:
            response = await self._post("/v1/music", headers=self._headers(), timeout=self._music_timeout,
                                        params={"output_format": "mp3_44100_128"},
                                        json={"prompt": prompt, "music_length_ms": self.MUSIC_LENGTH_MS,
                                              "model_id": self.MUSIC_MODEL})
        except Permanent as exc:
            raise Permanent("bad_prompt" if exc.reason == "provider_rejected" else exc.reason) from None
        return response.content


class SarvamProvider(_Http):
    name = "sarvam"
    model = "saaras:v3"

    def __init__(self, api_key: str, *, transport=None, base_url: str = "https://api.sarvam.ai",
                 timeout: float = 60.0) -> None:
        super().__init__(api_key, base_url, timeout, transport)

    async def transcribe(self, audio: bytes) -> str:
        response = await self._post("/speech-to-text-translate", headers={"api-subscription-key": self._key},
                                    files={"file": ("audio.webm", audio, "audio/webm")},
                                    data={"model": self.model, "mode": "transcribe"})
        try:
            body = response.json()
        except ValueError:
            raise Retryable("bad_response") from None
        if not isinstance(body, dict):
            raise Retryable("bad_response")
        transcript = next((body[k] for k in ("transcript", "text", "result") if isinstance(body.get(k), str)), None)
        if transcript is None:
            raise Retryable("bad_response")
        return transcript.strip()[:MAX_TRANSCRIPT_CHARS]
