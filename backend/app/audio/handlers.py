"""Job handlers: donation_audio and voice_transcription.

donation_audio (enqueued in the transaction that approves a paid donation). The donation is re-read when the
job runs; a donation that is gone, unpaid, no longer approved or already has audio finishes as "skipped".
Otherwise, in legacy order:
  1. hypersound            nothing to generate (the library sound plays)
  2. voice message         nothing to generate; the URL must be this creator's Phase 6 upload in this
                           environment's bucket and the object must exist
  3. "!sfx <prompt>"       ElevenLabs sound effect   (max 10 per donor per hour, one in flight)
  4. "!testmusic <prompt>" ElevenLabs music          (max 5 per donor per hour, one in flight)
  5. media                 TTS announcement ("... And they sent a GIF.") when the creator's TTS is on
  6. text                  TTS when TTS is on and amount_inr >= max(Rs 40, the creator's TTS minimum)
  7. anything else         the silent fallback
Generated audio goes to tts-audio/<creator id>/<donation id>.mp3 (deterministic). Before any paid call the
handler looks for that object: a previous attempt that generated and stored it but never reached the database
is reused, never generated again. Every paid call is preceded by the moderation guard (fail closed). A
permanent failure, or the last attempt failing, writes the silence URL instead (fallback): the alert is never lost.

voice_transcription (requested from the dashboard). Reads only the donation's own voice object (validated
key, size-capped read), sends it to Sarvam and stores the transcript once.
"""

import logging
import random
import re
from dataclasses import dataclass
from decimal import Decimal

from app.audio import text as tts_text
from app.audio.moderation import Decision, moderate_for_tts
from app.audio.providers import MusicProvider, SoundEffectProvider, SpeechProvider, TranscriptionProvider, validate_audio
from app.audio.repository import PROBE_LIMIT, AudioRepository, supporter_hash
from app.audio.silence import ensure_silence
from app.core.errors import ApiError
from app.jobs.queue import Job
from app.jobs.retry import Backoff, Done, Effects, Permanent, Retryable
from app.payments import rules as payment_rules
from app.storage.r2 import SILENCE_KEY, ObjectStore, StorageError
from app.uploads.media import VOICE_MAX_BYTES

log = logging.getLogger("hyperchat.audio")

PLATFORM_TTS_FLOOR_INR = Decimal("40")
COMMAND_MAX_PROMPT_CHARS = 500
COMMANDS = {"sfx": ("!sfx", 10), "music": ("!testmusic", 5)}  # kind -> (command, max per donor per hour)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def parse_command(message: str | None, command: str) -> str | None:
    """'' when the message is the command without a usable prompt, the prompt when it is, None otherwise."""
    if not isinstance(message, str):
        return None
    trimmed = tts_text.decode_html_entities(message).strip()
    if not re.match(rf"^{re.escape(command)}(\s|$)", trimmed, re.IGNORECASE):
        return None
    prompt = trimmed[len(command):].strip()
    return prompt if prompt and len(prompt) <= COMMAND_MAX_PROMPT_CHARS else ""


def route_speech(tts_voice_id: str | None, el_voice_id: str | None, emoji_spam: bool,
                 default_voice: str) -> tuple[str, str]:
    """Legacy routing. MiniMax voices (moss_audio_*) stay on MiniMax, except emoji-spam messages when the
    creator also has an ElevenLabs voice. Any other voice uses ElevenLabs when el_voice_id is set; otherwise
    MiniMax with the configured default voice (D4: never an id MiniMax would reject)."""
    is_moss = (tts_voice_id or "").startswith("moss_audio")
    use_el = (emoji_spam and bool(el_voice_id)) if is_moss else bool(el_voice_id)
    if use_el:
        return "elevenlabs", el_voice_id
    return "minimax", tts_voice_id if is_moss else default_voice


@dataclass(frozen=True)
class Plan:
    kind: str                  # sfx | music | speech | silent
    reason: str = ""           # why silent
    prompt: str | None = None  # sfx/music
    is_media: bool = False


def plan_audio(d: dict) -> Plan:
    for kind in ("sfx", "music"):  # legacy order: !sfx, then !testmusic, then TTS
        prompt = parse_command(d["message"], COMMANDS[kind][0])
        if prompt is not None:
            return Plan(kind, prompt=prompt) if prompt else Plan("silent", reason="invalid_command")
    if d["tts_enabled"] is False:
        return Plan("silent", reason="tts_disabled")
    if d["media_url"]:
        return Plan("speech", is_media=True)
    minimum = max(PLATFORM_TTS_FLOOR_INR, Decimal(str(d["min_tts_amount_inr"] or PLATFORM_TTS_FLOOR_INR)))
    if Decimal(str(d["amount_inr"] or 0)) < minimum:
        return Plan("silent", reason="below_tts_minimum")
    return Plan("speech")


def audio_key(streamer_id: str, donation_id: str) -> str:
    if not (_UUID.match(streamer_id or "") and _UUID.match(donation_id or "")):
        raise Permanent("invalid_ids")
    return f"tts-audio/{streamer_id}/{donation_id}.mp3"


def _donation_id(job: Job) -> str:
    did = job.payload.get("donation_id") if isinstance(job.payload, dict) else None
    if not isinstance(did, str) or not _UUID.match(did) or did != job.dedupe_key:
        raise Permanent("bad_payload")
    return did


class DonationAudioHandler:
    kind = "donation_audio"
    backoff = Backoff(base_seconds=5, cap_seconds=60, jitter=0.2)

    def __init__(self, repo: AudioRepository, store: ObjectStore, *, speech: dict[str, SpeechProvider],
                 sound_effects: SoundEffectProvider | None, music: MusicProvider | None,
                 moderation_key: str, default_voice: str, providers_enabled: bool,
                 rng: random.Random | None = None) -> None:
        self.repo, self.store = repo, store
        self.speech, self.sound_effects, self.music = speech, sound_effects, music
        self.moderation_key, self.default_voice = moderation_key, default_voice
        self.providers_enabled = providers_enabled
        self.rng = rng or random.Random()

    def _set(self, donation_id: str, url: str) -> Effects:
        async def effects(cur) -> None:
            await self.repo.set_audio_url(cur, donation_id, url)
        return effects

    async def _silence(self, donation_id: str, reason: str) -> Done:
        try:
            url = await ensure_silence(self.store)
        except StorageError:
            raise Retryable("storage_unavailable") from None
        return Done({"outcome": "silent", "reason": reason}, self._set(donation_id, url))

    async def run(self, job: Job) -> Done:
        did = _donation_id(job)
        d = await self.repo.donation_context(did)
        if d is None:
            return Done({"outcome": "skipped", "reason": "donation_missing"})
        if d["payment_status"] != "success" or d["moderation_status"] not in ("approved", "auto_approved"):
            return Done({"outcome": "skipped", "reason": "not_approved"})
        if d["tts_audio_url"]:
            return Done({"outcome": "skipped", "reason": "already_has_audio"})
        if d["hypersound_url"]:
            return Done({"outcome": "hypersound"})
        if d["voice_message_url"]:
            return await self._voice(d)
        plan = plan_audio(d)
        if plan.kind == "silent":
            return await self._silence(did, plan.reason)
        key = audio_key(d["streamer_id"], did)
        try:
            existing = await self.store.head_generated(key)
        except StorageError:
            raise Retryable("storage_unavailable") from None
        if existing is not None and existing.size > 0 and existing.content_type == "audio/mpeg":
            return Done({"outcome": plan.kind, "reused": True}, self._set(did, self.store.public_url(key)))
        provider, call = self._provider(d, plan)
        if provider is None:
            return await self._silence(did, call)  # call holds the reason here
        if plan.kind in COMMANDS:
            limited = await self._command_limit(d, plan.kind)
            if limited:
                return await self._silence(did, limited)
        content = plan.prompt if plan.prompt is not None else self._spoken_message(d, plan)
        blocked = await self._moderate(d, plan.kind if plan.kind in COMMANDS else "tts", content)
        if blocked:
            return await self._silence(did, blocked)
        audio = await call()
        seconds = validate_audio(audio)
        try:
            await self.store.put_object(key, audio, content_type="audio/mpeg")
        except StorageError:
            raise Retryable("storage_unavailable") from None
        return Done({"outcome": plan.kind, "provider": provider, "seconds": round(seconds, 2), "bytes": len(audio)},
                    self._set(did, self.store.public_url(key)))

    async def _voice(self, d: dict) -> Done:
        try:
            key = payment_rules.upload_key(d["voice_message_url"], self.store.public_url(""), d["streamer_slug"],
                                           "voice")
        except ApiError:
            raise Permanent("voice_invalid") from None
        try:
            head = await self.store.head_upload(key)
        except StorageError:
            raise Retryable("storage_unavailable") from None
        if head is None:
            raise Permanent("voice_missing")
        return Done({"outcome": "voice"})

    @staticmethod
    def _spoken_message(d: dict, plan: Plan) -> str | None:
        if plan.is_media or not d["message"]:
            return None
        return tts_text.sanitize_for_tts(tts_text.decode_html_entities(d["message"])) or None

    def _provider(self, d: dict, plan: Plan):
        """(provider name, zero-argument coroutine factory) or (None, reason)."""
        if not self.providers_enabled:
            return None, "providers_disabled"
        if plan.kind == "sfx":
            return ("elevenlabs", lambda: self.sound_effects.sound_effect(plan.prompt)) if self.sound_effects \
                else (None, "provider_not_configured")
        if plan.kind == "music":
            return ("elevenlabs", lambda: self.music.music(plan.prompt)) if self.music \
                else (None, "provider_not_configured")
        spam = not plan.is_media and tts_text.is_emoji_spam(tts_text.decode_html_entities(d["message"] or ""))
        name, voice = route_speech(d["tts_voice_id"], d["el_voice_id"], spam, self.default_voice)
        provider = self.speech.get(name)
        if provider is None:
            return None, "provider_not_configured"
        words = tts_text.announcement(
            donor_name=d["donor_name"], amount=d["amount"], currency=d["currency"], amount_inr=d["amount_inr"],
            message=d["message"], media_type=d["media_type"], is_media=plan.is_media,
            language_boost=d["tts_language_boost"], rng=self.rng)
        return name, lambda: provider.speech(words, voice, d["tts_language_boost"])

    async def _command_limit(self, d: dict, kind: str) -> str | None:
        command, limit = COMMANDS[kind]
        count, in_flight = await self.repo.recent_commands(d["streamer_id"], d["donor_name"], command, d["id"])
        if count >= limit:
            return "command_rate_limited"
        return "command_in_flight" if in_flight else None

    async def _moderate(self, d: dict, source: str, content: str | None) -> str | None:
        """None when allowed, else the silent reason. Fails closed: no key or any error blocks."""
        name = tts_text.decode_html_entities(d["donor_name"] or "")
        streamer = d["streamer_name"] or d["streamer_slug"]
        try:
            identity = supporter_hash(self.moderation_key, d["streamer_id"], name)
            if await self.repo.blocked_recently(identity) >= PROBE_LIMIT:
                decision = Decision(False, "HIGH_RISK", "probing", "probe_limit")
            else:
                decision = moderate_for_tts(name, content, streamer)
            if decision.allowed:
                return None
            if decision.invalid:
                return "moderation_invalid"
            await self.repo.record_block(donation_id=d["id"], streamer_id=d["streamer_id"], identity_hash=identity,
                                         source=source, decision=decision)
            return "moderation_blocked"
        except Exception as exc:  # noqa: BLE001 - fail closed; never logs names or content
            log.warning("moderation unavailable", extra={"reason": "moderation_unavailable", "error": type(exc).__name__})
            return "moderation_unavailable"

    async def fallback(self, job: Job, reason: str) -> Effects | None:
        """Final failure: the silent audio, so the alert still plays (no effect when the donation has a voice
        message or hypersound of its own, already has audio or is no longer paid + approved)."""
        try:
            did = _donation_id(job)
        except Permanent:
            return None
        # No I/O here (R2 may be the very thing that is failing): the worker uploads the silence object at start.
        return self._set(did, self.store.public_url(SILENCE_KEY))


class VoiceTranscriptionHandler:
    kind = "voice_transcription"
    backoff = Backoff(base_seconds=30, cap_seconds=300, jitter=0.2)

    def __init__(self, repo: AudioRepository, store: ObjectStore | None,
                 provider: TranscriptionProvider | None) -> None:
        self.repo, self.store, self.provider = repo, store, provider

    async def run(self, job: Job) -> Done:
        did = _donation_id(job)
        d = await self.repo.donation_context(did)
        if d is None:
            return Done({"outcome": "skipped", "reason": "donation_missing"})
        if d["transcribed"]:
            return Done({"outcome": "skipped", "reason": "already_transcribed"})
        if d["payment_status"] != "success" or not d["voice_message_url"]:
            return Done({"outcome": "skipped", "reason": "not_eligible"})
        if self.provider is None or self.store is None:
            raise Permanent("providers_disabled")
        try:  # only ever this donation's own Phase 6 voice object in this environment's bucket
            key = payment_rules.upload_key(d["voice_message_url"], self.store.public_url(""), d["streamer_slug"],
                                           "voice")
        except ApiError:
            raise Permanent("voice_invalid") from None
        try:
            audio = await self.store.get_voice(key, max_bytes=VOICE_MAX_BYTES)
        except StorageError as exc:
            if str(exc) == "too_large":
                raise Permanent("voice_too_large") from None
            if "NoSuchKey" in str(exc) or "404" in str(exc):
                raise Permanent("voice_missing") from None
            raise Retryable("storage_unavailable") from None
        transcript = await self.provider.transcribe(audio)

        async def effects(cur) -> None:
            await self.repo.insert_transcript(cur, donation_id=did, streamer_id=d["streamer_id"],
                                              transcript=transcript, model=self.provider.model)
        return Done({"outcome": "transcribed", "chars": len(transcript)}, effects)

    async def fallback(self, job: Job, reason: str) -> Effects | None:
        return None
