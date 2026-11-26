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

NV-3 (security audit 2026-11-26): an approval also binds WHO receives the creator. 0027's approval anchor and its
creator_created effect hashed only {slug, name, razorpay_account}, and the API roles could change a submitted
application's email right before an administrator approved it. Here both 0027 functions are re-created from
0027's own text with exactly two changes (rendered and substituted below, asserted to match once):
  * application.approve parameters gain 'email': lower(user_signups.email), in stepup_guard_user_signups (the
    anchor) and stepup_guard_streamer_insert (the effect, which must hash the same parameters);
  * stepup_guard_user_signups refuses any change of user_signups.email by an API role (no code path writes it;
    0027 already froze it after approval). The schema owner and the migrator are not API roles.
The API binds the same parameters (app.routers.admin.approve_application). A challenge issued before the deploy
has the 3-key hash and is refused after it (enforcement 'active'); challenges live 5 minutes.

pg_temp (security audit 2026-11-26, run-2/hardening-0037-pg-temp.md): the 11 SECURITY DEFINER payment functions
of 0031/0032/0034/0035 set search_path = pg_catalog, public and omitted pg_temp, which PostgreSQL then searches
FIRST for relations, so a caller's TEMP table could shadow an unqualified name inside them. ALTER FUNCTION ... SET
search_path = pg_catalog, public, pg_temp changes only that setting (lock_timeout on payment_accounting_set_state is
kept; bodies, owners and grants are unchanged); the PAY-15 verifier registers the new proconfig as head 0037's
"configs" (app.payments.verify.HEAD_DELTAS). Every other SECURITY DEFINER function already ends in pg_temp.

Downgrade drops the two guards and restores 0025's CHECK as NOT VALID (challenges already issued for the two
actions stay as audit history), re-creates the two 0027 functions exactly as 0027 wrote them, and restores the
11 search paths. Nothing in upgrade or downgrade reads or writes table data.
"""

import importlib.util
import pathlib
import types

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
    for sql in nv3_functions():  # CREATE OR REPLACE keeps 0027's grants (REVOKE ALL) and its triggers
        op.execute(sql)
    op.execute(search_path_sql("pg_catalog, public, pg_temp"))


def downgrade() -> None:
    op.execute(search_path_sql("pg_catalog, public"))
    for trigger, table, _, fn in GUARDS:
        op.execute(f"DROP TRIGGER {trigger} ON public.{table}")
        op.execute(f"DROP FUNCTION public.{fn}()")
    _check(OLD_ACTIONS, valid=False)
    _, statements = _render_0027()
    for name in NV3_FUNCTIONS:
        op.execute(_function_0027(statements, name))


# ── NV-3: the applicant's address in application.approve ─────────────────────────────────────────────────────
NV3_FUNCTIONS = ("stepup_guard_user_signups", "stepup_guard_streamer_insert")


def _render_0027() -> tuple[types.ModuleType, list[str]]:
    """0027's module and its upgrade() statements, rendered without a database (as 0035 renders 0034)."""
    path = pathlib.Path(__file__).with_name("0027_stepup_enforcement.py")
    spec = importlib.util.spec_from_file_location("_hyperchat_0027", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []
    module.op = types.SimpleNamespace(execute=statements.append)
    module.upgrade()
    return module, statements


def _function_0027(statements: list[str], name: str) -> str:
    (sql,) = [s for s in statements if f"CREATE FUNCTION public.{name}()" in s]
    return sql.replace(f"CREATE FUNCTION public.{name}()", f"CREATE OR REPLACE FUNCTION public.{name}()", 1)


def _swap(sql: str, old: str, new: str) -> str:
    assert sql.count(old) == 1, f"0027 text changed: {old!r}"
    return sql.replace(old, new)


def nv3_functions() -> list[str]:
    module, statements = _render_0027()
    signups = _function_0027(statements, "stepup_guard_user_signups")
    signups = _swap(signups, "'razorpay_account', s.razorpay_account), 'user_signups.review_status');",
                    "'razorpay_account', s.razorpay_account,\n"
                    "                                       'email', lower(NEW.email::text)), 'user_signups.review_status');")
    signups = _swap(signups, "            IF NEW.invite_id IS DISTINCT FROM OLD.invite_id AND NOT EXISTS (",
                    "            IF NEW.email IS DISTINCT FROM OLD.email THEN  -- NV-3: the applicant's own address\n"
                    f"                RAISE EXCEPTION 'an application''s address is the applicant''s own' {module.REFUSE};\n"
                    "            END IF;\n"
                    "            IF NEW.invite_id IS DISTINCT FROM OLD.invite_id AND NOT EXISTS (")
    inserts = _function_0027(statements, "stepup_guard_streamer_insert")
    inserts = _swap(inserts, "DECLARE signup uuid; s public.streamers;",
                    "DECLARE signup uuid; signup_email text; s public.streamers;")
    inserts = _swap(inserts, "SELECT a.id INTO signup FROM public.user_signups a",
                    "SELECT a.id, a.email INTO signup, signup_email FROM public.user_signups a")
    inserts = _swap(inserts, "'razorpay_account', s.razorpay_account), 'creator_created');",
                    "'razorpay_account', s.razorpay_account,\n"
                    "                                       'email', lower(signup_email)), 'creator_created');")
    return [signups, inserts]


# ── pg_temp: the SECURITY DEFINER payment functions (the PAY-15 verifier's DEFINER_FUNCTIONS) ───────────────────
PG_TEMP_FUNCTIONS = ("donation_accounting_apply", "donation_accounting_core", "donations_credit_writer",
                     "payment_accounting_apply_evaluation", "payment_accounting_keep", "payment_accounting_lock_state",
                     "payment_accounting_open_run", "payment_accounting_reconcile", "payment_accounting_revert",
                     "payment_accounting_set_state", "payment_evidence_fp")


def search_path_sql(path: str) -> str:
    """ALTER FUNCTION ... SET search_path for exactly these 11 functions (one each; their exact signatures from the
    catalog), refused unless all 11 are found: a schema that drifted stops the migration instead of half-applying."""
    names = ", ".join(f"'{n}'" for n in PG_TEMP_FUNCTIONS)
    return f"""
        DO $$
        DECLARE f regprocedure; n integer := 0;
        BEGIN
            FOR f IN SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace s ON s.oid = p.pronamespace
                     WHERE s.nspname = 'public' AND p.prosecdef AND p.proname IN ({names}) ORDER BY p.proname LOOP
                EXECUTE format('ALTER FUNCTION %s SET search_path = {path}', f);
                n := n + 1;
            END LOOP;
            IF n <> {len(PG_TEMP_FUNCTIONS)} THEN
                RAISE EXCEPTION 'expected {len(PG_TEMP_FUNCTIONS)} SECURITY DEFINER payment functions, found %', n;
            END IF;
        END $$
    """
