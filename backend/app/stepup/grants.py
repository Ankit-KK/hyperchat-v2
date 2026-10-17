"""Step-up grants as repositories consume them, and the step-up errors the API renders (no API imports: every layer's
repository uses this module)."""

from dataclasses import dataclass


class StepUpError(Exception):
    """Rendered as {"error": <message>, "reason": <code>, **extra}: unlike other API errors, the frontend must act on
    the reason (enroll, run a ceremony for this exact action, retry with a fresh challenge)."""

    def __init__(self, status: int, error: str, reason: str, extra: dict | None = None) -> None:
        super().__init__(reason)
        self.status, self.error, self.reason, self.extra = status, error, reason, extra or {}

    def body(self) -> dict:
        return {"error": self.error, "reason": self.reason, **self.extra}


class StepUpRequired(StepUpError):
    """This operation needs a passkey approval for exactly this action, target and params."""

    def __init__(self, action: str, target: str, params: dict) -> None:
        super().__init__(403, "Approve this action with your passkey", "step_up_required",
                         {"step_up": {"action": action, "target": target, "params": params}})


class StepUpRefused(StepUpError):
    """The presented grant does not authorize this operation (stepup_consume's reason, e.g. mismatch, consumed)."""

    def __init__(self, detail: str) -> None:
        super().__init__(403, "This passkey approval does not authorize this action", "step_up_refused",
                         {"detail": detail})
        self.detail = detail


class MfaRequired(StepUpError):
    def __init__(self) -> None:
        super().__init__(403, "Verify with your passkey to continue", "mfa_required")


@dataclass(frozen=True)
class Grant:
    grant_id: str
    user_id: str
    session_id: str
    action: str
    target: str
    params_hash: str


async def consume(cur, grant: Grant | None) -> None:
    """In the caller's transaction, before the operation's writes: spend the grant or refuse (rolling back)."""
    if grant is None:
        return
    await cur.execute("SELECT public.stepup_consume(%s, %s, %s, %s, %s, %s) AS r",
                      (grant.grant_id, grant.user_id, grant.session_id, grant.action, grant.target,
                       grant.params_hash))
    row = await cur.fetchone()
    result = row["r"] if isinstance(row, dict) else row[0]
    if result != "ok":
        raise StepUpRefused(result)
