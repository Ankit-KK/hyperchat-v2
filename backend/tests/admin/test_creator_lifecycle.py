"""PR1 of manual approval + mandatory KYC (migration 0020): creator lifecycle (pending default, admin activate /
suspend / reactivate with audit) and the one donation eligibility rule (public.creator_donation_block), enforced by
the API and by the donations insert trigger. Real app, scratch database, synthetic rows only."""

import secrets
import threading
import time

import psycopg
import pytest

from app.security.crypto import sha256_hex
from tests.integration import (
    DOMAIN,
    ORIGIN,
    db,
    fixture_db,
    make_donation,
    make_streamer,
    make_user,
    new_browser,
    requires_db,
    signed_in,
)

pytestmark = requires_db
A = "/api/v1/admin/creators"
P = "/api/v1/public/streamers"
NOT_FOUND = {"error": "Streamer not found"}


def _admin(app) -> dict:
    app.app_state.admin.payment_accounts = frozenset({"primary"})
    return signed_in(app, "life-boss" + DOMAIN, role="admin")


def _status(app, headers, sid, action, expected, reason=None):
    return app.post(f"{A}/{sid}/{action}", json={"expected_status": expected, "reason": reason}, headers=headers)


def _row(sid: str) -> tuple:
    with db() as conn:
        return conn.execute("SELECT status, kyc_track, kyc_baseline_owner_id::text, user_id::text "
                            "FROM public.streamers WHERE id = %s", (sid,)).fetchone()


def _events(sid: str) -> list[tuple]:
    with db() as conn:
        return conn.execute("SELECT action, from_status, to_status, reason FROM public.creator_status_events "
                            "WHERE streamer_id = %s ORDER BY created_at", (sid,)).fetchall()


def _block(sid: str) -> str | None:
    with db() as conn:
        return conn.execute("SELECT public.creator_donation_block(%s)", (sid,)).fetchone()[0]


def _insert_donation(sid: str, conn=None) -> None:
    """A donation insert by the runtime role (in the app only checkout does this; the trigger guards every one)."""
    def run(c):
        c.execute("INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, amount_inr) "
                  "VALUES (%s, %s, 'D', 5, 'INR', 5)", (sid, "authtest-" + secrets.token_hex(6)))
    if conn is not None:
        return run(conn)
    with db() as c:
        run(c)


def _obs_token(sid: str) -> str:
    token = "obs_" + secrets.token_hex(32)
    with db() as conn:
        conn.execute("INSERT INTO public.obs_tokens (streamer_id, token_hash, is_active) VALUES (%s, %s, true)",
                     (sid, sha256_hex(token)))
    return token


def test_a_new_creator_is_pending_and_cannot_be_activated_before_kyc(app):
    headers = _admin(app)
    r = app.post(A, json={"slug": "authtest-life-new", "name": "New", "razorpay_account": "primary"}, headers=headers)
    assert r.status_code == 201 and r.json()["creator"]["status"] == "pending", r.text
    sid = r.json()["creator"]["id"]
    assert _row(sid) == ("pending", "standard", None, None) and _block(sid) == "not_active"
    assert app.get(f"{P}/authtest-life-new").json() == NOT_FOUND  # identical to an unknown creator
    refused = _status(app, headers, sid, "activate", "pending")
    assert (refused.status_code, refused.json()["error"]) == (409, "The creator has no owner yet")
    owner = make_user("life-new-owner" + DOMAIN)
    assigned = app.post(f"{A}/{sid}/owner", json={"user_id": owner}, headers=headers)
    assert assigned.status_code == 200 and assigned.json()["donation_block"] == "not_active", assigned.text
    refused = _status(app, headers, sid, "activate", "pending")  # no KYC exists yet: nobody can be activated
    assert refused.status_code == 409 and "identity verification" in refused.json()["error"], refused.text
    assert _row(sid)[0] == "pending" and _events(sid) == []
    with pytest.raises(psycopg.Error) as exc:  # the trigger: no donation for a pending creator
        _insert_donation(sid)
    assert exc.value.sqlstate == "HC001" and exc.value.diag.message_hint == "not_active"


def test_suspend_and_reactivate_keep_everything_else(app):
    headers = _admin(app)
    owner_browser = new_browser(app)
    owner_headers = signed_in(owner_browser, "life-owner" + DOMAIN)
    with db() as conn:
        owner = conn.execute("SELECT id::text FROM public.users WHERE email = %s",
                             ("life-owner" + DOMAIN,)).fetchone()[0]
    sid = make_streamer("authtest-life-s", user_id=owner)
    make_donation(sid, 250, moderation="approved")
    token = _obs_token(sid)
    with db() as conn:
        history = conn.execute("SELECT id, payment_status, moderation_status, amount FROM public.donations "
                               "WHERE streamer_id = %s", (sid,)).fetchall()
    assert _row(sid) == ("active", "legacy", owner, owner) and _block(sid) is None  # an existing creator: eligible

    assert _status(app, headers, sid, "suspend", "active").json()["error"] == "A reason is required to suspend a creator"
    stale = _status(app, headers, sid, "suspend", "suspended", "x")
    assert (stale.status_code, stale.json()["error"]) == (409, "The creator's status is not expected_status")
    r = _status(app, headers, sid, "suspend", "active", "  Chargeback   review  ")
    assert r.status_code == 200 and r.json()["creator"]["status"] == "suspended", r.text
    assert r.json()["donation_block"] == "not_active"
    assert _events(sid) == [("suspend", "active", "suspended", "Chargeback review")]
    assert not any("Chargeback" in rec for rec in app.app_state.log_capture.records)  # reason text: audit table only

    assert app.get(f"{P}/authtest-life-s").json() == NOT_FOUND  # hidden exactly like an unknown creator
    assert owner_browser.get("/api/v1/dashboard/streamers/authtest-life-s/settings",
                             headers=owner_headers).status_code == 200  # the owner keeps the dashboard
    assert app.get("/api/v1/obs/session", headers={"x-obs-token": token}).status_code == 200  # and the OBS token
    with db() as conn:
        assert conn.execute("SELECT id, payment_status, moderation_status, amount FROM public.donations "
                            "WHERE streamer_id = %s", (sid,)).fetchall() == history
    with pytest.raises(psycopg.Error) as exc:
        _insert_donation(sid)
    assert exc.value.sqlstate == "HC001"

    r = _status(app, headers, sid, "reactivate", "suspended")
    assert r.status_code == 200 and r.json()["creator"]["status"] == "active" and r.json()["donation_block"] is None
    assert [e[0] for e in _events(sid)] == ["suspend", "reactivate"]
    assert app.get(f"{P}/authtest-life-s").status_code == 200
    _insert_donation(sid)  # eligible again


def test_reactivation_is_refused_when_the_creator_could_not_accept_donations(app):
    headers = _admin(app)
    first, second = make_user("life-first" + DOMAIN), make_user("life-second" + DOMAIN)
    sid = make_streamer("authtest-life-r", user_id=first)
    assert _status(app, headers, sid, "suspend", "active", "check").status_code == 200
    with db() as conn:
        conn.execute("UPDATE public.streamers SET user_id = %s WHERE id = %s", (second, sid))  # owner changed
    r = _status(app, headers, sid, "reactivate", "suspended")
    assert (r.status_code, r.json()["error"]) == (
        409, "The creator's owner has no approved identity verification (KYC)"), r.text
    assert _row(sid)[0] == "suspended" and [e[0] for e in _events(sid)] == ["suspend"]  # nothing changed


def test_assigning_or_transferring_an_owner_stops_new_donations_until_kyc(app):
    """Owner decision 2026-10-15: an unverified new owner never accepts new donations, even before the rollout."""
    headers = _admin(app)
    unowned = make_streamer("authtest-life-u")
    assert _block(unowned) is None  # an existing unowned creator keeps working as before
    new_owner = make_user("life-u-owner" + DOMAIN)
    r = app.post(f"{A}/{unowned}/owner", json={"user_id": new_owner}, headers=headers)
    assert r.status_code == 200 and r.json()["donation_block"] == "kyc_required", r.text
    assert app.get(f"{P}/authtest-life-u").status_code == 200  # still visible; only new donations stop
    with pytest.raises(psycopg.Error) as exc:
        _insert_donation(unowned)
    assert exc.value.sqlstate == "HC001" and exc.value.diag.message_hint == "kyc_required"

    a, b = make_user("life-t-a" + DOMAIN), make_user("life-t-b" + DOMAIN)
    owned = make_streamer("authtest-life-t", user_id=a)
    with db() as conn:
        conn.execute("UPDATE public.users SET streamer_id = %s WHERE id = %s", (owned, a))
    r = app.post(f"{A}/{owned}/owner/transfer", json={"user_id": b, "expected_owner_id": a}, headers=headers)
    assert r.status_code == 200 and r.json()["changed"] and r.json()["donation_block"] == "kyc_required", r.text
    with db() as conn:  # the creator and its OBS tokens are untouched; only eligibility changed
        assert conn.execute("SELECT status FROM public.streamers WHERE id = %s", (owned,)).fetchone()[0] == "active"


def test_status_changes_are_admin_only_and_validated(app):
    headers = _admin(app)
    sid = make_streamer("authtest-life-v", user_id=make_user("life-v" + DOMAIN))
    user_browser = new_browser(app)
    user_headers = signed_in(user_browser, "life-plain" + DOMAIN)
    assert user_browser.post(f"{A}/{sid}/suspend", json={"expected_status": "active", "reason": "x"},
                             headers=user_headers).status_code == 403
    assert app.post(f"{A}/{sid}/suspend", json={"expected_status": "active", "reason": "x"},
                    headers=ORIGIN).status_code == 403  # no CSRF token
    assert app.post(f"{A}/{sid}/suspend", json={"expected_status": "active", "reason": "x", "status": "active"},
                    headers=headers).status_code == 422
    assert app.post(f"{A}/{sid}/suspend", json={"expected_status": "rejected", "reason": "x"},
                    headers=headers).status_code == 422
    bad = _status(app, headers, sid, "activate", "active")
    assert (bad.status_code, bad.json()["error"]) == (409, "Cannot activate a creator whose status is active")
    assert _status(app, headers, "00000000-0000-4000-8000-000000000000", "suspend", "active", "x").status_code == 404
    assert _status(app, headers, "not-a-uuid", "suspend", "active", "x").status_code == 404
    assert _row(sid)[0] == "active" and _events(sid) == []


def test_runtime_roles_cannot_touch_the_kyc_columns_or_the_audit(app):
    sid = make_streamer("authtest-life-k", user_id=make_user("life-k" + DOMAIN))
    with db() as conn:
        for sql, args in (
                ("INSERT INTO public.streamers (streamer_slug, streamer_name, kyc_track) "
                 "VALUES ('authtest-life-k2', 'K', 'legacy')", ()),
                ("INSERT INTO public.streamers (streamer_slug, streamer_name, kyc_baseline_owner_id) "
                 "VALUES ('authtest-life-k3', 'K', gen_random_uuid())", ()),
                ("UPDATE public.streamers SET kyc_track = 'standard' WHERE id = %s", (sid,)),
                ("UPDATE public.streamers SET kyc_baseline_owner_id = NULL WHERE id = %s", (sid,))):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql, args)
        assert conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name) VALUES ('authtest-life-k4', "
                            "'K') RETURNING status, kyc_track").fetchone() == ("pending", "standard")
        conn.execute("INSERT INTO public.creator_status_events (streamer_id, action, from_status, to_status) "
                     "VALUES (%s, 'suspend', 'active', 'suspended')", (sid,))
        for sql in ("UPDATE public.creator_status_events SET reason = 'x' WHERE streamer_id = %s",
                    "DELETE FROM public.creator_status_events WHERE streamer_id = %s"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql, (sid,))
    with fixture_db() as conn:  # historical rows (the legacy import) are not refused by the trigger
        conn.execute("UPDATE public.streamers SET status = 'suspended' WHERE id = %s", (sid,))
        conn.execute("INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, amount_inr) "
                     "VALUES (%s, %s, 'Imported', 5, 'INR', 5)", (sid, "authtest-" + secrets.token_hex(6)))


def test_a_donation_insert_waits_for_a_concurrent_suspension_and_is_then_refused(app):
    sid = make_streamer("authtest-life-c", user_id=make_user("life-c" + DOMAIN))
    outcome: list = []
    boss = make_user("life-c-boss" + DOMAIN, role="admin")
    with db("hyperchat_lifecycle") as admin_conn:  # the admin API's transition connection
        admin_conn.autocommit = False
        admin_conn.execute("SELECT public.kyc_change_creator_status(%s, 'suspend', 'active', %s, 'race', NULL)",
                           (sid, boss))  # row locked

        def checkout():
            try:
                _insert_donation(sid)
                outcome.append("inserted")
            except psycopg.Error as exc:
                outcome.append((exc.sqlstate, exc.diag.message_hint))

        worker = threading.Thread(target=checkout)
        worker.start()
        time.sleep(0.5)
        assert worker.is_alive() and outcome == []  # blocked on the creator row, not decided on stale data
        admin_conn.commit()
    worker.join(10)
    assert outcome == [("HC001", "not_active")]
