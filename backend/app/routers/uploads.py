"""Donor uploads (Phase 6): POST /api/v1/public/streamers/{slug}/upload-grant and .../uploads/{voice,media}.

Public (donors are anonymous): no cookies, but the request must come from a trusted frontend Origin and
every action is rate limited per IP. Uploads are raw request bodies (Content-Type = the file's type) with
the single-use grant in the x-upload-grant header. See docs/uploads-api.md.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.auth.dependencies import client_ip
from app.core.errors import ApiError
from app.security.audit import security_event
from app.uploads.service import UNAVAILABLE, UploadService

router = APIRouter(prefix="/api/v1/public/streamers", tags=["uploads"])
NO_STORE = {"Cache-Control": "no-store"}  # grants are credentials; upload results are per-request


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["voice", "media"]


def _service(request: Request) -> UploadService:
    service = getattr(request.app.state, "uploads", None)
    if service is None:
        raise ApiError(503, UNAVAILABLE, "uploads_not_configured")
    return service


def _guard(action: str):
    """Trusted Origin, configuration, then the per-IP rate limit; runs before the body is touched."""
    async def dependency(request: Request) -> UploadService:
        origin = request.headers.get("origin")
        if not origin or origin not in request.app.state.settings.browser_origins:
            security_event("upload_rejected", reason="bad_origin", client_ip=client_ip(request))
            raise ApiError(403, "Request origin not allowed", "bad_origin")
        service = _service(request)
        service.ensure_available()
        await service.check_rate(action, client_ip(request))
        return service
    return dependency


async def _read_body(request: Request, limit: int, too_large: ApiError) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:  # also covers chunked uploads without Content-Length
            raise too_large
    return bytes(body)


@router.post("/{slug}/upload-grant", status_code=201)
async def upload_grant(slug: str, body: GrantRequest, service: UploadService = Depends(_guard("grant"))):
    return JSONResponse(await service.issue_grant(slug, body.kind), status_code=201, headers=NO_STORE)


async def _upload(kind: str, slug: str, request: Request, service: UploadService) -> JSONResponse:
    ticket = await service.prepare(kind, slug, content_type=request.headers.get("content-type"),
                                   content_length=request.headers.get("content-length"),
                                   grant=request.headers.get("x-upload-grant"), client_ip=client_ip(request))
    data = await _read_body(request, ticket.max_bytes, ticket.too_large)
    store = service.store_voice if kind == "voice" else service.store_media
    return JSONResponse(await store(ticket, data), status_code=201, headers=NO_STORE)


@router.post("/{slug}/uploads/voice", status_code=201)
async def upload_voice(slug: str, request: Request, service: UploadService = Depends(_guard("voice"))):
    return await _upload("voice", slug, request, service)


@router.post("/{slug}/uploads/media", status_code=201)
async def upload_media(slug: str, request: Request, service: UploadService = Depends(_guard("media"))):
    return await _upload("media", slug, request, service)
