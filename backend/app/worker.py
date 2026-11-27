"""The background worker: python -m app.worker (deploy/systemd/hyperchat-worker.service).

Reads the same environment as the API (EnvironmentFile), connects as the same DML-only role (hyperchat_app,
D6; the automatic invitations also open hyperchat_lifecycle, migration 0038) and processes donation_audio and voice_transcription jobs with HYPERCHAT_WORKER_CONCURRENCY slots.
Next to them, one loop publishes the realtime outbox to Pusher (Phase 9, app.realtime.publisher); while
HYPERCHAT_REALTIME_ENABLED is false it only marks events 'skipped'. A third loop records payment-hold alerts and
delivers operator alerts to Discord (app.alerts, docs/alerting.md); failed jobs are recorded as alerts too.
With HYPERCHAT_AUTO_INVITE_ENABLED=true and the `onboarding` email category active, a fourth loop invites
approved creator applicants (app.onboarding.auto_invite, docs/onboarding.md). With the lifecycle role configured, a
fifth loop activates creators that meet every onboarding condition, only while the database's operator switch is on
(app.onboarding.auto_activate, migration 0042).
Paid providers are only constructed when HYPERCHAT_AUDIO_PROVIDERS_ENABLED=true and their key is set (D7);
otherwise donation audio resolves to the silent fallback and transcription jobs fail as unavailable.
SIGTERM/SIGINT stop it gracefully (see app.jobs.runner). Logs carry names and reason codes only.
"""

import asyncio
import logging
import signal

from app.admin.applications import ApplicationRepository
from app.alerts import AlertNotifier, Alerts
from app.audio.handlers import DonationAudioHandler, VoiceTranscriptionHandler
from app.audio.providers import ElevenLabsProvider, MiniMaxProvider, SarvamProvider
from app.audio.repository import AudioRepository
from app.audio.silence import ensure_silence
from app.bots.clients import DiscordClient, TelegramClient
from app.bots.notify import ModeratorNotifyHandler, allowed
from app.auth.email import sender_from_settings
from app.bots.repository import BotRepository
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.pool import Database, single_connection_factory
from app.jobs.queue import JobQueue
from app.jobs.runner import Worker
from app.onboarding.auto_activate import AutoActivator
from app.onboarding.auto_invite import AutoInviter
from app.payments.accounts import PaymentAccounts
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
        recipients=settings.bot_recipients, frontend_url=settings.dashboard_link_url,
        public_base=settings.r2_public_base if store is not None else None, creators=settings.bot_creators)
    if store is not None:  # donation audio needs R2 (generated audio and the silence object)
        handlers["donation_audio"] = DonationAudioHandler(
            repo, store, speech=speech, sound_effects=elevenlabs, music=elevenlabs, moderation_key=moderation_key,
            default_voice=settings.minimax_default_voice_id, providers_enabled=enabled)
    return handlers


def build_auto_inviter(settings: Settings, connection_factory) -> AutoInviter | None:
    """The automatic invitation loop, or None while it is off or could not send (docs/onboarding.md)."""
    if not settings.auto_invite_enabled:
        return None
    if settings.email_backend != "resend" or "onboarding" not in settings.email_categories:
        log.warning("automatic invitations not started", extra={"reason": "onboarding_email_inactive"})
        return None
    if not settings.db_lifecycle_user:  # migration 0038: the invitation is issued on hyperchat_lifecycle
        log.warning("automatic invitations not started", extra={"reason": "lifecycle_role_unset"})
        return None
    lifecycle = single_connection_factory(settings.db_conninfo(role="lifecycle"))
    return AutoInviter(ApplicationRepository(connection_factory, lifecycle), sender_from_settings(settings),
                       Alerts(connection_factory),
                       frontend_url=settings.auth_link_url, ttl_days=settings.invite_ttl_days,
                       poll_seconds=settings.auto_invite_poll_seconds)


def build_auto_activator(settings: Settings, connection_factory) -> AutoActivator | None:
    """The automatic activation loop (migration 0042), or None without the lifecycle role. It runs always and does
    nothing while the database's operator switch is off."""
    if not settings.db_lifecycle_user:
        log.warning("automatic activation not started", extra={"reason": "lifecycle_role_unset"})
        return None
    accounts = frozenset(PaymentAccounts.from_settings(settings).summary()["payments"])  # as admin activation checks
    return AutoActivator(connection_factory, single_connection_factory(settings.db_conninfo(role="lifecycle")),
                         accounts, poll_seconds=settings.auto_activation_poll_seconds)


def build_notifier(settings: Settings, connection_factory) -> AlertNotifier:
    """Operator alerts (docs/alerting.md): delivered only with a channel and the Discord bot token; outside production
    only to a channel on HYPERCHAT_BOT_RECIPIENT_ALLOWLIST (D13). Otherwise they are recorded only."""
    channel, token = settings.alert_discord_channel_id, settings.discord_bot_token
    token = token.get_secret_value().strip() if token is not None else ""
    if channel and not allowed(settings.bot_recipients, channel):
        log.warning("alert channel not allow-listed: alerts are recorded only", extra={"reason": "not_allowlisted"})
        channel = None
    discord = DiscordClient(token) if channel and token else None
    return AlertNotifier(Alerts(connection_factory), discord=discord, channel_id=channel, app_env=settings.app_env,
                         interval=settings.alert_poll_seconds)


def build_publisher(settings: Settings, connection_factory) -> OutboxPublisher:
    silence_url = f"{settings.r2_public_base}/{SILENCE_KEY}" if settings.r2_public_base else None  # Phase 10
    return OutboxPublisher(OutboxQueue(connection_factory),
                           Renderer(RealtimeRepository(connection_factory), silence_url=silence_url,
                                    admin_session_hours=(settings.admin_session_idle_hours,
                                                         settings.admin_session_absolute_hours)),
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
        inviter = build_auto_inviter(settings, db.connection)
        activator = build_auto_activator(settings, db.connection)
        log.info("onboarding configuration", extra={"auto_invite_enabled": inviter is not None,
                                                    "auto_activation_loop": activator is not None})
        try:
            await asyncio.gather(
                Worker(JobQueue(db.connection), handlers, concurrency=settings.worker_concurrency,
                       on_failure=Alerts(db.connection).job_failed).run(stop),
                publisher.run(stop), build_notifier(settings, db.connection).run(stop),
                *([inviter.run(stop)] if inviter else []), *([activator.run(stop)] if activator else []))
        finally:
            if inviter is not None:
                await inviter.sender.aclose()
    finally:
        await db.close()
    return 0


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    return asyncio.run(run(settings))


if __name__ == "__main__":
    raise SystemExit(main())
