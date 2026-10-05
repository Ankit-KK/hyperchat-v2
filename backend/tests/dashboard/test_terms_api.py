"""G5 terms of service: status (D-R7) and acceptance (D-M5) against a scratch database."""

import pytest

from tests.integration import DOMAIN, ORIGIN, db, make_streamer, new_browser, requires_db, signed_in

pytestmark = requires_db
T = "/api/v1/me/terms"
CURRENT = "2026-07-03"


def _rows(email: str) -> list[tuple]:
    with db() as conn:
        return conn.execute("SELECT t.accepted_version, t.user_agent FROM public.terms_acceptances t "
                            "JOIN public.users u ON u.id = t.user_id WHERE u.email = %s ORDER BY t.accepted_at",
                            (email,)).fetchall()


def test_status_then_accept_is_idempotent(app):
    email = "creator" + DOMAIN
    headers = signed_in(app, email)
    r = app.get(T)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == {"current_version": CURRENT, "accepted": False, "is_admin": False}
    for _ in range(2):
        r = app.post(f"{T}/accept", json={"version": CURRENT}, headers={**headers, "user-agent": "authtest-browser/1.0"})
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        assert r.json() == {"accepted": True, "current_version": CURRENT}
    assert _rows(email) == [(CURRENT, "authtest-browser/1.0")]  # one row, however often it is accepted
    assert app.get(T).json() == {"current_version": CURRENT, "accepted": True, "is_admin": False}
    assert sum("terms_accepted" in rec for rec in app.app_state.log_capture.records) == 1


@pytest.mark.parametrize("version", ["2025-01-01", "2026-07-04", "", "latest"])
def test_only_the_server_version_can_be_accepted(app, version):
    email = "picky" + DOMAIN
    headers = signed_in(app, email)
    r = app.post(f"{T}/accept", json={"version": version}, headers=headers)
    assert (r.status_code, r.json()) == (409, {"error": "Terms version is not current"})
    assert r.headers["cache-control"] == "no-store" and _rows(email) == []


def test_an_older_acceptance_does_not_count(app):
    email = "old" + DOMAIN
    signed_in(app, email)
    with db() as conn:
        conn.execute("INSERT INTO public.terms_acceptances (user_id, accepted_version) SELECT id, '2025-01-01' "
                     "FROM public.users WHERE email = %s", (email,))
    assert app.get(T).json()["accepted"] is False


def test_admins_bypass_the_gate(app):
    headers = signed_in(app, "boss" + DOMAIN, role="admin")
    assert app.get(T).json() == {"current_version": CURRENT, "accepted": True, "is_admin": True}
    assert app.post(f"{T}/accept", json={"version": CURRENT}, headers=headers).json()["accepted"] is True


@pytest.mark.parametrize("body", [{}, {"version": 20260703}, {"version": "x" * 41}, {"version": CURRENT, "user_id": "x"},
                                  {"version": CURRENT, "accepted_version": "2099-01-01"}])
def test_accept_body_validation(app, body):
    email = "body" + DOMAIN
    headers = signed_in(app, email)
    r = app.post(f"{T}/accept", json=body, headers=headers)
    assert r.status_code == 422 and r.headers["cache-control"] == "no-store" and _rows(email) == []


def test_terms_need_a_session_and_csrf(app):
    anonymous = new_browser(app)
    for method, url in (("GET", T), ("POST", f"{T}/accept")):
        r = anonymous.request(method, url, json={"version": CURRENT}, headers=ORIGIN)
        assert (r.status_code, r.json()) == (401, {"error": "Not signed in"}) and r.headers["cache-control"] == "no-store"
    email = "csrf" + DOMAIN
    headers = signed_in(app, email)
    for bad in (ORIGIN, {**headers, "x-csrf-token": "wrong"}, {**headers, "origin": "https://evil.invalid"}):
        assert app.post(f"{T}/accept", json={"version": CURRENT}, headers=bad).status_code == 403
    assert _rows(email) == []
    with db() as conn:
        conn.execute("UPDATE public.users SET is_active = false WHERE email = %s", (email,))
    assert app.get(T).status_code == 401


def test_terms_do_not_gate_the_dashboard_api(app):
    """A9: acceptance stays a client-side gate; the server does not block dashboard calls."""
    headers = signed_in(app, "fresh" + DOMAIN)
    with db() as conn:
        owner = conn.execute("SELECT id::text FROM public.users WHERE email = %s", ("fresh" + DOMAIN,)).fetchone()[0]
    make_streamer("authtest-terms", user_id=owner)
    assert app.get(T).json()["accepted"] is False
    assert app.get("/api/v1/dashboard/streamers/authtest-terms/settings").status_code == 200
    r = app.patch("/api/v1/dashboard/streamers/authtest-terms/settings", json={"goal_overlay_id": 1}, headers=headers)
    assert r.status_code == 200


def test_user_agent_is_truncated(app):
    email = "agent" + DOMAIN
    headers = signed_in(app, email)
    app.post(f"{T}/accept", json={"version": f" {CURRENT} "}, headers={**headers, "user-agent": "a" * 900})
    assert _rows(email) == [(CURRENT, "a" * 500)]
