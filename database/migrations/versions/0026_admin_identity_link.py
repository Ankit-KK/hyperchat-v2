"""No new external identity for an administrator from any API role (administrator MFA, S2).

Revision ID: 0026
Revises: 0025

Migration 0024 refused the runtime roles (hyperchat_app, hyperchat_worker) linking an identity to an administrator,
but hyperchat_auth could still do it, and Google sign-in did: a verified Google account whose email matched an
administrator's was linked to that administrator and signed in (control of the mailbox's Google account meant
admin access). S2 removes that path in the API (app.auth.service.google_login, AuthRepository.google_sign_in); this
guard makes it hold in the database for every role the API connects as, hyperchat_auth included:
  * INSERT into user_identities for an administrator is refused, unless that very identity (provider, subject) is
    already linked to that administrator, which is what Google sign-in's INSERT ... ON CONFLICT DO UPDATE (recording
    the last sign-in of an already linked identity) needs: BEFORE INSERT triggers fire before the conflict check;
  * UPDATE that moves an identity onto an administrator, or changes its provider or subject while it belongs to one,
    is refused.
Identities already linked to administrators keep working; the owner (migrations, the operator) is unaffected.

Downgrade drops the trigger and its function.
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

API_ROLES = "('hyperchat_app', 'hyperchat_worker', 'hyperchat_auth', 'hyperchat_lifecycle', 'hyperchat_stepup')"


def upgrade() -> None:
    op.execute(f"""
        CREATE FUNCTION public.auth_guard_admin_identity_link() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        BEGIN
            IF current_user NOT IN {API_ROLES} THEN
                RETURN NEW;
            END IF;
            IF NOT COALESCE((SELECT u.role = 'admin' FROM public.users u WHERE u.id = NEW.user_id FOR SHARE OF u), false)
            THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'INSERT' AND EXISTS (
                    SELECT 1 FROM public.user_identities i WHERE i.provider = NEW.provider
                      AND i.provider_subject = NEW.provider_subject AND i.user_id = NEW.user_id) THEN
                RETURN NEW;  -- an already linked identity: the upsert only records the sign-in
            END IF;
            IF TG_OP = 'UPDATE' AND (NEW.user_id, NEW.provider, NEW.provider_subject)
                                    = (OLD.user_id, OLD.provider, OLD.provider_subject) THEN
                RETURN NEW;  -- the same identity of the same administrator (last sign-in, email)
            END IF;
            RAISE EXCEPTION 'an external identity is never linked to an administrator by the API'
                USING ERRCODE = 'insufficient_privilege';
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.auth_guard_admin_identity_link() FROM PUBLIC")
    op.execute("CREATE TRIGGER aab_auth_guard_admin_identity_link BEFORE INSERT OR UPDATE ON public.user_identities "
               "FOR EACH ROW EXECUTE FUNCTION public.auth_guard_admin_identity_link()")


def downgrade() -> None:
    op.execute("DROP TRIGGER aab_auth_guard_admin_identity_link ON public.user_identities")
    op.execute("DROP FUNCTION public.auth_guard_admin_identity_link()")
