"""SQL for the creator dashboard (runs as hyperchat_app; DML only).

Every query is scoped by streamer_id: a donation or moderator id from another creator never matches.
Indexes used (no new index in Phase 5):
  settings             streamers primary key                                    1 row
  donations / export   donations_streamer_created_idx (streamer_id, created_at DESC)
  stats / queue count  donations_streamer_status_idx (streamer_id, payment_status, moderation_status)
  is_donor_banned      banned_donors_name_idx (donor_name_lower) WHERE is_active = true
  donor totals         UNIQUE (streamer_id, donator_name_lower)
  moderators           streamers_moderators_unique_active (streamer_id, telegram_user_id) prefix
"""

from datetime import datetime
from decimal import Decimal

from psycopg import errors as pg_errors
from psycopg.rows import dict_row

from app.dashboard.moderation import Step, transition
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
_QUEUE_COLS = _DONATION_COLS + """, d.approved_at, d.approved_by_label,
    EXISTS (SELECT 1 FROM public.banned_donors b WHERE b.streamer_id = d.streamer_id AND b.is_active = true
            AND b.donor_name_lower = lower(d.donor_name)) AS is_donor_banned"""


_MODERATOR_COLS = """id::text AS moderator_id, mod_name, telegram_user_id, discord_user_id,
    COALESCE(role::text, 'moderator') AS role, COALESCE(can_approve, true) AS can_approve,
    COALESCE(can_reject, true) AS can_reject, COALESCE(can_hide_message, true) AS can_hide_message,
    COALESCE(can_ban, false) AS can_ban, COALESCE(can_replay, true) AS can_replay, is_active, created_at,
    last_action_at"""


class DuplicateModerator(Exception):
    """An active moderator of this creator already has the Telegram or Discord id."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind  # "telegram" | "discord"


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
                        after: tuple[datetime, str] | None, queue: bool = False) -> list[dict]:
        """Paid donations, newest first (created_at DESC, id DESC), after the keyset position `after`.
        `queue` adds the moderation columns (approved_at, approved_by_label, is_donor_banned)."""
        where = ["d.streamer_id = %(sid)s", "d.payment_status = 'success'"]
        params: dict = {"sid": streamer_id, "limit": limit}
        if statuses is not None:
            where.append("d.moderation_status = ANY(%(statuses)s)")
            params["statuses"] = list(statuses)
        if after is not None:
            where.append("(d.created_at, d.id) < (%(after_at)s, %(after_id)s::uuid)")
            params.update(after_at=after[0], after_id=after[1])
        columns = _QUEUE_COLS if queue else _DONATION_COLS
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

    # ── G3 moderation ──────────────────────────────────────────────────────
    async def pending_count(self, streamer_id: str) -> int:
        row = await self._one("SELECT count(*) AS n FROM public.donations WHERE streamer_id = %s "
                              "AND payment_status = 'success' AND moderation_status = 'pending'", (streamer_id,))
        return row["n"]

    async def moderate(self, streamer_id: str, donation_id: str, action: str, *, actor_user_id: str | None,
                       actor_label: str, notes: str | None) -> tuple[str, Step, dict] | None:
        """One action in ONE transaction: lock the donation (scoped to the creator), decide with
        transition(), then write the donation, the donor total, the ban and the audit row.

        Returns (previous status, step, donation row), or None when this creator has no such donation.
        NotPaid (raised by transition) rolls back with nothing written; so does any other error.
        Concurrent actions on one donation serialise on the row lock: the second sees the new state.
        """
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT donor_name, amount_inr, payment_status, moderation_status, message_visible "
                              "FROM public.donations WHERE id = %s AND streamer_id = %s FOR UPDATE",
                              (donation_id, streamer_id))
            current = await cur.fetchone()
            if current is None:
                return None
            banned = False
            if action == "ban_donor":
                # Two bans of one donor (e.g. on two of their donations) must not both insert a ban.
                await cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s::text || lower(%s::text), 0))",
                                  (f"hyperchat:ban:{streamer_id}:", current["donor_name"]))
                banned = await self._donor_banned(cur, streamer_id, current["donor_name"])
            step = transition(action, payment_status=current["payment_status"],
                              moderation_status=current["moderation_status"],
                              message_visible=current["message_visible"], donor_banned=banned)
            if step.result == "applied":
                await self._write_donation(cur, streamer_id, donation_id, current, step, actor_label)
                if step.totals_delta:
                    await self._change_totals(cur, streamer_id, current["donor_name"], current["amount_inr"],
                                              step.totals_delta)
                if step.ban:
                    await self._insert_ban(cur, streamer_id, current["donor_name"], actor_label, notes)
                await self._record_action(cur, streamer_id, donation_id, action, current["moderation_status"],
                                          step.new_status, actor_user_id, actor_label, notes)
            await cur.execute(f"SELECT {_QUEUE_COLS} FROM public.donations d WHERE d.id = %s", (donation_id,))
            return current["moderation_status"], step, await cur.fetchone()

    @staticmethod
    async def _donor_banned(cur, streamer_id: str, donor_name: str) -> bool:
        await cur.execute("SELECT EXISTS (SELECT 1 FROM public.banned_donors WHERE streamer_id = %s "
                          "AND is_active = true AND donor_name_lower = lower(%s)) AS banned", (streamer_id, donor_name))
        return (await cur.fetchone())["banned"]

    async def _write_donation(self, cur, streamer_id: str, donation_id: str, current: dict, step: Step,
                              actor_label: str) -> None:
        if not step.approve and (step.new_status, step.message_visible) == (current["moderation_status"],
                                                                            current["message_visible"]):
            return  # e.g. a ban on an already rejected donation: only the ban row is new
        sets = ["moderation_status = %(status)s", "message_visible = %(visible)s"]
        if step.approve:
            sets += ["approved_at = now()", "audio_scheduled_at = now()", "approved_by_label = %(label)s",
                     "approved_by_moderator_id = NULL"]
        await cur.execute(f"UPDATE public.donations SET {', '.join(sets)} WHERE id = %(id)s AND streamer_id = %(sid)s",
                          {"status": step.new_status, "visible": step.message_visible, "label": actor_label,
                           "id": donation_id, "sid": streamer_id})

    async def _change_totals(self, cur, streamer_id: str, donor_name: str, amount_inr, delta: int) -> None:
        """Credit (upsert) or debit (floored at 0) the donor's total, as legacy increment/decrement_donator_total
        did, keyed by (streamer_id, lower(donor name))."""
        if delta > 0:
            await cur.execute("""
                INSERT INTO public.streamer_donator_totals
                    (streamer_id, donator_name, donator_name_lower, total_amount, donation_count)
                VALUES (%(sid)s, %(name)s, lower(%(name)s), %(amount)s, 1)
                ON CONFLICT (streamer_id, donator_name_lower) DO UPDATE SET
                    total_amount = public.streamer_donator_totals.total_amount + EXCLUDED.total_amount,
                    donation_count = public.streamer_donator_totals.donation_count + 1,
                    donator_name = EXCLUDED.donator_name, updated_at = now()
            """, {"sid": streamer_id, "name": donor_name, "amount": amount_inr})
        else:
            await cur.execute("""
                UPDATE public.streamer_donator_totals SET total_amount = GREATEST(0, total_amount - %(amount)s),
                       donation_count = GREATEST(0, donation_count - 1), updated_at = now()
                WHERE streamer_id = %(sid)s AND donator_name_lower = lower(%(name)s)
            """, {"sid": streamer_id, "name": donor_name, "amount": amount_inr})

    async def _insert_ban(self, cur, streamer_id: str, donor_name: str, actor_label: str, notes: str | None) -> None:
        await cur.execute("INSERT INTO public.banned_donors (streamer_id, donor_name, banned_by_moderator_id, "
                          "banned_by_name, reason, is_active) VALUES (%s, %s, NULL, %s, %s, true)",
                          (streamer_id, donor_name, actor_label, notes))

    async def _record_action(self, cur, streamer_id: str, donation_id: str, action: str, previous: str, new: str,
                             actor_user_id: str | None, actor_label: str, notes: str | None) -> None:
        await cur.execute("""
            INSERT INTO public.moderation_actions (streamer_id, moderator_id, moderator_telegram_id, moderator_name,
                donation_id, action_type, action_source, previous_status, new_status, notes, actor_user_id)
            VALUES (%s, NULL, NULL, %s, %s, %s, 'dashboard', %s, %s, %s, %s)
        """, (streamer_id, actor_label, donation_id, action, previous, new, notes, actor_user_id))

    # ── G4 moderators ──────────────────────────────────────────────────────
    async def moderators(self, streamer_id: str) -> list[dict]:
        """Active and removed moderators, newest first (legacy order)."""
        return await self._all(f"SELECT {_MODERATOR_COLS} FROM public.streamers_moderators WHERE streamer_id = %s "
                               "ORDER BY created_at DESC, id DESC", (streamer_id,))

    async def add_moderator(self, streamer_id: str, mod_name: str, telegram_user_id: str | None,
                            discord_user_id: str | None) -> dict:
        """Insert an active moderator. Raises DuplicateModerator: Telegram ids through the existing unique
        index (active rows only), Discord ids through a check made under a per-creator lock (there is no
        Discord index: existing data already has duplicates, which stay untouched)."""
        try:
            async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))",
                                  (f"hyperchat:moderators:{streamer_id}",))
                if discord_user_id is not None:
                    await cur.execute("SELECT EXISTS (SELECT 1 FROM public.streamers_moderators WHERE streamer_id = %s "
                                      "AND is_active = true AND discord_user_id = %s) AS taken",
                                      (streamer_id, discord_user_id))
                    if (await cur.fetchone())["taken"]:
                        raise DuplicateModerator("discord")
                return await self._insert_moderator(cur, streamer_id, mod_name, telegram_user_id, discord_user_id)
        except pg_errors.UniqueViolation as exc:
            if exc.diag.constraint_name == "streamers_moderators_unique_active":
                raise DuplicateModerator("telegram") from None
            raise

    async def _insert_moderator(self, cur, streamer_id: str, mod_name: str, telegram_user_id: str | None,
                                discord_user_id: str | None) -> dict:
        await cur.execute(f"INSERT INTO public.streamers_moderators (streamer_id, mod_name, telegram_user_id, "
                          f"discord_user_id) VALUES (%s, %s, %s, %s) RETURNING {_MODERATOR_COLS}",
                          (streamer_id, mod_name, telegram_user_id, discord_user_id))
        return await cur.fetchone()

    async def remove_moderator(self, streamer_id: str, moderator_id: str) -> tuple[str, dict] | None:
        """Soft removal (is_active = false). ("removed" | "unchanged", row), or None when this creator has
        no such moderator."""
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"UPDATE public.streamers_moderators SET is_active = false WHERE id = %s "
                              f"AND streamer_id = %s AND is_active = true RETURNING {_MODERATOR_COLS}",
                              (moderator_id, streamer_id))
            row = await cur.fetchone()
            if row is not None:
                return "removed", row
            await cur.execute(f"SELECT {_MODERATOR_COLS} FROM public.streamers_moderators "
                              "WHERE id = %s AND streamer_id = %s", (moderator_id, streamer_id))
            row = await cur.fetchone()
            return ("unchanged", row) if row else None
