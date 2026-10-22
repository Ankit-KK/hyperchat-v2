"""Audited emergency return of administrator MFA enforcement to 'draft' (post-S3 audit A5).

Revision ID: 0028
Revises: 0027

Before: switching stepup_enforcement back to 'draft' (e.g. during an administrator lockout) meant hand-written SQL as
hyperchat_owner; the audit event (0025's stepup_guard_enforcement) recorded the operator and the from/to states but
no reason. Now:
  * stepup_enforcement.change_reason: the reason given WITH a change. The guard copies it into that change's
    enforcement_changed event and stores NULL, so a later change never inherits it.
  * public.stepup_enforcement_draft(operator, reason): owner only (the operator tool, app.stepup.operator
    enforcement-draft), EXECUTE revoked from PUBLIC and every runtime role. Requires a non-empty operator and reason,
    locks the row (concurrent calls serialize), changes 'active' -> 'draft' once ('already_draft' changes and records
    nothing), and never touches passkeys, grants, assurance or the audit history. The state change itself is
    recorded by the existing guard as an append-only enforcement_changed event, now with the reason.
Activation stays a separate approved step (the 0025 guard still requires two active passkeys per administrator).

Downgrade drops the function and the column and restores 0025's guard.
"""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

REFUSE = "USING ERRCODE = 'insufficient_privilege'"
EVERYONE = ("hyperchat_app, hyperchat_worker, hyperchat_auth, hyperchat_lifecycle, hyperchat_readonly, "
            "hyperchat_stepup, PUBLIC")


def _guard(detail: str, reason: str = "") -> str:
    return f"""
        CREATE OR REPLACE FUNCTION public.stepup_guard_enforcement() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        DECLARE missing integer;{" why text;" if reason else ""}
        BEGIN
            IF TG_OP <> 'UPDATE' OR current_user <> 'hyperchat_owner' THEN
                RAISE EXCEPTION 'only the operator changes MFA enforcement' {REFUSE};
            END IF;
            IF NEW.state = 'active' AND OLD.state <> 'active' THEN
                SELECT count(*) INTO missing FROM public.stepup_enforcement_readiness() r WHERE r.active_credentials < 2;
                IF missing > 0 OR NOT EXISTS (SELECT 1 FROM public.users WHERE role = 'admin' AND is_active) THEN
                    RAISE EXCEPTION 'MFA enforcement needs every active administrator to have two active passkeys'
                        {REFUSE};
                END IF;
            END IF;
            NEW.changed_at := now();{reason}
            INSERT INTO public.stepup_events (event, operator, detail)
            VALUES ('enforcement_changed', NEW.changed_by, {detail});
            RETURN NEW;
        END $$
    """


def upgrade() -> None:
    op.execute("ALTER TABLE public.stepup_enforcement ADD COLUMN change_reason text "
               "CHECK (change_reason IS NULL OR length(change_reason) <= 500)")
    op.execute(_guard("jsonb_strip_nulls(jsonb_build_object('from', OLD.state, 'to', NEW.state, 'reason', why))",
                      reason="""
            why := NEW.change_reason;  -- the reason given with THIS change goes into its event; the row never keeps
            NEW.change_reason := NULL;  -- one, so a later change can never inherit it"""))
    op.execute(f"""
        CREATE FUNCTION public.stepup_enforcement_draft(p_operator text, p_reason text)
        RETURNS text LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE current_state text;
        BEGIN
            IF current_user <> 'hyperchat_owner' THEN
                RAISE EXCEPTION 'operator only' {REFUSE};
            END IF;
            IF btrim(coalesce(p_operator, '')) = '' OR btrim(coalesce(p_reason, '')) = '' THEN
                RETURN 'operator_and_reason_required';
            END IF;
            IF length(p_reason) > 500 OR length(p_operator) > 200 THEN
                RETURN 'too_long';
            END IF;
            SELECT state INTO current_state FROM public.stepup_enforcement FOR UPDATE;  -- concurrent calls serialize
            IF NOT FOUND THEN
                RAISE EXCEPTION 'the enforcement row is missing';
            END IF;
            IF current_state = 'draft' THEN
                RETURN 'already_draft';  -- nothing changed, nothing recorded
            END IF;
            UPDATE public.stepup_enforcement
            SET state = 'draft', changed_by = btrim(p_operator), change_reason = btrim(p_reason);
            RETURN 'ok';
        END $$
    """)
    op.execute(f"REVOKE ALL ON FUNCTION public.stepup_enforcement_draft(text, text) FROM {EVERYONE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION public.stepup_enforcement_draft(text, text)")
    op.execute(_guard("jsonb_build_object('from', OLD.state, 'to', NEW.state)"))
    op.execute("ALTER TABLE public.stepup_enforcement DROP COLUMN change_reason")
