"""No new external identity for an administrator from any API role; operator-verified Google links (admin MFA, S2).

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

Policy: an administrator NEVER links Google self-service, and nothing the API records or displays can lead to a link
(a compromised API or hyperchat_auth could fabricate it). The one way is admin_link_google_identity(), callable by the
operator only (hyperchat_owner, i.e. the migrator, root on the server), from `python -m app.stepup.operator
link-google`, which establishes BOTH facts outside the API (docs/admin-stepup.md):
  * the administrator's identity: confirmed out of band by the operator (recorded as verified_by);
  * control of the exact Google account: the administrator completes Google's device sign-in on their own device for
    the OPERATOR's OAuth client; the tool receives the ID token directly from Google, verifies it (signature,
    issuer, the operator client as audience, freshness, verified email equal to the address the administrator
    stated out of band) and passes its subject here. The database cannot check a Google signature: this function
    trusts its caller, which is why only the owner may call it.
It links exactly that subject, never one linked to any account (never moved), only to an active administrator, and
audits it in stepup_events (identity_linked).

Downgrade drops the function, the trigger and its function, and restores 0025's event list NOT VALID (the audit
history is append-only, so rows of the new type stay).
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

API_ROLES = "('hyperchat_app', 'hyperchat_worker', 'hyperchat_auth', 'hyperchat_lifecycle', 'hyperchat_stepup')"
REFUSE = "USING ERRCODE = 'insufficient_privilege'"
EVENTS_0025 = ("challenge_issued", "registration_ok", "registration_failed", "assertion_ok", "assertion_failed",
               "grant_issued", "grant_consumed", "grant_claimed", "session_assured", "credential_approved",
               "credential_revoked", "admin_recovered", "enforcement_changed", "rate_limited")
EVENTS = EVENTS_0025 + ("identity_linked",)
LINK_FUNCTION = "public.admin_link_google_identity(text, text, text, text, text, jsonb)"


def _events_check(events, not_valid: bool = False) -> None:
    op.execute("ALTER TABLE public.stepup_events DROP CONSTRAINT stepup_events_event_check")
    listed = ", ".join(f"'{e}'" for e in events)
    op.execute(f"ALTER TABLE public.stepup_events ADD CONSTRAINT stepup_events_event_check CHECK (event IN ({listed}))"
               + (" NOT VALID" if not_valid else ""))


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
            RAISE EXCEPTION 'an external identity is never linked to an administrator by the API' {REFUSE};
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.auth_guard_admin_identity_link() FROM PUBLIC")
    op.execute("CREATE TRIGGER aab_auth_guard_admin_identity_link BEFORE INSERT OR UPDATE ON public.user_identities "
               "FOR EACH ROW EXECUTE FUNCTION public.auth_guard_admin_identity_link()")

    _events_check(EVENTS)
    op.execute(f"""
        CREATE FUNCTION public.admin_link_google_identity(p_email text, p_subject text, p_google_email text,
                                                          p_operator text, p_verified_by text, p_evidence jsonb)
        RETURNS text LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE uid uuid;
        BEGIN
            IF current_user <> 'hyperchat_owner' THEN
                RAISE EXCEPTION 'operator only' {REFUSE};
            END IF;
            IF btrim(coalesce(p_operator, '')) = '' OR btrim(coalesce(p_verified_by, '')) = '' THEN
                RETURN 'operator_and_verification_required';
            END IF;
            IF btrim(coalesce(p_subject, '')) = '' OR length(p_subject) > 255 OR p_evidence IS NULL
               OR jsonb_typeof(p_evidence) <> 'object' OR p_evidence->>'method' IS DISTINCT FROM 'google_device_flow' THEN
                RETURN 'verified_google_subject_required';
            END IF;
            SELECT u.id INTO uid FROM public.users u WHERE u.email = p_email::citext AND u.role = 'admin' AND u.is_active
            FOR UPDATE;
            IF uid IS NULL THEN RETURN 'not_admin'; END IF;
            IF EXISTS (SELECT 1 FROM public.user_identities i WHERE i.provider = 'google'
                       AND i.provider_subject = p_subject) THEN
                RETURN 'subject_in_use';  -- that Google account already belongs to an account: never moved
            END IF;
            INSERT INTO public.user_identities (user_id, provider, provider_subject, email)
            VALUES (uid, 'google', p_subject, p_google_email);
            INSERT INTO public.stepup_events (event, user_id, operator, detail)
            VALUES ('identity_linked', uid, p_operator,
                    jsonb_build_object('provider', 'google', 'google_email', p_google_email,
                                       'verified_by', p_verified_by) || p_evidence);
            RETURN 'ok';
        END $$
    """)
    everyone = "hyperchat_app, hyperchat_worker, hyperchat_auth, hyperchat_lifecycle, hyperchat_readonly, " \
               "hyperchat_stepup, PUBLIC"
    op.execute(f"REVOKE ALL ON FUNCTION {LINK_FUNCTION} FROM {everyone}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {LINK_FUNCTION}")
    _events_check(EVENTS_0025, not_valid=True)
    op.execute("DROP TRIGGER aab_auth_guard_admin_identity_link ON public.user_identities")
    op.execute("DROP FUNCTION public.auth_guard_admin_identity_link()")
