"""SQL for the moderation bots (runs as hyperchat_app). Every lookup is scoped by creator.

  notification context   the donation + its creator's bot settings (moderator_notify job)
  deliveries             moderator_notifications: ensure (INSERT .. ON CONFLICT DO NOTHING), mark; idempotent
  callbacks              telegram_callback_mapping / discord_callback_mapping: 16-character CSPRNG ids, 24 h
  moderators             active moderators of a creator; a platform user's assignments (commands)
  commands               pending / recent donations (paid), pending counts
"""

import secrets
import string
from dataclasses import dataclass

from psycopg.rows import dict_row

CALLBACK_TABLE = {"telegram": "telegram_callback_mapping", "discord": "discord_callback_mapping"}
_ID_ALPHABET = string.ascii_letters + string.digits
CALLBACK_ID_LENGTH = 16
NOTIFY_WINDOW_HOURS = 24
# Creators one Telegram user's commands cover. A safety bound (each creator is at least one message, /pending up to
# six), far above real use; past it the commands SAY the list was cut (never silently, as the old LIMIT 10 did).
MAX_ASSIGNMENTS = 50
_MOD_COLS = """m.id::text AS id, m.streamer_id::text AS streamer_id, m.mod_name, m.telegram_user_id, m.discord_user_id,
    COALESCE(m.role::text, 'moderator') AS role, m.can_approve, m.can_reject, m.can_hide_message, m.can_ban,
    m.is_active, COALESCE(m.total_actions, 0) AS total_actions, m.created_at"""
_STREAMER_COLS = """s.streamer_slug, s.streamer_name, COALESCE(s.moderation_mode, 'auto_approve') AS moderation_mode,
    COALESCE(s.telegram_moderation_enabled, true) AS telegram_enabled,
    COALESCE(s.discord_moderation_enabled, false) AS discord_enabled, s.discord_moderation_channel_id"""


def new_callback_id() -> str:
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(CALLBACK_ID_LENGTH))


@dataclass(frozen=True)
class Target:
    platform: str          # telegram | discord
    target_kind: str       # dm | channel
    target_id: str         # Telegram chat id, Discord user id (dm) or channel id
    moderator_id: str | None


class BotRepository:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def _all(self, sql: str, params) -> list[dict]:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    async def _one(self, sql: str, params) -> dict | None:
        rows = await self._all(sql, params)
        return rows[0] if rows else None

    # ── notification job ────────────────────────────────────────────────────
    async def notify_context(self, donation_id: str) -> dict | None:
        return await self._one(f"""
            SELECT d.id::text AS id, d.streamer_id::text AS streamer_id, d.donor_name, d.amount,
                   d.currency::text AS currency, d.message, d.message_visible, d.payment_status, d.moderation_status,
                   d.voice_message_url, d.media_url, d.media_type, d.created_at, d.mod_notified,
                   d.created_at < now() - make_interval(hours => %s) AS too_old, {_STREAMER_COLS}
            FROM public.donations d JOIN public.streamers s ON s.id = d.streamer_id WHERE d.id = %s
        """, (NOTIFY_WINDOW_HOURS, donation_id))

    async def moderators(self, streamer_id: str) -> list[dict]:
        return await self._all(f"SELECT {_MOD_COLS} FROM public.streamers_moderators m "
                               "WHERE m.streamer_id = %s AND m.is_active = true ORDER BY m.created_at, m.id",
                               (streamer_id,))

    async def ensure_deliveries(self, donation_id: str, streamer_id: str, targets: list[Target]) -> list[dict]:
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            for t in targets:
                await cur.execute("""
                    INSERT INTO public.moderator_notifications (donation_id, streamer_id, moderator_id, platform,
                        target_kind, target_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT ON CONSTRAINT moderator_notifications_target DO NOTHING
                """, (donation_id, streamer_id, t.moderator_id, t.platform, t.target_kind, t.target_id))
            await cur.execute("""
                SELECT id::text AS id, moderator_id::text AS moderator_id, platform, target_kind, target_id, status
                FROM public.moderator_notifications WHERE donation_id = %s ORDER BY created_at, id
            """, (donation_id,))
            return await cur.fetchall()

    async def mark_delivery(self, delivery_id: str, status: str, *, reason: str | None = None,
                            message_id: str | None = None) -> None:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("""
                UPDATE public.moderator_notifications SET status = %s, attempts = attempts + 1,
                    last_error = %s, provider_message_id = COALESCE(%s, provider_message_id),
                    sent_at = CASE WHEN %s = 'sent' THEN now() ELSE sent_at END
                WHERE id = %s AND status IN ('pending', 'failed')
            """, (status, reason, (message_id or None) and message_id[:40], status, delivery_id))

    @staticmethod
    async def mark_notified(cur, donation_id: str) -> None:
        await cur.execute("UPDATE public.donations SET mod_notified = true WHERE id = %s", (donation_id,))

    @staticmethod
    async def give_up(cur, donation_id: str, reason: str) -> None:
        await cur.execute("UPDATE public.donations SET mod_notified = true WHERE id = %s", (donation_id,))
        await cur.execute("UPDATE public.moderator_notifications SET status = 'failed', last_error = %s "
                          "WHERE donation_id = %s AND status = 'pending'", (reason, donation_id))

    # ── callbacks ───────────────────────────────────────────────────────────
    async def create_callback(self, platform: str, *, donation_id: str, streamer_id: str, action: str,
                              recipient_id: str, moderator_id: str | None) -> str:
        short_id = new_callback_id()
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(f"""
                INSERT INTO public.{CALLBACK_TABLE[platform]} (short_id, donation_id, streamer_id, action_type,
                    recipient_id, moderator_id, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, now() + interval '24 hours')
            """, (short_id, donation_id, streamer_id, action, recipient_id, moderator_id))
        return f"m:{short_id}"

    async def callback(self, platform: str, short_id: str) -> dict | None:
        return await self._one(f"""
            SELECT short_id, donation_id::text AS donation_id, streamer_id::text AS streamer_id, action_type,
                   recipient_id, moderator_id::text AS moderator_id, expires_at <= now() AS expired
            FROM public.{CALLBACK_TABLE[platform]} WHERE short_id = %s
        """, (short_id,))

    # ── moderators acting through a bot ─────────────────────────────────────
    async def moderator_for(self, platform: str, streamer_id: str, platform_user_id: str) -> dict | None:
        column = "telegram_user_id" if platform == "telegram" else "discord_user_id"
        return await self._one(f"""
            SELECT {_MOD_COLS}, {_STREAMER_COLS} FROM public.streamers_moderators m
            JOIN public.streamers s ON s.id = m.streamer_id
            WHERE m.streamer_id = %s AND m.{column} = %s AND m.is_active = true
            ORDER BY m.created_at LIMIT 1
        """, (streamer_id, platform_user_id))

    async def telegram_assignments(self, telegram_user_id: str, *, active_only: bool = True,
                                   slugs: frozenset[str] | None = None) -> list[dict]:
        """The user's moderator rows (one per creator), by creator name. `slugs` (the non-production creator
        allowlist) is applied here, BEFORE the bound, so an allow-listed creator is never cut off by others.
        At most MAX_ASSIGNMENTS + 1 rows: one more than commands show, so they can say the list was cut."""
        active = "AND m.is_active = true" if active_only else ""
        scope = "AND lower(s.streamer_slug) = ANY(%(slugs)s)" if slugs is not None else ""
        return await self._all(f"""
            SELECT {_MOD_COLS}, {_STREAMER_COLS} FROM public.streamers_moderators m
            JOIN public.streamers s ON s.id = m.streamer_id
            WHERE m.telegram_user_id = %(uid)s {active} {scope}
            ORDER BY s.streamer_name, m.created_at LIMIT %(limit)s
        """, {"uid": telegram_user_id, "slugs": sorted(slugs or ()), "limit": MAX_ASSIGNMENTS + 1})

    # ── commands ────────────────────────────────────────────────────────────
    async def pending_donations(self, streamer_id: str, limit: int = 5) -> list[dict]:
        return await self._all(f"""
            SELECT d.id::text AS id, d.streamer_id::text AS streamer_id, d.donor_name, d.amount,
                   d.currency::text AS currency, d.message, d.message_visible, d.moderation_status, d.voice_message_url,
                   d.media_url, d.media_type, d.created_at, {_STREAMER_COLS}
            FROM public.donations d JOIN public.streamers s ON s.id = d.streamer_id
            WHERE d.streamer_id = %s AND d.payment_status = 'success' AND d.moderation_status = 'pending'
            ORDER BY d.created_at, d.id LIMIT %s
        """, (streamer_id, limit))

    async def recent_donations(self, streamer_id: str, limit: int = 5) -> list[dict]:
        return await self._all("""
            SELECT donor_name, amount, currency::text AS currency, message, message_visible, created_at
            FROM public.donations WHERE streamer_id = %s AND payment_status = 'success'
              AND moderation_status IN ('approved', 'auto_approved')
            ORDER BY created_at DESC, id DESC LIMIT %s
        """, (streamer_id, limit))

    async def pending_count(self, streamer_id: str) -> int:
        row = await self._one("SELECT count(*) AS n FROM public.donations WHERE streamer_id = %s "
                              "AND payment_status = 'success' AND moderation_status = 'pending'", (streamer_id,))
        return row["n"]
