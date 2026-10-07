"""OBS token management (P10-D1..D3; runs as hyperchat_app).

  create   mints "obs_" + 64 hex characters (32 bytes from the CSPRNG; legacy format) and stores ONLY its SHA-256
           (obs_tokens.token_hash). Under a per-creator advisory lock, the creator's other usable tokens are
           scheduled for revocation 60 s later (legacy rotation grace: a running OBS source is not cut off
           mid-item). The token is returned ONCE, inside the OBS URLs; it can never be shown again.
  list     id, state (active | revoking | revoked | expired | inactive), created / last used, use count, expiry.
           Never the token or its hash.
  revoke   immediate (is_active = false, revoked_at = now()).
Scope stays the default (P10-D3): obs_tokens.pusher_channel_scope is never set here. Migrated legacy tokens
keep working (the ETL stored their hashes) but cannot be displayed.
"""

import secrets

from psycopg.rows import dict_row

from app.security.crypto import sha256_hex

GRACE_SECONDS = 60
LIST_LIMIT = 50
_STATE = """CASE WHEN expires_at IS NOT NULL AND expires_at <= now() THEN 'expired'
                 WHEN revoked_at IS NOT NULL AND revoked_at <= now() THEN 'revoked'
                 WHEN is_active IS NOT TRUE THEN 'inactive'
                 WHEN revoked_at IS NOT NULL THEN 'revoking'
                 ELSE 'active' END AS state"""
_COLS = f"""id::text AS id, {_STATE}, created_at, last_used_at, COALESCE(usage_count, 0) AS usage_count, expires_at,
    revoked_at"""


def new_token() -> str:
    return "obs_" + secrets.token_hex(32)


def obs_urls(api_base: str, token: str) -> dict:
    """The player URL carries the token in the #fragment (never sent to a server, never in a log); the Media
    Source URL needs it in the query string (OBS Media Source cannot send headers)."""
    base = api_base.rstrip("/")
    return {"player_url": f"{base}/obs/player#token={token}",
            "media_source_url": f"{base}/api/v1/obs/audio/next?token={token}"}


class TokenRepository:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def create(self, streamer_id: str) -> tuple[str, dict, int]:
        """(token, the new row, number of previous tokens scheduled for revocation)."""
        token = new_token()
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))",
                              (f"hyperchat:obs-tokens:{streamer_id}",))
            await cur.execute("""
                UPDATE public.obs_tokens SET revoked_at = now() + make_interval(secs => %s)
                WHERE streamer_id = %s AND is_active IS TRUE
                  AND (revoked_at IS NULL OR revoked_at > now() + make_interval(secs => %s))
            """, (GRACE_SECONDS, streamer_id, GRACE_SECONDS))
            rotated = cur.rowcount
            await cur.execute(f"INSERT INTO public.obs_tokens (streamer_id, token_hash, is_active) VALUES (%s, %s, true) "
                              f"RETURNING {_COLS}", (streamer_id, sha256_hex(token)))
            return token, await cur.fetchone(), rotated

    async def list(self, streamer_id: str) -> list[dict]:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"SELECT {_COLS} FROM public.obs_tokens WHERE streamer_id = %s "
                              "ORDER BY created_at DESC, id DESC LIMIT %s", (streamer_id, LIST_LIMIT))
            return await cur.fetchall()

    async def revoke(self, streamer_id: str, token_id: str) -> dict | None:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"""
                UPDATE public.obs_tokens SET is_active = false,
                    revoked_at = CASE WHEN revoked_at IS NOT NULL AND revoked_at <= now() THEN revoked_at ELSE now() END
                WHERE id = %s AND streamer_id = %s RETURNING {_COLS}
            """, (token_id, streamer_id))
            return await cur.fetchone()
