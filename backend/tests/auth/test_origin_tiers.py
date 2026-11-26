"""PDP v2.0 G1-G3: origin tiers, per-tier CORS, admin-session isolation, link URLs and the Google redirect allow-list.

No database: the app is built as the unit tests build it, and the session layer is a stub. The DEFAULTS tests are
the proof that a deploy without the new variables behaves exactly as before the tiers existed.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.auth.dependencies import CurrentUser, optional_user, require_admin_enrolling, require_user
from app.auth.repository import Session, User
from app.auth.service import AuthError
from app.bots.format import links
from app.core.config import Settings, get_settings
from app.routers.auth import _safe_redirect
from app.security.cors import cors_headers

FRONT = "https://hyperchat.space"
ADMIN = "https://admin.hyperchat.space"
AUTH = "https://auth.hyperchat.space"
DASH = "https://dashboard.hyperchat.space"
OVERLAY = "https://overlay.hyperchat.space"
EVIL = "https://evil.example"
TIERS = {"admin_origins": ADMIN, "session_origins": f"{AUTH},{DASH}", "donor_origins": FRONT,
         "overlay_origins": OVERLAY}
TIER_ENV = {"HYPERCHAT_ADMIN_ORIGINS": ADMIN, "HYPERCHAT_SESSION_ORIGINS": f"{AUTH},{DASH}",
            "HYPERCHAT_DONOR_ORIGINS": FRONT, "HYPERCHAT_OVERLAY_ORIGINS": OVERLAY}
ORIGINS = ["", FRONT, ADMIN, AUTH, DASH, OVERLAY, EVIL, "http://localhost:5173"]
PATHS = ["/api/v1/admin/users", "/api/v1/auth/mfa/status", "/api/v1/auth/session", "/api/v1/public/config",
         "/api/v1/dashboard/streamers/x/settings", "/api/v1/obs/session", "/api/v1/administrator"]


def settings(**kw) -> Settings:
    return Settings(db_enabled=False, **kw)


# ── settings ────────────────────────────────────────────────────────────────────


def test_defaults_every_tier_is_the_flat_browser_origin_set():
    s = settings(allowed_origins="http://localhost:5173")
    assert s.admin_tier == s.session_tier == s.donor_tier == s.overlay_tier == s.browser_origins
    assert s.admin_tier_enforced is False
    assert s.public_link_url == s.auth_link_url == s.dashboard_link_url == FRONT
    assert s.google_redirect_tier == frozenset({FRONT})


def test_an_explicit_tier_is_exactly_its_list():
    s = settings(**TIERS)
    assert s.admin_tier == {ADMIN} and s.session_tier == {AUTH, DASH}
    assert s.donor_tier == {FRONT} and s.overlay_tier == {OVERLAY}
    assert s.admin_tier_enforced is True
    assert s.browser_origins == {FRONT}  # the flat set itself is unchanged


def test_link_urls_follow_their_setting_and_fall_back_to_the_frontend():
    s = settings(frontend_url=f"{FRONT}/", auth_url=f"{AUTH}/", dashboard_url=DASH, public_url=FRONT)
    assert (s.public_link_url, s.auth_link_url, s.dashboard_link_url) == (FRONT, AUTH, DASH)
    assert settings(auth_url=AUTH).dashboard_link_url == FRONT
    assert s.google_redirect_tier == {DASH}
    dashboard = links({"streamer_slug": "ankit"}, None, s.dashboard_link_url)
    assert dashboard == [("📊 Dashboard", f"{DASH}/dashboard/ankit")]


# ── CORS ────────────────────────────────────────────────────────────────────────


def _app(env: dict, monkeypatch) -> TestClient:
    monkeypatch.setenv("HYPERCHAT_DB_ENABLED", "false")
    monkeypatch.setenv("HYPERCHAT_APP_ENV", "test")
    for key in ("HYPERCHAT_ALLOWED_ORIGINS", *TIER_ENV):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    from app.main import create_app

    try:
        return TestClient(create_app(), base_url="https://testserver")
    finally:
        get_settings.cache_clear()


def _preflight(client: TestClient, path: str, origin: str):
    headers = {"access-control-request-method": "POST"} | ({"origin": origin} if origin else {})
    return client.options(path, headers=headers).headers


def test_defaults_cors_is_byte_for_byte_the_flat_rule(monkeypatch):
    client = _app({"HYPERCHAT_ALLOWED_ORIGINS": "http://localhost:5173"}, monkeypatch)
    flat = frozenset({FRONT, "http://localhost:5173"})
    for path in PATHS:
        for origin in ORIGINS:
            got = {k: v for k, v in _preflight(client, path, origin).items() if k.startswith("access-control-")}
            assert got == {k.lower(): v for k, v in cors_headers(origin, flat).items() if k != "Vary"}, (path, origin)


@pytest.mark.parametrize("path, origin, allow, credentials", [
    ("/api/v1/admin/users", ADMIN, ADMIN, True),
    ("/api/v1/auth/mfa/status", ADMIN, ADMIN, True),
    ("/api/v1/admin/users", DASH, None, None),  # the session tier never reaches the admin paths
    ("/api/v1/admin/users", FRONT, None, None),
    ("/api/v1/auth/session", DASH, DASH, True),
    ("/api/v1/auth/session", AUTH, AUTH, True),
    ("/api/v1/auth/session", ADMIN, ADMIN, True),  # the admin app signs in and bootstraps here too
    ("/api/v1/auth/session", FRONT, FRONT, False),  # donor tier: readable, never with cookies
    ("/api/v1/public/config", FRONT, FRONT, False),
    ("/api/v1/obs/session", OVERLAY, OVERLAY, False),
    ("/api/v1/public/config", EVIL, None, None),
])
def test_credentialed_cors_only_for_the_session_and_admin_tiers(monkeypatch, path, origin, allow, credentials):
    headers = _preflight(_app(TIER_ENV, monkeypatch), path, origin)
    assert headers.get("access-control-allow-origin") == allow
    if allow is not None:
        assert (headers.get("access-control-allow-credentials") == "true") is credentials


# ── the session layer: admin isolation and the CSRF Origin tier ─────────────────


@dataclass
class StubAuth:
    settings: Settings
    users: dict

    async def resolve(self, token):
        user = self.users.get(token)
        if user is None:
            return None
        now = datetime.now(UTC)
        return Session(id=f"s-{token}", user_id=user.id, idle_expires_at=now + timedelta(hours=1),
                       absolute_expires_at=now + timedelta(hours=8)), user

    def csrf_token(self, session):
        return f"csrf-{session.id}"


def _user(uid: str, role: str) -> User:
    return User(id=uid, email=f"{uid}@authtest.invalid", username=None, role=role, streamer_id=None, is_active=True,
                password_hash=None, failed_login_attempts=0, locked_until=None)


def _session_app(**kw) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(AuthError, lambda request, exc: JSONResponse({"error": exc.error}, exc.status))
    app.state.auth = StubAuth(settings(**kw), {"admin": _user("admin", "admin"), "creator": _user("c", "streamer")})

    @app.get("/session")
    async def who(current: CurrentUser | None = Depends(optional_user)):
        return {"user": current.user.id if current else None}

    @app.post("/write")
    async def write(current: CurrentUser = Depends(require_user)):
        return {"ok": True}

    @app.get("/admin")
    async def admin(current: CurrentUser = Depends(require_admin_enrolling)):
        return {"ok": True}

    return TestClient(app, base_url="https://testserver")


def _call(client, method, path, who, origin=None):
    headers = {"x-csrf-token": f"csrf-s-{who}"} | ({"origin": origin} if origin else {})
    client.cookies.set("__Host-hc_session", who)
    return client.request(method, path, headers=headers)


@pytest.mark.parametrize("origin", [None, FRONT, DASH, EVIL])
def test_defaults_session_layer_behaves_as_before(origin):
    client = _session_app()  # no tier set: the flat set {FRONT}
    assert _call(client, "GET", "/session", "admin", origin).json() == {"user": "admin"}  # GET never checked Origin
    assert _call(client, "GET", "/admin", "admin", origin).status_code == 200
    ok = origin == FRONT
    assert (_call(client, "POST", "/write", "admin", origin).status_code == 200) is ok
    assert (_call(client, "POST", "/write", "creator", origin).status_code == 200) is ok


def test_an_admin_route_refuses_a_session_tier_origin():
    client = _session_app(**TIERS)
    for origin in (DASH, AUTH, FRONT, OVERLAY, EVIL):
        r = _call(client, "GET", "/admin", "admin", origin)
        assert r.status_code == 401 and r.json()["error"] == "Not signed in"  # hidden, not "you are an admin"
        assert _call(client, "GET", "/session", "admin", origin).json() == {"user": None}
        assert _call(client, "POST", "/write", "admin", origin).status_code == 401
    assert _call(client, "GET", "/admin", "admin", ADMIN).status_code == 200
    assert _call(client, "GET", "/admin", "admin").status_code == 200  # no Origin (not a browser page): as before
    assert _call(client, "POST", "/write", "admin", ADMIN).status_code == 200
    assert _call(client, "POST", "/write", "admin").status_code == 403  # a write always needs its Origin


def test_a_dashboard_route_refuses_an_origin_outside_the_session_tier():
    client = _session_app(**TIERS)
    assert _call(client, "POST", "/write", "creator", DASH).status_code == 200
    assert _call(client, "POST", "/write", "creator", AUTH).status_code == 200
    for origin in (FRONT, OVERLAY, ADMIN, EVIL, None):
        r = _call(client, "POST", "/write", "creator", origin)
        assert r.status_code == 403 and r.json()["error"] == "Request origin not allowed", origin
    assert _call(client, "GET", "/session", "creator", FRONT).json() == {"user": "c"}  # reads unchanged (CORS hides)
    assert _call(client, "GET", "/admin", "creator", ADMIN).status_code == 403  # the role check still applies


# ── donor POSTs and the OBS gate ────────────────────────────────────────────────


DONOR_POSTS = ["/api/v1/public/streamers/authtest-x/donations", "/api/v1/public/signups",
               "/api/v1/public/streamers/authtest-x/upload-grant"]


@pytest.mark.parametrize("path", DONOR_POSTS)
def test_donor_posts_accept_the_donor_tier_only(monkeypatch, path):
    client = _app(TIER_ENV, monkeypatch)
    for origin in (DASH, AUTH, ADMIN, OVERLAY, EVIL):
        r = client.post(path, json={}, headers={"origin": origin})
        assert r.status_code == 403 and r.json()["error"] == "Request origin not allowed", origin
    passed = client.post(path, json={}, headers={"origin": FRONT})  # past the Origin check: no service without a DB
    assert passed.status_code == 503


@pytest.mark.parametrize("path", DONOR_POSTS)
def test_defaults_donor_posts_accept_the_flat_set(monkeypatch, path):
    client = _app({}, monkeypatch)
    assert client.post(path, json={}, headers={"origin": DASH}).status_code == 403
    assert client.post(path, json={}, headers={"origin": FRONT}).status_code == 503


def test_obs_gate_uses_the_overlay_tier(monkeypatch):
    client = _app(TIER_ENV, monkeypatch)
    for origin in (FRONT, DASH, ADMIN):
        assert client.get("/api/v1/obs/session", headers={"origin": origin}).status_code == 403
    for headers in ({"origin": OVERLAY}, {"origin": "https://testserver"}, {}):  # overlay, the API itself, OBS (CEF)
        assert client.get("/api/v1/obs/session", headers=headers).status_code == 503


# ── G3: the Google success redirect ─────────────────────────────────────────────


@pytest.mark.parametrize("target, expected", [
    (None, f"{FRONT}/dashboard"),
    ("/dashboard/ankit?tab=alerts", f"{FRONT}/dashboard/ankit?tab=alerts"),
    ("//evil.example/x", f"{FRONT}/dashboard"),
    ("/\\evil.example", f"{FRONT}/dashboard"),
    (f"{EVIL}/x", f"{FRONT}/dashboard"),
])
def test_defaults_google_redirect_keeps_the_relative_path_rule(target, expected):
    assert _safe_redirect(target, settings()) == expected


@pytest.mark.parametrize("target, expected", [
    ("/onboarding", f"{DASH}/onboarding"),
    (f"{DASH}/onboarding?step=2", f"{DASH}/onboarding?step=2"),
    (f"{DASH}", f"{DASH}/"),
    (f"{AUTH}/welcome", f"{DASH}/dashboard"),  # not on the allow-list
    (f"{EVIL}/x", f"{DASH}/dashboard"),
    ("https://dashboard.hyperchat.space.evil.example/x", f"{DASH}/dashboard"),
    ("https://dashboard.hyperchat.space@evil.example/x", f"{DASH}/dashboard"),
    ("http://dashboard.hyperchat.space/x", f"{DASH}/dashboard"),
    (f"{DASH}//evil.example", f"{DASH}/dashboard"),
    (f"{DASH}/x#frag", f"{DASH}/dashboard"),
    ("javascript:alert(1)", f"{DASH}/dashboard"),
])
def test_google_redirect_refuses_an_unlisted_host(target, expected):
    assert _safe_redirect(target, settings(dashboard_url=DASH)) == expected


def test_google_redirect_allow_list_is_configurable():
    s = settings(dashboard_url=DASH, google_redirect_origins=f"{DASH},{AUTH}")
    assert _safe_redirect(f"{AUTH}/welcome", s) == f"{AUTH}/welcome"
    assert _safe_redirect(f"{FRONT}/x", s) == f"{DASH}/dashboard"


def test_a_stored_target_round_trips_through_the_callback(monkeypatch):
    """/google/start stores the checked absolute target; the callback must land exactly there (a fragment included)
    and still accept a relative path from a cookie issued before G3."""
    from app.auth.tokens import sign_payload
    from app.routers import auth as auth_router

    async def fake_login(*args):
        now = datetime.now(UTC)
        return _user("g", "streamer"), type("Issued", (), {"token": "t", "session": Session(
            id="s", user_id="g", idle_expires_at=now, absolute_expires_at=now + timedelta(hours=1))})()

    s = settings(dashboard_url=DASH, auth_secret="x" * 40)
    stub = type("Svc", (), {"settings": s, "secret": "x" * 40, "google_login": staticmethod(fake_login)})()
    monkeypatch.setattr(auth_router, "auth_service", lambda request: stub)
    app = FastAPI()
    app.include_router(auth_router.router)
    client = TestClient(app, base_url="https://testserver")
    for given, landed in (("/x#frag", f"{DASH}/x#frag"), ("/onboarding", f"{DASH}/onboarding")):
        for stored in (_safe_redirect(given, s), given):  # as /google/start stores it now; a pre-G3 cookie
            client.cookies.set("__Host-hc_oauth", sign_payload("x" * 40, {"s": "st", "n": "n", "v": "v",
                                                                         "r": stored}, 600))
            r = client.get("/api/v1/auth/google/callback", params={"code": "c", "state": "st"},
                           follow_redirects=False)
            assert r.status_code == 302 and r.headers["location"] == landed, (given, stored)


# ── G2: every link is built from its own URL setting ───────────────────────────


class Recorder:
    def __init__(self):
        self.sent = []

    async def send(self, email):
        self.sent.append(email)


class LinkRepo:
    async def user_by_email(self, email):
        return _user("r", "streamer") if email.startswith("known") else None

    async def create_reset_token(self, *args):
        pass

    async def create_invite(self, *args):
        return {"id": "i1", "expires_at": datetime.now(UTC)}


LINKS = {"auth_url": AUTH, "dashboard_url": DASH, "public_url": FRONT, "frontend_url": "https://old.example"}


def test_reset_and_invite_links_use_the_auth_url():
    import asyncio

    from app.auth.service import AuthService

    sender = Recorder()
    svc = AuthService(settings(**LINKS), LinkRepo(), sender, None)
    asyncio.run(svc.send_reset_link("known@authtest.invalid", None))
    asyncio.run(svc.create_invite(_user("admin", "admin"), "new@authtest.invalid", "streamer", None, None))
    assert f"{AUTH}/reset-password?token=" in sender.sent[0].html
    assert f"{AUTH}/invite?token=" in sender.sent[1].html


def test_the_kyc_return_url_uses_the_dashboard_url():
    import asyncio

    from app.kyc.service import KycService

    calls = []

    class Repo:
        async def owns_creator(self, user_id):
            return True

        async def sessions(self, user_id):
            return []

        async def record_session(self, *args):
            return {"session_id": "k1", "provider_status": "Not Started"}

    class Client:
        workflow_id = "wf"

        async def create_session(self, user_id, callback):
            calls.append(callback)
            return type("Created", (), {"url": "https://verify.example/k1"})()

    class Limiter:
        async def hit_rate_limit(self, *args):
            return True

    svc = KycService(Repo(), settings(**LINKS), client=Client(), limiter=Limiter())
    asyncio.run(svc.start(_user("c", "streamer"), None))
    assert calls == [f"{DASH}/kyc/return"]


def test_worker_wiring_bot_links_to_the_dashboard_and_auto_invites_to_the_auth_url(monkeypatch):
    from types import SimpleNamespace

    from app import worker

    handlers = worker.build_handlers(settings(**LINKS), None, None)
    assert handlers["moderator_notify"].frontend_url == DASH
    monkeypatch.setattr(worker, "sender_from_settings", lambda s: Recorder())
    ns = SimpleNamespace(auto_invite_enabled=True, email_backend="resend", email_categories=("onboarding",),
                         auth_link_url=AUTH, invite_ttl_days=7, auto_invite_poll_seconds=60,
                         db_lifecycle_user="hyperchat_lifecycle", db_conninfo=lambda role=None: "dbname=unused")
    assert worker.build_auto_inviter(ns, None).frontend_url == AUTH
