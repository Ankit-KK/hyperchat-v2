"""Admin user / invite management through the real app (scratch database, see tests/integration.py)."""

import asyncio

import pytest

from app.admin.repository import AdminRepository, LastAdminError
from app.cli import connection_factory
from app.core.config import Settings
from tests.integration import (
    DOMAIN,
    ORIGIN,
    db,
    login,
    make_streamer,
    make_user,
    new_browser,
    requires_db,
    signed_in,
)

pytestmark = requires_db
U = "/api/v1/admin/users"


def _admin(app) -> dict:
    return signed_in(app, "boss" + DOMAIN, role="admin")


def _id(email: str) -> str:
    with db() as conn:
        return conn.execute("SELECT id::text FROM public.users WHERE email = %s", (email,)).fetchone()[0]


def test_admin_endpoints_need_an_admin_session_and_csrf(app):
    target = make_user("target" + DOMAIN)
    anonymous = new_browser(app)
    assert anonymous.get(U).status_code == 401
    plain = new_browser(app)
    headers = signed_in(plain, "plain" + DOMAIN)
    assert plain.get(U).status_code == 403
    assert plain.patch(f"{U}/{target}", json={"role": "admin"}, headers=headers).status_code == 403
    assert plain.get("/api/v1/admin/invites").status_code == 403
    admin_headers = _admin(app)
    assert app.patch(f"{U}/{target}", json={"is_active": False}, headers=ORIGIN).status_code == 403  # no CSRF
    assert app.patch(f"{U}/{target}", json={"is_active": False},
                     headers={**admin_headers, "origin": "https://evil.invalid"}).status_code == 403
    with db() as conn:
        assert conn.execute("SELECT is_active FROM public.users WHERE id = %s", (target,)).fetchone()[0] is True


def test_list_users_filters_and_never_exposes_hashes(app):
    _admin(app)
    make_user("alice_list" + DOMAIN)
    make_user("bob_list" + DOMAIN, role="user", password=None)
    r = app.get(U, params={"q": "_list" + DOMAIN})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and [u["email"] for u in body["users"]] == ["alice_list" + DOMAIN, "bob_list" + DOMAIN]
    bob = body["users"][1]
    assert bob["has_password"] is False and bob["role"] == "user" and bob["is_active"] is True
    assert "password_hash" not in r.text and "$argon2" not in r.text
    assert app.get(U, params={"q": "list" + DOMAIN, "role": "user"}).json()["total"] == 1
    assert app.get(U, params={"q": "%"}).json()["total"] == 0  # LIKE wildcards are escaped
    assert app.get(U, params={"role": "superuser"}).status_code == 400
    assert app.get(U, params={"limit": 500}).status_code == 422


def test_role_change_revokes_target_sessions(app):
    headers = _admin(app)
    target_browser = new_browser(app)
    signed_in(target_browser, "promote" + DOMAIN)
    target = _id("promote" + DOMAIN)
    assert target_browser.get("/api/v1/auth/session").status_code == 200
    r = app.patch(f"{U}/{target}", json={"role": "admin"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == ["role"] and r.json()["sessions_revoked"] == 1 and r.json()["user"]["role"] == "admin"
    assert target_browser.get("/api/v1/auth/session").status_code == 401  # must sign in again
    again = login(target_browser, "promote" + DOMAIN)
    assert again.json()["user"]["role"] == "admin"
    same = app.patch(f"{U}/{target}", json={"role": "admin"}, headers=headers)
    assert same.json()["changed"] == [] and same.json()["sessions_revoked"] == 0


def test_deactivate_blocks_login_and_reactivate_restores_it(app):
    headers = _admin(app)
    browser = new_browser(app)
    signed_in(browser, "victim" + DOMAIN)
    victim = _id("victim" + DOMAIN)
    assert app.patch(f"{U}/{victim}", json={"is_active": False}, headers=headers).json()["sessions_revoked"] == 1
    assert browser.get("/api/v1/auth/session").status_code == 401
    assert login(new_browser(app), "victim" + DOMAIN).status_code == 401
    assert app.patch(f"{U}/{victim}", json={"is_active": True}, headers=headers).status_code == 200
    assert login(new_browser(app), "victim" + DOMAIN).status_code == 200


def test_admin_cannot_change_own_role_or_status(app):
    headers = _admin(app)
    me = _id("boss" + DOMAIN)
    for change in ({"role": "user"}, {"is_active": False}):
        r = app.patch(f"{U}/{me}", json=change, headers=headers)
        assert r.status_code == 403 and r.json() == {"error": "You cannot change your own role or status"}
    assert app.get("/api/v1/auth/session").status_code == 200


def test_streamer_link_unlink_and_validation(app):
    headers = _admin(app)
    sid = make_streamer("authtest-link")
    user = make_user("member" + DOMAIN)
    r = app.patch(f"{U}/{user}", json={"streamer_id": sid}, headers=headers)
    assert r.status_code == 200 and r.json()["user"]["streamer_id"] == sid
    r = app.patch(f"{U}/{user}", json={"streamer_id": None}, headers=headers)
    assert r.status_code == 200 and r.json()["user"]["streamer_id"] is None
    bad = [({"streamer_id": "00000000-0000-4000-8000-000000000000"}, 400), ({"streamer_id": "nope"}, 400),
           ({"role": "superuser"}, 422), ({"role": None}, 400), ({}, 400), ({"password_hash": "x"}, 422)]
    for body, status in bad:
        assert app.patch(f"{U}/{user}", json=body, headers=headers).status_code == status, body
    assert app.patch(f"{U}/not-a-uuid", json={"role": "user"}, headers=headers).status_code == 404
    assert app.patch(f"{U}/00000000-0000-4000-8000-000000000000", json={"role": "user"},
                     headers=headers).status_code == 404


def test_force_logout(app):
    headers = _admin(app)
    browser = new_browser(app)
    signed_in(browser, "kick" + DOMAIN)
    r = app.post(f"{U}/{_id('kick' + DOMAIN)}/sessions/revoke", headers=headers)
    assert r.status_code == 200 and r.json() == {"sessions_revoked": 1}
    assert browser.get("/api/v1/auth/session").status_code == 401


def test_invite_list_and_revoke(app):
    headers = _admin(app)
    assert app.post("/api/v1/auth/invites", json={"email": "invitee" + DOMAIN}, headers=headers).status_code == 201
    token = app.app_state.auth.sender.last_token()
    pending = app.get("/api/v1/admin/invites").json()["invites"]
    mine = [i for i in pending if i["email"] == "invitee" + DOMAIN]
    assert len(mine) == 1 and mine[0]["status"] == "pending" and "token" not in str(mine[0]).lower()
    invite_id = mine[0]["id"]
    assert app.post(f"/api/v1/admin/invites/{invite_id}/revoke", headers=headers).json() == {"success": True}
    again = app.post(f"/api/v1/admin/invites/{invite_id}/revoke", headers=headers)
    assert again.status_code == 409 and again.json() == {"error": "Invite is already revoked"}
    accept = new_browser(app).post("/api/v1/auth/invites/accept", headers=ORIGIN,
                                   json={"token": token, "password": "Invited-Pass-42"})
    assert accept.status_code == 400
    assert [i["status"] for i in app.get("/api/v1/admin/invites", params={"status": "all"}).json()["invites"]
            if i["id"] == invite_id] == ["revoked"]
    assert app.post("/api/v1/admin/invites/not-a-uuid/revoke", headers=headers).status_code == 404
    assert app.get("/api/v1/admin/invites", params={"status": "weird"}).status_code == 400


def test_changes_are_audited_without_secrets(app):
    headers = _admin(app)
    target = make_user("audited" + DOMAIN)
    app.patch(f"{U}/{target}", json={"role": "user"}, headers=headers)
    records = app.app_state.log_capture.records
    assert any("user_updated" in r and target in r for r in records)
    assert all(app.cookies.get("__Host-hc_session") not in r for r in records)


def test_last_sign_in_admin_cannot_be_removed_even_concurrently(strict_scratch_db, env):
    """The API forbids self-demotion; the repository additionally guarantees one sign-in admin remains."""
    only_admin = make_user("onlyadmin" + DOMAIN, role="admin")
    make_user("nopassadmin" + DOMAIN, role="admin", password=None)  # cannot sign in: does not count
    repo = AdminRepository(connection_factory(Settings()))
    with pytest.raises(LastAdminError):
        asyncio.run(repo.update_user(only_admin, {"role": "user"}))
    with pytest.raises(LastAdminError):
        asyncio.run(repo.update_user(only_admin, {"is_active": False}))
    second = make_user("secondadmin" + DOMAIN, role="admin")
    user, changed, _ = asyncio.run(repo.update_user(only_admin, {"role": "user"}))
    assert changed == ["role"] and user["role"] == "user"
    with pytest.raises(LastAdminError):
        asyncio.run(repo.update_user(second, {"is_active": False}))
