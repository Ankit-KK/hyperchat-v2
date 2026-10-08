import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.admin.repository import AdminRepository
from app.admin.service import AdminService
from app.auth.repository import AuthRepository
from app.auth.service import AuthError, AuthService
from app.authz.repository import AuthzRepository
from app.core.config import get_settings
from app.core.errors import install_api_error_handler
from app.core.logging import configure_logging
from app.dashboard.repository import DashboardRepository
from app.dashboard.service import DashboardService
from app.db.pool import Database
from app.public.repository import PublicRepository
from app.public.service import PublicService
from app.audio.repository import AudioRepository
from app.audio.transcriptions import TranscriptionService
from app.jobs.queue import JobQueue
from app.payments.accounts import AccountHealth, PaymentAccounts
from app.payments.razorpay import RazorpayClient
from app.payments.repository import PaymentRepository
from app.payments.service import PaymentService
from app.bots.clients import TelegramClient
from app.bots.discord import DiscordBot
from app.bots.repository import BotRepository
from app.bots.telegram import TelegramBot
from app.obs.queue import AudioQueue
from app.obs.service import ObsService
from app.obs.tokens import TokenRepository
from app.realtime.auth import RealtimeService
from app.realtime.payloads import RealtimeRepository
from app.realtime.pusher import PusherApps
from app.routers import (
    admin,
    auth,
    bots,
    dashboard,
    health,
    obs,
    payments,
    public,
    realtime,
    soundboard,
    streamers,
    transcriptions,
    uploads,
)
from app.security.audit import security_event
from app.security.cors import CorsMiddleware
from app.security.dependencies import SecurityStores, install_security_error_handler
from app.security.pg_stores import PostgresNonceStore, PostgresObsTokenStore, PostgresPartnerKeyStore
from app.soundboard.library import HypersoundLibrary
from app.soundboard.repository import SoundboardRepository
from app.soundboard.service import SoundboardService
from app.storage.r2 import build_object_store
from app.uploads.service import PublicCreators, UploadService

log = logging.getLogger("hyperchat.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info(
        "starting hyperchat-api",
        extra={"release": settings.release, "environment": settings.app_env, "db_label": settings.db_label},
    )
    if settings.app_env != "production":
        log.warning("non-production database", extra={"db_label": settings.db_label})
    app.state.db = Database(settings)
    await app.state.db.open()
    store = build_object_store(settings)  # None until every R2 variable is set and valid
    app.state.hypersounds = HypersoundLibrary(store)
    payment_accounts = PaymentAccounts.from_settings(settings)
    if app.state.db.enabled:
        app.state.auth = AuthService.from_settings(settings, AuthRepository(app.state.db.connection))
        app.state.authz = AuthzRepository(app.state.db.connection)
        app.state.admin = AdminService(AdminRepository(app.state.db.connection))
        conn = app.state.db.connection
        app.state.public = PublicService(PublicRepository(conn))
        app.state.dashboard = DashboardService(DashboardRepository(conn))
        app.state.security = SecurityStores(PostgresNonceStore(conn), PostgresObsTokenStore(conn),
                                            PostgresPartnerKeyStore(conn))
        # Donor uploads reuse the public creator lookup, the request_nonces store and the auth rate limiter.
        app.state.uploads = UploadService(
            grant_secret=settings.upload_grant_secret, creators=PublicCreators(app.state.public),
            nonces=app.state.security.nonces, limiter=app.state.auth.repo, store=store)
        app.state.soundboard = SoundboardService(SoundboardRepository(conn), store, limiter=app.state.auth.repo)
        # Phase 8: the API only enqueues transcription jobs; the worker (python -m app.worker) runs them.
        app.state.transcriptions = TranscriptionService(
            AudioRepository(conn), JobQueue(conn), app.state.auth.repo,
            available=store is not None and "sarvam" in settings.audio_provider_names())
        razorpay = RazorpayClient()
        app.state.payments = PaymentService(
            public=app.state.public, repo=PaymentRepository(conn, vaani_creators=settings.vaani_slugs), accounts=payment_accounts, client=razorpay,
            health=AccountHealth(razorpay), library=app.state.hypersounds, store=store, limiter=app.state.auth.repo)
        # Phase 9: channel authorization and client config; events are published by the worker.
        app.state.realtime = RealtimeService(
            enabled=settings.realtime_enabled, apps=PusherApps.from_settings(settings), repo=RealtimeRepository(conn),
            obs_tokens=app.state.security.obs_tokens, limiter=app.state.auth.repo)
        # Phase 10: OBS token management, the audio queue (claim / Media Source / replay) and the player's session.
        app.state.obs = ObsService(
            tokens=TokenRepository(conn), queue=AudioQueue(conn), realtime=RealtimeRepository(conn),
            apps=PusherApps.from_settings(settings), realtime_enabled=settings.realtime_enabled,
            public_base=settings.r2_public_base if store is not None else None, limiter=app.state.auth.repo)
        # Phase 11: bot webhooks only exist while bots are enabled AND the platform is configured (D13).
        bot_repo, moderation_repo = BotRepository(conn), DashboardRepository(conn)
        public_base = settings.r2_public_base if store is not None else None
        if settings.bots_enabled and settings.telegram_configured:
            app.state.telegram_bot = TelegramBot(
                secret=settings.telegram_webhook_secret.get_secret_value().strip(),
                client=TelegramClient(settings.telegram_bot_token.get_secret_value().strip()), repo=bot_repo,
                moderation=moderation_repo, nonces=app.state.security.nonces, limiter=app.state.auth.repo,
                recipients=settings.bot_recipients, frontend_url=settings.frontend_url, public_base=public_base)
        if settings.bots_enabled and settings.discord_configured:
            app.state.discord_bot = DiscordBot(public_key=settings.discord_public_key.strip(), repo=bot_repo,
                                               moderation=moderation_repo, nonces=app.state.security.nonces)
    log.info("authentication", extra={"auth_configured": settings.auth_secret is not None,
                                      "email_backend": settings.email_backend,
                                      "google_enabled": settings.google_enabled})
    # Variable names only, never values.
    log.info("uploads", extra={"upload_grants_configured": settings.upload_grants_configured,
                               "r2_configured": settings.r2_configured,
                               "r2_missing_or_invalid": settings.r2_config_problems()})
    # Account names (primary/secondary) only, never key ids or secrets.
    log.info("payments", extra={"razorpay_accounts": payment_accounts.summary()})
    log.info("audio", extra={"providers_enabled": settings.audio_providers_enabled,
                             "providers": settings.audio_provider_names()})
    # Group numbers and variable names only, never keys or secrets.
    log.info("realtime", extra={"realtime_enabled": settings.realtime_enabled,
                                "pusher_groups": settings.pusher_groups_configured,
                                "pusher_invalid": settings.pusher_config_problems()})
    # Booleans and counts only, never tokens, secrets or recipient ids.
    log.info("bots", extra={"bots_enabled": settings.bots_enabled, "telegram_configured": settings.telegram_configured,
                            "discord_configured": settings.discord_configured,
                            "recipient_allowlist": None if settings.bot_recipients is None else len(settings.bot_recipients)})
    log.info("obs", extra={"audio_queue_window_hours": 24, "vaani_creators": len(settings.vaani_slugs)})
    try:
        yield
    finally:
        await app.state.db.close()
        log.info("stopped hyperchat-api")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    docs = settings.enable_docs
    app = FastAPI(
        title="HyperChat API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )

    app.state.settings = settings
    access = logging.getLogger("hyperchat.access")

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        request_id = uuid.uuid4().hex[:16]
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        access.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,  # no query string: may contain tokens
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            },
        )
        return response

    install_security_error_handler(app)
    install_api_error_handler(app)
    dashboard.install_no_store(app)

    @app.exception_handler(AuthError)
    async def _auth_error(request: Request, exc: AuthError) -> JSONResponse:
        if exc.status >= 500:
            security_event("auth_unavailable", reason=exc.reason, status=exc.status, path=request.url.path)
        return JSONResponse({"error": exc.error}, status_code=exc.status)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(streamers.router)
    app.include_router(admin.router)
    app.include_router(public.streamers_router)
    app.include_router(public.metrics_router)
    app.include_router(uploads.router)
    app.include_router(soundboard.hypersounds_router)
    app.include_router(payments.checkout_router)
    app.include_router(payments.donations_router)
    app.include_router(payments.webhooks_router)
    app.include_router(dashboard.dashboard_router)
    app.include_router(transcriptions.router)
    app.include_router(realtime.router)
    app.include_router(obs.obs_router)
    app.include_router(obs.dashboard_router)
    app.include_router(obs.player_router)
    app.include_router(bots.router)
    app.include_router(dashboard.me_router)
    app.include_router(soundboard.soundboard_router)
    # Outermost: preflights are answered before routing, every response gets CORS headers.
    app.add_middleware(CorsMiddleware, allowed_origins=settings.browser_origins)
    return app


app = create_app()
