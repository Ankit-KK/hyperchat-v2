"""Owner identity verification (Didit, KYC PR3). See app.kyc.

Owner (session; changes need Origin + CSRF):
  GET  /api/v1/me/kyc            the signed-in user's KYC state (never identity data)
  POST /api/v1/me/kyc/session    start or resume Didit verification -> {session_id, status, url}
  POST /api/v1/me/kyc/pan-acknowledgement   the PAN-only notice, accepted (0041; needed before a start)
Didit (no cookies, no CSRF: authenticated by X-Signature-V2 over the whole body + fresh signed timestamps):
  POST /api/v1/webhooks/didit
Admin:
  GET  /api/v1/admin/users/{user_id}/kyc           sessions (status, workflow, dates), audit; no identity data
  POST /api/v1/admin/users/{user_id}/kyc/refresh   reconcile the newest session with Didit's decision endpoint
  POST /api/v1/admin/users/{user_id}/kyc/payout-hold/clear   lift an open payout hold (0041; step-up, reason)
There is deliberately no endpoint that marks a user approved.
"""

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.auth.dependencies import CurrentUser, client_ip, require_admin, require_user
from app.core.errors import ApiError
from app.kyc.enforcement import EnforcementService
from app.kyc.service import KycService
from app.stepup.actions import StepUp, step_up

me_router = APIRouter(prefix="/api/v1/me/kyc", tags=["kyc"])
webhook_router = APIRouter(prefix="/api/v1/webhooks", tags=["kyc"])
admin_router = APIRouter(prefix="/api/v1/admin/users", tags=["admin"])
enforcement_router = APIRouter(prefix="/api/v1/admin", tags=["admin"])
NO_STORE = {"Cache-Control": "no-store"}  # verification links and per-user state
WEBHOOK_MAX_BYTES = 512 * 1024  # decisions carry many fields; the body is never stored


def _service(request: Request) -> KycService:
    service = getattr(request.app.state, "kyc", None)
    if service is None:
        raise ApiError(503, "Identity verification is unavailable right now. Please try again later.",
                       "kyc_not_configured")
    return service


def _user_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ApiError(404, "User not found", "not_found") from None


@me_router.get("")
async def my_kyc(request: Request, current: CurrentUser = Depends(require_user)):
    return JSONResponse(await _service(request).status(current.user), headers=NO_STORE)


@me_router.post("/session")
async def start_kyc(request: Request, current: CurrentUser = Depends(require_user)):
    return JSONResponse(await _service(request).start(current.user, client_ip(request)), headers=NO_STORE)


@me_router.post("/pan-acknowledgement")
async def acknowledge_pan(request: Request, current: CurrentUser = Depends(require_user)):
    """The creator confirms the PAN-only notice before KYC (migration 0041): required for a start within 60 minutes."""
    return JSONResponse(await _service(request).acknowledge_pan(current.user, client_ip(request)), headers=NO_STORE)


@webhook_router.post("/didit")
async def didit_webhook(request: Request):
    service = _service(request)
    if (request.headers.get("content-type") or "").split(";")[0].strip().lower() != "application/json":
        raise ApiError(415, "Expected application/json", "kyc_webhook_content_type")
    too_large = ApiError(413, "Payload too large", "kyc_webhook_too_large")
    length = request.headers.get("content-length")
    if length is not None and (not length.isascii() or not length.isdigit() or int(length) > WEBHOOK_MAX_BYTES):
        raise too_large
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > WEBHOOK_MAX_BYTES:
            raise too_large
    result = await service.webhook(bytes(body), request.headers.get("x-signature-v2"),
                                   request.headers.get("x-timestamp"), client_ip(request))
    return JSONResponse(result, headers=NO_STORE)


@admin_router.get("/{user_id}/kyc")
async def admin_user_kyc(user_id: str, request: Request, current: CurrentUser = Depends(require_admin)):
    return JSONResponse(await _service(request).admin_view(_user_id(user_id)), headers=NO_STORE)


class PayoutHoldClear(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: StrictStr = Field(min_length=1, max_length=500)


@admin_router.post("/{user_id}/kyc/payout-hold/clear")
async def admin_clear_payout_hold(user_id: str, body: PayoutHoldClear, request: Request,
                                  current: CurrentUser = Depends(require_admin),
                                  stepup: StepUp = Depends(step_up("kyc.payout_hold.clear"))):
    """Lift the user's open payout hold (owner decision 2026-11-27, option C: admins resolve non-PAN holds until
    self-service PAN re-verification arrives with payouts, DASH-9). A reason is required; step-up action
    kyc.payout_hold.clear on user:<id> with {hold_id}; the database records who, when and why (migration 0041)."""
    service, target = _service(request), _user_id(user_id)
    hold_id = await service.repo.open_hold(target)
    if hold_id is None:
        raise ApiError(409, "This user has no open payout hold", "not_open")
    grant = stepup.bind(f"user:{target}", {"hold_id": hold_id})
    return JSONResponse(await service.admin_clear_payout_hold(current.user, target, hold_id, body.reason,
                                                              client_ip(request), grant), headers=NO_STORE)


@admin_router.post("/{user_id}/kyc/refresh")
async def admin_refresh_kyc(user_id: str, request: Request, current: CurrentUser = Depends(require_admin)):
    return JSONResponse(await _service(request).admin_refresh(current.user, _user_id(user_id), client_ip(request)),
                        headers=NO_STORE)


# ── KYC enforcement (PR4, migration 0023). No route activates or closes enforcement, grants grace or lifts a
#    restriction: those are the operator's (app.kyc.operator, migration role) after the approvals of
#    docs/kyc-rollout.md. ──

class RestrictionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: StrictStr = Field(max_length=32)
    note: StrictStr | None = Field(default=None, max_length=500)


class GraceRevoke(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: StrictStr = Field(max_length=500)


def _enforcement(request: Request) -> EnforcementService:
    service = getattr(request.app.state, "kyc_enforcement", None)
    if service is None:
        raise ApiError(503, "Administration is not configured", "admin_not_configured")
    return service


@enforcement_router.get("/kyc/enforcement")
async def enforcement_status(request: Request, current: CurrentUser = Depends(require_admin)):
    return JSONResponse(await _enforcement(request).status(), headers=NO_STORE)


@enforcement_router.get("/kyc/enforcement/preview")
async def enforcement_preview(request: Request, current: CurrentUser = Depends(require_admin)):
    """Read-only: what each creator's rule says now, once enforcement is active, and once it is closed."""
    return JSONResponse(await _enforcement(request).preview(), headers=NO_STORE)


@enforcement_router.get("/kyc/enforcement/events")
async def enforcement_events(request: Request, streamer_id: str | None = None,
                             limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0, le=100_000),
                             current: CurrentUser = Depends(require_admin)):
    return JSONResponse(await _enforcement(request).history(streamer_id, limit, offset), headers=NO_STORE)


@enforcement_router.get("/creators/{streamer_id}/kyc")
async def creator_kyc(streamer_id: str, request: Request, current: CurrentUser = Depends(require_admin)):
    return JSONResponse(await _enforcement(request).creator(streamer_id), headers=NO_STORE)


@enforcement_router.post("/creators/{streamer_id}/kyc/restrictions")
async def restrict_creator(streamer_id: str, body: RestrictionCreate, request: Request,
                           current: CurrentUser = Depends(require_admin)):
    return JSONResponse(await _enforcement(request).restrict(current.user.id, streamer_id, body.reason, body.note,
                                                             client_ip(request)), headers=NO_STORE)


@enforcement_router.post("/creators/{streamer_id}/kyc/grace/revoke")
async def revoke_grace(streamer_id: str, body: GraceRevoke, request: Request,
                       current: CurrentUser = Depends(require_admin)):
    return JSONResponse(await _enforcement(request).revoke_grace(current.user.id, streamer_id, body.reason,
                                                                 client_ip(request)), headers=NO_STORE)
