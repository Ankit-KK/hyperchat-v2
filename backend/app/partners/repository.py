"""SQL for the partner API (runs as hyperchat_app; DML only, tables from 0003). Partner keys are stored only as the
SHA-256 of the key plus a 12-character display prefix; the key itself is shown once, at creation, and never again."""

from psycopg.rows import dict_row

from app.stepup.grants import Grant, consume

_KEY_COLS = "id::text, partner_slug, key_prefix, revoked, last_used_at, created_at"


class PartnerRepository:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def creator_exists(self, slugs: list[str]) -> bool:
        """Any creator row whose slug is exactly one of `slugs` (every status, as legacy). Nothing else is read."""
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("SELECT EXISTS (SELECT 1 FROM public.streamers WHERE streamer_slug = ANY(%s))", (slugs,))
            return (await cur.fetchone())[0]

    async def add_signup(self, partner_slug: str, value: dict, source_ip: str | None,
                         user_agent: str | None) -> str | None:
        """The new row's id, or None when this partner already sent this email (any letter case; legacy UNIQUE index
        partner_signups_partner_email_uniq). Never creates a user or a creator."""
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO public.partner_signups (partner_slug, channel_name, mobile_number, youtube_channel, email,
                    instagram_handle, source_ip, user_agent)
                VALUES (%(p)s, %(channel_name)s, %(mobile_number)s, %(youtube_channel)s, %(email)s,
                    %(instagram_handle)s, %(ip)s, %(ua)s)
                ON CONFLICT (partner_slug, (lower(email))) DO NOTHING
                RETURNING id::text
            """, {**value, "p": partner_slug, "ip": source_ip, "ua": user_agent})
            row = await cur.fetchone()
            return row[0] if row else None

    # ── key management (admin) ─────────────────────────────────────────────
    async def create_key(self, partner_slug: str, key_hash: str, key_prefix: str, grant: Grant | None = None) -> dict:
        """A step-up grant (S2) is consumed in the same transaction as the insert."""
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await consume(cur, grant)
            await cur.execute(f"INSERT INTO public.partner_api_keys (partner_slug, key_hash, key_prefix) "
                              f"VALUES (%s, %s, %s) RETURNING {_KEY_COLS}", (partner_slug, key_hash, key_prefix))
            return await cur.fetchone()

    async def list_keys(self) -> list[dict]:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"SELECT {_KEY_COLS} FROM public.partner_api_keys ORDER BY created_at DESC LIMIT 200")
            return await cur.fetchall()

    async def revoke_key(self, key_id: str) -> tuple[str, dict | None]:
        """("revoked" | "already" | "not_found", the key row). Revocation is permanent; nothing else changes."""
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"SELECT {_KEY_COLS} FROM public.partner_api_keys WHERE id = %s FOR UPDATE", (key_id,))
            row = await cur.fetchone()
            if row is None:
                return "not_found", None
            if row["revoked"]:
                return "already", row
            await cur.execute(f"UPDATE public.partner_api_keys SET revoked = true WHERE id = %s RETURNING {_KEY_COLS}",
                              (key_id,))
            return "revoked", await cur.fetchone()
