"""/api/v1/admin — user, invite and creator administration (admins only; CSRF on every change).

Invites are still created with POST /api/v1/auth/invites (with streamer_id to give the invitee a new creator).
"""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from app.admin.service import AdminService
from app.auth.dependencies import CurrentUser, client_ip, require_admin
from app.auth.service import AuthError
from app.payments.accounts import health_report
from app.security.audit import security_event

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["streamer", "admin", "user"] | None = None
    is_active: bool | None = None
    streamer_id: str | None = None  # explicit null unlinks the account from its creator


class CreatorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # never user_id, status or other columns: those are not settable here
    slug: StrictStr = Field(max_length=64)
    name: StrictStr = Field(max_length=200)
    razorpay_account: Literal["primary"]  # required and explicit (D12); D6 retired "secondary"
    brand_color: StrictStr | None = Field(default=None, max_length=7)
    order_prefix: StrictStr | None = Field(default=None, max_length=14)


class SoundboardEligibility(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


class OwnerTransfer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: StrictStr = Field(max_length=64)
    expected_owner_id: StrictStr = Field(max_length=64)


class EmailChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: StrictStr = Field(max_length=320)  # at most 254 once trimmed (the service checks)


class CreatorStatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_status: Literal["pending", "active", "suspended"]  # compare-and-set: a stale request changes nothing
    reason: StrictStr | None = Field(default=None, max_length=500)


class OwnerAssign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: StrictStr = Field(max_length=64)


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


@router.put("/users/{user_id}/email")
async def change_user_email(user_id: str, body: EmailChange, request: Request,
                            current: CurrentUser = Depends(require_admin)) -> dict:
    """D11: correct a user's email (admin recovery). Revokes sessions, burns reset links, removes Google links."""
    return await admin_service(request).change_email(current.user, user_id, body.email, client_ip(request))


@router.post("/users/{user_id}/sessions/revoke")
async def revoke_user_sessions(user_id: str, request: Request, current: CurrentUser = Depends(require_admin)) -> dict:
    return await admin_service(request).revoke_sessions(current.user, user_id)


@router.post("/creators", status_code=201)
async def create_creator(body: CreatorCreate, request: Request, current: CurrentUser = Depends(require_admin)) -> dict:
    return await admin_service(request).create_creator(current.user, body.model_dump())


@router.post("/creators/{streamer_id}/owner")
async def assign_creator_owner(streamer_id: str, body: OwnerAssign, request: Request,
                               current: CurrentUser = Depends(require_admin)) -> dict:
    return await admin_service(request).assign_creator_owner(current.user, streamer_id, body.user_id)


@router.post("/creators/{streamer_id}/owner/transfer")
async def transfer_creator_owner(streamer_id: str, body: OwnerTransfer, request: Request,
                                 current: CurrentUser = Depends(require_admin)) -> dict:
    """D13: move a claimed creator to another active user; the previous owner is unlinked and signed out."""
    return await admin_service(request).transfer_creator_owner(current.user, streamer_id, body.user_id,
                                                               body.expected_owner_id, client_ip(request))


async def _status(action: str, streamer_id: str, body: CreatorStatusChange, request: Request, current: CurrentUser):
    return await admin_service(request).change_creator_status(current.user, streamer_id, action, body.expected_status,
                                                              body.reason, client_ip(request))


@router.post("/creators/{streamer_id}/activate")
async def activate_creator(streamer_id: str, body: CreatorStatusChange, request: Request,
                           current: CurrentUser = Depends(require_admin)) -> dict:
    """pending -> active. Never automatic: approval, invites and KYC alone do not activate a creator."""
    return await _status("activate", streamer_id, body, request, current)


@router.post("/creators/{streamer_id}/suspend")
async def suspend_creator(streamer_id: str, body: CreatorStatusChange, request: Request,
                          current: CurrentUser = Depends(require_admin)) -> dict:
    """active -> suspended: hidden like an unknown creator and refuses new donations; nothing else changes."""
    return await _status("suspend", streamer_id, body, request, current)


@router.post("/creators/{streamer_id}/reactivate")
async def reactivate_creator(streamer_id: str, body: CreatorStatusChange, request: Request,
                             current: CurrentUser = Depends(require_admin)) -> dict:
    """suspended -> active, only if the creator could accept donations again (else 409 with the reason)."""
    return await _status("reactivate", streamer_id, body, request, current)


@router.put("/creators/{streamer_id}/soundboard")
async def set_creator_soundboard(streamer_id: str, body: SoundboardEligibility, request: Request,
                                 current: CurrentUser = Depends(require_admin)) -> dict:
    """Custom Soundboard eligibility only (D3); the owner must still accept the current terms."""
    return await admin_service(request).set_soundboard_eligibility(current.user, streamer_id, body.enabled)


@router.get("/payments/health")
async def payment_health(request: Request, response: Response, current: CurrentUser = Depends(require_admin)) -> dict:
    """D7: is the Razorpay account usable? The same cached probe as checkout (no order, no payment); names and
    states only, never a key, secret or Razorpay response."""
    payments = getattr(request.app.state, "payments", None)
    if payments is None:
        raise AuthError(503, "Payments are not configured", "payments_not_configured")
    report = await health_report(payments.accounts, payments.health)
    security_event("payment_health_checked", level=20, user_id=current.user.id, reason=report["status"])
    response.headers["Cache-Control"] = "no-store"
    return report


@router.get("/invites")
async def list_invites(request: Request, status: str = "pending", limit: int = Query(50, ge=1, le=200),
                       current: CurrentUser = Depends(require_admin)) -> dict:
    return await admin_service(request).list_invites(status=status, limit=limit)


@router.post("/invites/{invite_id}/revoke")
async def revoke_invite(invite_id: str, request: Request, current: CurrentUser = Depends(require_admin)) -> dict:
    return await admin_service(request).revoke_invite(current.user, invite_id)
