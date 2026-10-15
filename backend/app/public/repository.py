"""Read-only SQL for the public API (runs as hyperchat_app). Every query is bounded and indexed:

  creator by slug      streamers_streamer_slug_key (unique)                     1 row
  voice tiers          streamer_voice_tiers_streamer_idx / platform (3 rows)    few rows
  soundboard           streamer_soundboard_slug_visible_order_idx (<=45) + platform (<=44)
  goal progress        donations_streamer_created_idx (streamer_id, created_at)  creator's rows since activation
  metrics              one aggregate pass over donations, cached in-process (E7/E8)
"""

from datetime import datetime

from psycopg.rows import dict_row

from app.soundboard import terms

# soundboard_enabled means the Custom Soundboard is USABLE (eligible AND current terms accepted, D3), not the raw flag.
_PUBLIC_STREAMER_COLS = f"""id::text AS id, streamer_slug, streamer_name, brand_color, brand_logo_url, background_url,
    COALESCE(tts_enabled, true) AS tts_enabled, COALESCE(hyperemotes_enabled, true) AS hyperemotes_enabled,
    COALESCE(media_upload_enabled, false) AS media_upload_enabled, media_max_file_size_mb,
    {terms.usable_sql("streamers")} AS soundboard_enabled,
    goal_overlay_id, goal_name, goal_target_amount, goal_activated_at, COALESCE(goal_is_active, false) AS goal_is_active,
    min_text_amount_inr, min_tts_amount_inr, min_voice_amount_inr, min_hypersound_amount_inr, media_min_amount,
    message_char_tiers"""

APPROVED = ("approved", "auto_approved")


class PublicRepository:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params or None)
            return await cur.fetchall()

    async def active_streamer(self, slug: str) -> dict | None:
        """Only `active` creators are public (D3). The id stays server-side, never in a response."""
        rows = await self._all(f"SELECT {_PUBLIC_STREAMER_COLS} FROM public.streamers "
                               "WHERE streamer_slug = %s AND status = 'active'", (slug,))
        return rows[0] if rows else None

    async def donation_block(self, streamer_id: str) -> str | None:
        """public.creator_donation_block: None when the creator may accept a NEW donation, else the reason code. The
        donations insert trigger runs the same function again, so no path can skip it."""
        return (await self._all("SELECT public.creator_donation_block(%s) AS block", (streamer_id,)))[0]["block"]

    async def voice_tiers(self, streamer_id: str) -> tuple[list[tuple], list[tuple]]:
        own = await self._all("SELECT min_amount_inr, max_duration_seconds FROM public.streamer_voice_tiers "
                              "WHERE streamer_id = %s AND is_active ORDER BY min_amount_inr", (streamer_id,))
        if own:
            return [(r["min_amount_inr"], r["max_duration_seconds"]) for r in own], []
        platform = await self._all("SELECT min_amount_inr, max_duration_seconds FROM public.platform_voice_tiers "
                                   "WHERE is_active ORDER BY min_amount_inr")
        return [], [(r["min_amount_inr"], r["max_duration_seconds"]) for r in platform]

    async def soundboard_slots(self, slug: str) -> tuple[list[dict], list[dict]]:
        platform = await self._all("SELECT id::text AS id, display_name, audio_url, image_url, default_order_index "
                                   "FROM public.platform_soundboard_slots ORDER BY default_order_index")
        own = await self._all("SELECT id::text AS id, platform_slot_id::text AS platform_slot_id, is_visible, "
                              "order_index, display_name, audio_url, image_url "
                              "FROM public.streamer_soundboard_slots WHERE streamer_slug = %s", (slug,))
        return platform, own

    async def goal_progress_inr(self, streamer_id: str, since: datetime) -> float:
        rows = await self._all("SELECT COALESCE(SUM(amount_inr), 0) AS total FROM public.donations "
                               "WHERE streamer_id = %s AND payment_status = 'success' "
                               "AND moderation_status IN ('approved', 'auto_approved') AND created_at >= %s",
                               (streamer_id, since))
        return float(rows[0]["total"])

    async def platform_metrics(self) -> dict:
        rows = await self._all("""
            SELECT count(*) FILTER (WHERE payment_status = 'success') AS total_interactions,
                   count(*) FILTER (WHERE payment_status = 'success'
                                    AND moderation_status IN ('approved', 'auto_approved')) AS total_alerts,
                   count(DISTINCT streamer_id) FILTER (WHERE payment_status = 'success') AS active_streams
            FROM public.donations
        """)
        return rows[0]

    async def showcase_metrics(self) -> dict:
        rows = await self._all("""
            SELECT (SELECT count(*) FROM public.streamers WHERE status = 'active') AS streamers_live,
                   (SELECT count(*) FROM public.donations
                    WHERE payment_status = 'success' AND moderation_status IN ('approved', 'auto_approved')
                      AND created_at > now() - interval '24 hours') AS alerts_last_24h
        """)
        return rows[0]
