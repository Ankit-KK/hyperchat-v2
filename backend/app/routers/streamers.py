"""Creator-scoped access endpoints used by the frontend to decide which dashboards to offer."""

from fastapi import APIRouter, Depends, Request

from app.auth.dependencies import CurrentUser, require_user
from app.authz.access import StreamerRef
from app.authz.dependencies import StreamerAccess, authz_repository, require_streamer_access

router = APIRouter(prefix="/api/v1", tags=["streamers"])


def _public(s: StreamerRef) -> dict:
    return {"id": s.id, "slug": s.slug, "name": s.name}


@router.get("/me/streamers")
async def my_streamers(request: Request, current: CurrentUser = Depends(require_user)) -> dict:
    """Creators the signed-in user owns or is linked to. Admins can additionally open any creator."""
    streamers = await authz_repository(request).streamers_of_user(current.user.id, current.user.streamer_id)
    return {"is_admin": current.user.role == "admin", "streamers": [_public(s) for s in streamers]}


@router.get("/streamers/{streamer}/access")
async def streamer_access(access: StreamerAccess = Depends(require_streamer_access)) -> dict:
    return {"streamer": _public(access.streamer), "access": access.via}
