"""No new external identity for an administrator from any API role; operator-approved Google links (admin MFA, S2).

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

Policy: an administrator NEVER links Google self-service (not by email, not after an email change, a recovery or an
invitation). The one way is operator-approved:
  admin_identity_link_requests  a refused administrator Google sign-in records (through hyperchat_auth) WHO
                                (the administrator), WHICH Google account (subject) and a reference code shown to the
                                person. A request grants nothing: at most 3 open per administrator, 24 hours, decided
                                once, only by the operator.
  admin_link_google_identity()  operator only (hyperchat_owner): links the request's exact subject to the request's
  admin_reject_google_link()    administrator, after confirming out of band that this administrator made it
                                (--verified-by), or rejects it; audited in stepup_events.

Downgrade drops the requests, the functions, the trigger and its function, and restores 0025's event list NOT VALID
(the audit history is append-only, so rows of the new types stay).
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
EVENTS = EVENTS_0025 + ("identity_linked", "identity_link_rejected")
OPERATOR_FUNCTIONS = ("public.admin_link_google_identity(text, text, text, text)",
                      "public.admin_reject_google_link(text, text, text, text)")


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

    # ── operator-approved Google links ─────────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE public.admin_identity_link_requests (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
            provider text NOT NULL CHECK (provider = 'google'),
            provider_subject text NOT NULL CHECK (length(provider_subject) BETWEEN 1 AND 255),
            email text CHECK (length(email) <= 320),
            code text NOT NULL UNIQUE CHECK (code ~ '^[A-Z2-7]{5}-[A-Z2-7]{5}$'),
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            decided_at timestamptz,
            decision text CHECK (decision IN ('linked', 'rejected')),
            decided_by text,
            CHECK ((decided_at IS NULL) = (decision IS NULL) AND (decided_at IS NULL) = (decided_by IS NULL))
        )
    """)
    op.execute("CREATE INDEX admin_identity_link_requests_open_idx ON public.admin_identity_link_requests "
               "(user_id, provider_subject) WHERE decided_at IS NULL")
    op.execute(f"""
        CREATE FUNCTION public.admin_link_requests_guard() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF current_user <> 'hyperchat_owner' THEN
                    RAISE EXCEPTION 'link requests are kept' {REFUSE};
                END IF;
                RETURN OLD;
            END IF;
            IF TG_OP = 'INSERT' THEN
                NEW.created_at := now();
                NEW.expires_at := now() + interval '24 hours';
                IF NEW.decided_at IS NOT NULL OR NEW.decision IS NOT NULL OR NEW.decided_by IS NOT NULL THEN
                    RAISE EXCEPTION 'a link request is recorded undecided' {REFUSE};
                END IF;
                IF NOT COALESCE((SELECT u.role = 'admin' AND u.is_active FROM public.users u WHERE u.id = NEW.user_id),
                                false) THEN
                    RAISE EXCEPTION 'link requests are for active administrators' {REFUSE};
                END IF;
                IF (SELECT count(*) FROM public.admin_identity_link_requests r WHERE r.user_id = NEW.user_id
                    AND r.decided_at IS NULL AND r.expires_at > now()) >= 3 THEN
                    RAISE EXCEPTION 'too many open link requests' {REFUSE};
                END IF;
                RETURN NEW;
            END IF;
            -- UPDATE: decided once, by the operator, nothing else changes
            IF current_user <> 'hyperchat_owner' OR OLD.decided_at IS NOT NULL OR NEW.decided_at IS NULL
               OR (to_jsonb(NEW) - 'decided_at' - 'decision' - 'decided_by')
                  <> (to_jsonb(OLD) - 'decided_at' - 'decision' - 'decided_by') THEN
                RAISE EXCEPTION 'a link request is decided once, by the operator' {REFUSE};
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.admin_link_requests_guard() FROM PUBLIC")
    op.execute("CREATE TRIGGER aaa_admin_link_requests_guard BEFORE INSERT OR UPDATE OR DELETE "
               "ON public.admin_identity_link_requests FOR EACH ROW EXECUTE FUNCTION public.admin_link_requests_guard()")

    _events_check(EVENTS)
    op.execute(f"""
        CREATE FUNCTION public.admin_link_google_identity(p_email text, p_code text, p_operator text,
                                                          p_verified_by text)
        RETURNS text LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE r public.admin_identity_link_requests;
        BEGIN
            IF current_user <> 'hyperchat_owner' THEN
                RAISE EXCEPTION 'operator only' {REFUSE};
            END IF;
            IF btrim(coalesce(p_operator, '')) = '' OR btrim(coalesce(p_verified_by, '')) = '' THEN
                RETURN 'operator_and_verification_required';
            END IF;
            SELECT q.* INTO r FROM public.admin_identity_link_requests q JOIN public.users u ON u.id = q.user_id
            WHERE q.code = upper(btrim(p_code)) AND u.email = p_email::citext FOR UPDATE OF q;
            IF NOT FOUND THEN RETURN 'not_found'; END IF;  -- the code must belong to THIS administrator
            IF r.decided_at IS NOT NULL THEN RETURN 'decided'; END IF;
            IF r.expires_at <= now() THEN RETURN 'expired'; END IF;
            IF NOT COALESCE((SELECT u.role = 'admin' AND u.is_active FROM public.users u WHERE u.id = r.user_id
                             FOR UPDATE OF u), false) THEN
                RETURN 'not_admin';
            END IF;
            IF EXISTS (SELECT 1 FROM public.user_identities i WHERE i.provider = r.provider
                       AND i.provider_subject = r.provider_subject) THEN
                RETURN 'subject_in_use';  -- that Google account already belongs to an account: never moved
            END IF;
            INSERT INTO public.user_identities (user_id, provider, provider_subject, email)
            VALUES (r.user_id, r.provider, r.provider_subject, r.email);
            UPDATE public.admin_identity_link_requests SET decided_at = now(), decision = 'linked',
                   decided_by = p_operator WHERE id = r.id;
            INSERT INTO public.stepup_events (event, user_id, operator, detail)
            VALUES ('identity_linked', r.user_id, p_operator,
                    jsonb_build_object('provider', r.provider, 'code', r.code, 'verified_by', p_verified_by));
            RETURN 'ok';
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.admin_reject_google_link(p_email text, p_code text, p_operator text, p_reason text)
        RETURNS text LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE r public.admin_identity_link_requests;
        BEGIN
            IF current_user <> 'hyperchat_owner' THEN
                RAISE EXCEPTION 'operator only' {REFUSE};
            END IF;
            IF btrim(coalesce(p_operator, '')) = '' OR btrim(coalesce(p_reason, '')) = '' THEN
                RETURN 'operator_and_reason_required';
            END IF;
            SELECT q.* INTO r FROM public.admin_identity_link_requests q JOIN public.users u ON u.id = q.user_id
            WHERE q.code = upper(btrim(p_code)) AND u.email = p_email::citext FOR UPDATE OF q;
            IF NOT FOUND THEN RETURN 'not_found'; END IF;
            IF r.decided_at IS NOT NULL THEN RETURN 'decided'; END IF;
            UPDATE public.admin_identity_link_requests SET decided_at = now(), decision = 'rejected',
                   decided_by = p_operator WHERE id = r.id;
            INSERT INTO public.stepup_events (event, user_id, operator, detail)
            VALUES ('identity_link_rejected', r.user_id, p_operator,
                    jsonb_build_object('provider', r.provider, 'code', r.code, 'reason', p_reason));
            RETURN 'ok';
        END $$
    """)
    everyone = "hyperchat_app, hyperchat_worker, hyperchat_auth, hyperchat_lifecycle, hyperchat_readonly, " \
               "hyperchat_stepup, PUBLIC"
    op.execute(f"REVOKE ALL ON public.admin_identity_link_requests FROM {everyone}")  # 0007's default grants
    for fn in OPERATOR_FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM {everyone}")
    # The authentication role records a refused administrator's request and finds an open one again (same code).
    op.execute("GRANT SELECT, INSERT ON public.admin_identity_link_requests TO hyperchat_auth")


def downgrade() -> None:
    for fn in OPERATOR_FUNCTIONS:
        op.execute(f"DROP FUNCTION {fn}")
    op.execute("DROP TABLE public.admin_identity_link_requests")
    op.execute("DROP FUNCTION public.admin_link_requests_guard()")
    # The audit history is append-only: rows of the new types may exist, so the old list is restored NOT VALID.
    _events_check(EVENTS_0025, not_valid=True)
    op.execute("DROP TRIGGER aab_auth_guard_admin_identity_link ON public.user_identities")
    op.execute("DROP FUNCTION public.auth_guard_admin_identity_link()")
