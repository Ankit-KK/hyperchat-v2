"""Multiple Didit accounts for KYC (owner requirement 2026-11-27): automatic rotation for NEW users, every user pinned
to the account of their first session, approvals from any configured, not-revoked account keep counting.

Revision ID: 0040
Revises: 0039

Model (docs/kyc.md, "Didit accounts"):
  * kyc_provider_config (0022's one row) stays THE database's environment and becomes the account 'primary', unchanged
    for every existing reader (0023's rules read its environment; etl/kyc_provider_config.py still writes it). It
    gains enabled, monthly_cap and revoked_at (+ reason).
  * kyc_provider_accounts: every further account, each with its own Didit application id (unique, required, and the
    primary's must be set first, so a webhook's signed application_id names exactly one account), workflow, webhook
    secret (verified inside PostgreSQL; no runtime role can read the table), enabled flag, optional monthly cap, a
    fixed rotation order and its own workflow epoch (0022's epoch trigger). The environment is the primary's.
  * kyc_sessions.account_id (existing sessions: 'primary') and kyc_webhook_events.account_id: which account created
    a session, and which account's secret verified a receipt.
  * kyc_account_pins: the user's account, set at the first session (or derived from the user's earliest session, so
    existing users stay where their history is) and moved only by the operator (kyc_account_move_user, audited).
  * kyc_account_reservations: an assignment whose Didit call is in flight (10 minutes), counted against the cap.
  * kyc_account_events: the audit of account changes (never a secret), pin moves and revocations.

Functions:
  * kyc_assign_account(user) -> (result, account, workflow), for hyperchat_app: under one rotation lock, the user's
    pinned account, or for a new user the least-used enabled, not-revoked account this UTC month that is under its
    cap (sessions created this month plus reservations in flight), ties by order then id; the choice is pinned and
    reserved. A pinned account that is revoked, disabled or at its cap is refused ('account_revoked',
    'account_disabled', 'account_at_cap'): never re-routed, so Didit's per-account duplicate checks keep seeing the
    user. No usable account: 'no_account_available'.
  * kyc_create_session(user, session, workflow, version, environment, account) stores a session only for the account
    the user is pinned to, with that account's workflow; the 0022 five-argument form remains as the 'primary' call
    (the previous release keeps working). Early webhooks apply only when the receipt's account is the session's.
  * kyc_receive_webhook routes by the signed body's application_id (an extra account's, else the primary), verifies
    with that account's secret, and never applies a receipt to a session of another account.
  * kyc_effective: the CURRENT session is the user's newest session of a not-revoked account in that account's
    current workflow epoch; approved while it is Approved and later than every Declined / Kyc Expired of ANY of the
    user's sessions in this environment (all accounts, revoked ones too). Disabling an account stops new sessions
    only; revoking one (kyc_account_revoke, final, audited) retires its approvals.
  * kyc_account_move_user, kyc_account_revoke: owner (operator) only, audited.
No 0023 function changes: they read the primary's environment, user_kyc_approved and kyc_owner_decided, which
already looks at every session of the environment.

Downgrade refuses (changing nothing) while anything exists that 0022's single account cannot hold: an extra account,
a session, receipt or pin of another account, an account event, or primary flags off their defaults. Otherwise it
restores 0022's three functions from 0022's own text and drops what 0040 added (pins to 'primary' are derivable).
"""

import importlib.util
import pathlib
import types

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

ACCOUNT_ID = "^[a-z][a-z0-9-]{1,31}$"
RESERVATION = "interval '10 minutes'"
EVERYONE = ("hyperchat_app, hyperchat_worker, hyperchat_auth, hyperchat_lifecycle, hyperchat_readonly, "
            "hyperchat_stepup, PUBLIC")
NEW_TABLES = ("kyc_provider_accounts", "kyc_account_pins", "kyc_account_reservations", "kyc_account_events")
APP_FUNCTIONS = ("public.kyc_assign_account(uuid)",
                 "public.kyc_create_session(uuid, uuid, uuid, integer, text, text)")
OPERATOR_FUNCTIONS = ("public.kyc_account_move_user(uuid, text, text, text)",
                      "public.kyc_account_revoke(text, text, text)")
INTERNAL_FUNCTIONS = ("public.kyc_accounts()", "public.kyc_account_usage(text)", "public.kyc_user_pin(uuid, text)",
                      "public.kyc_provider_accounts_guard()", "public.kyc_provider_config_guard()",
                      "public.kyc_account_audit()", "public.kyc_account_pins_guard()")
REPLACED_0022 = ("kyc_create_session", "kyc_receive_webhook", "kyc_effective")


def _m0022() -> tuple[types.ModuleType, list[str]]:
    path = pathlib.Path(__file__).with_name("0022_kyc_core.py")
    spec = importlib.util.spec_from_file_location("_hyperchat_0022_for_0040", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []
    module.op = types.SimpleNamespace(execute=statements.append)
    module.upgrade()
    return module, statements


def _function_0022(statements: list[str], name: str) -> str:
    (sql,) = [s for s in statements if f"CREATE FUNCTION public.{name}(" in s]
    return sql.replace(f"CREATE FUNCTION public.{name}(", f"CREATE OR REPLACE FUNCTION public.{name}(", 1)


def upgrade() -> None:
    m, _ = _m0022()
    sql = m._sql
    lock = m.LOCK

    # ── accounts ──────────────────────────────────────────────────────────────────────────────────────────────
    op.execute("ALTER TABLE public.kyc_provider_config ADD COLUMN enabled boolean NOT NULL DEFAULT true, "
               "ADD COLUMN monthly_cap integer CHECK (monthly_cap >= 0), ADD COLUMN revoked_at timestamptz, "
               "ADD COLUMN revoked_reason text CHECK (char_length(revoked_reason) <= 500)")
    op.execute(f"""
        CREATE TABLE public.kyc_provider_accounts (
            account_id text PRIMARY KEY CHECK (account_id ~ '{ACCOUNT_ID}' AND account_id <> 'primary'),
            environment text NOT NULL CHECK (environment IN ('sandbox', 'live')),
            workflow_id uuid NOT NULL,
            application_id uuid NOT NULL UNIQUE,
            webhook_secret text NOT NULL CHECK (char_length(webhook_secret) BETWEEN 16 AND 512),
            enabled boolean NOT NULL DEFAULT true,
            monthly_cap integer CHECK (monthly_cap >= 0),
            sort_order integer NOT NULL DEFAULT 100 CHECK (sort_order >= 1),  -- the primary is 0
            workflow_since timestamptz NOT NULL DEFAULT now(),  -- this account's epoch (0022's trigger keeps it)
            revoked_at timestamptz,
            revoked_reason text CHECK (char_length(revoked_reason) <= 500),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE TRIGGER kyc_provider_accounts_epoch BEFORE INSERT OR UPDATE ON public.kyc_provider_accounts "
               "FOR EACH ROW EXECUTE FUNCTION public.kyc_provider_config_epoch()")
    op.execute(f"""
        ALTER TABLE public.kyc_sessions ADD COLUMN account_id text NOT NULL DEFAULT 'primary'
            CHECK (account_id ~ '{ACCOUNT_ID}')
    """)
    op.execute("CREATE INDEX kyc_sessions_account_idx ON public.kyc_sessions (account_id, created_at)")
    op.execute(f"ALTER TABLE public.kyc_webhook_events ADD COLUMN account_id text CHECK (account_id ~ '{ACCOUNT_ID}')")
    op.execute(f"""
        CREATE TABLE public.kyc_account_pins (
            user_id uuid PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
            account_id text NOT NULL CHECK (account_id ~ '{ACCOUNT_ID}'),
            pinned_at timestamptz NOT NULL DEFAULT now(),
            moved_at timestamptz
        )
    """)
    op.execute(f"""
        CREATE TABLE public.kyc_account_reservations (
            user_id uuid PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
            account_id text NOT NULL CHECK (account_id ~ '{ACCOUNT_ID}'),
            reserved_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX kyc_account_reservations_account_idx ON public.kyc_account_reservations "
               "(account_id, reserved_at)")
    op.execute("""
        CREATE TABLE public.kyc_account_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            action text NOT NULL CHECK (action IN ('account_added', 'account_changed', 'account_revoked',
                                                   'pin_moved')),
            account_id text NOT NULL,
            user_id uuid,          -- pin_moved only (no foreign key: the audit outlives the account)
            from_account text,     -- pin_moved only
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,  -- changed column names, never a value of the secret
            operator text NOT NULL CHECK (char_length(operator) BETWEEN 1 AND 128),
            reason text CHECK (char_length(reason) <= 500),
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)

    # ── guards and audit ─────────────────────────────────────────────────────────────────────────────────────
    op.execute(sql("""
        CREATE FUNCTION public.kyc_provider_accounts_guard() RETURNS trigger LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE prim public.kyc_provider_config%ROWTYPE;
        BEGIN
            IF TG_OP = 'DELETE' THEN  -- history keeps naming the account: retire it with kyc_account_revoke
                IF EXISTS (SELECT 1 FROM public.kyc_sessions WHERE account_id = OLD.account_id)
                   OR EXISTS (SELECT 1 FROM public.kyc_account_pins WHERE account_id = OLD.account_id) THEN
                    RAISE EXCEPTION 'account % has sessions or pinned users: revoke it instead', OLD.account_id
                        USING ERRCODE = 'restrict_violation';
                END IF;
                RETURN OLD;
            END IF;
            SELECT * INTO prim FROM public.kyc_provider_config;
            IF NOT FOUND OR prim.application_id IS NULL THEN
                RAISE EXCEPTION 'configure the primary account with its application id first'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.environment <> prim.environment OR NEW.application_id = prim.application_id THEN
                RAISE EXCEPTION 'an account is another Didit application of this environment'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF NEW.account_id <> OLD.account_id OR (OLD.revoked_at IS NOT NULL
                       AND (NEW.revoked_at, NEW.revoked_reason) IS DISTINCT FROM (OLD.revoked_at, OLD.revoked_reason)) THEN
                    RAISE EXCEPTION 'an account''s id never changes and a revocation is final'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            NEW.updated_at := now();
            RETURN NEW;
        END $$
    """))
    op.execute(sql("""
        CREATE FUNCTION public.kyc_provider_config_guard() RETURNS trigger LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE extras boolean := EXISTS (SELECT 1 FROM public.kyc_provider_accounts);
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF extras THEN
                    RAISE EXCEPTION 'other accounts exist: the primary is not removed' USING ERRCODE = 'check_violation';
                END IF;
                RETURN OLD;
            END IF;
            IF OLD.revoked_at IS NOT NULL
               AND (NEW.revoked_at, NEW.revoked_reason) IS DISTINCT FROM (OLD.revoked_at, OLD.revoked_reason) THEN
                RAISE EXCEPTION 'a revocation is final' USING ERRCODE = 'check_violation';
            END IF;
            IF extras AND (NEW.application_id IS NULL OR NEW.environment <> OLD.environment
                   OR EXISTS (SELECT 1 FROM public.kyc_provider_accounts a WHERE a.application_id = NEW.application_id)) THEN
                RAISE EXCEPTION 'other accounts exist: the primary keeps its environment and a distinct application id'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
    """))
    op.execute(sql("""
        CREATE FUNCTION public.kyc_account_audit() RETURNS trigger LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE changed jsonb; acct text;
        BEGIN
            acct := coalesce(to_jsonb(NEW) ->> 'account_id', 'primary');  -- the config row is the primary
            SELECT coalesce(jsonb_agg(n.key ORDER BY n.key), '[]'::jsonb) INTO changed
            FROM jsonb_each(to_jsonb(NEW)) n
            WHERE n.key NOT IN ('updated_at', 'workflow_since', 'created_at')
              AND (TG_OP = 'INSERT' OR n.value IS DISTINCT FROM to_jsonb(OLD) -> n.key);
            IF TG_OP = 'UPDATE' AND changed = '[]'::jsonb THEN
                RETURN NULL;
            END IF;
            INSERT INTO public.kyc_account_events (action, account_id, detail, operator)
            VALUES (CASE WHEN TG_OP = 'INSERT' THEN 'account_added' ELSE 'account_changed' END, acct,
                    jsonb_build_object('changed', changed),
                    coalesce(nullif(current_setting('hyperchat.operator', true), ''), session_user));
            RETURN NULL;
        END $$
    """))
    op.execute(sql("""
        CREATE FUNCTION public.kyc_account_pins_guard() RETURNS trigger LANGUAGE plpgsql __DEFINER__ AS $$
        BEGIN
            IF NEW.account_id IS DISTINCT FROM OLD.account_id
               AND current_setting('hyperchat.kyc_pin_move', true) IS DISTINCT FROM 'on' THEN
                RAISE EXCEPTION 'a user''s Didit account moves only with kyc_account_move_user (audited)'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END $$
    """))
    op.execute("CREATE TRIGGER kyc_provider_accounts_guard BEFORE INSERT OR UPDATE OR DELETE "
               "ON public.kyc_provider_accounts FOR EACH ROW EXECUTE FUNCTION public.kyc_provider_accounts_guard()")
    op.execute("CREATE TRIGGER kyc_provider_accounts_audit AFTER INSERT OR UPDATE ON public.kyc_provider_accounts "
               "FOR EACH ROW EXECUTE FUNCTION public.kyc_account_audit()")
    op.execute("CREATE TRIGGER kyc_provider_config_guard BEFORE UPDATE OR DELETE ON public.kyc_provider_config "
               "FOR EACH ROW EXECUTE FUNCTION public.kyc_provider_config_guard()")
    op.execute("CREATE TRIGGER kyc_provider_config_audit AFTER UPDATE OF enabled, monthly_cap, revoked_at "
               "ON public.kyc_provider_config FOR EACH ROW EXECUTE FUNCTION public.kyc_account_audit()")
    op.execute("CREATE TRIGGER kyc_account_pins_guard BEFORE UPDATE ON public.kyc_account_pins "
               "FOR EACH ROW EXECUTE FUNCTION public.kyc_account_pins_guard()")

    # ── internal helpers ─────────────────────────────────────────────────────────────────────────────────────
    op.execute(sql("""
        CREATE FUNCTION public.kyc_accounts()
        RETURNS TABLE (account_id text, environment text, workflow_id uuid, application_id uuid,
                       workflow_since timestamptz, enabled boolean, monthly_cap integer, revoked_at timestamptz,
                       sort_order integer)
        LANGUAGE sql STABLE __DEFINER__ AS $$
            SELECT 'primary'::text, c.environment, c.workflow_id, c.application_id, c.workflow_since, c.enabled,
                   c.monthly_cap, c.revoked_at, 0 FROM public.kyc_provider_config c
            UNION ALL
            SELECT a.account_id, a.environment, a.workflow_id, a.application_id, a.workflow_since, a.enabled,
                   a.monthly_cap, a.revoked_at, a.sort_order FROM public.kyc_provider_accounts a
        $$
    """))
    op.execute(sql(f"""
        CREATE FUNCTION public.kyc_account_usage(p_account text) RETURNS bigint LANGUAGE sql STABLE __DEFINER__ AS $$
            SELECT (SELECT count(*) FROM public.kyc_sessions s WHERE s.account_id = p_account
                      AND s.created_at >= (date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'))
                 + (SELECT count(*) FROM public.kyc_account_reservations r WHERE r.account_id = p_account
                      AND r.reserved_at > now() - {RESERVATION})
        $$
    """))
    # The user's pin; without one, derived from the user's earliest session (existing users stay with their
    # history), else p_default; written when known.
    op.execute(sql("""
        CREATE FUNCTION public.kyc_user_pin(p_user_id uuid, p_default text) RETURNS text
        LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE a text;
        BEGIN
            SELECT account_id INTO a FROM public.kyc_account_pins WHERE user_id = p_user_id;
            IF FOUND THEN
                RETURN a;
            END IF;
            SELECT s.account_id INTO a FROM public.kyc_sessions s WHERE s.user_id = p_user_id
            ORDER BY s.created_at, s.id LIMIT 1;
            a := coalesce(a, p_default);
            IF a IS NOT NULL THEN
                INSERT INTO public.kyc_account_pins (user_id, account_id) VALUES (p_user_id, a)
                ON CONFLICT (user_id) DO NOTHING;
                SELECT account_id INTO a FROM public.kyc_account_pins WHERE user_id = p_user_id;
            END IF;
            RETURN a;
        END $$
    """))

    # ── entry points ─────────────────────────────────────────────────────────────────────────────────────────
    op.execute(sql(f"""
        CREATE FUNCTION public.kyc_assign_account(p_user_id uuid, OUT o_result text, OUT o_account text,
                                                  OUT o_workflow uuid)
        LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE env text; a record;
        BEGIN
            SELECT environment INTO env FROM public.kyc_provider_config;
            IF NOT FOUND THEN
                o_result := 'not_configured'; RETURN;
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended('kyc_account_rotation', 0));  -- choices and caps serialize
            o_account := public.kyc_user_pin(p_user_id, NULL);
            IF o_account IS NULL THEN  -- a new user: the least-used usable account this month
                SELECT c.* INTO a FROM public.kyc_accounts() c
                WHERE c.enabled AND c.revoked_at IS NULL AND c.environment = env
                  AND (c.monthly_cap IS NULL OR public.kyc_account_usage(c.account_id) < c.monthly_cap)
                ORDER BY public.kyc_account_usage(c.account_id), c.sort_order, c.account_id LIMIT 1;
                IF NOT FOUND THEN
                    o_result := 'no_account_available'; RETURN;
                END IF;
                o_account := public.kyc_user_pin(p_user_id, a.account_id);
            ELSE
                SELECT c.* INTO a FROM public.kyc_accounts() c WHERE c.account_id = o_account;
                IF NOT FOUND OR a.revoked_at IS NOT NULL THEN
                    o_result := 'account_revoked'; RETURN;
                ELSIF NOT a.enabled THEN
                    o_result := 'account_disabled'; RETURN;
                ELSIF a.monthly_cap IS NOT NULL AND public.kyc_account_usage(o_account)
                          - (SELECT count(*) FROM public.kyc_account_reservations r WHERE r.user_id = p_user_id
                               AND r.account_id = o_account AND r.reserved_at > now() - {RESERVATION})
                          >= a.monthly_cap THEN
                    o_result := 'account_at_cap'; RETURN;
                END IF;
            END IF;
            INSERT INTO public.kyc_account_reservations (user_id, account_id) VALUES (p_user_id, o_account)
            ON CONFLICT (user_id) DO UPDATE SET account_id = EXCLUDED.account_id, reserved_at = now();
            o_workflow := a.workflow_id;
            o_result := 'ok';
        END $$
    """))
    # 'ok' | 'not_configured' | 'wrong_configuration' | 'wrong_account' | 'owner_mismatch'
    op.execute(sql("""
        CREATE FUNCTION public.kyc_create_session(p_user_id uuid, p_session_id uuid, p_workflow_id uuid,
                                                  p_workflow_version integer, p_environment text, p_account text)
        RETURNS text LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE a record; s public.kyc_sessions%ROWTYPE; r record;
        BEGIN
            __READ_COMMITTED__
            IF NOT EXISTS (SELECT 1 FROM public.kyc_provider_config) THEN
                RETURN 'not_configured';
            END IF;
            SELECT c.* INTO a FROM public.kyc_accounts() c WHERE c.account_id = p_account;
            IF NOT FOUND OR p_environment IS DISTINCT FROM a.environment OR p_workflow_id IS DISTINCT FROM a.workflow_id
            THEN
                RETURN 'wrong_configuration';
            END IF;
            IF public.kyc_user_pin(p_user_id, p_account) IS DISTINCT FROM p_account THEN
                RETURN 'wrong_account';  -- a user's sessions all live on their pinned account
            END IF;
            """ + lock.format("p_session_id") + """
            INSERT INTO public.kyc_sessions (user_id, vendor_data, session_id, workflow_id, workflow_version,
                                             environment, account_id)
            VALUES (p_user_id, p_user_id::text, p_session_id, p_workflow_id, p_workflow_version, p_environment,
                    p_account)
            ON CONFLICT (session_id) DO NOTHING;
            IF FOUND THEN
                INSERT INTO public.kyc_events (user_id, session_id, action, to_status, source, actor_user_id)
                VALUES (p_user_id, p_session_id, 'session_created', 'Not Started', 'session_create', p_user_id);
            END IF;
            DELETE FROM public.kyc_account_reservations WHERE user_id = p_user_id;
            SELECT * INTO s FROM public.kyc_sessions WHERE session_id = p_session_id;
            IF s.user_id <> p_user_id OR s.account_id <> p_account THEN
                RETURN 'owner_mismatch';
            END IF;
            FOR r IN SELECT id, vendor_data, workflow_id, environment, provider_status, provider_updated_at,
                            check_statuses, coalesce(account_id, 'primary') AS account_id
                     FROM public.kyc_webhook_events WHERE session_id = p_session_id AND outcome = 'unmatched'
                     ORDER BY provider_updated_at, received_at FOR UPDATE LOOP
                IF public.kyc_uuid(r.vendor_data) IS DISTINCT FROM s.user_id OR r.workflow_id IS DISTINCT FROM s.workflow_id
                   OR r.environment IS DISTINCT FROM s.environment OR r.account_id IS DISTINCT FROM s.account_id THEN
                    UPDATE public.kyc_webhook_events SET outcome = 'identity_mismatch' WHERE id = r.id;
                ELSE
                    PERFORM public.kyc_record_provider_status(p_session_id, r.provider_status, r.provider_updated_at,
                                                              r.check_statuses, 'webhook', NULL);
                    UPDATE public.kyc_webhook_events SET outcome = 'matched_later' WHERE id = r.id;
                END IF;
            END LOOP;
            RETURN 'ok';
        END $$
    """))
    op.execute("""
        CREATE OR REPLACE FUNCTION public.kyc_create_session(p_user_id uuid, p_session_id uuid, p_workflow_id uuid,
                                                             p_workflow_version integer, p_environment text)
        RETURNS text LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
            -- 0022's form, kept for the previous release: the primary account
            SELECT public.kyc_create_session(p_user_id, p_session_id, p_workflow_id, p_workflow_version,
                                             p_environment, 'primary')
        $$
    """)
    op.execute(sql("""
        CREATE OR REPLACE FUNCTION public.kyc_receive_webhook(p_canonical text, p_signature text,
                                                              p_header_timestamp text)
        RETURNS text LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE
            prim public.kyc_provider_config%ROWTYPE; b jsonb; presented text := lower(btrim(coalesce(p_signature, '')));
            now_s numeric := extract(epoch FROM clock_timestamp());
            v_event text; v_type text; v_status text; v_session uuid; v_vendor uuid; v_workflow uuid; v_at timestamptz;
            v_decision jsonb; v_checks jsonb; v_outcome text := 'pending'; v_receipt uuid; v_result text;
            v_account text := 'primary'; v_secret text; v_account_workflow uuid; extra public.kyc_provider_accounts%ROWTYPE;
            s public.kyc_sessions%ROWTYPE;
        BEGIN
            __READ_COMMITTED__
            SELECT * INTO prim FROM public.kyc_provider_config;
            IF NOT FOUND THEN
                RETURN 'not_configured';
            END IF;
            v_secret := prim.webhook_secret; v_account_workflow := prim.workflow_id;
            -- The account is named by the body's application_id; the signature below then proves that account's
            -- secret signed exactly this body (an unparsable body is checked against the primary's secret).
            BEGIN
                b := p_canonical::jsonb;
            EXCEPTION WHEN invalid_text_representation OR untranslatable_character THEN
                b := NULL;
            END;
            IF jsonb_typeof(b) = 'object' THEN
                SELECT * INTO extra FROM public.kyc_provider_accounts
                WHERE application_id = public.kyc_uuid(b ->> 'application_id');
                IF FOUND THEN
                    v_account := extra.account_id; v_secret := extra.webhook_secret;
                    v_account_workflow := extra.workflow_id;
                END IF;
            END IF;
            IF p_canonical IS NULL OR presented !~ '^[0-9a-f]{64}$' OR sha256(convert_to(presented, 'UTF8')) <>
               sha256(convert_to(encode(public.kyc_hmac_sha256(convert_to(v_secret, 'UTF8'),
                                                               convert_to(p_canonical, 'UTF8')), 'hex'), 'UTF8')) THEN
                RETURN 'bad_signature';
            END IF;
            IF b IS NULL OR jsonb_typeof(b) <> 'object' THEN
                RETURN 'malformed';
            END IF;
            IF NOT (CASE WHEN jsonb_typeof(b -> 'timestamp') = 'number' AND (b ->> 'timestamp') ~ '^[0-9]{1,12}$'
                         THEN abs(now_s - (b ->> 'timestamp')::numeric) <= 300 ELSE false END)
               OR NOT (CASE WHEN coalesce(p_header_timestamp, '') ~ '^[0-9]{1,12}$'
                            THEN abs(now_s - p_header_timestamp::numeric) <= 300 ELSE false END) THEN
                RETURN 'stale_timestamp';
            END IF;
            v_event := CASE WHEN jsonb_typeof(b -> 'event_id') = 'string' THEN b ->> 'event_id' END;
            IF v_event IS NULL OR char_length(v_event) NOT BETWEEN 1 AND 128 THEN
                RETURN 'malformed';
            END IF;
            IF (b ->> 'environment') IS DISTINCT FROM prim.environment OR (v_account = 'primary'
                    AND prim.application_id IS NOT NULL
                    AND public.kyc_uuid(b ->> 'application_id') IS DISTINCT FROM prim.application_id) THEN
                RETURN 'wrong_application';
            END IF;

            v_type := left(coalesce(b ->> 'webhook_type', ''), 64);
            v_status := CASE WHEN jsonb_typeof(b -> 'status') = 'string' AND (b ->> 'status') IN (__STATUSES__)
                             THEN b ->> 'status' END;
            v_session := public.kyc_uuid(b ->> 'session_id');
            v_vendor := public.kyc_uuid(b ->> 'vendor_data');
            v_workflow := public.kyc_uuid(b ->> 'workflow_id');
            v_at := CASE WHEN jsonb_typeof(b -> 'created_at') = 'number' AND (b ->> 'created_at') ~ '^[1-9][0-9]{0,11}$'
                         THEN to_timestamp((b ->> 'created_at')::bigint) END;
            v_decision := CASE WHEN jsonb_typeof(b -> 'decision') = 'object' THEN b -> 'decision' END;
            IF v_type NOT IN ('status.updated', 'data.updated') THEN
                v_outcome := 'ignored';
            ELSIF v_status IS NULL THEN
                v_outcome := 'unknown_status';
            ELSIF v_session IS NULL OR v_at IS NULL THEN
                v_outcome := 'incomplete';
            ELSIF v_vendor IS NULL OR v_workflow IS DISTINCT FROM v_account_workflow THEN
                v_outcome := 'identity_mismatch';
            ELSIF v_status = 'Approved' AND NOT coalesce(public.kyc_uuid(v_decision ->> 'session_id') = v_session
                                                         AND (v_decision ->> 'status') = 'Approved', false) THEN
                v_outcome := 'incomplete';
            END IF;
            IF v_decision IS NOT NULL THEN
                v_checks := public.kyc_check_statuses(v_decision);
            END IF;

            INSERT INTO public.kyc_webhook_events (event_id, webhook_type, session_id, vendor_data, workflow_id,
                                                   provider_status, provider_updated_at, environment, check_statuses,
                                                   outcome, account_id)
            VALUES (v_event, v_type, v_session, v_vendor::text, v_workflow, v_status, v_at, prim.environment, v_checks,
                    v_outcome, v_account)
            ON CONFLICT (event_id) DO NOTHING RETURNING id INTO v_receipt;
            IF v_receipt IS NULL THEN
                RETURN 'duplicate';
            END IF;
            IF v_outcome <> 'pending' THEN
                RETURN v_outcome;
            END IF;
            """ + lock.format("v_session") + """
            SELECT * INTO s FROM public.kyc_sessions WHERE session_id = v_session;
            IF NOT FOUND THEN
                v_outcome := 'unmatched';
            ELSIF s.user_id <> v_vendor OR s.workflow_id <> v_workflow OR s.environment <> prim.environment
                  OR s.account_id <> v_account THEN  -- account A's signature never moves account B's session
                v_outcome := 'identity_mismatch';
            ELSE
                v_result := public.kyc_record_provider_status(v_session, v_status, v_at, v_checks, 'webhook', NULL);
                v_outcome := CASE WHEN v_result = 'stale' THEN 'stale' ELSE 'applied' END;
            END IF;
            UPDATE public.kyc_webhook_events SET outcome = v_outcome WHERE id = v_receipt;
            RETURN v_outcome;
        END $$
    """))
    op.execute("""
        CREATE OR REPLACE FUNCTION public.kyc_effective(p_user_id uuid, p_environment text)
        RETURNS TABLE (approved boolean, session_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
            WITH current_session AS (
                SELECT s.session_id, s.environment, s.provider_status, s.provider_updated_at
                FROM public.kyc_sessions s
                JOIN public.kyc_accounts() c ON c.account_id = s.account_id AND c.environment = s.environment
                                             AND c.workflow_id = s.workflow_id
                WHERE s.user_id = p_user_id AND s.environment = p_environment AND c.revoked_at IS NULL
                  AND (s.created_at >= c.workflow_since OR s.provider_updated_at >= c.workflow_since)
                ORDER BY s.created_at DESC, s.id DESC LIMIT 1)
            SELECT COALESCE(l.provider_status = 'Approved' AND l.provider_updated_at IS NOT NULL
                            AND NOT EXISTS (SELECT 1 FROM public.kyc_sessions o  -- every account, revoked ones too
                                            WHERE o.user_id = p_user_id AND o.environment = l.environment
                                              AND o.adverse_at >= l.provider_updated_at), false),
                   l.session_id
            FROM (SELECT 1) one LEFT JOIN current_session l ON true
        $$
    """)

    # ── operator functions (owner only) ──────────────────────────────────────────────────────────────────────
    op.execute(sql("""
        CREATE FUNCTION public.kyc_account_move_user(p_user_id uuid, p_to text, p_operator text, p_reason text)
        RETURNS text LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE from_account text; a record;
        BEGIN
            IF nullif(btrim(p_operator), '') IS NULL OR nullif(btrim(p_reason), '') IS NULL THEN
                RETURN 'operator_and_reason_required';
            END IF;
            SELECT c.* INTO a FROM public.kyc_accounts() c WHERE c.account_id = p_to;
            IF NOT FOUND OR a.revoked_at IS NOT NULL THEN
                RETURN 'unknown_or_revoked_account';
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended('kyc_account_rotation', 0));
            from_account := public.kyc_user_pin(p_user_id, NULL);
            IF from_account IS NOT DISTINCT FROM p_to THEN
                RETURN 'unchanged';
            END IF;
            PERFORM set_config('hyperchat.kyc_pin_move', 'on', true);
            INSERT INTO public.kyc_account_pins (user_id, account_id) VALUES (p_user_id, p_to)
            ON CONFLICT (user_id) DO UPDATE SET account_id = EXCLUDED.account_id, moved_at = now();
            PERFORM set_config('hyperchat.kyc_pin_move', '', true);
            DELETE FROM public.kyc_account_reservations WHERE user_id = p_user_id;
            INSERT INTO public.kyc_account_events (action, account_id, user_id, from_account, operator, reason)
            VALUES ('pin_moved', p_to, p_user_id, from_account, btrim(p_operator), btrim(p_reason));
            RETURN 'moved';
        END $$
    """))
    op.execute(sql("""
        CREATE FUNCTION public.kyc_account_revoke(p_account text, p_operator text, p_reason text)
        RETURNS text LANGUAGE plpgsql __DEFINER__ AS $$
        BEGIN
            IF nullif(btrim(p_operator), '') IS NULL OR nullif(btrim(p_reason), '') IS NULL THEN
                RETURN 'operator_and_reason_required';
            END IF;
            PERFORM set_config('hyperchat.operator', btrim(p_operator), true);
            IF p_account = 'primary' THEN
                UPDATE public.kyc_provider_config SET revoked_at = now(), revoked_reason = btrim(p_reason)
                WHERE revoked_at IS NULL;
            ELSE
                UPDATE public.kyc_provider_accounts SET revoked_at = now(), revoked_reason = btrim(p_reason)
                WHERE account_id = p_account AND revoked_at IS NULL;
            END IF;
            IF NOT FOUND THEN
                RETURN 'unknown_or_already_revoked';
            END IF;
            INSERT INTO public.kyc_account_events (action, account_id, operator, reason)
            VALUES ('account_revoked', p_account, btrim(p_operator), btrim(p_reason));
            RETURN 'revoked';
        END $$
    """))

    # ── privileges ───────────────────────────────────────────────────────────────────────────────────────────
    for table in NEW_TABLES:  # 0007's defaults gave app and worker DML, readonly SELECT
        op.execute(f"REVOKE ALL ON public.{table} FROM hyperchat_app, hyperchat_worker")
    op.execute("REVOKE ALL ON public.kyc_provider_accounts FROM hyperchat_readonly")  # webhook secrets
    op.execute("GRANT SELECT ON public.kyc_account_pins, public.kyc_account_events TO hyperchat_app")
    op.execute(f"REVOKE ALL ON FUNCTION {', '.join(APP_FUNCTIONS + OPERATOR_FUNCTIONS + INTERNAL_FUNCTIONS)} "
               f"FROM {EVERYONE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {', '.join(APP_FUNCTIONS)} TO hyperchat_app")


def downgrade() -> None:
    op.execute("""
        DO $$
        DECLARE extra bigint; foreign_rows bigint; events bigint; flags bigint;
        BEGIN
            SELECT count(*) INTO extra FROM public.kyc_provider_accounts;
            SELECT (SELECT count(*) FROM public.kyc_sessions WHERE account_id <> 'primary')
                 + (SELECT count(*) FROM public.kyc_webhook_events WHERE account_id <> 'primary')
                 + (SELECT count(*) FROM public.kyc_account_pins WHERE account_id <> 'primary') INTO foreign_rows;
            SELECT count(*) INTO events FROM public.kyc_account_events;
            SELECT count(*) INTO flags FROM public.kyc_provider_config
            WHERE NOT enabled OR monthly_cap IS NOT NULL OR revoked_at IS NOT NULL;
            IF extra > 0 OR foreign_rows > 0 OR events > 0 OR flags > 0 THEN
                RAISE EXCEPTION 'downgrade of 0040 stopped: % extra account(s), % row(s) of another account, % account '
                                'event(s), primary flags changed: % (nothing was changed); the operational rollback '
                                'is the previous release', extra, foreign_rows, events, flags > 0;
            END IF;
        END $$
    """)
    _, statements = _m0022()
    for name in REPLACED_0022:
        op.execute(_function_0022(statements, name))
    for fn in OPERATOR_FUNCTIONS + ("public.kyc_assign_account(uuid)",
                                    "public.kyc_create_session(uuid, uuid, uuid, integer, text, text)"):
        op.execute(f"DROP FUNCTION {fn}")
    op.execute("DROP TRIGGER kyc_provider_config_audit ON public.kyc_provider_config")
    op.execute("DROP TRIGGER kyc_provider_config_guard ON public.kyc_provider_config")
    for table in ("kyc_account_events", "kyc_account_reservations", "kyc_account_pins", "kyc_provider_accounts"):
        op.execute(f"DROP TABLE public.{table}")
    for fn in ("public.kyc_user_pin(uuid, text)", "public.kyc_account_usage(text)", "public.kyc_accounts()",
               "public.kyc_provider_accounts_guard()", "public.kyc_provider_config_guard()",
               "public.kyc_account_audit()", "public.kyc_account_pins_guard()"):
        op.execute(f"DROP FUNCTION {fn}")
    op.execute("ALTER TABLE public.kyc_webhook_events DROP COLUMN account_id")
    op.execute("DROP INDEX public.kyc_sessions_account_idx")
    op.execute("ALTER TABLE public.kyc_sessions DROP COLUMN account_id")
    op.execute("ALTER TABLE public.kyc_provider_config DROP COLUMN revoked_reason, DROP COLUMN revoked_at, "
               "DROP COLUMN monthly_cap, DROP COLUMN enabled")
