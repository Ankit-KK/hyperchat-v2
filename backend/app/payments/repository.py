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

from app.jobs.queue import enqueue
from app.payments import accounting, rules
from app.realtime import outbox

# V2 provenance (the one rule): created by the checkout API, which always writes payment_account and
# provider_order_id, and not imported (the ETL writes legacy_source_table and never payment_account; runtime roles
# cannot set or clear legacy_source_table, migration 0030). A row matching neither this nor the imported form, or both,
# is ambiguous: never reconciled, never reversed automatically. Migration 0032's accounting function checks the same.
V2_PROVENANCE = "d.legacy_source_table IS NULL AND d.payment_account IS NOT NULL AND d.provider_order_id IS NOT NULL"
LEGACY_PROVENANCE = "d.legacy_source_table IS NOT NULL AND d.payment_account IS NULL"

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
    paid_with: str | None = None  # already_paid: the payment id stored on the donation (read under the row lock)


class _Duplicate(Exception):
    """The webhook event was recorded by a concurrent delivery: roll back everything."""


class PaymentRepository:
    def __init__(self, connection_factory, vaani_creators: frozenset[str] = rules.VAANI_CREATORS,
                 accounting: bool = False) -> None:
        # accounting: PAY-15 evidence evaluation (HYPERCHAT_PAYMENT_ACCOUNTING_ENABLED, refused on at this stage)
        self._conn, self.vaani_creators, self.accounting = connection_factory, vaani_creators, accounting

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
                    moderation_status, voice_message_url, hypersound_url, media_url, media_type,
                    status_token_hash)
                VALUES (%(streamer_id)s, %(order_id)s, 'razorpay', %(provider_order_id)s, %(payment_account)s,
                    %(donor_name)s, %(amount)s, %(currency)s, %(amount_inr)s, %(message)s, 'pending', 'pending',
                    %(voice_message_url)s, %(hypersound_url)s, %(media_url)s, %(media_type)s,
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

    # ── reconciliation (Step G1) ────────────────────────────────────────────
    async def reconcile_candidates(self, min_age_seconds: int, max_age_seconds: int,
                                   limit: int) -> tuple[list[dict], dict]:
        """V2 pending/failed donations created between max_age and min_age ago, oldest first, at most `limit`,
        and the counts of everything NOT examined, in one read-only snapshot. V2 means created by the checkout
        API: payment_account set (create_donation always writes it) AND no legacy_source_table (the ETL writes
        that and never payment_account). A row matching neither or both is ambiguous: never eligible, counted."""
        v2, legacy = V2_PROVENANCE, LEGACY_PROVENANCE
        window = ("d.created_at <= now() - make_interval(secs => %(min)s) "
                  "AND d.created_at >= now() - make_interval(secs => %(max)s)")
        params = {"min": min_age_seconds, "max": max_age_seconds, "limit": limit}
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SET TRANSACTION READ ONLY")
            await cur.execute(f"""
                SELECT {_DONATION}, d.payment_account AS own_account FROM public.donations d
                JOIN public.streamers s ON s.id = d.streamer_id
                WHERE d.payment_status IN ('pending', 'failed') AND {v2} AND {window}
                ORDER BY d.created_at, d.id LIMIT %(limit)s
            """, params)
            rows = await cur.fetchall()
            await cur.execute(f"""
                SELECT count(*) FILTER (WHERE {v2} AND {window}) AS in_window,
                       count(*) FILTER (WHERE {v2} AND d.created_at > now() - make_interval(secs => %(min)s)) AS too_recent,
                       count(*) FILTER (WHERE {v2} AND d.created_at < now() - make_interval(secs => %(max)s)) AS too_old,
                       count(*) FILTER (WHERE {legacy}) AS legacy_excluded,
                       count(*) FILTER (WHERE NOT ({v2}) AND NOT ({legacy})) AS ambiguous_provenance
                FROM public.donations d WHERE d.payment_status IN ('pending', 'failed')
            """, params)
            counts = await cur.fetchone()
        counts["beyond_limit"] = max(counts.pop("in_window") - len(rows), 0)
        return rows, counts

    # ── webhook events ──────────────────────────────────────────────────────
    async def event_seen(self, event_id: str) -> bool:
        row = await self._one("SELECT 1 AS seen FROM public.payment_webhook_events "
                              "WHERE provider = 'razorpay' AND event_id = %s", (event_id,))
        return row is not None

    @staticmethod
    async def _insert_event(cur, event: WebhookEvent, outcome: str, donation_id: str | None) -> str | None:
        """The new event row's id, or None when the event id was already recorded."""
        await cur.execute("""
            INSERT INTO public.payment_webhook_events (event_id, event_type, account, provider_order_id,
                provider_payment_id, donation_id, outcome, received_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (provider, event_id) DO NOTHING RETURNING id
        """, (event.event_id, event.event_type, event.account, event.provider_order_id, event.provider_payment_id,
              donation_id, outcome, event.received_at))
        row = await cur.fetchone()
        return str(row["id"]) if row is not None else None

    async def record_event(self, event: WebhookEvent, outcome: str, donation_id: str | None = None) -> bool:
        """An event that changes no donation. False when the event id was already recorded."""
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            return await self._insert_event(cur, event, outcome, donation_id) is not None

    # ── integrity evidence (C1, migration 0029: append-only, never changes a donation) ─────
    async def integrity_target(self, payment_id: str, order_id: str | None) -> tuple[dict | None, str]:
        """(donation, match_type): the donation this payment completed ("payment"), else the donation of the
        payment's Razorpay order ("order": another payment of that order), else (None, "none")."""
        row = await self._one(f"SELECT {_DONATION} FROM public.donations d JOIN public.streamers s "
                              "ON s.id = d.streamer_id WHERE d.provider_payment_id = %s", (payment_id,))
        if row is not None:
            return row, "payment"
        if order_id is not None and (row := await self.by_provider_order(order_id)) is not None:
            return row, "order"
        return None, "none"

    async def record_integrity(self, event: WebhookEvent, outcome: str, evidence, donation_id: str | None,
                               match_type: str, consistent: bool) -> bool:
        """The webhook event and its evidence row in ONE transaction. False (nothing written) for a repeated event."""
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            if self.accounting and donation_id is not None:
                # PAY-15 6a lock order L-1: the accounting state row FIRST (a freeze waits for this transaction or
                # this transaction sees the freeze), then the donation BEFORE the evidence insert (the insert's
                # foreign key takes a share lock on it, and two deliveries upgrading that share lock would deadlock)
                await self._lock_accounting_state(cur)
                await cur.execute("SELECT 1 FROM public.donations WHERE id = %s FOR UPDATE", (donation_id,))
            event_row = await self._insert_event(cur, event, outcome, donation_id)
            if event_row is None:
                return False
            e = evidence
            await cur.execute("""
                INSERT INTO public.payment_integrity_events (webhook_event_id, donation_id, match_type, account, kind,
                    event_type, provider_ref, provider_payment_id, provider_order_id, phase, status, amount_paise,
                    currency, amount_deducted_paise, reason_code, respond_by, provider_event_at, consistent, received_at,
                    payment_amount_refunded_paise)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (event_row, donation_id, match_type, event.account, e.kind, e.event_type, e.provider_ref,
                  e.payment_id, e.order_id, e.phase, e.status, e.amount_paise, e.currency, e.amount_deducted_paise,
                  e.reason_code, e.respond_by, e.provider_event_at, consistent, event.received_at,
                  e.payment_amount_refunded_paise))
            if donation_id is not None:
                await self._settle_accounting(cur, donation_id)
            return True

    @staticmethod
    async def _lock_accounting_state(cur) -> dict:
        """PAY-15 6a, lock order L-1: the accounting state row FOR SHARE, as the FIRST lock of every transaction that
        may change accounting (before the donation, the donor total, jobs and the realtime outbox)."""
        # an owner function: FOR SHARE needs UPDATE privilege on the table, which the API role must not have
        await cur.execute("SELECT state, dispute_mode FROM public.payment_accounting_lock_state()")
        return await cur.fetchone()

    async def _settle_accounting(self, cur, donation_id: str) -> str | None:
        """PAY-15 stage 3 (migration 0032), only when accounting is enabled: evaluate the donation's CURRENT evidence
        (payment_integrity_current) with the PR 1 evaluator under the donation row lock, then apply the result through
        public.donation_accounting_apply, which re-derives a clear amount from the stored evidence it names (the
        references are not trusted) and lets the one credit writer move the donor total. Read after the lock: an
        evaluation committed later always sees every earlier delivery's evidence. Returns the function's outcome."""
        if not self.accounting:
            return None
        switch = await self._lock_accounting_state(cur)  # already held (L-1): re-read, no wait
        if switch["state"] != "active":
            return "inactive"  # off / frozen: nothing is evaluated (the database refuses it too)
        await cur.execute(f"""
            SELECT d.id::text AS id, d.streamer_id::text AS streamer_id, d.amount, d.amount_inr,
                   d.currency::text AS currency, d.payment_status, d.provider_payment_id, d.legacy_source_table,
                   ({V2_PROVENANCE}) AS v2
            FROM public.donations d WHERE d.id = %s FOR UPDATE
        """, (donation_id,))
        donation = await cur.fetchone()
        if donation is None or not donation["v2"]:
            return None  # PAY15-D8: imported or ambiguous provenance: reported, never reversed automatically
        await cur.execute("""
            SELECT kind, provider_ref, provider_payment_id, donation_id::text AS donation_id, match_type, phase,
                   status, amount_paise, currency, amount_deducted_paise, conflicting
            FROM public.payment_integrity_current WHERE donation_id = %s
        """, (donation_id,))
        rows = await cur.fetchall()
        result = accounting.evaluate(donation, rows)
        lost = any(r["donation_id"] == donation["id"] and donation["provider_payment_id"] is not None
                   and r["provider_payment_id"] == donation["provider_payment_id"]
                   and (r["kind"], r["status"]) == ("dispute", "lost") for r in rows)
        if switch["dispute_mode"] == "hold" and result.hold is None and lost:
            # refunds-only (PAY15 S6A-D3): a lost dispute is held, never applied (the database enforces it too)
            result = accounting.Evaluation(None, "H-DISPUTE-MODE", result.flags)
        # a clear result counts exactly the attributed processed refunds and lost disputes; the database checks it
        refs = [] if result.hold else sorted(
            r["provider_ref"] for r in rows
            if r["donation_id"] == donation["id"] and donation["provider_payment_id"] is not None
            and r["provider_payment_id"] == donation["provider_payment_id"]
            and (r["kind"], r["status"]) in (("refund", "processed"), ("dispute", "lost")))
        await cur.execute("SELECT public.donation_accounting_apply(%s, %s, %s, %s, %s) AS outcome",
                          (donation_id, result.reversed_paise, result.hold,
                           sorted(result.flags & {"F-BOTH", "F-OPEN"}), refs))
        outcome = (await cur.fetchone())["outcome"]
        if outcome in ("evaluation", "resolved"):  # the reversal changed: the writer may have moved the total
            await outbox.emit_totals_changed(cur, donation["streamer_id"])
        return outcome

    async def integrity_report(self, since) -> list[dict]:
        """Current state per dispute / refund (payment_integrity_current), oldest deadline first. Read-only."""
        async with self._conn() as conn:
            await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("""
                    SELECT c.provider_ref, c.kind, c.account, c.match_type, c.donation_id::text AS donation_id,
                           d.order_id, s.streamer_slug, c.phase, c.status, c.amount_paise, c.currency,
                           c.amount_deducted_paise, c.reason_code, c.respond_by, c.events, c.all_consistent,
                           c.conflicting, c.received_at,
                           r.applied_reversed_paise, r.hold AS accounting_hold, r.flags AS accounting_flags,
                           d.legacy_source_table IS NOT NULL AS imported
                    FROM public.payment_integrity_current c
                    LEFT JOIN public.donations d ON d.id = c.donation_id
                    LEFT JOIN public.streamers s ON s.id = d.streamer_id
                    LEFT JOIN public.donation_reversals r ON r.donation_id = c.donation_id
                    WHERE %(since)s::timestamptz IS NULL OR c.received_at >= %(since)s::timestamptz
                    ORDER BY c.respond_by NULLS LAST, c.received_at, c.provider_ref
                """, {"since": since})
                return await cur.fetchall()

    async def accounting_report(self) -> dict:
        """PAY-15 stage 5 (D6): the accounting state an operator reviews, read-only and complete (not limited by
        --since): held donations, F-BOTH review items, imported donations with evidence (D8, report only). Ids, enums
        and amounts only, never donor names or evidence payloads."""
        async with self._conn() as conn:
            await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("""
                    SELECT r.donation_id::text AS donation_id, d.order_id, s.streamer_slug, r.hold,
                           r.applied_reversed_paise, r.captured_paise, r.flags, r.evaluated_at
                    FROM public.donation_reversals r
                    JOIN public.donations d ON d.id = r.donation_id
                    JOIN public.streamers s ON s.id = d.streamer_id
                    WHERE r.hold IS NOT NULL OR 'F-BOTH' = ANY (r.flags)
                    ORDER BY r.evaluated_at, r.donation_id
                """)
                review = await cur.fetchall()
                await cur.execute("""
                    SELECT d.id::text AS donation_id, d.order_id, s.streamer_slug, count(*) AS evidence
                    FROM public.payment_integrity_current c
                    JOIN public.donations d ON d.id = c.donation_id
                    JOIN public.streamers s ON s.id = d.streamer_id
                    WHERE d.legacy_source_table IS NOT NULL
                    GROUP BY d.id, d.order_id, s.streamer_slug
                    ORDER BY d.order_id
                """)
                imported = await cur.fetchall()
        return {"held": [r for r in review if r["hold"] is not None],
                "review_both": [r for r in review if "F-BOTH" in (r["flags"] or [])],
                "imported_with_evidence": imported}

    # PAY-15 stage 5b (D9): per provider payment with evidence, the latest valid refund snapshot S (by the provider's
    # event time, then receipt / record time and id, as the current-state view ranks), and P / Q = the processed /
    # pending amounts of that payment's CURRENT refund references (one row per reference: the view). Diagnostic only.
    REFUND_SNAPSHOT_SQL = """
        WITH snapshots AS (
            SELECT e.provider_payment_id, e.payment_amount_refunded_paise AS refunded, e.provider_event_at,
                   row_number() OVER (PARTITION BY e.provider_payment_id
                                      ORDER BY e.provider_event_at DESC NULLS LAST, e.received_at DESC,
                                               e.recorded_at DESC, e.id DESC) AS n
            FROM public.payment_integrity_events e WHERE e.payment_amount_refunded_paise IS NOT NULL
        ), refunds AS (
            SELECT c.provider_payment_id,
                   COALESCE(sum(c.amount_paise) FILTER (WHERE c.status = 'processed'), 0)::bigint AS processed,
                   COALESCE(sum(c.amount_paise) FILTER (WHERE c.status = 'pending'), 0)::bigint AS pending
            FROM public.payment_integrity_current c WHERE c.kind = 'refund' GROUP BY c.provider_payment_id
        ), payments AS (
            SELECT DISTINCT provider_payment_id FROM public.payment_integrity_events
        )
        SELECT p.provider_payment_id, s.refunded AS snapshot_refunded_paise, s.provider_event_at AS snapshot_at,
               COALESCE(r.processed, 0) AS processed_refunds_paise, COALESCE(r.pending, 0) AS pending_refunds_paise,
               d.id::text AS donation_id, d.order_id, st.streamer_slug
        FROM payments p
        LEFT JOIN snapshots s ON s.provider_payment_id = p.provider_payment_id AND s.n = 1
        LEFT JOIN refunds r ON r.provider_payment_id = p.provider_payment_id
        LEFT JOIN public.donations d ON d.provider_payment_id = p.provider_payment_id
        LEFT JOIN public.streamers st ON st.id = d.streamer_id
        ORDER BY p.provider_payment_id
    """

    async def refund_snapshot_report(self) -> dict:
        """PAY-15 stage 5b (D9): every provider payment with evidence, classified (read-only, report only):
        consistent (S = P), pending_refunds (P < S <= P + Q), missing_refund_evidence (S > P + Q: Razorpay reports
        more refunded than our refund evidence, a likely missed webhook), snapshot_behind (S < P), no_snapshot.
        Ids, amounts and times only. Nothing here is read by any money path."""
        async with self._conn() as conn:
            await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(self.REFUND_SNAPSHOT_SQL)
                rows = await cur.fetchall()
        out = {k: [] for k in ("missing_refund_evidence", "pending_refunds", "snapshot_behind", "consistent",
                               "no_snapshot")}
        for row in rows:
            s, p, q = row["snapshot_refunded_paise"], row["processed_refunds_paise"], row["pending_refunds_paise"]
            if s is None:
                out["no_snapshot"].append(row)
            elif s == p:
                out["consistent"].append(row)
            elif s > p + q:
                out["missing_refund_evidence"].append(row)
            elif s > p:
                out["pending_refunds"].append(row)
            else:
                out["snapshot_behind"].append(row)
        return out

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
                if self.accounting:  # PAY-15 6a lock order L-1: the accounting state row before the donation
                    await self._lock_accounting_state(cur)
                await cur.execute("""
                    SELECT d.streamer_id::text AS streamer_id, d.donor_name, d.amount_inr, d.payment_status,
                           d.provider_payment_id, d.voice_message_url IS NOT NULL AS has_voice,
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
            return Applied("already_paid", "success", paid_with=row["provider_payment_id"])
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
            applied = await self._vaani(cur, donation_id, row, payment_id)
            await self._settle_accounting(cur, donation_id)  # evidence may have arrived before the capture
            return applied
        auto = rules.auto_approves(
            has_hypersound=row["has_hypersound"], has_voice=row["has_voice"],
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
        # Phase 11: moderator notification (pending: now; auto-approved: when its audio is due, as legacy). Only
        # donations completed from now on are ever enqueued: there is no scan of older unnotified rows.
        await enqueue(cur, "moderator_notify", donation_id, {"donation_id": donation_id},
                      delay_seconds=rules.AUDIO_DELAY_SECONDS if auto else 0)
        if auto:  # the donor total is credited by the donations credit writer (migration 0031), in this UPDATE
            # Phase 8: the audio job commits with the approval (or neither does).
            await enqueue(cur, "donation_audio", donation_id, {"donation_id": donation_id})
            await outbox.emit_totals_changed(cur, row["streamer_id"])
        await self._settle_accounting(cur, donation_id)  # evidence may have arrived before the capture
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
        await outbox.emit(cur, row["streamer_id"], "new-donation", subject_id=donation_id)
        await outbox.emit_totals_changed(cur, row["streamer_id"])
        await outbox.emit(cur, row["streamer_id"], "vaani-celebration", subject_id=donation_id)
        return Applied("completed", "success", True)
