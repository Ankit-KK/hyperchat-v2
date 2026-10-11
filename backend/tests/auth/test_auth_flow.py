"""End-to-end auth through the real app (create_app + lifespan) against a scratch database.

See tests/integration.py for how the database is selected and which rows are synthetic. No email or
Google call leaves the process: the sender and the Google client are in-memory fakes.
"""

from urllib.parse import parse_qs, urlparse

import bcrypt

from app.auth.google import GoogleIdentity
from app.auth.service import GENERIC_LOGIN_ERROR
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
    assert a.json() == b.json() == {"error": GENERIC_LOGIN_ERROR}
    assert "set-cookie" not in a.headers


def test_login_requires_allowed_origin(app):
    _user("origin" + DOMAIN)
    assert _login(app, "origin" + DOMAIN, headers={}).status_code == 403
    assert _login(app, "origin" + DOMAIN, headers={"origin": "https://evil.invalid"}).status_code == 403


# Sign-in throttling (security audit F2, replaces the account-wide lockout): tests/auth/test_login_throttling.py


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


def _reset_link(app, email: str) -> str:
    assert app.post("/api/v1/auth/password/forgot", json={"email": email}).status_code == 202
    return app.app_state.auth.sender.last_token()


def test_password_change_burns_reset_links_sent_before_it(app):
    """Security audit F10: someone who saw a reset link must not be able to use it after the owner changed the
    password."""
    _user("changed" + DOMAIN)
    stolen = _reset_link(app, "changed" + DOMAIN)
    r = _login(app, "changed" + DOMAIN)
    assert app.post("/api/v1/auth/password/change", headers=_csrf(r),
                    json={"current_password": PASSWORD, "new_password": "Owner-New-Pass-42"}).status_code == 200
    late = app.post("/api/v1/auth/password/reset", json={"token": stolen, "new_password": "Attacker-Pass-42"})
    assert late.status_code == 400
    assert _login(_new_client(app), "changed" + DOMAIN, "Attacker-Pass-42").status_code == 401
    assert _login(_new_client(app), "changed" + DOMAIN, "Owner-New-Pass-42").status_code == 200
    fresh = _reset_link(app, "changed" + DOMAIN)  # a link requested after the change still works
    assert app.post("/api/v1/auth/password/reset",
                    json={"token": fresh, "new_password": "Owner-Reset-Pass-42"}).status_code == 200


def test_reset_link_of_a_deactivated_account_is_refused(app):
    """Security audit F10: a link issued before an admin deactivated the account cannot set its password (which
    would otherwise work again once the account is reactivated)."""
    _user("disabled" + DOMAIN)
    link = _reset_link(app, "disabled" + DOMAIN)
    with _db() as conn:
        conn.execute("UPDATE public.users SET is_active = false WHERE email = %s", ("disabled" + DOMAIN,))
    r = app.post("/api/v1/auth/password/reset", json={"token": link, "new_password": "Attacker-Pass-42"})
    assert r.status_code == 400
    with _db() as conn:
        conn.execute("UPDATE public.users SET is_active = true WHERE email = %s", ("disabled" + DOMAIN,))
    assert _login(_new_client(app), "disabled" + DOMAIN, "Attacker-Pass-42").status_code == 401
    assert _login(_new_client(app), "disabled" + DOMAIN).status_code == 200  # the original password is untouched


def test_admin_deactivation_burns_reset_links_for_good(app):
    """Security audit F10: deactivating an account (the admin API, the only runtime path that sets is_active to
    false) burns its open reset links in the same transaction, so a link issued before cannot be used even after the
    account is reactivated. The password and normal activation are unaffected."""
    headers = _admin(app)
    uid = _user("deact" + DOMAIN)
    link = _reset_link(app, "deact" + DOMAIN)
    off = app.patch(f"/api/v1/admin/users/{uid}", headers=headers, json={"is_active": False})
    assert off.status_code == 200 and off.json()["user"]["is_active"] is False
    on = app.patch(f"/api/v1/admin/users/{uid}", headers=headers, json={"is_active": True})
    assert on.status_code == 200 and on.json()["user"]["is_active"] is True
    late = app.post("/api/v1/auth/password/reset", json={"token": link, "new_password": "Attacker-Pass-42"})
    assert late.status_code == 400
    with _db() as conn:
        assert conn.execute("SELECT count(*) FROM public.password_reset_tokens t JOIN public.users u ON u.id = t.user_id "
                            "WHERE u.email = %s AND t.used_at IS NULL", ("deact" + DOMAIN,)).fetchone()[0] == 0
    assert _login(_new_client(app), "deact" + DOMAIN, "Attacker-Pass-42").status_code == 401
    assert _login(_new_client(app), "deact" + DOMAIN).status_code == 200  # the original password still works
    fresh = _reset_link(app, "deact" + DOMAIN)  # a link requested after reactivation works normally
    assert app.post("/api/v1/auth/password/reset",
                    json={"token": fresh, "new_password": "After-Reactivation-42"}).status_code == 200


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


def _change(client, r, current):
    return client.post("/api/v1/auth/password/change", headers=_csrf(r),
                       json={"current_password": current, "new_password": "Changed-Pass-42"})


def test_current_password_guesses_are_limited_per_user(app):
    """Security audit F3: a stolen session cannot guess the current password without limit."""
    _user("guess" + DOMAIN)
    r = _login(app, "guess" + DOMAIN)
    assert [_change(app, r, f"wrong-{i}").status_code for i in range(5)] == [400] * 5
    blocked = _change(app, r, PASSWORD)  # the right password, after the limit: refused, so no oracle
    assert (blocked.status_code, blocked.json()) == (429, {"error": "Too many attempts. Please try again later."})
    assert _login(_new_client(app), "guess" + DOMAIN).status_code == 200  # password unchanged
    records = app.app_state.log_capture.records
    assert any("password_change_rate_limited" in rec for rec in records)
    assert sum("password_change_rejected" in rec for rec in records) == 5
    assert not any(PASSWORD in rec for rec in records)


def test_legitimate_retries_and_the_limit_window(app):
    _user("retry" + DOMAIN)
    r = _login(app, "retry" + DOMAIN)
    weak = app.post("/api/v1/auth/password/change", headers=_csrf(r),
                    json={"current_password": PASSWORD, "new_password": "short"})
    assert weak.status_code == 400  # rejected before any verification: not counted
    assert [_change(app, r, "typo").status_code for _ in range(4)] == [400] * 4
    assert _change(app, r, PASSWORD).status_code == 200  # 5th verification, within the limit
    # the window passes: the user may verify again
    with _db() as conn:
        conn.execute("UPDATE public.rate_limits SET window_start = now() - interval '2 hours' "
                     "WHERE endpoint = 'auth:password-change'")
    again = app.post("/api/v1/auth/password/change", headers=_csrf(r),
                     json={"current_password": "Changed-Pass-42", "new_password": "Changed-Again-42"})
    assert again.status_code == 200


def test_the_limit_is_per_user(app):
    _user("victim" + DOMAIN)
    _user("bystander" + DOMAIN)
    r = _login(app, "victim" + DOMAIN)
    assert [_change(app, r, "wrong").status_code for _ in range(6)] == [400] * 5 + [429]
    other = _new_client(app)
    r2 = _login(other, "bystander" + DOMAIN)
    assert _change(other, r2, PASSWORD).status_code == 200


def test_switching_sessions_does_not_reset_the_limit(app):
    """The counter belongs to the user, not to the session: guesses spread over several sessions (and a fresh
    sign-in after signing out) share one budget."""
    _user("hopper" + DOMAIN)
    first, second = app, _new_client(app)
    r1, r2 = _login(first, "hopper" + DOMAIN), _login(second, "hopper" + DOMAIN)
    assert [_change(first, r1, "wrong").status_code for _ in range(3)] == [400] * 3
    assert [_change(second, r2, "wrong").status_code for _ in range(2)] == [400] * 2
    assert first.post("/api/v1/auth/logout", headers=_csrf(r1)).status_code == 200
    r3 = _login(first, "hopper" + DOMAIN)  # a brand-new session
    assert _change(first, r3, PASSWORD).status_code == 429
    assert _change(second, r2, PASSWORD).status_code == 429
    assert second.get("/api/v1/auth/session").status_code == 200  # the session stays active at the limit


def test_concurrent_guesses_cannot_exceed_the_limit(env):
    """12 wrong guesses at once, each on its own database connection, through the real service: the atomic
    counter lets exactly 5 reach the password check; the rest get 429."""
    import asyncio

    from app.auth.repository import AuthRepository
    from app.auth.service import AuthError, AuthService
    from app.cli import connection_factory
    from app.core.config import Settings

    _user("racer" + DOMAIN)
    settings = Settings()
    service = AuthService.from_settings(settings, AuthRepository(connection_factory(settings)))

    async def race():
        user = await service.repo.user_by_email("racer" + DOMAIN)
        session = (await service.issue_session(user, "203.0.113.7", None)).session

        async def guess(i):
            try:
                await service.change_password(user, session, f"wrong-{i}", "Changed-Pass-42", "203.0.113.7")
            except AuthError as exc:
                return exc.status
            return 200
        return await asyncio.gather(*(guess(i) for i in range(12)))
    results = asyncio.run(race())
    assert sorted(results) == [400] * 5 + [429] * 7
    with _db() as conn:
        count = conn.execute("SELECT request_count FROM public.rate_limits WHERE ip_address = "
                             "(SELECT 'user:' || id::text FROM public.users WHERE email = %s) "
                             "AND endpoint = 'auth:password-change'", ("racer" + DOMAIN,)).fetchone()[0]
    assert count == 12  # every attempt was counted, none lost to the race


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


def test_invite_for_a_deactivated_imported_account_is_refused_without_a_session(app):
    """A deactivated password-less account: 403 with the generic disabled message, no cookie, the invite unused."""
    headers = _admin(app)
    uid = _user("imported-off" + DOMAIN, password=None, is_active=False)
    assert app.post("/api/v1/auth/invites", headers=headers,
                    json={"email": "imported-off" + DOMAIN}).status_code == 201
    token = app.app_state.auth.sender.last_token()
    acc = _new_client(app).post("/api/v1/auth/invites/accept", headers=ORIGIN,
                                json={"token": token, "password": "Imported-Pass-42"})
    assert acc.status_code == 403 and acc.json() == {"error": "This account is disabled"}
    assert "set-cookie" not in acc.headers
    with _db() as conn:
        hashed, active = conn.execute("SELECT password_hash, is_active FROM public.users WHERE id = %s",
                                      (uid,)).fetchone()
        unused = conn.execute("SELECT accepted_at IS NULL FROM public.user_invites WHERE token_hash = %s",
                              (sha256_hex(token),)).fetchone()[0]
    assert hashed is None and active is False and unused


def test_invite_acceptance_burns_open_reset_links(app):
    """Security audit F10: a password-less (e.g. migrated) account that requested a reset link, then accepted an
    invite, has a password now; the earlier link must not override it."""
    headers = _admin(app)
    _user("invited-reset" + DOMAIN, password=None)
    link = _reset_link(app, "invited-reset" + DOMAIN)
    assert app.post("/api/v1/auth/invites", headers=headers,
                    json={"email": "invited-reset" + DOMAIN}).status_code == 201
    invite = app.app_state.auth.sender.last_token()
    assert _new_client(app).post("/api/v1/auth/invites/accept", headers=ORIGIN,
                                 json={"token": invite, "password": "Invited-Pass-42"}).status_code == 200
    late = app.post("/api/v1/auth/password/reset", json={"token": link, "new_password": "Attacker-Pass-42"})
    assert late.status_code == 400
    assert _login(_new_client(app), "invited-reset" + DOMAIN, "Invited-Pass-42").status_code == 200


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
