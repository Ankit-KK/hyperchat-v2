"""donation_audio and voice_transcription handlers with in-memory fakes (repository, R2, providers).
tests/jobs/test_jobs_db.py repeats the important flows through PostgreSQL and the real worker."""

import asyncio
import logging
import random
import uuid
from decimal import Decimal

import pytest

from app.audio.handlers import (
    DonationAudioHandler,
    VoiceTranscriptionHandler,
    audio_key,
    parse_command,
    plan_audio,
    route_speech,
)
from app.audio.silence import silent_mp3
from app.jobs.queue import Job
from app.jobs.retry import Permanent, Retryable
from app.storage.memory import InMemoryObjectStore, StoredObject
from app.storage.r2 import SILENCE_KEY

BASE = "https://media.example.test"
SID, DID = str(uuid.uuid4()), str(uuid.uuid4())
VOICE_KEY = f"voice-messages/creator-a_1800000000000_{'d' * 32}.webm"
MP3 = silent_mp3()
SECRET_MESSAGE = "my very private donor message"
MOD_KEY = "TEST-ONLY-moderation-key"


def donation(**kw) -> dict:
    row = dict(id=DID, streamer_id=SID, donor_name="Ravi", amount=Decimal("100.00"), currency="INR",
               amount_inr=Decimal("100.00"), message=SECRET_MESSAGE, payment_status="success",
               moderation_status="auto_approved", tts_audio_url=None, voice_message_url=None, hypersound_url=None,
               media_url=None, media_type=None, is_hyperemote=False, streamer_slug="creator-a",
               streamer_name="Creator A", tts_enabled=True, min_tts_amount_inr=None,
               tts_voice_id="moss_audio_creator", el_voice_id=None, tts_language_boost="Hindi", transcribed=False)
    row.update(kw)
    return row


class FakeRepo:
    def __init__(self, row: dict | None) -> None:
        self.row, self.commands, self.blocked, self.fail_probe = row, (0, False), 0, False
        self.blocks, self.hashes, self.urls, self.transcripts, self.command_queries = [], [], {}, {}, []

    async def donation_context(self, did):
        return dict(self.row) if self.row and self.row["id"] == did else None

    async def recent_commands(self, streamer_id, donor_name, prefix, donation_id):
        self.command_queries.append(prefix)
        return self.commands

    async def blocked_recently(self, identity):
        if self.fail_probe:
            raise RuntimeError("db down")
        self.hashes.append(identity)
        return self.blocked

    async def record_block(self, **kw):
        self.blocks.append(kw)

    async def set_audio_url(self, cur, did, url):
        self.urls[did] = url

    async def insert_transcript(self, cur, **kw):
        self.transcripts[kw["donation_id"]] = kw


class FakeSpeech:
    def __init__(self, name, result=MP3):
        self.name, self.result, self.calls = name, result, []

    async def speech(self, text, voice, boost):
        self.calls.append((text, voice, boost))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeCommands:
    def __init__(self, result=MP3):
        self.result, self.sfx, self.music_calls = result, [], []

    async def sound_effect(self, prompt):
        self.sfx.append(prompt)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def music(self, prompt):
        self.music_calls.append(prompt)
        return self.result


class World:
    def __init__(self, row=None, *, enabled=True, mod_key=MOD_KEY):
        self.repo = FakeRepo(row if row is not None else donation())
        self.store = InMemoryObjectStore(BASE)
        self.minimax, self.eleven, self.commands = FakeSpeech("minimax"), FakeSpeech("elevenlabs"), FakeCommands()
        self.handler = DonationAudioHandler(
            self.repo, self.store, speech={"minimax": self.minimax, "elevenlabs": self.eleven},
            sound_effects=self.commands, music=self.commands, moderation_key=mod_key,
            default_voice="moss_audio_default", providers_enabled=enabled, rng=random.Random(3))

    def provider_calls(self):
        return len(self.minimax.calls) + len(self.eleven.calls) + len(self.commands.sfx) + len(self.commands.music_calls)

    def run(self, job=None):
        done = asyncio.run(self.handler.run(job or make_job()))
        if done.effects:
            asyncio.run(done.effects(None))
        return done


def make_job(did=DID, payload=None) -> Job:
    return Job(str(uuid.uuid4()), "donation_audio", did, {"donation_id": did} if payload is None else payload,
               1, 4, "w1", False)


GEN_URL = f"{BASE}/tts-audio/{SID}/{DID}.mp3"
SILENCE_URL = f"{BASE}/{SILENCE_KEY}"


# ── eligibility and the decision tree ───────────────────────────────────────

@pytest.mark.parametrize("row, reason", [
    (None, "donation_missing"), (donation(payment_status="pending"), "not_approved"),
    (donation(payment_status="failed"), "not_approved"), (donation(moderation_status="rejected"), "not_approved"),
    (donation(moderation_status="pending"), "not_approved"), (donation(tts_audio_url="https://x/y.mp3"), "already_has_audio"),
])
def test_ineligible_donations_are_skipped(row, reason):
    w = World()
    w.repo.row = row
    done = w.run()
    assert done.result == {"outcome": "skipped", "reason": reason} and done.effects is None and w.provider_calls() == 0


def test_hypersound_plays_itself():
    w = World(donation(hypersound_url=f"{BASE}/hypersounds/Horn.mp3"))
    assert w.run().result == {"outcome": "hypersound"} and w.repo.urls == {} and w.provider_calls() == 0


def test_voice_must_be_this_creators_existing_upload():
    w = World(donation(voice_message_url=f"{BASE}/{VOICE_KEY}"))
    w.store.objects[VOICE_KEY] = StoredObject(b"v", "audio/webm", "", {})
    assert w.run().result == {"outcome": "voice"} and w.repo.urls == {} and w.provider_calls() == 0


@pytest.mark.parametrize("url, reason", [
    (f"https://evil.example/{VOICE_KEY}", "voice_invalid"),
    (f"{BASE}/voice-messages/other-creator_1800000000000_{'d' * 32}.webm", "voice_invalid"),
    (f"{BASE}/voice-messages/../{VOICE_KEY}", "voice_invalid"),
    ("http://169.254.169.254/latest/meta-data", "voice_invalid"),
    (f"{BASE}/{VOICE_KEY}", "voice_missing"),
])
def test_bad_or_missing_voice_is_a_permanent_failure(url, reason):
    w = World(donation(voice_message_url=url))
    with pytest.raises(Permanent) as exc:
        w.run()
    assert exc.value.reason == reason and w.provider_calls() == 0


def test_text_donation_uses_minimax_and_a_deterministic_key():
    w = World()
    done = w.run()
    assert done.result["outcome"] == "speech" and done.result["provider"] == "minimax"
    assert w.repo.urls == {DID: GEN_URL} and w.store.objects[f"tts-audio/{SID}/{DID}.mp3"].content_type == "audio/mpeg"
    ((text, voice, boost),) = w.minimax.calls
    assert voice == "moss_audio_creator" and boost == "Hindi" and "Ravi" in text and "100 rupees" in text
    assert SECRET_MESSAGE in text


@pytest.mark.parametrize("tts_voice, el_voice, message, provider, voice", [
    ("moss_audio_x", None, "hi", "minimax", "moss_audio_x"),
    ("moss_audio_x", "elv", "hi", "minimax", "moss_audio_x"),
    ("moss_audio_x", "elv", "\U0001F600\U0001F601\U0001F602\U0001F923\U0001F603\U0001F604", "elevenlabs", "elv"),
    ("hindi_female_1_v2", "elv", "hi", "elevenlabs", "elv"),
    ("hindi_female_1_v2", None, "hi", "minimax", "moss_audio_default"),  # D4: never an id MiniMax rejects
    (None, None, "hi", "minimax", "moss_audio_default"),
])
def test_speech_routing(tts_voice, el_voice, message, provider, voice):
    w = World(donation(tts_voice_id=tts_voice, el_voice_id=el_voice, message=message))
    assert w.run().result["provider"] == provider
    calls = w.minimax.calls if provider == "minimax" else w.eleven.calls
    assert calls[0][1] == voice
    assert route_speech(tts_voice, el_voice, False, "d")[0] == ("elevenlabs" if tts_voice and not tts_voice.startswith(
        "moss") and el_voice else "minimax")


@pytest.mark.parametrize("cols, reason", [
    ({"amount_inr": Decimal("39.99")}, "below_tts_minimum"),
    ({"amount_inr": Decimal("79"), "min_tts_amount_inr": Decimal("80")}, "below_tts_minimum"),
    ({"amount_inr": Decimal("39"), "min_tts_amount_inr": Decimal("10")}, "below_tts_minimum"),  # Rs 40 floor wins
    ({"tts_enabled": False}, "tts_disabled"),
    ({"tts_enabled": False, "media_url": f"{BASE}/donation-media/x.png"}, "tts_disabled"),
    ({"message": "!sfx"}, "invalid_command"), ({"message": "!testmusic " + "x" * 501}, "invalid_command"),
])
def test_silent_cases(cols, reason):
    w = World(donation(**cols))
    done = w.run()
    assert done.result == {"outcome": "silent", "reason": reason} and w.repo.urls == {DID: SILENCE_URL}
    assert w.store.objects[SILENCE_KEY].data == silent_mp3() and w.provider_calls() == 0


def test_tts_minimum_boundaries():
    assert plan_audio(donation(amount_inr=Decimal("40"))).kind == "speech"
    assert plan_audio(donation(amount_inr=Decimal("80"), min_tts_amount_inr=Decimal("80"))).kind == "speech"
    assert plan_audio(donation(amount_inr=Decimal("5"), media_url="m", media_type="gif")).kind == "speech"  # media: no minimum


def test_media_announcement():
    w = World(donation(media_url=f"{BASE}/donation-media/a.gif", media_type="gif"))
    done = w.run()
    text = w.minimax.calls[0][0]
    assert done.result["outcome"] == "speech" and text.endswith("And they sent a GIF.") and SECRET_MESSAGE not in text


@pytest.mark.parametrize("message, kind, prompt", [("!sfx door creak", "sfx", "door creak"),
                                                   ("  !SFX   thunder ", "sfx", "thunder"),
                                                   ("!testmusic lofi &amp; rain", "music", "lofi & rain")])
def test_commands(message, kind, prompt):
    w = World(donation(message=message))
    done = w.run()
    assert done.result["outcome"] == kind and w.repo.urls == {DID: GEN_URL}
    assert (w.commands.sfx if kind == "sfx" else w.commands.music_calls) == [prompt]
    assert w.minimax.calls == []


def test_command_parsing():
    assert parse_command("!sfx", "!sfx") == "" and parse_command("!sfxy boom", "!sfx") is None
    assert parse_command("hello !sfx boom", "!sfx") is None and parse_command(None, "!sfx") is None


@pytest.mark.parametrize("commands, reason", [((10, False), "command_rate_limited"), ((0, True), "command_in_flight")])
def test_sfx_limits(commands, reason):
    w = World(donation(message="!sfx boom"))
    w.repo.commands = commands
    assert w.run().result == {"outcome": "silent", "reason": reason} and w.commands.sfx == []
    assert w.repo.command_queries == ["!sfx"]


def test_music_limit_is_five():
    w = World(donation(message="!testmusic beat"))
    w.repo.commands = (5, False)
    assert w.run().result["reason"] == "command_rate_limited"
    w = World(donation(message="!testmusic beat"))
    w.repo.commands = (4, False)
    assert w.run().result["outcome"] == "music" and w.repo.command_queries == ["!testmusic"]


def test_providers_disabled_means_silence_and_no_call():
    w = World(enabled=False)
    assert w.run().result == {"outcome": "silent", "reason": "providers_disabled"} and w.provider_calls() == 0


def test_routed_provider_not_configured():
    w = World(donation(tts_voice_id="other", el_voice_id="elv"))
    del w.handler.speech["elevenlabs"]
    assert w.run().result == {"outcome": "silent", "reason": "provider_not_configured"}
    w = World(donation(message="!sfx boom"))
    w.handler.sound_effects = None
    assert w.run().result["reason"] == "provider_not_configured"


# ── idempotency ─────────────────────────────────────────────────────────────

def test_existing_object_is_reused_without_a_provider_call():
    w = World()
    w.store.objects[f"tts-audio/{SID}/{DID}.mp3"] = StoredObject(MP3, "audio/mpeg", "", {})
    done = w.run()
    assert done.result == {"outcome": "speech", "reused": True} and w.repo.urls == {DID: GEN_URL}
    assert w.provider_calls() == 0


def test_audio_keys_only_from_uuids():
    assert audio_key(SID, DID) == f"tts-audio/{SID}/{DID}.mp3"
    for bad in ("../x", "x/../../y", SID.upper(), "", None):
        with pytest.raises(Permanent):
            audio_key(bad, DID)


@pytest.mark.parametrize("did, payload", [("not-a-uuid", {"donation_id": "not-a-uuid"}),
                                          (DID, {"donation_id": str(uuid.uuid4())}), (DID, {})])
def test_bad_payload_is_permanent(did, payload):
    w = World()
    with pytest.raises(Permanent) as exc:
        asyncio.run(w.handler.run(make_job(did, payload)))
    assert exc.value.reason == "bad_payload"


# ── moderation ──────────────────────────────────────────────────────────────

def test_moderation_block_is_audited_with_a_hash_and_silenced():
    w = World(donation(message="you are a nigger", donor_name="Rahul"))
    done = w.run()
    assert done.result == {"outcome": "silent", "reason": "moderation_blocked"} and w.provider_calls() == 0
    (block,) = w.repo.blocks
    assert block["source"] == "tts" and block["decision"].category == "racial"
    assert len(block["identity_hash"]) == 64 and "Rahul" not in block["identity_hash"].lower()


def test_probe_limit_blocks_clean_text_too():
    w = World(donation(message="nice stream"))
    w.repo.blocked = 3
    assert w.run().result["reason"] == "moderation_blocked" and w.repo.blocks[0]["decision"].rule_id == "probe_limit"


def test_commands_are_moderated_with_their_source():
    w = World(donation(message="!sfx kill you"))
    assert w.run().result["reason"] == "moderation_blocked" and w.repo.blocks[0]["source"] == "sfx"
    assert w.commands.sfx == []


@pytest.mark.parametrize("setup", ["no_key", "probe_error"])
def test_moderation_fails_closed(setup):
    w = World(mod_key="" if setup == "no_key" else MOD_KEY)
    w.repo.fail_probe = setup == "probe_error"
    assert w.run().result == {"outcome": "silent", "reason": "moderation_unavailable"}
    assert w.provider_calls() == 0 and w.repo.blocks == []


def test_same_supporter_same_hash_per_creator():
    from app.audio.repository import supporter_hash
    assert supporter_hash("k", SID, " Ravi ") == supporter_hash("k", SID, "ravi")
    assert supporter_hash("k", SID, "ravi") != supporter_hash("k", str(uuid.uuid4()), "ravi")
    assert supporter_hash("k", SID, "ravi") != supporter_hash("other", SID, "ravi")
    with pytest.raises(ValueError):
        supporter_hash("", SID, "ravi")


# ── failures and fallback ───────────────────────────────────────────────────

@pytest.mark.parametrize("error", [Retryable("provider_timeout"), Retryable("provider_rate_limited", 7),
                                   Permanent("provider_auth"), Permanent("provider_quota")])
def test_provider_errors_propagate(error):
    w = World()
    w.minimax.result = error
    with pytest.raises(type(error)) as exc:
        w.run()
    assert exc.value.reason == error.reason and w.repo.urls == {}


def test_malformed_audio_is_retryable_and_never_stored():
    w = World()
    w.minimax.result = b"<html>gateway error</html>" * 20
    with pytest.raises(Retryable) as exc:
        w.run()
    assert exc.value.reason == "invalid_audio" and not any(k.startswith("tts-audio/") for k in w.store.objects)


def test_r2_failures_are_retryable():
    w = World()
    w.store.fail_with, w.store.fail_ops = "r2_InternalError", {"put"}
    with pytest.raises(Retryable) as exc:
        w.run()
    assert exc.value.reason == "storage_unavailable" and w.repo.urls == {}
    w = World()
    w.store.fail_with, w.store.fail_ops = "r2_InternalError", {"head"}
    with pytest.raises(Retryable):
        w.run()


def test_fallback_writes_silence_without_touching_r2():
    w = World()
    w.store.fail_with = "r2_InternalError"  # every R2 operation fails
    effects = asyncio.run(w.handler.fallback(make_job(), "provider_auth"))
    asyncio.run(effects(None))
    assert w.repo.urls == {DID: SILENCE_URL}
    assert asyncio.run(w.handler.fallback(make_job("bad", {"donation_id": "bad"}), "x")) is None


def test_nothing_sensitive_is_logged(caplog):
    caplog.set_level(logging.DEBUG)
    w = World(donation(message=SECRET_MESSAGE))
    w.run()
    w2 = World(mod_key="")
    w2.repo.fail_probe = True
    w2.run()
    text = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert SECRET_MESSAGE not in text and MOD_KEY not in text and "Ravi" not in text


# ── voice transcription ─────────────────────────────────────────────────────

class FakeSarvam:
    model = "saaras:v3"

    def __init__(self, result="namaste"):
        self.result, self.calls = result, []

    async def transcribe(self, audio):
        self.calls.append(audio)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def transcribe(row, store=None, provider="default"):
    repo = FakeRepo(row)
    store = store or InMemoryObjectStore(BASE)
    sarvam = FakeSarvam() if provider == "default" else provider
    h = VoiceTranscriptionHandler(repo, store, sarvam)
    job = Job(str(uuid.uuid4()), "voice_transcription", DID, {"donation_id": DID}, 1, 3, "w", False)
    done = asyncio.run(h.run(job))
    if done.effects:
        asyncio.run(done.effects(None))
    return done, repo, sarvam


def test_transcription_reads_only_the_donations_own_voice_object():
    store = InMemoryObjectStore(BASE)
    store.objects[VOICE_KEY] = StoredObject(b"OPUS-BYTES", "audio/webm", "", {})
    done, repo, sarvam = transcribe(donation(voice_message_url=f"{BASE}/{VOICE_KEY}"), store)
    assert done.result == {"outcome": "transcribed", "chars": 7} and sarvam.calls == [b"OPUS-BYTES"]
    assert repo.transcripts[DID]["transcript"] == "namaste" and repo.transcripts[DID]["model"] == "saaras:v3"


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data", "file:///etc/passwd",
                                 f"https://evil.example/{VOICE_KEY}", f"{BASE}/voice-messages/../../system/silence.mp3",
                                 f"{BASE}/voice-messages/other-creator_1800000000000_{'d' * 32}.webm"])
def test_transcription_never_fetches_other_urls(url):
    sarvam = FakeSarvam()
    with pytest.raises(Permanent) as exc:
        transcribe(donation(voice_message_url=url), provider=sarvam)
    assert exc.value.reason == "voice_invalid" and sarvam.calls == []


@pytest.mark.parametrize("row, reason", [(donation(transcribed=True, voice_message_url="x"), "already_transcribed"),
                                         (donation(payment_status="pending", voice_message_url="x"), "not_eligible"),
                                         (donation(), "not_eligible"), (None, "donation_missing")])
def test_transcription_skips(row, reason):
    done, _, sarvam = transcribe(row)
    assert done.result["reason"] == reason and sarvam.calls == []


def test_transcription_failures():
    url = f"{BASE}/{VOICE_KEY}"
    with pytest.raises(Permanent) as exc:
        transcribe(donation(voice_message_url=url), provider=None)
    assert exc.value.reason == "providers_disabled"
    with pytest.raises(Permanent) as exc:
        transcribe(donation(voice_message_url=url))  # object missing
    assert exc.value.reason == "voice_missing"
    big = InMemoryObjectStore(BASE)
    big.objects[VOICE_KEY] = StoredObject(b"x" * (5 * 1024 * 1024 + 1), "audio/webm", "", {})
    with pytest.raises(Permanent) as exc:
        transcribe(donation(voice_message_url=url), big)
    assert exc.value.reason == "voice_too_large"
    down = InMemoryObjectStore(BASE)
    down.fail_with = "r2_InternalError"
    with pytest.raises(Retryable):
        transcribe(donation(voice_message_url=url), down)
