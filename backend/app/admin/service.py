"""Admin use-cases. Every rule lives here; the router stays thin."""

import re
import uuid

from app.admin.repository import AdminRepository, CreatorConflict, LastAdminError, UnknownStreamerError, iso
from app.auth.repository import User
from app.auth.service import AuthError
from app.security.audit import security_event
from app.stepup.grants import Grant

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


def creator_conflict(exc: CreatorConflict) -> AuthError:
    return AuthError(409, f"That {exc.field.replace('_', ' ')} is already used", f"{exc.field}_taken")


class AdminService:
    def __init__(self, repo: AdminRepository, *, payment_accounts: frozenset[str] = frozenset(),
                 pusher_groups: tuple[int, ...] = ()) -> None:
        self.repo = repo
        self.payment_accounts = payment_accounts  # Razorpay account names that can take payments here
        self.pusher_groups = pusher_groups        # configured Pusher groups
        self.enforcement = None  # app.kyc.enforcement.EnforcementService (set by app.main): records grace revocations

    # ── creators ───────────────────────────────────────────────────────────
    async def create_creator(self, admin: User, body: dict, grant: Grant | None = None) -> dict:
        """Onboard a creator (legacy: a manual SQL insert, docs/ADD_NEW_STREAMER.md step 3). The new creator is
        unclaimed: only admins can manage it until an invite with its streamer_id is accepted (step 16). The
        Razorpay account is never defaulted (owner decision D12): the admin names it, and it must be configured."""
        fields = self.creator_fields(body)
        try:
            row = await self.repo.create_creator(fields, self.pusher_groups, grant)
        except CreatorConflict as exc:
            raise creator_conflict(exc) from None
        security_event("creator_created", level=20, user_id=admin.id, streamer_id=row["id"],
                       streamer_slug=row["slug"], account=row["razorpay_account"])
        return {"creator": {**row, "created_at": iso(row["created_at"])}}

    def creator_fields(self, body: dict) -> dict:
        """The validated columns of a new creator (also used by application approval)."""
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
        return fields

    async def assign_creator_owner(self, admin: User, streamer_id: str, user_id: str, grant: Grant | None = None) -> dict:
        """Hand an unclaimed creator to its owner (legacy step 16 set streamers.user_id by hand). Ownership is never
        transferred here: a claimed creator answers 409 (a transfer is an owner policy decision)."""
        streamer_id = _uuid_or_404(streamer_id, "Creator")
        try:
            user_id = str(uuid.UUID(user_id))
        except (ValueError, AttributeError, TypeError):
            raise AuthError(400, "Unknown or inactive user", "bad_user") from None
        result, row = await self.repo.assign_owner(streamer_id, user_id, grant)
        if result == "not_found":
            raise AuthError(404, "Creator not found", "creator_not_found")
        if result == "claimed":
            raise AuthError(409, "This creator already has an owner", "creator_claimed")
        if result == "bad_user":
            raise AuthError(400, "Unknown or inactive user", "bad_user")
        security_event("creator_owner_assigned", level=20, user_id=admin.id, target_user_id=user_id,
                       streamer_id=streamer_id)
        # A new owner is unverified: the creator stops accepting new donations until their KYC (owner decision).
        return {"creator": {**row, "created_at": iso(row["created_at"])},
                "donation_block": await self.repo.donation_block(streamer_id)}

    async def transfer_creator_owner(self, admin: User, streamer_id: str, user_id: str, expected_owner_id: str,
                                     ip: str | None, grant: Grant | None = None) -> dict:
        """D13: move a claimed creator to another active user (admin correction / recovery, never self-service). The
        caller names the owner they expect, so a stale or mistaken request changes nothing. OBS tokens are kept (the
        same person's live overlays keep working); the count is returned so a wrong-owner case can rotate them."""
        streamer_id = _uuid_or_404(streamer_id, "Creator")
        try:
            user_id, expected_owner_id = str(uuid.UUID(user_id)), str(uuid.UUID(expected_owner_id))
        except (ValueError, AttributeError, TypeError):
            raise AuthError(400, "user_id and expected_owner_id must be user ids", "bad_user") from None
        result, row, counts = await self.repo.transfer_owner(streamer_id, user_id, expected_owner_id, admin.id,
                                                             grant)
        if result == "not_found":
            raise AuthError(404, "Creator not found", "creator_not_found")
        if result == "unclaimed":
            raise AuthError(409, "This creator has no owner; use POST /api/v1/admin/creators/{id}/owner",
                            "creator_unclaimed")
        if result == "owner_changed":
            raise AuthError(409, "The creator's current owner is not expected_owner_id", "owner_changed")
        if result == "bad_user":
            raise AuthError(400, "Unknown or inactive user", "bad_user")
        changed = result == "ok"
        if changed and self.enforcement is not None:  # the old owner's grace was revoked: witness it
            await self.enforcement.sync_witness(action="owner_changed", streamer_id=streamer_id)
        if changed:  # ids and counts only
            security_event("creator_owner_transferred", level=20, user_id=admin.id, streamer_id=streamer_id,
                           previous_user_id=counts["previous_owner_id"], target_user_id=user_id,
                           count=counts["sessions_revoked"], client_ip=ip)
        return {"creator": {**row, "created_at": iso(row["created_at"])}, "changed": changed,
                "previous_owner_id": None, "sessions_revoked": 0, "old_owner_unlinked": False, "active_obs_tokens": None,
                **counts, "donation_block": await self.repo.donation_block(streamer_id)}

    async def change_creator_status(self, admin: User, streamer_id: str, action: str, expected_status: str,
                                    reason: str | None, ip: str | None, grant: Grant | None = None) -> dict:
        """Admin-only creator lifecycle (owner decision 2026-10-15): activate (pending -> active: claimed, a configured
        payment account and the owner's approved KYC), suspend (active -> suspended: hidden, no new donations; the
        dashboard, OBS tokens and history stay) and reactivate (suspended -> active: only if the creator could accept
        donations again). The reason text is kept in the audit table, never in logs."""
        streamer_id = _uuid_or_404(streamer_id, "Creator")
        reason = " ".join((reason or "").split()) or None
        if action == "suspend" and reason is None:
            raise AuthError(400, "A reason is required to suspend a creator", "reason_required")
        result, row = await self.repo.change_status(streamer_id, action, expected_status, admin.id, reason,
                                                    self.payment_accounts, grant)
        refusals = {
            "not_found": (404, "Creator not found"),
            "status_changed": (409, "The creator's status is not expected_status"),
            "bad_transition": (409, f"Cannot {action} a creator whose status is {expected_status}"),
            "unclaimed": (409, "The creator has no owner yet"),
            "account_not_configured": (409, "The creator's Razorpay account is not configured on this server"),
            "kyc_required": (409, "The creator's owner has no approved identity verification (KYC)"),
            "restricted": (409, "The creator has an open KYC restriction"),
            "no_owner": (409, "The creator's owner is missing or inactive"),
            "account_changed": (409, "The creator's Razorpay account changed; please retry"),
            "lifecycle_not_configured": (503, "Creator status changes are not configured on this server"),
            "not_authorized": (403, "Only an active admin can change a creator's status"),
        }
        if result != "ok":
            status, message = refusals.get(result, (409, "The creator could not accept donations"))
            raise AuthError(status, message, result if result in refusals else f"creator_{result}")
        if action == "suspend" and self.enforcement is not None:  # grace was revoked: witness it
            await self.enforcement.sync_witness(action="creator_suspended", streamer_id=streamer_id)
        security_event("creator_status_changed", level=20, user_id=admin.id, streamer_id=streamer_id,
                       reason=action, status=row["status"], client_ip=ip)
        return {"creator": {**row, "created_at": iso(row["created_at"])},
                "donation_block": await self.repo.donation_block(streamer_id)}

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

    async def update_user(self, admin: User, user_id: str, changes: dict, grant: Grant | None = None) -> dict:
        user_id = _uuid_or_404(user_id, "User")
        if "role" in changes and changes["role"] not in ROLES:
            raise AuthError(400, "Invalid role", "bad_role")
        if changes.get("role") == "admin":  # migration 0024: an existing account's credentials, identities or reset
            current = await self.repo.user(user_id)  # links may have been prepared by anyone with database access
            if current is not None and current["role"] != "admin":
                raise AuthError(409, "Existing accounts cannot be made admins. Send an admin invitation instead.",
                                "admin_by_invite_only")
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
            row, changed, revoked = await self.repo.update_user(user_id, changes, grant)
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

    async def change_email(self, admin: User, user_id: str, email: str, ip: str | None, grant: Grant | None = None) -> dict:
        """D11: admin recovery / correction of a user's email (not self-service). Sends no email (D8)."""
        user_id = _uuid_or_404(user_id, "User")
        email = email.strip().lower()
        if len(email) > 254 or not EMAIL.fullmatch(email):
            raise AuthError(400, "A valid email is required", "bad_email")
        if user_id == admin.id:
            raise AuthError(403, "You cannot change your own email", "self_change")
        try:
            result, row, counts = await self.repo.change_email(user_id, email, grant)
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
