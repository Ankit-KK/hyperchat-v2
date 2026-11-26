"""Onboarding Stage 2: the automatic invitation loop (app.onboarding.auto_invite) on the scratch database, as the
worker's own roles (hyperchat_app picks, hyperchat_lifecycle issues: migration 0038). Fake email senders only;
synthetic rows only."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.admin.applications import ApplicationService
from app.auth.tokens import sha256_hex
from app.onboarding.auto_invite import MAX_ATTEMPTS
from tests.integration import db, make_user, requires_db
from tests.onboarding.conftest import FakeSender, age_events, alerts, events, invites, set_enforcement

pytestmark = requires_db


def _token(email) -> str:
    return email.text.split("\n")[3].split("token=", 1)[1]


def test_one_approval_gets_exactly_one_invitation_and_one_email(approved, inviter):
    sid, cid, email = approved("auto-one")
    sender = FakeSender()
    loop = inviter(sender)
    assert asyncio.run(loop.run_once()) == [(sid, "sent")]
    assert [m.to for m in sender.sent] == [email] and sender.sent[0].template == "invite"
    [(invite_id, token_hash, invited_by, revoked, to)] = invites(sid)
    assert (invited_by, revoked, to) == (None, False, email)  # no actor: the system issued it
    assert sha256_hex(_token(sender.sent[0])) == token_hash  # the emailed link is that invitation's
    assert [(action, note) for action, _, note in events(sid)] == [("approve", None), ("invite", "auto")]
    assert events(sid)[-1][1] is None  # the invite event has no actor either
    assert asyncio.run(loop.run_once()) == [] and len(sender.sent) == 1  # a second pass sends nothing
    assert len(invites(sid)) == 1


def test_the_admin_view_shows_the_derived_delivery_status(app, approved, inviter):
    from tests.admin.test_applications import _admin

    headers = _admin(app, "auto-view-boss")
    sid, _, _ = approved("auto-view")
    asyncio.run(inviter(FakeSender(fail=1)).run_once())
    view = app.get(f"/api/v1/admin/applications/{sid}", headers=headers).json()
    assert view["invite_delivery"] == {"status": "revoked", "auto_attempts": 1, "last_failure": "transient"}
    age_events(sid, "2 minutes")
    asyncio.run(inviter(FakeSender()).run_once())
    view = app.get(f"/api/v1/admin/applications/{sid}", headers=headers).json()
    assert view["invite_delivery"] == {"status": "pending", "auto_attempts": 2, "last_failure": "transient"}
    assert "@" not in str(view["invite_delivery"])  # codes only, never an address or a link


def test_a_send_failure_revokes_the_link_and_retries_after_the_backoff(approved, inviter):
    sid, _, email = approved("auto-retry")
    sender = FakeSender(fail=1)
    loop = inviter(sender)
    assert asyncio.run(loop.run_once()) == [(sid, "failed")]
    [(first, _, _, revoked, _)] = invites(sid)
    assert revoked and events(sid)[-1] == ("note", None, "auto:failed:transient")
    assert asyncio.run(loop.run_once()) == []  # the 60 s backoff has not passed
    age_events(sid, "61 seconds")
    assert asyncio.run(loop.run_once()) == [(sid, "sent")]
    assert [m.to for m in sender.sent] == [email]
    assert [(i[0] == first, i[3]) for i in invites(sid)] == [(True, True), (False, False)]  # a fresh link


def test_the_fifth_failure_alerts_and_stops(approved, inviter):
    sid, _, _ = approved("auto-five")
    sender = FakeSender(fail=-1)
    loop = inviter(sender)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        assert asyncio.run(loop.run_once()) == [(sid, "failed")], attempt
        assert len(alerts(sid)) == (0 if attempt < MAX_ATTEMPTS else 1)
        age_events(sid)
    assert asyncio.run(loop.run_once()) == []  # no sixth attempt, however long it waits
    [(source, code, context)] = alerts(sid)
    assert (source, code) == ("auto_invite", "transient")
    assert context == {"kind": "invite_failed", "signup_id": sid, "invite_id": invites(sid)[-1][0]}  # ids only
    assert sender.attempts == MAX_ATTEMPTS and all(i[3] for i in invites(sid))
    assert [e[2] for e in events(sid)].count("auto") == MAX_ATTEMPTS


def test_an_applicant_with_an_account_is_skipped_with_a_reason(approved, inviter):
    sid, _, email = approved("auto-existing")
    make_user(email)  # Q2: the manual owner-assign path, never an invitation over an account
    sender = FakeSender()
    loop = inviter(sender)
    assert asyncio.run(loop.run_once()) == [(sid, "skipped")]
    assert events(sid)[-1] == ("note", None, "auto:skipped:account_exists")
    assert asyncio.run(loop.run_once()) == []  # recorded once, not retried
    assert sender.attempts == 0 and invites(sid) == []


def test_a_claimed_creator_is_skipped(approved, inviter):
    sid, cid, _ = approved("auto-claimed")
    with db() as conn:
        conn.execute("UPDATE public.streamers SET user_id = %s WHERE id = %s",
                     (make_user("auto-claimer@authtest.invalid"), cid))
    sender = FakeSender()
    assert asyncio.run(inviter(sender).run_once()) == []
    assert sender.attempts == 0 and invites(sid) == []


def test_a_manual_invitation_takes_over_and_old_approvals_are_left_alone(approved, inviter, repo):
    manual, _, _ = approved("auto-manual")
    admin = make_user("auto-manual-boss@authtest.invalid", role="admin")
    assert asyncio.run(repo.issue_invite(manual, admin, "0" * 64, 7))[0] == "ok"
    with db() as conn:  # the admin revoked it: the system must not re-invite behind the admin's back
        conn.execute("UPDATE public.user_invites SET revoked_at = now() WHERE signup_id = %s", (manual,))
    candidate, _, _ = approved("auto-candidate")
    sender = FakeSender()
    later = datetime.now(UTC) + timedelta(days=8)  # a worker enabled more than 7 days after these approvals
    assert asyncio.run(inviter(sender, started_at=later).run_once()) == []
    assert asyncio.run(inviter(sender).run_once()) == [(candidate, "sent")]  # the manual one is the admin's


def test_two_workers_never_invite_the_same_application_twice(approved, inviter):
    sids = {approved(f"auto-race{n}")[0] for n in range(3)}
    a, b = FakeSender(), FakeSender()

    async def both():
        return await asyncio.gather(inviter(a).run_once(), inviter(b).run_once())

    for _ in range(2):  # the second round finds every application already invited
        outcomes = [o for run in asyncio.run(both()) for o in run]
        assert all(status == "sent" for _, status in outcomes)
    recipients = [m.to for m in a.sent + b.sent]
    assert len(recipients) == len(set(recipients)) == 3
    for sid in sids:
        assert len(invites(sid)) == 1 and [e[2] for e in events(sid)].count("auto") == 1


@pytest.mark.parametrize("state", ["draft", "active"])
def test_the_loop_invites_in_both_step_up_states(approved, inviter, state):
    """Q14 (migration 0038): the automatic invitation needs no passkey grant, whatever the step-up state."""
    sid, _, email = approved(f"auto-stepup-{state}")
    sender = FakeSender()
    set_enforcement(state)
    try:
        assert asyncio.run(inviter(sender).run_once()) == [(sid, "sent")]
    finally:
        set_enforcement("draft")
    assert [m.to for m in sender.sent] == [email] and alerts(sid) == []
    assert events(sid)[-1] == ("invite", None, "auto") and len(invites(sid)) == 1


@pytest.mark.parametrize("state", ["draft", "active"])
def test_a_manual_resend_needs_no_grant_and_its_link_claims_the_creator_in_both_states(app, approved, repo, state):
    """Q3 (migration 0038): the route asks for no grant, and the database no longer does either; the applicant then
    accepts and owns the creator (acceptance was already internal under 0027)."""
    from tests.admin.test_applications import _accept

    sid, cid, email = approved(f"auto-q3-{state}")
    svc = ApplicationService(repo, admin=app.app_state.admin, auth=app.app_state.auth)
    admin = SimpleNamespace(id=make_user(f"auto-q3-{state}-boss@authtest.invalid", role="admin"))
    set_enforcement(state)
    try:
        asyncio.run(svc.invite(admin, sid, None))
        again = asyncio.run(svc.invite(admin, sid, None))
        assert again["replaced_invites"] == 1
        accepted = _accept(app, app.app_state.auth.sender.last_token())
        assert accepted.status_code == 200, accepted.text
    finally:
        set_enforcement("draft")
    rows = invites(sid)
    assert [r[3] for r in rows] == [True, False] and str(rows[1][2]) == admin.id and rows[1][4] == email
    with db() as conn:
        assert conn.execute("SELECT user_id::text FROM public.streamers WHERE id = %s",
                            (cid,)).fetchone()[0] == accepted.json()["user"]["id"]
