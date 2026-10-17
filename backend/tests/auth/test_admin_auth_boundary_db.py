"""Migration 0024: the API's general database role (hyperchat_app) cannot create or change an administrator's
identity, credentials, privileges or sessions; the authentication flows (hyperchat_auth) still work. Every attack
below runs with the application's own database credentials, then tries the real API. Real PostgreSQL, synthetic
rows only."""

import hashlib
import threading
import time

import psycopg
import pytest

from app.auth.google import GoogleIdentity
from app.auth.passwords import hash_password
from app.auth.service import AuthError
from app.auth.tokens import new_token
from tests.integration import (
    DOMAIN,
    ORIGIN,
    PASSWORD,
    csrf_headers,
    db,
    fixture_db,
    login,
    make_user,
    new_browser,
    requires_db,
    signed_in,
)
from tests.kyc.test_enforcement_db import enf, owner_user, standard_creator, verify  # noqa: F401 (fixture)

pytestmark = requires_db
EVIL = "Attacker-Chosen-77"
REFUSED = psycopg.errors.InsufficientPrivilege


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def app_sql(sql: str, args: tuple = ()):
    """hyperchat_app, as the API connects: what a SQL injection or a leaked connection holds."""
    with db() as conn:
        return conn.execute(sql, args)


def admin_api(client) -> int:
    return client.get("/api/v1/admin/users").status_code


# ── 1. password hashes ──────────────────────────────────────────────────────────

def test_an_admins_password_hash_cannot_be_replaced(app):
    email = "ab-hash" + DOMAIN
    aid = make_user(email, role="admin")
    with pytest.raises(REFUSED):
        app_sql("UPDATE public.users SET password_hash = %s WHERE id = %s", (hash_password(EVIL), aid))
    for sql in ("UPDATE public.users SET email = 'ab-hijack@evil.invalid' WHERE id = %s",
                "UPDATE public.users SET is_active = false WHERE id = %s",
                "UPDATE public.users SET failed_login_attempts = 99, locked_until = now() + interval '1 day' "
                "WHERE id = %s",
                "DELETE FROM public.users WHERE id = %s"):
        with pytest.raises(REFUSED):
            app_sql(sql, (aid,))
    b = new_browser(app)
    assert login(b, email, EVIL).status_code == 401
    assert login(b, email).status_code == 200 and admin_api(b) == 200  # the real admin still signs in


# ── 2. privileges ───────────────────────────────────────────────────────────────

def test_no_user_can_be_made_an_admin_by_the_runtime_role(app):
    email = "ab-promo" + DOMAIN
    uid = make_user(email, role="user")
    with pytest.raises(REFUSED):
        app_sql("UPDATE public.users SET role = 'admin' WHERE id = %s", (uid,))
    with pytest.raises(REFUSED):
        app_sql("INSERT INTO public.users (email, password_hash, role) VALUES (%s, %s, 'admin')",
                ("ab-new-admin" + DOMAIN, hash_password(EVIL)))
    b = new_browser(app)
    login(b, email)
    assert admin_api(b) == 403
    # The admin API does not promote existing accounts either (their credentials may have been prepared).
    boss = new_browser(app)
    headers = signed_in(boss, "ab-promo-boss" + DOMAIN, role="admin")
    r = boss.patch(f"/api/v1/admin/users/{uid}", json={"role": "admin"}, headers=headers)
    assert (r.status_code, r.json()["error"]) == (409, "Existing accounts cannot be made admins. Send an admin "
                                                       "invitation instead.")
    assert boss.patch(f"/api/v1/admin/users/{uid}", json={"role": "streamer"}, headers=headers).status_code == 200


# ── 3. sessions ─────────────────────────────────────────────────────────────────

def test_admin_sessions_cannot_be_forged_altered_revived_or_extended(app):
    email = "ab-sess" + DOMAIN
    aid = make_user(email, role="admin")
    uid = make_user("ab-sess-user" + DOMAIN, role="user")
    token = new_token(32)
    insert = ("INSERT INTO public.sessions (user_id, token_hash, csrf_token_hash, idle_expires_at, absolute_expires_at) "
              "VALUES (%s, %s, 'x', now() + interval '1 day', now() + interval '2 days') RETURNING id::text")
    with pytest.raises(REFUSED):
        app_sql(insert, (aid, sha(token)))
    own = app_sql(insert, (uid, sha(token))).fetchone()[0]  # an ordinary user's session: still possible ...
    for sql in ("UPDATE public.sessions SET user_id = %s WHERE id = %s",  # ... but never re-pointed at an admin
                "UPDATE public.sessions SET token_hash = %s WHERE id = %s"):
        with pytest.raises(REFUSED):
            app_sql(sql, (aid if "user_id" in sql else sha("other"), own))
    forged = new_browser(app)
    forged.cookies.set("__Host-hc_session", token)
    assert forged.get("/api/v1/auth/session").json()["user"]["role"] == "user" and admin_api(forged) == 403
    # The admin's own session: revoked stays revoked; expired stays expired; absolute expiry is fixed.
    b = new_browser(app)
    headers = csrf_headers(login(b, email))
    with db() as conn:
        sid = conn.execute("SELECT id::text FROM public.sessions WHERE user_id = %s", (aid,)).fetchone()[0]
    with pytest.raises(REFUSED):
        app_sql("UPDATE public.sessions SET absolute_expires_at = now() + interval '1 year' WHERE id = %s", (sid,))
    assert b.post("/api/v1/auth/logout", headers=headers).status_code == 200
    with pytest.raises(REFUSED):
        app_sql("UPDATE public.sessions SET revoked_at = NULL WHERE id = %s", (sid,))
    b2 = new_browser(app)
    login(b2, email)
    with fixture_db() as conn:  # time passes: the second session is idle-expired
        sid2 = conn.execute("UPDATE public.sessions SET idle_expires_at = now() - interval '1 minute' "
                            "WHERE user_id = %s AND revoked_at IS NULL RETURNING id::text", (aid,)).fetchone()[0]
    with pytest.raises(REFUSED):
        app_sql("UPDATE public.sessions SET idle_expires_at = now() + interval '1 day' WHERE id = %s", (sid2,))
    assert admin_api(b2) == 401  # expired means expired
    b3 = new_browser(app)
    login(b3, email)
    assert admin_api(b3) == 200 and admin_api(b3) == 200  # a live session still slides (every request)


# ── 4. reset links and invites ──────────────────────────────────────────────────

def test_reset_links_and_invites_cannot_take_over_or_create_an_admin(app):
    email = "ab-reset" + DOMAIN
    aid = make_user(email, role="admin")
    reset = new_token(32)
    with pytest.raises(REFUSED):
        app_sql("INSERT INTO public.password_reset_tokens (user_id, token_hash, expires_at) "
                "VALUES (%s, %s, now() + interval '1 hour')", (aid, sha(reset)))
    r = app.post("/api/v1/auth/password/reset", json={"token": reset, "new_password": EVIL}, headers=ORIGIN)
    assert r.status_code == 400
    with fixture_db() as conn:  # a used link of this admin
        rid = conn.execute("INSERT INTO public.password_reset_tokens (user_id, token_hash, expires_at, used_at) "
                           "VALUES (%s, %s, now() + interval '1 hour', now()) RETURNING id", (aid, sha("used"))
                           ).fetchone()[0]
    for sql in ("UPDATE public.password_reset_tokens SET used_at = NULL WHERE id = %s",
                "UPDATE public.password_reset_tokens SET token_hash = 'x' WHERE id = %s",
                "UPDATE public.password_reset_tokens SET expires_at = now() + interval '1 year' WHERE id = %s"):
        with pytest.raises(REFUSED):
            app_sql(sql, (rid,))
    invite = new_token(32)
    with pytest.raises(REFUSED):
        app_sql("INSERT INTO public.user_invites (email, role, token_hash, expires_at) "
                "VALUES (%s, 'admin', %s, now() + interval '1 day')", ("ab-inv" + DOMAIN, sha(invite)))
    iid = app_sql("INSERT INTO public.user_invites (email, role, token_hash, expires_at) VALUES (%s, 'user', %s, "
                  "now() + interval '1 day') RETURNING id", ("ab-inv" + DOMAIN, sha(invite))).fetchone()[0]
    for sql in ("UPDATE public.user_invites SET role = 'admin' WHERE id = %s",
                "UPDATE public.user_invites SET email = 'x@evil.invalid' WHERE id = %s",
                "UPDATE public.user_invites SET expires_at = now() + interval '1 year' WHERE id = %s"):
        with pytest.raises(REFUSED):
            app_sql(sql, (iid,))
    # An ordinary invite to the admin's own address cannot upsert onto the admin.
    takeover = new_token(32)
    app_sql("INSERT INTO public.user_invites (email, role, token_hash, expires_at) VALUES (%s, 'user', %s, "
            "now() + interval '1 day')", (email, sha(takeover)))
    r = app.post("/api/v1/auth/invites/accept", json={"token": takeover, "password": EVIL}, headers=ORIGIN)
    assert r.status_code == 409
    b = new_browser(app)
    assert login(b, email, EVIL).status_code == 401 and login(b, email).status_code == 200


def test_a_real_admin_invite_creates_a_new_admin_and_never_an_existing_account(app):
    boss = new_browser(app)
    headers = signed_in(boss, "ab-inviter" + DOMAIN, role="admin")
    r = boss.post("/api/v1/auth/invites", json={"email": "ab-newadmin" + DOMAIN, "role": "admin"}, headers=headers)
    assert r.status_code == 201, r.text
    token = app.app_state.auth.sender.last_token()
    newcomer = new_browser(app)
    r = newcomer.post("/api/v1/auth/invites/accept", json={"token": token, "password": "New-Admin-Pass-42"},
                      headers=ORIGIN)
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin" and admin_api(newcomer) == 200
    make_user("ab-prepared" + DOMAIN, password=None, role="user")  # a password-less row someone prepared
    r = boss.post("/api/v1/auth/invites", json={"email": "ab-prepared" + DOMAIN, "role": "admin"}, headers=headers)
    token = app.app_state.auth.sender.last_token()
    r = app.post("/api/v1/auth/invites/accept", json={"token": token, "password": "New-Admin-Pass-42"},
                 headers=ORIGIN)
    assert r.status_code == 409  # an admin is always a new account


# ── 5. Google identities ────────────────────────────────────────────────────────

def test_google_identities_cannot_be_bound_to_an_admin(app):
    email = "ab-google" + DOMAIN
    aid = make_user(email, role="admin")
    uid = make_user("ab-google-user" + DOMAIN, role="user")
    with pytest.raises(REFUSED):
        app_sql("INSERT INTO public.user_identities (user_id, provider, provider_subject) "
                "VALUES (%s, 'google', 'attacker-sub')", (aid,))
    iid = app_sql("INSERT INTO public.user_identities (user_id, provider, provider_subject) VALUES (%s, 'google', "
                  "'attacker-sub') RETURNING id", (uid,)).fetchone()[0]
    with pytest.raises(REFUSED):
        app_sql("UPDATE public.user_identities SET user_id = %s WHERE id = %s", (aid, iid))
    svc = app.app_state.auth
    svc.google.identity = GoogleIdentity("attacker-sub", "attacker@evil.invalid", True)
    user, _ = app.portal.call(svc.google_login, "code", "v", "n", "203.0.113.9", None)
    assert user.role == "user"  # the attacker's Google account is still only the ordinary user
    # S2: the admin's own (verified) Google address no longer links or signs in: a matching email is not an identity.
    svc.google.identity = GoogleIdentity("admin-real-sub", email, True)
    with pytest.raises(AuthError) as refused:
        app.portal.call(svc.google_login, "code", "v", "n", "203.0.113.9", None)
    assert (refused.value.status, refused.value.reason) == (403, "admin_google_not_linked")
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.user_identities WHERE user_id = %s", (aid,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM public.sessions WHERE user_id = %s", (aid,)).fetchone()[0] == 0


def test_google_never_links_a_matching_email_to_an_admin_at_any_layer(app):
    """S2 + migration 0026: the service refuses, the repository re-checks under the row lock, and the database
    refuses every API role (hyperchat_auth included) a new identity for an admin; a linked identity keeps working."""
    from app.auth.repository import GoogleLinkRefused
    email = "ab-google-link" + DOMAIN
    aid = make_user(email, role="admin")
    svc = app.app_state.auth
    session_row = svc._new_session(aid, "203.0.113.9", None)[2]
    # The repository alone (a caller that skipped the service check, or a demotion racing it) refuses too.
    with pytest.raises(GoogleLinkRefused):
        app.portal.call(svc.repo.google_sign_in, aid, "new-sub", email, session_row)
    with db("hyperchat_auth") as conn, pytest.raises(REFUSED):  # the database: even the authentication role
        conn.execute("INSERT INTO public.user_identities (user_id, provider, provider_subject, email) "
                     "VALUES (%s, 'google', 'auth-role-sub', %s)", (aid, email))
    uid = make_user("ab-google-link-user" + DOMAIN, role="user")
    with db("hyperchat_auth") as conn:
        iid = conn.execute("INSERT INTO public.user_identities (user_id, provider, provider_subject) "
                           "VALUES (%s, 'google', 'moved-sub') RETURNING id", (uid,)).fetchone()[0]
        with pytest.raises(REFUSED):  # moving an ordinary identity onto the admin
            conn.execute("UPDATE public.user_identities SET user_id = %s WHERE id = %s", (aid, iid))
    # An identity linked to the admin before S2 (by the operator) still signs in, and only its own subject does.
    with fixture_db() as conn:
        conn.execute("INSERT INTO public.user_identities (user_id, provider, provider_subject, email) "
                     "VALUES (%s, 'google', 'linked-sub', %s)", (aid, email))
    svc.google.identity = GoogleIdentity("linked-sub", email, True)
    user, _ = app.portal.call(svc.google_login, "code", "v", "n", "203.0.113.9", None)
    assert user.id == aid
    svc.google.identity = GoogleIdentity("other-sub", email, True)  # same email, another Google account
    with pytest.raises(AuthError):
        app.portal.call(svc.google_login, "code", "v", "n", "203.0.113.9", None)
    with db() as conn:
        assert {r[0] for r in conn.execute("SELECT provider_subject FROM public.user_identities WHERE user_id = %s",
                                           (aid,))} == {"linked-sub"}
    with pytest.raises(REFUSED):
        app_sql("UPDATE public.user_identities SET provider_subject = 'attacker-sub-2' WHERE user_id = %s", (aid,))
    # A non-admin keeps email-based linking (unchanged behaviour).
    svc.google.identity = GoogleIdentity("user-sub", "ab-google-link-user" + DOMAIN, True)
    user, _ = app.portal.call(svc.google_login, "code", "v", "n", "203.0.113.9", None)
    assert user.id == uid


# ── 6. legitimate flows ─────────────────────────────────────────────────────────

def test_admin_password_change_reset_deactivation_and_logout_still_work(app):
    email = "ab-legit" + DOMAIN
    aid = make_user(email, role="admin")
    b = new_browser(app)
    headers = csrf_headers(login(b, email))
    r = b.post("/api/v1/auth/password/change", json={"current_password": PASSWORD, "new_password": "Changed-Pass-55"},
               headers=headers)
    assert r.status_code == 200, r.text
    app.post("/api/v1/auth/password/forgot", json={"email": email}, headers=ORIGIN)
    token = app.app_state.auth.sender.last_token()
    assert app.post("/api/v1/auth/password/reset", json={"token": token, "new_password": "Reset-Pass-66"},
                    headers=ORIGIN).status_code == 200
    assert admin_api(b) == 401  # the reset revoked every session
    c = new_browser(app)
    assert login(c, email, "Reset-Pass-66").status_code == 200 and admin_api(c) == 200
    boss = new_browser(app)
    bh = signed_in(boss, "ab-legit-boss" + DOMAIN, role="admin")
    assert boss.patch(f"/api/v1/admin/users/{aid}", json={"is_active": False}, headers=bh).status_code == 200
    assert admin_api(c) == 401 and login(new_browser(app), email, "Reset-Pass-66").status_code == 401


def no_sign_in_admins() -> None:
    """bootstrap-admin runs only while no admin can sign in: demote this suite's admins, skip on a real one."""
    with fixture_db() as conn:
        conn.execute("UPDATE public.users SET role = 'streamer' WHERE role = 'admin' AND email::text LIKE %s",
                     ("%" + DOMAIN,))
    with db() as conn:
        if conn.execute("SELECT count(*) FROM public.users WHERE role = 'admin' AND is_active").fetchone()[0]:
            pytest.skip("the scratch database has a real admin")


def test_bootstrap_never_promotes_an_existing_account(env):
    import asyncio

    from app.cli import bootstrap_admin
    from app.core.config import Settings
    no_sign_in_admins()
    uid = make_user("ab-boot" + DOMAIN, password=None, role="user")
    app_sql("INSERT INTO public.user_identities (user_id, provider, provider_subject) VALUES (%s, 'google', "
            "'planted-sub')", (uid,))
    assert asyncio.run(bootstrap_admin(Settings(), "ab-boot" + DOMAIN, "Bootstrap-Pass-77")) == 1
    with db() as conn:
        assert conn.execute("SELECT role, password_hash FROM public.users WHERE id = %s", (uid,)).fetchone() == (
            "user", None)


def test_no_api_role_can_update_an_account_into_an_admin(app):
    uid = make_user("ab-authpromo" + DOMAIN, password=None, role="user")
    for role in ("hyperchat_app", "hyperchat_auth"):
        with db(role) as conn, pytest.raises(REFUSED):
            conn.execute("UPDATE public.users SET role = 'admin' WHERE id = %s", (uid,))
    with db("hyperchat_auth") as conn:  # hyperchat_auth's other user writes are unchanged
        conn.execute("UPDATE public.users SET role = 'streamer' WHERE id = %s", (uid,))
        with pytest.raises(REFUSED):  # also via an upsert's DO UPDATE
            conn.execute("INSERT INTO public.users (email, role) VALUES (%s, 'admin') "
                         "ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role", ("ab-authpromo" + DOMAIN,))
    with db() as conn:
        assert conn.execute("SELECT role FROM public.users WHERE id = %s", (uid,)).fetchone()[0] == "streamer"


def test_an_ordinary_invite_never_gives_a_password_to_a_password_less_admin(app):
    email = "ab-googleonly" + DOMAIN
    aid = make_user(email, password=None, role="admin")  # signs in with Google only
    token = new_token(32)
    app_sql("INSERT INTO public.user_invites (email, role, token_hash, expires_at) VALUES (%s, 'user', %s, "
            "now() + interval '1 day')", (email, sha(token)))
    r = app.post("/api/v1/auth/invites/accept", json={"token": token, "password": EVIL}, headers=ORIGIN)
    assert r.status_code == 409
    with db() as conn:
        assert conn.execute("SELECT role, password_hash FROM public.users WHERE id = %s", (aid,)).fetchone() == (
            "admin", None)
        assert conn.execute("SELECT accepted_at FROM public.user_invites WHERE token_hash = %s",
                            (sha(token),)).fetchone()[0] is None


# ── 7. concurrency ──────────────────────────────────────────────────────────────

def test_a_session_insert_racing_a_promotion_waits_and_is_refused(app):
    uid = make_user("ab-race" + DOMAIN, role="user")
    result = {}

    def forge():
        try:
            app_sql("INSERT INTO public.sessions (user_id, token_hash, csrf_token_hash, idle_expires_at, "
                    "absolute_expires_at) VALUES (%s, %s, 'x', now() + interval '1 day', now() + interval '1 day')",
                    (uid, sha(new_token(16))))
            result["r"] = "inserted"
        except REFUSED:
            result["r"] = "refused"

    with fixture_db() as conn:  # a promotion in flight (as bootstrap / the schema owner would)
        with conn.transaction():
            conn.execute("UPDATE public.users SET role = 'admin' WHERE id = %s", (uid,))
            th = threading.Thread(target=forge)
            th.start()
            time.sleep(0.5)
            assert th.is_alive()  # the insert waits on the user row ...
        th.join(10)
    assert result["r"] == "refused"  # ... and then sees an admin


# ── 8. privileges ───────────────────────────────────────────────────────────────

def test_role_grants_are_exactly_the_boundary(app):
    with fixture_db() as conn:  # information_schema shows other roles' grants to the owner
        rows = conn.execute("""
            SELECT table_name, string_agg(privilege_type, ',' ORDER BY privilege_type)
            FROM information_schema.role_table_grants WHERE grantee = 'hyperchat_auth' GROUP BY 1 ORDER BY 1
        """).fetchall()
        exposed = conn.execute("SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace "
                               "AND prosecdef AND has_function_privilege('public', oid, 'EXECUTE')").fetchone()[0]
        guards = conn.execute("SELECT count(*) FROM pg_proc WHERE proname LIKE 'auth_guard_%%' "
                              "AND has_function_privilege('hyperchat_app', oid, 'EXECUTE')").fetchone()[0]
    assert dict(rows) == {"password_reset_tokens": "INSERT,SELECT,UPDATE", "sessions": "INSERT,SELECT,UPDATE",
                          "streamers": "SELECT", "user_identities": "DELETE,INSERT,SELECT,UPDATE",
                          "user_invites": "INSERT,SELECT,UPDATE", "user_signups": "SELECT",
                          "users": "INSERT,SELECT,UPDATE",
                          "stepup_enforcement": "SELECT",  # 0025: reads the MFA enforcement state, nothing else
                          "admin_identity_link_requests": "INSERT,SELECT"}  # 0026: records requests, never decides
    assert exposed == 0 and guards == 0


# ── 9. the lifecycle path after hardening ───────────────────────────────────────

def test_a_hijack_attempt_cannot_reach_a_lifecycle_change_but_a_real_admin_can(enf):
    email = "ab-life" + DOMAIN
    aid = make_user(email, role="admin")
    owner = owner_user("ab-life-owner")
    verify(owner)
    sid = standard_creator("authtest-ab-life", owner)
    with pytest.raises(REFUSED):
        app_sql("UPDATE public.users SET password_hash = %s WHERE id = %s", (hash_password(EVIL), aid))
    attacker = new_browser(enf)
    assert login(attacker, email, EVIL).status_code == 401
    real = new_browser(enf)
    headers = csrf_headers(login(real, email))
    r = real.post(f"/api/v1/admin/creators/{sid}/activate", json={"expected_status": "pending"}, headers=headers)
    assert r.status_code == 200 and r.json()["creator"]["status"] == "active"
