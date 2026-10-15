"""Owner identity verification (Didit KYC core; KYC PR3 of the owner-approved plan, 2026-10-15).

Revision ID: 0022
Revises: 0021

KYC belongs to a USER (the immutable users.id, sent to Didit as vendor_data), never to a creator, so it never moves
with a creator transfer. Nothing existing changes: no user is verified, no creator is activated, and the donation
rule (public.creator_donation_block, migration 0020) is untouched; PR4 wires KYC into activation and enforcement.

Trust boundary: the API's database role (hyperchat_app) can never make anyone approved. It has SELECT only on the KYC
tables, and every write goes through a SECURITY DEFINER function owned by hyperchat_owner:
* public.kyc_receive_webhook(canonical_body, signature, x_timestamp) verifies Didit's X-Signature-V2 itself, with the
  webhook secret held in public.kyc_provider_config, which no runtime role can read (the API process does not have the
  secret either). Everything it applies comes from the signed body: freshness, environment/application, event_id
  dedupe, the session's own user and workflow, and Approved only with its matching decision. This is the ONLY path
  that can approve.
* public.kyc_apply_refresh(...) records what Didit's decision endpoint reported (an admin refresh). That response is
  not signed, so a refresh can never approve: Approved is answered 'approval_needs_webhook' and nothing changes.
  Its time is when the provider was asked (at most 2 minutes ago), so a slow refresh never outranks a newer webhook.
* public.kyc_create_session(...) stores a session Didit created (status always 'Not Started', the configured
  workflow and environment only) and applies the signed webhooks that arrived before it (early webhooks).
  Sessions and webhooks for one Didit session serialize on an advisory lock, so an early webhook can never stay
  'unmatched' once its session is stored.
* public.kyc_purge_webhook_receipts(dry_run) is the daily 90-day cleanup (the runtime role cannot DELETE receipts).
* public.kyc_record_provider_status(...) is internal (no runtime grant): validates the status, keeps provider order
  (an older event never overwrites a newer one; a later revocation or expiry always wins) and writes the audit row.
* public.user_kyc_approved(user_id, environment): the one rule "this user has an approved Didit verification".

Tables: kyc_sessions (one row per Didit session; vendor_data must equal the user id; users with KYC history cannot be
deleted), kyc_webhook_events (one row per signature-verified delivery: ids, status, provider time, check statuses and
the outcome; never the body), kyc_events (audit, written only by the functions above), kyc_provider_config (one row:
environment, workflow, optional application id, webhook secret; set by the operator with etl/kyc_provider_config.py).

Downgrade refuses (and changes nothing) once any KYC session or audit row exists.
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

STATUSES = ("'Not Started', 'In Progress', 'Awaiting User', 'In Review', 'Approved', 'Declined', 'Resubmitted', "
            "'Abandoned', 'Expired', 'Kyc Expired'")
DEFINER = "SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp"
# Sessions and webhooks of one Didit session take this lock; READ COMMITTED gives each later statement a fresh
# snapshot, so whichever runs second sees what the first committed.
LOCK = "PERFORM pg_advisory_xact_lock(hashtextextended('kyc_session:' || {}::text, 0));"
READ_COMMITTED = """
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'KYC writes need READ COMMITTED' USING ERRCODE = 'feature_not_supported';
    END IF;"""
PUBLIC_FUNCTIONS = ("public.kyc_create_session(uuid, uuid, uuid, integer, text)",
                    "public.kyc_receive_webhook(text, text, text)",
                    "public.kyc_apply_refresh(uuid, text, jsonb, timestamptz, uuid)",
                    "public.kyc_purge_webhook_receipts(boolean)",
                    "public.user_kyc_approved(uuid, text)")
INTERNAL_FUNCTIONS = ("public.kyc_record_provider_status(uuid, text, timestamptz, jsonb, text, uuid)",
                      "public.kyc_hmac_sha256(bytea, bytea)", "public.kyc_uuid(text)", "public.kyc_check_statuses(jsonb)")


def _sql(text: str) -> str:
    return (text.replace("__STATUSES__", STATUSES).replace("__DEFINER__", DEFINER)
            .replace("__READ_COMMITTED__", READ_COMMITTED))


def upgrade() -> None:
    op.execute(_sql("""
        CREATE TABLE public.kyc_sessions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
            vendor_data text NOT NULL,
            session_id uuid NOT NULL UNIQUE,
            workflow_id uuid NOT NULL,
            workflow_version integer,
            environment text NOT NULL CHECK (environment IN ('sandbox', 'live')),
            provider_status text NOT NULL DEFAULT 'Not Started' CHECK (provider_status IN (__STATUSES__)),
            provider_updated_at timestamptz,
            check_statuses jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(check_statuses) = 'object'),
            approved_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT kyc_sessions_vendor_is_user CHECK (vendor_data = user_id::text)
        )
    """))
    op.execute("CREATE INDEX kyc_sessions_user_idx ON public.kyc_sessions (user_id, created_at)")

    op.execute("""
        CREATE TABLE public.kyc_webhook_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            event_id text NOT NULL UNIQUE CHECK (char_length(event_id) BETWEEN 1 AND 128),
            webhook_type text NOT NULL CHECK (char_length(webhook_type) <= 64),
            session_id uuid,
            vendor_data text CHECK (char_length(vendor_data) <= 64),
            workflow_id uuid,
            provider_status text CHECK (char_length(provider_status) <= 32),
            provider_updated_at timestamptz,
            environment text CHECK (char_length(environment) <= 16),
            check_statuses jsonb,
            outcome text NOT NULL CHECK (outcome IN ('pending', 'applied', 'stale', 'unmatched', 'matched_later',
                                                     'identity_mismatch', 'incomplete', 'unknown_status', 'ignored')),
            received_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX kyc_webhook_events_unmatched_idx ON public.kyc_webhook_events (session_id) "
               "WHERE outcome = 'unmatched'")
    op.execute("CREATE INDEX kyc_webhook_events_received_idx ON public.kyc_webhook_events (received_at)")

    op.execute(_sql("""
        CREATE TABLE public.kyc_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
            session_id uuid,
            action text NOT NULL CHECK (action IN ('session_created', 'status_changed', 'admin_refresh')),
            from_status text CHECK (from_status IN (__STATUSES__)),
            to_status text CHECK (to_status IN (__STATUSES__)),
            source text NOT NULL CHECK (source IN ('session_create', 'webhook', 'admin_refresh')),
            actor_user_id uuid REFERENCES public.users(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """))
    op.execute("CREATE INDEX kyc_events_user_idx ON public.kyc_events (user_id, created_at)")

    # One row: this database's Didit application. No runtime role may read it (the webhook secret lives only here).
    op.execute("""
        CREATE TABLE public.kyc_provider_config (
            singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            environment text NOT NULL CHECK (environment IN ('sandbox', 'live')),
            workflow_id uuid NOT NULL,
            application_id uuid,
            webhook_secret text NOT NULL CHECK (char_length(webhook_secret) BETWEEN 16 AND 512),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)

    # ── internal helpers (no runtime grant) ──
    op.execute("""
        CREATE FUNCTION public.kyc_uuid(p text) RETURNS uuid LANGUAGE sql IMMUTABLE
        SET search_path = pg_catalog AS $$
            SELECT CASE WHEN p ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN p::uuid END
        $$
    """)
    # RFC 2104 HMAC-SHA256 with the built-in sha256() (no extension privileges needed).
    op.execute("""
        CREATE FUNCTION public.kyc_hmac_sha256(p_key bytea, p_message bytea) RETURNS bytea
        LANGUAGE plpgsql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
        DECLARE k bytea := p_key; inner_key bytea; outer_key bytea;
        BEGIN
            IF length(k) > 64 THEN
                k := sha256(k);
            END IF;
            k := k || decode(repeat('00', 64 - length(k)), 'hex');
            inner_key := k; outer_key := k;
            FOR i IN 0..63 LOOP
                inner_key := set_byte(inner_key, i, get_byte(k, i) # 54);
                outer_key := set_byte(outer_key, i, get_byte(k, i) # 92);
            END LOOP;
            RETURN sha256(outer_key || sha256(inner_key || p_message));
        END $$
    """)
    # {check: [statuses]} for the four identity checks; never any field but `status`.
    op.execute("""
        CREATE FUNCTION public.kyc_check_statuses(p_decision jsonb) RETURNS jsonb LANGUAGE sql IMMUTABLE
        SET search_path = pg_catalog AS $$
            SELECT COALESCE(jsonb_object_agg(k, (
                       SELECT COALESCE(jsonb_agg(e -> 'status' ORDER BY n), '[]'::jsonb)
                       FROM jsonb_array_elements(p_decision -> k) WITH ORDINALITY AS t(e, n)
                       WHERE jsonb_typeof(e) = 'object' AND jsonb_typeof(e -> 'status') = 'string'
                             AND char_length(e ->> 'status') <= 32)), '{}'::jsonb)
            FROM unnest(ARRAY['id_verifications', 'liveness_checks', 'face_matches', 'aml_screenings']) AS k
            WHERE jsonb_typeof(p_decision) = 'object' AND jsonb_typeof(p_decision -> k) = 'array'
        $$
    """)
    # The only writer of a session's status. Returns 'applied' | 'stale' | 'unchanged' | 'not_found'.
    op.execute(_sql("""
        CREATE FUNCTION public.kyc_record_provider_status(p_session_id uuid, p_status text, p_provider_at timestamptz,
                                                          p_checks jsonb, p_source text, p_actor uuid)
        RETURNS text LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE s public.kyc_sessions%ROWTYPE;
        BEGIN
            IF p_status IS NULL OR p_status NOT IN (__STATUSES__) THEN
                RAISE EXCEPTION 'unknown KYC status' USING ERRCODE = 'check_violation';
            END IF;
            IF p_provider_at IS NULL OR p_source IS NULL OR p_source NOT IN ('webhook', 'admin_refresh') THEN
                RAISE EXCEPTION 'provider time and source are required' USING ERRCODE = 'check_violation';
            END IF;
            SELECT * INTO s FROM public.kyc_sessions WHERE session_id = p_session_id FOR UPDATE;
            IF NOT FOUND THEN
                RETURN 'not_found';
            END IF;
            IF s.provider_updated_at IS NOT NULL AND p_provider_at < s.provider_updated_at THEN
                RETURN 'stale';  -- an older provider state never overwrites a newer one
            END IF;
            IF s.provider_status = p_status AND (p_checks IS NULL OR p_checks = s.check_statuses) THEN
                UPDATE public.kyc_sessions SET provider_updated_at = p_provider_at, updated_at = now()
                    WHERE id = s.id;
                RETURN 'unchanged';
            END IF;
            UPDATE public.kyc_sessions SET provider_status = p_status, provider_updated_at = p_provider_at,
                check_statuses = COALESCE(p_checks, check_statuses), updated_at = now(),
                approved_at = CASE WHEN p_status = 'Approved' THEN COALESCE(approved_at, now()) ELSE NULL END
                WHERE id = s.id;
            IF s.provider_status IS DISTINCT FROM p_status THEN
                INSERT INTO public.kyc_events (user_id, session_id, action, from_status, to_status, source,
                                               actor_user_id)
                VALUES (s.user_id, s.session_id, CASE WHEN p_source = 'admin_refresh' THEN 'admin_refresh'
                        ELSE 'status_changed' END, s.provider_status, p_status, p_source, p_actor);
            END IF;
            RETURN 'applied';
        END $$
    """))

    # ── entry points for the API's role ──
    # 'ok' | 'not_configured' | 'wrong_configuration' | 'owner_mismatch'
    op.execute(_sql("""
        CREATE FUNCTION public.kyc_create_session(p_user_id uuid, p_session_id uuid, p_workflow_id uuid,
                                                  p_workflow_version integer, p_environment text)
        RETURNS text LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE cfg public.kyc_provider_config%ROWTYPE; s public.kyc_sessions%ROWTYPE; r record;
        BEGIN
            __READ_COMMITTED__
            SELECT * INTO cfg FROM public.kyc_provider_config;
            IF NOT FOUND THEN
                RETURN 'not_configured';
            END IF;
            IF p_environment IS DISTINCT FROM cfg.environment OR p_workflow_id IS DISTINCT FROM cfg.workflow_id THEN
                RETURN 'wrong_configuration';
            END IF;
            """ + LOCK.format("p_session_id") + """
            INSERT INTO public.kyc_sessions (user_id, vendor_data, session_id, workflow_id, workflow_version,
                                             environment)
            VALUES (p_user_id, p_user_id::text, p_session_id, p_workflow_id, p_workflow_version, p_environment)
            ON CONFLICT (session_id) DO NOTHING;
            IF FOUND THEN
                INSERT INTO public.kyc_events (user_id, session_id, action, to_status, source, actor_user_id)
                VALUES (p_user_id, p_session_id, 'session_created', 'Not Started', 'session_create', p_user_id);
            END IF;
            SELECT * INTO s FROM public.kyc_sessions WHERE session_id = p_session_id;
            IF s.user_id <> p_user_id THEN
                RETURN 'owner_mismatch';
            END IF;
            -- Signed webhooks that arrived before this row existed, oldest first (receipts are written only by
            -- kyc_receive_webhook, after the signature was verified).
            FOR r IN SELECT id, vendor_data, workflow_id, environment, provider_status, provider_updated_at,
                            check_statuses
                     FROM public.kyc_webhook_events WHERE session_id = p_session_id AND outcome = 'unmatched'
                     ORDER BY provider_updated_at, received_at FOR UPDATE LOOP
                IF public.kyc_uuid(r.vendor_data) IS DISTINCT FROM s.user_id
                   OR r.workflow_id IS DISTINCT FROM s.workflow_id OR r.environment IS DISTINCT FROM s.environment THEN
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
    # p_canonical: the canonical JSON (X-Signature-V2 form) of the delivered body, which is what Didit signed.
    # Refusals: 'not_configured' | 'bad_signature' | 'malformed' | 'stale_timestamp' | 'wrong_application'.
    # Recorded: 'duplicate' | 'ignored' | 'unknown_status' | 'incomplete' | 'identity_mismatch' | 'unmatched' |
    # 'stale' | 'applied'.
    op.execute(_sql("""
        CREATE FUNCTION public.kyc_receive_webhook(p_canonical text, p_signature text, p_header_timestamp text)
        RETURNS text LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE
            cfg public.kyc_provider_config%ROWTYPE; b jsonb; presented text := lower(btrim(coalesce(p_signature, '')));
            now_s numeric := extract(epoch FROM clock_timestamp());
            v_event text; v_type text; v_status text; v_session uuid; v_vendor uuid; v_workflow uuid; v_at timestamptz;
            v_decision jsonb; v_checks jsonb; v_outcome text := 'pending'; v_receipt uuid; v_result text;
            s public.kyc_sessions%ROWTYPE;
        BEGIN
            __READ_COMMITTED__
            SELECT * INTO cfg FROM public.kyc_provider_config;
            IF NOT FOUND THEN
                RETURN 'not_configured';
            END IF;
            -- Compared as digests of the hex strings, so the comparison time says nothing about the expected value.
            IF p_canonical IS NULL OR presented !~ '^[0-9a-f]{64}$' OR sha256(convert_to(presented, 'UTF8')) <>
               sha256(convert_to(encode(public.kyc_hmac_sha256(convert_to(cfg.webhook_secret, 'UTF8'),
                                                               convert_to(p_canonical, 'UTF8')), 'hex'), 'UTF8')) THEN
                RETURN 'bad_signature';
            END IF;
            BEGIN
                b := p_canonical::jsonb;
            EXCEPTION WHEN invalid_text_representation OR untranslatable_character THEN
                RETURN 'malformed';
            END;
            IF jsonb_typeof(b) <> 'object' THEN
                RETURN 'malformed';
            END IF;
            -- Both the SIGNED body timestamp and the (unsigned) X-Timestamp header within 300 s, either direction.
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
            IF (b ->> 'environment') IS DISTINCT FROM cfg.environment OR (cfg.application_id IS NOT NULL AND
                    public.kyc_uuid(b ->> 'application_id') IS DISTINCT FROM cfg.application_id) THEN
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
                v_outcome := 'unknown_status';  -- never applied, never approves
            ELSIF v_session IS NULL OR v_at IS NULL THEN
                v_outcome := 'incomplete';
            ELSIF v_vendor IS NULL OR v_workflow IS DISTINCT FROM cfg.workflow_id THEN
                v_outcome := 'identity_mismatch';
            ELSIF v_status = 'Approved' AND NOT coalesce(public.kyc_uuid(v_decision ->> 'session_id') = v_session
                                                         AND (v_decision ->> 'status') = 'Approved', false) THEN
                v_outcome := 'incomplete';  -- an approval must carry its matching decision
            END IF;
            IF v_decision IS NOT NULL THEN
                v_checks := public.kyc_check_statuses(v_decision);
            END IF;

            INSERT INTO public.kyc_webhook_events (event_id, webhook_type, session_id, vendor_data, workflow_id,
                                                   provider_status, provider_updated_at, environment, check_statuses,
                                                   outcome)
            VALUES (v_event, v_type, v_session, v_vendor::text, v_workflow, v_status, v_at, cfg.environment, v_checks,
                    v_outcome)
            ON CONFLICT (event_id) DO NOTHING RETURNING id INTO v_receipt;
            IF v_receipt IS NULL THEN
                RETURN 'duplicate';
            END IF;
            IF v_outcome <> 'pending' THEN
                RETURN v_outcome;
            END IF;
            """ + LOCK.format("v_session") + """
            SELECT * INTO s FROM public.kyc_sessions WHERE session_id = v_session;
            IF NOT FOUND THEN
                v_outcome := 'unmatched';  -- an early webhook: applied by kyc_create_session
            ELSIF s.user_id <> v_vendor OR s.workflow_id <> v_workflow OR s.environment <> cfg.environment THEN
                v_outcome := 'identity_mismatch';
            ELSE
                v_result := public.kyc_record_provider_status(v_session, v_status, v_at, v_checks, 'webhook', NULL);
                v_outcome := CASE WHEN v_result = 'stale' THEN 'stale' ELSE 'applied' END;
            END IF;
            UPDATE public.kyc_webhook_events SET outcome = v_outcome WHERE id = v_receipt;
            RETURN v_outcome;
        END $$
    """))
    # An admin refresh: Didit's decision endpoint is not signed, so it can never approve. p_observed_at is when the
    # provider was asked (before the call), within the last 2 minutes: a slow or delayed refresh is ordered by the
    # moment it describes, never by when it was written.
    op.execute(_sql("""
        CREATE FUNCTION public.kyc_apply_refresh(p_session_id uuid, p_status text, p_checks jsonb,
                                                 p_observed_at timestamptz, p_actor uuid)
        RETURNS text LANGUAGE plpgsql __DEFINER__ AS $$
        BEGIN
            IF p_observed_at IS NULL OR p_observed_at > clock_timestamp()
               OR p_observed_at < clock_timestamp() - interval '2 minutes' THEN
                RAISE EXCEPTION 'a refresh is dated when the provider was asked' USING ERRCODE = 'check_violation';
            END IF;
            IF p_status = 'Approved' THEN
                RETURN 'approval_needs_webhook';
            END IF;
            RETURN public.kyc_record_provider_status(p_session_id, p_status, p_observed_at,
                CASE WHEN jsonb_typeof(p_checks) = 'object' THEN p_checks END, 'admin_refresh', p_actor);
        END $$
    """))
    op.execute(_sql("""
        CREATE FUNCTION public.kyc_purge_webhook_receipts(p_dry_run boolean) RETURNS bigint
        LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE n bigint;
        BEGIN
            IF p_dry_run THEN
                SELECT count(*) INTO n FROM public.kyc_webhook_events WHERE received_at < now() - interval '90 days';
            ELSE
                DELETE FROM public.kyc_webhook_events WHERE received_at < now() - interval '90 days';
                GET DIAGNOSTICS n = ROW_COUNT;
            END IF;
            RETURN n;
        END $$
    """))
    op.execute("""
        CREATE FUNCTION public.user_kyc_approved(p_user_id uuid, p_environment text) RETURNS boolean
        LANGUAGE sql STABLE AS $$
            SELECT EXISTS (SELECT 1 FROM public.kyc_sessions
                           WHERE user_id = p_user_id AND environment = p_environment AND provider_status = 'Approved')
        $$
    """)

    for table in ("kyc_sessions", "kyc_webhook_events", "kyc_events", "kyc_provider_config"):
        op.execute(f"REVOKE ALL ON public.{table} FROM hyperchat_app, hyperchat_worker")
    op.execute("REVOKE ALL ON public.kyc_provider_config FROM hyperchat_readonly")
    op.execute("GRANT SELECT ON public.kyc_sessions, public.kyc_webhook_events, public.kyc_events TO hyperchat_app")
    op.execute(f"REVOKE ALL ON FUNCTION {', '.join(PUBLIC_FUNCTIONS + INTERNAL_FUNCTIONS)} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {', '.join(PUBLIC_FUNCTIONS)} TO hyperchat_app")


def downgrade() -> None:
    op.execute("""
        DO $$
        DECLARE sessions bigint; events bigint;
        BEGIN
            SELECT count(*) INTO sessions FROM public.kyc_sessions;
            SELECT count(*) INTO events FROM public.kyc_events;
            IF sessions > 0 OR events > 0 THEN
                RAISE EXCEPTION 'downgrade of 0022 stopped: % KYC session(s), % KYC audit row(s) would be lost '
                                '(nothing was changed)', sessions, events;
            END IF;
        END $$
    """)
    for function in PUBLIC_FUNCTIONS + INTERNAL_FUNCTIONS:
        op.execute(f"DROP FUNCTION {function}")
    for table in ("kyc_provider_config", "kyc_events", "kyc_webhook_events", "kyc_sessions"):
        op.execute(f"DROP TABLE public.{table}")
