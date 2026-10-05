"""/api/v1/admin — user and invite administration (admins only; CSRF on every change).

Invites are still created with POST /api/v1/auth/invites.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict

from app.admin.service import AdminService
from app.auth.dependencies import CurrentUser, require_admin
from app.auth.service import AuthError

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["streamer", "admin", "user"] | None = None
    is_active: bool | None = None
    streamer_id: str | None = None  # explicit null unlinks the account from its creator


def admin_service(request: Request) -> AdminService:
    service = getattr(request.app.state, "admin", None)
    if service is None:
        raise AuthError(503, "Administration is not configured", "admin_not_configured")
    return service


@router.get("/users")
async def list_users(request: Request, q: str | None = Query(None, max_length=254),
                     role: str | None = None, active: bool | None = None,
                     limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=100_000),
                     current: CurrentUser = Depends(require_admin)) -> dict:
    return await admin_service(request).list_users(query=q, role=role, active=active, limit=limit, offset=offset)


@router.patch("/users/{user_id}")
async def update_user(user_id: str, body: UserPatch, request: Request,
                      current: CurrentUser = Depends(require_admin)) -> dict:
    changes = {k: getattr(body, k) for k in body.model_fields_set}
    if not changes:
        raise AuthError(400, "Nothing to change", "empty_patch")
    if ("role" in changes and changes["role"] is None) or ("is_active" in changes and changes["is_active"] is None):
        raise AuthError(400, "role and is_active cannot be null", "null_field")
    return await admin_service(request).update_user(current.user, user_id, changes)


@router.post("/users/{user_id}/sessions/revoke")
async def revoke_user_sessions(user_id: str, request: Request, current: CurrentUser = Depends(require_admin)) -> dict:
    return await admin_service(request).revoke_sessions(current.user, user_id)


@router.get("/invites")
async def list_invites(request: Request, status: str = "pending", limit: int = Query(50, ge=1, le=200),
                       current: CurrentUser = Depends(require_admin)) -> dict:
    return await admin_service(request).list_invites(status=status, limit=limit)


@router.post("/invites/{invite_id}/revoke")
async def revoke_invite(invite_id: str, request: Request, current: CurrentUser = Depends(require_admin)) -> dict:
    return await admin_service(request).revoke_invite(current.user, invite_id)
