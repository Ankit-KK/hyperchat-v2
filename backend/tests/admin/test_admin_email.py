"""D11: admin email correction (PUT /api/v1/admin/users/{id}/email) through the real app (scratch database)."""

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest

from app.admin.repository import AdminRepository, LastAdminError
from app.auth.google import GoogleIdentity
from app.cli import connection_factory
from app.core.config import Settings
from tests.integration import DOMAIN, ORIGIN, db, fixture_db, login, make_user, new_browser, requires_db, signed_in

pytestmark = requires_db
U = "/api/v1/admin/users"
OLD, NEW = "d11-old" + DOMAIN, "d11-new" + DOMAIN


def _url(user_id: str) -> str:
    return f"{U}/{user_id}/email"


def _admin(app) -> dict:
    return signed_in(app, "d11-boss" + DOMAIN, role="admin")


def _row(user_id: str) -> tuple:
    with db() as conn:
        return conn.execute("""
            SELECT u.email::text, u.email_verified_at IS NOT NULL,
                   (SELECT count(*) FROM public.sessions s WHERE s.user_id = u.id AND s.revoked_at IS NULL),
                   (SELECT count(*) FROM public.password_reset_tokens t WHERE t.user_id = u.id AND t.used_at IS NULL),
                   (SELECT count(*) FROM public.user_identities i WHERE i.user_id = u.id)
            FROM public.users u WHERE u.id = %s""", (user_id,)).fetchone()


def _target(app, email: str = OLD, subject: str = "d11-sub-old"):
    """A signed-in user with a verified email, an open reset link and a linked Google identity."""
    browser = new_browser(app)
    signed_in(browser, email)
    with db() as conn:
        user_id = conn.execute("UPDATE public.users SET email_verified_at = now() WHERE email = %s RETURNING id::text",
                               (email,)).fetchone()[0]
        conn.execute("INSERT INTO public.user_identities (user_id, provider, provider_subject, email) "
                     "VALUES (%s, 'google', %s, %s)", (user_id, subject, email))
    assert new_browser(app).post("/api/v1/auth/password/forgot", json={"email": email}).status_code == 202
    reset_token = app.app_state.auth.sender.last_token()
    assert _row(user_id) == (email, True, 1, 1, 1)
    return user_id, browser, reset_token


def _google(app, client, identity: GoogleIdentity) -> str:
    """Sign `client` in with Google as `identity`; returns where the callback redirected."""
    app.app_state.auth.google.identity = identity
    r = client.get("/api/v1/auth/google/start", params={"redirect": "/dashboard"}, follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = client.get("/api/v1/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    return r.headers["location"]


def test_admin_corrects_an_email_and_the_old_holder_loses_every_way_in(app):
    headers = _admin(app)
    user_id, browser, reset_token = _target(app)
    r = app.put(_url(user_id), json={"email": "  D11-New" + DOMAIN.upper() + " "}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["changed"], body["sessions_revoked"], body["reset_tokens_burned"], body["identities_removed"]) \
        == (True, 1, 1, 1)
    assert body["user"]["email"] == NEW and body["user"]["id"] == user_id
    assert "password_hash" not in r.text and "$argon2" not in r.text and reset_token not in r.text
    assert _row(user_id) == (NEW, False, 0, 0, 0)  # normalized, unverified, nothing left open
    # Old session, old reset link, old email and the old Google subject: all refused.
    assert browser.get("/api/v1/auth/session").status_code == 401
    reset = new_browser(app).post("/api/v1/auth/password/reset",
                                  json={"token": reset_token, "new_password": "Taken-Over-42"})
    assert reset.status_code == 400
    assert login(new_browser(app), OLD).status_code == 401
    assert login(new_browser(app), NEW).status_code == 200  # the password is unchanged
    assert _google(app, new_browser(app), GoogleIdentity("d11-sub-old", OLD, True)).endswith("error=no_account")
    # The rightful owner re-links through the normal verified-email Google sign-in.
    relinked = new_browser(app)
    assert "error=" not in _google(app, relinked, GoogleIdentity("d11-sub-new", NEW, True))
    assert relinked.get("/api/v1/auth/session").json()["user"]["id"] == user_id
    with db() as conn:
        assert conn.execute("SELECT provider_subject FROM public.user_identities WHERE user_id = %s",
                            (user_id,)).fetchall() == [("d11-sub-new",)]


def test_access_csrf_and_self_change(app):
    target = make_user(OLD)
    anonymous = new_browser(app)
    assert anonymous.put(_url(target), json={"email": NEW}, headers=ORIGIN).status_code == 401
    plain = new_browser(app)
    plain_headers = signed_in(plain, "d11-plain" + DOMAIN)
    assert plain.put(_url(target), json={"email": NEW}, headers=plain_headers).status_code == 403
    headers = _admin(app)
    assert app.put(_url(target), json={"email": NEW}, headers=ORIGIN).status_code == 403  # no CSRF token
    assert app.put(_url(target), json={"email": NEW},
                   headers={**headers, "origin": "https://evil.invalid"}).status_code == 403
    boss = _row_id("d11-boss" + DOMAIN)
    r = app.put(_url(boss), json={"email": "d11-boss2" + DOMAIN}, headers=headers)
    assert (r.status_code, r.json()) == (403, {"error": "You cannot change your own email"})
    assert _row(target)[0] == OLD and _row(boss)[0] == "d11-boss" + DOMAIN
    assert app.get("/api/v1/auth/session").status_code == 200  # the admin's own session is untouched


def _row_id(email: str) -> str:
    with db() as conn:
        return conn.execute("SELECT id::text FROM public.users WHERE email = %s", (email,)).fetchone()[0]


@pytest.mark.parametrize("body, code", [
    ({"email": "no-at-sign" + DOMAIN.replace("@", ".")}, 400),
    ({"email": "two@at@" + DOMAIN[1:]}, 400),
    ({"email": "@" + DOMAIN[1:]}, 400),
    ({"email": "d11-x@"}, 400),
    ({"email": "d11 space" + DOMAIN}, 400),
    ({"email": "d11-tab\t" + DOMAIN}, 400),
    ({"email": "d11-ctl\x07" + DOMAIN}, 400),
    ({"email": "d11-nbsp " + DOMAIN}, 400),
    ({"email": "   "}, 400),
    ({"email": "d" * (255 - len(DOMAIN)) + DOMAIN}, 400),  # 255 characters
    ({"email": "d" * 400 + DOMAIN}, 422),
    ({"email": NEW, "role": "admin"}, 422),  # unknown fields are refused
    ({"email": NEW, "email_verified_at": "2026-01-01"}, 422),
    ({"email": 123}, 422),
    ({}, 422),
])
def test_validation_changes_nothing(app, body, code):
    headers = _admin(app)
    user_id, _, _ = _target(app)
    assert app.put(_url(user_id), json=body, headers=headers).status_code == code
    assert _row(user_id) == (OLD, True, 1, 1, 1)


def test_254_characters_is_accepted_and_unknown_users_are_404(app):
    headers = _admin(app)
    target = make_user(OLD)
    longest = "d" * (254 - len(DOMAIN)) + DOMAIN
    assert app.put(_url(target), json={"email": longest}, headers=headers).json()["user"]["email"] == longest
    for bad in ("00000000-0000-4000-8000-000000000000", "not-a-uuid"):
        assert app.put(_url(bad), json={"email": NEW}, headers=headers).status_code == 404


@pytest.mark.parametrize("taken", [NEW, "D11-NEW" + DOMAIN])  # the domain stays lowercase for cleanup
def test_an_address_used_by_another_account_is_refused(app, taken):
    headers = _admin(app)
    user_id, browser, _ = _target(app)
    make_user(taken)
    r = app.put(_url(user_id), json={"email": NEW}, headers=headers)
    assert (r.status_code, r.json()) == (409, {"error": "Another account already uses this email"})
    assert _row(user_id) == (OLD, True, 1, 1, 1)
    assert browser.get("/api/v1/auth/session").status_code == 200


def test_the_same_address_ignoring_case_is_a_no_op(app):
    headers = _admin(app)
    user_id, browser, reset_token = _target(app)
    r = app.put(_url(user_id), json={"email": " " + OLD.upper()}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert (body["changed"], body["sessions_revoked"], body["reset_tokens_burned"], body["identities_removed"]) \
        == (False, 0, 0, 0)
    assert _row(user_id) == (OLD, True, 1, 1, 1)  # verification, session, reset link and Google link all kept
    assert browser.get("/api/v1/auth/session").status_code == 200
    assert not any("user_email_changed" in r for r in app.app_state.log_capture.records)


def test_a_pending_invite_for_the_new_address_blocks_the_change(app):
    headers = _admin(app)
    user_id, browser, _ = _target(app)
    assert app.post("/api/v1/auth/invites", json={"email": NEW}, headers=headers).status_code == 201
    r = app.put(_url(user_id), json={"email": NEW}, headers=headers)
    assert (r.status_code, r.json()) == (409, {"error": "A pending invite exists for this email"})
    assert _row(user_id) == (OLD, True, 1, 1, 1)
    assert browser.get("/api/v1/auth/session").status_code == 200
    invite = [i for i in app.get("/api/v1/admin/invites").json()["invites"] if i["email"] == NEW][0]
    app.post(f"/api/v1/admin/invites/{invite['id']}/revoke", headers=headers)  # a revoked invite no longer blocks
    assert app.put(_url(user_id), json={"email": NEW}, headers=headers).json()["changed"] is True


def test_the_change_is_audited_without_either_address(app):
    headers = _admin(app)
    user_id, _, _ = _target(app)
    app.put(_url(user_id), json={"email": NEW}, headers=headers)
    records = app.app_state.log_capture.records
    assert [r for r in records if "user_email_changed" in r and user_id in r and "'count': 1" in r]
    assert not [r for r in records if OLD in r or NEW in r]


def test_a_google_only_last_admin_keeps_a_way_in(strict_scratch_db, env):
    """Removing the identities of an admin who can only sign in with Google needs another sign-in admin."""
    google_admin = make_user("d11-gadmin" + DOMAIN, role="admin", password=None)
    make_user("d11-nopass" + DOMAIN, role="admin", password=None)  # cannot sign in: does not count
    with fixture_db() as conn:  # fixture setup: an admin's identity is written by hyperchat_auth only (0024)
        conn.execute("INSERT INTO public.user_identities (user_id, provider, provider_subject) "
                     "VALUES (%s, 'google', 'd11-sub-admin')", (google_admin,))
    repo = AdminRepository(connection_factory(Settings()), auth_factory=connection_factory(Settings(), "auth"))
    with pytest.raises(LastAdminError):
        asyncio.run(repo.change_email(google_admin, NEW))
    assert _row(google_admin) == ("d11-gadmin" + DOMAIN, False, 0, 0, 1)
    make_user("d11-second" + DOMAIN, role="admin")
    result, user, counts = asyncio.run(repo.change_email(google_admin, NEW))
    assert (result, user["email"], counts["identities_removed"]) == ("ok", NEW, 1)


def test_two_users_racing_for_one_address_have_one_winner(env):
    users = [make_user(f"d11-racer{i}" + DOMAIN) for i in range(4)]
    repo = AdminRepository(connection_factory(Settings()), auth_factory=connection_factory(Settings(), "auth"))

    async def race():
        return await asyncio.gather(*(repo.change_email(u, NEW) for u in users))
    results = [r for r, _, _ in asyncio.run(race())]
    assert sorted(results) == ["email_taken", "email_taken", "email_taken", "ok"]
    emails = sorted(_row(u)[0] for u in users)
    assert emails == sorted([NEW] + [f"d11-racer{i}" + DOMAIN for i in range(4) if results[i] != "ok"])
