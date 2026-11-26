"""SEC-A and SEC-F (Phase 2 audit §3.2, §3.4) against real PostgreSQL, with the verifier on a Unix socket.

T-2  With enforcement 'active', an aal2 administrator can no longer grant soundboard eligibility or reopen an
     application without a passkey grant for exactly that action (403 step_up_required); in 'draft' both work
     without one, as every other action does. A presented grant is consumed in the operation's own transaction.
     With a correctly bound grant both succeed; the database (migration 0040) claims it at the anchor write and
     refuses the write itself without one, whatever code issues it.
SEC-F A missing stepup_enforcement row answers a clean 503 stepup_unavailable, never a 500 and never a skipped
     aal2 check.
"""

import asyncio
import logging
import os
import tempfile
import uuid

import psycopg
import pytest

from app.auth.repository import AuthRepository
from app.stepup.grants import StepUpError
from tests.integration import DOMAIN, ORIGIN, db, fixture_db, login, make_streamer, make_user, new_browser, requires_db
from tests.stepup.test_mfa_api import MFA, _api, _verifier, ceremony, enroll
from tests.stepup.test_stepup_db import guards_off
from tests.stepup.test_stepup_enforcement_db import set_enforcement

A = "/api/v1/admin"
GRANT = "x-step-up-grant"


# ── SEC-F without a database ────────────────────────────────────────────────────

class _NoRow(AuthRepository):
    def __init__(self, row) -> None:
        super().__init__(None)
        self.row = row

    async def _one(self, sql, params):
        return self.row


@pytest.mark.parametrize("call,row", [("stepup_enforcement", None),
                                      ("stepup_session", {"state": None, "aal": 1})])
def test_a_missing_enforcement_row_is_a_clean_503_not_a_500_or_draft(call, row):
    repo = _NoRow(row)
    args = (str(uuid.uuid4()),) if call == "stepup_session" else ()
    with pytest.raises(StepUpError) as exc:
        asyncio.run(getattr(repo, call)(*args))
    assert (exc.value.status, exc.value.reason) == (503, "stepup_unavailable")


# ── fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture
def api(env, monkeypatch):
    path = os.path.join(tempfile.mkdtemp(prefix="stepup-"), "stepup.sock")
    server, thread = _verifier(path)
    client, capture = _api(monkeypatch, path)
    yield client
    client.__exit__(None, None, None)
    logging.getLogger().removeHandler(capture)
    server.should_exit = True
    thread.join(10)
    set_enforcement("draft")


def browser(api, email: str):
    b = new_browser(api)
    r = login(b, email)
    assert r.status_code == 200, r.text
    return b, {**ORIGIN, "x-csrf-token": r.json()["csrf_token"]}


def rejected_application(local: str) -> str:
    with db() as conn:
        return conn.execute("INSERT INTO public.user_signups (name, email, mobile_number, review_status, "
                            "rejection_reason) VALUES ('R', %s, '+91 90000 00009', 'rejected', 'test') "
                            "RETURNING id::text", (local + DOMAIN,)).fetchone()[0]


def soundboard(sid: str) -> bool:
    with db() as conn:
        return conn.execute("SELECT soundboard_enabled FROM public.streamers WHERE id = %s", (sid,)).fetchone()[0]


def review_status(signup_id: str) -> str:
    with db() as conn:
        return conn.execute("SELECT review_status FROM public.user_signups WHERE id = %s",
                            (signup_id,)).fetchone()[0]


def body(r) -> tuple:
    data = r.json()
    return r.status_code, data.get("reason"), data.get("step_up") or data.get("detail")


# ── T-2 ─────────────────────────────────────────────────────────────────────────

@requires_db
def test_active_refuses_soundboard_eligibility_and_reopen_without_a_grant(api):
    sid = make_streamer("authtest-seca-act", user_id=make_user("seca-act-owner" + DOMAIN))
    app_id = rejected_application("seca-act-app")
    make_user("seca-act-admin" + DOMAIN, role="admin")
    b, h = browser(api, "seca-act-admin" + DOMAIN)
    auth = enroll(api, b, h, "seca-act-admin" + DOMAIN)
    set_enforcement("active")
    ceremony(b, h, auth, "session.aal2", "session", {})  # aal2: past require_admin, the case SEC-A was about

    r = b.put(f"{A}/creators/{sid}/soundboard", json={"enabled": True}, headers=h)
    assert body(r) == (403, "step_up_required",
                       {"action": "creator.soundboard", "target": f"creator:{sid}", "params": {"enabled": True}})
    r = b.post(f"{A}/applications/{app_id}/reopen", json={"note": "again"}, headers=h)
    assert body(r) == (403, "step_up_required",
                       {"action": "application.reopen", "target": f"application:{app_id}", "params": {}})
    assert soundboard(sid) is False and review_status(app_id) == "rejected"

    # A presented grant is consumed inside the operation's transaction: an unknown one refuses and changes nothing.
    forged = {**h, GRANT: str(uuid.uuid4())}
    assert body(b.put(f"{A}/creators/{sid}/soundboard", json={"enabled": True}, headers=forged)) == (
        403, "step_up_refused", "not_found")
    assert body(b.post(f"{A}/applications/{app_id}/reopen", json={}, headers=forged)) == (
        403, "step_up_refused", "not_found")
    assert soundboard(sid) is False and review_status(app_id) == "rejected"

    # A grant bound to other parameters is refused; the exact grant succeeds, is consumed and claimed by the anchor.
    wrong = ceremony(b, h, auth, "creator.soundboard", f"creator:{sid}", {"enabled": False})["grant_id"]
    assert body(b.put(f"{A}/creators/{sid}/soundboard", json={"enabled": True}, headers={**h, GRANT: wrong})) == (
        403, "step_up_refused", "mismatch")
    grant = ceremony(b, h, auth, "creator.soundboard", f"creator:{sid}", {"enabled": True})["grant_id"]
    r = b.put(f"{A}/creators/{sid}/soundboard", json={"enabled": True}, headers={**h, GRANT: grant})
    assert r.status_code == 200 and r.json()["changed"] is True, r.text
    grant = ceremony(b, h, auth, "application.reopen", f"application:{app_id}", {})["grant_id"]
    r = b.post(f"{A}/applications/{app_id}/reopen", json={"note": "again"}, headers={**h, GRANT: grant})
    assert r.status_code == 200, r.text
    assert soundboard(sid) is True and review_status(app_id) == "submitted"
    with fixture_db() as conn:  # the runtime roles cannot read step-up tables
        claimed = conn.execute("SELECT array_agg(claimed_anchor ORDER BY claimed_anchor) FROM public.stepup_grants "
                               "WHERE action IN ('creator.soundboard', 'application.reopen') "
                               "AND claimed_at IS NOT NULL").fetchone()[0]
    assert claimed == ["streamers.soundboard_enabled", "user_signups.reopen"]


@requires_db
def test_active_the_database_refuses_both_writes_without_a_grant_whatever_the_caller(api):
    sid = make_streamer("authtest-seca-guard", user_id=make_user("seca-guard-owner" + DOMAIN))
    app_id = rejected_application("seca-guard-app")
    set_enforcement("active")
    for sql, args in (("UPDATE public.streamers SET soundboard_enabled = true WHERE id = %s", (sid,)),
                      ("UPDATE public.user_signups SET review_status = 'submitted', rejection_reason = NULL "
                       "WHERE id = %s", (app_id,))):
        with db() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege, match="step-up required"):
            conn.execute(sql, args)
    assert soundboard(sid) is False and review_status(app_id) == "rejected"
    set_enforcement("draft")  # the legacy path: no grant presented
    with db() as conn:
        conn.execute("UPDATE public.streamers SET soundboard_enabled = true WHERE id = %s", (sid,))
    assert soundboard(sid) is True


@requires_db
def test_draft_keeps_both_routes_working_without_a_grant(api):
    sid = make_streamer("authtest-seca-draft", user_id=make_user("seca-draft-owner" + DOMAIN))
    app_id = rejected_application("seca-draft-app")
    make_user("seca-draft-admin" + DOMAIN, role="admin")
    b, h = browser(api, "seca-draft-admin" + DOMAIN)
    set_enforcement("draft")

    # A presented (unknown) grant still fails closed in draft, before anything changes, even for an unknown creator.
    forged = {**h, GRANT: str(uuid.uuid4())}
    assert body(b.put(f"{A}/creators/{sid}/soundboard", json={"enabled": True}, headers=forged))[:2] == (
        403, "step_up_refused")
    assert body(b.put(f"{A}/creators/{uuid.uuid4()}/soundboard", json={"enabled": True}, headers=forged))[:2] == (
        403, "step_up_refused")
    assert soundboard(sid) is False

    r = b.put(f"{A}/creators/{sid}/soundboard", json={"enabled": True}, headers=h)
    assert r.status_code == 200 and r.json() == {"creator_id": sid, "soundboard_enabled": True, "changed": True}
    assert b.put(f"{A}/creators/{uuid.uuid4()}/soundboard", json={"enabled": True}, headers=h).status_code == 404
    r = b.post(f"{A}/applications/{app_id}/reopen", json={"note": "again"}, headers=h)
    assert r.status_code == 200, r.text
    assert soundboard(sid) is True and review_status(app_id) == "submitted"


# ── SEC-F with the row really absent ────────────────────────────────────────────

@requires_db
def test_a_missing_enforcement_row_refuses_admin_routes_with_503(api):
    make_user("secf-admin" + DOMAIN, role="admin")
    b, h = browser(api, "secf-admin" + DOMAIN)
    sid = make_streamer("authtest-secf", user_id=make_user("secf-owner" + DOMAIN))
    with guards_off("stepup_enforcement") as conn:
        cur = conn.execute("SELECT * FROM public.stepup_enforcement")
        columns, saved = [d.name for d in cur.description], cur.fetchone()
        conn.execute("DELETE FROM public.stepup_enforcement")
    try:
        for r in (b.get(f"{A}/users"), b.put(f"{A}/creators/{sid}/soundboard", json={"enabled": True}, headers=h),
                  b.get(f"{MFA}/status")):
            assert (r.status_code, r.json().get("reason")) == (503, "stepup_unavailable"), r.text
        assert soundboard(sid) is False
    finally:
        with guards_off("stepup_enforcement") as conn:
            conn.execute(f"INSERT INTO public.stepup_enforcement ({', '.join(columns)}) "
                         f"VALUES ({', '.join(['%s'] * len(columns))})", saved)
    assert b.get(f"{A}/users").status_code == 200
