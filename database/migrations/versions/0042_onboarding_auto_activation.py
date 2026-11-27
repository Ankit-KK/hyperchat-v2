"""Onboarding Stage 5 Phase B (PDP v1.1; owner decisions Q11, Q12, Q15 and 2026-11-27): automatic activation of an
application-created creator once every condition holds, behind an operator switch that starts OFF.

Revision ID: 0042
Revises: 0041

The rule lives here, in one function the API, the checklist and the worker share:

  * public.onboarding_activation_block(streamer) -> NULL, or the first failing code:
      not_found, not_pending (a suspended, active or rejected creator never auto-activates), not_application_created
      (Q12: an approved application created it and it is on the standard KYC track), restricted / no_owner /
      kyc_required (0023's kyc_activation_block: an open restriction wins, Q15; an active owner; the owner's approved
      KYC, ANY approval: owner decision 2026-11-27), owner_not_invitee (the current owner is the user who accepted the
      application's invitation), pan_evidence_missing (the approval is PAN-verified, or a payout hold covers it:
      see onboarding_pan_covered), profile_picture_missing / background_missing (the pointer is exactly the primary url
      of the creator's 'ready' creator_image_uploads row, migration 0039).
    The Razorpay account is the one condition outside it: the configured list lives in this server's settings, so the
    caller checks it and the transition compares and sets it against the locked row (as admin activation does).
  * public.onboarding_pan_covered(user): the current approval is PAN-verified (0041), or a payout hold for that very
    approval exists (open, or cleared by an administrator). An approval recorded before 0041 carries no document
    evidence and so no automatic hold: it is never auto-activated (the operator backfill kyc_sync_payout_holds() is
    the owner's decision; an administrator may still activate it by hand).
  * public.onboarding_settings: one row, auto_activation false. Only the operator changes it, through
    onboarding_set_auto_activation(on, operator, approval_reference) (Q11: "Operator switch"), and every change is
    one append-only onboarding_settings_events row (a trigger writes it for any update, whoever issues it).
  * public.onboarding_auto_activate(streamer, expected_account): SECURITY DEFINER, EXECUTE for hyperchat_lifecycle
    ONLY. Refuses while the switch is off and in a transaction that consumed a step-up grant (one grant, one action:
    the granted path stays kyc_change_creator_status). Locks the creator (FOR NO KEY UPDATE, the lock
    kyc_restrict_creator and the donations trigger take; nothing else is locked, so no lock order changes),
    compare-and-sets 'pending' and the account, re-evaluates the whole rule under the lock, then writes the
    creator_status_events row (action 'activate', no actor, reason 'auto'), the onboarding_activations success row
    (the inputs as ids and codes) and the status, in that order. Returns 'ok' or the refusal code; on a refusal
    nothing changes.
  * public.onboarding_activations: one row per automatic activation ever (a creator leaves 'pending' once), no
    runtime-role write. Its txid column is the transaction that wrote it.

Step-up (explicit): 0027's stepup_guard_streamers anchors pending -> active on creator.activate for every API role.
It is re-created from 0027's text with exactly one change (rendered and substituted below, asserted to match once):
the transition needs no grant when, and only when, session_user is hyperchat_lifecycle, an onboarding_activations
row for this creator was written in THIS transaction (only onboarding_auto_activate can write one), and the
transaction consumed no grant. A marker ROW, not a setting: hyperchat_lifecycle may also execute
kyc_change_creator_status and set any custom setting, and neither opens the exception. Everything else is 0027's:
manual activation (POST /api/v1/admin/creators/{id}/activate) still claims its creator.activate grant under 'active',
reactivation its creator.reactivate grant, and no runtime role can write either new table.

Downgrade refuses (changing nothing) while the switch is on or any activation or switch event exists; otherwise it
re-creates 0027's guard exactly and drops the objects. No other row is read or written by either direction.
"""

import importlib.util
import pathlib
import types

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

DEFINER = "SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp"
EVERYONE = ("hyperchat_app, hyperchat_worker, hyperchat_auth, hyperchat_lifecycle, hyperchat_readonly, "
            "hyperchat_stepup, PUBLIC")
NEW_TABLES = ("onboarding_settings", "onboarding_settings_events", "onboarding_activations")
APP_FUNCTIONS = ("public.onboarding_activation_block(uuid)", "public.onboarding_pan_covered(uuid)")
LIFECYCLE_FUNCTION = "public.onboarding_auto_activate(uuid, text)"
OPERATOR_FUNCTIONS = ("public.onboarding_set_auto_activation(boolean, text, text)",)
INTERNAL_FUNCTIONS = ("public.onboarding_settings_audit()",)
# the image rule of app.onboarding.checklist.image_state / images.validated_url
READY = ("EXISTS (SELECT 1 FROM public.creator_image_uploads u WHERE u.streamer_id = s.id AND u.kind = '{kind}' "
         "AND u.state = 'ready' AND u.variants -> 0 ->> 'url' = s.{column})")


def upgrade() -> None:
    # ── the switch (Q11) ─────────────────────────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE public.onboarding_settings (
            id boolean PRIMARY KEY DEFAULT true CHECK (id),
            auto_activation boolean NOT NULL DEFAULT false,
            changed_at timestamptz,
            changed_by text CHECK (char_length(changed_by) BETWEEN 1 AND 100),
            approval_reference text CHECK (char_length(approval_reference) BETWEEN 1 AND 200)
        )
    """)
    op.execute("INSERT INTO public.onboarding_settings DEFAULT VALUES")
    op.execute("""
        CREATE TABLE public.onboarding_settings_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            auto_activation boolean NOT NULL,
            changed_by text NOT NULL,
            approval_reference text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(f"""
        CREATE FUNCTION public.onboarding_settings_audit() RETURNS trigger LANGUAGE plpgsql {DEFINER} AS $$
        BEGIN
            IF TG_OP <> 'UPDATE' THEN
                RAISE EXCEPTION 'onboarding_settings is one row: it is only updated' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.changed_by IS NULL OR NEW.approval_reference IS NULL THEN
                RAISE EXCEPTION 'a switch change names the operator and the approval reference'
                    USING ERRCODE = 'check_violation';
            END IF;
            NEW.changed_at := now();
            INSERT INTO public.onboarding_settings_events (auto_activation, changed_by, approval_reference)
            VALUES (NEW.auto_activation, NEW.changed_by, NEW.approval_reference);
            RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER onboarding_settings_audit BEFORE INSERT OR UPDATE OR DELETE ON public.onboarding_settings "
               "FOR EACH ROW EXECUTE FUNCTION public.onboarding_settings_audit()")
    op.execute(f"""
        CREATE FUNCTION public.onboarding_set_auto_activation(p_on boolean, p_operator text, p_approval text)
        RETURNS text LANGUAGE plpgsql {DEFINER} AS $$
        BEGIN
            IF p_on IS NULL OR nullif(btrim(p_operator), '') IS NULL OR nullif(btrim(p_approval), '') IS NULL THEN
                RETURN 'operator_and_approval_required';
            END IF;
            UPDATE public.onboarding_settings SET auto_activation = p_on, changed_by = btrim(p_operator),
                approval_reference = btrim(p_approval);
            RETURN CASE WHEN p_on THEN 'on' ELSE 'off' END;
        END $$
    """)

    # ── the rule ────────────────────────────────────────────────────────────────────────────────────────────
    op.execute(f"""
        CREATE FUNCTION public.onboarding_pan_covered(p_user_id uuid) RETURNS boolean
        LANGUAGE sql STABLE {DEFINER} AS $$
            SELECT coalesce((
                SELECT public.kyc_pan_status(p_user_id, c.environment) = 'pan_verified' OR EXISTS (
                    SELECT 1 FROM public.kyc_effective(p_user_id, c.environment) e
                    JOIN public.kyc_payout_holds h ON h.user_id = p_user_id AND h.session_id = e.session_id
                    WHERE e.approved AND (h.cleared_at IS NULL OR h.cleared_reason = 'admin'))
                FROM public.kyc_provider_config c), false)
        $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.onboarding_activation_block(p_streamer_id uuid) RETURNS text
        LANGUAGE sql STABLE {DEFINER} AS $$
            SELECT CASE
                WHEN s.id IS NULL THEN 'not_found'
                WHEN s.status <> 'pending' THEN 'not_pending'
                WHEN s.kyc_track <> 'standard' OR NOT EXISTS (
                        SELECT 1 FROM public.user_signups a WHERE a.streamer_id = s.id AND a.review_status = 'approved')
                    THEN 'not_application_created'
                WHEN public.kyc_activation_block(s.id) IS NOT NULL THEN public.kyc_activation_block(s.id)
                WHEN NOT EXISTS (
                        SELECT 1 FROM public.user_signups a JOIN public.user_invites i ON i.signup_id = a.id
                        WHERE a.streamer_id = s.id AND a.review_status = 'approved' AND i.accepted_at IS NOT NULL
                          AND i.accepted_user_id = s.user_id)
                    THEN 'owner_not_invitee'
                WHEN NOT public.onboarding_pan_covered(s.user_id) THEN 'pan_evidence_missing'
                WHEN NOT {READY.format(kind="profile", column="brand_logo_url")} THEN 'profile_picture_missing'
                WHEN NOT {READY.format(kind="background", column="background_url")} THEN 'background_missing'
            END
            FROM (SELECT 1) AS one LEFT JOIN public.streamers s ON s.id = p_streamer_id
        $$
    """)

    # ── the transition and its audit ─────────────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE public.onboarding_activations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            streamer_id uuid NOT NULL UNIQUE REFERENCES public.streamers(id) ON DELETE RESTRICT,
            status_event_id uuid NOT NULL REFERENCES public.creator_status_events(id) ON DELETE RESTRICT,
            user_id uuid NOT NULL,               -- the owner (audit ids, no foreign keys: the audit outlives them)
            signup_id uuid NOT NULL,
            invite_id uuid NOT NULL,
            kyc_session_id uuid,
            pan_status text NOT NULL CHECK (pan_status IN ('pan_verified', 'non_pan')),
            payout_hold_id uuid,                 -- the hold that covers a non-PAN approval
            profile_upload_id uuid NOT NULL,
            background_upload_id uuid NOT NULL,
            razorpay_account text NOT NULL,
            txid xid8 NOT NULL DEFAULT pg_current_xact_id(),  -- read by stepup_guard_streamers
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(f"""
        CREATE FUNCTION public.onboarding_auto_activate(p_streamer_id uuid, p_expected_account text) RETURNS text
        LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE s public.streamers%ROWTYPE; refusal text; env text; sess uuid; pan text; hold uuid; signup uuid;
                invite uuid; logo uuid; background uuid; event uuid;
        BEGIN
            IF (SELECT auto_activation FROM public.onboarding_settings) IS NOT TRUE THEN
                RETURN 'switched_off';
            END IF;
            IF EXISTS (SELECT 1 FROM public.stepup_grants WHERE consumed_txid = pg_current_xact_id_if_assigned()) THEN
                RETURN 'grant_presented';  -- one grant, one action: the granted path is kyc_change_creator_status
            END IF;
            SELECT * INTO s FROM public.streamers WHERE id = p_streamer_id FOR NO KEY UPDATE;
            IF NOT FOUND THEN
                RETURN 'not_found';
            END IF;
            IF s.status <> 'pending' THEN
                RETURN 'status_changed';
            END IF;
            IF p_expected_account IS NULL OR s.razorpay_account IS DISTINCT FROM p_expected_account THEN
                RETURN 'account_changed';  -- not the account the worker found configured on this server
            END IF;
            refusal := public.onboarding_activation_block(s.id);  -- under the creator lock: a restriction wins
            IF refusal IS NOT NULL THEN
                RETURN refusal;
            END IF;
            SELECT environment INTO env FROM public.kyc_provider_config;
            SELECT e.session_id INTO sess FROM public.kyc_effective(s.user_id, env) e;
            pan := public.kyc_pan_status(s.user_id, env);
            SELECT h.id INTO hold FROM public.kyc_payout_holds h WHERE h.user_id = s.user_id AND h.session_id = sess
            ORDER BY h.set_at DESC, h.id LIMIT 1;
            SELECT a.id, i.id INTO signup, invite FROM public.user_signups a JOIN public.user_invites i ON i.signup_id = a.id
            WHERE a.streamer_id = s.id AND a.review_status = 'approved' AND i.accepted_at IS NOT NULL
              AND i.accepted_user_id = s.user_id
            ORDER BY i.accepted_at DESC, i.id LIMIT 1;
            SELECT id INTO logo FROM public.creator_image_uploads WHERE streamer_id = s.id AND kind = 'profile'
              AND state = 'ready';
            SELECT id INTO background FROM public.creator_image_uploads WHERE streamer_id = s.id AND kind = 'background'
              AND state = 'ready';
            INSERT INTO public.creator_status_events (streamer_id, action, from_status, to_status, actor_user_id, reason)
            VALUES (s.id, 'activate', 'pending', 'active', NULL, 'auto') RETURNING id INTO event;
            INSERT INTO public.onboarding_activations (streamer_id, status_event_id, user_id, signup_id, invite_id,
                kyc_session_id, pan_status, payout_hold_id, profile_upload_id, background_upload_id, razorpay_account)
            VALUES (s.id, event, s.user_id, signup, invite, sess, pan, CASE WHEN pan = 'non_pan' THEN hold END, logo,
                    background, s.razorpay_account);
            UPDATE public.streamers SET status = 'active' WHERE id = s.id;  -- the guard reads the row above
            RETURN 'ok';
        END $$
    """)

    # ── privileges ───────────────────────────────────────────────────────────────────────────────────────────
    for table in NEW_TABLES:  # 0007's defaults gave app and worker DML, readonly SELECT
        op.execute(f"REVOKE ALL ON public.{table} FROM hyperchat_app, hyperchat_worker")
    op.execute("GRANT SELECT ON public.onboarding_settings, public.onboarding_activations TO hyperchat_app")
    functions = APP_FUNCTIONS + (LIFECYCLE_FUNCTION,) + OPERATOR_FUNCTIONS + INTERNAL_FUNCTIONS
    op.execute(f"REVOKE ALL ON FUNCTION {', '.join(functions)} FROM {EVERYONE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {', '.join(APP_FUNCTIONS)} TO hyperchat_app")
    op.execute(f"GRANT EXECUTE ON FUNCTION {LIFECYCLE_FUNCTION} TO hyperchat_lifecycle")
    op.execute(streamers_guard())  # CREATE OR REPLACE keeps 0027's grants (REVOKE ALL) and its trigger


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF (SELECT auto_activation FROM public.onboarding_settings)
               OR EXISTS (SELECT 1 FROM public.onboarding_activations)
               OR EXISTS (SELECT 1 FROM public.onboarding_settings_events) THEN
                RAISE EXCEPTION 'downgrade of 0042 stopped: automatic activation is on, or has an activation or a '
                                'switch change on record (audit rows are never dropped)';
            END IF;
        END $$
    """)
    op.execute(_function_0027(_render_0027()[1], GUARD))
    op.execute(f"DROP FUNCTION {LIFECYCLE_FUNCTION}, public.onboarding_activation_block(uuid), "
               f"public.onboarding_pan_covered(uuid), {OPERATOR_FUNCTIONS[0]}")
    op.execute("DROP TABLE public.onboarding_activations, public.onboarding_settings, "
               "public.onboarding_settings_events")
    op.execute(f"DROP FUNCTION {INTERNAL_FUNCTIONS[0]}")


# ── the guard exception ───────────────────────────────────────────────────────────────────────────────────────
GUARD = "stepup_guard_streamers"
_ANCHOR = "            IF NEW.status = 'active' AND OLD.status IN ('pending', 'suspended') THEN\n"
_EXCEPTION = """\
            IF NEW.status = 'active' AND OLD.status IN ('pending', 'suspended') AND (
                    OLD.status = 'pending' AND session_user = 'hyperchat_lifecycle'  -- 0042: onboarding_auto_activate
                    AND EXISTS (SELECT 1 FROM public.onboarding_activations a  -- its success row, this transaction
                                WHERE a.streamer_id = NEW.id AND a.txid = pg_current_xact_id())
                    AND NOT EXISTS (SELECT 1 FROM public.stepup_grants
                                    WHERE consumed_txid = pg_current_xact_id_if_assigned())) IS NOT TRUE THEN
"""


def _render_0027() -> tuple[types.ModuleType, list[str]]:
    """0027's module and its upgrade() statements, rendered without a database (as 0038 does)."""
    path = pathlib.Path(__file__).with_name("0027_stepup_enforcement.py")
    spec = importlib.util.spec_from_file_location("_hyperchat_0027_for_0042", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []
    module.op = types.SimpleNamespace(execute=statements.append)
    module.upgrade()
    return module, statements


def _function_0027(statements: list[str], name: str) -> str:
    (sql,) = [s for s in statements if f"CREATE FUNCTION public.{name}()" in s]
    return sql.replace(f"CREATE FUNCTION public.{name}()", f"CREATE OR REPLACE FUNCTION public.{name}()", 1)


def streamers_guard() -> str:
    sql = _function_0027(_render_0027()[1], GUARD)
    assert sql.count(_ANCHOR) == 1, "0027 text changed: the creator activation anchor"
    return sql.replace(_ANCHOR, _EXCEPTION)
