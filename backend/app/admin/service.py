"""Admin use-cases. Every rule lives here; the router stays thin."""

import uuid

from app.admin.repository import AdminRepository, LastAdminError, UnknownStreamerError, iso
from app.auth.repository import User
from app.auth.service import AuthError
from app.security.audit import security_event

ROLES = ("streamer", "admin", "user")


def _uuid_or_404(value: str, what: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise AuthError(404, f"{what} not found", f"{what.lower()}_not_found") from None


def public_admin_user(row: dict) -> dict:
    return {**row, "last_login_at": iso(row["last_login_at"]), "created_at": iso(row["created_at"])}


def public_invite(row: dict) -> dict:
    return {**row, **{k: iso(row[k]) for k in ("created_at", "expires_at", "accepted_at", "revoked_at")}}


class AdminService:
    def __init__(self, repo: AdminRepository) -> None:
        self.repo = repo

    async def list_users(self, *, query: str | None, role: str | None, active: bool | None,
                         limit: int, offset: int) -> dict:
        if role is not None and role not in ROLES:
            raise AuthError(400, "Invalid role", "bad_role")
        rows, total = await self.repo.list_users(query=(query or "").strip() or None, role=role, active=active,
                                                 limit=limit, offset=offset)
        return {"total": total, "limit": limit, "offset": offset, "users": [public_admin_user(r) for r in rows]}

    async def update_user(self, admin: User, user_id: str, changes: dict) -> dict:
        user_id = _uuid_or_404(user_id, "User")
        if "role" in changes and changes["role"] not in ROLES:
            raise AuthError(400, "Invalid role", "bad_role")
        if "is_active" in changes and not isinstance(changes["is_active"], bool):
            raise AuthError(400, "is_active must be true or false", "bad_is_active")
        if changes.get("streamer_id") is not None:
            try:
                changes["streamer_id"] = str(uuid.UUID(changes["streamer_id"]))
            except ValueError:
                raise AuthError(400, "Unknown streamer", "bad_streamer_id") from None
        if user_id == admin.id and ("role" in changes or "is_active" in changes):
            raise AuthError(403, "You cannot change your own role or status", "self_change")
        try:
            row, changed, revoked = await self.repo.update_user(user_id, changes)
        except LastAdminError:
            raise AuthError(409, "At least one active admin must remain", "last_admin") from None
        except UnknownStreamerError:
            raise AuthError(400, "Unknown streamer", "bad_streamer_id") from None
        if row is None:
            raise AuthError(404, "User not found", "user_not_found")
        if changed:
            security_event("user_updated", level=20, user_id=admin.id, target_user_id=user_id,
                           reason=",".join(changed), count=revoked)
        return {"user": public_admin_user(row), "changed": changed, "sessions_revoked": revoked}

    async def revoke_sessions(self, admin: User, user_id: str) -> dict:
        user_id = _uuid_or_404(user_id, "User")
        if await self.repo.user(user_id) is None:
            raise AuthError(404, "User not found", "user_not_found")
        revoked = await self.repo.revoke_sessions(user_id)
        security_event("user_sessions_revoked", level=20, user_id=admin.id, target_user_id=user_id, count=revoked)
        return {"sessions_revoked": revoked}

    async def list_invites(self, *, status: str, limit: int) -> dict:
        if status not in ("pending", "all"):
            raise AuthError(400, "status must be 'pending' or 'all'", "bad_status")
        return {"invites": [public_invite(r) for r in await self.repo.list_invites(status=status, limit=limit)]}

    async def revoke_invite(self, admin: User, invite_id: str) -> dict:
        invite_id = _uuid_or_404(invite_id, "Invite")
        result = await self.repo.revoke_invite(invite_id)
        if result == "not_found":
            raise AuthError(404, "Invite not found", "invite_not_found")
        if result != "ok":
            raise AuthError(409, f"Invite is already {result}", f"invite_{result}")
        security_event("invite_revoked", level=20, user_id=admin.id, invite_id=invite_id)
        return {"success": True}
