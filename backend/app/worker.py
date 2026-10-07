"""The background worker: python -m app.worker (deploy/systemd/hyperchat-worker.service).

Reads the same environment as the API (EnvironmentFile), connects as the same DML-only role (hyperchat_app,
D6) and processes donation_audio and voice_transcription jobs with HYPERCHAT_WORKER_CONCURRENCY slots.
Paid providers are only constructed when HYPERCHAT_AUDIO_PROVIDERS_ENABLED=true and their key is set (D7);
otherwise donation audio resolves to the silent fallback and transcription jobs fail as unavailable.
SIGTERM/SIGINT stop it gracefully (see app.jobs.runner). Logs carry names and reason codes only.
"""

import asyncio
import logging
import signal

from app.audio.handlers import DonationAudioHandler, VoiceTranscriptionHandler
from app.audio.providers import ElevenLabsProvider, MiniMaxProvider, SarvamProvider
from app.audio.repository import AudioRepository
from app.audio.silence import ensure_silence
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.pool import Database
from app.jobs.queue import JobQueue
from app.jobs.runner import Worker
from app.storage.r2 import StorageError, build_object_store

log = logging.getLogger("hyperchat.worker")


def build_handlers(settings: Settings, connection_factory, store) -> dict:
    enabled = settings.audio_providers_enabled
    minimax_key, el_key, sarvam_key = (settings.provider_key(n) for n in ("minimax", "elevenlabs", "sarvam"))
    elevenlabs = ElevenLabsProvider(el_key) if enabled and el_key else None
    speech = {}
    if enabled and minimax_key:
        speech["minimax"] = MiniMaxProvider(minimax_key)
    if elevenlabs:
        speech["elevenlabs"] = elevenlabs
    moderation_key = settings.tts_moderation_hash_key.get_secret_value().strip() \
        if settings.tts_moderation_hash_key is not None else ""
    repo = AudioRepository(connection_factory)
    handlers = {
        "voice_transcription": VoiceTranscriptionHandler(
            repo, store, SarvamProvider(sarvam_key) if enabled and sarvam_key else None),
    }
    if store is not None:  # donation audio needs R2 (generated audio and the silence object)
        handlers["donation_audio"] = DonationAudioHandler(
            repo, store, speech=speech, sound_effects=elevenlabs, music=elevenlabs, moderation_key=moderation_key,
            default_voice=settings.minimax_default_voice_id, providers_enabled=enabled)
    return handlers


async def run(settings: Settings, stop: asyncio.Event | None = None) -> int:
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # e.g. Windows development machines
            pass
    db = Database(settings)
    await db.open()
    if not db.enabled:
        log.error("worker needs the database", extra={"reason": "database_disabled"})
        return 1
    try:
        store = build_object_store(settings)
        if store is not None:
            try:
                await ensure_silence(store)
            except StorageError as exc:  # retried by the jobs themselves; never fatal at start
                log.warning("silence object not ensured", extra={"reason": str(exc)})
        handlers = build_handlers(settings, db.connection, store)
        log.info("worker configuration", extra={
            "release": settings.release, "db_label": settings.db_label, "concurrency": settings.worker_concurrency,
            "providers_enabled": settings.audio_providers_enabled, "providers": settings.audio_provider_names(),
            "r2_configured": store is not None, "moderation_key_set": bool(settings.tts_moderation_hash_key),
            "kinds": sorted(handlers)})
        await Worker(JobQueue(db.connection), handlers, concurrency=settings.worker_concurrency).run(stop)
    finally:
        await db.close()
    return 0


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    return asyncio.run(run(settings))


if __name__ == "__main__":
    raise SystemExit(main())
