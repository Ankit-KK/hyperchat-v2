"""SQL for the creator dashboard (runs as hyperchat_app; DML only).

Every query is scoped by streamer_id: a donation or moderator id from another creator never matches.
Indexes used (no new index in Phase 5):
  settings             streamers primary key                                    1 row
  donations / export   donations_streamer_created_idx (streamer_id, created_at DESC)
  stats                donations_streamer_status_idx (streamer_id, payment_status, ...)
"""

from datetime import datetime
from decimal import Decimal

from psycopg.rows import dict_row

from app.dashboard.validation import WRITABLE_SETTINGS

# Nullable booleans / mode are served with their column default, as Phase 4 does for the public API.
_SETTINGS_COLS = """streamer_slug AS slug, streamer_name AS name, status,
    COALESCE(moderation_mode, 'auto_approve') AS moderation_mode,
    COALESCE(telegram_moderation_enabled, true) AS telegram_moderation_enabled,
    COALESCE(discord_moderation_enabled, false) AS discord_moderation_enabled,
    COALESCE(media_moderation_enabled, true) AS media_moderation_enabled,
    COALESCE(tts_enabled, true) AS tts_enabled, tts_language_boost, el_voice_id,
    COALESCE(hyperemotes_enabled, true) AS hyperemotes_enabled, hyperemotes_min_amount,
    COALESCE(media_upload_enabled, false) AS media_upload_enabled, media_min_amount,
    brand_color, alert_box_scale, alert_overlay_id, leaderboard_overlay_id, goal_overlay_id,
    overlay_adjust_mode, leaderboard_bg_enabled,
    COALESCE(leaderboard_widget_enabled, true) AS leaderboard_widget_enabled,
    goal_name, goal_target_amount, COALESCE(goal_is_active, false) AS goal_is_active, goal_activated_at"""

# Column types of the writable settings: parameters are cast so that IS DISTINCT FROM compares values
# exactly as stored (e.g. numeric 1.00 equals 1.0).
SETTINGS_SQL_TYPES = {
    "moderation_mode": "text", "telegram_moderation_enabled": "boolean", "discord_moderation_enabled": "boolean",
    "tts_enabled": "boolean", "hyperemotes_enabled": "boolean", "alert_box_scale": "numeric",
    "goal_is_active": "boolean", "goal_name": "text", "goal_target_amount": "numeric",
    "goal_activated_at": "timestamptz", "media_upload_enabled": "boolean", "media_moderation_enabled": "boolean",
    "tts_language_boost": "text", "alert_overlay_id": "smallint", "leaderboard_overlay_id": "smallint",
    "overlay_adjust_mode": "boolean", "leaderboard_bg_enabled": "boolean", "goal_overlay_id": "smallint",
    "brand_color": "text", "leaderboard_widget_enabled": "boolean", "el_voice_id": "text",
}


_DONATION_COLS = """d.id::text AS donation_id, d.donor_name, d.amount, d.currency, d.amount_inr, d.message,
    d.message_visible, d.is_hyperemote, d.voice_message_url, d.tts_audio_url, d.hypersound_url, d.media_url,
    d.media_type, d.moderation_status, d.created_at"""


def _db_value(value):
    # JSON numbers arrive as float; numeric columns get the exact decimal the client wrote.
    return Decimal(repr(value)) if isinstance(value, float) else value


class DashboardRepository:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def _all(self, sql: str, params: dict | tuple | None = None) -> list[dict]:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params or None)
            return await cur.fetchall()

    async def _one(self, sql: str, params: dict | tuple | None = None) -> dict | None:
        rows = await self._all(sql, params)
        return rows[0] if rows else None

    # ── G1 settings ────────────────────────────────────────────────────────
    async def settings(self, streamer_id: str) -> dict | None:
        return await self._one(f"SELECT {_SETTINGS_COLS} FROM public.streamers WHERE id = %s", (streamer_id,))

    async def update_settings(self, streamer_id: str, changes: dict) -> tuple[dict | None, list[str]]:
        """Write only the settings whose value differs (compared by PostgreSQL under a row lock).

        Returns (settings row, changed fields). When nothing differs, no UPDATE runs, so updated_at
        is untouched.
        """
        cols = [c for c in WRITABLE_SETTINGS if c in changes]
        params = {**{c: _db_value(changes[c]) for c in cols}, "id": streamer_id}
        differs = ", ".join(f"({c} IS DISTINCT FROM %({c})s::{SETTINGS_SQL_TYPES[c]}) AS {c}" for c in cols)
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"SELECT {differs} FROM public.streamers WHERE id = %(id)s FOR UPDATE", params)
            diff = await cur.fetchone()
            if diff is None:
                return None, []
            changed = [c for c in cols if diff[c]]
            if changed:
                sets = ", ".join(f"{c} = %({c})s::{SETTINGS_SQL_TYPES[c]}" for c in changed)
                await cur.execute(f"UPDATE public.streamers SET {sets} WHERE id = %(id)s", params)
            await cur.execute(f"SELECT {_SETTINGS_COLS} FROM public.streamers WHERE id = %(id)s", params)
            return await cur.fetchone(), changed

    # ── G2 donations (paid only) ───────────────────────────────────────────
    async def donations(self, streamer_id: str, *, statuses: tuple[str, ...] | None, limit: int,
                        after: tuple[datetime, str] | None, columns: str = _DONATION_COLS) -> list[dict]:
        """Paid donations, newest first (created_at DESC, id DESC), after the keyset position `after`."""
        where = ["d.streamer_id = %(sid)s", "d.payment_status = 'success'"]
        params: dict = {"sid": streamer_id, "limit": limit}
        if statuses is not None:
            where.append("d.moderation_status = ANY(%(statuses)s)")
            params["statuses"] = list(statuses)
        if after is not None:
            where.append("(d.created_at, d.id) < (%(after_at)s, %(after_id)s::uuid)")
            params.update(after_at=after[0], after_id=after[1])
        return await self._all(f"SELECT {columns} FROM public.donations d WHERE {' AND '.join(where)} "
                               "ORDER BY d.created_at DESC, d.id DESC LIMIT %(limit)s", params)

    async def donation_stats(self, streamer_id: str, *, day: tuple[datetime, datetime],
                             month: tuple[datetime, datetime]) -> dict:
        return await self._one("""
            SELECT COALESCE(sum(amount_inr), 0) AS total_revenue_inr, count(*) AS total_donations,
                   COALESCE(sum(amount_inr) FILTER (WHERE created_at >= %(day_start)s
                                                     AND created_at < %(day_end)s), 0) AS today_revenue_inr,
                   COALESCE(sum(amount_inr) FILTER (WHERE created_at >= %(month_start)s
                                                     AND created_at < %(month_end)s), 0) AS month_revenue_inr
            FROM public.donations WHERE streamer_id = %(sid)s AND payment_status = 'success'
        """, {"sid": streamer_id, "day_start": day[0], "day_end": day[1], "month_start": month[0],
              "month_end": month[1]})

    async def export_rows(self, streamer_id: str, *, start: datetime, end: datetime, limit: int) -> list[dict]:
        """Paid donations with start <= created_at < end, newest first (no order ids: D6)."""
        return await self._all("""
            SELECT created_at, donor_name, amount, currency, amount_inr FROM public.donations
            WHERE streamer_id = %s AND payment_status = 'success' AND created_at >= %s AND created_at < %s
            ORDER BY created_at DESC, id DESC LIMIT %s
        """, (streamer_id, start, end, limit))
