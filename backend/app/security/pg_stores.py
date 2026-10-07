"""PostgreSQL implementations of the security store protocols (run as hyperchat_app)."""

from datetime import datetime

from psycopg import errors as pg_errors
from psycopg.rows import dict_row

from app.security.nonce_store import NonceReplayError, NonceStoreError
from app.security.obs_token import ObsTokenInfo
from app.security.shared_secret import PartnerKey


class PostgresNonceStore:
    """public.request_nonces: the primary key turns a second use of a nonce into a replay error."""

    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def insert(self, nonce: str, user_id: str | None, expires_at: datetime) -> None:
        try:
            async with self._conn() as conn, conn.transaction():
                await conn.execute("INSERT INTO public.request_nonces (nonce, user_id, expires_at) VALUES (%s, %s, %s)",
                                   (nonce, user_id, expires_at))
        except pg_errors.UniqueViolation:
            raise NonceReplayError(nonce) from None
        except Exception as exc:  # noqa: BLE001 - callers fail closed on any other error
            raise NonceStoreError(type(exc).__name__) from None

    async def purge_expired(self) -> None:
        async with self._conn() as conn:
            await conn.execute("DELETE FROM public.request_nonces WHERE expires_at < now()")


class PostgresObsTokenStore:
    """public.obs_tokens, looked up by token_hash only. A valid lookup records last use.

    A revoked token stays valid until its revoked_at passes (legacy rotation grace, P10-D2): creating a new token
    schedules the previous ones for revocation 60 s later so a running OBS source is not cut off mid-item.
    """

    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def validate(self, token_hash: str) -> ObsTokenInfo | None:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("""
                UPDATE public.obs_tokens t
                SET last_used_at = now(), usage_count = COALESCE(t.usage_count, 0) + 1
                FROM public.streamers s
                WHERE t.token_hash = %s AND s.id = t.streamer_id AND t.is_active IS TRUE
                  AND (t.revoked_at IS NULL OR t.revoked_at > now()) AND (t.expires_at IS NULL OR t.expires_at > now())
                RETURNING t.streamer_id::text, s.streamer_slug, s.streamer_name
            """, (token_hash,))
            row = await cur.fetchone()
        return ObsTokenInfo(row["streamer_id"], row["streamer_slug"], row["streamer_name"]) if row else None

    async def channel_scope(self, token_hash: str, streamer_id: str) -> list[str] | None:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT pusher_channel_scope FROM public.obs_tokens WHERE token_hash = %s "
                              "AND streamer_id = %s AND is_active IS TRUE AND (revoked_at IS NULL OR revoked_at > now())",
                              (token_hash, streamer_id))
            row = await cur.fetchone()
        scope = row["pusher_channel_scope"] if row else None
        return list(scope) if scope else None


class PostgresPartnerKeyStore:
    """public.partner_api_keys, looked up by key_hash; revoked keys never match. Records last use."""

    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def find_active_by_hash(self, key_hash: str) -> PartnerKey | None:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("UPDATE public.partner_api_keys SET last_used_at = now() "
                              "WHERE key_hash = %s AND revoked IS FALSE RETURNING id::text, partner_slug", (key_hash,))
            row = await cur.fetchone()
        return PartnerKey(row["id"], row["partner_slug"]) if row else None
