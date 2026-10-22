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
from app.stepup.actions import StepUp, step_up, uid

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


class ApplicationApprove(CreatorCreate):
    """The pending creator to create for an approved application (the same rules as POST /admin/creators)."""


class ApplicationReject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: StrictStr = Field(max_length=500)


class ApplicationNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: StrictStr | None = Field(default=None, max_length=2000)


class OwnerAssign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: StrictStr = Field(max_length=64)


def _creator_params(svc: AdminService, body: CreatorCreate) -> dict:
    """The step-up parameters of a new creator (S3: what the database reads back from the inserted row)."""
    fields = svc.creator_fields(body.model_dump())
    return {"slug": fields["streamer_slug"], "name": fields["streamer_name"],
            "razorpay_account": fields["razorpay_account"]}


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
                      current: CurrentUser = Depends(require_admin),
                      stepup: StepUp = Depends(step_up("admin.user.change"))) -> dict:
    changes = {k: getattr(body, k) for k in body.model_fields_set}
    if not changes:
        raise AuthError(400, "Nothing to change", "empty_patch")
    if ("role" in changes and changes["role"] is None) or ("is_active" in changes and changes["is_active"] is None):
        raise AuthError(400, "role and is_active cannot be null", "null_field")
    svc, key, grant = admin_service(request), uid(user_id), None
    if stepup.presented is not None or stepup.enforcement == "active":  # S3: the account's resulting privileges
        before = (await svc.repo.user(key) if key else None) or {}
        after = {k: before.get(k) for k in ("role", "is_active", "streamer_id")}
        after.update({k: uid(v) if k == "streamer_id" else v for k, v in changes.items()})
        grant = stepup.bind(f"user:{key or user_id}", after)
    return await svc.update_user(current.user, user_id, changes, grant)


@router.put("/users/{user_id}/email")
async def change_user_email(user_id: str, body: EmailChange, request: Request,
                            current: CurrentUser = Depends(require_admin),
                            stepup: StepUp = Depends(step_up("admin.user.email"))) -> dict:
    """D11: correct a user's email (admin recovery). Revokes sessions, burns reset links, removes Google links."""
    grant = stepup.bind(f"user:{uid(user_id) or user_id}", {"email": body.email.strip().lower()})  # as stored
    return await admin_service(request).change_email(current.user, user_id, body.email, client_ip(request), grant)


@router.post("/users/{user_id}/sessions/revoke")
async def revoke_user_sessions(user_id: str, request: Request, current: CurrentUser = Depends(require_admin)) -> dict:
    return await admin_service(request).revoke_sessions(current.user, user_id)


@router.post("/creators", status_code=201)
async def create_creator(body: CreatorCreate, request: Request, current: CurrentUser = Depends(require_admin),
                         stepup: StepUp = Depends(step_up("creator.create"))) -> dict:
    svc = admin_service(request)
    grant = stepup.bind("creator:new", _creator_params(svc, body))
    return await svc.create_creator(current.user, body.model_dump(), grant)


@router.post("/creators/{streamer_id}/owner")
async def assign_creator_owner(streamer_id: str, body: OwnerAssign, request: Request,
                               current: CurrentUser = Depends(require_admin),
                               stepup: StepUp = Depends(step_up("creator.owner.assign"))) -> dict:
    grant = stepup.bind(f"creator:{uid(streamer_id) or streamer_id}", {"user_id": uid(body.user_id)})
    return await admin_service(request).assign_creator_owner(current.user, streamer_id, body.user_id, grant)


@router.post("/creators/{streamer_id}/owner/transfer")
async def transfer_creator_owner(streamer_id: str, body: OwnerTransfer, request: Request,
                                 current: CurrentUser = Depends(require_admin),
                                 stepup: StepUp = Depends(step_up("creator.owner.transfer"))) -> dict:
    """D13: move a claimed creator to another active user; the previous owner is unlinked and signed out."""
    grant = stepup.bind(f"creator:{uid(streamer_id) or streamer_id}",
                        {"user_id": uid(body.user_id), "expected_owner_id": uid(body.expected_owner_id)})
    return await admin_service(request).transfer_creator_owner(current.user, streamer_id, body.user_id,
                                                               body.expected_owner_id, client_ip(request), grant)


async def _status(action: str, streamer_id: str, body: CreatorStatusChange, request: Request, current: CurrentUser,
                  stepup: StepUp | None = None):
    grant = (stepup.bind(f"creator:{uid(streamer_id) or streamer_id}", {"expected_status": body.expected_status})
             if stepup is not None else None)
    return await admin_service(request).change_creator_status(current.user, streamer_id, action, body.expected_status,
                                                              body.reason, client_ip(request), grant)


@router.post("/creators/{streamer_id}/activate")
async def activate_creator(streamer_id: str, body: CreatorStatusChange, request: Request,
                           current: CurrentUser = Depends(require_admin),
                           stepup: StepUp = Depends(step_up("creator.activate"))) -> dict:
    """pending -> active. Never automatic: approval, invites and KYC alone do not activate a creator."""
    return await _status("activate", streamer_id, body, request, current, stepup)


@router.post("/creators/{streamer_id}/suspend")
async def suspend_creator(streamer_id: str, body: CreatorStatusChange, request: Request,
                          current: CurrentUser = Depends(require_admin)) -> dict:
    """active -> suspended: hidden like an unknown creator and refuses new donations; nothing else changes."""
    return await _status("suspend", streamer_id, body, request, current)


@router.post("/creators/{streamer_id}/reactivate")
async def reactivate_creator(streamer_id: str, body: CreatorStatusChange, request: Request,
                             current: CurrentUser = Depends(require_admin),
                             stepup: StepUp = Depends(step_up("creator.reactivate"))) -> dict:
    """suspended -> active, only if the creator could accept donations again (else 409 with the reason)."""
    return await _status("reactivate", streamer_id, body, request, current, stepup)


def application_service(request: Request):
    service = getattr(request.app.state, "applications", None)
    if service is None:
        raise AuthError(503, "Administration is not configured", "admin_not_configured")
    return service


@router.get("/applications")
async def list_applications(request: Request, status: str = "submitted", limit: int = Query(50, ge=1, le=200),
                            offset: int = Query(0, ge=0, le=100_000),
                            current: CurrentUser = Depends(require_admin)) -> dict:
    """Creator applications (the public signup form), newest first; status: submitted (default), legacy_unreviewed,
    approved, rejected or all."""
    return await application_service(request).search(status=status, limit=limit, offset=offset)


@router.get("/applications/{signup_id}")
async def get_application(signup_id: str, request: Request, current: CurrentUser = Depends(require_admin)) -> dict:
    """One application with its review history and latest invitation."""
    return await application_service(request).get(signup_id)


@router.post("/applications/{signup_id}/approve")
async def approve_application(signup_id: str, body: ApplicationApprove, request: Request,
                              current: CurrentUser = Depends(require_admin),
                              stepup: StepUp = Depends(step_up("application.approve"))) -> dict:
    """Approve and create the creator, PENDING (never active), in one transaction; a repeat approval is 409."""
    svc = application_service(request)
    grant = stepup.bind(f"application:{uid(signup_id) or signup_id}", _creator_params(svc.admin, body))
    return await svc.approve(current.user, signup_id, body.model_dump(), client_ip(request), grant)


@router.post("/applications/{signup_id}/reject")
async def reject_application(signup_id: str, body: ApplicationReject, request: Request,
                             current: CurrentUser = Depends(require_admin)) -> dict:
    return await application_service(request).reject(current.user, signup_id, body.reason, client_ip(request))


@router.post("/applications/{signup_id}/reopen")
async def reopen_application(signup_id: str, body: ApplicationNote, request: Request,
                             current: CurrentUser = Depends(require_admin)) -> dict:
    """rejected -> submitted, with an optional note."""
    return await application_service(request).reopen(current.user, signup_id, body.note, client_ip(request))


@router.post("/applications/{signup_id}/notes")
async def note_application(signup_id: str, body: ApplicationNote, request: Request,
                           current: CurrentUser = Depends(require_admin)) -> dict:
    """An internal review note (admins only; never shown to the applicant)."""
    return await application_service(request).note(current.user, signup_id, body.note, client_ip(request))


@router.post("/applications/{signup_id}/invite")
async def invite_application(signup_id: str, request: Request, current: CurrentUser = Depends(require_admin),
                             stepup: StepUp = Depends(step_up("application.invite"))) -> dict:
    """Send (or resend: the previous open link is revoked) the applicant's account invitation; accepting it makes
    them the owner of the application's still-unclaimed creator."""
    grant = stepup.bind(f"application:{uid(signup_id) or signup_id}", {})
    return await application_service(request).invite(current.user, signup_id, client_ip(request), grant)


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
