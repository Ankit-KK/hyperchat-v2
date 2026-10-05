"""End-to-end auth through the real app (create_app + lifespan) against a scratch database.

See tests/integration.py for how the database is selected and which rows are synthetic. No email or
Google call leaves the process: the sender and the Google client are in-memory fakes.
"""

from urllib.parse import parse_qs, urlparse

import bcrypt

from app.auth.google import GoogleIdentity
from app.auth.tokens import sha256_hex
from tests.integration import DOMAIN, FRONT, ORIGIN, PASSWORD, requires_db
from tests.integration import csrf_headers as _csrf
from tests.integration import db as _db
from tests.integration import login as _login
from tests.integration import make_user as _user
from tests.integration import new_browser as _new_client

pytestmark = requires_db


# ── login / session / cookie ────────────────────────────────────────────────────


def test_login_sets_hardened_cookie_and_stores_only_hashes(app):
    _user("login" + DOMAIN)
    r = _login(app, "LOGIN" + DOMAIN)  # citext: case-insensitive email
    assert r.status_code == 200, r.text
    assert r.json()["user"]["email"] == "login" + DOMAIN and r.json()["user"]["role"] == "streamer"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-hc_session=")
    low = cookie.lower()
    assert "secure" in low and "httponly" in low and "samesite=lax" in low and "path=/" in low
    assert "domain=" not in low
    token = app.cookies.get("__Host-hc_session")
    with _db() as conn:
        rows = conn.execute("SELECT token_hash, csrf_token_hash FROM public.sessions s JOIN public.users u "
                            "ON u.id = s.user_id WHERE u.email = %s", ("login" + DOMAIN,)).fetchall()
    assert rows == [(sha256_hex(token), sha256_hex(r.json()["csrf_token"]))]
    s = app.get("/api/v1/auth/session")
    assert s.status_code == 200 and s.json()["csrf_token"] == r.json()["csrf_token"]


def test_wrong_password_and_unknown_email_get_the_same_generic_error(app):
    _user("generic" + DOMAIN)
    a = _login(app, "generic" + DOMAIN, "wrong-password")
    b = _login(app, "nobody" + DOMAIN, "wrong-password")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json() == {"error": "Invalid email or password"}
    assert "set-cookie" not in a.headers


def test_login_requires_allowed_origin(app):
    _user("origin" + DOMAIN)
    assert _login(app, "origin" + DOMAIN, headers={}).status_code == 403
    assert _login(app, "origin" + DOMAIN, headers={"origin": "https://evil.invalid"}).status_code == 403


def test_lockout_after_five_failures_even_with_the_right_password(app):
    _user("lock" + DOMAIN)
    for _ in range(5):
        assert _login(app, "lock" + DOMAIN, "wrong-password").status_code == 401
    r = _login(app, "lock" + DOMAIN)
    assert r.status_code == 429 and r.json() == {"error": "Too many attempts. Please try again later."}
    with _db() as conn:
        conn.execute("UPDATE public.users SET locked_until = now() - interval '1 second' WHERE email = %s",
                     ("lock" + DOMAIN,))
    assert _login(app, "lock" + DOMAIN).status_code == 200
    with _db() as conn:
        assert conn.execute("SELECT failed_login_attempts, locked_until FROM public.users WHERE email = %s",
                            ("lock" + DOMAIN,)).fetchone() == (0, None)


def test_legacy_bcrypt_hash_logs_in_and_is_upgraded_to_argon2id(app):
    legacy = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
    _user("bcrypt" + DOMAIN, hashed=legacy)
    assert _login(app, "bcrypt" + DOMAIN).status_code == 200
    with _db() as conn:
        stored = conn.execute("SELECT password_hash FROM public.users WHERE email = %s",
                              ("bcrypt" + DOMAIN,)).fetchone()[0]
    assert stored.startswith("$argon2id$")
    assert _login(_new_client(app), "bcrypt" + DOMAIN).status_code == 200


def test_account_without_password_or_disabled_cannot_log_in(app):
    _user("nopass" + DOMAIN, password=None)
    _user("disabled" + DOMAIN)
    with _db() as conn:
        conn.execute("UPDATE public.users SET is_active = false WHERE email = %s", ("disabled" + DOMAIN,))
    assert _login(app, "nopass" + DOMAIN, "").status_code in (401, 422)
    assert _login(app, "nopass" + DOMAIN, "anything").status_code == 401
    assert _login(app, "disabled" + DOMAIN).status_code == 401


# ── CSRF / logout ───────────────────────────────────────────────────────────────


def test_state_changes_need_origin_and_csrf_token(app):
    _user("csrf" + DOMAIN)
    r = _login(app, "csrf" + DOMAIN)
    assert app.post("/api/v1/auth/logout").status_code == 403
    assert app.post("/api/v1/auth/logout", headers=ORIGIN).status_code == 403
    assert app.post("/api/v1/auth/logout", headers={**ORIGIN, "x-csrf-token": "0" * 64}).status_code == 403
    bad_origin = {"origin": "https://evil.invalid", "x-csrf-token": r.json()["csrf_token"]}
    assert app.post("/api/v1/auth/logout", headers=bad_origin).status_code == 403
    assert app.get("/api/v1/auth/session").status_code == 200  # still signed in
    out = app.post("/api/v1/auth/logout", headers=_csrf(r))
    assert out.status_code == 200 and "__Host-hc_session=" in out.headers["set-cookie"]
    assert app.get("/api/v1/auth/session").status_code == 401


def test_revoked_session_token_is_rejected_even_if_replayed(app):
    _user("replay" + DOMAIN)
    r = _login(app, "replay" + DOMAIN)
    token = app.cookies.get("__Host-hc_session")
    app.post("/api/v1/auth/logout", headers=_csrf(r))
    replay = _new_client(app)
    assert replay.get("/api/v1/auth/session", headers={"cookie": f"__Host-hc_session={token}"}).status_code == 401


def test_logout_all_revokes_every_session(app):
    _user("all" + DOMAIN)
    other = _new_client(app)
    assert _login(other, "all" + DOMAIN).status_code == 200
    r = _login(app, "all" + DOMAIN)
    out = app.post("/api/v1/auth/logout-all", headers=_csrf(r))
    assert out.status_code == 200 and out.json()["sessions_revoked"] == 2
    assert other.get("/api/v1/auth/session").status_code == 401
    assert app.get("/api/v1/auth/session").status_code == 401


def test_expired_session_is_rejected(app):
    _user("expired" + DOMAIN)
    _login(app, "expired" + DOMAIN)
    with _db() as conn:
        conn.execute("UPDATE public.sessions SET idle_expires_at = now() - interval '1 second' WHERE user_id = "
                     "(SELECT id FROM public.users WHERE email = %s)", ("expired" + DOMAIN,))
    assert app.get("/api/v1/auth/session").status_code == 401


# ── password reset / change ─────────────────────────────────────────────────────


def test_forgot_password_never_reveals_whether_the_account_exists(app):
    _user("forgot" + DOMAIN)
    sender = app.app_state.auth.sender
    a = app.post("/api/v1/auth/password/forgot", json={"email": "forgot" + DOMAIN})
    b = app.post("/api/v1/auth/password/forgot", json={"email": "ghost" + DOMAIN})
    assert a.status_code == b.status_code == 202 and a.json() == b.json()
    assert [e.to for e in sender.sent] == ["forgot" + DOMAIN]
    assert sender.sent[0].template == "reset-password"
    assert sender.sent[0].text.split("\n")[3].startswith(FRONT + "/reset-password?token=")


def test_reset_is_single_use_revokes_sessions_and_never_logs_the_token(app):
    _user("reset" + DOMAIN)
    signed_in = _new_client(app)
    assert _login(signed_in, "reset" + DOMAIN).status_code == 200
    app.post("/api/v1/auth/password/forgot", json={"email": "reset" + DOMAIN})
    token = app.app_state.auth.sender.last_token()
    with _db() as conn:
        stored = conn.execute("SELECT token_hash FROM public.password_reset_tokens t JOIN public.users u "
                              "ON u.id = t.user_id WHERE u.email = %s", ("reset" + DOMAIN,)).fetchone()[0]
    assert stored == sha256_hex(token) and stored != token

    assert app.post("/api/v1/auth/password/reset",
                    json={"token": token, "new_password": "short"}).status_code == 400
    ok = app.post("/api/v1/auth/password/reset", json={"token": token, "new_password": "New-Password-42"})
    assert ok.status_code == 200
    again = app.post("/api/v1/auth/password/reset", json={"token": token, "new_password": "Another-Pass-42"})
    assert again.status_code == 400
    assert signed_in.get("/api/v1/auth/session").status_code == 401
    assert _login(app, "reset" + DOMAIN).status_code == 401
    assert _login(app, "reset" + DOMAIN, "New-Password-42").status_code == 200
    assert all(token not in rec for rec in app.app_state.log_capture.records)


def test_expired_reset_token_is_rejected(app):
    _user("oldreset" + DOMAIN)
    app.post("/api/v1/auth/password/forgot", json={"email": "oldreset" + DOMAIN})
    token = app.app_state.auth.sender.last_token()
    with _db() as conn:
        conn.execute("UPDATE public.password_reset_tokens SET expires_at = now() - interval '1 second' "
                     "WHERE token_hash = %s", (sha256_hex(token),))
    r = app.post("/api/v1/auth/password/reset", json={"token": token, "new_password": "New-Password-42"})
    assert r.status_code == 400


def test_change_password_keeps_current_session_and_revokes_others(app):
    _user("change" + DOMAIN)
    other = _new_client(app)
    _login(other, "change" + DOMAIN)
    r = _login(app, "change" + DOMAIN)
    bad = app.post("/api/v1/auth/password/change", headers=_csrf(r),
                   json={"current_password": "wrong", "new_password": "Changed-Pass-42"})
    assert bad.status_code == 400
    ok = app.post("/api/v1/auth/password/change", headers=_csrf(r),
                  json={"current_password": PASSWORD, "new_password": "Changed-Pass-42"})
    assert ok.status_code == 200
    assert app.get("/api/v1/auth/session").status_code == 200
    assert other.get("/api/v1/auth/session").status_code == 401


# ── invites ─────────────────────────────────────────────────────────────────────


def _admin(app):
    _user("admin" + DOMAIN, role="admin")
    return _csrf(_login(app, "admin" + DOMAIN))


def test_only_admins_can_invite(app):
    _user("plain" + DOMAIN)
    r = _login(app, "plain" + DOMAIN)
    assert app.post("/api/v1/auth/invites", headers=_csrf(r), json={"email": "x" + DOMAIN}).status_code == 403
    anon = _new_client(app)
    assert anon.post("/api/v1/auth/invites", headers=ORIGIN, json={"email": "x" + DOMAIN}).status_code == 401


def test_invite_accept_creates_account_signs_in_and_cannot_be_reused(app):
    headers = _admin(app)
    with _db() as conn:
        streamer_id = conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name) VALUES "
                                   "('authtest-streamer', 'Auth Test') RETURNING id::text").fetchone()[0]
    created = app.post("/api/v1/auth/invites", headers=headers,
                       json={"email": "invitee" + DOMAIN, "streamer_id": streamer_id})
    assert created.status_code == 201 and "accept_url" not in created.json()
    token = app.app_state.auth.sender.last_token()
    assert app.app_state.auth.sender.sent[-1].template == "invite"

    newcomer = _new_client(app)
    short = newcomer.post("/api/v1/auth/invites/accept", headers=ORIGIN, json={"token": token, "password": "x"})
    assert short.status_code == 400
    acc = newcomer.post("/api/v1/auth/invites/accept", headers=ORIGIN,
                        json={"token": token, "password": "Invited-Pass-42", "username": "authtest_user"})
    assert acc.status_code == 200, acc.text
    user = acc.json()["user"]
    assert user["email"] == "invitee" + DOMAIN and user["role"] == "streamer" and user["streamer_id"] == streamer_id
    assert newcomer.get("/api/v1/auth/session").status_code == 200
    reuse = _new_client(app).post("/api/v1/auth/invites/accept", headers=ORIGIN,
                                  json={"token": token, "password": "Invited-Pass-42"})
    assert reuse.status_code == 400
    assert _login(_new_client(app), "invitee" + DOMAIN, "Invited-Pass-42").status_code == 200


def test_invite_cannot_take_over_an_account_that_has_a_password(app):
    headers = _admin(app)
    _user("taken" + DOMAIN)
    assert app.post("/api/v1/auth/invites", headers=headers, json={"email": "taken" + DOMAIN}).status_code == 409


def test_invite_activates_imported_account_without_password(app):
    headers = _admin(app)
    uid = _user("imported" + DOMAIN, password=None)
    assert app.post("/api/v1/auth/invites", headers=headers, json={"email": "imported" + DOMAIN}).status_code == 201
    token = app.app_state.auth.sender.last_token()
    acc = _new_client(app).post("/api/v1/auth/invites/accept", headers=ORIGIN,
                                json={"token": token, "password": "Imported-Pass-42"})
    assert acc.status_code == 200 and acc.json()["user"]["id"] == uid


def test_expired_invite_is_rejected(app):
    headers = _admin(app)
    app.post("/api/v1/auth/invites", headers=headers, json={"email": "late" + DOMAIN})
    token = app.app_state.auth.sender.last_token()
    with _db() as conn:
        conn.execute("UPDATE public.user_invites SET expires_at = now() - interval '1 second' WHERE token_hash = %s",
                     (sha256_hex(token),))
    r = _new_client(app).post("/api/v1/auth/invites/accept", headers=ORIGIN,
                              json={"token": token, "password": "Late-Pass-42"})
    assert r.status_code == 400


# ── Google ──────────────────────────────────────────────────────────────────────


def _google_start(client, redirect="/dashboard/settings"):
    r = client.get("/api/v1/auth/google/start", params={"redirect": redirect}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://accounts.google.invalid/")
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith("__host-hc_oauth=") and "secure" in cookie and "httponly" in cookie
    return parse_qs(urlparse(r.headers["location"]).query)["state"][0]


def test_google_links_existing_account_by_verified_email_then_by_subject(app):
    uid = _user("google" + DOMAIN)
    app.app_state.auth.google.identity = GoogleIdentity("google-sub-1", "google" + DOMAIN, True)
    state = _google_start(app)
    r = app.get("/api/v1/auth/google/callback", params={"code": "c1", "state": state}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == FRONT + "/dashboard/settings"
    assert app.get("/api/v1/auth/session").json()["user"]["id"] == uid
    with _db() as conn:
        assert conn.execute("SELECT user_id::text FROM public.user_identities WHERE provider = 'google' "
                            "AND provider_subject = 'google-sub-1'").fetchone()[0] == uid
    # Second login: Google now reports a different email, but the subject is already linked.
    app.app_state.auth.google.identity = GoogleIdentity("google-sub-1", "renamed" + DOMAIN, True)
    second = _new_client(app)
    state = _google_start(second)
    r = second.get("/api/v1/auth/google/callback", params={"code": "c2", "state": state}, follow_redirects=False)
    assert r.headers["location"] == FRONT + "/dashboard/settings"
    assert second.get("/api/v1/auth/session").json()["user"]["id"] == uid


def test_google_unknown_email_is_rejected_invite_only(app):
    app.app_state.auth.google.identity = GoogleIdentity("google-sub-x", "stranger" + DOMAIN, True)
    state = _google_start(app)
    r = app.get("/api/v1/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert r.headers["location"] == FRONT + "/auth?error=no_account"
    assert app.get("/api/v1/auth/session").status_code == 401


def test_google_state_mismatch_missing_cookie_and_open_redirect(app):
    _user("gstate" + DOMAIN)
    app.app_state.auth.google.identity = GoogleIdentity("google-sub-2", "gstate" + DOMAIN, True)
    _google_start(app)
    r = app.get("/api/v1/auth/google/callback", params={"code": "c", "state": "forged"}, follow_redirects=False)
    assert r.headers["location"] == FRONT + "/auth?error=invalid_state"
    fresh = _new_client(app)
    r = fresh.get("/api/v1/auth/google/callback", params={"code": "c", "state": "x"}, follow_redirects=False)
    assert r.headers["location"] == FRONT + "/auth?error=invalid_state"
    assert app.app_state.auth.google.seen == []  # no code exchange without a valid state
    state = _google_start(app, redirect="//evil.invalid/x")
    r = app.get("/api/v1/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert r.headers["location"] == FRONT + "/dashboard"


def test_google_cancel_and_verification_failure(app):
    state = _google_start(app)
    r = app.get("/api/v1/auth/google/callback", params={"error": "access_denied", "state": state},
                follow_redirects=False)
    assert r.headers["location"] == FRONT + "/auth?error=google_cancelled"
    state = _google_start(app)
    app.app_state.auth.google.identity = None
    r = app.get("/api/v1/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert r.headers["location"] == FRONT + "/auth?error=invalid_id_token"
