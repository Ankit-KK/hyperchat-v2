"""Admin use-cases. Every rule lives here; the router stays thin."""

import re
import uuid

from app.admin.repository import AdminRepository, CreatorConflict, LastAdminError, UnknownStreamerError, iso
from app.auth.repository import User
from app.auth.service import AuthError
from app.security.audit import security_event

ROLES = ("streamer", "admin", "user")

# New creator slugs: lowercase letters, digits, "_" and "-", 3-64 characters, starting with a letter or digit (the
# platform-wide slug shape, at least 3 long). Reserved: the site's own top-level paths (legacy routes and the
# planned generic /:slug routing), so a creator page can never shadow one.
NEW_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}")
RESERVED_SLUGS = frozenset({
    "about", "account", "admin", "api", "app", "assets", "auth", "blog", "cancellation-refunds", "contact",
    "dashboard", "docs", "feature-showcase", "health", "help", "invite", "login", "logout", "me", "obs",
    "privacy-policy", "public", "register", "renew-hyperchat-brand-ambassador", "reset-password",
    "reset-password-request", "selected-as-hyperchat-brand-ambassador", "settings", "signup", "sitemap", "static",
    "status", "support", "terms", "terms-and-conditions", "www",
})
ORDER_PREFIX = re.compile(r"[a-z0-9]{1,10}_rp_")  # legacy shape ("sx_rp_"); our order ids stay <= 40 characters
CREATOR_NAME_MAX = 100
EMAIL = re.compile(r"[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+")  # one "@", no whitespace or control characters


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
    def __init__(self, repo: AdminRepository, *, payment_accounts: frozenset[str] = frozenset(),
                 pusher_groups: tuple[int, ...] = ()) -> None:
        self.repo = repo
        self.payment_accounts = payment_accounts  # Razorpay account names that can take payments here
        self.pusher_groups = pusher_groups        # configured Pusher groups

    # ── creators ───────────────────────────────────────────────────────────
    async def create_creator(self, admin: User, body: dict) -> dict:
        """Onboard a creator (legacy: a manual SQL insert, docs/ADD_NEW_STREAMER.md step 3). The new creator is
        unclaimed: only admins can manage it until an invite with its streamer_id is accepted (step 16). The
        Razorpay account is never defaulted (owner decision D12): the admin names it, and it must be configured."""
        slug = body["slug"].strip().lower()
        if not NEW_SLUG.fullmatch(slug):
            raise AuthError(400, "Slug must be 3-64 characters: a-z, 0-9, '_' or '-', starting with a letter or "
                                 "digit", "bad_slug")
        if slug in RESERVED_SLUGS:
            raise AuthError(400, "This slug is reserved", "reserved_slug")
        name = " ".join(body["name"].split())
        if not 1 <= len(name) <= CREATOR_NAME_MAX:
            raise AuthError(400, f"Name must be 1-{CREATOR_NAME_MAX} characters", "bad_name")
        account = body["razorpay_account"]
        if account not in self.payment_accounts:
            raise AuthError(400, "That Razorpay account is not configured on this server", "account_not_configured")
        fields = {"streamer_slug": slug, "streamer_name": name, "razorpay_account": account,
                  "custom_page_path": f"/{slug}"}
        if body.get("brand_color") is not None:
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", body["brand_color"]):
                raise AuthError(400, "brand_color must be #RRGGBB", "bad_brand_color")
            fields["brand_color"] = body["brand_color"].lower()
        if body.get("order_prefix") is not None:
            if not ORDER_PREFIX.fullmatch(body["order_prefix"]):
                raise AuthError(400, "order_prefix must look like 'abc_rp_' (1-10 of a-z/0-9, then _rp_)",
                                "bad_order_prefix")
            fields["order_prefix"] = body["order_prefix"]
        try:
            row = await self.repo.create_creator(fields, self.pusher_groups)
        except CreatorConflict as exc:
            raise AuthError(409, f"That {exc.field.replace('_', ' ')} is already used", f"{exc.field}_taken") from None
        security_event("creator_created", level=20, user_id=admin.id, streamer_id=row["id"], streamer_slug=slug,
                       account=account)
        return {"creator": {**row, "created_at": iso(row["created_at"])}}

    async def assign_creator_owner(self, admin: User, streamer_id: str, user_id: str) -> dict:
        """Hand an unclaimed creator to its owner (legacy step 16 set streamers.user_id by hand). Ownership is never
        transferred here: a claimed creator answers 409 (a transfer is an owner policy decision)."""
        streamer_id = _uuid_or_404(streamer_id, "Creator")
        try:
            user_id = str(uuid.UUID(user_id))
        except (ValueError, AttributeError, TypeError):
            raise AuthError(400, "Unknown or inactive user", "bad_user") from None
        result, row = await self.repo.assign_owner(streamer_id, user_id)
        if result == "not_found":
            raise AuthError(404, "Creator not found", "creator_not_found")
        if result == "claimed":
            raise AuthError(409, "This creator already has an owner", "creator_claimed")
        if result == "bad_user":
            raise AuthError(400, "Unknown or inactive user", "bad_user")
        security_event("creator_owner_assigned", level=20, user_id=admin.id, target_user_id=user_id,
                       streamer_id=streamer_id)
        return {"creator": {**row, "created_at": iso(row["created_at"])}}

    async def set_soundboard_eligibility(self, admin: User, streamer_id: str, enabled: bool) -> dict:
        """Grant / revoke Custom Soundboard eligibility (D3). Idempotent. Never accepts terms for the owner and never
        deletes acceptance history: usable access also needs the owner's acceptance of the current version."""
        streamer_id = _uuid_or_404(streamer_id, "Creator")
        previous = await self.repo.set_soundboard_eligibility(streamer_id, enabled)
        if previous is None:
            raise AuthError(404, "Creator not found", "creator_not_found")
        changed = previous != enabled
        if changed:
            security_event("soundboard_eligibility_changed", level=20, user_id=admin.id, streamer_id=streamer_id,
                           reason="granted" if enabled else "revoked")
        return {"creator_id": streamer_id, "soundboard_enabled": enabled, "changed": changed}

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

    async def change_email(self, admin: User, user_id: str, email: str, ip: str | None) -> dict:
        """D11: admin recovery / correction of a user's email (not self-service). Sends no email (D8)."""
        user_id = _uuid_or_404(user_id, "User")
        email = email.strip().lower()
        if len(email) > 254 or not EMAIL.fullmatch(email):
            raise AuthError(400, "A valid email is required", "bad_email")
        if user_id == admin.id:
            raise AuthError(403, "You cannot change your own email", "self_change")
        try:
            result, row, counts = await self.repo.change_email(user_id, email)
        except LastAdminError:
            raise AuthError(409, "At least one active admin must remain", "last_admin") from None
        if result == "not_found":
            raise AuthError(404, "User not found", "user_not_found")
        if result == "email_taken":
            raise AuthError(409, "Another account already uses this email", "email_taken")
        if result == "invite_pending":
            raise AuthError(409, "A pending invite exists for this email", "invite_pending")
        changed = result == "ok"
        if changed:  # ids and counts only, never either address (audit.py)
            security_event("user_email_changed", level=20, user_id=admin.id, target_user_id=user_id,
                           count=counts["sessions_revoked"], client_ip=ip)
        return {"user": public_admin_user(row), "changed": changed, "sessions_revoked": 0, "reset_tokens_burned": 0,
                "identities_removed": 0, **counts}

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
