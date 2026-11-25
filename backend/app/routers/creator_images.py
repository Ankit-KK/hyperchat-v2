"""Creator images (onboarding Stage 4, Phase A): PUT /api/v1/streamers/{streamer}/images/{profile,background} and
GET /api/v1/streamers/{streamer}/images. See docs/creator-images.md.

Owner, linked account (or an admin, with admin assurance) through require_streamer_access; PUT also passes
require_user's Origin + CSRF check. A pending creator may upload (onboarding happens while pending). The request
body is the raw image; Content-Type is its type. Responses are never cached.
"""

import asyncio

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from app.authz.dependencies import StreamerAccess, require_streamer_access
from app.core.errors import ApiError
from app.onboarding.images import DECODE_SLOTS, CreatorImageService, check_declared, parse_crop, too_large
from app.routers.uploads import _read_body

router = APIRouter(prefix="/api/v1/streamers/{streamer}/images", tags=["creator-images"])
NO_STORE = {"Cache-Control": "no-store"}


def service_for(app: FastAPI) -> CreatorImageService:
    """One service per process (its decode slots are per process), built from the app's pool, R2 store and limiter."""
    service = getattr(app.state, "creator_images", None)
    if service is None:
        db, auth, uploads = (getattr(app.state, n, None) for n in ("db", "auth", "uploads"))
        if db is None or not db.enabled or auth is None or uploads is None:
            raise ApiError(503, "Image uploads are not available", "uploads_not_configured")
        service = CreatorImageService(db.connection, uploads.store, auth.repo, asyncio.Semaphore(DECODE_SLOTS))
        app.state.creator_images = service
    return service


@router.get("")
async def list_images(request: Request, access: StreamerAccess = Depends(require_streamer_access)) -> JSONResponse:
    return JSONResponse(await service_for(request.app).current(access.streamer.id), headers=NO_STORE)


async def _upload(kind: str, request: Request, access: StreamerAccess, crop=None) -> JSONResponse:
    service = service_for(request.app)
    service.ensure_available()
    content_type = request.headers.get("content-type")
    limit = check_declared(kind, content_type, request.headers.get("content-length"))
    await service.check_rate(access.streamer.id)  # before the body: refused attempts count too
    data = await _read_body(request, limit, too_large(kind))
    result = await service.upload(access.streamer.id, kind, data, content_type, crop)
    return JSONResponse(result, headers=NO_STORE)


@router.put("/profile")
async def upload_profile(request: Request,
                         access: StreamerAccess = Depends(require_streamer_access)) -> JSONResponse:
    return await _upload("profile", request, access)


@router.put("/background")
async def upload_background(request: Request, crop_x: int | None = Query(None, ge=0),
                            crop_y: int | None = Query(None, ge=0), crop_width: int | None = Query(None, gt=0),
                            crop_height: int | None = Query(None, gt=0),
                            access: StreamerAccess = Depends(require_streamer_access)) -> JSONResponse:
    return await _upload("background", request, access, parse_crop(crop_x, crop_y, crop_width, crop_height))
