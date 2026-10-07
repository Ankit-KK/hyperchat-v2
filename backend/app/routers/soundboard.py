"""Creator soundboard (Phase 6): /api/v1/dashboard/streamers/{streamer}/soundboard/...
and the public hypersound library: GET /api/v1/public/hypersounds.

Soundboard routes depend on require_streamer_access (signed in; owner, linked account or admin; an
unclaimed creator is admin-only); writes also pass require_user's Origin + CSRF check. The soundboard
switch is checked only after that. Responses are never cached (dashboard no-store middleware).
"""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.authz.dependencies import StreamerAccess, require_streamer_access
from app.core.errors import ApiError
from app.soundboard.models import (
    AudioReplace,
    Board,
    ChangesApplied,
    Hypersounds,
    ImageReplace,
    Restored,
    SlotCreated,
    SlotResult,
    SoundboardChanges,
    SoundRequest,
    Swapped,
    UploadRequest,
    UploadTicket,
)
from app.soundboard.service import SoundboardService, SwapRejected

soundboard_router = APIRouter(prefix="/api/v1/dashboard/streamers/{streamer}/soundboard", tags=["soundboard"])
hypersounds_router = APIRouter(prefix="/api/v1/public", tags=["public"])


def _service(request: Request) -> SoundboardService:
    service = getattr(request.app.state, "soundboard", None)
    if service is None:
        raise ApiError(503, "Service unavailable", "soundboard_not_configured")
    return service


@soundboard_router.get("", response_model=Board)
async def read_board(request: Request, access: StreamerAccess = Depends(require_streamer_access)) -> Board:
    return await _service(request).board(access)


@soundboard_router.patch("", response_model=ChangesApplied)
async def apply_changes(body: SoundboardChanges, request: Request,
                        access: StreamerAccess = Depends(require_streamer_access)) -> ChangesApplied:
    return await _service(request).apply(access, body)


@soundboard_router.post("/restore-defaults", response_model=Restored)
async def restore_defaults(request: Request, access: StreamerAccess = Depends(require_streamer_access)) -> Restored:
    return await _service(request).restore(access)


@soundboard_router.post("/uploads", response_model=UploadTicket, status_code=201)
async def create_upload(body: UploadRequest, request: Request,
                        access: StreamerAccess = Depends(require_streamer_access)) -> UploadTicket:
    return await _service(request).presign(access, body.kind, body.content_type, body.content_length)


@soundboard_router.post("/slots", response_model=SlotCreated, status_code=201)
async def create_slot(body: SoundRequest, request: Request,
                      access: StreamerAccess = Depends(require_streamer_access)) -> SlotCreated:
    return await _service(request).create(access, body)


@soundboard_router.put("/swappable/{index}", response_model=Swapped)
async def swap_slot(index: str, body: SoundRequest, request: Request,
                    access: StreamerAccess = Depends(require_streamer_access)):
    try:
        return await _service(request).swap(access, index, body)
    except SwapRejected as exc:
        return JSONResponse(exc.body, status_code=429)


@soundboard_router.patch("/slots/{slot_id}/audio", response_model=SlotResult)
async def replace_audio(slot_id: str, body: AudioReplace, request: Request,
                        access: StreamerAccess = Depends(require_streamer_access)) -> SlotResult:
    return await _service(request).replace(access, slot_id, "audio", body.audio_key)


@soundboard_router.patch("/slots/{slot_id}/image", response_model=SlotResult)
async def replace_image(slot_id: str, body: ImageReplace, request: Request,
                        access: StreamerAccess = Depends(require_streamer_access)) -> SlotResult:
    return await _service(request).replace(access, slot_id, "image", body.image_key)


@soundboard_router.delete("/slots/{slot_id}", response_model=SlotResult)
async def delete_slot(slot_id: str, request: Request,
                      access: StreamerAccess = Depends(require_streamer_access)) -> SlotResult:
    return await _service(request).delete(access, slot_id)


@hypersounds_router.get("/hypersounds", response_model=Hypersounds)
async def hypersounds(request: Request, response: Response) -> Hypersounds:
    library = getattr(request.app.state, "hypersounds", None)
    if library is None:
        raise ApiError(503, "Sound library unavailable", "library_not_configured")
    result = await library.sounds()
    response.headers["Cache-Control"] = "public, max-age=60"
    return result
