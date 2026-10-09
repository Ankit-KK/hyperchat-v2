"""The background worker: python -m app.worker (deploy/systemd/hyperchat-worker.service).

Reads the same environment as the API (EnvironmentFile), connects as the same DML-only role (hyperchat_app,
D6) and processes donation_audio and voice_transcription jobs with HYPERCHAT_WORKER_CONCURRENCY slots.
Next to them, one loop publishes the realtime outbox to Pusher (Phase 9, app.realtime.publisher); while
HYPERCHAT_REALTIME_ENABLED is false it only marks events 'skipped'.
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
from app.bots.clients import DiscordClient, TelegramClient
from app.bots.notify import ModeratorNotifyHandler
from app.bots.repository import BotRepository
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.pool import Database
from app.jobs.queue import JobQueue
from app.jobs.runner import Worker
from app.realtime.outbox import OutboxQueue
from app.realtime.payloads import Renderer, RealtimeRepository
from app.realtime.publisher import OutboxPublisher
from app.realtime.pusher import PusherApps, PusherClient
from app.storage.r2 import SILENCE_KEY, StorageError, build_object_store

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
    # Phase 11: moderator notifications. Clients only exist when bots are enabled and the platform is configured
    # (D13); otherwise every job finishes as skipped (bots_disabled) without sending anything.
    telegram = (TelegramClient(settings.telegram_bot_token.get_secret_value().strip())
                if settings.bots_enabled and settings.telegram_configured else None)
    discord = (DiscordClient(settings.discord_bot_token.get_secret_value().strip())
               if settings.bots_enabled and settings.discord_configured else None)
    handlers["moderator_notify"] = ModeratorNotifyHandler(
        BotRepository(connection_factory), telegram=telegram, discord=discord,
        enabled=settings.bots_enabled and (telegram is not None or discord is not None),
        recipients=settings.bot_recipients, frontend_url=settings.frontend_url,
        public_base=settings.r2_public_base if store is not None else None, creators=settings.bot_creators)
    if store is not None:  # donation audio needs R2 (generated audio and the silence object)
        handlers["donation_audio"] = DonationAudioHandler(
            repo, store, speech=speech, sound_effects=elevenlabs, music=elevenlabs, moderation_key=moderation_key,
            default_voice=settings.minimax_default_voice_id, providers_enabled=enabled)
    return handlers


def build_publisher(settings: Settings, connection_factory) -> OutboxPublisher:
    silence_url = f"{settings.r2_public_base}/{SILENCE_KEY}" if settings.r2_public_base else None  # Phase 10
    return OutboxPublisher(OutboxQueue(connection_factory),
                           Renderer(RealtimeRepository(connection_factory), silence_url=silence_url),
                           PusherClient(), PusherApps.from_settings(settings), enabled=settings.realtime_enabled,
                           rate=settings.realtime_publish_rate)


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
            "kinds": sorted(handlers), "bots_enabled": settings.bots_enabled,
            "telegram_configured": settings.telegram_configured, "discord_configured": settings.discord_configured})
        publisher = build_publisher(settings, db.connection)
        log.info("realtime configuration", extra={
            "realtime_enabled": settings.realtime_enabled, "pusher_groups": settings.pusher_groups_configured,
            "pusher_invalid": settings.pusher_config_problems()})
        await asyncio.gather(
            Worker(JobQueue(db.connection), handlers, concurrency=settings.worker_concurrency).run(stop),
            publisher.run(stop))
    finally:
        await db.close()
    return 0


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    return asyncio.run(run(settings))


if __name__ == "__main__":
    raise SystemExit(main())
