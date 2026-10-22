"""Realtime (Phase 9): Pusher private-channel authorization and client configuration. See app.realtime.auth.

  POST /api/v1/realtime/pusher/auth           OBS token -> overlay channels; session + CSRF -> dashboard
  GET  /api/v1/realtime/config?streamer=slug  {key, cluster, auth_endpoint}
"""

import json
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.auth.dependencies import auth_service, client_ip, enforce_csrf, optional_user, require_admin_assurance
from app.realtime.auth import RATE_CONFIG, RATE_IP, RealtimeService
from app.security.dependencies import SecurityError
from app.security.obs_token import is_obs_origin_allowed, own_origin, validate_obs_token

router = APIRouter(prefix="/api/v1/realtime", tags=["realtime"])
MAX_BODY = 2048
NO_STORE = {"Cache-Control": "no-store"}


def _service(request: Request) -> RealtimeService:
    service = getattr(request.app.state, "realtime", None)
    if service is None:
        raise SecurityError(503, "Realtime is not available", "realtime_unavailable")
    return service


async def _fields(request: Request) -> tuple[str, str]:
    """socket_id and channel_name from a form (pusher-js default) or JSON body."""
    body = await request.body()
    if len(body) > MAX_BODY:
        raise SecurityError(413, "Request too large", "body_too_large")
    try:
        if (request.headers.get("content-type") or "").split(";")[0].strip() == "application/json":
            data = json.loads(body or b"{}")
            data = data if isinstance(data, dict) else {}
        else:
            data = {k: v[0] for k, v in parse_qs(body.decode(), max_num_fields=10).items()}
    except (ValueError, UnicodeDecodeError):
        raise SecurityError(400, "Invalid request", "bad_body") from None
    socket_id, channel = data.get("socket_id"), data.get("channel_name")
    if not isinstance(socket_id, str) or not isinstance(channel, str):
        raise SecurityError(400, "Invalid request", "bad_body")
    return socket_id, channel


@router.post("/pusher/auth")
async def pusher_auth(request: Request):
    svc = _service(request)
    svc.require_available()
    await svc.limit(f"ip:{client_ip(request)}", RATE_IP)
    socket_id, channel = await _fields(request)
    token = request.headers.get("x-obs-token") or request.query_params.get("token") or ""
    has_session = bool(request.cookies.get(request.app.state.settings.session_cookie_name))
    if token and has_session:
        raise SecurityError(400, "Use one credential", "two_credentials")
    slug, suffix = svc.parse(socket_id, channel)
    if token:
        if not is_obs_origin_allowed(request.headers.get("origin"), request.app.state.settings.browser_origins,
                                     own_origin(request.headers.get("host"))):  # Phase 10: the API-served player
            raise SecurityError(403, "Request origin not allowed", "bad_origin")
        result = await validate_obs_token(svc.obs_tokens, token)
        if not result.ok:
            raise SecurityError(401, "Invalid or missing OBS token", result.error or "invalid_token")
        body = await svc.authorize_obs(token, result.token_info, socket_id, channel, slug, suffix)
    else:
        current = await optional_user(request)
        if current is None:
            raise SecurityError(401, "Not signed in", "no_session")
        enforce_csrf(request, auth_service(request), current.session)  # AuthError 403 (bad_origin / bad_csrf)
        body = await svc.authorize_session(current.user, socket_id, channel, slug, suffix,
                                           assure_admin=lambda: require_admin_assurance(request, current.session))
    return JSONResponse(body, headers=NO_STORE)


@router.get("/config")
async def realtime_config(request: Request, streamer: str = ""):
    svc = _service(request)
    svc.require_available()
    await svc.limit(f"ip:{client_ip(request)}", RATE_CONFIG)
    return JSONResponse(await svc.config(streamer), headers={"Cache-Control": "public, max-age=300"})
