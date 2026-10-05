"""FastAPI dependencies for creator-scoped routes.

Usage:  @router.get("/api/v1/streamers/{streamer}/...")
        async def handler(access: StreamerAccess = Depends(require_streamer_access)): ...
The path parameter must be called `streamer` (a UUID or a slug). State-changing methods also get
the CSRF/Origin check through require_user.
"""

from dataclasses import dataclass

from fastapi import Depends, Request

from app.auth.dependencies import CurrentUser, require_user
from app.auth.service import AuthError
from app.authz.access import AccessVia, StreamerRef, access_via, denial_reason, parse_streamer_ref
from app.authz.repository import AuthzRepository
from app.security.audit import security_event


@dataclass(frozen=True)
class StreamerAccess:
    current: CurrentUser
    streamer: StreamerRef
    via: AccessVia


def authz_repository(request: Request) -> AuthzRepository:
    repo = getattr(request.app.state, "authz", None)
    if repo is None:
        raise AuthError(503, "Authorization is not configured", "authz_not_configured")
    return repo


async def require_streamer_access(streamer: str, request: Request,
                                  current: CurrentUser = Depends(require_user)) -> StreamerAccess:
    ref = parse_streamer_ref(streamer)
    found = await authz_repository(request).streamer(ref) if ref else None
    if found is None:
        raise AuthError(404, "Streamer not found", "streamer_not_found")
    via = access_via(current.user, found)
    if via is None:
        reason = denial_reason(current.user, found)
        security_event("streamer_access_denied", reason=reason, user_id=current.user.id, streamer_id=found.id,
                       path=request.url.path, method=request.method)
        message = "This creator account is not claimed yet" if reason == "unclaimed_streamer" else "Forbidden"
        raise AuthError(403, message, reason)
    return StreamerAccess(current=current, streamer=found, via=via)
