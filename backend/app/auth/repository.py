"""SQL for authentication (runs as hyperchat_app; DML only)."""

from dataclasses import dataclass
from datetime import datetime

from psycopg.rows import dict_row


@dataclass(frozen=True)
class User:
    id: str
    email: str
    username: str | None
    role: str
    streamer_id: str | None
    is_active: bool
    password_hash: str | None
    failed_login_attempts: int
    locked_until: datetime | None


@dataclass(frozen=True)
class Session:
    id: str
    user_id: str
    idle_expires_at: datetime
    absolute_expires_at: datetime


_USER_COLS = ("id::text, email::text, username, role, streamer_id::text, is_active, password_hash, "
              "failed_login_attempts, locked_until")


async def burn_reset_tokens(cur, user_id: str) -> None:
    """Invalidate every open password-reset link of the user (inside the caller's transaction)."""
    await cur.execute("UPDATE public.password_reset_tokens SET used_at = now() WHERE user_id = %s AND used_at IS NULL",
                      (user_id,))


async def revoke_sessions(cur, user_id: str, except_session_id: str | None = None) -> int:
    """Revoke the user's open sessions (optionally keeping one), inside the caller's transaction."""
    await cur.execute("""
        UPDATE public.sessions SET revoked_at = now()
        WHERE user_id = %s AND revoked_at IS NULL AND (%s::uuid IS NULL OR id <> %s::uuid)
    """, (user_id, except_session_id, except_session_id))
    return cur.rowcount


_INSERT_SESSION = """
    INSERT INTO public.sessions (id, user_id, token_hash, csrf_token_hash, ua_hash, ip_address,
                                 idle_expires_at, absolute_expires_at)
    VALUES (%s, %s, %s, %s, %s, %s::inet, now() + make_interval(days => %s), now() + make_interval(days => %s))
    RETURNING id::text, user_id::text, idle_expires_at, absolute_expires_at
"""


class AuthRepository:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def _one(self, sql: str, params: tuple) -> dict | None:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()

    async def _exec(self, sql: str, params: tuple) -> int:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            return cur.rowcount

    # ── users ──────────────────────────────────────────────────────────────
    async def user_by_email(self, email: str) -> User | None:
        row = await self._one(f"SELECT {_USER_COLS} FROM public.users WHERE email = %s", (email,))
        return User(**row) if row else None

    async def user_by_id(self, user_id: str) -> User | None:
        row = await self._one(f"SELECT {_USER_COLS} FROM public.users WHERE id = %s", (user_id,))
        return User(**row) if row else None

    async def record_successful_login(self, user_id: str) -> None:
        """Google sign-in (no password involved). Password sign-in uses password_sign_in."""
        await self._exec("""
            UPDATE public.users SET failed_login_attempts = 0, locked_until = NULL, last_login_at = now()
            WHERE id = %s
        """, (user_id,))

    async def password_sign_in(self, user_id: str, verified_hash: str, new_hash: str | None,
                               session: tuple) -> Session | None:
        """Finish a password sign-in in ONE transaction (security audit F10): lock the user row, require that the
        account is still active and its stored hash is still the one the password was verified against, record the
        sign-in (with the optional rehash) and insert the session. A reset or change that commits first makes this
        return None (no session); one that commits after it revokes the session in its own transaction, so a session
        authenticated against an old password can never outlive a completed reset or change. `session` holds
        _INSERT_SESSION's parameters; Argon2 runs before the call."""
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT password_hash, is_active FROM public.users WHERE id = %s FOR UPDATE", (user_id,))
            row = await cur.fetchone()
            if row is None or not row["is_active"] or row["password_hash"] != verified_hash:
                return None
            await cur.execute("""
                UPDATE public.users SET failed_login_attempts = 0, locked_until = NULL, last_login_at = now(),
                       password_hash = COALESCE(%s, password_hash)
                WHERE id = %s
            """, (new_hash, user_id))
            await cur.execute(_INSERT_SESSION, session)
            return Session(**await cur.fetchone())

    # Lock order for every path that sets a password, changes is_active or issues a password session:
    # users row -> password_reset_tokens -> sessions (change_password, reset_password, password_sign_in, accept_invite,
    # AdminRepository.bootstrap_admin / update_user), so two of them can never deadlock (security audit F10).

    async def change_password(self, user_id: str, verified_hash: str, new_hash: str,
                              keep_session_id: str) -> int | None:
        """Set a new password only if the stored hash is still the one the current password was verified against
        (compare-and-set): a reset or another change that finished in the meantime wins, and this one is refused
        (None). In the same transaction: burn the user's open reset links and revoke every other session (the
        current one is kept). Returns the number of sessions revoked (security audit F10)."""
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute("""
                UPDATE public.users SET password_hash = %s, password_changed_at = now(),
                       failed_login_attempts = 0, locked_until = NULL
                WHERE id = %s AND password_hash = %s
            """, (new_hash, user_id, verified_hash))
            if cur.rowcount != 1:
                return None
            await burn_reset_tokens(cur, user_id)
            return await revoke_sessions(cur, user_id, except_session_id=keep_session_id)

    # ── sessions ───────────────────────────────────────────────────────────
    async def create_session(self, session_id: str, user_id: str, token_hash: str, csrf_hash: str,
                             ua_hash: str | None, ip: str | None, idle_days: int, absolute_days: int) -> Session:
        row = await self._one(_INSERT_SESSION,
                              (session_id, user_id, token_hash, csrf_hash, ua_hash, ip, idle_days, absolute_days))
        return Session(**row)

    async def active_session(self, token_hash: str, idle_days: int) -> Session | None:
        """Valid, unexpired, unrevoked session; slides the idle expiry (never past the absolute expiry)."""
        row = await self._one("""
            UPDATE public.sessions SET last_seen_at = now(),
                   idle_expires_at = LEAST(now() + make_interval(days => %s), absolute_expires_at)
            WHERE token_hash = %s AND revoked_at IS NULL
              AND idle_expires_at > now() AND absolute_expires_at > now()
            RETURNING id::text, user_id::text, idle_expires_at, absolute_expires_at
        """, (idle_days, token_hash))
        return Session(**row) if row else None

    async def revoke_session(self, session_id: str) -> None:
        await self._exec("UPDATE public.sessions SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
                         (session_id,))

    async def revoke_all_sessions(self, user_id: str, except_session_id: str | None = None) -> int:
        return await self._exec("""
            UPDATE public.sessions SET revoked_at = now()
            WHERE user_id = %s AND revoked_at IS NULL AND (%s::uuid IS NULL OR id <> %s::uuid)
        """, (user_id, except_session_id, except_session_id))

    # ── password reset ─────────────────────────────────────────────────────
    async def create_reset_token(self, user_id: str, token_hash: str, ttl_minutes: int) -> None:
        await self._exec("""
            INSERT INTO public.password_reset_tokens (user_id, token_hash, expires_at)
            VALUES (%s, %s, now() + make_interval(mins => %s))
        """, (user_id, token_hash, ttl_minutes))

    async def reset_password(self, token_hash: str, new_hash: str) -> str | None:
        """Redeem a reset link and set the password in ONE transaction (security audit F10): lock the user row,
        require an active account and a valid (unused, unexpired) link, set the password, burn every open link.
        A change or reset that committed first has burned the link, so this one is refused and never overwrites it.
        Revokes every session in the same transaction. The caller hashes before calling, so no row lock is held while
        Argon2 runs. Returns (user id, sessions revoked) or None."""
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute("SELECT user_id::text FROM public.password_reset_tokens WHERE token_hash = %s",
                              (token_hash,))
            found = await cur.fetchone()
            if found is None:
                return None
            user_id = found[0]
            await cur.execute("SELECT is_active FROM public.users WHERE id = %s FOR UPDATE", (user_id,))  # users first
            user = await cur.fetchone()
            if user is None or not user[0]:
                return None
            await cur.execute("""
                UPDATE public.password_reset_tokens SET used_at = now()
                WHERE token_hash = %s AND user_id = %s AND used_at IS NULL AND expires_at > now()
            """, (token_hash, user_id))
            if cur.rowcount != 1:
                return None  # used, burned by a password change, or expired (re-checked under the user lock)
            await cur.execute("""
                UPDATE public.users SET password_hash = %s, password_changed_at = now(),
                       failed_login_attempts = 0, locked_until = NULL
                WHERE id = %s
            """, (new_hash, user_id))
            await burn_reset_tokens(cur, user_id)
            return user_id, await revoke_sessions(cur, user_id)

    # ── invites ────────────────────────────────────────────────────────────
    async def create_invite(self, email: str, role: str, streamer_id: str | None, token_hash: str,
                            invited_by: str, ttl_days: int) -> dict:
        return await self._one("""
            INSERT INTO public.user_invites (email, role, streamer_id, token_hash, invited_by, expires_at)
            VALUES (%s, %s, %s, %s, %s, now() + make_interval(days => %s))
            RETURNING id::text, expires_at
        """, (email, role, streamer_id, token_hash, invited_by, ttl_days))

    async def accept_invite(self, token_hash: str, password_hash: str, username: str | None) -> User | None:
        """Atomically consume the invite and create (or activate) the account."""
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("""
                UPDATE public.user_invites SET accepted_at = now()
                WHERE token_hash = %s AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now()
                RETURNING id, email::text, role, streamer_id
            """, (token_hash,))
            invite = await cur.fetchone()
            if not invite:
                return None
            await cur.execute(f"""
                INSERT INTO public.users (email, password_hash, username, role, streamer_id, email_verified_at,
                                          password_changed_at)
                VALUES (%(email)s, %(hash)s, %(username)s, %(role)s, %(streamer_id)s, now(), now())
                ON CONFLICT (email) DO UPDATE SET
                    password_hash = EXCLUDED.password_hash,
                    username = COALESCE(public.users.username, EXCLUDED.username),
                    streamer_id = COALESCE(EXCLUDED.streamer_id, public.users.streamer_id),
                    role = CASE WHEN public.users.role = 'admin' THEN 'admin' ELSE EXCLUDED.role END,
                    email_verified_at = COALESCE(public.users.email_verified_at, now()),
                    password_changed_at = now(), failed_login_attempts = 0, locked_until = NULL
                WHERE public.users.password_hash IS NULL
                RETURNING {_USER_COLS}
            """, {"email": invite["email"], "hash": password_hash, "username": username, "role": invite["role"],
                  "streamer_id": invite["streamer_id"]})
            user = await cur.fetchone()
            if not user:  # account already has a password: the invite cannot take it over
                raise InviteConflict()
            await cur.execute("UPDATE public.user_invites SET accepted_user_id = %s WHERE id = %s",
                              (user["id"], invite["id"]))
            await burn_reset_tokens(cur, user["id"])  # F10: the account now has a password
            return User(**user)

    # ── Google identities ──────────────────────────────────────────────────
    async def user_by_identity(self, provider: str, subject: str) -> User | None:
        row = await self._one(f"""
            SELECT {', '.join('u.' + c.strip() for c in _USER_COLS.split(','))}
            FROM public.user_identities i JOIN public.users u ON u.id = i.user_id
            WHERE i.provider = %s AND i.provider_subject = %s
        """, (provider, subject))
        return User(**row) if row else None

    async def link_identity(self, user_id: str, provider: str, subject: str, email: str | None) -> None:
        await self._exec("""
            INSERT INTO public.user_identities (user_id, provider, provider_subject, email, last_login_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (provider, provider_subject) DO UPDATE SET last_login_at = now()
        """, (user_id, provider, subject, email))

    # ── rate limiting (fixed window in public.rate_limits) ─────────────────
    async def hit_rate_limit(self, key: str, bucket: str, limit: int, window_seconds: int) -> bool:
        """Count one request; True while within the limit."""
        row = await self._one("""
            INSERT INTO public.rate_limits (ip_address, endpoint, request_count, window_start)
            VALUES (%s, %s, 1, now())
            ON CONFLICT (ip_address, endpoint) DO UPDATE SET
              request_count = CASE WHEN public.rate_limits.window_start < now() - make_interval(secs => %s)
                                   THEN 1 ELSE public.rate_limits.request_count + 1 END,
              window_start = CASE WHEN public.rate_limits.window_start < now() - make_interval(secs => %s)
                                  THEN now() ELSE public.rate_limits.window_start END
            RETURNING request_count
        """, (key, bucket, window_seconds, window_seconds))
        return row["request_count"] <= limit

    async def clear_rate_limits(self, key: str, buckets: list[str]) -> None:
        await self._exec("DELETE FROM public.rate_limits WHERE ip_address = %s AND endpoint = ANY(%s)", (key, buckets))


class InviteConflict(Exception):
    """The invited email already belongs to an account that has a password."""
