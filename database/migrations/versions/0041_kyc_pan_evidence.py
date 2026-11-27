"""PAN evidence for KYC (owner decisions 2026-11-27): which identity document a signed Didit approval used, a
fail-closed PAN rule, a database-owned payout hold for a non-PAN approval, and the creator's PAN acknowledgement.

Revision ID: 0041
Revises: 0040

* Evidence travels the existing signed path: kyc_check_statuses (internal, only applied from a signed webhook or its
  early-webhook replay, ordered like the status) now also records `id_documents`, one object per
  decision.id_verifications item with its status, document_type, document_subtype, issuing_state and
  verification_method: labels of at most 64 characters, never a document number, name or date. liveness_checks may
  be absent (the owner's workflows have none). An admin refresh is unsigned: kyc_apply_refresh keeps the session's
  signed `id_documents` and drops any the caller sends.
* kyc_document_policy: which documents count as PAN (operator configuration; documents match a row when the
  issuing_state is equal and each of the row's non-NULL document_type / document_subtype is equal). Seeded with one
  row, India's tax card: IND + TAX_CARD_STANDARD (Didit's document_subtype; the value and the key are to be confirmed
  in the owner's sandbox test, D7).
* kyc_documents_pan(checks): PAN only when there is at least one document and EVERY one is Approved, read by
  verification_method 'document' and matches a policy row; any missing field means not PAN (fail closed).
* kyc_pan_status(user, environment) -> 'pan_verified' | 'non_pan' | 'not_approved', from the CURRENT approval
  (kyc_effective).
* kyc_payout_holds: the payout hold (owner decision: a non-PAN approval still activates, but payouts are held until
  the creator re-verifies with PAN). Written only by the database: kyc_sync_payout_hold opens a hold (reason
  'non_pan_kyc', the approving session) when the user's current approval is not PAN, and clears it
  ('pan_verified') when a PAN-verified approval is current; losing the approval never clears it. Triggers re-sync
  on every session status / evidence / veto change, and on every policy or account change. No API role can write
  it. Holds open automatically only for approvals recorded after 0041 (their check_statuses carry
  `id_documents`); approvals recorded before get one only when the operator runs kyc_sync_payout_holds() (an owner
  decision), not as a side effect of a configuration change.
* kyc_clear_payout_hold(hold, admin, reason): the administrator's resolution (owner decision 2026-11-27, option C):
  an active admin, a reason, and the step-up action kyc.payout_hold.clear (user:<id>, {hold_id}) claimed at this
  write (0041 widens the stepup_challenges action CHECK); the hold row keeps who, when and why, and a hold an admin
  cleared is not re-opened for the same approving session. Self-service re-verification comes with payouts (DASH-9).
* kyc_pan_acknowledgements: the creator ticked "Only use PAN card for verification, otherwise your payout can be put
  on hold if found using different ID" before KYC: who, when, the text version and its hash, and the session it was
  bound to. The API inserts it and binds it once (guarded); nothing deletes it.

Payment path: kyc_donation_decision and creator_donation_block are unchanged; no PAY-15-pinned function changes.
Downgrade refuses while any acknowledgement or hold exists or the policy differs from its seed; otherwise it restores
0022's two functions from 0022's text. `id_documents` already recorded stays in check_statuses (harmless to 0022).
"""

import importlib.util
import pathlib
import types

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

API_ROLES = "('hyperchat_app', 'hyperchat_worker', 'hyperchat_auth', 'hyperchat_lifecycle', 'hyperchat_stepup')"
EVERYONE = ("hyperchat_app, hyperchat_worker, hyperchat_auth, hyperchat_lifecycle, hyperchat_readonly, "
            "hyperchat_stepup, PUBLIC")
DEFINER = "SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp"
REFUSE = "USING ERRCODE = 'insufficient_privilege'"
LABEL = "CASE WHEN jsonb_typeof(e -> '{k}') = 'string' AND char_length(e ->> '{k}') <= 64 THEN e ->> '{k}' END"
DOC_FIELDS = ("status", "document_type", "document_subtype", "issuing_state", "verification_method")
APP_FUNCTIONS = ("public.kyc_pan_status(uuid, text)", "public.kyc_payout_hold(uuid)",
                 "public.kyc_clear_payout_hold(uuid, uuid, text)")
OPERATOR_FUNCTIONS = ("public.kyc_sync_payout_holds()",)
INTERNAL_FUNCTIONS = ("public.kyc_documents_pan(jsonb)", "public.kyc_sync_payout_hold(uuid, boolean)",
                      "public.kyc_payout_hold_on_session()", "public.kyc_payout_hold_on_config()",
                      "public.kyc_pan_acknowledgements_guard()")
SEED = ("IND", None, "TAX_CARD_STANDARD")
REPLACED_0022 = ("kyc_check_statuses", "kyc_apply_refresh")


def _m0022() -> list[str]:
    path = pathlib.Path(__file__).with_name("0022_kyc_core.py")
    spec = importlib.util.spec_from_file_location("_hyperchat_0022_for_0041", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []
    module.op = types.SimpleNamespace(execute=statements.append)
    module.upgrade()
    return statements


def _function_0022(statements: list[str], name: str) -> str:
    (sql,) = [s for s in statements if f"CREATE FUNCTION public.{name}(" in s]
    return sql.replace(f"CREATE FUNCTION public.{name}(", f"CREATE OR REPLACE FUNCTION public.{name}(", 1)


def _m0037():
    path = pathlib.Path(__file__).with_name("0037_stepup_sec_a_anchors.py")
    spec = importlib.util.spec_from_file_location("_hyperchat_0037_for_0041", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OLD_ACTIONS = _m0037().ACTIONS
ACTIONS = OLD_ACTIONS + ("kyc.payout_hold.clear",)  # app.stepup.canonical.ACTIONS


def _action_check(actions, valid: bool = True) -> None:
    op.execute("ALTER TABLE public.stepup_challenges DROP CONSTRAINT stepup_challenges_action_check")
    quoted = ", ".join(f"'{a}'" for a in actions)
    op.execute(f"ALTER TABLE public.stepup_challenges ADD CONSTRAINT stepup_challenges_action_check "
               f"CHECK (action IN ({quoted})){'' if valid else ' NOT VALID'}")


def upgrade() -> None:
    labels = ", ".join(f"'{k}', {LABEL.format(k=k)}" for k in DOC_FIELDS)
    op.execute(f"""
        CREATE OR REPLACE FUNCTION public.kyc_check_statuses(p_decision jsonb) RETURNS jsonb LANGUAGE sql IMMUTABLE
        SET search_path = pg_catalog AS $$
            SELECT (SELECT COALESCE(jsonb_object_agg(k, (
                        SELECT COALESCE(jsonb_agg(e -> 'status' ORDER BY n), '[]'::jsonb)
                        FROM jsonb_array_elements(p_decision -> k) WITH ORDINALITY AS t(e, n)
                        WHERE jsonb_typeof(e) = 'object' AND jsonb_typeof(e -> 'status') = 'string'
                              AND char_length(e ->> 'status') <= 32)), '{{}}'::jsonb)
                    FROM unnest(ARRAY['id_verifications', 'liveness_checks', 'face_matches', 'aml_screenings']) AS k
                    WHERE jsonb_typeof(p_decision) = 'object' AND jsonb_typeof(p_decision -> k) = 'array')
                -- every signed decision gets the key (an empty list without id_verifications), so "recorded
                -- after 0041" is the key's presence
                || CASE WHEN jsonb_typeof(p_decision) = 'object' THEN jsonb_build_object(
                       'id_documents', CASE WHEN jsonb_typeof(p_decision -> 'id_verifications') = 'array' THEN
                           (SELECT COALESCE(jsonb_agg(jsonb_build_object({labels}) ORDER BY n), '[]'::jsonb)
                            FROM jsonb_array_elements(p_decision -> 'id_verifications') WITH ORDINALITY AS t(e, n)
                            WHERE jsonb_typeof(e) = 'object')
                           ELSE '[]'::jsonb END)
                   ELSE '{{}}'::jsonb END
        $$
    """)
    op.execute(f"""
        CREATE OR REPLACE FUNCTION public.kyc_apply_refresh(p_session_id uuid, p_status text, p_checks jsonb,
                                                            p_observed_at timestamptz, p_actor uuid)
        RETURNS text LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE docs jsonb; checks jsonb;
        BEGIN
            IF p_observed_at IS NULL OR p_observed_at > clock_timestamp()
               OR p_observed_at < clock_timestamp() - interval '2 minutes' THEN
                RAISE EXCEPTION 'a refresh is dated when the provider was asked' USING ERRCODE = 'check_violation';
            END IF;
            IF p_status = 'Approved' THEN
                RETURN 'approval_needs_webhook';
            END IF;
            -- 0041: document evidence comes only from signed webhooks; a refresh keeps what is recorded
            SELECT ks.check_statuses -> 'id_documents' INTO docs FROM public.kyc_sessions ks
            WHERE ks.session_id = p_session_id;
            IF jsonb_typeof(p_checks) = 'object' THEN
                checks := (p_checks - 'id_documents')
                          || CASE WHEN docs IS NULL THEN '{{}}'::jsonb ELSE jsonb_build_object('id_documents', docs) END;
            END IF;
            RETURN public.kyc_record_provider_status(p_session_id, p_status, p_observed_at, checks, 'admin_refresh',
                                                      p_actor);
        END $$
    """)

    # ── the PAN rule ─────────────────────────────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE public.kyc_document_policy (
            id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            issuing_state text NOT NULL CHECK (issuing_state ~ '^[A-Z]{3}$'),
            document_type text CHECK (char_length(document_type) BETWEEN 1 AND 64),
            document_subtype text CHECK (char_length(document_subtype) BETWEEN 1 AND 64),
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (document_type IS NOT NULL OR document_subtype IS NOT NULL),
            UNIQUE NULLS NOT DISTINCT (issuing_state, document_type, document_subtype)
        )
    """)
    op.execute("INSERT INTO public.kyc_document_policy (issuing_state, document_type, document_subtype) "
               f"VALUES ('{SEED[0]}', NULL, '{SEED[2]}')")
    op.execute(f"""
        CREATE FUNCTION public.kyc_documents_pan(p_checks jsonb) RETURNS boolean LANGUAGE sql STABLE {DEFINER} AS $$
            SELECT coalesce(jsonb_typeof(p_checks -> 'id_documents') = 'array'
                   AND jsonb_array_length(p_checks -> 'id_documents') > 0
                   AND NOT EXISTS (
                       SELECT 1 FROM jsonb_array_elements(p_checks -> 'id_documents') d
                       WHERE (d ->> 'status' = 'Approved' AND d ->> 'verification_method' = 'document'
                              AND EXISTS (SELECT 1 FROM public.kyc_document_policy p
                                          WHERE p.issuing_state = d ->> 'issuing_state'
                                            AND (p.document_type IS NULL OR p.document_type = d ->> 'document_type')
                                            AND (p.document_subtype IS NULL
                                                 OR p.document_subtype = d ->> 'document_subtype'))) IS NOT TRUE),
                   false)
        $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.kyc_pan_status(p_user_id uuid, p_environment text) RETURNS text
        LANGUAGE sql STABLE {DEFINER} AS $$
            SELECT CASE WHEN NOT coalesce(e.approved, false) THEN 'not_approved'
                        WHEN public.kyc_documents_pan(s.check_statuses) THEN 'pan_verified'
                        ELSE 'non_pan' END
            FROM public.kyc_effective(p_user_id, p_environment) e
            LEFT JOIN public.kyc_sessions s ON s.session_id = e.session_id
        $$
    """)

    # ── the payout hold ──────────────────────────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE public.kyc_payout_holds (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
            reason text NOT NULL CHECK (reason IN ('non_pan_kyc')),
            session_id uuid,          -- the approving session (no foreign key: the hold outlives it)
            set_at timestamptz NOT NULL DEFAULT now(),
            cleared_at timestamptz,
            cleared_reason text CHECK (cleared_reason IN ('pan_verified', 'admin')),
            cleared_session_id uuid,  -- the PAN-verified session that cleared it
            cleared_by uuid,          -- the administrator who cleared it (no foreign key: the audit outlives them)
            clear_note text CHECK (char_length(clear_note) BETWEEN 1 AND 500),
            CHECK ((cleared_at IS NULL) = (cleared_reason IS NULL)),
            CHECK ((cleared_reason = 'admin') = (cleared_by IS NOT NULL AND clear_note IS NOT NULL))
        )
    """)
    op.execute("CREATE UNIQUE INDEX kyc_payout_holds_one_open ON public.kyc_payout_holds (user_id) "
               "WHERE cleared_at IS NULL")
    op.execute(f"""
        CREATE FUNCTION public.kyc_sync_payout_hold(p_user_id uuid, p_backfill boolean) RETURNS void
        LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE env text; st text; sess uuid; recorded boolean;
        BEGIN
            SELECT environment INTO env FROM public.kyc_provider_config;
            IF env IS NULL THEN
                RETURN;
            END IF;
            st := public.kyc_pan_status(p_user_id, env);
            SELECT e.session_id INTO sess FROM public.kyc_effective(p_user_id, env) e;
            -- automatic opening only for an approval whose decision was recorded after 0041 (it carries the key);
            -- older approvals are held only by the operator's kyc_sync_payout_holds() (an owner decision)
            SELECT ks.check_statuses ? 'id_documents' INTO recorded FROM public.kyc_sessions ks WHERE ks.session_id = sess;
            IF st = 'non_pan' AND (p_backfill OR coalesce(recorded, false)) THEN
                IF NOT EXISTS (SELECT 1 FROM public.kyc_payout_holds h WHERE h.user_id = p_user_id
                               AND h.session_id = sess AND h.cleared_reason = 'admin') THEN  -- an admin resolved it
                    INSERT INTO public.kyc_payout_holds (user_id, reason, session_id)
                    VALUES (p_user_id, 'non_pan_kyc', sess)
                    ON CONFLICT (user_id) WHERE cleared_at IS NULL DO NOTHING;
                END IF;
            ELSIF st = 'pan_verified' THEN
                UPDATE public.kyc_payout_holds SET cleared_at = now(), cleared_reason = 'pan_verified',
                    cleared_session_id = sess
                WHERE user_id = p_user_id AND cleared_at IS NULL;
            END IF;  -- not approved: an open hold stays (only a PAN-verified approval clears it)
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.kyc_payout_hold_on_session() RETURNS trigger LANGUAGE plpgsql {DEFINER} AS $$
        BEGIN
            PERFORM public.kyc_sync_payout_hold(NEW.user_id, false);
            RETURN NULL;
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.kyc_sync_payout_holds() RETURNS integer LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE u uuid; n integer := 0;
        BEGIN
            FOR u IN SELECT DISTINCT user_id FROM public.kyc_sessions LOOP
                PERFORM public.kyc_sync_payout_hold(u, true);
                n := n + 1;
            END LOOP;
            RETURN n;
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.kyc_payout_hold_on_config() RETURNS trigger LANGUAGE plpgsql {DEFINER} AS $$
        BEGIN
            -- a policy row, an account revocation or a workflow epoch can change the current approval without a
            -- session write: re-sync users whose hold is open, and users whose current approval may now be non-PAN
            PERFORM public.kyc_sync_payout_hold(u, false) FROM (
                SELECT DISTINCT s.user_id AS u FROM public.kyc_sessions s WHERE s.provider_status = 'Approved'
                UNION SELECT h.user_id FROM public.kyc_payout_holds h WHERE h.cleared_at IS NULL) x;
            RETURN NULL;
        END $$
    """)
    op.execute("CREATE TRIGGER kyc_payout_hold_on_session AFTER INSERT OR UPDATE OF provider_status, check_statuses, "
               "adverse_at, approved_at ON public.kyc_sessions FOR EACH ROW "
               "EXECUTE FUNCTION public.kyc_payout_hold_on_session()")
    for table in ("kyc_document_policy", "kyc_provider_config", "kyc_provider_accounts"):
        op.execute(f"CREATE TRIGGER kyc_payout_hold_on_config AFTER INSERT OR UPDATE OR DELETE ON public.{table} "
                   "FOR EACH STATEMENT EXECUTE FUNCTION public.kyc_payout_hold_on_config()")
    op.execute(f"""
        CREATE FUNCTION public.kyc_payout_hold(p_user_id uuid) RETURNS TABLE (reason text, since timestamptz)
        LANGUAGE sql STABLE {DEFINER} AS $$
            SELECT h.reason, h.set_at FROM public.kyc_payout_holds h WHERE h.user_id = p_user_id AND h.cleared_at IS NULL
        $$
    """)

    # Owner decision 2026-11-27 (option C, "Admin resolves for now"): an administrator clears a hold, audited with
    # a reason, under the step-up action kyc.payout_hold.clear (user:<id>, {hold_id}); self-service PAN
    # re-verification comes with the payout feature (DASH-9).
    op.execute(f"""
        CREATE FUNCTION public.kyc_clear_payout_hold(p_hold_id uuid, p_actor uuid, p_reason text) RETURNS text
        LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE h public.kyc_payout_holds%ROWTYPE;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM public.users WHERE id = p_actor AND role = 'admin' AND is_active) THEN
                RETURN 'not_authorized';
            END IF;
            IF nullif(btrim(p_reason), '') IS NULL OR char_length(btrim(p_reason)) > 500 THEN
                RETURN 'reason_required';
            END IF;
            SELECT * INTO h FROM public.kyc_payout_holds WHERE id = p_hold_id FOR UPDATE;
            IF NOT FOUND OR h.cleared_at IS NOT NULL THEN
                RETURN 'not_open';
            END IF;
            PERFORM public.stepup_authorize('kyc.payout_hold.clear', 'user:' || h.user_id,
                                            jsonb_build_object('hold_id', h.id::text), 'kyc_payout_holds.clear');
            UPDATE public.kyc_payout_holds SET cleared_at = now(), cleared_reason = 'admin', cleared_by = p_actor,
                clear_note = btrim(p_reason)
            WHERE id = h.id;
            RETURN 'cleared';
        END $$
    """)
    _action_check(ACTIONS)

    # ── the PAN acknowledgement ──────────────────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE public.kyc_pan_acknowledgements (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
            text_version text NOT NULL CHECK (char_length(text_version) BETWEEN 1 AND 64),
            text_sha256 text NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
            acknowledged_at timestamptz NOT NULL DEFAULT now(),
            session_id uuid,    -- the KYC session it was given for (no foreign key, as 0039's lesson)
            bound_at timestamptz,
            CHECK ((session_id IS NULL) = (bound_at IS NULL))
        )
    """)
    op.execute("CREATE INDEX kyc_pan_acknowledgements_user_idx ON public.kyc_pan_acknowledgements "
               "(user_id, acknowledged_at)")
    op.execute(f"""
        CREATE FUNCTION public.kyc_pan_acknowledgements_guard() RETURNS trigger LANGUAGE plpgsql {DEFINER} AS $$
        BEGIN
            IF session_user NOT IN {API_ROLES} THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.acknowledged_at <> now() OR NEW.session_id IS NOT NULL THEN
                    RAISE EXCEPTION 'an acknowledgement is recorded now, before its session' {REFUSE};
                END IF;
                RETURN NEW;
            END IF;
            IF (to_jsonb(NEW) - 'session_id' - 'bound_at') <> (to_jsonb(OLD) - 'session_id' - 'bound_at')
               OR OLD.session_id IS NOT NULL OR NEW.session_id IS NULL OR NEW.bound_at <> now() THEN
                RAISE EXCEPTION 'an acknowledgement is only bound to its session, once' {REFUSE};
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER kyc_pan_acknowledgements_guard BEFORE INSERT OR UPDATE ON public.kyc_pan_acknowledgements "
               "FOR EACH ROW EXECUTE FUNCTION public.kyc_pan_acknowledgements_guard()")

    # ── privileges ───────────────────────────────────────────────────────────────────────────────────────────
    for table in ("kyc_document_policy", "kyc_payout_holds", "kyc_pan_acknowledgements"):
        op.execute(f"REVOKE ALL ON public.{table} FROM hyperchat_app, hyperchat_worker")
    op.execute("GRANT SELECT ON public.kyc_document_policy, public.kyc_payout_holds TO hyperchat_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON public.kyc_pan_acknowledgements TO hyperchat_app")
    op.execute(f"REVOKE ALL ON FUNCTION {', '.join(APP_FUNCTIONS + OPERATOR_FUNCTIONS + INTERNAL_FUNCTIONS)} "
               f"FROM {EVERYONE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {', '.join(APP_FUNCTIONS)} TO hyperchat_app")


def downgrade() -> None:
    op.execute(f"""
        DO $$
        DECLARE acks bigint; holds bigint; policy text;
        BEGIN
            SELECT count(*) INTO acks FROM public.kyc_pan_acknowledgements;
            SELECT count(*) INTO holds FROM public.kyc_payout_holds;
            SELECT string_agg(issuing_state || '/' || coalesce(document_type, '') || '/' || coalesce(document_subtype, ''),
                              ',' ORDER BY id) INTO policy FROM public.kyc_document_policy;
            IF acks > 0 OR holds > 0 OR policy IS DISTINCT FROM '{SEED[0]}//{SEED[2]}' THEN
                RAISE EXCEPTION 'downgrade of 0041 stopped: % PAN acknowledgement(s), % payout hold(s), policy % '
                                '(nothing was changed); the operational rollback is the previous release',
                                acks, holds, policy;
            END IF;
        END $$
    """)
    _action_check(OLD_ACTIONS, valid=False)  # challenges already issued for the action stay as audit history
    op.execute("DROP FUNCTION public.kyc_clear_payout_hold(uuid, uuid, text)")
    statements = _m0022()
    for name in REPLACED_0022:
        op.execute(_function_0022(statements, name))
    op.execute("DROP TRIGGER kyc_payout_hold_on_session ON public.kyc_sessions")
    for table in ("kyc_provider_config", "kyc_provider_accounts"):
        op.execute(f"DROP TRIGGER kyc_payout_hold_on_config ON public.{table}")
    for table in ("kyc_pan_acknowledgements", "kyc_payout_holds", "kyc_document_policy"):
        op.execute(f"DROP TABLE public.{table}")
    for fn in ("public.kyc_payout_hold(uuid)", "public.kyc_sync_payout_holds()", "public.kyc_payout_hold_on_config()",
               "public.kyc_payout_hold_on_session()", "public.kyc_sync_payout_hold(uuid, boolean)",
               "public.kyc_pan_status(uuid, text)",
               "public.kyc_documents_pan(jsonb)", "public.kyc_pan_acknowledgements_guard()"):
        op.execute(f"DROP FUNCTION {fn}")
