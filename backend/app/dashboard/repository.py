"""SQL for the creator dashboard (runs as hyperchat_app; DML only).

Every query is scoped by streamer_id: a donation or moderator id from another creator never matches.
Indexes used (no new index in Phase 5):
  settings             streamers primary key                                    1 row
"""

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
