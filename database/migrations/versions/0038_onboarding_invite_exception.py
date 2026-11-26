"""Onboarding Stage 2 Phase B (PDP v1.1, owner decisions Q3, Q4, Q13, Q14): an application invitation without a
passkey grant, for an application an administrator already approved with one, in every step-up state.

Revision ID: 0038
Revises: 0037

0027's stepup_guard_user_invites anchors application.invite: with enforcement 'active' an application invitation
from a transaction that presented no grant is refused. Q3 decided that a resend needs no passkey (the approval,
step-up-signed since NV-3 over the applicant's lower-cased address, already fixed the creator and the address), and
the automatic invitation (app.onboarding.auto_invite) has no administrator at all. This migration adds:

  * public.onboarding_issue_invite(signup, token_hash, ttl_days, actor, automatic): SECURITY DEFINER, EXECUTE for
    hyperchat_lifecycle ONLY (the role has no table access of its own, so it can write user_invites only through a
    definer function). It locks the application, then its open invitations, then its creator (the order of
    ApplicationRepository._issue and accept_invite), and refuses unless the application is approved, its creator is
    unclaimed and no account has its address. It takes the address and the creator from the locked application,
    never from the caller, caps the expiry at 7 days, revokes the open links, inserts the invitation, points the
    application at it and writes the 'invite' event (note 'auto' when automatic). Automatic calls (actor NULL) are
    also refused while an earlier invitation of the application is still unrevoked and unaccepted (open, or expired
    after delivery: Q4, no automatic resend after the expiry), after a manual invitation, and after 5 automatic ones
    (Q4). A manual call names an active administrator (consistency; the role is the authority). A transaction that
    consumed a step-up grant is refused (one grant, one action): the granted path stays ApplicationRepository._issue.
  * stepup_guard_user_invites, re-created from 0027's text with exactly one change (rendered and substituted below,
    asserted to match once): an application invitation needs no grant when, and only when, session_user is
    hyperchat_lifecycle, the row's token hash is the one onboarding_issue_invite announced for this transaction
    (hyperchat.onboarding_invite), the address is exactly the approved application's, and the transaction consumed no
    grant. Everything else is 0027's: the approved application's own creator and address (lower-cased), role
    'streamer', and stepup_authorize for any other caller (a presented grant is claimed; draft's legacy path; 'active'
    refuses).

What this does NOT open: hyperchat_app (and every other API role) still cannot insert an application invitation
without a grant under 'active', whatever it sets; a known token for an approved application now needs the lifecycle
role, i.e. the API or worker process itself (Q13, accepted with the attempt cap and the alerts).

Downgrade drops the function and re-creates 0027's guard exactly; invitations already issued stay (they are ordinary
rows the 0027 guard accepts). No row is read or written by either direction.
"""

import importlib.util
import pathlib
import types

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

DEFINER = "SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp"
FUNCTION = "public.onboarding_issue_invite(uuid, text, integer, uuid, boolean)"
MAX_AUTO_ATTEMPTS = 5  # owner decision Q4 (app.onboarding.auto_invite.MAX_ATTEMPTS)
MAX_TTL_DAYS = 7  # owner decision Q4: the 7-day expiry (Settings.invite_ttl_days defaults to it)
EVERYONE = ("hyperchat_app, hyperchat_worker, hyperchat_auth, hyperchat_lifecycle, hyperchat_readonly, "
            "hyperchat_stepup, PUBLIC")


def upgrade() -> None:
    op.execute(f"""
        CREATE FUNCTION public.onboarding_issue_invite(p_signup_id uuid, p_token_hash text, p_ttl_days integer,
                                                       p_actor uuid, p_automatic boolean,
                                                       OUT o_result text, OUT o_invite_id uuid, OUT o_email text,
                                                       OUT o_expires_at timestamptz, OUT o_replaced integer)
        LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE a public.user_signups%ROWTYPE; owner_id uuid;
        BEGIN
            o_replaced := 0;
            IF p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{{64}}$' OR p_automatic IS NULL THEN
                o_result := 'bad_request'; RETURN;
            END IF;
            IF p_automatic AND p_actor IS NOT NULL OR NOT p_automatic AND NOT EXISTS (
                    SELECT 1 FROM public.users WHERE id = p_actor AND role = 'admin' AND is_active) THEN
                o_result := 'not_authorized'; RETURN;
            END IF;
            IF EXISTS (SELECT 1 FROM public.stepup_grants WHERE consumed_txid = pg_current_xact_id_if_assigned()) THEN
                o_result := 'grant_presented'; RETURN;  -- one grant, one action: the granted path claims it
            END IF;
            SELECT * INTO a FROM public.user_signups WHERE id = p_signup_id FOR UPDATE;  -- application first
            IF NOT FOUND THEN
                o_result := 'not_found'; RETURN;
            END IF;
            IF a.review_status <> 'approved' OR a.streamer_id IS NULL THEN
                o_result := 'not_approved'; RETURN;
            END IF;
            PERFORM 1 FROM public.user_invites WHERE signup_id = a.id AND accepted_at IS NULL AND revoked_at IS NULL
                FOR UPDATE;  -- then its open invitations
            IF p_automatic AND (
                    EXISTS (SELECT 1 FROM public.user_invites WHERE signup_id = a.id AND accepted_at IS NULL
                              AND revoked_at IS NULL)  -- open, or expired after delivery (Q4)
                    OR EXISTS (SELECT 1 FROM public.application_events WHERE signup_id = a.id AND action = 'invite'
                                 AND note IS DISTINCT FROM 'auto')) THEN  -- an administrator took over
                o_result := 'not_due'; RETURN;
            END IF;
            IF p_automatic AND (SELECT count(*) FROM public.application_events WHERE signup_id = a.id
                                AND action = 'invite' AND note = 'auto') >= {MAX_AUTO_ATTEMPTS} THEN
                o_result := 'attempts_exhausted'; RETURN;
            END IF;
            SELECT user_id INTO owner_id FROM public.streamers WHERE id = a.streamer_id FOR NO KEY UPDATE;
            IF owner_id IS NOT NULL THEN  -- then its creator
                o_result := 'creator_claimed'; RETURN;
            END IF;
            IF EXISTS (SELECT 1 FROM public.users WHERE email = a.email::citext) THEN
                o_result := 'account_exists'; RETURN;  -- Q2: the manual owner-assign path
            END IF;
            UPDATE public.user_invites SET revoked_at = now()
            WHERE signup_id = a.id AND accepted_at IS NULL AND revoked_at IS NULL;
            GET DIAGNOSTICS o_replaced = ROW_COUNT;
            o_expires_at := now() + make_interval(days => least(greatest(coalesce(p_ttl_days, {MAX_TTL_DAYS}), 1),
                                                                {MAX_TTL_DAYS}));
            PERFORM set_config('hyperchat.onboarding_invite', p_token_hash, true);  -- read by the guard below
            INSERT INTO public.user_invites (email, role, streamer_id, token_hash, invited_by, expires_at, signup_id)
            VALUES (a.email, 'streamer', a.streamer_id, p_token_hash, p_actor, o_expires_at, a.id)
            RETURNING id INTO o_invite_id;
            PERFORM set_config('hyperchat.onboarding_invite', '', true);
            UPDATE public.user_signups SET invite_id = o_invite_id WHERE id = a.id;
            INSERT INTO public.application_events (signup_id, action, actor_user_id, note, streamer_id, invite_id)
            VALUES (a.id, 'invite', p_actor, CASE WHEN p_automatic THEN 'auto' END, a.streamer_id, o_invite_id);
            o_email := a.email;
            o_result := 'ok';
        END $$
    """)
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM {EVERYONE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO hyperchat_lifecycle")
    op.execute(invite_guard())  # CREATE OR REPLACE keeps 0027's grants (REVOKE ALL) and its trigger


def downgrade() -> None:
    op.execute(_function_0027(_render_0027()[1], GUARD))
    op.execute(f"DROP FUNCTION {FUNCTION}")


# ── the guard exception ───────────────────────────────────────────────────────────────────────────────────────
GUARD = "stepup_guard_user_invites"
_ANCHOR = ("                PERFORM public.stepup_authorize('application.invite', 'application:' || NEW.signup_id, "
           "'{}'::jsonb,\n                                                'user_invites.insert');")
_EXCEPTION = """\
                IF NOT (session_user = 'hyperchat_lifecycle'  -- 0038: onboarding_issue_invite's own insert
                        AND NEW.token_hash = current_setting('hyperchat.onboarding_invite', true)
                        AND NEW.email::text = (SELECT a.email FROM public.user_signups a WHERE a.id = NEW.signup_id)
                        AND NOT EXISTS (SELECT 1 FROM public.stepup_grants
                                        WHERE consumed_txid = pg_current_xact_id_if_assigned())) THEN
                    PERFORM public.stepup_authorize('application.invite', 'application:' || NEW.signup_id, '{}'::jsonb,
                                                    'user_invites.insert');
                END IF;"""


def _render_0027() -> tuple[types.ModuleType, list[str]]:
    """0027's module and its upgrade() statements, rendered without a database (as 0037 does)."""
    path = pathlib.Path(__file__).with_name("0027_stepup_enforcement.py")
    spec = importlib.util.spec_from_file_location("_hyperchat_0027_for_0038", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []
    module.op = types.SimpleNamespace(execute=statements.append)
    module.upgrade()
    return module, statements


def _function_0027(statements: list[str], name: str) -> str:
    (sql,) = [s for s in statements if f"CREATE FUNCTION public.{name}()" in s]
    return sql.replace(f"CREATE FUNCTION public.{name}()", f"CREATE OR REPLACE FUNCTION public.{name}()", 1)


def invite_guard() -> str:
    sql = _function_0027(_render_0027()[1], GUARD)
    assert sql.count(_ANCHOR) == 1, "0027 text changed: the application.invite anchor"
    return sql.replace(_ANCHOR, _EXCEPTION)
