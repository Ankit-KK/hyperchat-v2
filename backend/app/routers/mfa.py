"""/api/v1/auth/mfa: administrator passkeys (S2; docs/admin-stepup.md).

Every endpoint needs an admin session (cookie); every POST also needs an allowlisted Origin and the session's CSRF
token (require_user). The administrator and the session are ALWAYS the authenticated ones: bodies refuse any other
field (extra="forbid"), so no user or session id can be supplied. The isolated verifier does the WebAuthn work over
its Unix socket (never exposed through nginx); this API only forwards and rate-limits.

  GET  /status                  enforcement state, this session's assurance (aal), this admin's passkeys
  GET  /credentials             this admin's passkeys: fingerprint, status (pending / active / revoked), dates
  POST /registration/options    WebAuthn creation options for a new passkey (canonical action mfa.register)
  POST /registration/verify     store the new passkey PENDING; the operator approves it after checking the
                                fingerprint out of band (python -m app.stepup.operator approve)
  POST /assertion/options       a ceremony for {"action": "session.aal2", "target": "session", "params": {}} or for one
                                sensitive action with its exact target and params (app.stepup.actions.ROUTES)
  POST /assertion/verify        session.aal2: this session's assurance (aal2); an action: a one-time grant for exactly
                                that action, to send as X-Step-Up-Grant with the protected request (2 minutes)
"""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.dependencies import CurrentUser, client_ip, require_admin_enrolling
from app.stepup.grants import StepUpError
from app.stepup.service import MfaService

router = APIRouter(prefix="/api/v1/auth/mfa", tags=["auth"])


class NoBody(BaseModel):
    """Registration options take nothing from the client: any field (a user or session id) is refused (422)."""
    model_config = ConfigDict(extra="forbid")


class VerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: str = Field(min_length=36, max_length=36)
    credential: dict


class AssertionOptionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(max_length=64)
    target: str = Field(max_length=200)
    params: dict = Field(default_factory=dict)


def mfa_service(request: Request) -> MfaService:
    service = getattr(request.app.state, "mfa", None)
    if service is None:
        raise StepUpError(503, "Passkey verification is not configured", "stepup_not_configured")
    return service


@router.get("/status")
async def status(request: Request, current: CurrentUser = Depends(require_admin_enrolling)) -> dict:
    return await mfa_service(request).status(current)


@router.get("/credentials")
async def credentials(request: Request, current: CurrentUser = Depends(require_admin_enrolling)) -> dict:
    return await mfa_service(request).credentials(current, client_ip(request))


@router.post("/registration/options")
async def registration_options(request: Request, body: NoBody | None = None,
                               current: CurrentUser = Depends(require_admin_enrolling)) -> dict:
    return await mfa_service(request).registration_options(current, client_ip(request))


@router.post("/registration/verify")
async def registration_verify(body: VerifyIn, request: Request,
                              current: CurrentUser = Depends(require_admin_enrolling)) -> dict:
    return await mfa_service(request).registration_verify(current, client_ip(request), body.challenge_id,
                                                         body.credential)


@router.post("/assertion/options")
async def assertion_options(body: AssertionOptionsIn, request: Request,
                            current: CurrentUser = Depends(require_admin_enrolling)) -> dict:
    return await mfa_service(request).assertion_options(current, client_ip(request), body.action, body.target,
                                                       body.params)


@router.post("/assertion/verify")
async def assertion_verify(body: VerifyIn, request: Request,
                           current: CurrentUser = Depends(require_admin_enrolling)) -> dict:
    return await mfa_service(request).assertion_verify(current, client_ip(request), body.challenge_id,
                                                      body.credential)
