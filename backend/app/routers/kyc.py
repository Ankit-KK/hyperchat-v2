"""Owner identity verification (Didit, KYC PR3). See app.kyc.

Owner (session; changes need Origin + CSRF):
  GET  /api/v1/me/kyc            the signed-in user's KYC state (never identity data)
  POST /api/v1/me/kyc/session    start or resume Didit verification -> {session_id, status, url}
Didit (no cookies, no CSRF: authenticated by X-Signature-V2 over the whole body + fresh signed timestamps):
  POST /api/v1/webhooks/didit
Admin:
  GET  /api/v1/admin/users/{user_id}/kyc           sessions (status, workflow, dates), audit; no identity data
  POST /api/v1/admin/users/{user_id}/kyc/refresh   reconcile the newest session with Didit's decision endpoint
There is deliberately no endpoint that marks a user approved.
"""

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.auth.dependencies import CurrentUser, client_ip, require_admin, require_user
from app.core.errors import ApiError
from app.kyc.service import KycService

me_router = APIRouter(prefix="/api/v1/me/kyc", tags=["kyc"])
webhook_router = APIRouter(prefix="/api/v1/webhooks", tags=["kyc"])
admin_router = APIRouter(prefix="/api/v1/admin/users", tags=["admin"])
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


@admin_router.post("/{user_id}/kyc/refresh")
async def admin_refresh_kyc(user_id: str, request: Request, current: CurrentUser = Depends(require_admin)):
    return JSONResponse(await _service(request).admin_refresh(current.user, _user_id(user_id), client_ip(request)),
                        headers=NO_STORE)
