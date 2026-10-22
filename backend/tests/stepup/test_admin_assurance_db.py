"""Post-S3 administrator security closure, against real PostgreSQL with real enforcement states (disposable test
database only; 'active' is set with the guard off and always put back to 'draft'):

  A1  an administrator acts on creators (dashboard, OBS tokens, soundboard, transcriptions, the realtime dashboard
      channel) only with the assurance admin routes demand: aal2 while enforcement is active;
  A4  administrator sessions end 2 hours after their last use and 12 hours after sign-in, in the database;
  A5  `python -m app.stepup.operator enforcement-draft`: the audited emergency return to draft.
"""

import logging
import os
import tempfile
import threading
from datetime import timedelta

import psycopg
import pytest

from app.auth.google import GoogleIdentity
from tests.integration import (
    DOMAIN,
    FIXTURE_ROLE,
    ORIGIN,
    PG_HOST,
    db,
    fixture_db,
    login,
    make_streamer,
    make_user,
    new_browser,
    requires_db,
    target,
)
from tests.realtime.test_realtime_db import APP, SECRET
from tests.stepup.test_mfa_api import MFA, _api, _verifier, ceremony, enroll
from tests.stepup.test_stepup_db import REFUSED, guards_off
from tests.stepup.test_stepup_enforcement_db import set_enforcement

pytestmark = requires_db
PUSHER = "/api/v1/realtime/pusher/auth"
DASH = "/api/v1/dashboard/streamers"


@pytest.fixture
def api(env, monkeypatch):
    """The real API with the verifier on a Unix socket AND realtime signing enabled (test-only Pusher values)."""
    for key, value in {"HYPERCHAT_REALTIME_ENABLED": "true", "HYPERCHAT_PUSHER_APP_ID_1": APP.app_id,
                       "HYPERCHAT_PUSHER_KEY_1": APP.key, "HYPERCHAT_PUSHER_SECRET_1": SECRET,
                       "HYPERCHAT_PUSHER_CLUSTER_1": APP.cluster}.items():
        monkeypatch.setenv(key, value)
    path = os.path.join(tempfile.mkdtemp(prefix="stepup-"), "stepup.sock")
    server, thread = _verifier(path)
    client, capture = _api(monkeypatch, path)
    yield client
    client.__exit__(None, None, None)
    logging.getLogger().removeHandler(capture)
    server.should_exit = True
    thread.join(10)
    set_enforcement("draft")
    with db() as conn:
        conn.execute("DELETE FROM public.rate_limits WHERE endpoint LIKE 'realtime:%'")


def browser(api, email: str):
    b = new_browser(api)
    r = login(b, email)
    assert r.status_code == 200, r.text
    return b, {**ORIGIN, "x-csrf-token": r.json()["csrf_token"]}


def channel(api, b, headers, slug: str):
    return b.post(PUSHER, data={"socket_id": "1234.5678", "channel_name": f"private-{slug}-dashboard"}, headers=headers)


def reason(r) -> tuple[int, str | None]:
    is_json = r.headers.get("content-type", "").startswith("application/json")
    return r.status_code, r.json().get("reason") if is_json else None


def creator_surface(b, headers, slug: str) -> dict[str, tuple[int, str | None]]:
    """One read and one write of each creator-scoped area, as this browser."""
    return {
        "settings": reason(b.get(f"{DASH}/{slug}/settings")),
        "settings_write": reason(b.patch(f"{DASH}/{slug}/settings", json={"goal_name": "A1"}, headers=headers)),
        "donations": reason(b.get(f"{DASH}/{slug}/donations")),
        "export": reason(b.get(f"{DASH}/{slug}/donations/export.csv",
                               params={"from": "2026-01-01", "to": "2026-01-31"})),
        "obs_token": reason(b.post(f"{DASH}/{slug}/obs-tokens", headers=headers)),
        "soundboard": reason(b.get(f"{DASH}/{slug}/soundboard")),
        "access": reason(b.get(f"/api/v1/streamers/{slug}/access")),
    }


def admitted(surface: dict) -> bool:
    """Past the access check: no assurance refusal (a route may still answer its own refusal, e.g. a soundboard the
    creator is not eligible for), and the plain reads succeed."""
    return (all(r != "mfa_required" and code != 401 for code, r in surface.values())
            and surface["settings"][0] == surface["access"][0] == 200)


# ── A1 ──────────────────────────────────────────────────────────────────────────

def test_with_enforcement_active_an_aal1_admin_reaches_no_creator_route_or_dashboard_channel(api):
    owner = make_user("a1-owner" + DOMAIN)
    sid = make_streamer("authtest-a1", user_id=owner)
    make_user("a1-staff" + DOMAIN, streamer_id=sid)
    make_user("a1-admin" + DOMAIN, role="admin")
    admin, admin_h = browser(api, "a1-admin" + DOMAIN)
    auth = enroll(api, admin, admin_h, "a1-admin" + DOMAIN)
    with db() as conn:
        tokens_before = conn.execute("SELECT count(*) FROM public.obs_tokens WHERE streamer_id = %s", (sid,)).fetchone()[0]
    set_enforcement("active")
    surface = creator_surface(admin, admin_h, "authtest-a1")
    assert set(surface.values()) == {(403, "mfa_required")}, surface
    assert reason(channel(api, admin, admin_h, "authtest-a1")) == (403, "mfa_required")
    assert reason(admin.get("/api/v1/admin/users")) == (403, "mfa_required")  # unchanged
    with db() as conn:  # nothing happened
        assert conn.execute("SELECT count(*) FROM public.obs_tokens WHERE streamer_id = %s",
                            (sid,)).fetchone()[0] == tokens_before
        assert conn.execute("SELECT goal_name FROM public.streamers WHERE id = %s", (sid,)).fetchone()[0] != "A1"
    # CSRF / Origin still come first; enrollment and ceremonies stay reachable at aal1.
    r = admin.patch(f"{DASH}/authtest-a1/settings", json={"goal_name": "A1"}, headers=ORIGIN)
    assert r.status_code == 403 and "CSRF" in r.text
    assert admin.get(f"{MFA}/status").json()["aal"] == 1
    # The owner and the linked staff account keep their access: they are not acting as administrators.
    for email in ("a1-owner", "a1-staff"):
        b, h = browser(api, email + DOMAIN)
        assert b.get(f"{DASH}/authtest-a1/settings").status_code == 200
        assert channel(api, b, h, "authtest-a1").status_code == 200
    # With assurance the administrator is back in.
    ceremony(admin, admin_h, auth, "session.aal2", "session", {})
    surface = creator_surface(admin, admin_h, "authtest-a1")
    assert admitted(surface), surface
    assert channel(api, admin, admin_h, "authtest-a1").status_code == 200


def test_the_realtime_service_never_signs_for_an_admin_without_an_assurance_check(api):
    """Fail closed: a caller of RealtimeService.authorize_session that passes no assurance check (none exists today
    besides the route) gets no administrator signature, even in draft; owners are unaffected."""
    from app.security.dependencies import SecurityError
    owner = make_user("a1-rt-owner" + DOMAIN)
    make_streamer("authtest-a1-rt", user_id=owner)
    admin_id = make_user("a1-rt-admin" + DOMAIN, role="admin")
    svc, users = api.app_state.realtime, api.app_state.auth.repo
    admin_user, owner_user = (api.portal.call(users.user_by_id, uid) for uid in (admin_id, owner))
    with pytest.raises(SecurityError) as refused:
        api.portal.call(svc.authorize_session, admin_user, "1.2", "private-authtest-a1-rt-dashboard", "authtest-a1-rt",
                        "dashboard")
    assert (refused.value.status, refused.value.reason) == (403, "assurance_unchecked")
    assert "auth" in api.portal.call(svc.authorize_session, owner_user, "1.2", "private-authtest-a1-rt-dashboard",
                                     "authtest-a1-rt", "dashboard")


def test_an_unclaimed_creator_is_admin_only_and_still_needs_assurance(api):
    make_streamer("authtest-a1-unclaimed")
    make_user("a1-admin2" + DOMAIN, role="admin")
    admin, h = browser(api, "a1-admin2" + DOMAIN)
    set_enforcement("active")
    assert reason(admin.get(f"{DASH}/authtest-a1-unclaimed/settings")) == (403, "mfa_required")
    assert reason(channel(api, admin, h, "authtest-a1-unclaimed")) == (403, "mfa_required")


def test_in_draft_an_admin_keeps_creator_access_without_a_passkey(api):
    sid = make_streamer("authtest-a1-draft", user_id=make_user("a1-draft-owner" + DOMAIN))
    assert sid
    make_user("a1-draft-admin" + DOMAIN, role="admin")
    admin, h = browser(api, "a1-draft-admin" + DOMAIN)
    set_enforcement("draft")
    surface = creator_surface(admin, h, "authtest-a1-draft")
    assert admitted(surface), surface
    assert channel(api, admin, h, "authtest-a1-draft").status_code == 200


def test_assurance_that_expired_ends_creator_access_although_the_session_lives_on(api):
    make_streamer("authtest-a1-expiry", user_id=make_user("a1-exp-owner" + DOMAIN))
    make_user("a1-exp-admin" + DOMAIN, role="admin")
    admin, h = browser(api, "a1-exp-admin" + DOMAIN)
    auth = enroll(api, admin, h, "a1-exp-admin" + DOMAIN)
    ceremony(admin, h, auth, "session.aal2", "session", {})
    set_enforcement("active")
    assert admin.get(f"{DASH}/authtest-a1-expiry/settings").status_code == 200
    with fixture_db() as conn:  # assurance never outlives its session (verifier clamp); here it simply runs out
        row = conn.execute("SELECT a.expires_at <= s.absolute_expires_at FROM public.session_assurance a "
                           "JOIN public.sessions s ON s.id = a.session_id JOIN public.users u ON u.id = a.user_id "
                           "WHERE u.email = %s", ("a1-exp-admin" + DOMAIN,)).fetchone()
        assert row == (True,)
    with guards_off("session_assurance") as conn:
        conn.execute("UPDATE public.session_assurance SET verified_at = now() - interval '2 seconds', "
                     "expires_at = now() - interval '1 second' WHERE user_id = "
                     "(SELECT id FROM public.users WHERE email = %s)", ("a1-exp-admin" + DOMAIN,))
    assert admin.get(f"{MFA}/status").json()["aal"] == 1  # still signed in ...
    assert reason(admin.get(f"{DASH}/authtest-a1-expiry/settings")) == (403, "mfa_required")  # ... without the access
    assert reason(channel(api, admin, h, "authtest-a1-expiry")) == (403, "mfa_required")


# ── A4 ──────────────────────────────────────────────────────────────────────────

def session_row(email: str):
    with fixture_db() as conn:
        return conn.execute("""
            SELECT s.id::text, s.absolute_expires_at - s.created_at, s.idle_expires_at - s.last_seen_at
            FROM public.sessions s JOIN public.users u ON u.id = s.user_id
            WHERE u.email = %s AND s.revoked_at IS NULL ORDER BY s.created_at DESC LIMIT 1
        """, (email,)).fetchone()


def close(delta, expected: timedelta) -> bool:
    return abs(delta - expected) < timedelta(minutes=1)


def test_admin_sessions_are_12_hours_absolute_and_2_hours_idle_others_unchanged(app):
    make_user("a4-admin" + DOMAIN, role="admin")
    make_user("a4-user" + DOMAIN, role="user")
    a = new_browser(app)
    assert login(a, "a4-admin" + DOMAIN).status_code == 200
    _, absolute, idle = session_row("a4-admin" + DOMAIN)
    assert close(absolute, timedelta(hours=12)) and close(idle, timedelta(hours=2))
    cookie = next(c for c in a.cookies.jar if c.name == "__Host-hc_session")
    assert cookie.expires is not None  # the cookie follows the session (convenience only)
    u = new_browser(app)
    assert login(u, "a4-user" + DOMAIN).status_code == 200
    _, absolute, idle = session_row("a4-user" + DOMAIN)
    assert close(absolute, timedelta(days=30)) and close(idle, timedelta(days=7))
    # Sliding: an admin request moves the idle expiry 2 hours ahead, never past the absolute one.
    assert a.get("/api/v1/auth/session").status_code == 200
    _, absolute, idle = session_row("a4-admin" + DOMAIN)
    assert close(idle, timedelta(hours=2)) and close(absolute, timedelta(hours=12))
    u.get("/api/v1/auth/session")
    assert close(session_row("a4-user" + DOMAIN)[2], timedelta(days=7))


@pytest.mark.parametrize("aging", ["idle_expired", "absolute_expired", "issued_long_ago", "unused_for_3h"])
def test_an_admin_session_ends_server_side_whatever_the_cookie_says(app, aging):
    email = f"a4-{aging.replace('_', '-')}" + DOMAIN
    make_user(email, role="admin")
    b = new_browser(app)
    assert login(b, email).status_code == 200
    sid = session_row(email)[0]
    sets = {  # issued_long_ago / unused_for_3h: a session issued before the admin limits existed (30 / 7 days stored)
        "idle_expired": "idle_expires_at = now() - interval '1 second'",
        "absolute_expired": "absolute_expires_at = now() - interval '1 second'",
        "issued_long_ago": "created_at = now() - interval '13 hours', absolute_expires_at = now() + interval '20 days', "
                           "idle_expires_at = now() + interval '5 days'",
        "unused_for_3h": "last_seen_at = now() - interval '3 hours', absolute_expires_at = now() + interval '20 days', "
                         "idle_expires_at = now() + interval '5 days'",
    }[aging]
    with guards_off("sessions") as conn:
        conn.execute(f"UPDATE public.sessions SET {sets} WHERE id = %s", (sid,))
    assert b.get("/api/v1/auth/session").status_code == 401


def test_the_same_aging_leaves_an_ordinary_session_alone(app):
    make_user("a4-ordinary" + DOMAIN, role="user")
    b = new_browser(app)
    assert login(b, "a4-ordinary" + DOMAIN).status_code == 200
    with guards_off("sessions") as conn:
        conn.execute("UPDATE public.sessions SET created_at = now() - interval '13 hours', "
                     "last_seen_at = now() - interval '3 hours' WHERE id = %s", (session_row("a4-ordinary" + DOMAIN)[0],))
    assert b.get("/api/v1/auth/session").status_code == 200


def test_google_sign_in_and_an_admin_invitation_also_get_admin_sessions(app):
    from tests.stepup.test_admin_google_link import IP, preexisting_link
    make_user("a4-google" + DOMAIN, role="admin")
    preexisting_link("a4-google" + DOMAIN, "a4-google-subject")
    svc = app.app_state.auth
    svc.google.identity = GoogleIdentity("a4-google-subject", "a4-google" + DOMAIN, True)
    _, issued = app.portal.call(svc.google_login, "code", "v", "n", IP, None)
    assert close(issued.session.absolute_expires_at - issued.session.idle_expires_at, timedelta(hours=10))
    _, absolute, idle = session_row("a4-google" + DOMAIN)
    assert close(absolute, timedelta(hours=12)) and close(idle, timedelta(hours=2))
    token = "a4-invite-token"
    from app.auth.tokens import sha256_hex
    with fixture_db() as conn:
        conn.execute("INSERT INTO public.user_invites (email, role, token_hash, expires_at) VALUES "
                     "(%s, 'admin', %s, now() + interval '1 day')", ("a4-invited" + DOMAIN, sha256_hex(token)))
    b = new_browser(app)
    r = b.post("/api/v1/auth/invites/accept", json={"token": token, "password": "Correct-Horse-9"}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    _, absolute, idle = session_row("a4-invited" + DOMAIN)
    assert close(absolute, timedelta(hours=12)) and close(idle, timedelta(hours=2))


def test_deactivation_ends_an_admin_session_and_reactivation_starts_no_old_one(app):
    make_user("a4-react" + DOMAIN, role="admin")
    make_user("a4-other-admin" + DOMAIN, role="admin")
    b = new_browser(app)
    assert login(b, "a4-react" + DOMAIN).status_code == 200
    with fixture_db() as conn:
        conn.execute("UPDATE public.users SET is_active = false WHERE email = %s", ("a4-react" + DOMAIN,))
    assert b.get("/api/v1/auth/session").status_code == 401
    with fixture_db() as conn:
        conn.execute("UPDATE public.users SET is_active = true WHERE email = %s", ("a4-react" + DOMAIN,))
    assert b.get("/api/v1/auth/session").status_code == 401  # the old session stays ended (revoked on resolve)
    assert login(b, "a4-react" + DOMAIN).status_code == 200
    _, absolute, idle = session_row("a4-react" + DOMAIN)
    assert close(absolute, timedelta(hours=12)) and close(idle, timedelta(hours=2))


# ── A5 ──────────────────────────────────────────────────────────────────────────

def draft_cli(monkeypatch, *argv) -> int:
    from app.stepup.operator import main
    name, port = target()
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", f"host={PG_HOST} port={port} dbname={name} user={FIXTURE_ROLE}")
    return main(["enforcement-draft", *argv])


def state_and_events() -> tuple[str, int, list]:
    with fixture_db() as conn:
        state = conn.execute("SELECT state FROM public.stepup_enforcement").fetchone()[0]
        n = conn.execute("SELECT count(*) FROM public.stepup_events").fetchone()[0]
        last = conn.execute("SELECT operator, detail, db_role FROM public.stepup_events "
                            "WHERE event = 'enforcement_changed' ORDER BY id DESC LIMIT 1").fetchone()
    return state, n, last


def counts() -> tuple:
    with fixture_db() as conn:
        return conn.execute("SELECT (SELECT count(*) FROM public.webauthn_credentials), "
                            "(SELECT count(*) FROM public.session_assurance), "
                            "(SELECT count(*) FROM public.stepup_grants)").fetchone()


@pytest.fixture
def active(env):
    set_enforcement("active")
    yield
    set_enforcement("draft")


def test_enforcement_draft_returns_to_draft_once_with_an_audit_event_and_keeps_everything_else(active, monkeypatch,
                                                                                               capsys):
    name = target()[0]
    before, (_, events_before, _) = counts(), state_and_events()
    assert draft_cli(monkeypatch, "--reason", "admins locked out: verifier down", "--operator", "op-a5",
                     "--confirm-database", name) == 0
    state, events, (operator, detail, role) = state_and_events()
    assert state == "draft" and events == events_before + 1
    assert (operator, detail, role) == ("op-a5", {"from": "active", "to": "draft",
                                                  "reason": "admins locked out: verifier down"}, "hyperchat_owner")
    with fixture_db() as conn:  # the reason lives in the event only; a later change never inherits it
        assert conn.execute("SELECT change_reason FROM public.stepup_enforcement").fetchone()[0] is None
    assert counts() == before  # passkeys, assurance, grants untouched
    assert draft_cli(monkeypatch, "--reason", "again", "--operator", "op-a5", "--confirm-database", name) == 0
    assert "already draft" in capsys.readouterr().out and state_and_events()[1] == events  # nothing recorded
    with guards_off("stepup_enforcement") as conn:  # a later change without a reason records none
        conn.execute("UPDATE public.stepup_enforcement SET state = 'active'")
    with fixture_db() as conn:
        conn.execute("UPDATE public.stepup_enforcement SET state = 'draft', changed_by = 'manual'")
    assert state_and_events()[2][1] == {"from": "active", "to": "draft"}


@pytest.mark.parametrize("argv", [
    ["--reason", " ", "--operator", "op", "--confirm-database", "{db}"],
    ["--reason", "x", "--operator", "", "--confirm-database", "{db}"],
    ["--reason", "x", "--operator", "op", "--confirm-database", "not-this-database"],
    ["--reason", "x" * 501, "--operator", "op", "--confirm-database", "{db}"],
])
def test_enforcement_draft_refuses_bad_arguments_and_the_wrong_database(active, monkeypatch, argv):
    before = state_and_events()
    assert draft_cli(monkeypatch, *[a.replace("{db}", target()[0]) for a in argv]) == 2
    assert state_and_events() == before and before[0] == "active"


def test_enforcement_draft_needs_every_argument(monkeypatch):
    with pytest.raises(SystemExit):
        draft_cli(monkeypatch, "--reason", "x", "--confirm-database", target()[0])


@pytest.mark.parametrize("role", ["hyperchat_app", "hyperchat_worker", "hyperchat_auth", "hyperchat_lifecycle",
                                  "hyperchat_stepup", "hyperchat_readonly"])
def test_no_api_role_can_return_enforcement_to_draft(active, role):
    with db(role) as conn:
        with pytest.raises(REFUSED):
            conn.execute("SELECT public.stepup_enforcement_draft('x', 'y')")
        if role != "hyperchat_readonly":
            with pytest.raises(REFUSED):
                conn.execute("UPDATE public.stepup_enforcement SET state = 'draft', changed_by = 'x', "
                             "change_reason = 'y'")
    assert state_and_events()[0] == "active"


def test_concurrent_rollbacks_change_the_state_once(active):
    _, events_before, _ = state_and_events()
    first = fixture_db()
    results: list = []

    def second():
        with fixture_db() as conn, conn.transaction():
            results.append(conn.execute("SELECT public.stepup_enforcement_draft('op-2', 'second')").fetchone()[0])

    with first:
        with first.transaction():
            assert first.execute("SELECT public.stepup_enforcement_draft('op-1', 'first')").fetchone()[0] == "ok"
            t = threading.Thread(target=second)
            t.start()
            t.join(1)
            assert t.is_alive()  # waits on the row lock
        t.join(20)
    assert results == ["already_draft"]
    state, events, (operator, detail, _) = state_and_events()
    assert (state, events, operator, detail["reason"]) == ("draft", events_before + 1, "op-1", "first")


def test_the_rollback_function_is_owner_only_even_through_set_role(active):
    with db("hyperchat_app") as conn:
        for target_role in ("hyperchat_owner", "hyperchat_migrator"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"SET ROLE {target_role}")
