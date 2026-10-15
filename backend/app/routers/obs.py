"""OBS audio playback (Phase 10). See app.obs and docs/obs-audio.md.

OBS (OBS token: x-obs-token header, or ?token= for the Media Source URL; Origin absent, allowlisted or the API's own):
  GET  /api/v1/obs/session        the player's start-up data (creator, Pusher config, poll interval)
  GET  /api/v1/obs/overlay-config overlay display settings of the TOKEN's creator (= settings-updated)
  GET  /api/v1/obs/leaderboard    {enabled, top_donor{name, total_amount_inr}|null} of the TOKEN's creator
  POST /api/v1/obs/audio/claim    atomic queue pop -> 200 {donation_id, kind, audio_url, display} | 204 (Retry-After)
  GET  /api/v1/obs/audio/next     the same claim for an OBS Media Source -> 302 to the audio file | 204 (P10-D8)
  GET  /obs/player, /obs/player.js the browser-source audio player (token in the URL #fragment, never sent)
Dashboard (owner / linked account / admin; changes need Origin + CSRF):
  POST   /api/v1/dashboard/streamers/{streamer}/obs-tokens              create (token shown once in the URLs)
  GET    /api/v1/dashboard/streamers/{streamer}/obs-tokens              list (never the token)
  DELETE /api/v1/dashboard/streamers/{streamer}/obs-tokens/{token_id}   revoke now
  POST   /api/v1/dashboard/streamers/{streamer}/donations/{donation_id}/replay
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.authz.dependencies import StreamerAccess, require_streamer_access
from app.core.errors import ApiError
from app.obs.service import ObsService
from app.security.dependencies import ObsCaller, require_obs_token
from app.security.obs_token import own_origin

obs_router = APIRouter(prefix="/api/v1/obs", tags=["obs"])
dashboard_router = APIRouter(prefix="/api/v1/dashboard/streamers/{streamer}", tags=["dashboard"])
player_router = APIRouter(tags=["obs"])
NO_STORE = {"Cache-Control": "no-store"}
STATIC = Path(__file__).resolve().parents[1] / "obs" / "static"


def _service(request: Request) -> ObsService:
    service = getattr(request.app.state, "obs", None)
    if service is None:
        raise ApiError(503, "OBS playback is not available", "obs_unavailable")
    return service


def _no_content(retry_after: int | None) -> Response:
    headers = dict(NO_STORE)
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return Response(status_code=204, headers=headers)


@obs_router.get("/session")
async def obs_session(request: Request, caller: ObsCaller = Depends(require_obs_token)):
    return JSONResponse(await _service(request).session(caller), headers=NO_STORE)


@obs_router.get("/overlay-config")
async def overlay_config(request: Request, caller: ObsCaller = Depends(require_obs_token)):
    return JSONResponse(await _service(request).overlay_config(caller, request.app.state.security.obs_tokens),
                        headers=NO_STORE)


@obs_router.get("/leaderboard")
async def leaderboard(request: Request, caller: ObsCaller = Depends(require_obs_token)):
    return JSONResponse(await _service(request).leaderboard(caller, request.app.state.security.obs_tokens),
                        headers=NO_STORE)


@obs_router.post("/audio/claim")
async def claim_audio(request: Request, caller: ObsCaller = Depends(require_obs_token)):
    service = _service(request)
    result = await service.claim(caller)
    if result.item is None:
        return _no_content(result.retry_after)
    return JSONResponse(service.claim_body(result), headers=NO_STORE)


@obs_router.get("/audio/next")
async def next_audio(request: Request, caller: ObsCaller = Depends(require_obs_token)):
    """OBS Media Source compatibility (legacy get-current-audio): a 302 to the static audio file, or 204."""
    result = await _service(request).claim(caller)
    if result.item is None:
        return _no_content(result.retry_after)
    return RedirectResponse(result.item.audio_url, status_code=302, headers=NO_STORE)


@dashboard_router.post("/obs-tokens", status_code=201)
async def create_obs_token(request: Request, access: StreamerAccess = Depends(require_streamer_access)):
    api_base = own_origin(request.headers.get("host")) or str(request.base_url)
    return JSONResponse(await _service(request).create_token(access, api_base), status_code=201)


@dashboard_router.get("/obs-tokens")
async def list_obs_tokens(request: Request, access: StreamerAccess = Depends(require_streamer_access)):
    return await _service(request).list_tokens(access)


@dashboard_router.delete("/obs-tokens/{token_id}")
async def revoke_obs_token(token_id: str, request: Request, access: StreamerAccess = Depends(require_streamer_access)):
    return await _service(request).revoke_token(access, token_id)


@dashboard_router.post("/donations/{donation_id}/replay")
async def replay_donation_audio(donation_id: str, request: Request,
                                access: StreamerAccess = Depends(require_streamer_access)):
    return await _service(request).replay(access, donation_id)


def _csp(request: Request) -> str:
    media = request.app.state.settings.r2_public_base or "'none'"
    return ("default-src 'none'; script-src 'self'; connect-src 'self' wss://*.pusher.com; "
            f"media-src {media}/; style-src 'self'; img-src 'none'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")


@player_router.get("/obs/player")
async def obs_player(request: Request):
    return Response((STATIC / "player.html").read_bytes(), media_type="text/html; charset=utf-8",
                    headers={"Content-Security-Policy": _csp(request), "Cache-Control": "no-cache",
                             "X-Robots-Tag": "noindex"})


@player_router.get("/obs/player.js")
async def obs_player_js():
    return Response((STATIC / "player.js").read_bytes(), media_type="text/javascript; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})


@player_router.get("/obs/player.css")
async def obs_player_css():
    return Response((STATIC / "player.css").read_bytes(), media_type="text/css; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})
