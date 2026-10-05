"""SQL for user / invite administration (runs as hyperchat_app; DML only)."""

from datetime import datetime

from psycopg import errors as pg_errors
from psycopg.rows import dict_row

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


class BootstrapRefused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class AdminRepository:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

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
