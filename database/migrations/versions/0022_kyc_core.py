"""Owner identity verification (Didit KYC core; KYC PR3 of the owner-approved plan, 2026-10-15).

Revision ID: 0022
Revises: 0021

KYC belongs to a USER (the immutable users.id, sent to Didit as vendor_data), never to a creator, so it never moves
with a creator transfer. Nothing existing changes: no user is verified, no creator is activated, and the donation
rule (public.creator_donation_block, migration 0020) is untouched; PR4 wires KYC into activation and enforcement.

* kyc_sessions: one row per Didit session a user started (session_id unique; vendor_data must equal the user id).
  Holds only the provider status, its time, and the statuses of the individual checks (no identity data). The
  runtime role may INSERT a new session (status always 'Not Started') and read; it can never UPDATE or DELETE rows,
  so a status is written only by public.kyc_record_provider_status, a SECURITY DEFINER function that validates the
  status, keeps provider order (a stale event never overwrites a newer one) and writes the audit row in the same
  transaction. Users with KYC history cannot be deleted (RESTRICT).
* kyc_webhook_events: one row per signature-verified Didit delivery (event_id unique = replay / retry dedupe), with
  the outcome. No payload is ever stored. Deleted after 90 days by the daily cleanup (app.maintenance).
* kyc_events: append-only audit of session creation, status changes and admin refreshes.
* public.user_kyc_approved(user_id, environment): the one rule "this user has an approved Didit verification".

Downgrade refuses (and changes nothing) once any KYC session or audit row exists.
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

STATUSES = ("'Not Started', 'In Progress', 'Awaiting User', 'In Review', 'Approved', 'Declined', 'Resubmitted', "
            "'Abandoned', 'Expired', 'Kyc Expired'")


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE public.kyc_sessions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
            vendor_data text NOT NULL,
            session_id uuid NOT NULL UNIQUE,
            workflow_id uuid NOT NULL,
            workflow_version integer,
            environment text NOT NULL CHECK (environment IN ('sandbox', 'live')),
            provider_status text NOT NULL DEFAULT 'Not Started' CHECK (provider_status IN ({STATUSES})),
            provider_updated_at timestamptz,
            check_statuses jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK (jsonb_typeof(check_statuses) = 'object'),
            approved_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT kyc_sessions_vendor_is_user CHECK (vendor_data = user_id::text)
        )
    """)
    op.execute("CREATE INDEX kyc_sessions_user_idx ON public.kyc_sessions (user_id, created_at)")

    op.execute("""
        CREATE TABLE public.kyc_webhook_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            event_id text NOT NULL UNIQUE CHECK (char_length(event_id) BETWEEN 1 AND 128),
            webhook_type text NOT NULL CHECK (char_length(webhook_type) <= 64),
            session_id uuid,
            vendor_data text CHECK (char_length(vendor_data) <= 64),
            provider_status text CHECK (char_length(provider_status) <= 32),
            provider_updated_at timestamptz,
            environment text CHECK (char_length(environment) <= 16),
            outcome text NOT NULL CHECK (outcome IN ('pending', 'applied', 'stale', 'unmatched', 'matched_later',
                                                     'identity_mismatch',
                                                     'incomplete', 'unknown_status', 'ignored')),
            received_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX kyc_webhook_events_unmatched_idx ON public.kyc_webhook_events (session_id) "
               "WHERE outcome = 'unmatched'")
    op.execute("CREATE INDEX kyc_webhook_events_received_idx ON public.kyc_webhook_events (received_at)")

    op.execute(f"""
        CREATE TABLE public.kyc_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
            session_id uuid,
            action text NOT NULL CHECK (action IN ('session_created', 'status_changed', 'admin_refresh')),
            from_status text CHECK (from_status IN ({STATUSES})),
            to_status text CHECK (to_status IN ({STATUSES})),
            source text NOT NULL CHECK (source IN ('session_create', 'webhook', 'admin_refresh')),
            actor_user_id uuid REFERENCES public.users(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX kyc_events_user_idx ON public.kyc_events (user_id, created_at)")

    # The only writer of a session's provider status. SECURITY DEFINER (owned by hyperchat_owner, fixed search_path):
    # the runtime role has no UPDATE on kyc_sessions. Returns 'applied' | 'stale' | 'unchanged' | 'not_found'.
    op.execute(f"""
        CREATE FUNCTION public.kyc_record_provider_status(p_session_id uuid, p_status text, p_provider_at timestamptz,
                                                          p_checks jsonb, p_source text, p_actor uuid)
        RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE s public.kyc_sessions%ROWTYPE;
        BEGIN
            IF p_status IS NULL OR p_status NOT IN ({STATUSES}) THEN
                RAISE EXCEPTION 'unknown KYC status' USING ERRCODE = 'check_violation';
            END IF;
            IF p_provider_at IS NULL OR p_source NOT IN ('webhook', 'admin_refresh') THEN
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
    """)
    op.execute("""
        CREATE FUNCTION public.user_kyc_approved(p_user_id uuid, p_environment text) RETURNS boolean
        LANGUAGE sql STABLE AS $$
            SELECT EXISTS (SELECT 1 FROM public.kyc_sessions
                           WHERE user_id = p_user_id AND environment = p_environment AND provider_status = 'Approved')
        $$
    """)

    for table in ("kyc_sessions", "kyc_webhook_events", "kyc_events"):
        op.execute(f"REVOKE ALL ON public.{table} FROM hyperchat_app, hyperchat_worker")
    op.execute("GRANT SELECT ON public.kyc_sessions TO hyperchat_app")
    op.execute("GRANT INSERT (user_id, vendor_data, session_id, workflow_id, workflow_version, environment) "
               "ON public.kyc_sessions TO hyperchat_app")
    op.execute("GRANT SELECT, INSERT, DELETE ON public.kyc_webhook_events TO hyperchat_app")
    op.execute("GRANT UPDATE (outcome) ON public.kyc_webhook_events TO hyperchat_app")
    op.execute("GRANT SELECT, INSERT ON public.kyc_events TO hyperchat_app")
    op.execute("GRANT EXECUTE ON FUNCTION public.kyc_record_provider_status(uuid, text, timestamptz, jsonb, text, uuid), "
               "public.user_kyc_approved(uuid, text) TO hyperchat_app")


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
    op.execute("DROP FUNCTION public.user_kyc_approved(uuid, text)")
    op.execute("DROP FUNCTION public.kyc_record_provider_status(uuid, text, timestamptz, jsonb, text, uuid)")
    op.execute("DROP TABLE public.kyc_events")
    op.execute("DROP TABLE public.kyc_webhook_events")
    op.execute("DROP TABLE public.kyc_sessions")
