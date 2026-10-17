"""Step-up authentication core (administrator MFA, S1): WebAuthn credentials, challenges, one-time grants, session
assurance, audit events, rate limits and the enforcement state, behind a dedicated verifier role.

Revision ID: 0025
Revises: 0024

The trust boundary (docs/admin-stepup.md): a WebAuthn signature is verified by the step-up verifier, a separate
service with its own OS user and its own database role, hyperchat_stepup, which the API's OS user cannot open
(deploy/postgres/pg_ident*.conf). Only that role can record what a verification proves:
  webauthn_credentials  registered passkeys. The verifier inserts them PENDING and may only advance their sign count;
                        only the operator (hyperchat_owner, via the migrator on the server) approves or revokes one.
  stepup_challenges     single-use, expiring challenges. A challenge is SHA-256 over the canonical action it signs
                        (app.stepup.canonical); the database recomputes the hash and checks that the canonical text
                        names the row's own purpose, administrator, session, action, target, parameter hash and expiry.
  stepup_grants         one-time authorizations for ONE action, written by the verifier only for a challenge it has
                        just verified, bound to administrator, session, credential, action, target and exact
                        parameters, expiring within 2 minutes. Consumed once through stepup_consume() (the API), then
                        claimed once, in that same transaction, by the action's anchor write through stepup_claim()
                        (S3's guards); stepup_effect() records that action's secondary effects, each once.
  session_assurance     "this session passed a passkey ceremony": written only by the verifier, read through
                        stepup_session_aal(), which also requires the credential, the administrator and the session to
                        still be valid.
  stepup_events         append-only audit history: nobody may update, delete or truncate it.
  stepup_rate_limits    the verifier's own attempt counters (the API's roles cannot clear them).
  stepup_enforcement    'draft' (this migration) or 'active'. Only the operator changes it, and activation is refused
                        while any active administrator has fewer than two active credentials. Nothing in S1 reads it
                        to refuse anything; S2/S3 enforce.
The API's roles (hyperchat_app / _auth / _lifecycle / _worker) get no access to these tables except reading the
enforcement state; they may call stepup_consume() and stepup_session_aal(). Migration 0007's default privileges
would give the runtime roles DML on every new table, so each table here is revoked explicitly first.

Downgrade drops everything this migration created (the audit history included).
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

STEPUP = "hyperchat_stepup"
API_ROLES = ("hyperchat_app", "hyperchat_worker", "hyperchat_auth", "hyperchat_lifecycle")
TABLES = ("webauthn_credentials", "stepup_challenges", "stepup_grants", "stepup_grant_effects", "session_assurance",
          "stepup_events", "stepup_rate_limits", "stepup_enforcement")
DEFINER = "SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp"
REFUSE = "USING ERRCODE = 'insufficient_privilege'"
# Every action a challenge may name. Grants exist only for these; S2/S3 bind each to its route and database write.
ACTIONS = ("session.aal2", "mfa.register", "creator.activate", "creator.reactivate", "creator.create",
           "creator.owner.assign", "creator.owner.transfer", "application.approve", "application.invite",
           "admin.invite", "admin.user.change", "admin.user.email", "partner_key.create", "mfa.credential.revoke")
EVENTS = ("challenge_issued", "registration_ok", "registration_failed", "assertion_ok", "assertion_failed",
          "grant_issued", "grant_consumed", "grant_claimed", "session_assured", "credential_approved", "credential_revoked",
          "admin_recovered", "enforcement_changed", "rate_limited")
OPERATOR_FUNCTIONS = ("public.stepup_approve_credential(text, text, text, text)",
                      "public.stepup_revoke_credential(text, text, text, text)",
                      "public.stepup_recover_admin(text, text, text)", "public.stepup_enforcement_readiness()")
GUARD_HELPERS = ("public.stepup_claim(uuid, uuid, text, text, text, text)",
                 "public.stepup_effect(uuid, uuid, text, text, text, text)")  # for S3's guards: owner only
INTERNAL_FUNCTIONS = GUARD_HELPERS + ("public.stepup_admin_ok(uuid)",)
API_FUNCTIONS = ("public.stepup_consume(uuid, uuid, uuid, text, text, text)", "public.stepup_session_aal(uuid)")
TRIGGER_FUNCTIONS = ("stepup_guard_credentials", "stepup_guard_challenges", "stepup_guard_grants", "stepup_guard_effects",
                     "stepup_guard_assurance", "stepup_guard_events", "stepup_guard_enforcement")


def _quoted(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    # An active administrator, read under a share lock so a concurrent demotion or deactivation is waited for. Runs as
    # the owner: FOR SHARE needs an UPDATE privilege on users, which hyperchat_stepup deliberately does not have.
    op.execute(f"""
        CREATE FUNCTION public.stepup_admin_ok(p_user uuid) RETURNS boolean LANGUAGE sql VOLATILE {DEFINER} AS $$
            SELECT COALESCE((SELECT u.role = 'admin' AND u.is_active FROM public.users u WHERE u.id = p_user
                             FOR SHARE OF u), false)
        $$
    """)

    op.execute("""
        CREATE TABLE public.webauthn_credentials (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
            credential_id bytea NOT NULL UNIQUE CHECK (octet_length(credential_id) BETWEEN 16 AND 1023),
            public_key bytea NOT NULL CHECK (octet_length(public_key) BETWEEN 32 AND 2048),
            fingerprint text NOT NULL UNIQUE CHECK (fingerprint ~ '^[A-Z2-7]{4}(-[A-Z2-7]{4}){4}$'),
            sign_count bigint NOT NULL DEFAULT 0 CHECK (sign_count >= 0),
            aaguid uuid,
            device_type text CHECK (device_type IN ('single_device', 'multi_device')),
            backed_up boolean NOT NULL DEFAULT false,
            status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'revoked')),
            created_at timestamptz NOT NULL DEFAULT now(),
            approved_at timestamptz,
            approved_by text,
            revoked_at timestamptz,
            revoked_by text,
            revoke_reason text,
            last_used_at timestamptz,
            CONSTRAINT webauthn_credentials_state CHECK (
                (status = 'pending' AND approved_at IS NULL AND approved_by IS NULL AND revoked_at IS NULL)
                OR (status = 'active' AND approved_at IS NOT NULL AND approved_by <> '' AND revoked_at IS NULL)
                OR (status = 'revoked' AND revoked_at IS NOT NULL AND revoked_by <> '' AND revoke_reason <> ''))
        )
    """)
    op.execute("CREATE INDEX webauthn_credentials_user_idx ON public.webauthn_credentials (user_id, status)")

    op.execute(f"""
        CREATE TABLE public.stepup_challenges (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
            session_id uuid NOT NULL REFERENCES public.sessions(id) ON DELETE CASCADE,
            purpose text NOT NULL CHECK (purpose IN ('register', 'session', 'action')),
            action text NOT NULL CHECK (action IN ({_quoted(ACTIONS)})),
            target text NOT NULL CHECK (length(target) BETWEEN 1 AND 200),
            params_hash text NOT NULL CHECK (params_hash ~ '^[0-9a-f]{{64}}$'),
            canonical text NOT NULL CHECK (length(canonical) <= 8192),
            challenge bytea NOT NULL UNIQUE CHECK (octet_length(challenge) = 32),
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            used_at timestamptz,
            outcome text CHECK (outcome IN ('ok', 'failed')),
            CHECK ((used_at IS NULL) = (outcome IS NULL)),
            CHECK (expires_at > created_at AND expires_at <= created_at + interval '5 minutes'),
            CHECK ((purpose = 'register') = (action = 'mfa.register')),
            CHECK ((purpose = 'session') = (action = 'session.aal2'))
        )
    """)
    op.execute("CREATE INDEX stepup_challenges_open_idx ON public.stepup_challenges (expires_at) WHERE used_at IS NULL")

    op.execute("""
        CREATE TABLE public.stepup_grants (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
            session_id uuid NOT NULL REFERENCES public.sessions(id) ON DELETE CASCADE,
            credential_id uuid NOT NULL REFERENCES public.webauthn_credentials(id) ON DELETE CASCADE,
            challenge_id uuid NOT NULL UNIQUE REFERENCES public.stepup_challenges(id) ON DELETE CASCADE,
            action text NOT NULL,
            target text NOT NULL,
            params_hash text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            consumed_at timestamptz,
            consumed_txid xid8,
            claimed_at timestamptz,
            claimed_anchor text CHECK (length(claimed_anchor) BETWEEN 1 AND 200),
            CHECK ((consumed_at IS NULL) = (consumed_txid IS NULL)),
            CHECK ((claimed_at IS NULL) = (claimed_anchor IS NULL)),
            CHECK (claimed_at IS NULL OR consumed_at IS NOT NULL),
            CHECK (expires_at > created_at AND expires_at <= created_at + interval '2 minutes')
        )
    """)
    # The secondary effects of ONE claimed grant (one logical action): each named effect at most once per grant.
    op.execute("""
        CREATE TABLE public.stepup_grant_effects (
            grant_id uuid NOT NULL REFERENCES public.stepup_grants(id) ON DELETE CASCADE,
            effect text NOT NULL CHECK (length(effect) BETWEEN 1 AND 200),
            at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (grant_id, effect)
        )
    """)

    op.execute("""
        CREATE TABLE public.session_assurance (
            session_id uuid PRIMARY KEY REFERENCES public.sessions(id) ON DELETE CASCADE,
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
            credential_id uuid NOT NULL REFERENCES public.webauthn_credentials(id) ON DELETE CASCADE,
            challenge_id uuid NOT NULL UNIQUE REFERENCES public.stepup_challenges(id) ON DELETE CASCADE,
            aal smallint NOT NULL CHECK (aal = 2),
            verified_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            CHECK (expires_at > verified_at AND expires_at <= verified_at + interval '12 hours')
        )
    """)

    op.execute(f"""
        CREATE TABLE public.stepup_events (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            at timestamptz NOT NULL DEFAULT now(),
            event text NOT NULL CHECK (event IN ({_quoted(EVENTS)})),
            user_id uuid,
            credential_id uuid,
            challenge_id uuid,
            grant_id uuid,
            db_role text NOT NULL DEFAULT current_user,
            operator text,
            detail jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK (jsonb_typeof(detail) = 'object')
        )
    """)
    op.execute("CREATE INDEX stepup_events_user_idx ON public.stepup_events (user_id, at)")

    op.execute("""
        CREATE TABLE public.stepup_rate_limits (
            key text NOT NULL CHECK (length(key) <= 200),
            bucket text NOT NULL CHECK (length(bucket) <= 64),
            window_start timestamptz NOT NULL,
            count integer NOT NULL CHECK (count >= 0),
            PRIMARY KEY (key, bucket)
        )
    """)

    op.execute("""
        CREATE TABLE public.stepup_enforcement (
            id boolean PRIMARY KEY DEFAULT true CHECK (id),
            state text NOT NULL CHECK (state IN ('draft', 'active')),
            changed_at timestamptz NOT NULL DEFAULT now(),
            changed_by text NOT NULL
        )
    """)
    op.execute("INSERT INTO public.stepup_enforcement (state, changed_by) VALUES ('draft', 'migration 0025')")

    # ── guards (they hold for every role; "operator" means hyperchat_owner, i.e. the migrator on the server) ──
    op.execute(f"""
        CREATE FUNCTION public.stepup_guard_credentials() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN  -- only by the owner: a cascade from users, or maintenance
                IF current_user <> 'hyperchat_owner' THEN
                    RAISE EXCEPTION 'passkey credentials are never deleted, only revoked' {REFUSE};
                END IF;
                RETURN OLD;
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'pending' OR NEW.sign_count < 0 OR NEW.last_used_at IS NOT NULL THEN
                    RAISE EXCEPTION 'a passkey credential is registered pending' {REFUSE};
                END IF;
                IF NOT public.stepup_admin_ok(NEW.user_id) THEN
                    RAISE EXCEPTION 'passkeys are registered for active administrators only' {REFUSE};
                END IF;
                IF (SELECT count(*) FROM public.webauthn_credentials c
                    WHERE c.user_id = NEW.user_id AND c.status = 'pending') >= 5 THEN
                    RAISE EXCEPTION 'too many pending passkeys: have the operator approve or revoke them' {REFUSE};
                END IF;
                NEW.created_at := now();
                RETURN NEW;
            END IF;
            IF (NEW.id, NEW.user_id, NEW.credential_id, NEW.public_key, NEW.fingerprint, NEW.created_at, NEW.aaguid)
               IS DISTINCT FROM (OLD.id, OLD.user_id, OLD.credential_id, OLD.public_key, OLD.fingerprint,
                                 OLD.created_at, OLD.aaguid) THEN
                RAISE EXCEPTION 'a passkey credential''s identity never changes' {REFUSE};
            END IF;
            IF NEW.sign_count < OLD.sign_count THEN
                RAISE EXCEPTION 'a passkey sign count never decreases' {REFUSE};
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status
               OR (NEW.approved_at, NEW.approved_by, NEW.revoked_at, NEW.revoked_by, NEW.revoke_reason)
                  IS DISTINCT FROM (OLD.approved_at, OLD.approved_by, OLD.revoked_at, OLD.revoked_by, OLD.revoke_reason)
            THEN
                IF current_user <> 'hyperchat_owner' THEN
                    RAISE EXCEPTION 'only the operator approves or revokes a passkey' {REFUSE};
                END IF;
                IF NOT ((OLD.status = 'pending' AND NEW.status IN ('active', 'revoked'))
                        OR (OLD.status = 'active' AND NEW.status = 'revoked')) THEN
                    RAISE EXCEPTION 'passkey status % cannot become %', OLD.status, NEW.status {REFUSE};
                END IF;
                IF NEW.status = 'active' AND NOT public.stepup_admin_ok(NEW.user_id) THEN
                    RAISE EXCEPTION 'only an active administrator''s passkey can be approved' {REFUSE};
                END IF;
            ELSIF OLD.status <> 'active' THEN  -- sign count / last use: only on an active credential
                RAISE EXCEPTION 'a % passkey cannot be used', OLD.status {REFUSE};
            END IF;
            RETURN NEW;
        END $$
    """)

    op.execute(f"""
        CREATE FUNCTION public.stepup_guard_challenges() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        DECLARE c jsonb;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                NEW.created_at := now();
                IF NEW.used_at IS NOT NULL THEN
                    RAISE EXCEPTION 'a challenge is issued unused' {REFUSE};
                END IF;
                IF NEW.challenge <> public.digest(convert_to(E'hyperchat-stepup-v1\\n' || NEW.canonical, 'UTF8'),
                                                 'sha256') THEN
                    RAISE EXCEPTION 'a challenge is the hash of its canonical action' {REFUSE};
                END IF;
                c := NEW.canonical::jsonb;
                IF c->>'v' IS DISTINCT FROM '1' OR c->>'purpose' IS DISTINCT FROM NEW.purpose
                   OR c->>'admin' IS DISTINCT FROM NEW.user_id::text
                   OR c->>'session' IS DISTINCT FROM encode(public.digest(NEW.session_id::text, 'sha256'), 'hex')
                   OR c->>'action' IS DISTINCT FROM NEW.action OR c->>'target' IS DISTINCT FROM NEW.target
                   OR c->>'params_hash' IS DISTINCT FROM NEW.params_hash
                   OR (c->>'expires_at')::bigint IS DISTINCT FROM floor(extract(epoch FROM NEW.expires_at))::bigint
                   OR NEW.expires_at > now() + interval '5 minutes' THEN
                    RAISE EXCEPTION 'the canonical action does not describe this challenge' {REFUSE};
                END IF;
                IF NOT public.stepup_admin_ok(NEW.user_id) OR NOT EXISTS (
                        SELECT 1 FROM public.sessions s WHERE s.id = NEW.session_id AND s.user_id = NEW.user_id
                        AND s.revoked_at IS NULL AND s.idle_expires_at > now() AND s.absolute_expires_at > now()) THEN
                    RAISE EXCEPTION 'challenges are issued for an active administrator''s live session' {REFUSE};
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                IF current_user <> 'hyperchat_owner' THEN
                    RAISE EXCEPTION 'challenges are deleted only by the owner' {REFUSE};
                END IF;
                RETURN OLD;
            END IF;
            IF (to_jsonb(NEW) - 'used_at' - 'outcome') <> (to_jsonb(OLD) - 'used_at' - 'outcome')
               OR OLD.used_at IS NOT NULL OR NEW.outcome IS NULL THEN
                RAISE EXCEPTION 'a challenge is only ever marked used, once' {REFUSE};
            END IF;
            NEW.used_at := now();  -- the transaction's time: a grant or assurance must be written in this transaction
            RETURN NEW;
        END $$
    """)

    op.execute(f"""
        CREATE FUNCTION public.stepup_guard_grants() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                NEW.created_at := now();
                IF NEW.consumed_at IS NOT NULL OR NEW.expires_at > now() + interval '2 minutes' THEN
                    RAISE EXCEPTION 'a grant is issued unconsumed and short-lived' {REFUSE};
                END IF;
                -- Only for a challenge verified just now, in this transaction, with the same bindings.
                IF NOT EXISTS (
                        SELECT 1 FROM public.stepup_challenges ch
                        WHERE ch.id = NEW.challenge_id AND ch.purpose = 'action' AND ch.outcome = 'ok'
                          AND ch.used_at = now() AND ch.user_id = NEW.user_id AND ch.session_id = NEW.session_id
                          AND ch.action = NEW.action AND ch.target = NEW.target AND ch.params_hash = NEW.params_hash)
                   OR NOT EXISTS (
                        SELECT 1 FROM public.webauthn_credentials c
                        WHERE c.id = NEW.credential_id AND c.user_id = NEW.user_id AND c.status = 'active') THEN
                    RAISE EXCEPTION 'a grant needs a challenge verified in this transaction and an active passkey'
                        {REFUSE};
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                IF current_user <> 'hyperchat_owner' THEN
                    RAISE EXCEPTION 'grants are deleted only by the owner' {REFUSE};
                END IF;
                RETURN OLD;
            END IF;
            -- Two transitions, both only through the owner's functions: consumed once (stepup_consume), then claimed
            -- once, in the transaction that consumed it (stepup_claim). Nothing else about a grant ever changes.
            IF current_user <> 'hyperchat_owner' THEN
                RAISE EXCEPTION 'grants change only through stepup_consume and stepup_claim' {REFUSE};
            END IF;
            IF (to_jsonb(NEW) - 'consumed_at' - 'consumed_txid' - 'claimed_at' - 'claimed_anchor')
                   <> (to_jsonb(OLD) - 'consumed_at' - 'consumed_txid' - 'claimed_at' - 'claimed_anchor')
               OR NOT ((OLD.consumed_at IS NULL AND NEW.consumed_at IS NOT NULL AND NEW.claimed_at IS NULL)
                       OR (OLD.consumed_at IS NOT NULL AND OLD.claimed_at IS NULL AND NEW.claimed_at IS NOT NULL
                           AND (NEW.consumed_at, NEW.consumed_txid) = (OLD.consumed_at, OLD.consumed_txid)
                           AND OLD.consumed_txid = pg_current_xact_id_if_assigned())) THEN
                RAISE EXCEPTION 'a grant is consumed once, then claimed once in the same transaction' {REFUSE};
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.stepup_guard_effects() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND current_user = 'hyperchat_owner' THEN
                RETURN OLD;  -- a cascade from its grant
            END IF;
            IF TG_OP <> 'INSERT' OR current_user <> 'hyperchat_owner' OR NOT EXISTS (
                    SELECT 1 FROM public.stepup_grants g WHERE g.id = NEW.grant_id AND g.claimed_at IS NOT NULL
                      AND g.consumed_txid = pg_current_xact_id_if_assigned()) THEN
                RAISE EXCEPTION 'grant effects are recorded only via stepup_effect, for a grant claimed now' {REFUSE};
            END IF;
            NEW.at := now();
            RETURN NEW;
        END $$
    """)

    op.execute(f"""
        CREATE FUNCTION public.stepup_guard_assurance() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF current_user <> 'hyperchat_owner' THEN
                    RAISE EXCEPTION 'session assurance is deleted only by the owner' {REFUSE};
                END IF;
                RETURN OLD;
            END IF;
            NEW.verified_at := now();
            IF NOT EXISTS (
                    SELECT 1 FROM public.stepup_challenges ch
                    WHERE ch.id = NEW.challenge_id AND ch.purpose = 'session' AND ch.outcome = 'ok'
                      AND ch.used_at = now() AND ch.user_id = NEW.user_id AND ch.session_id = NEW.session_id)
               OR NOT EXISTS (
                    SELECT 1 FROM public.webauthn_credentials c
                    WHERE c.id = NEW.credential_id AND c.user_id = NEW.user_id AND c.status = 'active')
               OR NOT EXISTS (
                    SELECT 1 FROM public.sessions s
                    WHERE s.id = NEW.session_id AND s.user_id = NEW.user_id AND s.revoked_at IS NULL
                      AND NEW.expires_at <= s.absolute_expires_at)
               OR (TG_OP = 'UPDATE' AND (NEW.session_id, NEW.user_id) IS DISTINCT FROM (OLD.session_id, OLD.user_id))
            THEN
                RAISE EXCEPTION 'session assurance needs a session ceremony verified in this transaction' {REFUSE};
            END IF;
            RETURN NEW;
        END $$
    """)

    op.execute(f"""
        CREATE FUNCTION public.stepup_guard_events() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                NEW.at := now();
                NEW.db_role := current_user;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'the step-up audit history is append-only' {REFUSE};
        END $$
    """)

    op.execute(f"""
        CREATE FUNCTION public.stepup_guard_enforcement() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        DECLARE missing integer;
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
            NEW.changed_at := now();
            INSERT INTO public.stepup_events (event, operator, detail)
            VALUES ('enforcement_changed', NEW.changed_by, jsonb_build_object('from', OLD.state, 'to', NEW.state));
            RETURN NEW;
        END $$
    """)

    op.execute("""
        CREATE FUNCTION public.stepup_enforcement_readiness()
        RETURNS TABLE (user_id uuid, email text, active_credentials bigint, pending_credentials bigint)
        LANGUAGE sql STABLE SET search_path = pg_catalog, public AS $$
            SELECT u.id, u.email::text,
                   count(c.id) FILTER (WHERE c.status = 'active'), count(c.id) FILTER (WHERE c.status = 'pending')
            FROM public.users u LEFT JOIN public.webauthn_credentials c ON c.user_id = u.id
            WHERE u.role = 'admin' AND u.is_active
            GROUP BY u.id, u.email ORDER BY u.email
        $$
    """)

    for table, fn, events in (("webauthn_credentials", "stepup_guard_credentials", "INSERT OR UPDATE OR DELETE"),
                              ("stepup_challenges", "stepup_guard_challenges", "INSERT OR UPDATE OR DELETE"),
                              ("stepup_grants", "stepup_guard_grants", "INSERT OR UPDATE OR DELETE"),
                              ("stepup_grant_effects", "stepup_guard_effects", "INSERT OR UPDATE OR DELETE"),
                              ("session_assurance", "stepup_guard_assurance", "INSERT OR UPDATE OR DELETE"),
                              ("stepup_events", "stepup_guard_events", "INSERT OR UPDATE OR DELETE"),
                              ("stepup_enforcement", "stepup_guard_enforcement", "INSERT OR UPDATE OR DELETE")):
        op.execute(f"CREATE TRIGGER aaa_{fn} BEFORE {events} ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.{fn}()")
    op.execute(f"""
        CREATE FUNCTION public.stepup_refuse_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'step-up tables are never truncated' {REFUSE};
        END $$
    """)
    for table in TABLES:
        op.execute(f"CREATE TRIGGER aaa_stepup_refuse_truncate BEFORE TRUNCATE ON public.{table} "
                   "FOR EACH STATEMENT EXECUTE FUNCTION public.stepup_refuse_truncate()")

    # ── the API's interface ────────────────────────────────────────────────────────────────────────────────
    # Consume one grant for exactly this administrator, session, action, target and parameter hash, in the caller's
    # transaction. 'ok' consumes it (once: the row is locked and re-checked); anything else changes nothing. The
    # grant's administrator, session and passkey must still be valid at this moment.
    op.execute(f"""
        CREATE FUNCTION public.stepup_consume(p_grant uuid, p_user uuid, p_session uuid, p_action text,
                                              p_target text, p_params_hash text)
        RETURNS text LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE g public.stepup_grants;
        BEGIN
            SELECT * INTO g FROM public.stepup_grants WHERE id = p_grant FOR UPDATE;
            IF NOT FOUND THEN RETURN 'not_found'; END IF;
            IF (g.user_id, g.session_id, g.action, g.target, g.params_hash)
               IS DISTINCT FROM (p_user, p_session, p_action, p_target, p_params_hash) THEN
                RETURN 'mismatch';
            END IF;
            IF g.consumed_at IS NOT NULL THEN RETURN 'consumed'; END IF;
            IF g.expires_at <= now() THEN RETURN 'expired'; END IF;
            IF NOT public.stepup_admin_ok(g.user_id) THEN RETURN 'not_admin'; END IF;
            IF NOT EXISTS (SELECT 1 FROM public.webauthn_credentials c WHERE c.id = g.credential_id
                           AND c.status = 'active') THEN
                RETURN 'credential_revoked';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM public.sessions s WHERE s.id = g.session_id AND s.user_id = g.user_id
                           AND s.revoked_at IS NULL AND s.idle_expires_at > now() AND s.absolute_expires_at > now()) THEN
                RETURN 'session_invalid';
            END IF;
            UPDATE public.stepup_grants SET consumed_at = now(), consumed_txid = pg_current_xact_id() WHERE id = g.id;
            INSERT INTO public.stepup_events (event, user_id, credential_id, challenge_id, grant_id, detail)
            VALUES ('grant_consumed', g.user_id, g.credential_id, g.challenge_id, g.id,
                    jsonb_build_object('action', g.action, 'target', g.target, 'by', session_user));
            RETURN 'ok';
        END $$
    """)
    # ── for the database guards of S3 (owner-only; S3's SECURITY DEFINER guards call them) ─────────────────────
    # Policy (docs/admin-stepup.md, "One grant, one action"): one grant authorizes exactly ONE logical action.
    #   * The action's ANCHOR write (the one write that IS the action, e.g. the creator's status change) calls
    #     stepup_claim(). It takes one grant consumed in THIS transaction with exactly these bindings, the session
    #     included, that no write has claimed yet, and claims it. A second anchor write (the same action again, or a
    #     second independent sensitive action) finds no unclaimed grant and is refused: it needs its own grant.
    #   * Legitimate secondary effects of that same action (an event row, a revoked grace grant) call stepup_effect(),
    #     which needs a grant CLAIMED in this transaction with the same bindings and records each named effect at
    #     most once per grant (stepup_grant_effects' primary key). An effect never authorizes anything on its own.
    #   * Anything that is itself a sensitive action is an anchor, never an effect.
    op.execute(f"""
        CREATE FUNCTION public.stepup_claim(p_user uuid, p_session uuid, p_action text, p_target text,
                                            p_params_hash text, p_anchor text)
        RETURNS uuid LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE g uuid;
        BEGIN
            IF btrim(coalesce(p_anchor, '')) = '' THEN
                RAISE EXCEPTION 'a claim names its anchor write' {REFUSE};
            END IF;
            SELECT id INTO g FROM public.stepup_grants
            WHERE consumed_txid = pg_current_xact_id_if_assigned() AND claimed_at IS NULL
              AND user_id = p_user AND session_id = p_session AND action = p_action AND target = p_target
              AND params_hash = p_params_hash
            ORDER BY consumed_at, id LIMIT 1 FOR UPDATE;
            IF g IS NULL THEN
                RAISE EXCEPTION 'this write needs its own step-up grant, consumed in this transaction for exactly this administrator, session, action, target and parameters' {REFUSE};
            END IF;
            UPDATE public.stepup_grants SET claimed_at = now(), claimed_anchor = p_anchor WHERE id = g;
            INSERT INTO public.stepup_events (event, user_id, grant_id, detail)
            VALUES ('grant_claimed', p_user, g, jsonb_build_object('action', p_action, 'target', p_target,
                                                                   'anchor', p_anchor));
            RETURN g;
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.stepup_effect(p_user uuid, p_session uuid, p_action text, p_target text,
                                             p_params_hash text, p_effect text)
        RETURNS uuid LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE g uuid;
        BEGIN
            SELECT id INTO g FROM public.stepup_grants gr
            WHERE consumed_txid = pg_current_xact_id_if_assigned() AND claimed_at IS NOT NULL
              AND user_id = p_user AND session_id = p_session AND action = p_action AND target = p_target
              AND params_hash = p_params_hash
              AND NOT EXISTS (SELECT 1 FROM public.stepup_grant_effects e WHERE e.grant_id = gr.id AND e.effect = p_effect)
            ORDER BY claimed_at, id LIMIT 1 FOR UPDATE;
            IF g IS NULL THEN
                RAISE EXCEPTION 'effect % needs a step-up grant claimed in this transaction for the same action, and happens once per grant', p_effect {REFUSE};
            END IF;
            INSERT INTO public.stepup_grant_effects (grant_id, effect) VALUES (g, p_effect);
            RETURN g;
        END $$
    """)
    # The session's assurance level: 2 only while the ceremony, its passkey, the administrator and the session hold.
    op.execute(f"""
        CREATE FUNCTION public.stepup_session_aal(p_session uuid) RETURNS smallint LANGUAGE sql STABLE {DEFINER} AS $$
            SELECT CASE WHEN EXISTS (
                SELECT 1 FROM public.session_assurance a
                JOIN public.sessions s ON s.id = a.session_id AND s.user_id = a.user_id
                JOIN public.webauthn_credentials c ON c.id = a.credential_id AND c.user_id = a.user_id
                JOIN public.users u ON u.id = a.user_id
                WHERE a.session_id = p_session AND a.expires_at > now() AND c.status = 'active'
                  AND u.role = 'admin' AND u.is_active AND s.revoked_at IS NULL
                  AND s.idle_expires_at > now() AND s.absolute_expires_at > now()) THEN 2 ELSE 1 END::smallint
        $$
    """)

    # ── operator functions (hyperchat_owner only: the migrator on the server, app.stepup.operator) ─────────
    op.execute(f"""
        CREATE FUNCTION public.stepup_approve_credential(p_email text, p_fingerprint text, p_operator text,
                                                         p_verified_by text)
        RETURNS text LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE c public.webauthn_credentials;
        BEGIN
            IF current_user <> 'hyperchat_owner' THEN
                RAISE EXCEPTION 'operator only' {REFUSE};
            END IF;
            IF btrim(coalesce(p_operator, '')) = '' OR btrim(coalesce(p_verified_by, '')) = '' THEN
                RETURN 'operator_and_verification_required';
            END IF;
            SELECT c2.* INTO c FROM public.webauthn_credentials c2 JOIN public.users u ON u.id = c2.user_id
            WHERE c2.fingerprint = upper(btrim(p_fingerprint)) AND u.email = p_email::citext FOR UPDATE OF c2;
            IF NOT FOUND THEN RETURN 'not_found'; END IF;  -- the fingerprint must belong to THIS administrator
            IF c.status <> 'pending' THEN RETURN 'not_pending'; END IF;
            IF c.created_at < now() - interval '72 hours' THEN RETURN 'stale'; END IF;
            IF NOT public.stepup_admin_ok(c.user_id) THEN RETURN 'not_admin'; END IF;
            UPDATE public.webauthn_credentials SET status = 'active', approved_at = now(), approved_by = p_operator
            WHERE id = c.id;
            INSERT INTO public.stepup_events (event, user_id, credential_id, operator, detail)
            VALUES ('credential_approved', c.user_id, c.id, p_operator,
                    jsonb_build_object('fingerprint', c.fingerprint, 'verified_by', p_verified_by));
            RETURN 'ok';
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.stepup_revoke_credential(p_email text, p_fingerprint text, p_operator text,
                                                        p_reason text)
        RETURNS text LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE c public.webauthn_credentials;
        BEGIN
            IF current_user <> 'hyperchat_owner' THEN
                RAISE EXCEPTION 'operator only' {REFUSE};
            END IF;
            IF btrim(coalesce(p_operator, '')) = '' OR btrim(coalesce(p_reason, '')) = '' THEN
                RETURN 'operator_and_reason_required';
            END IF;
            SELECT c2.* INTO c FROM public.webauthn_credentials c2 JOIN public.users u ON u.id = c2.user_id
            WHERE c2.fingerprint = upper(btrim(p_fingerprint)) AND u.email = p_email::citext FOR UPDATE OF c2;
            IF NOT FOUND THEN RETURN 'not_found'; END IF;
            IF c.status = 'revoked' THEN RETURN 'already_revoked'; END IF;
            UPDATE public.webauthn_credentials SET status = 'revoked', revoked_at = now(), revoked_by = p_operator,
                   revoke_reason = p_reason WHERE id = c.id;
            INSERT INTO public.stepup_events (event, user_id, credential_id, operator, detail)
            VALUES ('credential_revoked', c.user_id, c.id, p_operator,
                    jsonb_build_object('fingerprint', c.fingerprint, 'reason', p_reason, 'was', c.status));
            RETURN 'ok';
        END $$
    """)
    # Lost or compromised authenticators: revoke every passkey of the administrator and end every session, so
    # nothing proven with an old authenticator survives. The administrator then registers new passkeys, which the
    # operator approves after verifying the person out of band. Password reset or Google never restore MFA.
    op.execute(f"""
        CREATE FUNCTION public.stepup_recover_admin(p_email text, p_operator text, p_reason text)
        RETURNS integer LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE uid uuid; revoked integer; sessions integer;
        BEGIN
            IF current_user <> 'hyperchat_owner' THEN
                RAISE EXCEPTION 'operator only' {REFUSE};
            END IF;
            IF btrim(coalesce(p_operator, '')) = '' OR btrim(coalesce(p_reason, '')) = '' THEN
                RAISE EXCEPTION 'operator and reason are required';
            END IF;
            SELECT id INTO uid FROM public.users WHERE email = p_email::citext AND role = 'admin' FOR UPDATE;
            IF uid IS NULL THEN RAISE EXCEPTION 'no administrator with this email'; END IF;
            UPDATE public.webauthn_credentials SET status = 'revoked', revoked_at = now(), revoked_by = p_operator,
                   revoke_reason = 'recovery: ' || p_reason
            WHERE user_id = uid AND status <> 'revoked';
            GET DIAGNOSTICS revoked = ROW_COUNT;
            UPDATE public.sessions SET revoked_at = now() WHERE user_id = uid AND revoked_at IS NULL;
            GET DIAGNOSTICS sessions = ROW_COUNT;
            INSERT INTO public.stepup_events (event, user_id, operator, detail)
            VALUES ('admin_recovered', uid, p_operator,
                    jsonb_build_object('reason', p_reason, 'credentials_revoked', revoked, 'sessions_revoked', sessions));
            RETURN revoked;
        END $$
    """)

    # ── privileges ─────────────────────────────────────────────────────────────────────────────────────────
    everyone = ", ".join(API_ROLES + ("hyperchat_readonly", STEPUP, "PUBLIC"))
    for table in TABLES:  # undo migration 0007's default privileges first
        op.execute(f"REVOKE ALL ON public.{table} FROM {everyone}")
    for fn in OPERATOR_FUNCTIONS + INTERNAL_FUNCTIONS + API_FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM {everyone}")
    for fn in TRIGGER_FUNCTIONS + ("stepup_refuse_truncate",):
        op.execute(f"REVOKE ALL ON FUNCTION public.{fn}() FROM {everyone}")

    op.execute(f"GRANT USAGE ON SCHEMA public TO {STEPUP}")
    op.execute(f"GRANT SELECT (id, email, role, is_active) ON public.users TO {STEPUP}")
    op.execute(f"GRANT SELECT (id, user_id, revoked_at, idle_expires_at, absolute_expires_at) ON public.sessions "
               f"TO {STEPUP}")
    op.execute(f"GRANT SELECT ON public.webauthn_credentials TO {STEPUP}")
    op.execute(f"GRANT INSERT (user_id, credential_id, public_key, fingerprint, sign_count, aaguid, device_type, "
               f"backed_up) ON public.webauthn_credentials TO {STEPUP}")
    op.execute(f"GRANT UPDATE (sign_count, last_used_at) ON public.webauthn_credentials TO {STEPUP}")
    op.execute(f"GRANT SELECT, INSERT ON public.stepup_challenges TO {STEPUP}")
    op.execute(f"GRANT UPDATE (used_at, outcome) ON public.stepup_challenges TO {STEPUP}")
    op.execute(f"GRANT SELECT, INSERT ON public.stepup_grants TO {STEPUP}")
    op.execute(f"GRANT SELECT, INSERT ON public.session_assurance TO {STEPUP}")
    op.execute(f"GRANT UPDATE (credential_id, challenge_id, aal, verified_at, expires_at) ON public.session_assurance "
               f"TO {STEPUP}")
    op.execute(f"GRANT SELECT, INSERT ON public.stepup_events TO {STEPUP}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.stepup_rate_limits TO {STEPUP}")
    op.execute(f"GRANT SELECT ON public.stepup_enforcement TO {STEPUP}, {', '.join(API_ROLES)}")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.stepup_admin_ok(uuid) TO {STEPUP}")  # used by its guards
    op.execute(f"GRANT EXECUTE ON FUNCTION public.digest(text, text), public.digest(bytea, text) TO {STEPUP}")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.stepup_session_aal(uuid) TO {STEPUP}, hyperchat_app, "
               "hyperchat_auth, hyperchat_lifecycle")
    op.execute("GRANT EXECUTE ON FUNCTION public.stepup_consume(uuid, uuid, uuid, text, text, text) TO hyperchat_app, "
               "hyperchat_auth, hyperchat_lifecycle")


def downgrade() -> None:
    for fn in API_FUNCTIONS + OPERATOR_FUNCTIONS + GUARD_HELPERS:
        op.execute(f"DROP FUNCTION {fn}")
    for table in ("session_assurance", "stepup_grant_effects", "stepup_grants", "stepup_challenges", "webauthn_credentials", "stepup_events",
                  "stepup_rate_limits", "stepup_enforcement"):
        op.execute(f"DROP TABLE public.{table}")
    for fn in TRIGGER_FUNCTIONS + ("stepup_refuse_truncate",):
        op.execute(f"DROP FUNCTION public.{fn}()")
    op.execute("DROP FUNCTION public.stepup_admin_ok(uuid)")
    op.execute("REVOKE EXECUTE ON FUNCTION public.digest(text, text), public.digest(bytea, text) FROM hyperchat_stepup")
    op.execute("REVOKE ALL ON public.users, public.sessions FROM hyperchat_stepup")
    op.execute("REVOKE USAGE ON SCHEMA public FROM hyperchat_stepup")
