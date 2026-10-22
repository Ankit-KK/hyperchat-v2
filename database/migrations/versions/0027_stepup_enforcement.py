"""Database-enforced administrator step-up (admin MFA, S3): the sensitive writes themselves claim their passkey grant.

Revision ID: 0027
Revises: 0026

Every sensitive administrator write is guarded in PostgreSQL, whatever code issues it (docs/admin-stepup.md, "S3").
A guard fires when the LOGIN role (session_user) is one the API connects as (hyperchat_app / _worker / _auth /
_lifecycle / _stepup); keying on session_user rather than current_user means a SECURITY DEFINER function called by an
API role (e.g. kyc_change_creator_status) is guarded too, while the operator (who logs in as the migrator and acts as
hyperchat_owner), migrations and the legacy import stay outside, as the explicitly trusted boundary.

One grant -> one logical action. The ANCHOR write of an action calls stepup_authorize(action, target, params, anchor):
  * the target and the parameters are derived from the row being written (never from the caller), and the parameter
    hash is computed here (stepup_params_hash: the same canonical JSON as app.stepup.canonical, byte for byte);
  * it claims exactly one grant consumed IN THIS TRANSACTION (stepup_consume bound it to the API's authenticated
    administrator and session) for exactly this action, target and hash (stepup_claim: once, never later);
  * no such grant: refused unless enforcement is exactly 'draft'; refused in 'draft' too when the transaction consumed
    ANY grant, claimed or not (a presented grant authorizes only its own write, never a second one through the legacy
    path); allowed in 'draft' only for a transaction that presented no grant at all (the legacy path, so
    administrators are not locked out before enrolling).
A legitimate secondary EFFECT of the same action calls stepup_effect_or_legacy(action, target, params, effect): it
needs the grant that action claimed in this transaction for exactly these parameters and records the named effect
once per grant (stepup_effect); its legacy path has the same rule.

Anchors (S2 action, target, parameters derived from the write):
  streamers.status -> active from pending      creator.activate     creator:<id>     {expected_status}
  streamers.status -> active from suspended    creator.reactivate   creator:<id>     {expected_status}
  streamers.user_id NULL -> u (assign)         creator.owner.assign creator:<id>     {user_id}
  streamers.user_id u -> v (transfer)          creator.owner.transfer creator:<id>   {expected_owner_id, user_id}
  INSERT streamers (not from an approval)      creator.create       creator:new      {name, razorpay_account, slug}
  user_signups.review_status -> approved       application.approve  application:<id> {name, razorpay_account, slug}
                                                                                     (of the creator it references)
  INSERT user_invites with signup_id           application.invite   application:<signup id> {}
  INSERT user_invites role admin / streamer_id admin.invite         invite           {email, role, streamer_id}
  users.role / is_active / streamer_id         admin.user.change    user:<id>        {is_active, role, streamer_id}
                                                                                     (the resulting values)
  users.email                                  admin.user.email     user:<id>        {email}
  INSERT partner_api_keys                      partner_key.create   partner_key      {partner_slug}
Effects:
  INSERT streamers referenced by an approval    effect 'creator_created' of application.approve (checked at commit:
                                                the approval is written after the creator it references)
  users.streamer_id u -> NULL of the old owner  effect 'old_owner_unlinked' of creator.owner.transfer
Internal paths (no administrator action; authorized otherwise):
  invite acceptance (hyperchat_auth): the user row an invitation accepted IN THIS TRANSACTION creates or upgrades, and
    the unclaimed creator an application invitation accepted in this transaction claims: the invitation's own issue
    was a guarded anchor;
  bootstrap (hyperchat_auth): an admin row while no active administrator can sign in;
  everything that only removes or revokes (suspension, revocations, deactivation-side burns, cleanup deletes).
Also closed for every API role, hyperchat_auth included (0024 covered only the runtime roles): removing a creator's
owner (cascades included) or changing its creation time; changing an approved application's status, address or
creator; changing an invitation in any way but accepting it (now, once, while open) or revoking it; and inserting a
user with role admin or a creator link outside the two internal paths above.

Downgrade drops the guards and the helpers; grants and claims already recorded stay.
"""

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

API_ROLES = "('hyperchat_app', 'hyperchat_worker', 'hyperchat_auth', 'hyperchat_lifecycle', 'hyperchat_stepup')"
DEFINER = "SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp"
REFUSE = "USING ERRCODE = 'insufficient_privilege'"
HELPERS = ("public.stepup_canonical_json(jsonb)", "public.stepup_params_hash(jsonb)",
           "public.stepup_authorize(text, text, jsonb, text)", "public.stepup_effect_or_legacy(text, text, jsonb, text)")
GUARDS = (  # (trigger, table, timing, function)
    ("aac_stepup_guard_streamers", "streamers", "BEFORE UPDATE", "stepup_guard_streamers"),
    ("aac_stepup_guard_user_signups", "user_signups", "BEFORE INSERT OR UPDATE", "stepup_guard_user_signups"),
    ("aac_stepup_guard_user_invites", "user_invites", "BEFORE INSERT OR UPDATE", "stepup_guard_user_invites"),
    ("aac_stepup_guard_users", "users", "BEFORE INSERT OR UPDATE", "stepup_guard_users"),
    ("aac_stepup_guard_partner_keys", "partner_api_keys", "BEFORE INSERT OR UPDATE", "stepup_guard_partner_keys"),
)


def upgrade() -> None:
    # ── canonical parameters, as app.stepup.canonical writes them (ASCII keys sorted, no whitespace, JSON escaping of
    #    '"', '\\' and control characters, non-ASCII as UTF-8, integers and literals as JSON) ─────────────────────
    op.execute("""
        CREATE FUNCTION public.stepup_canonical_json(v jsonb) RETURNS text LANGUAGE plpgsql IMMUTABLE STRICT
        SET search_path = pg_catalog, public AS $$
        DECLARE kind text := jsonb_typeof(v); out text; k text; e jsonb;
        BEGIN
            IF kind = 'object' THEN
                out := '';
                FOR k IN SELECT key FROM jsonb_object_keys(v) AS key ORDER BY key COLLATE "C" LOOP
                    out := out || CASE WHEN out = '' THEN '' ELSE ',' END || to_json(k)::text || ':'
                               || public.stepup_canonical_json(v -> k);
                END LOOP;
                RETURN '{' || out || '}';
            ELSIF kind = 'array' THEN
                out := '';
                FOR e IN SELECT value FROM jsonb_array_elements(v) WITH ORDINALITY ORDER BY ordinality LOOP
                    out := out || CASE WHEN out = '' THEN '' ELSE ',' END || public.stepup_canonical_json(e);
                END LOOP;
                RETURN '[' || out || ']';
            ELSIF kind = 'string' THEN
                RETURN to_json(v #>> '{}')::text;
            ELSE
                RETURN v::text;  -- integers, true, false, null
            END IF;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.stepup_params_hash(p jsonb) RETURNS text LANGUAGE sql IMMUTABLE STRICT
        SET search_path = pg_catalog, public AS $$
            SELECT encode(public.digest(convert_to(public.stepup_canonical_json(p), 'UTF8'), 'sha256'), 'hex')
        $$
    """)

    # ── the anchor and effect rules ──────────────────────────────────────────────────────────────────────────
    op.execute(f"""
        CREATE FUNCTION public.stepup_authorize(p_action text, p_target text, p_params jsonb, p_anchor text)
        RETURNS uuid LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE h text := public.stepup_params_hash(p_params); g public.stepup_grants;
        BEGIN
            SELECT * INTO g FROM public.stepup_grants
            WHERE consumed_txid = pg_current_xact_id_if_assigned() AND claimed_at IS NULL
              AND action = p_action AND target = p_target AND params_hash = h
            ORDER BY consumed_at, id LIMIT 1;
            IF FOUND THEN  -- claims exactly this grant (row-locked and re-checked by stepup_claim)
                RETURN public.stepup_claim(g.user_id, g.session_id, p_action, p_target, h, p_anchor);
            END IF;
            IF (SELECT state FROM public.stepup_enforcement) IS DISTINCT FROM 'draft' THEN  -- unknown = enforced
                RAISE EXCEPTION 'step-up required: % on % needs a passkey grant for exactly these parameters',
                    p_action, p_target {REFUSE};
            END IF;
            -- Draft's legacy path is only for a transaction that presented NO grant at all. Once any grant was
            -- consumed here (claimed by its own anchor or not), every further sensitive write needs its own grant:
            -- one grant, one action, in draft too. (A consumption rolled back with its savepoint left no trace and
            -- presented nothing.)
            IF EXISTS (SELECT 1 FROM public.stepup_grants WHERE consumed_txid = pg_current_xact_id_if_assigned()) THEN
                RAISE EXCEPTION 'this transaction presented a step-up grant: % on % needs its own', p_action, p_target
                    {REFUSE};
            END IF;
            RETURN NULL;  -- draft, no grant presented: the legacy path
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.stepup_effect_or_legacy(p_action text, p_target text, p_params jsonb, p_effect text)
        RETURNS uuid LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE h text := public.stepup_params_hash(p_params); g public.stepup_grants;
        BEGIN
            SELECT * INTO g FROM public.stepup_grants gr  -- the claimed grant of exactly this action
            WHERE consumed_txid = pg_current_xact_id_if_assigned() AND claimed_at IS NOT NULL
              AND action = p_action AND target = p_target AND params_hash = h
              AND NOT EXISTS (SELECT 1 FROM public.stepup_grant_effects e WHERE e.grant_id = gr.id
                              AND e.effect = p_effect)
            ORDER BY claimed_at, id LIMIT 1;
            IF FOUND THEN
                RETURN public.stepup_effect(g.user_id, g.session_id, p_action, p_target, h, p_effect);
            END IF;
            IF (SELECT state FROM public.stepup_enforcement) IS DISTINCT FROM 'draft' OR EXISTS (
                    SELECT 1 FROM public.stepup_grants WHERE consumed_txid = pg_current_xact_id_if_assigned()) THEN
                RAISE EXCEPTION 'effect % needs the step-up grant claimed by its % on %, once', p_effect, p_action,
                    p_target {REFUSE};
            END IF;
            RETURN NULL;  -- draft, no grant in this transaction: the legacy path
        END $$
    """)

    # ── guards ─────────────────────────────────────────────────────────────────────────────────────────────
    op.execute(f"""
        CREATE FUNCTION public.stepup_guard_streamers() RETURNS trigger LANGUAGE plpgsql {DEFINER} AS $$
        BEGIN
            IF session_user NOT IN {API_ROLES} THEN
                RETURN NEW;
            END IF;
            IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN  -- an approval relies on it (created now)
                RAISE EXCEPTION 'a creator''s creation time never changes' {REFUSE};
            END IF;
            IF NEW.status = 'active' AND OLD.status IN ('pending', 'suspended') THEN
                PERFORM public.stepup_authorize(
                    CASE OLD.status WHEN 'pending' THEN 'creator.activate' ELSE 'creator.reactivate' END,
                    'creator:' || NEW.id, jsonb_build_object('expected_status', OLD.status), 'streamers.status');
            END IF;
            IF NEW.user_id IS DISTINCT FROM OLD.user_id THEN
                IF NEW.user_id IS NULL THEN
                    RAISE EXCEPTION 'a creator''s owner is never removed by the API' {REFUSE};
                ELSIF OLD.user_id IS NULL THEN
                    IF NOT (session_user = 'hyperchat_auth' AND EXISTS (  -- an application invitation accepted now
                            SELECT 1 FROM public.user_invites i WHERE i.signup_id IS NOT NULL AND i.streamer_id = NEW.id
                              AND i.accepted_at = now() AND i.accepted_user_id = NEW.user_id)) THEN
                        PERFORM public.stepup_authorize('creator.owner.assign', 'creator:' || NEW.id,
                                                        jsonb_build_object('user_id', NEW.user_id), 'streamers.user_id');
                    END IF;
                ELSE
                    PERFORM public.stepup_authorize('creator.owner.transfer', 'creator:' || NEW.id,
                        jsonb_build_object('user_id', NEW.user_id, 'expected_owner_id', OLD.user_id), 'streamers.user_id');
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    # A creator INSERT is checked at commit: an approval writes the creator first and the approval (its anchor) after.
    op.execute(f"""
        CREATE FUNCTION public.stepup_guard_streamer_insert() RETURNS trigger LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE signup uuid; s public.streamers;
        BEGIN
            IF session_user NOT IN {API_ROLES} THEN
                RETURN NULL;
            END IF;
            SELECT * INTO s FROM public.streamers WHERE id = NEW.id;
            IF NOT FOUND THEN
                RETURN NULL;  -- deleted again in this transaction
            END IF;
            SELECT a.id INTO signup FROM public.user_signups a WHERE a.streamer_id = s.id AND a.review_status = 'approved';
            IF signup IS NOT NULL THEN
                PERFORM public.stepup_effect_or_legacy('application.approve', 'application:' || signup,
                    jsonb_build_object('slug', s.streamer_slug, 'name', s.streamer_name,
                                       'razorpay_account', s.razorpay_account), 'creator_created');
            ELSE
                PERFORM public.stepup_authorize('creator.create', 'creator:new',
                    jsonb_build_object('slug', s.streamer_slug, 'name', s.streamer_name,
                                       'razorpay_account', s.razorpay_account), 'streamers.insert');
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.stepup_guard_user_signups() RETURNS trigger LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE s public.streamers;
        BEGIN
            IF session_user NOT IN {API_ROLES} THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'INSERT' THEN  -- an application is approved only by the guarded update below
                IF NEW.review_status = 'approved' OR NEW.streamer_id IS NOT NULL OR NEW.invite_id IS NOT NULL THEN
                    RAISE EXCEPTION 'an application starts unreviewed' {REFUSE};
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.review_status = 'approved' THEN  -- final: its creator and address are what invitations rely on
                IF (NEW.review_status, NEW.streamer_id, NEW.email) IS DISTINCT FROM
                   (OLD.review_status, OLD.streamer_id, OLD.email) THEN
                    RAISE EXCEPTION 'an approved application is final' {REFUSE};
                END IF;
            ELSIF NEW.review_status = 'approved' AND NEW.streamer_id IS NOT NULL THEN  -- (without: CHECK refuses)
                SELECT * INTO s FROM public.streamers WHERE id = NEW.streamer_id;
                IF NOT FOUND OR s.created_at <> now() OR s.user_id IS NOT NULL THEN
                    RAISE EXCEPTION 'an approval creates its own new creator, in the same transaction' {REFUSE};
                END IF;
                PERFORM public.stepup_authorize('application.approve', 'application:' || NEW.id,
                    jsonb_build_object('slug', s.streamer_slug, 'name', s.streamer_name,
                                       'razorpay_account', s.razorpay_account), 'user_signups.review_status');
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.stepup_guard_user_invites() RETURNS trigger LANGUAGE plpgsql {DEFINER} AS $$
        BEGIN
            IF session_user NOT IN {API_ROLES} THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE' THEN  -- an invitation is only ever accepted or revoked, once (every API role)
                IF (to_jsonb(NEW) - 'accepted_at' - 'accepted_user_id' - 'revoked_at')
                       <> (to_jsonb(OLD) - 'accepted_at' - 'accepted_user_id' - 'revoked_at')
                   OR (OLD.accepted_at IS NOT NULL AND NEW.accepted_at IS DISTINCT FROM OLD.accepted_at)
                   OR (OLD.accepted_user_id IS NOT NULL AND NEW.accepted_user_id IS DISTINCT FROM OLD.accepted_user_id)
                   OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)
                   OR (NEW.accepted_at IS DISTINCT FROM OLD.accepted_at  -- accepted now, only while it is open
                       AND (NEW.accepted_at <> now() OR OLD.revoked_at IS NOT NULL OR OLD.expires_at <= now()))
                   OR (NEW.revoked_at IS DISTINCT FROM OLD.revoked_at AND NEW.revoked_at <> now())
                   OR (NEW.accepted_user_id IS DISTINCT FROM OLD.accepted_user_id AND NEW.accepted_at IS NULL) THEN
                    RAISE EXCEPTION 'an invitation is only accepted (while open) or revoked, once' {REFUSE};
                END IF;
                -- Accepting needs the invitation's own token (AuthRepository.accept_invite presents it for this
                -- transaction only): the acceptance is what the internal exceptions below trust, so no role can
                -- fabricate one for an invitation it merely sees.
                IF OLD.accepted_at IS NULL AND NEW.accepted_at IS NOT NULL AND (session_user <> 'hyperchat_auth'
                       OR encode(public.digest(coalesce(current_setting('hyperchat.invite_token', true), ''), 'sha256'),
                                 'hex') IS DISTINCT FROM OLD.token_hash) THEN
                    RAISE EXCEPTION 'an invitation is accepted only with its token' {REFUSE};
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.accepted_at IS NOT NULL OR NEW.accepted_user_id IS NOT NULL OR NEW.revoked_at IS NOT NULL THEN
                RAISE EXCEPTION 'an invitation starts open' {REFUSE};
            END IF;
            IF NEW.signup_id IS NOT NULL THEN  -- exactly the approved application's address and creator
                IF NEW.role <> 'streamer' OR NOT EXISTS (
                        SELECT 1 FROM public.user_signups a WHERE a.id = NEW.signup_id AND a.review_status = 'approved'
                          AND a.streamer_id = NEW.streamer_id AND lower(a.email::text) = lower(NEW.email::text)) THEN
                    RAISE EXCEPTION 'an application invitation is for its approved application''s creator and address'
                        {REFUSE};
                END IF;
                PERFORM public.stepup_authorize('application.invite', 'application:' || NEW.signup_id, '{{}}'::jsonb,
                                                'user_invites.insert');
            ELSIF NEW.role = 'admin' OR NEW.streamer_id IS NOT NULL THEN
                PERFORM public.stepup_authorize('admin.invite', 'invite',
                    jsonb_build_object('email', lower(NEW.email::text), 'role', NEW.role, 'streamer_id', NEW.streamer_id),
                    'user_invites.insert');
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.stepup_guard_users() RETURNS trigger LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE invited boolean;
        BEGIN
            IF session_user NOT IN {API_ROLES} THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.role = 'admin' OR NEW.streamer_id IS NOT NULL THEN
                    invited := EXISTS (SELECT 1 FROM public.user_invites i WHERE i.email = NEW.email
                                       AND i.accepted_at = now() AND i.role = NEW.role
                                       AND i.streamer_id IS NOT DISTINCT FROM NEW.streamer_id);
                    IF NOT invited AND NOT (session_user = 'hyperchat_auth' AND NEW.role = 'admin'
                            AND NEW.streamer_id IS NULL AND NOT EXISTS (  -- bootstrap: no admin can sign in
                                SELECT 1 FROM public.users u WHERE u.role = 'admin' AND u.is_active
                                  AND (u.password_hash IS NOT NULL OR EXISTS (
                                       SELECT 1 FROM public.user_identities x WHERE x.user_id = u.id)))) THEN
                        RAISE EXCEPTION 'an admin or creator-linked account is created only by an invitation' {REFUSE};
                    END IF;
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.email::text IS DISTINCT FROM OLD.email::text THEN
                PERFORM public.stepup_authorize('admin.user.email', 'user:' || NEW.id,
                                                jsonb_build_object('email', lower(NEW.email::text)), 'users.email');
            END IF;
            IF (NEW.role, NEW.is_active, NEW.streamer_id) IS DISTINCT FROM (OLD.role, OLD.is_active, OLD.streamer_id) THEN
                IF session_user = 'hyperchat_auth' AND NEW.is_active = OLD.is_active AND NEW.role <> 'admin' AND EXISTS (
                        SELECT 1 FROM public.user_invites i WHERE i.email = NEW.email AND i.accepted_at = now()
                          AND i.role = NEW.role AND NEW.streamer_id IS NOT DISTINCT FROM COALESCE(i.streamer_id,
                                                                                                    OLD.streamer_id)) THEN
                    RETURN NEW;  -- an invitation accepted in this transaction (its issue was the guarded anchor)
                END IF;
                IF NEW.streamer_id IS NULL AND OLD.streamer_id IS NOT NULL AND (NEW.role, NEW.is_active)
                       IS NOT DISTINCT FROM (OLD.role, OLD.is_active) AND EXISTS (  -- THE previous owner of a transfer
                        SELECT 1 FROM public.stepup_grants g JOIN public.streamers s ON s.id = OLD.streamer_id
                        WHERE g.consumed_txid = pg_current_xact_id_if_assigned() AND g.claimed_at IS NOT NULL
                          AND g.action = 'creator.owner.transfer' AND g.target = 'creator:' || OLD.streamer_id
                          AND g.params_hash = public.stepup_params_hash(
                                jsonb_build_object('user_id', s.user_id, 'expected_owner_id', NEW.id))) THEN
                    PERFORM public.stepup_effect_or_legacy('creator.owner.transfer', 'creator:' || OLD.streamer_id,
                        jsonb_build_object('user_id', (SELECT user_id FROM public.streamers WHERE id = OLD.streamer_id),
                                           'expected_owner_id', NEW.id), 'old_owner_unlinked');
                    RETURN NEW;
                END IF;
                PERFORM public.stepup_authorize('admin.user.change', 'user:' || NEW.id,
                    jsonb_build_object('role', NEW.role, 'is_active', NEW.is_active, 'streamer_id', NEW.streamer_id),
                    'users.privileges');
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.stepup_guard_partner_keys() RETURNS trigger LANGUAGE plpgsql {DEFINER} AS $$
        BEGIN
            IF session_user NOT IN {API_ROLES} THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE' THEN  -- a key is only used or revoked; a new key or partner is a new grant's job
                IF (to_jsonb(NEW) - 'revoked' - 'last_used_at' - 'updated_at')
                       <> (to_jsonb(OLD) - 'revoked' - 'last_used_at' - 'updated_at')
                   OR (OLD.revoked AND NOT NEW.revoked) THEN
                    RAISE EXCEPTION 'a partner key is only used or revoked' {REFUSE};
                END IF;
                RETURN NEW;
            END IF;
            PERFORM public.stepup_authorize('partner_key.create', 'partner_key',
                                            jsonb_build_object('partner_slug', NEW.partner_slug), 'partner_api_keys');
            RETURN NEW;
        END $$
    """)
    for trigger, table, timing, fn in GUARDS:
        op.execute(f"CREATE TRIGGER {trigger} {timing} ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.{fn}()")
    op.execute("CREATE CONSTRAINT TRIGGER aac_stepup_guard_streamer_insert AFTER INSERT ON public.streamers "
               "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.stepup_guard_streamer_insert()")

    everyone = "hyperchat_app, hyperchat_worker, hyperchat_auth, hyperchat_lifecycle, hyperchat_readonly, " \
               "hyperchat_stepup, PUBLIC"
    for fn in HELPERS:
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM {everyone}")
    for fn in [g[3] for g in GUARDS] + ["stepup_guard_streamer_insert"]:
        op.execute(f"REVOKE ALL ON FUNCTION public.{fn}() FROM {everyone}")


def downgrade() -> None:
    op.execute("DROP TRIGGER aac_stepup_guard_streamer_insert ON public.streamers")
    for trigger, table, _, fn in GUARDS:
        op.execute(f"DROP TRIGGER {trigger} ON public.{table}")
        op.execute(f"DROP FUNCTION public.{fn}()")
    op.execute("DROP FUNCTION public.stepup_guard_streamer_insert()")
    for fn in reversed(HELPERS):
        op.execute(f"DROP FUNCTION {fn}")
