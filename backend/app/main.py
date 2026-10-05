import logging
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.auth.repository import AuthRepository
from app.auth.service import AuthError, AuthService
from app.authz.repository import AuthzRepository
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.pool import Database
from app.routers import auth, health, streamers
from app.security.audit import security_event
from app.security.cors import CorsMiddleware

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
    if app.state.db.enabled:
        app.state.auth = AuthService.from_settings(settings, AuthRepository(app.state.db.connection))
        app.state.authz = AuthzRepository(app.state.db.connection)
    log.info("authentication", extra={"auth_configured": settings.auth_secret is not None,
                                      "email_backend": settings.email_backend,
                                      "google_enabled": settings.google_enabled})
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

    @app.exception_handler(AuthError)
    async def _auth_error(request: Request, exc: AuthError) -> JSONResponse:
        if exc.status >= 500:
            security_event("auth_unavailable", reason=exc.reason, status=exc.status, path=request.url.path)
        return JSONResponse({"error": exc.error}, status_code=exc.status)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(streamers.router)
    # Outermost: preflights are answered before routing, every response gets CORS headers.
    front = urlparse(settings.frontend_url)
    app.add_middleware(CorsMiddleware, extra_origins=(f"{front.scheme}://{front.netloc}",))
    return app


app = create_app()
