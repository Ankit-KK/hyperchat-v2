"""Creator applications (KYC PR2; owner-approved manual creator approval, 2026-10-15).

The public signup interest form (POST /api/v1/public/signups, unchanged) creates an application; admins review it:

  submitted | legacy_unreviewed --approve--> approved (a PENDING creator is created in the same transaction)
                                --reject---> rejected --reopen--> submitted
  approved --invite--> an account invitation bound to the application, its creator and the applicant's email

Approval never activates anything: the creator stays pending (migration 0020) until an admin activates it after the
owner's KYC (later PRs). Accepting an application invite makes the invitee the owner of that creator only while it
is still unclaimed (app.auth.repository.accept_invite). An applicant who already has an account is never invited
over it: the admin links the existing account explicitly (POST /api/v1/admin/creators/{id}/owner). Every decision
and note is kept in application_events (append-only); no token is ever stored or logged.
"""

import uuid
from datetime import datetime

from psycopg.rows import dict_row

from app.admin.repository import CreatorConflict, insert_creator
from app.admin.service import AdminService, creator_conflict
from app.auth.email import render
from app.auth.repository import User
from app.auth.service import AuthError, AuthService
from app.auth.tokens import new_hex_token, sha256_hex
from app.security.audit import security_event
from app.stepup.grants import Grant, consume

STATUSES = ("legacy_unreviewed", "submitted", "approved", "rejected")
REVIEWABLE = ("legacy_unreviewed", "submitted")
_COLS = """a.id::text, a.name, a.email, a.mobile_number, a.youtube_channel, a.instagram_handle, a.created_at,
    a.review_status, a.reviewed_by::text, a.reviewed_at, a.rejection_reason, a.streamer_id::text, a.invite_id::text"""


class _Stop(Exception):
    """Rolls the transaction back with a result code."""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def public_application(row: dict) -> dict:
    return {**row, "created_at": _iso(row["created_at"]), "reviewed_at": _iso(row["reviewed_at"])}


class ApplicationRepository:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def search(self, status: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
        cond, params = ("a.review_status = %s", [status]) if status else ("true", [])
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"SELECT count(*) AS n FROM public.user_signups a WHERE {cond}", params)
            total = (await cur.fetchone())["n"]
            await cur.execute(f"SELECT {_COLS} FROM public.user_signups a WHERE {cond} "
                              "ORDER BY a.created_at DESC, a.id LIMIT %s OFFSET %s", [*params, limit, offset])
            return await cur.fetchall(), total

    async def get(self, signup_id: str) -> tuple[dict | None, list[dict], dict | None]:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"SELECT {_COLS} FROM public.user_signups a WHERE a.id = %s", (signup_id,))
            row = await cur.fetchone()
            if row is None:
                return None, [], None
            await cur.execute("SELECT action, actor_user_id::text, note, streamer_id::text, invite_id::text, "
                              "created_at FROM public.application_events WHERE signup_id = %s "
                              "ORDER BY created_at, id", (signup_id,))
            events = await cur.fetchall()
            invite = None
            if row["invite_id"]:
                await cur.execute("SELECT id::text, expires_at, accepted_at, revoked_at, accepted_user_id::text "
                                  "FROM public.user_invites WHERE id = %s", (row["invite_id"],))
                invite = await cur.fetchone()
            return row, events, invite

    async def _locked(self, cur, signup_id: str) -> dict:
        await cur.execute(f"SELECT {_COLS} FROM public.user_signups a WHERE a.id = %s FOR UPDATE", (signup_id,))
        row = await cur.fetchone()
        if row is None:
            raise _Stop("not_found")
        return row

    @staticmethod
    async def _event(cur, signup_id: str, action: str, actor_id: str, note: str | None = None,
                     streamer_id: str | None = None, invite_id: str | None = None) -> None:
        await cur.execute("INSERT INTO public.application_events (signup_id, action, actor_user_id, note, "
                          "streamer_id, invite_id) VALUES (%s, %s, %s, %s, %s, %s)",
                          (signup_id, action, actor_id, note, streamer_id, invite_id))

    async def _run(self, work, grant: Grant | None = None) -> tuple[str, dict | None]:
        """One transaction; the application row is locked first, so concurrent decisions on it are serialized. A
        step-up grant (S2) is consumed first in the same transaction; a refusal (_Stop) rolls it back unspent."""
        try:
            async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                await consume(cur, grant)
                return "ok", await work(cur)
        except _Stop as stop:
            return str(stop), None

    async def approve(self, signup_id: str, actor_id: str, fields: dict, pusher_groups: tuple[int, ...],
                      grant: Grant | None = None):
        async def work(cur):
            app = await self._locked(cur, signup_id)
            if app["review_status"] == "approved":
                raise _Stop("already_approved")
            if app["review_status"] not in REVIEWABLE:
                raise _Stop("not_reviewable")
            creator = await insert_creator(cur, fields, pusher_groups)  # pending, standard track (migration 0020)
            await cur.execute("UPDATE public.user_signups SET review_status = 'approved', reviewed_by = %s, "
                              "reviewed_at = now(), streamer_id = %s WHERE id = %s",
                              (actor_id, creator["id"], signup_id))
            await self._event(cur, signup_id, "approve", actor_id, streamer_id=creator["id"])
            return creator
        return await self._run(work, grant)

    async def reject(self, signup_id: str, actor_id: str, reason: str):
        async def work(cur):
            app = await self._locked(cur, signup_id)
            if app["review_status"] not in REVIEWABLE:
                raise _Stop("not_reviewable")
            await cur.execute("UPDATE public.user_signups SET review_status = 'rejected', rejection_reason = %s, "
                              "reviewed_by = %s, reviewed_at = now() WHERE id = %s", (reason, actor_id, signup_id))
            await self._event(cur, signup_id, "reject", actor_id, note=reason)
            return None
        return await self._run(work)

    async def reopen(self, signup_id: str, actor_id: str, note: str | None):
        async def work(cur):
            app = await self._locked(cur, signup_id)
            if app["review_status"] != "rejected":
                raise _Stop("not_rejected")
            await cur.execute("UPDATE public.user_signups SET review_status = 'submitted', rejection_reason = NULL, "
                              "reviewed_by = %s, reviewed_at = now() WHERE id = %s", (actor_id, signup_id))
            await self._event(cur, signup_id, "reopen", actor_id, note=note)
            return None
        return await self._run(work)

    async def note(self, signup_id: str, actor_id: str, note: str):
        async def work(cur):
            await self._locked(cur, signup_id)
            await self._event(cur, signup_id, "note", actor_id, note=note)
            return None
        return await self._run(work)

    async def issue_invite(self, signup_id: str, actor_id: str, token_hash: str, ttl_days: int,
                           grant: Grant | None = None):
        """Revoke this application's open invitations and issue a new one (a retry never leaves two usable links).
        Lock order: application -> its invites -> its creator, matching accept_invite (invite -> creator -> users),
        so a retry racing an acceptance waits instead of deadlocking (and then sees the creator claimed)."""
        async def work(cur):
            app = await self._locked(cur, signup_id)
            if app["review_status"] != "approved":
                raise _Stop("not_approved")
            await cur.execute("UPDATE public.user_invites SET revoked_at = now() WHERE signup_id = %s "
                              "AND accepted_at IS NULL AND revoked_at IS NULL", (signup_id,))
            replaced = cur.rowcount
            await cur.execute("SELECT user_id FROM public.streamers WHERE id = %s FOR NO KEY UPDATE",
                              (app["streamer_id"],))
            if (await cur.fetchone())["user_id"] is not None:
                raise _Stop("creator_claimed")
            await cur.execute("SELECT 1 FROM public.users WHERE email = %s", (app["email"],))
            if await cur.fetchone() is not None:
                raise _Stop("account_exists")
            await cur.execute("""
                INSERT INTO public.user_invites (email, role, streamer_id, token_hash, invited_by, expires_at, signup_id)
                VALUES (%s, 'streamer', %s, %s, %s, now() + make_interval(days => %s), %s)
                RETURNING id::text, expires_at
            """, (app["email"], app["streamer_id"], token_hash, actor_id, ttl_days, signup_id))
            invite = await cur.fetchone()
            await cur.execute("UPDATE public.user_signups SET invite_id = %s WHERE id = %s", (invite["id"], signup_id))
            await self._event(cur, signup_id, "invite", actor_id, streamer_id=app["streamer_id"],
                              invite_id=invite["id"])
            return {**invite, "email": app["email"], "replaced": replaced}
        return await self._run(work, grant)


_REFUSALS = {
    "not_found": (404, "Application not found"),
    "already_approved": (409, "This application is already approved"),
    "not_reviewable": (409, "Only a submitted or unreviewed application can be approved or rejected"),
    "not_rejected": (409, "Only a rejected application can be reopened"),
    "not_approved": (409, "Only an approved application can be invited"),
    "creator_claimed": (409, "The application's creator already has an owner"),
    "account_exists": (409, "An account with this email already exists: link it to the creator with "
                            "POST /api/v1/admin/creators/{id}/owner"),
}


def _text(value: str | None, field: str, limit: int, required: bool) -> str | None:
    value = " ".join((value or "").split()) or None
    if required and value is None:
        raise AuthError(400, f"{field} is required", f"{field}_required")
    if value is not None and len(value) > limit:
        raise AuthError(400, f"{field} must be at most {limit} characters", f"bad_{field}")
    return value


class ApplicationService:
    def __init__(self, repo: ApplicationRepository, *, admin: AdminService, auth: AuthService) -> None:
        self.repo, self.admin, self.auth = repo, admin, auth

    @staticmethod
    def _id(signup_id: str) -> str:
        try:
            return str(uuid.UUID(signup_id))
        except (ValueError, AttributeError, TypeError):
            raise AuthError(404, "Application not found", "not_found") from None

    @staticmethod
    def _check(result: str) -> None:
        if result != "ok":
            status, message = _REFUSALS[result]
            raise AuthError(status, message, result)

    async def search(self, *, status: str, limit: int, offset: int) -> dict:
        if status != "all" and status not in STATUSES:
            raise AuthError(400, "status must be one of: all, " + ", ".join(STATUSES), "bad_status")
        rows, total = await self.repo.search(None if status == "all" else status, limit, offset)
        return {"applications": [public_application(r) for r in rows], "total": total}

    async def get(self, signup_id: str) -> dict:
        row, events, invite = await self.repo.get(self._id(signup_id))
        if row is None:
            raise AuthError(404, "Application not found", "not_found")
        return {"application": public_application(row),
                "events": [{**e, "created_at": _iso(e["created_at"])} for e in events],
                "invite": {**invite, **{k: _iso(invite[k]) for k in ("expires_at", "accepted_at", "revoked_at")}}
                if invite else None}

    def _audit(self, admin: User, action: str, ip: str | None, **extra) -> None:
        # Ids and codes only: never the applicant's name, email, phone or a note.
        security_event("application_reviewed", level=20, user_id=admin.id, reason=action, client_ip=ip, **extra)

    async def approve(self, admin: User, signup_id: str, body: dict, ip: str | None, grant: Grant | None = None) -> dict:
        signup_id = self._id(signup_id)
        fields = self.admin.creator_fields(body)
        try:
            result, creator = await self.repo.approve(signup_id, admin.id, fields, self.admin.pusher_groups,
                                                      grant)
        except CreatorConflict as exc:
            raise creator_conflict(exc) from None
        self._check(result)
        self._audit(admin, "approve", ip, streamer_id=creator["id"])
        return {"creator": {**creator, "created_at": _iso(creator["created_at"])}, **await self.get(signup_id)}

    async def reject(self, admin: User, signup_id: str, reason: str | None, ip: str | None) -> dict:
        signup_id = self._id(signup_id)
        self._check((await self.repo.reject(signup_id, admin.id, _text(reason, "reason", 500, True)))[0])
        self._audit(admin, "reject", ip)
        return await self.get(signup_id)

    async def reopen(self, admin: User, signup_id: str, note: str | None, ip: str | None) -> dict:
        signup_id = self._id(signup_id)
        self._check((await self.repo.reopen(signup_id, admin.id, _text(note, "note", 2000, False)))[0])
        self._audit(admin, "reopen", ip)
        return await self.get(signup_id)

    async def note(self, admin: User, signup_id: str, note: str | None, ip: str | None) -> dict:
        signup_id = self._id(signup_id)
        self._check((await self.repo.note(signup_id, admin.id, _text(note, "note", 2000, True)))[0])
        self._audit(admin, "note", ip)
        return await self.get(signup_id)

    async def invite(self, admin: User, signup_id: str, ip: str | None, grant: Grant | None = None) -> dict:
        """Create (or retry: the previous open link is revoked) the account invitation of an approved application,
        then email it. A failed email leaves the new invitation in place and answers 502: retrying issues a fresh
        link and revokes this one; the creator is never created twice."""
        signup_id = self._id(signup_id)
        token = new_hex_token(32)
        result, invite = await self.repo.issue_invite(signup_id, admin.id, sha256_hex(token),
                                                      self.auth.settings.invite_ttl_days, grant)
        self._check(result)
        url = f"{self.auth.settings.frontend_url.rstrip('/')}/invite?token={token}"
        try:
            await self.auth.sender.send(render("invite", invite["email"], url))
        except Exception as exc:  # noqa: BLE001 - any provider failure: report the class only, never the link
            security_event("invite_delivery_failed", reason=type(exc).__name__, user_id=admin.id,
                           invite_id=invite["id"], client_ip=ip)
            raise AuthError(502, "The invitation was created but the email could not be sent; retry to send a new "
                                 "link", "invite_delivery_failed") from None
        self._audit(admin, "invite", ip, invite_id=invite["id"], count=invite["replaced"])
        result = {"invite_id": invite["id"], "expires_at": _iso(invite["expires_at"]),
                  "replaced_invites": invite["replaced"]}
        if self.auth.settings.auth_return_invite_links:  # development only (refused in production, F6)
            result["accept_url"] = url
        return result
