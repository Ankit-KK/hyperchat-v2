"""Creator dashboard API (Phase 5): /api/v1/dashboard/streamers/{streamer}/... and /api/v1/me/terms.

Creator routes depend on require_streamer_access (owner, linked account or admin; an unclaimed creator
is admin-only); POST/PATCH/DELETE also pass require_user's Origin + CSRF check. Responses on these
paths are never cached (install_no_store).
"""

from fastapi import APIRouter, Depends, FastAPI, Request

from app.authz.dependencies import StreamerAccess, require_streamer_access
from app.core.errors import ApiError
from app.dashboard.models import DashboardSettings, SettingsPatch, SettingsUpdate
from app.dashboard.service import DashboardService

dashboard_router = APIRouter(prefix="/api/v1/dashboard/streamers/{streamer}", tags=["dashboard"])
me_router = APIRouter(prefix="/api/v1/me", tags=["dashboard"])


def is_private_path(path: str) -> bool:
    """Dashboard and terms paths: per-user data that no browser or proxy may cache."""
    return (path == "/api/v1/dashboard" or path.startswith("/api/v1/dashboard/")
            or path == "/api/v1/me/terms" or path.startswith("/api/v1/me/terms/"))


def install_no_store(app: FastAPI) -> None:
    """Cache-Control: no-store on every dashboard/terms response. Runs outside the exception handlers,
    so validation (422), authorization (401/403) and business errors (400/404/409) get it too."""

    @app.middleware("http")
    async def _no_store(request: Request, call_next):
        response = await call_next(request)
        if is_private_path(request.url.path):
            response.headers["Cache-Control"] = "no-store"
        return response


def _service(request: Request) -> DashboardService:
    service = getattr(request.app.state, "dashboard", None)
    if service is None:
        raise ApiError(503, "Service unavailable", "dashboard_not_configured")
    return service


# ── G1 settings ─────────────────────────────────────────────────────────────────

@dashboard_router.get("/settings", response_model=DashboardSettings)
async def read_settings(request: Request,
                        access: StreamerAccess = Depends(require_streamer_access)) -> DashboardSettings:
    return await _service(request).settings(access)


@dashboard_router.patch("/settings", response_model=SettingsUpdate)
async def update_settings(body: SettingsPatch, request: Request,
                          access: StreamerAccess = Depends(require_streamer_access)) -> SettingsUpdate:
    return await _service(request).update_settings(access, body.changes())
