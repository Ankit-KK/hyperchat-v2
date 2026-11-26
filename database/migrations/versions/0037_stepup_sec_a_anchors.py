"""SEC-A: step-up actions for soundboard eligibility and application reopen, with their database anchors.

Revision ID: 0037
Revises: 0036 (creator pricing constraints; assigned by the coordinator 2026-11-26)

The API binds both since PR #159 (app.stepup.actions.ROUTES); this migration makes them grantable and guards the
writes themselves, as 0027 does for every other action (docs/admin-stepup.md, "S3"):
  * stepup_challenges.action CHECK (0025) widened by creator.soundboard and application.reopen;
  * streamers.soundboard_enabled changed     creator.soundboard   creator:<id>      {enabled}  (the new value)
  * user_signups.review_status rejected -> submitted
                                             application.reopen   application:<id>  {}
Both anchors call stepup_authorize (0027): the grant consumed in this transaction for exactly this action, target
and hash is claimed; without one the write is refused unless enforcement is 'draft' and the transaction presented
no grant at all. The only writers are AdminRepository.set_soundboard_eligibility and ApplicationRepository.reopen
(the public signup form only inserts 'submitted' rows; nothing else moves an application out of 'rejected').

Downgrade drops the two guards and restores 0025's CHECK as NOT VALID (challenges already issued for the two
actions stay as audit history).
"""

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

API_ROLES = "('hyperchat_app', 'hyperchat_worker', 'hyperchat_auth', 'hyperchat_lifecycle', 'hyperchat_stepup')"
DEFINER = "SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp"
OLD_ACTIONS = ("session.aal2", "mfa.register", "creator.activate", "creator.reactivate", "creator.create",
               "creator.owner.assign", "creator.owner.transfer", "application.approve", "application.invite",
               "admin.invite", "admin.user.change", "admin.user.email", "partner_key.create", "mfa.credential.revoke")
ACTIONS = OLD_ACTIONS + ("creator.soundboard", "application.reopen")  # app.stepup.canonical.ACTIONS
GUARDS = (  # (trigger, table, timing, function)
    ("aad_stepup_guard_soundboard", "streamers", "BEFORE UPDATE OF soundboard_enabled", "stepup_guard_soundboard"),
    ("aad_stepup_guard_reopen", "user_signups", "BEFORE UPDATE OF review_status", "stepup_guard_reopen"),
)
EVERYONE = ("hyperchat_app, hyperchat_worker, hyperchat_auth, hyperchat_lifecycle, hyperchat_readonly, "
            "hyperchat_stepup, PUBLIC")


def _quoted(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _check(actions, valid: bool = True) -> None:
    op.execute("ALTER TABLE public.stepup_challenges DROP CONSTRAINT stepup_challenges_action_check")
    op.execute(f"ALTER TABLE public.stepup_challenges ADD CONSTRAINT stepup_challenges_action_check "
               f"CHECK (action IN ({_quoted(actions)})){'' if valid else ' NOT VALID'}")


def upgrade() -> None:
    _check(ACTIONS)
    op.execute(f"""
        CREATE FUNCTION public.stepup_guard_soundboard() RETURNS trigger LANGUAGE plpgsql {DEFINER} AS $$
        BEGIN
            IF session_user IN {API_ROLES} AND NEW.soundboard_enabled IS DISTINCT FROM OLD.soundboard_enabled THEN
                PERFORM public.stepup_authorize('creator.soundboard', 'creator:' || NEW.id,
                    jsonb_build_object('enabled', NEW.soundboard_enabled), 'streamers.soundboard_enabled');
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.stepup_guard_reopen() RETURNS trigger LANGUAGE plpgsql {DEFINER} AS $$
        BEGIN
            IF session_user IN {API_ROLES} AND OLD.review_status = 'rejected' AND NEW.review_status = 'submitted' THEN
                PERFORM public.stepup_authorize('application.reopen', 'application:' || NEW.id, '{{}}'::jsonb,
                                                'user_signups.reopen');
            END IF;
            RETURN NEW;
        END $$
    """)
    for trigger, table, timing, fn in GUARDS:
        op.execute(f"CREATE TRIGGER {trigger} {timing} ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.{fn}()")
        op.execute(f"REVOKE ALL ON FUNCTION public.{fn}() FROM {EVERYONE}")


def downgrade() -> None:
    for trigger, table, _, fn in GUARDS:
        op.execute(f"DROP TRIGGER {trigger} ON public.{table}")
        op.execute(f"DROP FUNCTION public.{fn}()")
    _check(OLD_ACTIONS, valid=False)
