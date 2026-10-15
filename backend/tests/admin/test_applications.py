"""KYC PR2 (migration 0021): creator applications and manual approval through the real app on the scratch database.
The public signup form creates the application; admins approve (a PENDING creator), reject, reopen, note and invite;
an application invite makes its invitee the owner of the still-unclaimed creator. Synthetic rows only."""

import asyncio
import threading

import psycopg
import pytest

from app.admin.applications import ApplicationRepository
from app.cli import connection_factory
from app.core.config import Settings
from tests.integration import (
    DOMAIN,
    ORIGIN,
    PASSWORD,
    db,
    make_streamer,
    make_user,
    new_browser,
    requires_db,
    signed_in,
)

pytestmark = requires_db
AP = "/api/v1/admin/applications"
NEW = {"razorpay_account": "primary"}


def _admin(app, name: str = "apps-boss") -> dict:
    app.app_state.admin.payment_accounts = frozenset({"primary"})
    return signed_in(app, name + DOMAIN, role="admin")


def _apply(app, local: str, **extra) -> str:
    """Submit the public signup form (unchanged contract) and return the application id."""
    body = {"name": "Asha Rao", "email": local + DOMAIN, "mobile_number": "+91 90000 00000", **extra}
    r = app.post("/api/v1/public/signups", json=body, headers=ORIGIN)
    assert (r.status_code, r.json()) == (201, {"status": "received"}), r.text
    with db() as conn:
        return conn.execute("SELECT id::text FROM public.user_signups WHERE email = %s",
                            (local + DOMAIN,)).fetchone()[0]


def _app_row(sid: str) -> tuple:
    with db() as conn:
        return conn.execute("SELECT review_status, streamer_id::text, invite_id::text, rejection_reason "
                            "FROM public.user_signups WHERE id = %s", (sid,)).fetchone()


def _events(sid: str) -> list[str]:
    with db() as conn:
        return [r[0] for r in conn.execute("SELECT action FROM public.application_events WHERE signup_id = %s "
                                           "ORDER BY created_at, id", (sid,))]


def _creator(cid: str) -> tuple:
    with db() as conn:
        return conn.execute("SELECT status, kyc_track, user_id::text, public.creator_donation_block(id) "
                            "FROM public.streamers WHERE id = %s", (cid,)).fetchone()


def _approve(app, headers, sid, slug, **extra):
    return app.post(f"{AP}/{sid}/approve", json={"slug": slug, "name": "Asha", **NEW, **extra}, headers=headers)


def _accept(app, token: str):
    return new_browser(app).post("/api/v1/auth/invites/accept", json={"token": token, "password": PASSWORD},
                                 headers=ORIGIN)


def test_the_public_form_is_unchanged_and_creates_a_submitted_application(app):
    sid = _apply(app, "apps-form")
    again = app.post("/api/v1/public/signups", json={"name": "Other", "email": "APPS-FORM" + DOMAIN,
                                                      "mobile_number": "+91 90000 00001"}, headers=ORIGIN)
    assert (again.status_code, again.json()) == (201, {"status": "received"})  # D14: identical, nothing stored
    assert _app_row(sid) == ("submitted", None, None, None)
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.users WHERE email = %s", ("apps-form" + DOMAIN,)
                            ).fetchone()[0] == 0  # no account is ever created by the form
    smuggled = app.post("/api/v1/public/signups", json={"name": "X", "email": "apps-x" + DOMAIN,
                                                         "mobile_number": "+91 90000 00002",
                                                         "review_status": "approved"}, headers=ORIGIN)
    assert smuggled.status_code == 422  # review fields are server-controlled


def test_admin_only_csrf_and_no_anonymous_access(app):
    sid = _apply(app, "apps-auth")
    assert app.get(AP).status_code == 401
    plain = new_browser(app)
    plain_headers = signed_in(plain, "apps-plain" + DOMAIN)
    assert plain.get(AP).status_code == 403
    assert plain.post(f"{AP}/{sid}/notes", json={"note": "x"}, headers=plain_headers).status_code == 403
    headers = _admin(app)
    assert app.post(f"{AP}/{sid}/notes", json={"note": "x"}, headers=ORIGIN).status_code == 403  # no CSRF token
    assert app.post(f"{AP}/{sid}/notes", json={"note": "x"},
                    headers={**headers, "origin": "https://evil.example"}).status_code == 403
    listed = app.get(AP, params={"status": "all", "limit": 200})
    assert listed.status_code == 200 and sid in {a["id"] for a in listed.json()["applications"]}
    assert app.get(AP, params={"status": "nope"}).status_code == 400
    assert app.get(f"{AP}/not-a-uuid").status_code == 404


def test_approve_creates_a_pending_creator_and_never_activates(app):
    headers = _admin(app)
    sid = _apply(app, "apps-ok")
    r = _approve(app, headers, sid, "authtest-apps-ok")
    assert r.status_code == 200, r.text
    creator = r.json()["creator"]
    assert creator["status"] == "pending" and r.json()["application"]["review_status"] == "approved"
    assert _creator(creator["id"]) == ("pending", "standard", None, "not_active")  # hidden, no donations
    assert app.get("/api/v1/public/streamers/authtest-apps-ok").status_code == 404
    assert _app_row(sid)[:2] == ("approved", creator["id"])
    again = _approve(app, headers, sid, "authtest-apps-ok2")
    assert (again.status_code, again.json()["error"]) == (409, "This application is already approved")
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.streamers WHERE streamer_slug LIKE 'authtest-apps-ok%%'"
                            ).fetchone()[0] == 1
    rejected = app.post(f"{AP}/{sid}/reject", json={"reason": "late"}, headers=headers)
    assert rejected.status_code == 409  # an approved application (with its creator) cannot be rejected
    assert _events(sid) == ["approve"]


def test_slug_conflict_and_validation_change_nothing(app):
    headers = _admin(app)
    make_streamer("authtest-apps-taken")
    sid = _apply(app, "apps-slug")
    taken = _approve(app, headers, sid, "authtest-apps-taken")
    assert (taken.status_code, taken.json()["error"]) == (409, "That slug is already used")
    assert _approve(app, headers, sid, "x").status_code == 400  # too short
    assert app.post(f"{AP}/{sid}/approve", json={"slug": "authtest-apps-s", "name": "A",
                                                 "razorpay_account": "secondary"}, headers=headers).status_code == 422
    assert _app_row(sid) == ("submitted", None, None, None) and _events(sid) == []


def test_reject_reopen_and_notes(app):
    headers = _admin(app)
    sid = _apply(app, "apps-rr")
    assert app.post(f"{AP}/{sid}/reject", json={"reason": "  "}, headers=headers).status_code == 400
    r = app.post(f"{AP}/{sid}/reject", json={"reason": "Incomplete  channel"}, headers=headers)
    assert r.status_code == 200 and r.json()["application"]["rejection_reason"] == "Incomplete channel"
    assert _approve(app, headers, sid, "authtest-apps-rr").status_code == 409  # rejected: reopen first
    assert app.post(f"{AP}/{sid}/reject", json={"reason": "x"}, headers=headers).status_code == 409
    r = app.post(f"{AP}/{sid}/reopen", json={"note": "New channel link"}, headers=headers)
    assert r.status_code == 200 and _app_row(sid) == ("submitted", None, None, None)
    assert app.post(f"{AP}/{sid}/reopen", json={}, headers=headers).status_code == 409  # only a rejected one
    note = app.post(f"{AP}/{sid}/notes", json={"note": "Called the applicant"}, headers=headers)
    assert note.status_code == 200
    assert [e["action"] for e in note.json()["events"]] == ["reject", "reopen", "note"]
    assert note.json()["events"][-1]["note"] == "Called the applicant"
    assert app.post(f"{AP}/{sid}/notes", json={}, headers=headers).status_code == 400
    assert not any("Called the applicant" in rec or "Incomplete" in rec for rec in app.app_state.log_capture.records)
    with db() as conn:  # the history is append-only for the API's role
        for sql in ("UPDATE public.application_events SET note = 'x' WHERE signup_id = %s",
                    "DELETE FROM public.application_events WHERE signup_id = %s"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql, (sid,))


def test_concurrent_approvals_create_exactly_one_creator(env):
    sid_holder = {}

    async def scenario():
        apps = ApplicationRepository(connection_factory(Settings()))
        admin = make_user("apps-race-boss" + DOMAIN, role="admin")
        with db() as conn:
            sid = conn.execute("INSERT INTO public.user_signups (name, email, mobile_number) VALUES "
                               "('R', %s, '+91 90000 00009') RETURNING id::text", ("apps-race" + DOMAIN,)).fetchone()[0]
        sid_holder["sid"] = sid
        fields = [{"streamer_slug": f"authtest-apps-race{n}", "streamer_name": "R", "razorpay_account": "primary",
                   "custom_page_path": f"/authtest-apps-race{n}"} for n in range(2)]
        return await asyncio.gather(*(apps.approve(sid, admin, f, ()) for f in fields))

    results = asyncio.run(scenario())
    assert sorted(r[0] for r in results) == ["already_approved", "ok"]
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.streamers WHERE streamer_slug LIKE 'authtest-apps-race%%'"
                            ).fetchone()[0] == 1
    assert _events(sid_holder["sid"]) == ["approve"]


def test_concurrent_approve_and_reject_serialize(env):
    async def scenario():
        apps = ApplicationRepository(connection_factory(Settings()))
        admin = make_user("apps-ar-boss" + DOMAIN, role="admin")
        with db() as conn:
            sid = conn.execute("INSERT INTO public.user_signups (name, email, mobile_number) VALUES "
                               "('R', %s, '+91 90000 00008') RETURNING id::text", ("apps-ar" + DOMAIN,)).fetchone()[0]
        fields = {"streamer_slug": "authtest-apps-ar", "streamer_name": "R", "razorpay_account": "primary",
                  "custom_page_path": "/authtest-apps-ar"}
        out = await asyncio.gather(apps.approve(sid, admin, fields, ()), apps.reject(sid, admin, "no"))
        return sid, out

    sid, out = asyncio.run(scenario())
    assert sorted(r[0] for r in out) == ["not_reviewable", "ok"]  # one decision wins, the other is refused
    status = _app_row(sid)[0]
    assert (status == "approved") == (out[0][0] == "ok") and len(_events(sid)) == 1


def test_invitation_binds_the_applicant_and_claims_only_an_unclaimed_creator(app):
    headers = _admin(app)
    sid = _apply(app, "apps-inv")
    cid = _approve(app, headers, sid, "authtest-apps-inv").json()["creator"]["id"]
    sender = app.app_state.auth.sender
    first = app.post(f"{AP}/{sid}/invite", headers=headers)
    assert first.status_code == 200 and "accept_url" not in first.json(), first.text
    assert sender.sent[-1].to == "apps-inv" + DOMAIN  # the approved applicant's own address
    old_token = sender.last_token()
    retry = app.post(f"{AP}/{sid}/invite", headers=headers)  # resend: the previous link is revoked
    assert retry.status_code == 200 and retry.json()["replaced_invites"] == 1
    token = sender.last_token()
    assert _accept(app, old_token).status_code == 400  # revoked
    assert not any(old_token in rec or token in rec for rec in app.app_state.log_capture.records)
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.application_events WHERE note LIKE %s OR note LIKE %s",
                            (f"%{old_token}%", f"%{token}%")).fetchone()[0] == 0
    accepted = _accept(app, token)
    assert accepted.status_code == 200, accepted.text
    owner = accepted.json()["user"]["id"]
    assert _creator(cid) == ("pending", "standard", owner, "not_active")  # owned, still pending: no activation
    with db() as conn:
        assert conn.execute("SELECT streamer_id::text FROM public.users WHERE id = %s", (owner,)).fetchone()[0] == cid
    assert app.post(f"{AP}/{sid}/invite", headers=headers).status_code == 409  # claimed: no more invites
    assert _events(sid) == ["approve", "invite", "invite"]


def test_failed_email_is_recoverable_without_a_second_creator(app):
    headers = _admin(app)
    sid = _apply(app, "apps-mail")
    cid = _approve(app, headers, sid, "authtest-apps-mail").json()["creator"]["id"]
    sender = app.app_state.auth.sender

    class Broken:
        async def send(self, email):
            raise RuntimeError("email provider error")

    app.app_state.auth.sender = Broken()
    failed = app.post(f"{AP}/{sid}/invite", headers=headers)
    assert (failed.status_code, failed.json()["error"]) == (
        502, "The invitation was created but the email could not be sent; retry to send a new link")
    app.app_state.auth.sender = sender
    ok = app.post(f"{AP}/{sid}/invite", headers=headers)
    assert ok.status_code == 200 and ok.json()["replaced_invites"] == 1  # the undelivered link is revoked
    assert _accept(app, sender.last_token()).status_code == 200
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.streamers WHERE streamer_slug = 'authtest-apps-mail'"
                            ).fetchone()[0] == 1
    assert _creator(cid)[0] == "pending"


def test_invites_need_an_approved_application_and_never_take_over_accounts(app):
    headers = _admin(app)
    sid = _apply(app, "apps-pre")
    assert app.post(f"{AP}/{sid}/invite", headers=headers).status_code == 409  # not approved yet
    existing = _apply(app, "apps-existing")
    make_user("apps-existing" + DOMAIN)
    cid = _approve(app, headers, existing, "authtest-apps-existing").json()["creator"]["id"]
    r = app.post(f"{AP}/{existing}/invite", headers=headers)
    assert r.status_code == 409 and "/owner" in r.json()["error"]  # explicit admin linkage instead
    with db() as conn:
        user = conn.execute("SELECT id::text FROM public.users WHERE email = %s",
                            ("apps-existing" + DOMAIN,)).fetchone()[0]
    linked = app.post(f"/api/v1/admin/creators/{cid}/owner", json={"user_id": user}, headers=headers)
    assert linked.status_code == 200 and _creator(cid)[2] == user


def test_an_application_invite_refuses_a_claimed_creator_or_a_mismatch(app):
    headers = _admin(app)
    sid = _apply(app, "apps-claimed")
    cid = _approve(app, headers, sid, "authtest-apps-claimed").json()["creator"]["id"]
    assert app.post(f"{AP}/{sid}/invite", headers=headers).status_code == 200
    token = app.app_state.auth.sender.last_token()
    other = make_user("apps-someone" + DOMAIN)
    with db() as conn:  # someone else became the owner after the invite was sent (admin linkage)
        conn.execute("UPDATE public.streamers SET user_id = %s WHERE id = %s", (other, cid))
    r = _accept(app, token)
    assert (r.status_code, r.json()["error"]) == (409, "This creator already has an owner. Please contact support.")
    with db() as conn:  # nothing consumed, no account, owner unchanged
        assert conn.execute("SELECT count(*) FROM public.users WHERE email = %s",
                            ("apps-claimed" + DOMAIN,)).fetchone()[0] == 0
        assert conn.execute("SELECT accepted_at FROM public.user_invites WHERE signup_id = %s",
                            (sid,)).fetchone()[0] is None
        conn.execute("UPDATE public.streamers SET user_id = NULL WHERE id = %s", (cid,))
        conn.execute("UPDATE public.user_invites SET email = %s WHERE signup_id = %s", ("apps-other" + DOMAIN, sid))
    assert _accept(app, token).status_code == 400  # bound to the applicant's email: no longer valid


def test_concurrent_acceptances_have_one_owner(app):
    headers = _admin(app)
    sid = _apply(app, "apps-cc")
    cid = _approve(app, headers, sid, "authtest-apps-cc").json()["creator"]["id"]
    app.post(f"{AP}/{sid}/invite", headers=headers)
    token = app.app_state.auth.sender.last_token()
    codes: list[int] = []
    threads = [threading.Thread(target=lambda: codes.append(_accept(app, token).status_code)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert sorted(codes) == [200, 400]
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.users WHERE email = %s",
                            ("apps-cc" + DOMAIN,)).fetchone()[0] == 1
    assert _creator(cid)[2] is not None


def test_a_plain_admin_invite_never_claims_a_creator(app):
    """Only invites issued for an approved application assign ownership; POST /auth/invites only links (unchanged)."""
    headers = _admin(app)
    cid = make_streamer("authtest-apps-plain")
    r = app.post("/api/v1/auth/invites", json={"email": "apps-plaininv" + DOMAIN, "streamer_id": cid},
                 headers=headers)
    assert r.status_code == 201, r.text
    assert _accept(app, app.app_state.auth.sender.last_token()).status_code == 200
    assert _creator(cid)[2] is None


def test_existing_applications_are_legacy_unreviewed_by_status_filter(app):
    _admin(app)
    with db() as conn:  # a row as the migration left it (a signup collected before manual approval)
        sid = conn.execute("INSERT INTO public.user_signups (name, email, mobile_number, review_status) VALUES "
                           "('L', %s, '+91 90000 00007', 'legacy_unreviewed') RETURNING id::text",
                           ("apps-legacy" + DOMAIN,)).fetchone()[0]
    legacy = app.get(AP, params={"status": "legacy_unreviewed", "limit": 200}).json()["applications"]
    submitted = app.get(AP, params={"limit": 200}).json()["applications"]
    assert sid in {a["id"] for a in legacy} and sid not in {a["id"] for a in submitted}
    assert app.get(f"{AP}/{sid}").json()["application"]["review_status"] == "legacy_unreviewed"


def test_database_constraints_keep_states_consistent(env):
    with db() as conn:
        sid = conn.execute("INSERT INTO public.user_signups (name, email, mobile_number) VALUES ('C', %s, "
                           "'+91 90000 00006') RETURNING id::text", ("apps-cons" + DOMAIN,)).fetchone()[0]
        for sql in ("UPDATE public.user_signups SET review_status = 'approved' WHERE id = %s",
                    "UPDATE public.user_signups SET review_status = 'rejected' WHERE id = %s",
                    "UPDATE public.user_signups SET review_status = 'other' WHERE id = %s"):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(sql, (sid,))


def test_an_application_invite_can_never_lose_its_binding(app):
    """An application invite cannot become an ordinary invite: its application cannot be deleted while it exists and
    its signup_id cannot be changed, so acceptance keeps the application's ownership and email rules."""
    headers = _admin(app)
    sid = _apply(app, "apps-bind")
    cid = _approve(app, headers, sid, "authtest-apps-bind").json()["creator"]["id"]
    assert app.post(f"{AP}/{sid}/invite", headers=headers).status_code == 200
    token = app.app_state.auth.sender.last_token()
    with db() as conn:  # the API's own role
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("DELETE FROM public.user_signups WHERE id = %s", (sid,))
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            conn.execute("UPDATE public.user_invites SET signup_id = NULL WHERE signup_id = %s", (sid,))
        other = conn.execute("INSERT INTO public.user_signups (name, email, mobile_number) VALUES ('O', %s, "
                             "'+91 90000 00005') RETURNING id", ("apps-bind-other" + DOMAIN,)).fetchone()[0]
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            conn.execute("UPDATE public.user_invites SET signup_id = %s WHERE signup_id = %s", (other, sid))
        assert conn.execute("SELECT count(*) FROM public.user_invites WHERE signup_id = %s AND accepted_at IS NULL "
                            "AND revoked_at IS NULL", (sid,)).fetchone()[0] == 1
        conn.execute("UPDATE public.streamers SET user_id = %s WHERE id = %s",
                     (make_user("apps-bind-owner" + DOMAIN), cid))
    r = _accept(app, token)  # still an application invite: an owner already there is refused, never transferred
    assert (r.status_code, r.json()["error"]) == (409, "This creator already has an owner. Please contact support.")
    with db() as conn:  # deleting the invite itself (e.g. the daily cleanup of expired ones) stays possible
        conn.execute("DELETE FROM public.user_invites WHERE signup_id = %s", (sid,))
        assert conn.execute("SELECT invite_id FROM public.user_signups WHERE id = %s", (sid,)).fetchone()[0] is None
        with pytest.raises(psycopg.errors.ForeignKeyViolation):  # the review history still protects it
            conn.execute("DELETE FROM public.user_signups WHERE id = %s", (sid,))


def test_review_history_cannot_be_erased_by_deleting_the_application(app):
    headers = _admin(app)
    reviewed = _apply(app, "apps-hist")
    assert app.post(f"{AP}/{reviewed}/reject", json={"reason": "spam"}, headers=headers).status_code == 200
    app.post(f"{AP}/{reviewed}/notes", json={"note": "keep"}, headers=headers)
    unreviewed = _apply(app, "apps-fresh")
    with db() as conn:  # the API's own role
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("DELETE FROM public.user_signups WHERE id = %s", (reviewed,))
        assert conn.execute("SELECT count(*) FROM public.application_events WHERE signup_id = %s",
                            (reviewed,)).fetchone()[0] == 2
        conn.execute("DELETE FROM public.user_signups WHERE id = %s", (unreviewed,))  # nothing to protect: allowed


def test_ordinary_invites_are_unaffected_by_the_binding_rules(app):
    headers = _admin(app)
    r = app.post("/api/v1/auth/invites", json={"email": "apps-ordinary" + DOMAIN}, headers=headers)
    assert r.status_code == 201, r.text
    with db() as conn:  # an existing-style invite (no application): revoking and deleting work as before
        conn.execute("UPDATE public.user_invites SET revoked_at = now() WHERE id = %s", (r.json()["invite_id"],))
        conn.execute("DELETE FROM public.user_invites WHERE id = %s", (r.json()["invite_id"],))
    r = app.post("/api/v1/auth/invites", json={"email": "apps-ordinary2" + DOMAIN}, headers=headers)
    assert _accept(app, app.app_state.auth.sender.last_token()).status_code == 200
