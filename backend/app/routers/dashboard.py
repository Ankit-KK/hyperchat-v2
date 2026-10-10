"""Creator dashboard API (Phase 5): /api/v1/dashboard/streamers/{streamer}/... and /api/v1/me/terms.

Creator routes depend on require_streamer_access (owner, linked account or admin; an unclaimed creator
is admin-only); POST/PATCH/DELETE also pass require_user's Origin + CSRF check. Responses on these
paths are never cached (install_no_store).
"""

from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import StreamingResponse

from app.auth.dependencies import CurrentUser, require_user
from app.authz.dependencies import StreamerAccess, require_streamer_access
from app.core.errors import ApiError
from app.dashboard import csv_export
from app.dashboard.models import (
    BannedDonorList,
    DashboardSettings,
    DonationPage,
    DonationStats,
    ModerationRequest,
    ModerationResult,
    Moderator,
    ModeratorCreate,
    ModeratorList,
    ModeratorPermissionsPatch,
    ModeratorPermissionsResult,
    ModeratorRemoval,
    QueuePage,
    SettingsPatch,
    SettingsUpdate,
    TermsAccept,
    TermsAccepted,
    TermsStatus,
    UnbanResult,
)
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


# ── G2 donations ────────────────────────────────────────────────────────────────

@dashboard_router.get("/donations", response_model=DonationPage)
async def list_donations(request: Request, limit: int = Query(20, ge=1, le=100), cursor: str | None = None,
                         access: StreamerAccess = Depends(require_streamer_access)) -> DonationPage:
    return await _service(request).donations(access, limit=limit, cursor=cursor)


@dashboard_router.get("/donations/stats", response_model=DonationStats)
async def donation_stats(request: Request,
                         access: StreamerAccess = Depends(require_streamer_access)) -> DonationStats:
    return await _service(request).donation_stats(access)


@dashboard_router.get("/donations/export.csv", response_class=StreamingResponse,
                      responses={200: {"content": {"text/csv": {}}, "description": "CSV attachment"}})
async def export_donations(request: Request, start: str | None = Query(None, alias="from"),
                           end: str | None = Query(None, alias="to"),
                           access: StreamerAccess = Depends(require_streamer_access)) -> StreamingResponse:
    # Rows are fetched first (the pooled connection is released), then encoded while streaming.
    filename, rows = await _service(request).export_donations(access, start, end)
    return StreamingResponse(csv_export.encode(rows), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ── G3 moderation ───────────────────────────────────────────────────────────────

@dashboard_router.get("/moderation/queue", response_model=QueuePage)
async def moderation_queue(request: Request, status: Literal["pending", "approved", "rejected", "all"] = "pending",
                           limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
                           access: StreamerAccess = Depends(require_streamer_access)) -> QueuePage:
    return await _service(request).moderation.queue(access, status=status, limit=limit, cursor=cursor)


@dashboard_router.get("/moderation/banned-donors", response_model=BannedDonorList)
async def banned_donors(request: Request,
                        access: StreamerAccess = Depends(require_streamer_access)) -> BannedDonorList:
    return await _service(request).moderation.banned_donors(access)


@dashboard_router.post("/moderation/banned-donors/{ban_id}/unban", response_model=UnbanResult)
async def unban_donor(ban_id: str, request: Request,
                      access: StreamerAccess = Depends(require_streamer_access)) -> UnbanResult:
    return await _service(request).moderation.unban(access, ban_id)


@dashboard_router.post("/donations/{donation_id}/moderation", response_model=ModerationResult)
async def moderate_donation(donation_id: str, body: ModerationRequest, request: Request,
                            access: StreamerAccess = Depends(require_streamer_access)) -> ModerationResult:
    return await _service(request).moderation.moderate(access, donation_id, body.action, body.reason)


# ── G4 moderators ───────────────────────────────────────────────────────────────

@dashboard_router.get("/moderators", response_model=ModeratorList)
async def list_moderators(request: Request,
                          access: StreamerAccess = Depends(require_streamer_access)) -> ModeratorList:
    return await _service(request).moderators(access)


@dashboard_router.post("/moderators", response_model=Moderator, status_code=201)
async def add_moderator(body: ModeratorCreate, request: Request,
                        access: StreamerAccess = Depends(require_streamer_access)) -> Moderator:
    return await _service(request).add_moderator(access, body)


@dashboard_router.patch("/moderators/{moderator_id}", response_model=ModeratorPermissionsResult)
async def update_moderator(moderator_id: str, body: ModeratorPermissionsPatch, request: Request,
                           access: StreamerAccess = Depends(require_streamer_access)) -> ModeratorPermissionsResult:
    return await _service(request).update_moderator(access, moderator_id, body)


@dashboard_router.delete("/moderators/{moderator_id}", response_model=ModeratorRemoval)
async def remove_moderator(moderator_id: str, request: Request,
                           access: StreamerAccess = Depends(require_streamer_access)) -> ModeratorRemoval:
    return await _service(request).remove_moderator(access, moderator_id)


# ── G5 terms (the signed-in user's own acceptance) ──────────────────────────────

@me_router.get("/terms", response_model=TermsStatus)
async def terms_status(request: Request, current: CurrentUser = Depends(require_user)) -> TermsStatus:
    return await _service(request).terms_status(current.user)


@me_router.post("/terms/accept", response_model=TermsAccepted)
async def accept_terms(body: TermsAccept, request: Request,
                       current: CurrentUser = Depends(require_user)) -> TermsAccepted:
    return await _service(request).accept_terms(current.user, body.version, request.headers.get("user-agent"))
