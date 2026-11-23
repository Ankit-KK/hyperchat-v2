"""PAY-15 (D17) stage 6a hardening, narrowed (OD-35-S): F-7 (run-id and acknowledgement forgery) and F-11 (operator
runs bound to their declared scope).

Revision ID: 0035
Revises: 0034

* F-7  donation_accounting_evaluations.ack_run and donation_credit_events.run_id reference payment_accounting_runs
       (runs are append-only, so the keys never need ON DELETE behaviour). A guard trigger on the evaluations refuses
       any INSERT / UPDATE that sets or changes a non-NULL acknowledgement unless payment_accounting_keep marked the
       transaction (transaction-local setting hyperchat.accounting_ack = 1, cleared right after its write). Clearing
       an acknowledgement (the core on new evidence, revert) stays allowed. The owner can still set the marker or
       disable the trigger: this prevents mistakes and non-owner writes and leaves owner bypass to detection (I10
       reports a disabled or rebound trigger), as the recovery design §5a states.
* F-11 enforced at USE time (reviewer ruling on OD-35-S, variant 1): reconcile, keep, apply-evaluation and revert keep
       0034's run check and add payment_accounting_scope_covers(), a rule-for-rule port of the verifier's D-10
       parse_scope + scope_covers: the scope must be an object of exactly its kind's keys (donations / creator /
       all_v2 / recovery), expires_at an ISO time with an offset, not before the run and at most 24 h after it, and
       not passed; donations a non-empty duplicate-free uuid list; all_v2 covers V2 donations for reconcile only.
       Unscoped or malformed runs can still be opened (open_run and the runs table are unchanged, so 0034-era runs
       stay reportable by D-10) but never used. donation_accounting_core stays owner-internal and unchanged.

The four operation bodies are 0034's own text with only the run check extended (and keep's acknowledgement write
marked), rendered from the 0034 revision; the downgrade restores 0034's bodies byte for byte the same way. The
downgrade loses no data (constraints, a trigger and two functions only) and is therefore not refused.
"""

import importlib.util
import pathlib
import types

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

EVERYONE = ("hyperchat_app, hyperchat_worker, hyperchat_auth, hyperchat_lifecycle, hyperchat_readonly, "
            "hyperchat_stepup, PUBLIC")
REFUSE = "USING ERRCODE = 'insufficient_privilege'"
OPERATIONS = {"payment_accounting_reconcile": "reconcile", "payment_accounting_keep": "resolve-keep",
              "payment_accounting_apply_evaluation": "resolve-apply", "payment_accounting_revert": "revert"}
ACK_MARK = "PERFORM pg_catalog.set_config('hyperchat.accounting_ack', '1', true);"
ACK_UNMARK = "PERFORM pg_catalog.set_config('hyperchat.accounting_ack', '', true);"
KEEP_ACK_WRITE = "            INSERT INTO public.donation_accounting_evaluations AS e (donation_id, evidence_fp, evaluated_at, state,"
KEEP_ACK_END = "ack_fp = EXCLUDED.ack_fp, ack_run = EXCLUDED.ack_run;"

SCOPE_COVERS = r"""
        CREATE FUNCTION public.payment_accounting_scope_covers(p_scope jsonb, p_created timestamptz, p_command text,
                                                               p_donation uuid)
        RETURNS boolean LANGUAGE plpgsql STABLE SET search_path = pg_catalog, public AS $$
        DECLARE
            v_kind text;
            v_keys text[];
            v_expires timestamptz;
            v_ids uuid[];
            v_creator uuid;
            d record;
        BEGIN
            -- the verifier's parse_scope (D-10): unscoped ({} or no kind) and every malformed scope cover nothing
            IF p_scope IS NULL OR jsonb_typeof(p_scope) <> 'object' OR NOT p_scope ? 'kind'
               OR jsonb_typeof(p_scope -> 'kind') <> 'string' THEN
                RETURN false;
            END IF;
            v_kind := p_scope ->> 'kind';
            v_keys := CASE v_kind WHEN 'donations' THEN ARRAY['donations', 'expires_at', 'kind']
                                  WHEN 'creator' THEN ARRAY['creator', 'expires_at', 'kind']
                                  WHEN 'all_v2' THEN ARRAY['expires_at', 'kind']
                                  WHEN 'recovery' THEN ARRAY['donations', 'expires_at', 'kind', 'verify_report_sha256']
                      END;
            IF v_keys IS NULL
               OR v_keys <> ARRAY(SELECT k FROM jsonb_object_keys(p_scope) AS k ORDER BY k COLLATE "C") THEN
                RETURN false;
            END IF;
            IF jsonb_typeof(p_scope -> 'expires_at') <> 'string' OR (p_scope ->> 'expires_at')
               !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}([.][0-9]{1,6})?)?(Z|[+-][0-9]{2}(:?[0-9]{2})?)$'
            THEN
                RETURN false;  -- an aware ISO time only (the verifier refuses a naive one)
            END IF;
            BEGIN
                v_expires := (p_scope ->> 'expires_at')::timestamptz;
            EXCEPTION WHEN OTHERS THEN
                RETURN false;
            END;
            IF NOT (p_created <= v_expires AND v_expires <= p_created + interval '24 hours') THEN
                RETURN false;
            END IF;
            IF v_kind IN ('donations', 'recovery') THEN
                IF jsonb_typeof(p_scope -> 'donations') <> 'array' OR jsonb_array_length(p_scope -> 'donations') = 0
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(p_scope -> 'donations') AS e
                              WHERE jsonb_typeof(e) <> 'string') THEN
                    RETURN false;
                END IF;
                BEGIN
                    v_ids := ARRAY(SELECT (e #>> '{}')::uuid FROM jsonb_array_elements(p_scope -> 'donations') AS e);
                EXCEPTION WHEN OTHERS THEN
                    RETURN false;
                END;
                IF cardinality(v_ids) <> (SELECT count(DISTINCT x) FROM unnest(v_ids) AS x) THEN
                    RETURN false;  -- a duplicate after normalisation
                END IF;
            END IF;
            IF v_kind = 'creator' THEN
                IF jsonb_typeof(p_scope -> 'creator') <> 'string' THEN
                    RETURN false;
                END IF;
                BEGIN
                    v_creator := (p_scope ->> 'creator')::uuid;
                EXCEPTION WHEN OTHERS THEN
                    RETURN false;
                END;
            END IF;
            IF v_kind = 'recovery' AND (jsonb_typeof(p_scope -> 'verify_report_sha256') <> 'string'
                                        OR (p_scope ->> 'verify_report_sha256') !~ '^[0-9a-f]{64}$') THEN
                RETURN false;
            END IF;
            -- the verifier's scope_covers: never after expires_at; listed / same creator / all V2 for reconcile only
            IF now() > v_expires THEN
                RETURN false;
            END IF;
            IF v_kind IN ('donations', 'recovery') THEN
                RETURN p_donation = ANY (v_ids);
            END IF;
            SELECT x.streamer_id, (x.legacy_source_table IS NULL AND x.payment_account IS NOT NULL
                                   AND x.provider_order_id IS NOT NULL) AS v2
                INTO d FROM public.donations x WHERE x.id = p_donation;
            IF NOT FOUND THEN
                RETURN false;
            END IF;
            IF v_kind = 'creator' THEN
                RETURN d.streamer_id = v_creator;
            END IF;
            RETURN d.v2 AND p_command = 'reconcile';
        END $$
"""

ACK_GUARD = f"""
        CREATE FUNCTION public.payment_accounting_ack_guard() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        BEGIN
            IF (NEW.ack_fp IS NOT NULL OR NEW.ack_run IS NOT NULL)
               AND (TG_OP = 'INSERT' OR (NEW.ack_fp, NEW.ack_run) IS DISTINCT FROM (OLD.ack_fp, OLD.ack_run))
               AND current_setting('hyperchat.accounting_ack', true) IS DISTINCT FROM '1' THEN
                RAISE EXCEPTION 'a keep acknowledgement is written only by payment_accounting_keep (F-7)' {REFUSE};
            END IF;
            RETURN NEW;
        END $$
"""


def _render_0034() -> tuple[types.ModuleType, list[str]]:
    """0034's module and its upgrade() statements, rendered without a database (its own helpers expand)."""
    path = pathlib.Path(__file__).with_name("0034_payment_accounting_activation.py")
    spec = importlib.util.spec_from_file_location("_hyperchat_0034", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []
    module.op = types.SimpleNamespace(execute=statements.append)
    module.upgrade()
    return module, statements


def _function_0034(statements: list[str], name: str) -> str:
    (sql,) = [s for s in statements if f"CREATE FUNCTION public.{name}(" in s]
    return sql.replace(f"CREATE FUNCTION public.{name}(", f"CREATE OR REPLACE FUNCTION public.{name}(", 1)


def _scoped_check(module, command: str) -> str:
    return module._run_check(command) + f"""
            IF NOT public.payment_accounting_scope_covers(
                    (SELECT r0.scope FROM public.payment_accounting_runs r0 WHERE r0.id = p_run),
                    (SELECT r0.created_at FROM public.payment_accounting_runs r0 WHERE r0.id = p_run),
                    '{command}', p_donation) THEN
                RAISE EXCEPTION 'run % does not cover donation % now: its scope is missing, malformed, expired or '
                                'excludes it (F-11)', p_run, p_donation {REFUSE};
            END IF;"""


def _hardened(module, statements: list[str], name: str) -> str:
    sql = _function_0034(statements, name)
    command = OPERATIONS[name]
    assert sql.count(module._run_check(command)) == 1, name
    sql = sql.replace(module._run_check(command), _scoped_check(module, command), 1)
    if name == "payment_accounting_keep":
        assert sql.count(KEEP_ACK_WRITE) == 1 and sql.count(KEEP_ACK_END) == 1
        sql = sql.replace(KEEP_ACK_WRITE, f"            {ACK_MARK}\n" + KEEP_ACK_WRITE, 1)
        sql = sql.replace(KEEP_ACK_END, f"{KEEP_ACK_END}\n            {ACK_UNMARK}", 1)
    return sql


def upgrade() -> None:
    module, statements = _render_0034()
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.donation_accounting_evaluations e WHERE e.ack_run IS NOT NULL
                       AND NOT EXISTS (SELECT 1 FROM public.payment_accounting_runs r WHERE r.id = e.ack_run))
               OR EXISTS (SELECT 1 FROM public.donation_credit_events h WHERE h.run_id IS NOT NULL
                          AND NOT EXISTS (SELECT 1 FROM public.payment_accounting_runs r WHERE r.id = h.run_id)) THEN
                RAISE EXCEPTION 'upgrade to 0035 stopped: an acknowledgement or a history row names no operator run '
                                '(F-7; investigate before hardening; nothing was changed)';
            END IF;
        END $$
    """)
    # ── F-7 ──
    op.execute("ALTER TABLE public.donation_accounting_evaluations ADD CONSTRAINT donation_accounting_evaluations_ack_run "
               "FOREIGN KEY (ack_run) REFERENCES public.payment_accounting_runs (id) NOT VALID")
    op.execute("ALTER TABLE public.donation_accounting_evaluations "
               "VALIDATE CONSTRAINT donation_accounting_evaluations_ack_run")
    op.execute("ALTER TABLE public.donation_credit_events ADD CONSTRAINT donation_credit_events_run_id "
               "FOREIGN KEY (run_id) REFERENCES public.payment_accounting_runs (id) NOT VALID")
    op.execute("ALTER TABLE public.donation_credit_events VALIDATE CONSTRAINT donation_credit_events_run_id")
    op.execute(ACK_GUARD)
    op.execute("CREATE TRIGGER donation_accounting_evaluations_ack_guard BEFORE INSERT OR UPDATE "
               "ON public.donation_accounting_evaluations FOR EACH ROW "
               "EXECUTE FUNCTION public.payment_accounting_ack_guard()")
    # ── F-11 ──
    op.execute(SCOPE_COVERS)
    for name in OPERATIONS:
        op.execute(_hardened(module, statements, name))
    for fn in ("payment_accounting_ack_guard()",
               "payment_accounting_scope_covers(jsonb, timestamp with time zone, text, uuid)"):
        op.execute(f"REVOKE ALL ON FUNCTION public.{fn} FROM {EVERYONE}")


def downgrade() -> None:
    _, statements = _render_0034()
    for name in OPERATIONS:  # 0034's bodies, byte for byte
        op.execute(_function_0034(statements, name))
    op.execute("DROP FUNCTION public.payment_accounting_scope_covers(jsonb, timestamp with time zone, text, uuid)")
    op.execute("DROP TRIGGER donation_accounting_evaluations_ack_guard ON public.donation_accounting_evaluations")
    op.execute("DROP FUNCTION public.payment_accounting_ack_guard()")
    op.execute("ALTER TABLE public.donation_credit_events DROP CONSTRAINT donation_credit_events_run_id")
    op.execute("ALTER TABLE public.donation_accounting_evaluations DROP CONSTRAINT donation_accounting_evaluations_ack_run")
