"""SQL for user / invite / creator administration (runs as hyperchat_app; DML only)."""

from datetime import datetime

from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

_ADMIN_USER_COLS = """id::text, email::text, username, role, streamer_id::text, is_active,
    password_hash IS NOT NULL AS has_password, (locked_until IS NOT NULL AND locked_until > now()) AS locked,
    last_login_at, created_at"""

_INVITE_COLS = """id::text, email::text, role, streamer_id::text, invited_by::text, created_at, expires_at,
    accepted_at, revoked_at,
    CASE WHEN accepted_at IS NOT NULL THEN 'accepted' WHEN revoked_at IS NOT NULL THEN 'revoked'
         WHEN expires_at <= now() THEN 'expired' ELSE 'pending' END AS status"""

# Active admins who can actually sign in (password or linked Google identity). Used to never leave
# the platform without one.
_SIGN_IN_ADMINS = ("SELECT u.id::text FROM public.users u WHERE u.role = 'admin' AND u.is_active "
                   "AND (u.password_hash IS NOT NULL OR EXISTS "
                   "(SELECT 1 FROM public.user_identities i WHERE i.user_id = u.id)) FOR UPDATE OF u")


class LastAdminError(Exception):
    """The change would leave no active admin who can sign in."""


class UnknownStreamerError(Exception):
    """users.streamer_id must reference an existing creator."""


class CreatorConflict(Exception):
    """The slug or the order prefix is already used by another creator."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field  # "slug" | "order_prefix"


# The legacy launch defaults for a new creator (legacy docs/ADD_NEW_STREAMER.md, step 3 "Recommended: one insert
# with every launch default"). Written explicitly: some column defaults differ (e.g. telegram_moderation_enabled
# defaults to true in the schema, false at launch).
LAUNCH_DEFAULTS = {
    "status": "active", "moderation_mode": "auto_approve", "tts_enabled": True,
    "telegram_moderation_enabled": False, "discord_moderation_enabled": False, "leaderboard_widget_enabled": True,
    "hyperemotes_enabled": True, "media_upload_enabled": True, "media_moderation_enabled": False,
    "min_text_amount_inr": 40, "min_tts_amount_inr": 40, "min_voice_amount_inr": 150,
    "min_hypersound_amount_inr": 30, "media_min_amount": 100, "media_max_file_size_mb": 10,
    "message_char_tiers": [{"min_amount": 40, "max_chars": 200}, {"min_amount": 100, "max_chars": 300},
                           {"min_amount": 500, "max_chars": 500}],
    "goal_is_active": False,
}
_CREATOR_COLS = """id::text, streamer_slug AS slug, streamer_name AS name, status, razorpay_account, pusher_group,
    order_prefix, brand_color, moderation_mode, user_id IS NOT NULL AS claimed, created_at"""


class BootstrapRefused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class AdminRepository:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    # ── creators ───────────────────────────────────────────────────────────
    async def create_creator(self, fields: dict, pusher_groups: tuple[int, ...]) -> dict:
        """Insert a new, unclaimed creator with the legacy launch defaults, in one transaction. The Pusher group is
        the least-loaded configured group (legacy rule; ties -> the lowest), 1 when none is configured. Raises
        CreatorConflict when the slug or the order prefix is taken (the unique constraints decide, so two
        concurrent creations of one slug can never both succeed)."""
        row = {**LAUNCH_DEFAULTS, **fields}
        row["message_char_tiers"] = Jsonb(row["message_char_tiers"])
        try:
            async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                group = 1
                if pusher_groups:
                    await cur.execute("""
                        SELECT g FROM unnest(%s::int[]) AS g
                        ORDER BY (SELECT count(*) FROM public.streamers s WHERE s.pusher_group = g), g LIMIT 1
                    """, (list(pusher_groups),))
                    group = (await cur.fetchone())["g"]
                row["pusher_group"] = group
                cols = ", ".join(row)
                await cur.execute(f"INSERT INTO public.streamers ({cols}) VALUES "
                                  f"({', '.join(f'%({c})s' for c in row)}) RETURNING {_CREATOR_COLS}", row)
                return await cur.fetchone()
        except pg_errors.UniqueViolation as exc:
            name = exc.diag.constraint_name or ""
            if "order_prefix" in name:
                raise CreatorConflict("order_prefix") from None
            if "slug" in name:
                raise CreatorConflict("slug") from None
            raise

    async def assign_owner(self, streamer_id: str, user_id: str) -> tuple[str, dict | None]:
        """Make an existing, active user the owner of an UNCLAIMED creator (streamers.user_id, which the access
        rules require: an unclaimed creator is admin-only). Never replaces an owner. One transaction with the creator
        row locked, so two concurrent assignments cannot both win. Returns ("ok" | "not_found" | "claimed" |
        "bad_user", the creator row)."""
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT user_id FROM public.streamers WHERE id = %s FOR UPDATE", (streamer_id,))
            current = await cur.fetchone()
            if current is None:
                return "not_found", None
            if current["user_id"] is not None:
                await cur.execute(f"SELECT {_CREATOR_COLS} FROM public.streamers WHERE id = %s", (streamer_id,))
                return "claimed", await cur.fetchone()
            await cur.execute("SELECT 1 FROM public.users WHERE id = %s AND is_active", (user_id,))
            if await cur.fetchone() is None:
                return "bad_user", None
            await cur.execute(f"UPDATE public.streamers SET user_id = %s WHERE id = %s RETURNING {_CREATOR_COLS}",
                              (user_id, streamer_id))
            return "ok", await cur.fetchone()

    # ── users ──────────────────────────────────────────────────────────────
    async def list_users(self, *, query: str | None, role: str | None, active: bool | None,
                         limit: int, offset: int) -> tuple[list[dict], int]:
        where, params = ["true"], []
        if query:
            where.append("(email::text ILIKE %s OR username ILIKE %s)")
            params += [_like(query), _like(query)]
        if role:
            where.append("role = %s")
            params.append(role)
        if active is not None:
            where.append("is_active = %s")
            params.append(active)
        cond = " AND ".join(where)
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"SELECT count(*) AS n FROM public.users WHERE {cond}", params)
            total = (await cur.fetchone())["n"]
            await cur.execute(f"SELECT {_ADMIN_USER_COLS} FROM public.users WHERE {cond} "
                              "ORDER BY email LIMIT %s OFFSET %s", [*params, limit, offset])
            return await cur.fetchall(), total

    async def user(self, user_id: str) -> dict | None:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"SELECT {_ADMIN_USER_COLS} FROM public.users WHERE id = %s", (user_id,))
            return await cur.fetchone()

    async def update_user(self, user_id: str, changes: dict) -> tuple[dict | None, list[str], int]:
        """Apply role / is_active / streamer_id changes atomically.

        Returns (user, changed_fields, sessions_revoked). Any change revokes the user's sessions, so
        new privileges (or their loss) take effect immediately.
        """
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT u.id::text, u.role, u.is_active, u.streamer_id::text, "
                              "(u.password_hash IS NOT NULL OR EXISTS (SELECT 1 FROM public.user_identities i "
                              "WHERE i.user_id = u.id)) AS can_sign_in FROM public.users u WHERE u.id = %s "
                              "FOR UPDATE OF u", (user_id,))
            current = await cur.fetchone()
            if current is None:
                return None, [], 0
            changed = [k for k in ("role", "is_active", "streamer_id") if k in changes and changes[k] != current[k]]
            if not changed:
                return await self._fetch_user(cur, user_id), [], 0

            loses_admin = (current["role"] == "admin" and current["is_active"] and current["can_sign_in"]
                           and (changes.get("role", "admin") != "admin" or changes.get("is_active", True) is False))
            if loses_admin:
                await cur.execute(_SIGN_IN_ADMINS)
                if not [r for r in await cur.fetchall() if r["id"] != user_id]:
                    raise LastAdminError()

            sets = ", ".join(f"{k} = %({k})s" for k in changed)
            try:
                await cur.execute(f"UPDATE public.users SET {sets} WHERE id = %(id)s",
                                  {**{k: changes[k] for k in changed}, "id": user_id})
            except pg_errors.ForeignKeyViolation:
                raise UnknownStreamerError() from None
            revoked = await self._revoke_sessions(cur, user_id)
            return await self._fetch_user(cur, user_id), changed, revoked

    async def revoke_sessions(self, user_id: str) -> int:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            return await self._revoke_sessions(cur, user_id)

    @staticmethod
    async def _revoke_sessions(cur, user_id: str) -> int:
        await cur.execute("UPDATE public.sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                          (user_id,))
        return cur.rowcount

    @staticmethod
    async def _fetch_user(cur, user_id: str) -> dict:
        await cur.execute(f"SELECT {_ADMIN_USER_COLS} FROM public.users WHERE id = %s", (user_id,))
        return await cur.fetchone()

    # ── invites ────────────────────────────────────────────────────────────
    async def list_invites(self, *, status: str, limit: int) -> list[dict]:
        cond = ("accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now()" if status == "pending"
                else "true")
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"SELECT {_INVITE_COLS} FROM public.user_invites WHERE {cond} "
                              "ORDER BY created_at DESC LIMIT %s", (limit,))
            return await cur.fetchall()

    async def revoke_invite(self, invite_id: str) -> str:
        """'ok' when revoked now, 'not_found', or the invite's current status when it cannot be revoked."""
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("UPDATE public.user_invites SET revoked_at = now() WHERE id = %s "
                              "AND accepted_at IS NULL AND revoked_at IS NULL RETURNING id", (invite_id,))
            if await cur.fetchone():
                return "ok"
            await cur.execute(f"SELECT {_INVITE_COLS} FROM public.user_invites WHERE id = %s", (invite_id,))
            row = await cur.fetchone()
            return row["status"] if row else "not_found"

    # ── bootstrap ──────────────────────────────────────────────────────────
    async def bootstrap_admin(self, email: str, password_hash: str) -> str:
        """Create (or activate a password-less) admin account, only while no admin can sign in."""
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_SIGN_IN_ADMINS)
            if await cur.fetchall():
                raise BootstrapRefused("admin_exists")
            await cur.execute("""
                INSERT INTO public.users (email, password_hash, role, is_active, email_verified_at, password_changed_at)
                VALUES (%s, %s, 'admin', true, now(), now())
                ON CONFLICT (email) DO UPDATE SET
                    password_hash = EXCLUDED.password_hash, role = 'admin', is_active = true,
                    failed_login_attempts = 0, locked_until = NULL, password_changed_at = now(),
                    email_verified_at = COALESCE(public.users.email_verified_at, now())
                WHERE public.users.password_hash IS NULL
                RETURNING id::text
            """, (email, password_hash))
            row = await cur.fetchone()
            if row is None:
                raise BootstrapRefused("account_has_password")
            await self._revoke_sessions(cur, row["id"])
            return row["id"]


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
