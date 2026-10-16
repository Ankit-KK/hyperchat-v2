"""Mandatory KYC enforcement and the controlled legacy rollout (KYC PR4 of the owner-approved plan, 2026-10-15).

Revision ID: 0023
Revises: 0022

Enforcement is ONE row, public.kyc_enforcement, that starts 'draft' and only moves forward (draft -> active ->
closed, never back; a trigger refuses anything else, for every role). Nothing here activates it: not this migration,
not Didit credentials, not verified KYC. Activation, closure and grace grants are SECURITY DEFINER functions with no
runtime grant; the operator runs them with the migration role (app.kyc.operator), after the approvals of
docs/kyc-rollout.md.

The rule (public.creator_donation_block, the one rule of migration 0020, extended; the donations insert trigger still
runs it for every runtime-role insert):
  not_found / not_active (status, unchanged) -> 'restricted' (an open admin restriction) ->
  draft: a legacy creator still owned by its baseline owner keeps its PR1 eligibility (nothing changes before the
         rollout) -> 'no_owner' (no owner, or an inactive one) -> eligible when the owner's Didit KYC is approved now
         (public.user_kyc_approved, migration 0022) -> active: a legacy creator still owned by its baseline owner is
         eligible while it holds an unexpired, unrevoked grace grant bound to that owner -> 'kyc_required'.
  closed: no grace at all; every creator needs its owner's KYC.

* kyc_grace_grants: finite (at most 90 days, a CHECK), bound to the creator AND the owner at grant time (no FK on the
  owner: a deleted user fails closed), one per creator-owner pair ever (no rolling extensions), immutable except one
  revocation. Suspension, an owner transfer and a restriction revoke them (app.admin, kyc_restrict_creator); closure
  ignores them.
* kyc_restrictions: admin holds with a reason code; immutable except one lift, which needs a note. A lift never
  bypasses KYC (the rule still needs the owner's KYC or valid grace).
* kyc_enforcement_events: append-only audit of activation, closure, grants, revocations, expiries, restrictions and
  restore acknowledgements. UPDATE is refused for every role; the runtime roles may only read. Creator rows with
  enforcement history cannot be deleted (RESTRICT), and creator_status_events (migration 0020) becomes RESTRICT too,
  so deleting a creator can no longer erase its status audit.
* kyc_enforcement.generation / chain: every enforcement change is one audit row at the next generation whose chain
  is sha256(previous chain | generation | the row's random id | its content | time): a hash chain over the history.
  The API and the operator tool record (generation, chain) in a restore witness OUTSIDE PostgreSQL (app.kyc.witness).
  A database restored from an older snapshot no longer holds the witnessed chain at the witnessed generation, and
  that never heals by adding changes (the restored history diverged; its later rows carry other random ids), so new
  donations fail closed until the operator's audited acknowledgement (kyc_acknowledge_restore) re-anchors it. A
  database-local value alone cannot detect a restore; the witness file is the external part.
* Grace is for a NEVER-decided owner only: once the owner has any Didit decision (an approval, a Declined or a Kyc
  Expired, in this environment, durable in kyc_sessions.adverse_at / kyc_events), grace never applies again: a
  revoked, expired or failed verification never falls back to legacy grace; only a new approval restores eligibility.
* streamers_guard_activation: the runtime roles cannot set a creator 'active' (insert or update) unless the activation
  rule (pending: the owner's own KYC, no restriction) and the donation rule allow it, so the application's database
  credentials cannot bypass admin activation / reactivation checks.

Grants to the API role: SELECT on the new tables; EXECUTE on creator_donation_block, kyc_activation_block,
kyc_revoke_grace, kyc_restrict_creator, kyc_record_grace_expiries, kyc_enforcement_preview: only fail-closed changes
and reads. Lifting a restriction is operator-only (kyc_lift_restriction), like activation, closure and grants.

Downgrade refuses (and changes nothing) once enforcement left 'draft', its generation moved, or any grant,
restriction or enforcement event exists.
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

DEFINER = "SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp"
GRACE_MAX_DAYS = 90
RESTRICTION_REASONS = "'identity_concern', 'owner_dispute', 'fraud_review', 'kyc_lost', 'admin_hold'"
RUNTIME_FUNCTIONS = ("public.kyc_activation_block(uuid)", "public.kyc_revoke_grace(uuid, text, uuid)",
                     "public.kyc_restrict_creator(uuid, text, text, uuid)", "public.kyc_record_grace_expiries()",
                     "public.kyc_enforcement_preview()")
OPERATOR_FUNCTIONS = ("public.kyc_enforcement_activate(text, text)", "public.kyc_enforcement_close(text, text)",
                      "public.kyc_grant_grace(uuid, integer, text, text)",
                      "public.kyc_lift_restriction(uuid, uuid, text, text)",
                      "public.kyc_acknowledge_restore(text, bigint, text)")
INTERNAL_FUNCTIONS = ("public.kyc_donation_decision(uuid, text)", "public.kyc_owner_decided(uuid, text)",
                      "public.kyc_enforcement_log(text, uuid, uuid, uuid, uuid, text, text, bigint)",
                      "public.streamers_guard_activation()",
                      "public.kyc_enforcement_guard()", "public.kyc_grace_grants_guard()",
                      "public.kyc_restrictions_guard()", "public.kyc_enforcement_events_guard()")
NEW_TABLES = ("kyc_enforcement", "kyc_grace_grants", "kyc_restrictions", "kyc_enforcement_events")

# PR1's rule (migration 0020), restored by the downgrade.
PR1_RULE = """
    CREATE OR REPLACE FUNCTION public.creator_donation_block(p_streamer_id uuid) RETURNS text LANGUAGE sql AS $$
        SELECT CASE
            WHEN s.id IS NULL THEN 'not_found'
            WHEN s.status <> 'active' THEN 'not_active'
            -- Until KYC exists, only a legacy creator still owned by its baseline owner may accept donations.
            WHEN NOT (s.kyc_track = 'legacy' AND s.user_id IS NOT DISTINCT FROM s.kyc_baseline_owner_id)
                THEN 'kyc_required'
        END
        FROM (SELECT 1) AS one LEFT JOIN public.streamers s ON s.id = p_streamer_id
    $$
"""


def _sql(text: str) -> str:
    return (text.replace("__DEFINER__", DEFINER).replace("__REASONS__", RESTRICTION_REASONS)
            .replace("__GRACE_MAX_DAYS__", str(GRACE_MAX_DAYS)))


def upgrade() -> None:
    op.execute(_sql("""
        CREATE TABLE public.kyc_enforcement (
            singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            state text NOT NULL DEFAULT 'draft' CHECK (state IN ('draft', 'active', 'closed')),
            generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
            chain text NOT NULL DEFAULT '',  -- hash-chain head of kyc_enforcement_events (the restore witness's anchor)
            grace_max_days integer NOT NULL DEFAULT __GRACE_MAX_DAYS__
                CHECK (grace_max_days BETWEEN 1 AND __GRACE_MAX_DAYS__),
            policy text CHECK (char_length(policy) <= 64),
            activated_at timestamptz,
            activated_by text CHECK (char_length(activated_by) BETWEEN 1 AND 200),
            activation_authorization text CHECK (char_length(activation_authorization) BETWEEN 1 AND 200),
            activation_migration text,
            closed_at timestamptz,
            closed_by text CHECK (char_length(closed_by) BETWEEN 1 AND 200),
            closure_authorization text CHECK (char_length(closure_authorization) BETWEEN 1 AND 200),
            CONSTRAINT kyc_enforcement_state_record CHECK (CASE state
                WHEN 'draft' THEN activated_at IS NULL AND closed_at IS NULL
                WHEN 'active' THEN activated_at IS NOT NULL AND activated_by IS NOT NULL
                    AND activation_authorization IS NOT NULL AND policy IS NOT NULL AND closed_at IS NULL
                ELSE activated_at IS NOT NULL AND closed_at IS NOT NULL AND closed_by IS NOT NULL
                    AND closure_authorization IS NOT NULL END)
        )
    """))
    op.execute("INSERT INTO public.kyc_enforcement DEFAULT VALUES")  # draft: enforcement starts inactive
    op.execute("""
        CREATE FUNCTION public.kyc_enforcement_guard() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog AS $$
        DECLARE old_rank int := array_position(ARRAY['draft', 'active', 'closed'], OLD.state);
                new_rank int := array_position(ARRAY['draft', 'active', 'closed'], NEW.state);
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'the KYC enforcement record cannot be deleted' USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF new_rank < old_rank OR NEW.generation < OLD.generation
               OR (NEW.chain IS DISTINCT FROM OLD.chain AND NEW.generation <= OLD.generation) THEN
                RAISE EXCEPTION 'KYC enforcement only moves forward' USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF NEW.grace_max_days <> OLD.grace_max_days
               OR (OLD.activated_at IS NOT NULL AND (NEW.activated_at, NEW.activated_by, NEW.activation_authorization,
                   NEW.activation_migration, NEW.policy) IS DISTINCT FROM (OLD.activated_at, OLD.activated_by,
                   OLD.activation_authorization, OLD.activation_migration, OLD.policy))
               OR (OLD.closed_at IS NOT NULL AND (NEW.closed_at, NEW.closed_by, NEW.closure_authorization)
                   IS DISTINCT FROM (OLD.closed_at, OLD.closed_by, OLD.closure_authorization)) THEN
                RAISE EXCEPTION 'the KYC enforcement policy and its activation record are immutable'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER kyc_enforcement_guard BEFORE UPDATE OR DELETE ON public.kyc_enforcement "
               "FOR EACH ROW EXECUTE FUNCTION public.kyc_enforcement_guard()")

    op.execute(_sql("""
        CREATE TABLE public.kyc_grace_grants (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            streamer_id uuid NOT NULL REFERENCES public.streamers(id) ON DELETE RESTRICT,
            owner_id uuid NOT NULL,  -- the owner at grant time; no FK on purpose (a deleted user fails closed)
            granted_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            granted_by text NOT NULL CHECK (char_length(granted_by) BETWEEN 1 AND 200),
            reason text NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 500),
            revoked_at timestamptz,
            revoked_by uuid,
            revoke_reason text CHECK (char_length(revoke_reason) BETWEEN 1 AND 500),
            CONSTRAINT kyc_grace_grants_finite CHECK (expires_at > granted_at
                AND expires_at <= granted_at + interval '__GRACE_MAX_DAYS__ days'),
            CONSTRAINT kyc_grace_grants_revocation CHECK ((revoked_at IS NULL) = (revoke_reason IS NULL)),
            CONSTRAINT kyc_grace_grants_once UNIQUE (streamer_id, owner_id)
        )
    """))
    op.execute("""
        CREATE FUNCTION public.kyc_grace_grants_guard() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog AS $$
        BEGIN
            IF (NEW.id, NEW.streamer_id, NEW.owner_id, NEW.granted_at, NEW.expires_at, NEW.granted_by, NEW.reason)
               IS DISTINCT FROM (OLD.id, OLD.streamer_id, OLD.owner_id, OLD.granted_at, OLD.expires_at,
                                 OLD.granted_by, OLD.reason)
               OR (OLD.revoked_at IS NOT NULL AND (NEW.revoked_at, NEW.revoked_by, NEW.revoke_reason)
                   IS DISTINCT FROM (OLD.revoked_at, OLD.revoked_by, OLD.revoke_reason)) THEN
                RAISE EXCEPTION 'a grace grant is immutable except its one revocation'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER kyc_grace_grants_guard BEFORE UPDATE ON public.kyc_grace_grants "
               "FOR EACH ROW EXECUTE FUNCTION public.kyc_grace_grants_guard()")

    op.execute(_sql("""
        CREATE TABLE public.kyc_restrictions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            streamer_id uuid NOT NULL REFERENCES public.streamers(id) ON DELETE RESTRICT,
            reason text NOT NULL CHECK (reason IN (__REASONS__)),
            note text CHECK (char_length(note) <= 500),
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            lifted_at timestamptz,
            lifted_by uuid,
            lift_note text CHECK (char_length(lift_note) BETWEEN 1 AND 500),
            CONSTRAINT kyc_restrictions_lift CHECK ((lifted_at IS NULL) = (lift_note IS NULL))
        )
    """))
    op.execute("CREATE INDEX kyc_restrictions_open_idx ON public.kyc_restrictions (streamer_id) WHERE lifted_at IS NULL")
    op.execute("""
        CREATE FUNCTION public.kyc_restrictions_guard() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog AS $$
        BEGIN
            IF (NEW.id, NEW.streamer_id, NEW.reason, NEW.note, NEW.created_by, NEW.created_at)
               IS DISTINCT FROM (OLD.id, OLD.streamer_id, OLD.reason, OLD.note, OLD.created_by, OLD.created_at)
               OR (OLD.lifted_at IS NOT NULL AND (NEW.lifted_at, NEW.lifted_by, NEW.lift_note)
                   IS DISTINCT FROM (OLD.lifted_at, OLD.lifted_by, OLD.lift_note)) THEN
                RAISE EXCEPTION 'a KYC restriction is immutable except its one lift'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER kyc_restrictions_guard BEFORE UPDATE ON public.kyc_restrictions "
               "FOR EACH ROW EXECUTE FUNCTION public.kyc_restrictions_guard()")

    op.execute("""
        CREATE TABLE public.kyc_enforcement_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            action text NOT NULL CHECK (action IN ('enforcement_activated', 'enforcement_closed', 'grace_granted',
                                                   'grace_revoked', 'grace_expired', 'restriction_added',
                                                   'restriction_lifted', 'restore_acknowledged')),
            streamer_id uuid REFERENCES public.streamers(id) ON DELETE RESTRICT,
            grant_id uuid REFERENCES public.kyc_grace_grants(id) ON DELETE RESTRICT,
            restriction_id uuid REFERENCES public.kyc_restrictions(id) ON DELETE RESTRICT,
            actor_user_id uuid,  -- no FK: deleting a user never rewrites the audit
            operator text CHECK (char_length(operator) <= 200),
            detail text CHECK (char_length(detail) <= 500),
            generation bigint,
            chain text,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE UNIQUE INDEX kyc_enforcement_events_generation ON public.kyc_enforcement_events (generation) "
               "WHERE generation IS NOT NULL")
    op.execute("CREATE INDEX kyc_enforcement_events_streamer_idx ON public.kyc_enforcement_events "
               "(streamer_id, created_at)")
    op.execute("CREATE UNIQUE INDEX kyc_enforcement_events_expiry_once ON public.kyc_enforcement_events (grant_id) "
               "WHERE action = 'grace_expired'")
    op.execute("""
        CREATE FUNCTION public.kyc_enforcement_events_guard() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'the KYC enforcement audit is append-only' USING ERRCODE = 'insufficient_privilege';
        END $$
    """)
    op.execute("CREATE TRIGGER kyc_enforcement_events_guard BEFORE UPDATE ON public.kyc_enforcement_events "
               "FOR EACH ROW EXECUTE FUNCTION public.kyc_enforcement_events_guard()")
    # Deleting a creator must not erase its status audit either (migration 0020 had ON DELETE CASCADE).
    op.execute("""
        ALTER TABLE public.creator_status_events
            DROP CONSTRAINT creator_status_events_streamer_id_fkey,
            ADD CONSTRAINT creator_status_events_streamer_id_fkey FOREIGN KEY (streamer_id)
                REFERENCES public.streamers(id) ON DELETE RESTRICT
    """)

    # ── the rule ──
    # Has this user ever had a Didit decision here (an approval, a Declined or a Kyc Expired)? Durable: adverse_at is
    # never lowered and kyc_events is append-only.
    op.execute(_sql("""
        CREATE FUNCTION public.kyc_owner_decided(p_user_id uuid, p_environment text) RETURNS boolean
        LANGUAGE sql STABLE __DEFINER__ AS $$
            SELECT EXISTS (
                SELECT 1 FROM public.kyc_sessions s
                WHERE s.user_id = p_user_id AND s.environment = p_environment
                  AND (s.adverse_at IS NOT NULL OR s.provider_status IN ('Approved', 'Declined', 'Kyc Expired')
                       OR EXISTS (SELECT 1 FROM public.kyc_events e WHERE e.session_id = s.session_id
                                  AND e.to_status IN ('Approved', 'Declined', 'Kyc Expired'))))
        $$
    """))
    op.execute(_sql("""
        CREATE FUNCTION public.kyc_donation_decision(p_streamer_id uuid, p_state text) RETURNS text
        LANGUAGE sql VOLATILE __DEFINER__ AS $$
            SELECT CASE
                WHEN s.id IS NULL THEN 'not_found'
                WHEN s.status <> 'active' THEN 'not_active'
                WHEN EXISTS (SELECT 1 FROM public.kyc_restrictions r WHERE r.streamer_id = s.id AND r.lifted_at IS NULL)
                    THEN 'restricted'
                -- Before the rollout: PR1's legacy baseline, unchanged.
                WHEN p_state = 'draft' AND s.kyc_track = 'legacy'
                     AND s.user_id IS NOT DISTINCT FROM s.kyc_baseline_owner_id THEN NULL
                WHEN NOT EXISTS (SELECT 1 FROM public.users u WHERE u.id = s.user_id AND u.is_active) THEN 'no_owner'
                WHEN public.user_kyc_approved(s.user_id, (SELECT c.environment FROM public.kyc_provider_config c))
                    THEN NULL
                -- Rollout grace: only for an owner who never had a Didit decision (a revoked, expired or failed
                -- verification never falls back to grace).
                WHEN p_state = 'active' AND s.kyc_track = 'legacy' AND s.user_id = s.kyc_baseline_owner_id
                     AND EXISTS (SELECT 1 FROM public.kyc_grace_grants g
                                 WHERE g.streamer_id = s.id AND g.owner_id = s.user_id AND g.revoked_at IS NULL
                                   AND clock_timestamp() < g.expires_at)
                     AND NOT public.kyc_owner_decided(s.user_id, (SELECT c.environment FROM public.kyc_provider_config c))
                    THEN NULL
                ELSE 'kyc_required'
            END
            FROM (SELECT 1) AS one LEFT JOIN public.streamers s ON s.id = p_streamer_id
        $$
    """))
    # VOLATILE (as in 0020): called from the donations trigger after its row lock, it must see the latest commit.
    op.execute(_sql("""
        CREATE OR REPLACE FUNCTION public.creator_donation_block(p_streamer_id uuid) RETURNS text
        LANGUAGE sql VOLATILE __DEFINER__ AS $$
            SELECT public.kyc_donation_decision(p_streamer_id, (SELECT state FROM public.kyc_enforcement))
        $$
    """))
    # Activation of a pending creator: always the current owner's own KYC (no baseline, no grace).
    op.execute(_sql("""
        CREATE FUNCTION public.kyc_activation_block(p_streamer_id uuid) RETURNS text
        LANGUAGE sql VOLATILE __DEFINER__ AS $$
            SELECT CASE
                WHEN s.id IS NULL THEN 'not_found'
                WHEN EXISTS (SELECT 1 FROM public.kyc_restrictions r WHERE r.streamer_id = s.id AND r.lifted_at IS NULL)
                    THEN 'restricted'
                WHEN NOT EXISTS (SELECT 1 FROM public.users u WHERE u.id = s.user_id AND u.is_active) THEN 'no_owner'
                WHEN NOT public.user_kyc_approved(s.user_id, (SELECT c.environment FROM public.kyc_provider_config c))
                    THEN 'kyc_required'
            END
            FROM (SELECT 1) AS one LEFT JOIN public.streamers s ON s.id = p_streamer_id
        $$
    """))

    # The application's database credentials cannot activate or reactivate a creator past the rules (the admin API
    # checks first; this catches any direct write). AFTER: the functions see the new row.
    op.execute("""
        CREATE FUNCTION public.streamers_guard_activation() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        DECLARE refusal text;
        BEGIN
            IF current_user IN ('hyperchat_app', 'hyperchat_worker') AND NEW.status = 'active'
               AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'active') THEN
                IF TG_OP = 'INSERT' OR OLD.status <> 'suspended' THEN
                    refusal := public.kyc_activation_block(NEW.id);
                END IF;
                refusal := COALESCE(refusal, public.creator_donation_block(NEW.id));
                IF refusal IS NOT NULL THEN
                    RAISE EXCEPTION 'creator activation refused: %', refusal
                        USING ERRCODE = 'insufficient_privilege', HINT = refusal;
                END IF;
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute("CREATE TRIGGER streamers_guard_activation AFTER INSERT OR UPDATE OF status ON public.streamers "
               "FOR EACH ROW EXECUTE FUNCTION public.streamers_guard_activation()")

    # ── audit + generation + hash chain (internal) ──
    op.execute(_sql("""
        CREATE FUNCTION public.kyc_enforcement_log(p_action text, p_streamer uuid, p_grant uuid, p_restriction uuid,
                                                   p_actor uuid, p_operator text, p_detail text,
                                                   p_min_generation bigint DEFAULT NULL)
        RETURNS bigint LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE e public.kyc_enforcement%ROWTYPE; g bigint; event_id uuid := gen_random_uuid();
                at timestamptz := clock_timestamp(); detail text := left(p_detail, 500); head text;
        BEGIN
            SELECT * INTO e FROM public.kyc_enforcement FOR UPDATE;  -- serializes every change (one chain)
            g := GREATEST(e.generation + 1, COALESCE(p_min_generation, 0));
            head := encode(sha256(convert_to(format('%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s', e.chain, g, event_id,
                    p_action, e.state, p_streamer, p_grant, p_restriction, p_actor, p_operator, detail, at), 'UTF8')),
                    'hex');
            UPDATE public.kyc_enforcement SET generation = g, chain = head;
            INSERT INTO public.kyc_enforcement_events (id, action, streamer_id, grant_id, restriction_id, actor_user_id,
                                                       operator, detail, generation, chain, created_at)
            VALUES (event_id, p_action, p_streamer, p_grant, p_restriction, p_actor, p_operator, detail, g, head, at);
            RETURN g;
        END $$
    """))

    # ── operator only (no runtime grant): activation, closure, grants, restore acknowledgement ──
    op.execute(_sql("""
        CREATE FUNCTION public.kyc_enforcement_activate(p_operator text, p_authorization text)
        RETURNS bigint LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE e public.kyc_enforcement%ROWTYPE;
        BEGIN
            SELECT * INTO e FROM public.kyc_enforcement FOR UPDATE;
            IF e.state <> 'draft' THEN
                RAISE EXCEPTION 'KYC enforcement is already %', e.state USING ERRCODE = 'check_violation';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM public.kyc_provider_config) THEN
                RAISE EXCEPTION 'Didit is not configured in this database' USING ERRCODE = 'check_violation';
            END IF;
            UPDATE public.kyc_enforcement SET state = 'active', policy = 'kyc-pr4-v1', activated_at = now(),
                activated_by = btrim(p_operator), activation_authorization = btrim(p_authorization),
                activation_migration = (SELECT version_num FROM public.alembic_version);
            RETURN public.kyc_enforcement_log('enforcement_activated', NULL, NULL, NULL, NULL, btrim(p_operator),
                                              btrim(p_authorization));
        END $$
    """))
    op.execute(_sql("""
        CREATE FUNCTION public.kyc_enforcement_close(p_operator text, p_authorization text)
        RETURNS bigint LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE e public.kyc_enforcement%ROWTYPE;
        BEGIN
            SELECT * INTO e FROM public.kyc_enforcement FOR UPDATE;
            IF e.state <> 'active' THEN
                RAISE EXCEPTION 'only active KYC enforcement can be closed (it is %)', e.state
                    USING ERRCODE = 'check_violation';
            END IF;
            UPDATE public.kyc_enforcement SET state = 'closed', closed_at = now(), closed_by = btrim(p_operator),
                closure_authorization = btrim(p_authorization);
            RETURN public.kyc_enforcement_log('enforcement_closed', NULL, NULL, NULL, NULL, btrim(p_operator),
                                              btrim(p_authorization));
        END $$
    """))
    op.execute(_sql("""
        CREATE FUNCTION public.kyc_grant_grace(p_streamer_id uuid, p_days integer, p_operator text, p_reason text)
        RETURNS uuid LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE s public.streamers%ROWTYPE; e public.kyc_enforcement%ROWTYPE; grant_id uuid;
        BEGIN
            SELECT * INTO s FROM public.streamers WHERE id = p_streamer_id FOR NO KEY UPDATE;
            SELECT * INTO e FROM public.kyc_enforcement;
            IF e.state = 'closed' THEN
                RAISE EXCEPTION 'KYC enforcement is closed: no more grace' USING ERRCODE = 'check_violation';
            END IF;
            IF p_days IS NULL OR p_days < 1 OR p_days > e.grace_max_days THEN
                RAISE EXCEPTION 'grace is 1 to % days', e.grace_max_days USING ERRCODE = 'check_violation';
            END IF;
            IF s.id IS NULL OR s.status <> 'active' OR s.kyc_track <> 'legacy' OR s.user_id IS NULL
               OR s.user_id IS DISTINCT FROM s.kyc_baseline_owner_id
               OR EXISTS (SELECT 1 FROM public.kyc_restrictions r WHERE r.streamer_id = s.id AND r.lifted_at IS NULL)
               OR public.kyc_owner_decided(s.user_id, (SELECT c.environment FROM public.kyc_provider_config c))
            THEN
                RAISE EXCEPTION 'grace is only for an active, unrestricted legacy creator still owned by its baseline '
                                'owner, who never had a Didit decision' USING ERRCODE = 'check_violation';
            END IF;
            INSERT INTO public.kyc_grace_grants (streamer_id, owner_id, expires_at, granted_by, reason)
            VALUES (s.id, s.user_id, now() + make_interval(days => p_days), btrim(p_operator), btrim(p_reason))
            RETURNING id INTO grant_id;  -- a second grant for the same owner violates kyc_grace_grants_once
            PERFORM public.kyc_enforcement_log('grace_granted', s.id, grant_id, NULL, NULL, btrim(p_operator),
                                               p_days || ' days: ' || btrim(p_reason));
            RETURN grant_id;
        END $$
    """))
    op.execute(_sql("""
        CREATE FUNCTION public.kyc_acknowledge_restore(p_operator text, p_generation bigint, p_detail text)
        RETURNS bigint LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE g bigint;
        BEGIN
            SELECT generation INTO g FROM public.kyc_enforcement FOR UPDATE;
            IF p_generation IS NULL OR p_generation <= g OR NULLIF(btrim(p_detail), '') IS NULL THEN
                RAISE EXCEPTION 'an acknowledgement moves the generation forward (it is %) and says why', g
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN public.kyc_enforcement_log('restore_acknowledged', NULL, NULL, NULL, NULL, btrim(p_operator),
                                              btrim(p_detail), p_generation);
        END $$
    """))

    # ── the API's role: only fail-closed changes and reads ──
    op.execute(_sql("""
        CREATE FUNCTION public.kyc_revoke_grace(p_streamer_id uuid, p_reason text, p_actor uuid)
        RETURNS integer LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE r record; n integer := 0;
        BEGIN
            FOR r IN UPDATE public.kyc_grace_grants SET revoked_at = now(), revoked_by = p_actor,
                         revoke_reason = left(btrim(p_reason), 500)
                     WHERE streamer_id = p_streamer_id AND revoked_at IS NULL AND expires_at > now()
                     RETURNING id LOOP
                PERFORM public.kyc_enforcement_log('grace_revoked', p_streamer_id, r.id, NULL, p_actor, NULL, p_reason);
                n := n + 1;
            END LOOP;
            RETURN n;
        END $$
    """))
    op.execute(_sql("""
        CREATE FUNCTION public.kyc_restrict_creator(p_streamer_id uuid, p_reason text, p_note text, p_actor uuid)
        RETURNS uuid LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE restriction uuid;
        BEGIN
            PERFORM 1 FROM public.streamers WHERE id = p_streamer_id FOR NO KEY UPDATE;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;
            INSERT INTO public.kyc_restrictions (streamer_id, reason, note, created_by)
            VALUES (p_streamer_id, p_reason, NULLIF(btrim(p_note), ''), p_actor) RETURNING id INTO restriction;
            PERFORM public.kyc_enforcement_log('restriction_added', p_streamer_id, NULL, restriction, p_actor, NULL,
                                               p_reason);
            PERFORM public.kyc_revoke_grace(p_streamer_id, 'restricted: ' || p_reason, p_actor);
            RETURN restriction;
        END $$
    """))
    op.execute(_sql("""
        CREATE FUNCTION public.kyc_lift_restriction(p_streamer_id uuid, p_restriction_id uuid, p_note text,
                                                    p_operator text)
        RETURNS text LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE r public.kyc_restrictions%ROWTYPE;
        BEGIN
            IF NULLIF(btrim(p_note), '') IS NULL THEN
                RAISE EXCEPTION 'lifting a restriction needs a note' USING ERRCODE = 'check_violation';
            END IF;
            PERFORM 1 FROM public.streamers WHERE id = p_streamer_id FOR NO KEY UPDATE;  -- creator first, as always
            SELECT * INTO r FROM public.kyc_restrictions WHERE id = p_restriction_id AND streamer_id = p_streamer_id
                FOR UPDATE;
            IF NOT FOUND THEN
                RETURN 'not_found';
            END IF;
            IF r.lifted_at IS NOT NULL THEN
                RETURN 'already_lifted';
            END IF;
            IF NULLIF(btrim(p_operator), '') IS NULL THEN
                RAISE EXCEPTION 'the operator is required' USING ERRCODE = 'check_violation';
            END IF;
            UPDATE public.kyc_restrictions SET lifted_at = now(), lift_note = btrim(p_note) WHERE id = r.id;
            PERFORM public.kyc_enforcement_log('restriction_lifted', p_streamer_id, NULL, r.id, NULL, btrim(p_operator),
                                               btrim(p_note));
            RETURN 'ok';
        END $$
    """))
    # Daily (app.maintenance): one 'grace_expired' audit row per grant that ran out (expiry itself is the clock).
    op.execute(_sql("""
        CREATE FUNCTION public.kyc_record_grace_expiries() RETURNS integer LANGUAGE plpgsql __DEFINER__ AS $$
        DECLARE n integer;
        BEGIN
            INSERT INTO public.kyc_enforcement_events (action, streamer_id, grant_id)
            SELECT 'grace_expired', g.streamer_id, g.id FROM public.kyc_grace_grants g
            WHERE g.revoked_at IS NULL AND g.expires_at <= now()
            ON CONFLICT (grant_id) WHERE action = 'grace_expired' DO NOTHING;
            GET DIAGNOSTICS n = ROW_COUNT;
            RETURN n;
        END $$
    """))
    # Read-only impact preview: what each creator's rule says now, once active, and once closed.
    op.execute(_sql("""
        CREATE FUNCTION public.kyc_enforcement_preview()
        RETURNS TABLE (streamer_id uuid, streamer_slug text, status text, kyc_track text, owner_id uuid,
                       owner_is_baseline boolean, owner_active boolean, owner_kyc_approved boolean,
                       open_restrictions bigint, grace_expires_at timestamptz, block_now text,
                       block_when_active text, block_when_closed text)
        LANGUAGE sql VOLATILE __DEFINER__ AS $$
            SELECT s.id, s.streamer_slug, s.status, s.kyc_track, s.user_id,
                   s.user_id IS NOT NULL AND s.user_id = s.kyc_baseline_owner_id,
                   COALESCE(u.is_active, false),
                   s.user_id IS NOT NULL AND public.user_kyc_approved(s.user_id,
                       (SELECT c.environment FROM public.kyc_provider_config c)) IS TRUE,
                   (SELECT count(*) FROM public.kyc_restrictions r WHERE r.streamer_id = s.id AND r.lifted_at IS NULL),
                   (SELECT max(g.expires_at) FROM public.kyc_grace_grants g WHERE g.streamer_id = s.id
                       AND g.owner_id = s.user_id AND g.revoked_at IS NULL AND g.expires_at > clock_timestamp()),
                   public.creator_donation_block(s.id),
                   public.kyc_donation_decision(s.id, 'active'),
                   public.kyc_donation_decision(s.id, 'closed')
            FROM public.streamers s LEFT JOIN public.users u ON u.id = s.user_id
            ORDER BY s.streamer_slug
        $$
    """))

    for table in NEW_TABLES:
        op.execute(f"REVOKE ALL ON public.{table} FROM hyperchat_app, hyperchat_worker")
        op.execute(f"GRANT SELECT ON public.{table} TO hyperchat_app")
    op.execute(f"REVOKE ALL ON FUNCTION {', '.join(RUNTIME_FUNCTIONS + OPERATOR_FUNCTIONS + INTERNAL_FUNCTIONS)} "
               "FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {', '.join(RUNTIME_FUNCTIONS)} TO hyperchat_app")


def downgrade() -> None:
    op.execute("""
        DO $$
        DECLARE e public.kyc_enforcement%ROWTYPE; rows bigint;
        BEGIN
            SELECT * INTO e FROM public.kyc_enforcement;
            SELECT (SELECT count(*) FROM public.kyc_grace_grants) + (SELECT count(*) FROM public.kyc_restrictions)
                   + (SELECT count(*) FROM public.kyc_enforcement_events) INTO rows;
            IF e.state <> 'draft' OR e.generation > 0 OR rows > 0 THEN
                RAISE EXCEPTION 'downgrade of 0023 stopped: KYC enforcement is % (generation %), % enforcement row(s) '
                                'would be lost (nothing was changed)', e.state, e.generation, rows;
            END IF;
        END $$
    """)
    op.execute(PR1_RULE)
    op.execute("""
        ALTER TABLE public.creator_status_events
            DROP CONSTRAINT creator_status_events_streamer_id_fkey,
            ADD CONSTRAINT creator_status_events_streamer_id_fkey FOREIGN KEY (streamer_id)
                REFERENCES public.streamers(id) ON DELETE CASCADE
    """)
    op.execute("DROP TRIGGER streamers_guard_activation ON public.streamers")
    for table in ("kyc_enforcement_events", "kyc_restrictions", "kyc_grace_grants", "kyc_enforcement"):
        op.execute(f"DROP TABLE public.{table}")
    for function in RUNTIME_FUNCTIONS + OPERATOR_FUNCTIONS + INTERNAL_FUNCTIONS:
        op.execute(f"DROP FUNCTION {function}")
