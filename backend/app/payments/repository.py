"""SQL for payments (runs as hyperchat_app; DML only). Indexes used:

  creator payment settings   streamers primary key (+ streamer_payment_health primary key)     1 row
  donation by our order id   donations_order_id_key (unique)                                    1 row
  donation by Razorpay order order_lookup_razorpay_order_id_key (unique), else
                             donations_provider_order_idx (legacy rows outside order_lookup)    1 row
  webhook idempotency        payment_webhook_events_event_key (provider, event_id)              1 row

Completion (apply) is ONE transaction: the donation row is locked (FOR UPDATE), the transition decided on
the locked state, the donor total credited, the realtime events written (Phase 9 outbox) and, for a webhook,
its event row inserted. A second delivery, a confirm or a poll racing it waits on the lock, then sees the
committed state: a donation is completed and credited at most once, and one webhook event id is processed
at most once.
"""

from dataclasses import dataclass
from datetime import datetime

from psycopg.rows import dict_row

from app.db.donor_totals import change_donor_total
from app.jobs.queue import enqueue
from app.payments import rules
from app.realtime import outbox

_DONATION = """d.id::text AS id, d.order_id, d.provider_order_id, d.amount, d.currency::text AS currency,
    d.payment_status, d.status_token_hash, d.provider_payment_id,
    COALESCE(d.payment_account, s.razorpay_account) AS account, s.streamer_slug"""

HEALTH_MESSAGES = {  # streamer_payment_health.error_message: never a secret or a variable value
    "ok": None,
    "missing_key": "razorpay credentials are not configured",
    "invalid_key": "razorpay rejected the credentials",
    "network_error": "razorpay api unreachable",
}


@dataclass(frozen=True)
class WebhookEvent:
    event_id: str
    event_type: str
    account: str
    provider_order_id: str | None
    provider_payment_id: str | None
    received_at: datetime


@dataclass(frozen=True)
class Applied:
    result: str  # completed | failed | already_paid | already_failed | duplicate | unknown_order
    payment_status: str | None
    auto_approved: bool = False


class _Duplicate(Exception):
    """The webhook event was recorded by a concurrent delivery: roll back everything."""


class PaymentRepository:
    def __init__(self, connection_factory, vaani_creators: frozenset[str] = rules.VAANI_CREATORS) -> None:
        self._conn, self.vaani_creators = connection_factory, vaani_creators

    async def _one(self, sql: str, params: tuple | dict) -> dict | None:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()

    # ── checkout ────────────────────────────────────────────────────────────
    async def payment_settings(self, streamer_id: str) -> dict:
        return await self._one("""
            SELECT s.order_prefix, s.razorpay_account, h.status AS health_status, h.on_hold AS health_on_hold,
                   h.last_checked_at AS health_checked_at
            FROM public.streamers s LEFT JOIN public.streamer_payment_health h ON h.streamer_id = s.id
            WHERE s.id = %s
        """, (streamer_id,))

    async def record_health(self, streamer_id: str, status: str) -> None:
        await self._one("""
            INSERT INTO public.streamer_payment_health (streamer_id, status, error_message, on_hold, last_checked_at)
            VALUES (%(sid)s, %(status)s, %(message)s, %(hold)s, now())
            ON CONFLICT (streamer_id) DO UPDATE SET status = EXCLUDED.status,
                error_message = EXCLUDED.error_message, on_hold = EXCLUDED.on_hold, last_checked_at = now()
            RETURNING streamer_id
        """, {"sid": streamer_id, "status": status, "message": HEALTH_MESSAGES[status], "hold": status != "ok"})

    async def create_donation(self, row: dict) -> str:
        """The pending donation and its order_lookup row, together or not at all."""
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("""
                INSERT INTO public.donations (streamer_id, order_id, payment_provider, provider_order_id,
                    payment_account, donor_name, amount, currency, amount_inr, message, payment_status,
                    moderation_status, is_hyperemote, voice_message_url, hypersound_url, media_url, media_type,
                    status_token_hash)
                VALUES (%(streamer_id)s, %(order_id)s, 'razorpay', %(provider_order_id)s, %(payment_account)s,
                    %(donor_name)s, %(amount)s, %(currency)s, %(amount_inr)s, %(message)s, 'pending', 'pending',
                    false, %(voice_message_url)s, %(hypersound_url)s, %(media_url)s, %(media_type)s,
                    %(status_token_hash)s)
                RETURNING id::text AS id
            """, row)
            donation_id = (await cur.fetchone())["id"]
            await cur.execute("INSERT INTO public.order_lookup (razorpay_order_id, order_id, donation_id) "
                              "VALUES (%s, %s, %s)", (row["provider_order_id"], row["order_id"], donation_id))
            return donation_id

    # ── lookups ─────────────────────────────────────────────────────────────
    async def by_order_id(self, order_id: str) -> dict | None:
        return await self._one(f"SELECT {_DONATION} FROM public.donations d "
                               "JOIN public.streamers s ON s.id = d.streamer_id WHERE d.order_id = %s", (order_id,))

    async def by_provider_order(self, razorpay_order_id: str) -> dict | None:
        row = await self._one(f"""
            SELECT {_DONATION} FROM public.order_lookup o JOIN public.donations d ON d.id = o.donation_id
            JOIN public.streamers s ON s.id = d.streamer_id WHERE o.razorpay_order_id = %s
        """, (razorpay_order_id,))
        if row is not None:
            return row
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"SELECT {_DONATION} FROM public.donations d JOIN public.streamers s "
                              "ON s.id = d.streamer_id WHERE d.provider_order_id = %s LIMIT 2", (razorpay_order_id,))
            rows = await cur.fetchall()
        return rows[0] if len(rows) == 1 else None  # ambiguous legacy duplicates are never guessed

    # ── webhook events ──────────────────────────────────────────────────────
    async def event_seen(self, event_id: str) -> bool:
        row = await self._one("SELECT 1 AS seen FROM public.payment_webhook_events "
                              "WHERE provider = 'razorpay' AND event_id = %s", (event_id,))
        return row is not None

    @staticmethod
    async def _insert_event(cur, event: WebhookEvent, outcome: str, donation_id: str | None) -> bool:
        await cur.execute("""
            INSERT INTO public.payment_webhook_events (event_id, event_type, account, provider_order_id,
                provider_payment_id, donation_id, outcome, received_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (provider, event_id) DO NOTHING RETURNING id
        """, (event.event_id, event.event_type, event.account, event.provider_order_id, event.provider_payment_id,
              donation_id, outcome, event.received_at))
        return await cur.fetchone() is not None

    async def record_event(self, event: WebhookEvent, outcome: str, donation_id: str | None = None) -> bool:
        """An event that changes no donation. False when the event id was already recorded."""
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            return await self._insert_event(cur, event, outcome, donation_id)

    # ── completion ──────────────────────────────────────────────────────────
    async def apply(self, donation_id: str, outcome: str, payment_id: str | None,
                    event: WebhookEvent | None = None) -> Applied:
        """Apply one verified payment outcome ("captured", "failed" or a mismatch, which counts as failed).

            current \\ outcome   captured                          failed / mismatch
            pending             paid (+ auto-approve, credit, audio job)  failed, rejected
            failed              paid (+ auto-approve, credit)     unchanged
            success             unchanged (never failed again)    unchanged
        """
        try:
            async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("""
                    SELECT d.streamer_id::text AS streamer_id, d.donor_name, d.amount_inr, d.payment_status,
                           d.is_hyperemote, d.voice_message_url IS NOT NULL AS has_voice,
                           d.hypersound_url IS NOT NULL AS has_hypersound, d.media_url IS NOT NULL AS has_media,
                           s.moderation_mode, s.media_moderation_enabled, s.streamer_slug, d.message
                    FROM public.donations d JOIN public.streamers s ON s.id = d.streamer_id
                    WHERE d.id = %s FOR UPDATE OF d
                """, (donation_id,))
                row = await cur.fetchone()
                if row is None:  # deleted since it was looked up: nothing to complete
                    applied, donation_id = Applied("unknown_order", None), None
                else:
                    applied = await self._transition(cur, donation_id, row, outcome, payment_id)
                if event is not None:  # a mismatch is recorded as such, whatever it did to the donation
                    recorded = outcome if outcome in rules.MISMATCHES and row is not None else applied.result
                    if not await self._insert_event(cur, event, recorded, donation_id):
                        raise _Duplicate()
                return applied
        except _Duplicate:
            return Applied("duplicate", None)

    async def _transition(self, cur, donation_id: str, row: dict, outcome: str, payment_id: str | None) -> Applied:
        status = row["payment_status"]
        if status == "success":
            return Applied("already_paid", "success")
        if outcome != "captured":
            if status == "failed":
                return Applied("already_failed", "failed")
            await cur.execute("UPDATE public.donations SET payment_status = 'failed', moderation_status = 'rejected' "
                              "WHERE id = %s", (donation_id,))
            return Applied("failed", "failed")
        vaani = rules.is_vaani_celebration(
            slug=row["streamer_slug"], message=row["message"], has_voice=row["has_voice"],
            has_hypersound=row["has_hypersound"], has_media=row["has_media"], creators=self.vaani_creators)
        if vaani:
            return await self._vaani(cur, donation_id, row, payment_id)
        auto = rules.auto_approves(
            is_hyperemote=row["is_hyperemote"], has_hypersound=row["has_hypersound"], has_voice=row["has_voice"],
            has_media=row["has_media"], moderation_mode=row["moderation_mode"],
            media_moderation_enabled=row["media_moderation_enabled"])
        await cur.execute("""
            UPDATE public.donations SET payment_status = 'success', provider_payment_id = %(pid)s,
                moderation_status = CASE WHEN %(auto)s THEN 'auto_approved' ELSE 'pending' END,
                approved_at = CASE WHEN %(auto)s THEN now() END,
                approved_by_label = CASE WHEN %(auto)s THEN 'system' END,
                approved_by_moderator_id = NULL,
                audio_scheduled_at = CASE WHEN %(auto)s THEN now() + make_interval(secs => %(delay)s) END
            WHERE id = %(id)s
        """, {"pid": payment_id, "auto": auto, "delay": rules.AUDIO_DELAY_SECONDS, "id": donation_id})
        # Phase 9: the dashboard event commits with the payment (or neither does).
        await outbox.emit(cur, row["streamer_id"], "new-donation", subject_id=donation_id)
        if auto:
            await change_donor_total(cur, row["streamer_id"], row["donor_name"], row["amount_inr"], +1)
            # Phase 8: the audio job commits with the approval (or neither does).
            await enqueue(cur, "donation_audio", donation_id, {"donation_id": donation_id})
            await outbox.emit_totals_changed(cur, row["streamer_id"])
        return Applied("completed", "success", auto)

    @staticmethod
    async def _vaani(cur, donation_id: str, row: dict, payment_id: str | None) -> Applied:
        """Phase 10 (P10-D13): the legacy "!vaani" celebration, on every completion path (legacy: webhook only).
        Auto-approved whatever the moderation mode and credited like any approved donation (goal, leaderboard),
        but no audio: no audio_scheduled_at (never queued), no donation_audio job. The overlay plays the
        celebration on `vaani-celebration` (private alerts channel), written in this transaction."""
        await cur.execute("""
            UPDATE public.donations SET payment_status = 'success', provider_payment_id = %s,
                moderation_status = 'auto_approved', approved_at = now(), approved_by_label = 'system',
                approved_by_moderator_id = NULL, audio_scheduled_at = NULL
            WHERE id = %s
        """, (payment_id, donation_id))
        await change_donor_total(cur, row["streamer_id"], row["donor_name"], row["amount_inr"], +1)
        await outbox.emit(cur, row["streamer_id"], "new-donation", subject_id=donation_id)
        await outbox.emit_totals_changed(cur, row["streamer_id"])
        await outbox.emit(cur, row["streamer_id"], "vaani-celebration", subject_id=donation_id)
        return Applied("completed", "success", True)
