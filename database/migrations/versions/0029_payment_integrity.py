"""Payment integrity evidence (C1): provider-initiated disputes and refunds, recorded append-only.

Revision ID: 0029
Revises: 0028

* payment_integrity_events: one row per signed Razorpay dispute / refund webhook event (payment.dispute.created,
  .under_review, .action_required, .won, .lost, .closed; refund.created, .processed, .failed). Ids, enums, amounts
  (paise), currency, reason code and timestamps only: never the payload, the payer's contact details or card data.
  match_type says how the event relates to a donation:
      payment  the donation's own completing payment (donations.provider_payment_id), signing account verified
      order    the donation's Razorpay order but another payment (e.g. a duplicate capture), account verified
      none     no donation found (donation_id NULL)
  Only `payment` rows may be attributed to a donation. Nothing here changes a donation, a donor total, a goal,
  the leaderboard or stats: a compensating accounting entry would be a separate, approved change.
* payment_integrity_current: the effective state per dispute / refund, computed, never stored. Order: phase rank,
  then status rank, then the provider's event time, then receipt time, then the row id (deterministic). A won
  chargeback that reopens in pre-arbitration is therefore open again; a late "created" never undoes a later state.
  `conflicting` marks a top state that another event of the same rank contradicts (e.g. won vs lost).
* payment_webhook_events: three new outcomes (integrity_recorded, integrity_unknown_payment, integrity_mismatch);
  donation_id ON DELETE SET NULL becomes RESTRICT (deleting a donation never silently unlinks its evidence).
* Both evidence tables are append-only for EVERY role, the owner included: UPDATE and DELETE raise; TRUNCATE
  (direct or cascaded) raises while the table holds a row (an empty table, e.g. a fresh cutover target, may be
  truncated). hyperchat_app keeps SELECT and INSERT only, hyperchat_worker has nothing, hyperchat_readonly SELECT.
  Only the table owner (migrations, the ETL) could disable the triggers; no runtime role owns them.

Existing rows are not modified, linked or backfilled. Downgrade refuses while any integrity row or any of the new
outcomes exists; otherwise it restores 0010's SET NULL foreign key, outcome list and the 0007 grants.
"""

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

OUTCOMES_0010 = ("completed", "failed", "already_paid", "already_failed", "ignored", "no_order", "unknown_order",
                 "account_mismatch", "amount_mismatch", "currency_mismatch", "payment_mismatch", "payment_not_found")
NEW_OUTCOMES = ("integrity_recorded", "integrity_unknown_payment", "integrity_mismatch")
EVENT_TYPES = ("payment.dispute.created", "payment.dispute.under_review", "payment.dispute.action_required",
               "payment.dispute.won", "payment.dispute.lost", "payment.dispute.closed",
               "refund.created", "refund.processed", "refund.failed")
EVIDENCE_TABLES = ("payment_webhook_events", "payment_integrity_events")


def _list(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _outcome_check(values) -> None:
    op.execute("ALTER TABLE public.payment_webhook_events DROP CONSTRAINT payment_webhook_events_outcome_check, "
               f"ADD CONSTRAINT payment_webhook_events_outcome_check CHECK (outcome IN ({_list(values)}))")


def upgrade() -> None:
    _outcome_check(OUTCOMES_0010 + NEW_OUTCOMES)
    op.execute("""
        ALTER TABLE public.payment_webhook_events
            DROP CONSTRAINT payment_webhook_events_donation_id_fkey,
            ADD CONSTRAINT payment_webhook_events_donation_id_fkey FOREIGN KEY (donation_id)
                REFERENCES public.donations(id) ON DELETE RESTRICT
    """)
    op.execute(f"""
        CREATE TABLE public.payment_integrity_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            webhook_event_id uuid NOT NULL UNIQUE
                REFERENCES public.payment_webhook_events(id) ON DELETE RESTRICT,
            donation_id uuid REFERENCES public.donations(id) ON DELETE RESTRICT,
            match_type text NOT NULL CHECK (match_type IN ('payment', 'order', 'none')),
            account text NOT NULL CHECK (account IN ('primary')),
            kind text NOT NULL CHECK (kind IN ('dispute', 'refund')),
            event_type text NOT NULL CHECK (event_type IN ({_list(EVENT_TYPES)})),
            provider_ref text NOT NULL CHECK (provider_ref ~ '^(disp|rfnd)_[A-Za-z0-9]{{1,40}}$'),
            provider_payment_id text NOT NULL CHECK (provider_payment_id ~ '^pay_[A-Za-z0-9]{{1,40}}$'),
            provider_order_id text CHECK (provider_order_id ~ '^order_[A-Za-z0-9]{{1,40}}$'),
            phase text CHECK (phase IN ('fraud', 'retrieval', 'chargeback', 'pre_arbitration', 'arbitration')),
            status text NOT NULL CHECK (status IN ('open', 'under_review', 'action_required', 'won', 'lost', 'closed',
                                                   'pending', 'processed', 'failed')),
            amount_paise bigint CHECK (amount_paise >= 0),
            currency text CHECK (currency ~ '^[A-Z]{{3}}$'),
            amount_deducted_paise bigint CHECK (amount_deducted_paise >= 0),
            reason_code text CHECK (char_length(reason_code) <= 100),
            respond_by timestamptz,
            provider_event_at timestamptz,
            consistent boolean NOT NULL,
            received_at timestamptz NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT payment_integrity_match CHECK ((match_type = 'none') = (donation_id IS NULL)),
            CONSTRAINT payment_integrity_kind_ref CHECK ((kind = 'dispute') = (provider_ref ~ '^disp_')
                                                       AND (kind = 'dispute') = (event_type ~ '^payment\\.dispute\\.')),
            CONSTRAINT payment_integrity_phase CHECK (kind = 'dispute' OR phase IS NULL),
            CONSTRAINT payment_integrity_status CHECK (
                (kind = 'dispute' AND status IN ('open', 'under_review', 'action_required', 'won', 'lost', 'closed'))
                OR (kind = 'refund' AND status IN ('pending', 'processed', 'failed')))
        )
    """)
    op.execute("CREATE INDEX payment_integrity_donation_idx ON public.payment_integrity_events (donation_id, received_at)")
    op.execute("CREATE INDEX payment_integrity_ref_idx ON public.payment_integrity_events (provider_ref, received_at)")
    op.execute("""
        CREATE FUNCTION public.payment_evidence_guard() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog AS $$
        DECLARE
            v_has_rows boolean;
        BEGIN
            IF TG_OP = 'TRUNCATE' THEN
                -- an empty evidence table may be truncated (a fresh cutover target); recorded evidence never is
                EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.%I)', TG_TABLE_SCHEMA, TG_TABLE_NAME) INTO v_has_rows;
                IF NOT v_has_rows THEN
                    RETURN NULL;
                END IF;
            END IF;
            RAISE EXCEPTION 'payment evidence in %.% is append-only (% refused)', TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.payment_evidence_guard() FROM PUBLIC")
    for table in EVIDENCE_TABLES:
        op.execute(f"CREATE TRIGGER {table}_guard BEFORE UPDATE OR DELETE ON public.{table} "
                   "FOR EACH ROW EXECUTE FUNCTION public.payment_evidence_guard()")
        op.execute(f"CREATE TRIGGER {table}_guard_truncate BEFORE TRUNCATE ON public.{table} "
                   "FOR EACH STATEMENT EXECUTE FUNCTION public.payment_evidence_guard()")
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON public.{table} FROM hyperchat_app")
        op.execute(f"REVOKE ALL ON public.{table} FROM hyperchat_worker")
    op.execute("""
        CREATE VIEW public.payment_integrity_current AS
        WITH ranked AS (
            SELECT e.*,
                   CASE e.phase WHEN 'arbitration' THEN 3 WHEN 'pre_arbitration' THEN 2 WHEN 'chargeback' THEN 1
                                ELSE 0 END AS phase_rank,
                   CASE e.status WHEN 'open' THEN 0 WHEN 'pending' THEN 0
                                 WHEN 'under_review' THEN 1 WHEN 'action_required' THEN 1 WHEN 'failed' THEN 1
                                 ELSE 2 END AS status_rank
            FROM public.payment_integrity_events e
        ), ordered AS (
            SELECT r.*,
                   row_number() OVER (PARTITION BY provider_ref ORDER BY phase_rank DESC, status_rank DESC,
                                      provider_event_at DESC NULLS LAST, received_at DESC, recorded_at DESC, id DESC) AS n,
                   min(status) OVER same_rank <> max(status) OVER same_rank AS conflicting,
                   count(*) OVER (PARTITION BY provider_ref) AS events,
                   bool_and(consistent) OVER (PARTITION BY provider_ref) AS all_consistent
            FROM ranked r
            WINDOW same_rank AS (PARTITION BY provider_ref, phase_rank, status_rank)
        )
        SELECT provider_ref, kind, account, provider_payment_id, provider_order_id, donation_id, match_type,
               phase, status, amount_paise, currency, amount_deducted_paise, reason_code, respond_by,
               provider_event_at, received_at, events, all_consistent, conflicting
        FROM ordered WHERE n = 1
    """)
    op.execute("REVOKE ALL ON public.payment_integrity_current FROM hyperchat_worker")
    op.execute("REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.payment_integrity_current FROM hyperchat_app")


def downgrade() -> None:
    op.execute(f"""
        DO $$
        DECLARE evidence bigint; outcomes bigint;
        BEGIN
            SELECT count(*) INTO evidence FROM public.payment_integrity_events;
            SELECT count(*) INTO outcomes FROM public.payment_webhook_events WHERE outcome IN ({_list(NEW_OUTCOMES)});
            IF evidence > 0 OR outcomes > 0 THEN
                RAISE EXCEPTION 'downgrade of 0029 stopped: % payment integrity row(s) and % webhook event(s) with a '
                                '0029 outcome would be lost (nothing was changed)', evidence, outcomes;
            END IF;
        END $$
    """)
    op.execute("DROP VIEW public.payment_integrity_current")
    op.execute("DROP TABLE public.payment_integrity_events")  # empty (checked above): its triggers go with it
    op.execute("DROP TRIGGER payment_webhook_events_guard ON public.payment_webhook_events")
    op.execute("DROP TRIGGER payment_webhook_events_guard_truncate ON public.payment_webhook_events")
    op.execute("DROP FUNCTION public.payment_evidence_guard()")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON public.payment_webhook_events TO hyperchat_app, hyperchat_worker")
    op.execute("""
        ALTER TABLE public.payment_webhook_events
            DROP CONSTRAINT payment_webhook_events_donation_id_fkey,
            ADD CONSTRAINT payment_webhook_events_donation_id_fkey FOREIGN KEY (donation_id)
                REFERENCES public.donations(id) ON DELETE SET NULL
    """)
    _outcome_check(OUTCOMES_0010)
