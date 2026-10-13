"""SQL for the creator soundboard (runs as hyperchat_app; DML only, no new schema).

The legacy SECURITY DEFINER functions are reimplemented here as single transactions, each holding a
per-creator advisory lock so edits, swaps and budget checks cannot interleave (legacy had no lock):
  board            seed_streamer_soundboard + the get-soundboard read + get_soundboard_budget
  apply_changes    apply_soundboard_changes (hide, update name, reorder, unhide)
  restore_defaults restore_soundboard_defaults
  swap             swap_soundboard_slot (15 swaps / 7 days, 24 h per position, swap log)
  create / replace / delete   the soundboard-confirm-upload and delete-soundboard-slot writes
Tables are keyed by streamer_slug (ON UPDATE CASCADE). Every statement is scoped by the creator: a slot id
of another creator never matches. Database constraints stay authoritative (45 visible: trigger
enforce_soundboard_active_cap; unique visible position; one custom row per swappable position).
"""

from psycopg import errors as pg_errors
from psycopg.rows import dict_row

from app.soundboard import terms
from app.soundboard.rules import SLOT_COOLDOWN, SWAP_BUDGET, SWAP_WINDOW

_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))"
_SLOTS = """SELECT s.id::text AS id, s.source::text AS source, s.is_visible, s.order_index, s.updated_at,
    s.display_name, s.audio_key, s.audio_url, s.audio_duration_s, s.image_key, s.image_url,
    s.image_type::text AS image_type, s.platform_slot_id::text AS platform_slot_id, s.swappable_index,
    s.last_swapped_at, p.display_name AS p_display_name, p.audio_url AS p_audio_url,
    p.audio_duration_s AS p_audio_duration_s, p.image_url AS p_image_url, p.image_type::text AS p_image_type,
    p.default_order_index, COALESCE(p.is_locked, false) AS is_locked, p.swappable_index AS p_swappable_index
  FROM public.streamer_soundboard_slots s
  LEFT JOIN public.platform_soundboard_slots p ON p.id = s.platform_slot_id"""


class SlotNotFound(Exception):
    pass


class SlotLocked(Exception):
    pass


class PlatformSlot(Exception):
    pass


class SlotHidden(Exception):
    pass


class OrderConflict(Exception):
    pass


class CapExceeded(Exception):
    pass


class KeyInUse(Exception):
    pass


class SwapLimited(Exception):
    def __init__(self, code: str, swaps_used: int, *, frees_at=None, cooldown_ends_at=None) -> None:
        super().__init__(code)
        self.code, self.swaps_used, self.frees_at, self.cooldown_ends_at = code, swaps_used, frees_at, cooldown_ends_at


def _map_db_error(exc: Exception) -> Exception:
    """Constraint violations raised by the database -> domain errors (anything else is re-raised)."""
    if isinstance(exc, pg_errors.UniqueViolation):
        return OrderConflict()
    if isinstance(exc, pg_errors.CheckViolation):
        constraint = exc.diag.constraint_name or ""
        return OrderConflict() if constraint else CapExceeded()  # the cap trigger raises without a constraint
    return exc


class SoundboardRepository:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def terms_state(self, streamer_id: str) -> dict | None:
        """Eligibility and the CURRENT version's acceptance (D3): {enabled, accepted_at, revoked_at} or None."""
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT s.soundboard_enabled AS enabled, t.accepted_at, t.revoked_at "
                              "FROM public.streamers s LEFT JOIN public.soundboard_tnc_acceptances t "
                              "ON t.streamer_id = s.id AND t.terms_version = %s WHERE s.id = %s",
                              (terms.VERSION, streamer_id))
            return await cur.fetchone()

    async def accept_terms(self, streamer_id: str, user_id: str, user_agent: str | None) -> bool:
        """Records the owner's acceptance of the current version. Idempotent (UNIQUE (streamer_id, terms_version)):
        an existing row, revoked or not, is never touched; older versions' rows stay as they are. True only when
        this call recorded it."""
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO public.soundboard_tnc_acceptances (streamer_slug, streamer_id, streamer_name, user_id,
                    user_email, terms_version, user_agent)
                SELECT s.streamer_slug, s.id, s.streamer_name, u.id, u.email::text, %s, %s
                FROM public.streamers s JOIN public.users u ON u.id = %s WHERE s.id = %s
                ON CONFLICT (streamer_id, terms_version) DO NOTHING
            """, (terms.VERSION, user_agent, user_id, streamer_id))
            return cur.rowcount == 1

    @staticmethod
    async def _lock(cur, streamer_id: str) -> None:
        await cur.execute(_LOCK, (f"hyperchat:soundboard:{streamer_id}",))

    @staticmethod
    async def _budget(cur, slug: str) -> dict:
        await cur.execute("SELECT count(*) AS used, min(swapped_at) + %s AS frees_at FROM public.soundboard_swap_log "
                          "WHERE streamer_slug = %s AND swapped_at > now() - %s", (SWAP_WINDOW, slug, SWAP_WINDOW))
        row = await cur.fetchone()
        await cur.execute("SELECT slot_index, max(swapped_at) + %s AS ends_at FROM public.soundboard_swap_log "
                          "WHERE streamer_slug = %s AND swapped_at > now() - %s GROUP BY slot_index ORDER BY slot_index",
                          (SLOT_COOLDOWN, slug, SLOT_COOLDOWN))
        cooldowns = {r["slot_index"]: r["ends_at"] for r in await cur.fetchall()}
        return {"used": row["used"], "frees_at": row["frees_at"] if row["used"] else None, "cooldowns": cooldowns}

    @staticmethod
    async def _keys_in_use(cur, keys: list[str]) -> bool:
        await cur.execute("""SELECT EXISTS (
            SELECT 1 FROM public.streamer_soundboard_slots WHERE audio_key = ANY(%(k)s) OR image_key = ANY(%(k)s)
            UNION ALL
            SELECT 1 FROM public.platform_soundboard_slots WHERE audio_key = ANY(%(k)s) OR image_key = ANY(%(k)s)
        ) AS used""", {"k": keys})
        return (await cur.fetchone())["used"]

    async def keys_in_use(self, keys: list[str]) -> bool:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            return await self._keys_in_use(cur, keys)

    # ── read ────────────────────────────────────────────────────────────────
    async def board(self, streamer_id: str, slug: str) -> tuple[list[dict], dict]:
        """Seeds the creator's rows from the platform set on first use (seed_streamer_soundboard), then
        returns (rows joined with their platform slot, budget)."""
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await self._lock(cur, streamer_id)
            await cur.execute("""
                INSERT INTO public.streamer_soundboard_slots (streamer_slug, platform_slot_id, source, is_visible,
                                                             order_index)
                SELECT %(s)s, p.id, 'platform', true, p.default_order_index FROM public.platform_soundboard_slots p
                WHERE NOT EXISTS (SELECT 1 FROM public.streamer_soundboard_slots WHERE streamer_slug = %(s)s)
                ORDER BY p.default_order_index""", {"s": slug})
            await cur.execute(_SLOTS + " WHERE s.streamer_slug = %s", (slug,))
            rows = await cur.fetchall()
            return rows, await self._budget(cur, slug)

    # ── PATCH: hide, update, reorder, unhide (apply_soundboard_changes) ─────
    async def apply_changes(self, streamer_id: str, slug: str, hide: list[str], update: list[tuple[str, str]],
                            reorder: list[tuple[str, int]], unhide: list[tuple[str, int]]) -> int:
        try:
            async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                await self._lock(cur, streamer_id)
                await cur.execute(_SLOTS + " WHERE s.streamer_slug = %s", (slug,))
                rows = {r["id"]: r for r in await cur.fetchall()}
                ids = set(hide) | {i for i, _ in update} | {i for i, _ in reorder} | {i for i, _ in unhide}
                if any(i not in rows for i in ids):
                    raise SlotNotFound()
                if any(rows[i]["is_locked"] for i in ids):
                    raise SlotLocked()  # locked platform sounds: no hide, rename, reorder or re-position
                if hide:
                    await cur.execute("UPDATE public.streamer_soundboard_slots SET is_visible = false, order_index = NULL "
                                      "WHERE streamer_slug = %s AND id = ANY(%s::uuid[])", (slug, hide))
                for slot_id, name in update:
                    await cur.execute("UPDATE public.streamer_soundboard_slots SET display_name = %s "
                                      "WHERE streamer_slug = %s AND id = %s", (name, slug, slot_id))
                if reorder:
                    hidden = set(hide) | {i for i, r in rows.items() if not r["is_visible"]}
                    if any(i in hidden for i, _ in reorder):
                        raise SlotHidden()
                    for step in ("-1 - %s", "%s"):  # placeholders first, so positions can be exchanged
                        for slot_id, order in reorder:
                            await cur.execute(f"UPDATE public.streamer_soundboard_slots SET order_index = {step} "
                                              "WHERE streamer_slug = %s AND id = %s", (order, slug, slot_id))
                for slot_id, order in unhide:
                    await cur.execute("UPDATE public.streamer_soundboard_slots SET is_visible = true, order_index = %s "
                                      "WHERE streamer_slug = %s AND id = %s", (order, slug, slot_id))
                await cur.execute("SELECT count(*) AS n FROM public.streamer_soundboard_slots "
                                  "WHERE streamer_slug = %s AND is_visible = true", (slug,))
                return (await cur.fetchone())["n"]
        except (pg_errors.UniqueViolation, pg_errors.CheckViolation) as exc:
            raise _map_db_error(exc) from None

    async def restore_defaults(self, streamer_id: str, slug: str) -> None:
        """restore_soundboard_defaults: custom sounds hidden (kept), platform overrides cleared, locked and
        swappable platform sounds shown at their default position."""
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            await self._lock(cur, streamer_id)
            await cur.execute("UPDATE public.streamer_soundboard_slots SET is_visible = false, order_index = NULL "
                              "WHERE streamer_slug = %s AND source = 'custom'", (slug,))
            await cur.execute("""UPDATE public.streamer_soundboard_slots SET is_visible = false, order_index = NULL,
                display_name = NULL, audio_key = NULL, audio_url = NULL, audio_duration_s = NULL, image_key = NULL,
                image_url = NULL, image_type = NULL WHERE streamer_slug = %s AND source = 'platform'""", (slug,))
            await cur.execute("""UPDATE public.streamer_soundboard_slots s SET is_visible = true,
                order_index = p.default_order_index FROM public.platform_soundboard_slots p
                WHERE s.streamer_slug = %s AND s.source = 'platform' AND s.platform_slot_id = p.id
                  AND (p.is_locked = true OR p.swappable_index IS NOT NULL)""", (slug,))

    # ── create / swap / replace / delete ────────────────────────────────────
    async def create_custom(self, streamer_id: str, slug: str, sound: dict) -> str:
        """A new custom sound, hidden until the creator places it (legacy "create" mode)."""
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await self._lock(cur, streamer_id)
            if await self._keys_in_use(cur, [sound["audio_key"], sound["image_key"]]):
                raise KeyInUse()
            await cur.execute("""INSERT INTO public.streamer_soundboard_slots (streamer_slug, source, display_name,
                audio_key, audio_url, audio_duration_s, image_key, image_url, image_type, is_visible, order_index)
                VALUES (%(slug)s, 'custom', %(display_name)s, %(audio_key)s, %(audio_url)s, %(audio_duration_s)s,
                        %(image_key)s, %(image_url)s, %(image_type)s::public.soundboard_image_type, false, NULL)
                RETURNING id::text AS id""", {**sound, "slug": slug})
            return (await cur.fetchone())["id"]

    async def swap(self, streamer_id: str, slug: str, index: int, sound: dict) -> dict:
        """swap_soundboard_slot. The platform sound at a position is found through the creator's row OR its
        platform slot (the position the dashboard shows), and hidden when the custom sound takes its place."""
        try:
            async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                await self._lock(cur, streamer_id)
                if await self._keys_in_use(cur, [sound["audio_key"], sound["image_key"]]):
                    raise KeyInUse()
                budget = await self._budget(cur, slug)
                if budget["used"] >= SWAP_BUDGET:
                    raise SwapLimited("budget_exhausted", budget["used"], frees_at=budget["frees_at"])
                if index in budget["cooldowns"]:
                    raise SwapLimited("slot_cooldown_active", budget["used"],
                                      cooldown_ends_at=budget["cooldowns"][index])
                await cur.execute("SELECT id::text AS id, audio_key, audio_url, image_key, image_url, order_index "
                                  "FROM public.streamer_soundboard_slots WHERE streamer_slug = %s AND source = 'custom' "
                                  "AND swappable_index = %s FOR UPDATE", (slug, index))
                existing = await cur.fetchone()
                await cur.execute("""SELECT s.id::text AS id, s.is_visible, s.order_index
                    FROM public.streamer_soundboard_slots s JOIN public.platform_soundboard_slots p ON p.id = s.platform_slot_id
                    WHERE s.streamer_slug = %s AND s.source = 'platform'
                      AND COALESCE(s.swappable_index, p.swappable_index) = %s FOR UPDATE OF s""", (slug, index))
                twin = await cur.fetchone()
                order = existing["order_index"] if existing else None
                if order is None and twin is not None and twin["is_visible"]:
                    # the custom sound takes the platform sound's place (a visible custom sound keeps its own)
                    order = twin["order_index"]
                    await cur.execute("UPDATE public.streamer_soundboard_slots SET is_visible = false, order_index = NULL "
                                      "WHERE id = %s", (twin["id"],))
                if order is None:
                    await cur.execute("SELECT COALESCE(max(order_index), -1) + 1 AS next FROM "
                                      "public.streamer_soundboard_slots WHERE streamer_slug = %s AND is_visible = true",
                                      (slug,))
                    order = (await cur.fetchone())["next"]
                params = {**sound, "slug": slug, "index": index, "order": order}
                if existing:
                    await cur.execute("""UPDATE public.streamer_soundboard_slots SET display_name = %(display_name)s,
                        audio_key = %(audio_key)s, audio_url = %(audio_url)s, audio_duration_s = %(audio_duration_s)s,
                        image_key = %(image_key)s, image_url = %(image_url)s,
                        image_type = %(image_type)s::public.soundboard_image_type, is_visible = true,
                        order_index = %(order)s, last_swapped_at = now() WHERE id = %(id)s""", {**params, "id": existing["id"]})
                    slot_id = existing["id"]
                    previous = [(existing["audio_key"], existing["audio_url"]), (existing["image_key"], existing["image_url"])]
                else:
                    await cur.execute("""INSERT INTO public.streamer_soundboard_slots (streamer_slug, source,
                        swappable_index, display_name, audio_key, audio_url, audio_duration_s, image_key, image_url,
                        image_type, is_visible, order_index, last_swapped_at)
                        VALUES (%(slug)s, 'custom', %(index)s, %(display_name)s, %(audio_key)s, %(audio_url)s,
                                %(audio_duration_s)s, %(image_key)s, %(image_url)s,
                                %(image_type)s::public.soundboard_image_type, true, %(order)s, now())
                        RETURNING id::text AS id""", params)
                    slot_id = (await cur.fetchone())["id"]
                    previous = []
                await cur.execute("INSERT INTO public.soundboard_swap_log (streamer_slug, slot_index) VALUES (%s, %s) "
                                  "RETURNING swapped_at + %s AS cooldown_ends_at", (slug, index, SLOT_COOLDOWN))
                ends = (await cur.fetchone())["cooldown_ends_at"]
                return {"slot_id": slot_id, "swaps_used": budget["used"] + 1, "cooldown_ends_at": ends,
                        "previous": previous}
        except (pg_errors.UniqueViolation, pg_errors.CheckViolation) as exc:
            raise _map_db_error(exc) from None

    async def replace(self, streamer_id: str, slug: str, slot_id: str, kind: str, sound: dict) -> tuple:
        """Replace a slot's audio or image (custom sounds, or an override on an unlocked platform sound).
        Returns the previous (key, url)."""
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await self._lock(cur, streamer_id)
            if await self._keys_in_use(cur, [sound["key"]]):
                raise KeyInUse()
            await cur.execute("""SELECT s.audio_key, s.audio_url, s.image_key, s.image_url,
                    COALESCE(p.is_locked, false) AS is_locked
                FROM public.streamer_soundboard_slots s LEFT JOIN public.platform_soundboard_slots p ON p.id = s.platform_slot_id
                WHERE s.id = %s AND s.streamer_slug = %s FOR UPDATE OF s""", (slot_id, slug))
            row = await cur.fetchone()
            if row is None:
                raise SlotNotFound()
            if row["is_locked"]:
                raise SlotLocked()
            if kind == "audio":
                await cur.execute("UPDATE public.streamer_soundboard_slots SET audio_key = %s, audio_url = %s, "
                                  "audio_duration_s = %s WHERE id = %s",
                                  (sound["key"], sound["url"], sound["duration_s"], slot_id))
                return row["audio_key"], row["audio_url"]
            await cur.execute("UPDATE public.streamer_soundboard_slots SET image_key = %s, image_url = %s, "
                              "image_type = %s::public.soundboard_image_type WHERE id = %s",
                              (sound["key"], sound["url"], sound["image_type"], slot_id))
            return row["image_key"], row["image_url"]

    async def delete_custom(self, streamer_id: str, slug: str, slot_id: str) -> list[tuple]:
        """Delete a custom sound (platform sounds can only be hidden). Returns its (key, url) pairs."""
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await self._lock(cur, streamer_id)
            await cur.execute("SELECT source::text AS source, audio_key, audio_url, image_key, image_url "
                              "FROM public.streamer_soundboard_slots WHERE id = %s AND streamer_slug = %s FOR UPDATE",
                              (slot_id, slug))
            row = await cur.fetchone()
            if row is None:
                raise SlotNotFound()
            if row["source"] != "custom":
                raise PlatformSlot()
            await cur.execute("DELETE FROM public.streamer_soundboard_slots WHERE id = %s", (slot_id,))
            return [(row["audio_key"], row["audio_url"]), (row["image_key"], row["image_url"])]
