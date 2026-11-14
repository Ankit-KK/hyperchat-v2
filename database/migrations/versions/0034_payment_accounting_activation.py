"""PAY-15 (D17) stage 6a-1: the accounting switch (state off / active / frozen), its audit, operator runs, evaluation
tracking, the refunds-only dispute mode and the owner-only accounting operations.

Revision ID: 0034
Revises: 0033

Nothing here evaluates or changes any donation: the state is created 'off', so the existing switch-gated evaluation
(PR 3) does nothing even where HYPERCHAT_PAYMENT_ACCOUNTING_ENABLED is on (which the API still refuses).

* payment_accounting_state   singleton: state off | active | frozen, dispute_mode hold | apply (default hold:
                             refunds-only). Changed only by the owner, with an operator and a reason (10-500
                             characters) along the allowed transitions (off->active, active->frozen, frozen->active,
                             frozen->off once nothing is applied or held; the mode only in off / frozen); the guard
                             writes the audit event itself.
* payment_accounting_events  append-only (every role) history of every state / mode change.
* payment_accounting_runs    append-only operator runs (reconcile, resolve-keep, resolve-apply, revert); every
                             manual / activation / revert accounting row names its run (CHECK).
* donation_accounting_evaluations
                             operational metadata (NOT financial history): the evidence fingerprint each donation was
                             last evaluated on, the state / mode then, the outcome, and an operator's keep
                             acknowledgement (valid only for exactly that evidence). Written only by the accounting
                             functions; runtime roles SELECT.
* payment_evidence_fp(donation)
                             md5 over the payment id, the dispute mode and every current evidence row of the donation
                             (reference, kind, match type, payment id, status, phase, amounts, currency, conflicting):
                             anything that changes attribution or the evaluation changes it.
* donation_accounting_core   0032's apply body, factored out (EXECUTE: nobody), plus: the dispute mode (hold: a clear
                             result is refused while a current attributed lost dispute exists; H-DISPUTE-MODE is the
                             hold for it), the keep acknowledgement, the evaluation metadata, and a cause / run id.
* donation_accounting_apply  (app) now takes the state row FOR SHARE first and does nothing ('inactive') unless the
                             state is 'active'; otherwise 0032's contract, unchanged.
* owner-only operations      payment_accounting_set_state, _open_run, _reconcile (cause activation), _keep (only an
                             H-DISPUTE-MODE hold; acknowledges the current evidence), _apply_evaluation (cause
                             manual; only a clear, evidence-supported result), _revert (frozen / off; applied -> 0,
                             the one writer restores the credit). Lock order in every one: the state row, then the
                             donation, then (through the writer) the totals.

Downgrade refuses once the state ever changed or any run, accounting row of a 0034 cause, H-DISPUTE-MODE hold or
evaluation row exists; otherwise it restores 0032's apply function verbatim (read from the 0032 revision) and drops
everything above.
"""

import importlib.util
import pathlib
import types

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

EVERYONE = ("hyperchat_app, hyperchat_worker, hyperchat_auth, hyperchat_lifecycle, hyperchat_readonly, "
            "hyperchat_stepup, PUBLIC")
WRITERS = "hyperchat_app, hyperchat_worker"
HOLDS_0032 = "('H-CONFLICT', 'H-CURRENCY', 'H-NO-DEDUCTION', 'H-OVER-REFUND')"
HOLDS = "('H-CONFLICT', 'H-CURRENCY', 'H-NO-DEDUCTION', 'H-OVER-REFUND', 'H-DISPUTE-MODE')"
TABLES = ("payment_accounting_state", "payment_accounting_events", "payment_accounting_runs",
          "donation_accounting_evaluations")
OWNER_FUNCTIONS = ("payment_evidence_fp(uuid)",
                   "donation_accounting_core(uuid, bigint, text, text[], text[], text, bigint)",
                   "payment_accounting_set_state(text, text, text, text, text)",
                   "payment_accounting_open_run(text, text, text, text, jsonb)",
                   "payment_accounting_reconcile(uuid, bigint, text, text[], text[], bigint)",
                   "payment_accounting_keep(uuid, bigint)",
                   "payment_accounting_apply_evaluation(uuid, bigint, text[], text[], bigint)",
                   "payment_accounting_revert(uuid, bigint)")
REFUSE = "USING ERRCODE = 'insufficient_privilege'"
INVALID = "USING ERRCODE = 'invalid_parameter_value'"


def _apply_0032() -> str:
    """0032's donation_accounting_apply, byte for byte (its own revision file renders it)."""
    path = pathlib.Path(__file__).with_name("0032_donation_reversal_accounting.py")
    spec = importlib.util.spec_from_file_location("_hyperchat_0032", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []
    module.op = types.SimpleNamespace(execute=statements.append)
    module.upgrade()
    sql = next(s for s in statements if "FUNCTION public.donation_accounting_apply(" in s)
    return sql.replace("CREATE FUNCTION public.donation_accounting_apply",
                       "CREATE OR REPLACE FUNCTION public.donation_accounting_apply", 1)


def _ledger(cause_sql: str, credited: str, before: str, after: str, hold_before: str, hold_after: str,
            refs: str, run: str) -> str:
    return f"""
            INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause,
                    payment_status, moderation_status, credited_before, credited_after, delta_inr, delta_count,
                    total_after, actor, reversed_before_paise, reversed_after_paise, hold_before, hold_after,
                    evidence_refs, run_id)
                VALUES (d.id, d.streamer_id, lower(d.donor_name), {cause_sql}, d.payment_status, d.moderation_status,
                        {credited}, {credited}, 0, 0, NULL, session_user, {before}, {after}, {hold_before},
                        {hold_after}, {refs}, {run});"""


LOCK_DONATION_V2 = f"""
            SELECT x.id, x.streamer_id, x.donor_name, x.amount, x.currency::text AS currency, x.payment_status,
                   x.moderation_status, x.provider_payment_id, x.legacy_source_table, x.credited_inr,
                   x.payment_account, x.provider_order_id
                INTO d FROM public.donations x WHERE x.id = p_donation FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'no donation %', p_donation {INVALID};
            END IF;
            IF NOT (d.legacy_source_table IS NULL AND d.payment_account IS NOT NULL
                    AND d.provider_order_id IS NOT NULL) THEN
                RAISE EXCEPTION 'only V2 checkout donations have accounting (imported or ambiguous provenance, '
                                'PAY15-D8)' {REFUSE};
            END IF;"""


# Revision 2 §3.3: an owner operation that moves a reversal emits the realtime goal and leaderboard events, as
# app/realtime/outbox.py emit_totals_changed does (channels, coalesce keys "<event>:<streamer id>", max_attempts =
# outbox.MAX_ATTEMPTS, skipped while one is pending), after the writer moved the total (lock order T -> U).
EMIT_TOTALS_CHANGED = """
                INSERT INTO public.realtime_outbox (streamer_id, channel, event, coalesce_key, max_attempts)
                    VALUES (d.streamer_id, 'goal', 'goal-progress', 'goal-progress:' || d.streamer_id::text, 6),
                           (d.streamer_id, 'leaderboard', 'leaderboard-updated',
                            'leaderboard-updated:' || d.streamer_id::text, 6)
                    ON CONFLICT (coalesce_key) WHERE status = 'pending' DO NOTHING;"""


def _run_check(command: str) -> str:
    return f"""
            IF NOT EXISTS (SELECT 1 FROM public.payment_accounting_runs WHERE id = p_run AND command = '{command}') THEN
                RAISE EXCEPTION 'an operator run of command {command} is required' {REFUSE};
            END IF;"""


def upgrade() -> None:
    # ── tables ──
    op.execute("""
        CREATE TABLE public.payment_accounting_state (
            id boolean PRIMARY KEY DEFAULT true CHECK (id),
            state text NOT NULL CHECK (state IN ('off', 'active', 'frozen')),
            dispute_mode text NOT NULL DEFAULT 'hold' CHECK (dispute_mode IN ('hold', 'apply')),
            change_operator text,
            change_reason text,
            change_ref text,
            changed_at timestamptz NOT NULL DEFAULT now(),
            changed_by text NOT NULL
        )
    """)
    op.execute("INSERT INTO public.payment_accounting_state (state, changed_by) VALUES ('off', 'migration 0034')")
    op.execute("""
        CREATE TABLE public.payment_accounting_events (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            from_state text NOT NULL, to_state text NOT NULL, from_mode text NOT NULL, to_mode text NOT NULL,
            operator text NOT NULL CHECK (btrim(operator) <> ''),
            reason text NOT NULL CHECK (char_length(btrim(reason)) BETWEEN 10 AND 500),
            ref text CHECK (ref IS NULL OR char_length(ref) <= 200),
            db_user text NOT NULL,
            txid bigint NOT NULL DEFAULT txid_current(),
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT payment_accounting_events_moves CHECK ((from_state, from_mode) IS DISTINCT FROM (to_state, to_mode))
        )
    """)
    op.execute("""
        CREATE TABLE public.payment_accounting_runs (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            command text NOT NULL CHECK (command IN ('reconcile', 'resolve-keep', 'resolve-apply', 'revert')),
            operator text NOT NULL CHECK (btrim(operator) <> ''),
            reason text NOT NULL CHECK (char_length(btrim(reason)) BETWEEN 10 AND 500),
            ref text CHECK (ref IS NULL OR char_length(ref) <= 200),
            scope jsonb NOT NULL,
            db_user text NOT NULL,
            txid bigint NOT NULL DEFAULT txid_current(),
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE public.donation_accounting_evaluations (
            donation_id uuid PRIMARY KEY,
            evidence_fp text NOT NULL,
            evaluated_at timestamptz NOT NULL,
            state text NOT NULL,
            dispute_mode text NOT NULL,
            outcome text NOT NULL,
            ack_fp text,
            ack_run bigint,
            CONSTRAINT donation_accounting_evaluations_ack CHECK ((ack_fp IS NULL) = (ack_run IS NULL))
        )
    """)
    op.execute("CREATE INDEX payment_accounting_events_created_idx ON public.payment_accounting_events (created_at)")

    # ── history: run ids and the refunds-only hold ──
    op.execute("ALTER TABLE public.donation_credit_events ADD COLUMN run_id bigint")
    op.execute("""
        ALTER TABLE public.donation_credit_events
            ADD CONSTRAINT donation_credit_events_run
                CHECK (cause NOT IN ('manual', 'activation', 'revert') OR run_id IS NOT NULL)
    """)
    op.execute("ALTER TABLE public.donation_credit_events DROP CONSTRAINT donation_credit_events_holds")
    op.execute(f"""
        ALTER TABLE public.donation_credit_events ADD CONSTRAINT donation_credit_events_holds CHECK (
            (hold_before IS NULL OR hold_before IN {HOLDS}) AND (hold_after IS NULL OR hold_after IN {HOLDS}))
    """)
    op.execute("ALTER TABLE public.donation_reversals DROP CONSTRAINT donation_reversals_hold_check")
    op.execute(f"ALTER TABLE public.donation_reversals ADD CONSTRAINT donation_reversals_hold_check "
               f"CHECK (hold IN {HOLDS})")

    # ── guards ──
    op.execute(f"""
        CREATE FUNCTION public.payment_accounting_state_guard() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP <> 'UPDATE' THEN
                RAISE EXCEPTION 'the accounting state row is fixed (% refused)', TG_OP {REFUSE};
            END IF;
            IF current_user <> 'hyperchat_owner' THEN
                RAISE EXCEPTION 'only the operator changes the accounting state' {REFUSE};
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id THEN
                RAISE EXCEPTION 'the accounting state row is fixed' {REFUSE};
            END IF;
            IF (NEW.state, NEW.dispute_mode) IS NOT DISTINCT FROM (OLD.state, OLD.dispute_mode) THEN
                RAISE EXCEPTION 'nothing to change' {INVALID};
            END IF;
            IF btrim(COALESCE(NEW.change_operator, '')) = ''
               OR char_length(btrim(COALESCE(NEW.change_reason, ''))) NOT BETWEEN 10 AND 500 THEN
                RAISE EXCEPTION 'an accounting state change needs an operator and a reason (10-500 characters)' {REFUSE};
            END IF;
            IF NEW.dispute_mode IS DISTINCT FROM OLD.dispute_mode
               AND (NEW.state IS DISTINCT FROM OLD.state OR OLD.state = 'active') THEN
                RAISE EXCEPTION 'the dispute mode changes on its own, only while off or frozen' {INVALID};
            END IF;
            IF NEW.state IS DISTINCT FROM OLD.state AND (OLD.state, NEW.state) NOT IN
                    (('off', 'active'), ('active', 'frozen'), ('frozen', 'active'), ('frozen', 'off')) THEN
                RAISE EXCEPTION 'accounting state % -> % is not allowed (freeze first)', OLD.state, NEW.state {INVALID};
            END IF;
            IF (OLD.state, NEW.state) = ('frozen', 'off') AND EXISTS (
                    SELECT 1 FROM public.donation_reversals WHERE applied_reversed_paise > 0 OR hold IS NOT NULL) THEN
                RAISE EXCEPTION 'accounting goes off only once every reversal and hold is reverted' {INVALID};
            END IF;
            INSERT INTO public.payment_accounting_events (from_state, to_state, from_mode, to_mode, operator, reason,
                                                          ref, db_user)
                VALUES (OLD.state, NEW.state, OLD.dispute_mode, NEW.dispute_mode, btrim(NEW.change_operator),
                        btrim(NEW.change_reason), NEW.change_ref, session_user);
            NEW.change_operator := NULL;
            NEW.change_reason := NULL;
            NEW.change_ref := NULL;
            NEW.changed_at := now();
            NEW.changed_by := session_user;
            RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER payment_accounting_state_guard BEFORE INSERT OR UPDATE OR DELETE "
               "ON public.payment_accounting_state FOR EACH ROW EXECUTE FUNCTION public.payment_accounting_state_guard()")
    op.execute("""
        CREATE FUNCTION public.payment_accounting_append_only() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        DECLARE v_rows boolean;
        BEGIN
            IF TG_OP = 'TRUNCATE' THEN
                IF TG_TABLE_NAME = 'payment_accounting_state' THEN
                    v_rows := true;
                ELSE
                    EXECUTE format('SELECT EXISTS (SELECT 1 FROM public.%I)', TG_TABLE_NAME) INTO v_rows;
                END IF;
                IF NOT v_rows THEN
                    RETURN NULL;
                END IF;
            END IF;
            RAISE EXCEPTION '% is append-only (% refused)', TG_TABLE_NAME, TG_OP USING ERRCODE = 'insufficient_privilege';
        END $$
    """)
    for table in ("payment_accounting_events", "payment_accounting_runs"):
        op.execute(f"CREATE TRIGGER {table}_guard BEFORE UPDATE OR DELETE ON public.{table} "
                   "FOR EACH ROW EXECUTE FUNCTION public.payment_accounting_append_only()")
    for table in ("payment_accounting_state", "payment_accounting_events", "payment_accounting_runs"):
        op.execute(f"CREATE TRIGGER {table}_guard_truncate BEFORE TRUNCATE ON public.{table} "
                   "FOR EACH STATEMENT EXECUTE FUNCTION public.payment_accounting_append_only()")

    # ── grants: runtime roles read only ──
    for table in TABLES:
        op.execute(f"REVOKE ALL ON public.{table} FROM {WRITERS}")
        op.execute(f"GRANT SELECT ON public.{table} TO {WRITERS}")

    # ── lock order L-1 for the API role: SELECT ... FOR SHARE needs UPDATE privilege, which the runtime roles must
    # not have, so the API takes the state row lock through this owner function (the lock lasts to its commit) ──
    op.execute("""
        CREATE FUNCTION public.payment_accounting_lock_state(OUT state text, OUT dispute_mode text)
        LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
            SELECT s.state, s.dispute_mode FROM public.payment_accounting_state s FOR SHARE
        $$
    """)
    op.execute(f"REVOKE ALL ON FUNCTION public.payment_accounting_lock_state() FROM {EVERYONE}")
    op.execute("GRANT EXECUTE ON FUNCTION public.payment_accounting_lock_state() TO hyperchat_app")

    # ── the evidence fingerprint ──
    op.execute("""
        CREATE FUNCTION public.payment_evidence_fp(p_donation uuid) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
            SELECT md5(ROW(
                (SELECT x.provider_payment_id FROM public.donations x WHERE x.id = p_donation),
                (SELECT s.dispute_mode FROM public.payment_accounting_state s),
                (SELECT string_agg(ROW(c.provider_ref, c.kind, c.match_type, c.provider_payment_id, c.status, c.phase,
                                       c.amount_paise, c.amount_deducted_paise, c.currency, c.conflicting)::text,
                                   ',' ORDER BY c.provider_ref)
                   FROM public.payment_integrity_current c WHERE c.donation_id = p_donation))::text)
        $$
    """)

    # ── the core: 0032's apply, with the dispute mode, the keep acknowledgement, metadata and a cause / run ──
    op.execute(f"""
        CREATE FUNCTION public.donation_accounting_core(p_donation uuid, p_reversed bigint, p_hold text,
                                                        p_flags text[], p_refs text[], p_cause text, p_run bigint)
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
            v_state text;
            v_mode text;
            v_fp text;
            v_ack text;
            v_lost_dispute boolean;
            v_outcome text;
        BEGIN
            IF p_cause NOT IN ('evaluation', 'activation', 'manual') THEN
                RAISE EXCEPTION 'unknown accounting cause %', p_cause {INVALID};
            END IF;
            SELECT s.state, s.dispute_mode INTO v_state, v_mode FROM public.payment_accounting_state s;
            {LOCK_DONATION_V2}
            IF (p_hold IS NULL) = (p_reversed IS NULL) THEN
                RAISE EXCEPTION 'exactly one of a clear reversal or a hold' {INVALID};
            END IF;
            IF p_hold IS NOT NULL AND p_hold NOT IN {HOLDS} THEN
                RAISE EXCEPTION 'unknown hold %', p_hold {INVALID};
            END IF;
            IF NOT (v_flags <@ ARRAY['F-BOTH', 'F-OPEN']::text[]) OR array_position(v_flags, NULL) IS NOT NULL THEN
                RAISE EXCEPTION 'unknown flag' {INVALID};
            END IF;
            IF cardinality(v_refs) <> (SELECT count(DISTINCT r) FROM unnest(v_refs) AS r WHERE r IS NOT NULL) THEN
                RAISE EXCEPTION 'duplicate or empty evidence reference' {INVALID};
            END IF;
            v_captured := (d.amount * 100)::bigint;
            SELECT r.applied_reversed_paise, r.hold, r.flags INTO v_before, v_hold_before, v_flags_before
                FROM public.donation_reversals r WHERE r.donation_id = p_donation;
            v_found := FOUND;
            v_before := COALESCE(v_before, 0);
            v_fp := public.payment_evidence_fp(p_donation);
            SELECT e.ack_fp INTO v_ack FROM public.donation_accounting_evaluations e WHERE e.donation_id = p_donation;
            v_lost_dispute := EXISTS (
                SELECT 1 FROM public.payment_integrity_current c
                WHERE c.donation_id = d.id AND c.provider_payment_id = d.provider_payment_id
                  AND c.kind = 'dispute' AND c.status = 'lost');

            IF p_hold = 'H-DISPUTE-MODE' AND (v_mode <> 'hold' OR NOT v_lost_dispute) THEN
                RAISE EXCEPTION 'H-DISPUTE-MODE holds only a current lost dispute in dispute mode hold' {INVALID};
            END IF;

            IF p_hold = 'H-DISPUTE-MODE' AND v_ack IS NOT NULL AND v_ack = v_fp THEN
                -- an operator's keep acknowledged exactly this evidence (and mode): nothing to hold again
                v_after := v_before;
                v_hold_after := v_hold_before;
                v_outcome := 'acknowledged';
            ELSIF p_hold IS NOT NULL THEN
                IF cardinality(v_refs) > 0 THEN
                    RAISE EXCEPTION 'a hold applies nothing: it names no evidence' {INVALID};
                END IF;
                v_after := v_before;
                v_hold_after := p_hold;
            ELSE
                IF p_reversed < 0 OR p_reversed > v_captured THEN
                    RAISE EXCEPTION 'a reversal is between 0 and the captured amount' {INVALID};
                END IF;
                IF p_reversed > 0 AND d.payment_status <> 'success' THEN
                    RAISE EXCEPTION 'an unpaid donation has nothing to reverse' {INVALID};
                END IF;
                -- refunds-only (dispute mode hold): an unverified dispute deduction is never applied automatically
                IF v_mode = 'hold' AND v_lost_dispute THEN
                    RAISE EXCEPTION 'dispute mode hold: a lost dispute is held, never applied' {REFUSE};
                END IF;
                IF EXISTS (
                    SELECT 1 FROM public.payment_integrity_current c
                    WHERE c.donation_id = d.id AND c.match_type = 'payment'
                      AND c.provider_payment_id IS DISTINCT FROM d.provider_payment_id) THEN
                    RAISE EXCEPTION 'evidence matched to this donation by payment names another payment: no clear '
                                    'reversal' {REFUSE};
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
                    RAISE EXCEPTION 'this payment has evidence that must be held: no clear reversal' {REFUSE};
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
                                    'counted evidence of this payment, all of it named' {REFUSE};
                END IF;
                v_after := p_reversed;
                v_hold_after := NULL;
            END IF;

            IF v_outcome IS NULL THEN
                v_outcome := CASE WHEN v_after = v_before AND v_hold_after IS NOT DISTINCT FROM v_hold_before
                                  THEN 'unchanged'
                                  WHEN p_cause <> 'evaluation' THEN p_cause
                                  WHEN v_hold_after IS NOT NULL THEN 'hold'
                                  WHEN v_hold_before IS NOT NULL THEN 'resolved' ELSE 'evaluation' END;
            END IF;
            -- operational metadata (not financial history): what was evaluated, on which evidence; a keep
            -- acknowledgement stays valid only while the evidence fingerprint is unchanged
            INSERT INTO public.donation_accounting_evaluations AS e (donation_id, evidence_fp, evaluated_at, state,
                                                                     dispute_mode, outcome)
                VALUES (p_donation, v_fp, now(), v_state, v_mode, v_outcome)
                ON CONFLICT (donation_id) DO UPDATE SET evidence_fp = EXCLUDED.evidence_fp, evaluated_at = now(),
                    state = EXCLUDED.state, dispute_mode = EXCLUDED.dispute_mode, outcome = EXCLUDED.outcome,
                    ack_fp = CASE WHEN e.ack_fp = EXCLUDED.evidence_fp THEN e.ack_fp END,
                    ack_run = CASE WHEN e.ack_fp = EXCLUDED.evidence_fp THEN e.ack_run END;

            IF v_outcome IN ('unchanged', 'acknowledged') THEN  -- F2: no financial change, no history row
                IF v_found AND v_flags IS DISTINCT FROM v_flags_before THEN
                    UPDATE public.donation_reversals SET flags = v_flags, evaluated_at = now()
                        WHERE donation_id = p_donation;
                ELSIF NOT v_found AND cardinality(v_flags) > 0 THEN
                    INSERT INTO public.donation_reversals (donation_id, captured_paise, flags)
                        VALUES (p_donation, v_captured, v_flags);
                END IF;
                RETURN v_outcome;
            END IF;

            INSERT INTO public.donation_reversals AS r (donation_id, captured_paise, applied_reversed_paise, hold, flags)
                VALUES (p_donation, v_captured, v_after, v_hold_after, v_flags)
                ON CONFLICT (donation_id) DO UPDATE SET applied_reversed_paise = EXCLUDED.applied_reversed_paise,
                    hold = EXCLUDED.hold, flags = EXCLUDED.flags, evaluated_at = now();
            v_cause := CASE WHEN p_cause <> 'evaluation' THEN p_cause WHEN v_hold_after IS NOT NULL THEN 'hold'
                            WHEN v_hold_before IS NOT NULL THEN 'resolved' ELSE 'evaluation' END;
            {_ledger('v_cause', 'd.credited_inr', 'v_before', 'v_after', 'v_hold_before', 'v_hold_after',
                     "CASE WHEN v_hold_after IS NOT NULL AND v_after = v_before THEN NULL ELSE v_refs END", 'p_run')}
            IF v_after <> v_before THEN
                UPDATE public.donations SET updated_at = now() WHERE id = d.id;  -- the one writer moves the total
                IF p_cause <> 'evaluation' THEN  -- the app path emits its own events (repository._settle_accounting)
                    {EMIT_TOTALS_CHANGED}
                END IF;
            END IF;
            RETURN v_outcome;
        END $$
    """)

    # ── apply (app): the state first, then 0032's contract through the core ──
    op.execute("""
        CREATE OR REPLACE FUNCTION public.donation_accounting_apply(p_donation uuid, p_reversed bigint, p_hold text,
                                                                   p_flags text[], p_refs text[])
        RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE v_state text;
        BEGIN
            -- the state row before anything else (lock order L-1; already held by the payment paths)
            SELECT state INTO v_state FROM public.payment_accounting_state FOR SHARE;
            IF v_state IS DISTINCT FROM 'active' THEN
                RETURN 'inactive';
            END IF;
            RETURN public.donation_accounting_core(p_donation, p_reversed, p_hold, p_flags, p_refs, 'evaluation', NULL);
        END $$
    """)

    # ── owner-only operations ──
    # S6A-D6: a state change waits at most 5 s for the state row (in-flight evaluations hold it FOR SHARE), then
    # fails with 55P03 and rolls back (no event). A function-level SET: in force only while the function runs (the
    # caller's own lock_timeout is restored on exit), and no caller can lift it.
    op.execute("""
        CREATE FUNCTION public.payment_accounting_set_state(p_state text, p_mode text, p_operator text, p_reason text,
                                                            p_ref text)
        RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public SET lock_timeout = '5s'
        AS $$
        DECLARE cur record;
        BEGIN
            SELECT state, dispute_mode INTO cur FROM public.payment_accounting_state FOR UPDATE;
            IF (COALESCE(p_state, cur.state), COALESCE(p_mode, cur.dispute_mode))
               IS NOT DISTINCT FROM (cur.state, cur.dispute_mode) THEN
                RETURN 'unchanged';
            END IF;
            UPDATE public.payment_accounting_state
                SET state = COALESCE(p_state, cur.state), dispute_mode = COALESCE(p_mode, cur.dispute_mode),
                    change_operator = p_operator, change_reason = p_reason, change_ref = p_ref;
            RETURN COALESCE(p_state, cur.state);
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.payment_accounting_open_run(p_command text, p_operator text, p_reason text, p_ref text,
                                                           p_scope jsonb)
        RETURNS bigint LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
            INSERT INTO public.payment_accounting_runs (command, operator, reason, ref, scope, db_user)
                VALUES (p_command, btrim(p_operator), btrim(p_reason), p_ref, p_scope, session_user) RETURNING id
        $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.payment_accounting_reconcile(p_donation uuid, p_reversed bigint, p_hold text,
                                                            p_flags text[], p_refs text[], p_run bigint)
        RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE v_state text;
        BEGIN
            SELECT state INTO v_state FROM public.payment_accounting_state FOR SHARE;
            IF v_state IS DISTINCT FROM 'active' THEN
                RAISE EXCEPTION 'reconcile needs the accounting state active' {REFUSE};
            END IF;
            {_run_check('reconcile')}
            RETURN public.donation_accounting_core(p_donation, p_reversed, p_hold, p_flags, p_refs, 'activation', p_run);
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.payment_accounting_keep(p_donation uuid, p_run bigint)
        RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            d record;
            r record;
            v_state text;
            v_mode text;
            v_fp text;
        BEGIN
            SELECT state, dispute_mode INTO v_state, v_mode FROM public.payment_accounting_state FOR SHARE;
            IF v_state NOT IN ('active', 'frozen') THEN
                RAISE EXCEPTION 'keep needs the accounting state active or frozen' {REFUSE};
            END IF;
            {_run_check('resolve-keep')}
            {LOCK_DONATION_V2}
            SELECT * INTO r FROM public.donation_reversals WHERE donation_id = p_donation;
            IF NOT FOUND OR r.hold IS NULL THEN
                RETURN 'unchanged';
            END IF;
            IF r.hold <> 'H-DISPUTE-MODE' THEN  -- evidence-integrity holds need clear evidence, never a keep
                RAISE EXCEPTION 'keep releases only H-DISPUTE-MODE (this donation is held %)', r.hold {REFUSE};
            END IF;
            v_fp := public.payment_evidence_fp(p_donation);
            UPDATE public.donation_reversals SET hold = NULL, evaluated_at = now() WHERE donation_id = p_donation;
            {_ledger("'manual'", 'd.credited_inr', 'r.applied_reversed_paise', 'r.applied_reversed_paise', 'r.hold',
                     'NULL', 'NULL', 'p_run')}
            INSERT INTO public.donation_accounting_evaluations AS e (donation_id, evidence_fp, evaluated_at, state,
                                                                     dispute_mode, outcome, ack_fp, ack_run)
                VALUES (p_donation, v_fp, now(), v_state, v_mode, 'kept', v_fp, p_run)
                ON CONFLICT (donation_id) DO UPDATE SET evidence_fp = EXCLUDED.evidence_fp, evaluated_at = now(),
                    state = EXCLUDED.state, dispute_mode = EXCLUDED.dispute_mode, outcome = 'kept',
                    ack_fp = EXCLUDED.ack_fp, ack_run = EXCLUDED.ack_run;
            RETURN 'kept';
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.payment_accounting_apply_evaluation(p_donation uuid, p_reversed bigint, p_flags text[],
                                                                   p_refs text[], p_run bigint)
        RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE v_state text;
        BEGIN
            SELECT state INTO v_state FROM public.payment_accounting_state FOR SHARE;
            IF v_state IS DISTINCT FROM 'active' THEN
                RAISE EXCEPTION 'apply-evaluation needs the accounting state active' {REFUSE};
            END IF;
            {_run_check('resolve-apply')}
            IF NOT EXISTS (SELECT 1 FROM public.donation_reversals WHERE donation_id = p_donation AND hold IS NOT NULL)
            THEN
                RETURN 'unchanged';  -- nothing held: automatic evaluation already governs it
            END IF;
            -- only a clear result, re-derived by the core from the stored evidence exactly as automatically
            RETURN public.donation_accounting_core(p_donation, p_reversed, NULL, p_flags, p_refs, 'manual', p_run);
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.payment_accounting_revert(p_donation uuid, p_run bigint)
        RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            d record;
            r record;
            v_state text;
        BEGIN
            SELECT state INTO v_state FROM public.payment_accounting_state FOR SHARE;
            IF v_state NOT IN ('frozen', 'off') THEN
                RAISE EXCEPTION 'revert needs the accounting state frozen or off' {REFUSE};
            END IF;
            {_run_check('revert')}
            {LOCK_DONATION_V2}
            SELECT * INTO r FROM public.donation_reversals WHERE donation_id = p_donation;
            IF NOT FOUND OR (r.applied_reversed_paise = 0 AND r.hold IS NULL) THEN
                RETURN 'unchanged';
            END IF;
            UPDATE public.donation_reversals SET applied_reversed_paise = 0, hold = NULL, evaluated_at = now()
                WHERE donation_id = p_donation;
            {_ledger("'revert'", 'd.credited_inr', 'r.applied_reversed_paise', '0', 'r.hold', 'NULL', 'NULL', 'p_run')}
            UPDATE public.donation_accounting_evaluations SET outcome = 'reverted', ack_fp = NULL, ack_run = NULL,
                    evaluated_at = now()
                WHERE donation_id = p_donation;
            IF r.applied_reversed_paise <> 0 THEN
                UPDATE public.donations SET updated_at = now() WHERE id = d.id;  -- the one writer restores the credit
                {EMIT_TOTALS_CHANGED}
            END IF;
            RETURN 'reverted';
        END $$
    """)
    for fn in OWNER_FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION public.{fn} FROM {EVERYONE}")
    for fn in ("payment_accounting_state_guard()", "payment_accounting_append_only()"):
        op.execute(f"REVOKE ALL ON FUNCTION public.{fn} FROM {EVERYONE}")


def downgrade() -> None:
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.payment_accounting_events)
               OR EXISTS (SELECT 1 FROM public.payment_accounting_runs)
               OR EXISTS (SELECT 1 FROM public.donation_accounting_evaluations)
               OR (SELECT (state, dispute_mode) <> ('off', 'hold') FROM public.payment_accounting_state)
               OR EXISTS (SELECT 1 FROM public.donation_credit_events
                          WHERE cause IN ('manual', 'activation', 'revert') OR run_id IS NOT NULL
                             OR 'H-DISPUTE-MODE' IN (hold_before, hold_after))
               OR EXISTS (SELECT 1 FROM public.donation_reversals WHERE hold = 'H-DISPUTE-MODE') THEN
                RAISE EXCEPTION 'downgrade of 0034 stopped: accounting state, runs, evaluations or 0034 history exist '
                                '(nothing was changed)';
            END IF;
        END $$
    """)
    op.execute(_apply_0032())
    for fn in OWNER_FUNCTIONS:
        op.execute(f"DROP FUNCTION public.{fn}")
    for table in ("payment_accounting_state", "payment_accounting_events", "payment_accounting_runs",
                  "donation_accounting_evaluations"):
        op.execute(f"DROP TABLE public.{table}")
    op.execute("DROP FUNCTION public.payment_accounting_lock_state()")
    op.execute("DROP FUNCTION public.payment_accounting_state_guard()")
    op.execute("DROP FUNCTION public.payment_accounting_append_only()")
    op.execute("ALTER TABLE public.donation_reversals DROP CONSTRAINT donation_reversals_hold_check")
    op.execute(f"ALTER TABLE public.donation_reversals ADD CONSTRAINT donation_reversals_hold_check "
               f"CHECK (hold IN {HOLDS_0032})")
    op.execute("ALTER TABLE public.donation_credit_events DROP CONSTRAINT donation_credit_events_holds, "
               "DROP CONSTRAINT donation_credit_events_run")
    op.execute(f"""
        ALTER TABLE public.donation_credit_events ADD CONSTRAINT donation_credit_events_holds CHECK (
            (hold_before IS NULL OR hold_before IN {HOLDS_0032}) AND (hold_after IS NULL OR hold_after IN {HOLDS_0032}))
    """)
    op.execute("ALTER TABLE public.donation_credit_events DROP COLUMN run_id")
