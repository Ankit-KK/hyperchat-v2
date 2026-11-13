"""PAY-15 (D17) stage 3: per-donation reversal state, its append-only history and the reversal-aware credit writer.

Revision ID: 0032
Revises: 0031

Refunds and lost disputes are recorded as evidence since 0029 (PAY-13) and evaluated by app/payments/accounting.py
(PR 1). This revision lets an evaluation change money, through the one writer of migration 0031, without any new
writer of donor totals:

* donation_reversals      one row per evaluated V2 donation (none is created here: no row = nothing reversed):
                          applied_reversed_paise (cumulative, in the payment currency's smallest unit, 0..captured),
                          the current hold (H-CONFLICT, H-CURRENCY, H-NO-DEDUCTION, H-OVER-REFUND; the reversal is
                          frozen while held) and the review flags (F-BOTH, F-OPEN). Runtime roles: SELECT only; it
                          is written only by public.donation_accounting_apply() (privileges, no session flag).
* donations_credit_writer target = paid AND approved ? ceiling - floor(ceiling * reversed / captured) : 0, where
                          ceiling = COALESCE(creditable_inr, amount_inr). With no reversal (every row at this
                          revision) it is exactly 0031's rule. Runtime roles may also no longer change the checkout
                          provenance (payment_account, provider_order_id: NULL to a value, a value to NULL or to
                          another value), which nothing at runtime writes after the checkout INSERT. Everything else
                          is unchanged (credit columns, fixed financial fields, fail-closed debit HC002, delete
                          guard); the owner (migrations, the ETL) keeps writing them.
* donation_credit_events  gains reversed_before_paise / reversed_after_paise, hold_before / hold_after,
                          evidence_refs and txid (the transaction: an accounting row and the writer row that moved
                          the money share it). New causes: evaluation (the reversal changed, no hold), hold (a hold
                          started or changed: the reversal is frozen), resolved (a hold was lifted by clear
                          evidence); manual, activation and revert are reserved for the stage-6 tools. Accounting
                          rows never move money themselves (delta 0): the total moves in the writer's own row. Rows
                          written by 0031 stay valid (their new columns are NULL).
* donation_accounting_apply(donation, reversed_paise | NULL, hold | NULL, flags, evidence_refs)
                          SECURITY DEFINER, EXECUTE for hyperchat_app only (the API and payment-reconcile). Locks the
                          donation first (the moderation lock order), accepts only V2 checkout donations (the
                          reconciliation rule: no legacy_source_table, payment_account and provider_order_id set;
                          imported and ambiguous rows refused, D8), and for a clear result re-derives the amount
                          from ALL current evidence attributed to this donation AND its completing payment: refused
                          while any item must be held (conflicting, a currency mismatch, a non-INR or deduction-less
                          loss) or while evidence matched by payment names another payment (the evaluator's
                          attribution conflict); the named references must be exactly the
                          counted items (processed refunds, lost disputes) and the reversal exactly max(their
                          refunds, their deductions), never above captured. The references are therefore never
                          authority: they cannot raise, lower or remove a reversal the evidence does not support.
                          A hold applies nothing and names no evidence. Nothing changed: nothing written.

Evaluation runs only when HYPERCHAT_PAYMENT_ACCOUNTING_ENABLED is on, which the API refuses at this stage: deploying
this revision changes no figure. Downgrade refuses once any reversal row or accounting history exists.
"""

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

RUNTIME_ROLES = ("('hyperchat_app', 'hyperchat_worker', 'hyperchat_auth', 'hyperchat_lifecycle', 'hyperchat_stepup', "
                 "'hyperchat_readonly')")
EVERYONE = ("hyperchat_app, hyperchat_worker, hyperchat_auth, hyperchat_lifecycle, hyperchat_readonly, "
            "hyperchat_stepup, PUBLIC")
WRITERS = "hyperchat_app, hyperchat_worker"
HOLDS = "('H-CONFLICT', 'H-CURRENCY', 'H-NO-DEDUCTION', 'H-OVER-REFUND')"
CAUSES_0031 = "('backfill', 'writer')"
CAUSES = "('backfill', 'writer', 'evaluation', 'hold', 'resolved', 'manual', 'activation', 'revert')"
ACCOUNTING_CAUSES = "('evaluation', 'hold', 'resolved', 'manual', 'activation', 'revert')"


def _writer(reversal: bool) -> str:
    """donations_credit_writer: 0031's body, plus (reversal=True) the reversal term and the ledger's new columns."""
    declare = """
            v_rev bigint;
            v_hold text;
            v_ceiling numeric(12,2);""" if reversal else ""
    target = """
            SELECT r.applied_reversed_paise, r.hold INTO v_rev, v_hold
                FROM public.donation_reversals r WHERE r.donation_id = NEW.id;
            v_rev := COALESCE(v_rev, 0);
            IF NEW.payment_status = 'success' AND NEW.moderation_status IN ('approved', 'auto_approved') THEN
                v_ceiling := COALESCE(NEW.creditable_inr, NEW.amount_inr);
                v_target := v_ceiling;
                IF v_rev > 0 THEN  -- PAY15-D4: pro rata of the ceiling on the cumulative reversal, rounded down
                    v_target := v_ceiling - floor(v_ceiling * 100 * v_rev / ((NEW.amount * 100)::bigint)) / 100;
                END IF;
            ELSE
                v_target := 0;
            END IF;""" if reversal else """
            v_target := CASE WHEN NEW.payment_status = 'success' AND NEW.moderation_status IN ('approved', 'auto_approved')
                             THEN COALESCE(NEW.creditable_inr, NEW.amount_inr) ELSE 0 END;"""
    # 0032: the checkout provenance (payment_account, provider_order_id) is fixed for runtime roles too, so no
    # runtime UPDATE can make an ambiguous or imported row look like a V2 checkout donation (or the reverse)
    fixed_new = "NEW.amount, NEW.amount_inr, NEW.currency, NEW.streamer_id, NEW.donor_name" + (
        ", NEW.payment_account, NEW.provider_order_id" if reversal else "")
    fixed_old = fixed_new.replace("NEW.", "OLD.")
    fixed_what = ("amount, currency, creator, donor, checkout provenance, payment id and paid status" if reversal
                  else "amount, currency, creator, donor, payment id and paid status")
    ledger_cols = ", reversed_before_paise, reversed_after_paise, hold_before, hold_after" if reversal else ""
    ledger_vals = ", v_rev, v_rev, v_hold, v_hold" if reversal else ""
    return f"""
        CREATE OR REPLACE FUNCTION public.donations_credit_writer() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        DECLARE
            v_runtime boolean := session_user IN {RUNTIME_ROLES};
            v_before numeric(12,2) := CASE WHEN TG_OP = 'INSERT' THEN 0 ELSE OLD.credited_inr END;
            v_target numeric(12,2);
            v_delta numeric(12,2);
            v_count smallint;
            v_total numeric(12,2);
            v_name text := lower(NEW.donor_name);{declare}
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.credited_inr <> 0 OR NEW.creditable_inr IS NOT NULL THEN
                    RAISE EXCEPTION 'donations.credited_inr / creditable_inr are set by the credit writer only'
                        USING ERRCODE = 'insufficient_privilege';
                END IF;
            ELSIF NEW.credited_inr IS DISTINCT FROM OLD.credited_inr
                  OR NEW.creditable_inr IS DISTINCT FROM OLD.creditable_inr THEN
                -- every role, the owner included: only public.donations_credit_backfill() (which disables this
                -- trigger for its own UPDATE) sets them
                RAISE EXCEPTION 'donations.credited_inr / creditable_inr are set by the credit writer only (and once '
                                'by public.donations_credit_backfill())' USING ERRCODE = 'insufficient_privilege';
            ELSIF v_runtime AND (
                    ({fixed_new})
                        IS DISTINCT FROM ({fixed_old})
                    OR (OLD.provider_payment_id IS NOT NULL
                        AND NEW.provider_payment_id IS DISTINCT FROM OLD.provider_payment_id)
                    OR (OLD.payment_status = 'success' AND NEW.payment_status <> 'success')) THEN
                -- financial attribution is fixed at checkout; the payment id is set once (pending / failed ->
                -- success) and a paid donation stays paid. No runtime code path changes any of these.
                RAISE EXCEPTION 'donation {fixed_what} are fixed '
                                'for runtime roles' USING ERRCODE = 'insufficient_privilege';
            ELSIF OLD.credited_inr > 0 AND (NEW.streamer_id IS DISTINCT FROM OLD.streamer_id
                                            OR lower(NEW.donor_name) IS DISTINCT FROM lower(OLD.donor_name)) THEN
                RAISE EXCEPTION 'a credited donation cannot move to another donor total'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
{target}
            v_delta := v_target - v_before;
            NEW.credited_inr := v_before;
            IF v_delta = 0 THEN
                RETURN NEW;
            END IF;
            v_count := CASE WHEN v_before = 0 THEN 1 WHEN v_target = 0 THEN -1 ELSE 0 END;
            IF v_delta > 0 THEN
                INSERT INTO public.streamer_donator_totals AS t
                        (streamer_id, donator_name, donator_name_lower, total_amount, donation_count)
                    VALUES (NEW.streamer_id, NEW.donor_name, v_name, v_delta, GREATEST(v_count, 0))
                    ON CONFLICT (streamer_id, donator_name_lower) DO UPDATE SET
                        total_amount = t.total_amount + EXCLUDED.total_amount,
                        donation_count = GREATEST(0, t.donation_count + v_count),
                        donator_name = EXCLUDED.donator_name, updated_at = now()
                    RETURNING t.total_amount INTO v_total;
            ELSE
                UPDATE public.streamer_donator_totals t
                    SET total_amount = t.total_amount + v_delta,
                        donation_count = GREATEST(0, t.donation_count + v_count), updated_at = now()
                    WHERE t.streamer_id = NEW.streamer_id AND t.donator_name_lower = v_name
                      AND t.total_amount >= -v_delta
                    RETURNING t.total_amount INTO v_total;
                IF NOT FOUND THEN  -- fail closed: never floor, never lose the exact amount
                    RAISE EXCEPTION 'donor total would go below zero (debit % of donation %)', -v_delta, NEW.id
                        USING ERRCODE = 'HC002';
                END IF;
            END IF;
            INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause,
                    payment_status, moderation_status, credited_before, credited_after, delta_inr, delta_count,
                    total_after, actor{ledger_cols})
                VALUES (NEW.id, NEW.streamer_id, v_name, 'writer', NEW.payment_status, NEW.moderation_status,
                        v_before, v_target, v_delta, v_count, v_total, session_user{ledger_vals});
            NEW.credited_inr := v_target;
            RETURN NEW;
        END $$
    """


def upgrade() -> None:
    # ── the ledger: new columns (NULL on 0031's rows) and causes; 0031's rows satisfy every new CHECK ──
    op.execute("""
        ALTER TABLE public.donation_credit_events
            ADD COLUMN reversed_before_paise bigint,
            ADD COLUMN reversed_after_paise bigint,
            ADD COLUMN hold_before text,
            ADD COLUMN hold_after text,
            ADD COLUMN evidence_refs text[],
            ADD COLUMN txid bigint
    """)
    op.execute("ALTER TABLE public.donation_credit_events ALTER COLUMN txid SET DEFAULT txid_current()")
    op.execute("ALTER TABLE public.donation_credit_events DROP CONSTRAINT donation_credit_events_cause_check, "
               "DROP CONSTRAINT donation_credit_events_moves")
    op.execute(f"""
        ALTER TABLE public.donation_credit_events
            ADD CONSTRAINT donation_credit_events_cause_check CHECK (cause IN {CAUSES}),
            ADD CONSTRAINT donation_credit_events_moves CHECK (
                delta_inr <> 0 OR delta_count <> 0
                OR reversed_before_paise IS DISTINCT FROM reversed_after_paise OR hold_before IS DISTINCT FROM hold_after),
            ADD CONSTRAINT donation_credit_events_reversal CHECK (
                (reversed_before_paise IS NULL) = (reversed_after_paise IS NULL)
                AND (reversed_before_paise IS NULL OR (reversed_before_paise >= 0 AND reversed_after_paise >= 0))),
            ADD CONSTRAINT donation_credit_events_holds CHECK (
                (hold_before IS NULL OR hold_before IN {HOLDS}) AND (hold_after IS NULL OR hold_after IN {HOLDS})),
            ADD CONSTRAINT donation_credit_events_accounting CHECK (
                cause NOT IN {ACCOUNTING_CAUSES}
                OR (delta_inr = 0 AND delta_count = 0 AND credited_before = credited_after AND total_after IS NULL
                    AND reversed_before_paise IS NOT NULL)),
            ADD CONSTRAINT donation_credit_events_hold_cause CHECK (
                (cause <> 'hold' OR (hold_after IS NOT NULL AND reversed_after_paise = reversed_before_paise
                                     AND evidence_refs IS NULL))
                AND (cause <> 'resolved' OR (hold_before IS NOT NULL AND hold_after IS NULL))
                AND (cause <> 'evaluation' OR (hold_before IS NULL AND hold_after IS NULL)))
    """)

    # ── reversal state ──
    op.execute(f"""
        CREATE TABLE public.donation_reversals (
            donation_id uuid PRIMARY KEY REFERENCES public.donations(id) ON DELETE RESTRICT,
            captured_paise bigint NOT NULL CHECK (captured_paise > 0),
            applied_reversed_paise bigint NOT NULL DEFAULT 0,
            hold text CHECK (hold IN {HOLDS}),
            flags text[] NOT NULL DEFAULT '{{}}' CHECK (flags <@ ARRAY['F-BOTH', 'F-OPEN']::text[]),
            evaluated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT donation_reversals_bounds
                CHECK (applied_reversed_paise >= 0 AND applied_reversed_paise <= captured_paise)
        )
    """)
    op.execute(f"REVOKE ALL ON public.donation_reversals FROM {WRITERS}")  # 0007's default privileges granted DML
    op.execute(f"GRANT SELECT ON public.donation_reversals TO {WRITERS}")

    op.execute(_writer(reversal=True))

    op.execute(f"""
        CREATE FUNCTION public.donation_accounting_apply(p_donation uuid, p_reversed bigint, p_hold text,
                                                         p_flags text[], p_refs text[])
        RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            d record;
            v_refs text[] := COALESCE(p_refs, '{{}}');
            v_flags text[] := COALESCE(p_flags, '{{}}');
            v_captured bigint;
            v_found boolean;
            v_before bigint;
            v_hold_before text;
            v_flags_before text[];
            v_after bigint;
            v_hold_after text;
            v_counted bigint;
            v_named bigint;
            v_refunds bigint;
            v_lost bigint;
            v_cause text;
        BEGIN
            -- the donation first: the lock order of payment completion and moderation (no deadlock)
            SELECT x.id, x.streamer_id, x.donor_name, x.amount, x.currency::text AS currency, x.payment_status,
                   x.moderation_status, x.provider_payment_id, x.legacy_source_table, x.credited_inr,
                   x.payment_account, x.provider_order_id
                INTO d FROM public.donations x WHERE x.id = p_donation FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'no donation %', p_donation USING ERRCODE = 'invalid_parameter_value';
            END IF;
            -- V2 provenance, the reconciliation rule (app/payments/repository.py V2_PROVENANCE): created by the
            -- checkout (payment_account and provider_order_id set) and not imported. Imported and ambiguous rows are
            -- never reversed automatically (PAY15-D8).
            IF NOT (d.legacy_source_table IS NULL AND d.payment_account IS NOT NULL
                    AND d.provider_order_id IS NOT NULL) THEN
                RAISE EXCEPTION 'only V2 checkout donations are reversed automatically (imported or ambiguous '
                                'provenance, PAY15-D8)' USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF (p_hold IS NULL) = (p_reversed IS NULL) THEN
                RAISE EXCEPTION 'exactly one of a clear reversal or a hold' USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF p_hold IS NOT NULL AND p_hold NOT IN {HOLDS} THEN
                RAISE EXCEPTION 'unknown hold %', p_hold USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF NOT (v_flags <@ ARRAY['F-BOTH', 'F-OPEN']::text[]) OR array_position(v_flags, NULL) IS NOT NULL THEN
                RAISE EXCEPTION 'unknown flag' USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF cardinality(v_refs) <> (SELECT count(DISTINCT r) FROM unnest(v_refs) AS r WHERE r IS NOT NULL) THEN
                RAISE EXCEPTION 'duplicate or empty evidence reference' USING ERRCODE = 'invalid_parameter_value';
            END IF;
            v_captured := (d.amount * 100)::bigint;
            SELECT r.applied_reversed_paise, r.hold, r.flags INTO v_before, v_hold_before, v_flags_before
                FROM public.donation_reversals r WHERE r.donation_id = p_donation;
            v_found := FOUND;
            v_before := COALESCE(v_before, 0);

            IF p_hold IS NOT NULL THEN
                -- F10: a hold changes no applied reversal, in either direction, and names no evidence
                IF cardinality(v_refs) > 0 THEN
                    RAISE EXCEPTION 'a hold applies nothing: it names no evidence' USING ERRCODE = 'invalid_parameter_value';
                END IF;
                v_after := v_before;
                v_hold_after := p_hold;
            ELSE
                IF p_reversed < 0 OR p_reversed > v_captured THEN
                    RAISE EXCEPTION 'a reversal is between 0 and the captured amount' USING ERRCODE = 'invalid_parameter_value';
                END IF;
                IF p_reversed > 0 AND d.payment_status <> 'success' THEN
                    RAISE EXCEPTION 'an unpaid donation has nothing to reverse' USING ERRCODE = 'invalid_parameter_value';
                END IF;
                -- The references are not trusted. Over ALL current evidence attributed to this donation AND its
                -- completing payment: a clear result is refused while an item that must be held exists (a conflicting
                -- state; a processed refund or lost dispute in another currency, a non-INR dispute, a loss without a
                -- deduction); the named references must be exactly the counted items, and the reversal exactly
                -- max(sum of the processed refunds, sum of the lost deductions) (never more, never less: an
                -- applied reversal cannot be removed by naming less).
                -- Attribution (as app/payments/accounting.py): evidence matched to this donation BY PAYMENT
                -- (match_type 'payment') that names another payment id is a conflict: no clear result. Evidence
                -- matched only by the order (a second payment of the order) is not this donation's money: ignored.
                IF EXISTS (
                    SELECT 1 FROM public.payment_integrity_current c
                    WHERE c.donation_id = d.id AND c.match_type = 'payment'
                      AND c.provider_payment_id IS DISTINCT FROM d.provider_payment_id) THEN
                    RAISE EXCEPTION 'evidence matched to this donation by payment names another payment: no clear '
                                    'reversal' USING ERRCODE = 'insufficient_privilege';
                END IF;
                IF EXISTS (
                    SELECT 1 FROM public.payment_integrity_current c
                    WHERE c.donation_id = d.id AND c.provider_payment_id = d.provider_payment_id
                      AND (c.conflicting
                           OR (c.kind = 'refund' AND c.status = 'processed'
                               AND (c.amount_paise IS NULL OR c.currency IS DISTINCT FROM d.currency))
                           OR (c.kind = 'dispute' AND c.status = 'lost'
                               AND (COALESCE(c.amount_deducted_paise, 0) = 0 OR c.phase IS NULL
                                    OR c.currency IS DISTINCT FROM 'INR' OR d.currency <> 'INR')))) THEN
                    RAISE EXCEPTION 'this payment has evidence that must be held: no clear reversal'
                        USING ERRCODE = 'insufficient_privilege';
                END IF;
                SELECT count(*), count(*) FILTER (WHERE c.provider_ref = ANY (v_refs)),
                       COALESCE(sum(c.amount_paise) FILTER (WHERE c.kind = 'refund'), 0),
                       COALESCE(sum(c.amount_deducted_paise) FILTER (WHERE c.kind = 'dispute'), 0)
                    INTO v_counted, v_named, v_refunds, v_lost
                    FROM public.payment_integrity_current c
                    WHERE c.donation_id = d.id AND c.provider_payment_id = d.provider_payment_id
                      AND ((c.kind = 'refund' AND c.status = 'processed') OR (c.kind = 'dispute' AND c.status = 'lost'));
                IF v_counted <> cardinality(v_refs) OR v_named <> cardinality(v_refs)
                   OR p_reversed <> GREATEST(v_refunds, v_lost) THEN
                    RAISE EXCEPTION 'the reversal must equal max(processed refunds, lost deductions) over exactly the '
                                    'counted evidence of this payment, all of it named' USING ERRCODE = 'insufficient_privilege';
                END IF;
                v_after := p_reversed;
                v_hold_after := NULL;
            END IF;

            IF v_after = v_before AND v_hold_after IS NOT DISTINCT FROM v_hold_before THEN  -- F2: a no-op
                IF v_found AND v_flags IS DISTINCT FROM v_flags_before THEN
                    UPDATE public.donation_reversals SET flags = v_flags, evaluated_at = now()
                        WHERE donation_id = p_donation;
                ELSIF NOT v_found AND cardinality(v_flags) > 0 THEN
                    INSERT INTO public.donation_reversals (donation_id, captured_paise, flags)
                        VALUES (p_donation, v_captured, v_flags);
                END IF;
                RETURN 'unchanged';
            END IF;

            INSERT INTO public.donation_reversals AS r (donation_id, captured_paise, applied_reversed_paise, hold, flags)
                VALUES (p_donation, v_captured, v_after, v_hold_after, v_flags)
                ON CONFLICT (donation_id) DO UPDATE SET applied_reversed_paise = EXCLUDED.applied_reversed_paise,
                    hold = EXCLUDED.hold, flags = EXCLUDED.flags, evaluated_at = now();
            v_cause := CASE WHEN v_hold_after IS NOT NULL THEN 'hold'
                            WHEN v_hold_before IS NOT NULL THEN 'resolved' ELSE 'evaluation' END;
            INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause,
                    payment_status, moderation_status, credited_before, credited_after, delta_inr, delta_count,
                    total_after, actor, reversed_before_paise, reversed_after_paise, hold_before, hold_after,
                    evidence_refs)
                VALUES (d.id, d.streamer_id, lower(d.donor_name), v_cause, d.payment_status, d.moderation_status,
                        d.credited_inr, d.credited_inr, 0, 0, NULL, session_user, v_before, v_after, v_hold_before,
                        v_hold_after, CASE WHEN v_cause = 'hold' THEN NULL ELSE v_refs END);
            IF v_after <> v_before THEN
                -- the one writer (0031) recomputes the target from the new reversal and moves the total, in its own
                -- ledger row of this transaction (same txid)
                UPDATE public.donations SET updated_at = now() WHERE id = d.id;
            END IF;
            RETURN v_cause;
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.donation_accounting_apply(uuid, bigint, text, text[], text[]) "
               f"FROM {EVERYONE}")
    op.execute("GRANT EXECUTE ON FUNCTION public.donation_accounting_apply(uuid, bigint, text, text[], text[]) "
               "TO hyperchat_app")


def downgrade() -> None:
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.donation_reversals)
               OR EXISTS (SELECT 1 FROM public.donation_credit_events WHERE cause IN {ACCOUNTING_CAUSES}) THEN
                RAISE EXCEPTION 'downgrade of 0032 stopped: reversal state or accounting history exists (nothing '
                                'was changed)';
            END IF;
        END $$
    """)
    op.execute("DROP FUNCTION public.donation_accounting_apply(uuid, bigint, text, text[], text[])")
    op.execute(_writer(reversal=False))
    op.execute("DROP TABLE public.donation_reversals")
    op.execute("""
        ALTER TABLE public.donation_credit_events
            DROP CONSTRAINT donation_credit_events_hold_cause, DROP CONSTRAINT donation_credit_events_accounting,
            DROP CONSTRAINT donation_credit_events_holds, DROP CONSTRAINT donation_credit_events_reversal,
            DROP CONSTRAINT donation_credit_events_moves, DROP CONSTRAINT donation_credit_events_cause_check
    """)
    # writer rows written at 0032 carry the (always unchanged, here 0 / NULL) reversal and hold: nothing is lost
    op.execute("ALTER TABLE public.donation_credit_events DROP COLUMN txid, DROP COLUMN evidence_refs, "
               "DROP COLUMN hold_after, DROP COLUMN hold_before, DROP COLUMN reversed_after_paise, "
               "DROP COLUMN reversed_before_paise")
    op.execute(f"""
        ALTER TABLE public.donation_credit_events
            ADD CONSTRAINT donation_credit_events_cause_check CHECK (cause IN {CAUSES_0031}),
            ADD CONSTRAINT donation_credit_events_moves CHECK (delta_inr <> 0 OR delta_count <> 0)
    """)
