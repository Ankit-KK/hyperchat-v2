"""Action-bound step-up authorization in the API (S2, docs/admin-stepup.md "S2: API integration").

A sensitive admin route names its canonical action (ROUTES below) and computes its target and EXACT parameters from
the path and the validated body. The administrator and session come only from the authenticated request
(CurrentUser), never from the client. The browser obtains a grant for precisely that (action, target, params) through
/api/v1/auth/mfa/assertion/* and sends it back in the X-Step-Up-Grant header; the repository then consumes it with
public.stepup_consume() INSIDE the transaction that performs the operation, on the database role that performs it.
If the grant is refused the transaction rolls back; if the operation itself is refused (a stale compare-and-set, a
claimed creator), the consumption rolls back with it, so a grant is spent only by the operation it approved.

Enforcement (stepup_enforcement):
  * 'draft' (now): a route without a grant still works (existing admins are not locked out before enrolling); a
    route WITH a grant consumes it and fails closed when it is invalid.
  * 'active' (after S3 is approved and the operator activates it): a sensitive route without a grant answers
    403 step_up_required with the exact action, target and params to approve.
The database guards that make a grant mandatory for these writes whatever the caller (stepup_claim / stepup_effect
on each anchor write) are S3. Until then this is API-level authorization only.
"""

import uuid
from dataclasses import dataclass

from fastapi import Depends, Request

from app.auth.dependencies import CurrentUser, require_admin
from app.stepup.canonical import ACTIONS, CanonicalError, canonical_json, sha256_hex
from app.stepup.grants import Grant, StepUpRefused, StepUpRequired

GRANT_HEADER = "x-step-up-grant"

# Route -> canonical action. Every action of app.stepup.canonical with purpose "action" is either bound here or
# listed in UNBOUND with the reason (tests/stepup/test_actions.py keeps the two in step).
ROUTES = {
    ("POST", "/api/v1/admin/creators/{streamer_id}/activate"): "creator.activate",
    ("POST", "/api/v1/admin/creators/{streamer_id}/reactivate"): "creator.reactivate",
    ("POST", "/api/v1/admin/creators"): "creator.create",
    ("POST", "/api/v1/admin/creators/{streamer_id}/owner"): "creator.owner.assign",
    ("POST", "/api/v1/admin/creators/{streamer_id}/owner/transfer"): "creator.owner.transfer",
    ("POST", "/api/v1/admin/applications/{signup_id}/approve"): "application.approve",
    ("POST", "/api/v1/admin/applications/{signup_id}/invite"): "application.invite",
    ("POST", "/api/v1/auth/invites"): "admin.invite",  # only when role = admin; other invites are ordinary
    ("PATCH", "/api/v1/admin/users/{user_id}"): "admin.user.change",
    ("PUT", "/api/v1/admin/users/{user_id}/email"): "admin.user.email",
    ("POST", "/api/v1/admin/partner-keys"): "partner_key.create",
}
UNBOUND = {"mfa.credential.revoke": "passkeys are revoked by the operator only (S1); no API route revokes one yet"}


@dataclass(frozen=True)
class StepUp:
    """The step-up context of one sensitive request: who (from the session), which action, the presented grant."""
    action: str
    user_id: str
    session_id: str
    presented: str | None
    enforcement: str

    def bind(self, target: str, params: dict) -> Grant | None:
        """The grant to consume with this operation, or None (draft mode, no grant presented)."""
        if self.presented is None:
            if self.enforcement == "active":
                raise StepUpRequired(self.action, target, params)
            return None
        try:
            grant_id = str(uuid.UUID(self.presented))
            params_hash = sha256_hex(canonical_json(params))
        except (ValueError, CanonicalError):
            raise StepUpRefused("malformed") from None
        return Grant(grant_id, self.user_id, self.session_id, self.action, target, params_hash)


def step_up(action: str):
    """FastAPI dependency for a sensitive route: require_admin, then the step-up context for `action`."""
    if ACTIONS.get(action) != "action":
        raise ValueError(f"not an action: {action}")

    async def dependency(request: Request, current: CurrentUser = Depends(require_admin)) -> StepUp:
        state = await request.app.state.auth.repo.stepup_enforcement()
        return StepUp(action, current.user.id, current.session.id, request.headers.get(GRANT_HEADER), state)
    return dependency
