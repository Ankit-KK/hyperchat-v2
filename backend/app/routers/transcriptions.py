"""Voice transcription (Phase 8, D1):

  GET  /api/v1/dashboard/streamers/{streamer}/donations/{donation_id}/transcription   status (+ transcript)
  POST /api/v1/dashboard/streamers/{streamer}/donations/{donation_id}/transcription   request it once

Owner, linked account or admin (require_streamer_access); the POST also passes require_user's Origin + CSRF
check. Responses are never cached (dashboard no-store). The transcription itself runs in the worker.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.audio.transcriptions import TranscriptionService
from app.authz.dependencies import StreamerAccess, require_streamer_access
from app.core.errors import ApiError

router = APIRouter(prefix="/api/v1/dashboard/streamers/{streamer}", tags=["dashboard"])


def _service(request: Request) -> TranscriptionService:
    service = getattr(request.app.state, "transcriptions", None)
    if service is None:
        raise ApiError(503, "Transcription is not available", "transcription_unavailable")
    return service


@router.get("/donations/{donation_id}/transcription")
async def transcription_status(donation_id: str, request: Request,
                               access: StreamerAccess = Depends(require_streamer_access)):
    return await _service(request).status(access, donation_id)


@router.post("/donations/{donation_id}/transcription")
async def request_transcription(donation_id: str, request: Request,
                                access: StreamerAccess = Depends(require_streamer_access)):
    status, body = await _service(request).request(access, donation_id)
    return JSONResponse(body, status_code=status)
