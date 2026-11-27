"""Migration 0040: several Didit accounts. Rotation for new users, every user pinned to the account of their first
session (never re-routed), webhooks verified only with their own account's secret, approvals from any not-revoked
account, vetoes across all accounts, operator moves and revocations audited. The real app on the scratch database
with fake Didit clients; synthetic rows only (extra accounts and their events are removed after each test)."""

import threading
import time
import uuid

import psycopg
import pytest
from pydantic import SecretStr

from app.kyc.service import KycService
from tests.integration import DOMAIN, db, fixture_db, make_user, new_browser, requires_db
from tests.kyc.test_kyc_db import (
    APP_ID,
    ME,
    WF,
    FakeDidit,
    approved,
    approved_decision,
    configure,
    hook,
    owner,
    start,
    unconfigure,
)

pytestmark = requires_db
B, B_APP, B_WF, B_SECRET = "acct-b", "0b0b0b0b-2222-4333-8444-555555555555", str(uuid.uuid4()), "whsec_kyc_account_b_x"
REFUSED = psycopg.errors.InsufficientPrivilege


def add_account(account: str = B, app_id: str = B_APP, workflow: str = B_WF, secret: str = B_SECRET, **cols) -> None:
    columns = {"account_id": account, "environment": "sandbox", "workflow_id": workflow, "application_id": app_id,
               "webhook_secret": secret, **cols}
    with fixture_db() as conn:  # the operator (etl.kyc_provider_account add)
        conn.execute(f"INSERT INTO public.kyc_provider_accounts ({', '.join(columns)}) "
                     f"VALUES ({', '.join(['%s'] * len(columns))})", tuple(columns.values()))


def set_primary(**cols) -> None:
    with fixture_db() as conn:
        conn.execute("UPDATE public.kyc_provider_config SET " + ", ".join(f"{k} = %s" for k in cols),
                     tuple(cols.values()))


def clear_accounts() -> None:
    with fixture_db() as conn:
        conn.execute("ALTER TABLE public.kyc_provider_accounts DISABLE TRIGGER USER")
        conn.execute("ALTER TABLE public.kyc_provider_config DISABLE TRIGGER USER")
        try:
            conn.execute("DELETE FROM public.kyc_provider_accounts")
            conn.execute("UPDATE public.kyc_provider_config SET enabled = true, monthly_cap = NULL, revoked_at = NULL, "
                         "revoked_reason = NULL")
        finally:
            conn.execute("ALTER TABLE public.kyc_provider_config ENABLE TRIGGER USER")
            conn.execute("ALTER TABLE public.kyc_provider_accounts ENABLE TRIGGER USER")
        conn.execute("DELETE FROM public.kyc_account_events")
        conn.execute("DELETE FROM public.kyc_webhook_events WHERE account_id <> 'primary'")
        conn.execute("DELETE FROM public.system_alerts WHERE context->>'kind' = 'kyc_account_unavailable'")


@pytest.fixture
def multi(app):
    """KYC with the primary (fake A) and account acct-b (fake B)."""
    settings = app.app_state.settings.model_copy(update={
        "didit_api_key": SecretStr("test-key"), "didit_workflow_id": WF, "didit_environment": "sandbox"})
    app.fake, app.fake_b = FakeDidit(), FakeDidit(B_WF)
    configure()
    add_account()
    app.app_state.kyc = KycService(app.app_state.kyc.repo, settings, client=app.fake, limiter=app.app_state.auth.repo,
                                   clients={B: app.fake_b}, alerts=app.app_state.kyc.alerts)
    yield app
    clear_accounts()
    unconfigure()


def account_of(sid: str) -> str:
    with db() as conn:
        return conn.execute("SELECT account_id FROM public.kyc_sessions WHERE session_id = %s", (sid,)).fetchone()[0]


def pin(uid: str) -> str | None:
    with db() as conn:
        row = conn.execute("SELECT account_id FROM public.kyc_account_pins WHERE user_id = %s", (uid,)).fetchone()
    return row[0] if row else None


def events(action: str) -> list[tuple]:
    with fixture_db() as conn:
        return conn.execute("SELECT account_id, user_id::text, from_account, operator, reason FROM "
                            "public.kyc_account_events WHERE action = %s ORDER BY created_at", (action,)).fetchall()


def alerts() -> list[str]:
    with db() as conn:
        return [r[0] for r in conn.execute("SELECT message FROM public.system_alerts WHERE context->>'kind' = "
                                           "'kyc_account_unavailable' ORDER BY created_at").fetchall()]


def b_hook(app, **kw):
    return hook(app, secret=B_SECRET, application_id=B_APP, workflow_id=B_WF, **kw)


# ── rotation and pinning ───────────────────────────────────────────────────────────────────────────────────────

def test_new_users_rotate_and_every_user_stays_on_the_account_of_their_first_session(multi):
    one, two = new_browser(multi), new_browser(multi)
    h1, u1 = owner(one, "kyc-acc-1")
    s1 = start(one, h1)["session_id"]  # both unused: the fixed order picks the primary
    assert (account_of(s1), pin(u1), multi.fake.calls, multi.fake_b.calls) == ("primary", "primary", 1, 0)
    h2, u2 = owner(two, "kyc-acc-2")
    s2 = start(two, h2)["session_id"]  # the primary was used once this month: the least-used is acct-b
    assert (account_of(s2), pin(u2), multi.fake_b.calls, multi.fake_b.last_workflow) == (B, B, 1, B_WF)
    multi.fake.finish(u1)
    start(one, h1)  # a retry stays pinned, even though acct-b is now as used as the primary
    assert multi.fake.calls == 2 and pin(u1) == "primary"


def test_an_existing_user_without_a_pin_stays_with_their_history(multi):
    h, uid = owner(multi, "kyc-acc-hist")
    with fixture_db() as conn:  # a session from before 0040 (the primary), and acct-b as the least-used account
        conn.execute("INSERT INTO public.kyc_sessions (user_id, vendor_data, session_id, workflow_id, environment) "
                     "VALUES (%s, %s, gen_random_uuid(), %s, 'sandbox')", (uid, uid, WF))
        conn.execute("INSERT INTO public.kyc_account_reservations (user_id, account_id) VALUES (%s, 'primary')",
                     (make_user("kyc-acc-load" + DOMAIN),))
    start(multi, h)
    assert pin(uid) == "primary" and multi.fake_b.calls == 0


def test_a_pinned_account_that_cannot_take_sessions_answers_503_and_alerts_never_reroutes(multi):
    h, uid = owner(multi, "kyc-acc-pinned")
    set_primary(enabled=False)  # new users now go to acct-b
    start(multi, h)
    assert pin(uid) == B
    with fixture_db() as conn:
        conn.execute("UPDATE public.kyc_provider_accounts SET enabled = false WHERE account_id = %s", (B,))
    set_primary(enabled=True)
    multi.fake_b.finish(uid)
    r = multi.post(f"{ME}/session", headers=h)
    assert r.status_code == 503
    assert multi.fake.calls == 0 and multi.fake_b.calls == 1 and pin(uid) == B  # never silently on the primary
    assert alerts() == ["account_disabled"]
    assert [e[0] for e in events("account_changed")] == ["primary", B, "primary"]  # every change audited


def test_monthly_caps_hold_under_concurrent_assignments(multi):
    set_primary(enabled=False)
    with fixture_db() as conn:
        conn.execute("UPDATE public.kyc_provider_accounts SET monthly_cap = 3 WHERE account_id = %s", (B,))
    users = [make_user(f"kyc-acc-cap{n}" + DOMAIN) for n in range(8)]
    results: list[str] = []

    def assign(uid):
        with db() as conn:
            results.append(conn.execute("SELECT o_result FROM public.kyc_assign_account(%s)", (uid,)).fetchone()[0])

    threads = [threading.Thread(target=assign, args=(u,)) for u in users]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(results) == ["no_account_available"] * 5 + ["ok"] * 3
    h, _ = owner(multi, "kyc-acc-capped")
    r = multi.post(f"{ME}/session", headers=h)
    assert r.status_code == 503 and "no_account_available" in alerts()


def test_an_account_without_an_api_key_here_answers_503(multi):
    multi.app_state.kyc.clients.pop(B)
    set_primary(enabled=False)
    h, _ = owner(multi, "kyc-acc-nokey")
    r = multi.post(f"{ME}/session", headers=h)
    assert r.status_code == 503 and alerts() == ["account_key_missing"]


# ── webhooks: each account's own secret, each session's own account ─────────────────────────────────────────────

def test_a_webhook_is_verified_with_its_own_accounts_secret_and_never_moves_another_accounts_session(multi):
    set_primary(enabled=False)
    h, uid = owner(multi, "kyc-acc-hook")
    sid = start(multi, h)["session_id"]
    t = int(time.time())
    common = {"session_id": sid, "vendor": uid, "status": "Approved", "created_at": t,
              "decision": approved_decision(sid)}
    # signed by the primary (its app id and workflow): a genuine primary signature, but not this session's account
    assert hook(multi, **common).json()["status"] == "identity_mismatch"
    # acct-b's app id signed with the primary's secret: the account named decides the secret
    r = hook(multi, application_id=B_APP, workflow_id=B_WF, **common)
    assert r.status_code == 401
    assert not approved(uid)
    assert b_hook(multi, **common).json()["status"] == "applied" and approved(uid)
    with db() as conn:
        assert conn.execute("SELECT account_id FROM public.kyc_webhook_events WHERE session_id = %s AND "
                            "outcome = 'applied'", (sid,)).fetchone()[0] == B


def test_an_early_webhook_applies_only_to_a_session_of_its_own_account(multi):
    set_primary(enabled=False)
    h, uid = owner(multi, "kyc-acc-early")
    early = str(uuid.uuid4())
    t = int(time.time())
    assert hook(multi, session_id=early, vendor=uid, status="In Progress", created_at=t).json()["status"] == \
        "unmatched"  # verified by the primary, before the session exists
    multi.fake_b.sessions[uid] = early  # acct-b's Didit then returns that session id
    start(multi, h)
    with db() as conn:
        assert conn.execute("SELECT outcome FROM public.kyc_webhook_events WHERE session_id = %s",
                            (early,)).fetchone()[0] == "identity_mismatch"


# ── approvals, vetoes, revocation, moves ───────────────────────────────────────────────────────────────────────

def test_a_decline_on_one_account_beats_an_older_approval_on_another_and_a_newer_one_wins(multi):
    me = new_browser(multi)
    h, uid = owner(me, "kyc-acc-veto")
    a = start(me, h)["session_id"]  # the primary
    t = int(time.time()) - 600
    assert hook(multi, session_id=a, vendor=uid, status="Declined", created_at=t + 100).json()["status"] == "applied"
    with fixture_db() as conn:
        assert conn.execute("SELECT public.kyc_account_move_user(%s, %s, 'ops', 'primary retired')",
                            (uid, B)).fetchone()[0] == "moved"
    b = start(me, h)["session_id"]
    assert account_of(b) == B
    assert b_hook(multi, session_id=b, vendor=uid, status="Approved", created_at=t + 50,
                  decision=approved_decision(b)).json()["status"] == "applied"
    assert not approved(uid)  # approved before the decline (on another account): the decline still vetoes
    assert b_hook(multi, session_id=b, vendor=uid, status="Approved", created_at=t + 200,
                  decision=approved_decision(b)).json()["status"] == "applied"
    assert approved(uid)  # a newer signed approval wins, as within one account
    assert events("pin_moved") == [(B, uid, "primary", "ops", "primary retired")]


def test_disabling_keeps_approvals_and_revoking_retires_them_for_good(multi):
    set_primary(enabled=False)
    h, uid = owner(multi, "kyc-acc-revoke")
    sid = start(multi, h)["session_id"]
    assert b_hook(multi, session_id=sid, vendor=uid, status="Approved", created_at=int(time.time()),
                  decision=approved_decision(sid)).json()["status"] == "applied"
    with fixture_db() as conn:
        conn.execute("UPDATE public.kyc_provider_accounts SET enabled = false WHERE account_id = %s", (B,))
    assert approved(uid)  # disabled: no new sessions, existing approvals count
    with fixture_db() as conn:
        assert conn.execute("SELECT public.kyc_account_revoke(%s, '', 'x')", (B,)).fetchone()[0] == \
            "operator_and_reason_required"
        assert conn.execute("SELECT public.kyc_account_revoke(%s, 'ops', 'account closed')",
                            (B,)).fetchone()[0] == "revoked"
    assert not approved(uid) and events("account_revoked") == [(B, None, None, "ops", "account closed")]
    with pytest.raises(psycopg.errors.CheckViolation), fixture_db() as conn:  # final, even for the operator
        conn.execute("UPDATE public.kyc_provider_accounts SET revoked_at = NULL WHERE account_id = %s", (B,))
    with pytest.raises(psycopg.errors.RestrictViolation), fixture_db() as conn:  # its history keeps it
        conn.execute("DELETE FROM public.kyc_provider_accounts WHERE account_id = %s", (B,))
    r = multi.post(f"{ME}/session", headers=h)
    assert r.status_code == 503 and alerts() == ["account_revoked"]


def test_a_pin_moves_only_through_the_audited_operator_function(multi):
    h, uid = owner(multi, "kyc-acc-move")
    start(multi, h)
    with pytest.raises(REFUSED), fixture_db() as conn:  # not even the owner by hand
        conn.execute("UPDATE public.kyc_account_pins SET account_id = %s WHERE user_id = %s", (B, uid))
    with fixture_db() as conn:
        assert conn.execute("SELECT public.kyc_account_move_user(%s, 'nope', 'ops', 'x')",
                            (uid,)).fetchone()[0] == "unknown_or_revoked_account"
    assert pin(uid) == "primary" and events("pin_moved") == []


# ── the trust boundary ─────────────────────────────────────────────────────────────────────────────────────────

def test_no_runtime_role_reads_an_accounts_secret_or_moves_pins(multi):
    h, uid = owner(multi, "kyc-acc-boundary")
    start(multi, h)
    with fixture_db() as conn:
        for role in ("hyperchat_app", "hyperchat_worker", "hyperchat_auth", "hyperchat_lifecycle", "hyperchat_readonly",
                     "hyperchat_stepup"):
            assert not conn.execute("SELECT has_table_privilege(%s, 'public.kyc_provider_accounts', 'SELECT')",
                                    (role,)).fetchone()[0], role
            for fn, allowed in (("kyc_assign_account(uuid)", role == "hyperchat_app"),
                                ("kyc_account_move_user(uuid, text, text, text)", False),
                                ("kyc_account_revoke(text, text, text)", False),
                                ("kyc_accounts()", False), ("kyc_user_pin(uuid, text)", False)):
                assert conn.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                                    (role, "public." + fn)).fetchone()[0] is allowed, (role, fn)
    with db() as conn:
        for sql, args in (("UPDATE public.kyc_account_pins SET account_id = %s WHERE user_id = %s", (B, uid)),
                          ("DELETE FROM public.kyc_account_pins WHERE user_id = %s", (uid,)),
                          ("INSERT INTO public.kyc_account_events (action, account_id, operator) VALUES "
                           "('pin_moved', 'primary', 'x')", ()),
                          ("SELECT public.kyc_create_session(%s, gen_random_uuid(), %s, 1, 'sandbox', %s)",
                           (uid, B_WF, B))):
            if sql.startswith("SELECT"):  # callable, but a user's sessions live on their pinned account only
                assert conn.execute(sql, args).fetchone()[0] == "wrong_account"
                continue
            with pytest.raises(REFUSED):
                conn.execute(sql, args)
    assert pin(uid) == "primary"
    fresh = make_user("kyc-acc-fresh" + DOMAIN)  # no pin yet: naming an account is not choosing one
    with db() as conn:
        assert conn.execute("SELECT public.kyc_create_session(%s, gen_random_uuid(), %s, 1, 'sandbox', %s)",
                            (fresh, B_WF, B)).fetchone()[0] == "wrong_account"
    assert pin(fresh) is None


def test_extra_accounts_need_the_primarys_application_id_and_their_own(multi):
    with pytest.raises(psycopg.errors.CheckViolation), fixture_db() as conn:  # the same application twice
        conn.execute("INSERT INTO public.kyc_provider_accounts (account_id, environment, workflow_id, application_id, "
                     "webhook_secret) VALUES ('acct-c', 'sandbox', %s, %s, 'whsec_kyc_account_c_x')", (WF, APP_ID))
    with pytest.raises(psycopg.errors.CheckViolation), fixture_db() as conn:  # another environment
        conn.execute("INSERT INTO public.kyc_provider_accounts (account_id, environment, workflow_id, application_id, "
                     "webhook_secret) VALUES ('acct-c', 'live', %s, gen_random_uuid(), 'whsec_kyc_account_c_x')", (WF,))
    with pytest.raises(psycopg.errors.CheckViolation), fixture_db() as conn:  # routing needs the primary's id
        conn.execute("UPDATE public.kyc_provider_config SET application_id = NULL")
