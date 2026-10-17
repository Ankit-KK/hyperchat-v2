"""S2 end to end: the real API (cookies, CSRF, Origin) -> the real verifier over a Unix socket -> PostgreSQL (0025),
with real WebAuthn signatures from a software authenticator. The administrator and session always come from the
server-side session; grants are consumed in the same transaction as the protected operation."""

import asyncio
import logging
import os
import tempfile
import threading
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.stepup.actions import ROUTES, UNBOUND
from app.stepup.canonical import ACTIONS
from tests.integration import (
    DOMAIN,
    FRONT,
    ORIGIN,
    PG_HOST,
    CaptureSender,
    FakeGoogle,
    LogCapture,
    db,
    fixture_db,
    login,
    make_user,
    new_browser,
    requires_db,
    signed_in,
    target,
)
from tests.stepup.authenticator import SoftAuthenticator

pytestmark = requires_db
RP = "hyperchat.space"
MFA = "/api/v1/auth/mfa"
KEYS = "/api/v1/admin/partner-keys"


# ── fixtures: the verifier on a Unix socket, the API pointed at it ──────────────

def _verifier(path: str):
    import uvicorn

    from app.stepup.config import StepupSettings
    from app.stepup.server import create_app as create_verifier
    name, port = target()
    settings = StepupSettings(webauthn_rp_id=RP, webauthn_origins=FRONT, db_host=PG_HOST, db_port=int(port),
                              db_name=name, log_level="WARNING")
    server = uvicorn.Server(uvicorn.Config(create_verifier(settings, environ={}), uds=path, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    return server, thread


def _api(monkeypatch, socket_path: str | None):
    from app.core.config import get_settings
    from app.main import create_app
    if socket_path is None:
        monkeypatch.delenv("HYPERCHAT_STEPUP_SOCKET", raising=False)
    else:
        monkeypatch.setenv("HYPERCHAT_STEPUP_SOCKET", socket_path)
    get_settings.cache_clear()
    application = create_app()
    capture = LogCapture()
    logging.getLogger().addHandler(capture)
    client = TestClient(application, base_url="https://testserver")
    client.__enter__()
    application.state.auth.sender = CaptureSender()
    application.state.auth.google = FakeGoogle()
    client.app_state = application.state
    return client, capture


@pytest.fixture
def api(env, monkeypatch):
    path = os.path.join(tempfile.mkdtemp(prefix="stepup-"), "stepup.sock")
    server, thread = _verifier(path)
    client, capture = _api(monkeypatch, path)
    yield client
    client.__exit__(None, None, None)
    logging.getLogger().removeHandler(capture)
    server.should_exit = True
    thread.join(10)


@pytest.fixture
def api_without_verifier(env, monkeypatch, request):
    path = request.param  # None: not configured; a path: configured but nothing listens there
    client, capture = _api(monkeypatch, path)
    yield client
    client.__exit__(None, None, None)
    logging.getLogger().removeHandler(capture)


# ── helpers ─────────────────────────────────────────────────────────────────────

def admin_browser(api, name: str):
    b = new_browser(api)
    headers = signed_in(b, name + DOMAIN, role="admin")
    return b, headers


def enroll(api, b, headers, email: str) -> SoftAuthenticator:
    auth = SoftAuthenticator(RP, FRONT)
    options = b.post(f"{MFA}/registration/options", headers=headers)
    assert options.status_code == 200, options.text
    r = b.post(f"{MFA}/registration/verify", headers=headers,
               json={"challenge_id": options.json()["challenge_id"],
                     "credential": auth.register(options.json()["public_key"])})
    assert r.status_code == 200 and r.json()["status"] == "pending", r.text
    with fixture_db() as conn:
        assert conn.execute("SELECT public.stepup_approve_credential(%s, %s, 'test-operator', 'test: in person')",
                            (email, r.json()["fingerprint"])).fetchone()[0] == "ok"
    return auth


def ceremony(b, headers, auth, action: str, target_: str, params: dict) -> dict:
    options = b.post(f"{MFA}/assertion/options", headers=headers,
                     json={"action": action, "target": target_, "params": params})
    assert options.status_code == 200, options.text
    r = b.post(f"{MFA}/assertion/verify", headers=headers,
               json={"challenge_id": options.json()["challenge_id"],
                     "credential": auth.assert_(options.json()["public_key"])})
    assert r.status_code == 200, r.text
    return r.json()


def key_grant(b, headers, auth, slug: str) -> str:
    return ceremony(b, headers, auth, "partner_key.create", "partner_key", {"partner_slug": slug})["grant_id"]


def consumed(grant_id: str) -> bool:
    with fixture_db() as conn:
        return conn.execute("SELECT consumed_at IS NOT NULL FROM public.stepup_grants WHERE id = %s",
                            (grant_id,)).fetchone()[0]


def keys(slug: str) -> int:
    with db() as conn:
        return conn.execute("SELECT count(*) FROM public.partner_api_keys WHERE partner_slug = %s", (slug,)).fetchone()[0]


# ── who may call ────────────────────────────────────────────────────────────────

ENDPOINTS = [("GET", "/status"), ("GET", "/credentials"), ("POST", "/registration/options"),
             ("POST", "/registration/verify"), ("POST", "/assertion/options"), ("POST", "/assertion/verify")]


def test_only_a_signed_in_admin_with_csrf_and_origin_reaches_the_passkey_endpoints(api):
    anonymous = new_browser(api)
    for method, path in ENDPOINTS:
        assert anonymous.request(method, MFA + path, headers=ORIGIN, json={}).status_code == 401, path
    user = new_browser(api)
    user_headers = signed_in(user, "mfa-user" + DOMAIN, role="user")
    for method, path in ENDPOINTS:
        assert user.request(method, MFA + path, headers=user_headers, json={}).status_code == 403, path
    b, headers = admin_browser(api, "mfa-csrf")
    assert b.post(f"{MFA}/registration/options", headers=ORIGIN).status_code == 403  # no CSRF token
    assert b.post(f"{MFA}/registration/options",
                  headers={**headers, "origin": "https://evil.example"}).status_code == 403  # cross-origin
    assert b.post(f"{MFA}/registration/options", headers=headers).status_code == 200


def test_the_admin_and_session_come_only_from_the_server_side_session(api):
    b, headers = admin_browser(api, "mfa-spoof")
    victim = make_user("mfa-victim" + DOMAIN, role="admin")
    for path, body in (("/registration/options", {"user_id": victim}),
                       ("/assertion/options", {"action": "session.aal2", "target": "session", "params": {},
                                               "session_id": str(uuid.uuid4())}),
                       ("/assertion/verify", {"challenge_id": str(uuid.uuid4()), "credential": {}, "user_id": victim})):
        assert b.post(MFA + path, headers=headers, json=body).status_code == 422, path  # no such field


def test_an_expired_or_revoked_session_cannot_use_the_endpoints(api):
    b, headers = admin_browser(api, "mfa-revoked")
    with fixture_db() as conn:
        conn.execute("UPDATE public.sessions SET revoked_at = now() WHERE user_id = (SELECT id FROM public.users "
                     "WHERE email = %s)", ("mfa-revoked" + DOMAIN,))
    assert b.post(f"{MFA}/registration/options", headers=headers).status_code == 401
    assert b.get(f"{MFA}/status").status_code == 401


# ── enrollment and assurance ────────────────────────────────────────────────────

def test_enrollment_pending_approval_and_session_assurance(api):
    b, headers = admin_browser(api, "mfa-flow")
    status = b.get(f"{MFA}/status").json()
    assert status == {"enforcement": "draft", "aal": 1, "mfa_required": False, "verifier": "ok", "credentials": []}
    auth = SoftAuthenticator(RP, FRONT)
    options = b.post(f"{MFA}/registration/options", headers=headers).json()
    r = b.post(f"{MFA}/registration/verify", headers=headers,
               json={"challenge_id": options["challenge_id"], "credential": auth.register(options["public_key"])})
    fingerprint = r.json()["fingerprint"]
    listed = b.get(f"{MFA}/credentials").json()["credentials"]
    assert [(c["fingerprint"], c["status"]) for c in listed] == [(fingerprint, "pending")]
    assert "public_key" not in listed[0] and "credential_id" not in listed[0]
    pending = b.post(f"{MFA}/assertion/options", headers=headers,
                     json={"action": "session.aal2", "target": "session", "params": {}})
    assert (pending.status_code, pending.json()["reason"]) == (409, "no_active_credential")
    with fixture_db() as conn:
        conn.execute("SELECT public.stepup_approve_credential(%s, %s, 'op', 'test')", ("mfa-flow" + DOMAIN, fingerprint))
    assert ceremony(b, headers, auth, "session.aal2", "session", {})["aal"] == 2
    assert b.get(f"{MFA}/status").json()["aal"] == 2
    other, _ = admin_browser(api, "mfa-flow-other")  # assurance is per session: another admin's session has none
    assert other.get(f"{MFA}/status").json()["aal"] == 1


def test_a_wrong_origin_signature_or_replayed_ceremony_is_refused(api):
    b, headers = admin_browser(api, "mfa-bad")
    auth = enroll(api, b, headers, "mfa-bad" + DOMAIN)
    options = b.post(f"{MFA}/assertion/options", headers=headers,
                     json={"action": "session.aal2", "target": "session", "params": {}}).json()
    evil = auth.assert_(options["public_key"], origin="https://evil.example")
    r = b.post(f"{MFA}/assertion/verify", headers=headers, json={"challenge_id": options["challenge_id"],
                                                                 "credential": evil})
    assert (r.status_code, r.json()["reason"]) == (400, "verification_failed")
    good = auth.assert_(options["public_key"])
    r = b.post(f"{MFA}/assertion/verify", headers=headers, json={"challenge_id": options["challenge_id"],
                                                                 "credential": good})
    assert (r.status_code, r.json()["reason"]) == (409, "challenge_used")


# ── action-bound grants ─────────────────────────────────────────────────────────

def test_a_grant_authorizes_exactly_its_action_once_in_the_operations_own_transaction(api):
    b, headers = admin_browser(api, "mfa-grant")
    auth = enroll(api, b, headers, "mfa-grant" + DOMAIN)
    grant = key_grant(b, headers, auth, "authtest-mfa-a")
    # Another parameter (slug): refused, nothing created, and the grant is NOT spent (the transaction rolled back).
    r = b.post(KEYS, headers={**headers, "x-step-up-grant": grant}, json={"partner_slug": "authtest-mfa-b"})
    assert (r.status_code, r.json()["reason"], r.json()["detail"]) == (403, "step_up_refused", "mismatch")
    assert keys("authtest-mfa-b") == 0 and not consumed(grant)
    r = b.post(KEYS, headers={**headers, "x-step-up-grant": grant}, json={"partner_slug": "authtest-mfa-a"})
    assert r.status_code == 201 and keys("authtest-mfa-a") == 1 and consumed(grant)
    r = b.post(KEYS, headers={**headers, "x-step-up-grant": grant}, json={"partner_slug": "authtest-mfa-a"})
    assert (r.status_code, r.json()["detail"]) == (403, "consumed") and keys("authtest-mfa-a") == 1  # no replay
    r = b.post(KEYS, headers={**headers, "x-step-up-grant": "not-a-uuid"}, json={"partner_slug": "authtest-mfa-a"})
    assert (r.status_code, r.json()["detail"]) == (403, "malformed")


def test_a_grant_cannot_be_reused_from_another_session_or_another_admin(api):
    b, headers = admin_browser(api, "mfa-cross")
    auth = enroll(api, b, headers, "mfa-cross" + DOMAIN)
    grant = key_grant(b, headers, auth, "authtest-mfa-cross")
    second = new_browser(api)  # the same administrator, a second session
    second_headers = {**ORIGIN, "x-csrf-token": login(second, "mfa-cross" + DOMAIN).json()["csrf_token"]}
    other, other_headers = admin_browser(api, "mfa-cross-other")
    for browser, h in ((second, second_headers), (other, other_headers)):
        r = browser.post(KEYS, headers={**h, "x-step-up-grant": grant}, json={"partner_slug": "authtest-mfa-cross"})
        assert (r.status_code, r.json()["detail"]) == (403, "mismatch")
    assert keys("authtest-mfa-cross") == 0 and not consumed(grant)


def test_a_grant_for_one_action_does_not_authorize_another(api):
    b, headers = admin_browser(api, "mfa-other-action")
    auth = enroll(api, b, headers, "mfa-other-action" + DOMAIN)
    uid = make_user("mfa-other-target" + DOMAIN, role="user")
    grant = ceremony(b, headers, auth, "admin.user.change", f"user:{uid}", {"role": "streamer"})["grant_id"]
    r = b.post(KEYS, headers={**headers, "x-step-up-grant": grant}, json={"partner_slug": "authtest-mfa-x"})
    assert (r.status_code, r.json()["detail"]) == (403, "mismatch")  # another action (partner key)
    r = b.patch(f"/api/v1/admin/users/{uid}", headers={**headers, "x-step-up-grant": grant}, json={"role": "user"})
    assert (r.status_code, r.json()["detail"]) == (403, "mismatch")  # the same action, other parameters
    r = b.patch(f"/api/v1/admin/users/{uid}", headers={**headers, "x-step-up-grant": grant}, json={"role": "streamer"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "streamer" and consumed(grant)


def test_a_refused_operation_never_spends_its_grant(api):
    """The lifecycle function refuses (a stale compare-and-set: the creator is active, not suspended): the grant's
    consumption, made in the same hyperchat_lifecycle transaction, rolls back with it."""
    from tests.integration import make_streamer
    b, headers = admin_browser(api, "mfa-refused")
    auth = enroll(api, b, headers, "mfa-refused" + DOMAIN)
    sid = make_streamer("authtest-mfa-refused")  # active
    body = {"expected_status": "suspended", "reason": None}
    grant = ceremony(b, headers, auth, "creator.reactivate", f"creator:{sid}", body)["grant_id"]
    r = b.post(f"/api/v1/admin/creators/{sid}/reactivate", headers={**headers, "x-step-up-grant": grant}, json=body)
    assert r.status_code == 409 and "reason" not in r.json()  # the operation's own refusal, not the grant's
    assert not consumed(grant)
    with db() as conn:
        assert conn.execute("SELECT status FROM public.streamers WHERE id = %s", (sid,)).fetchone()[0] == "active"


def test_a_change_that_changes_nothing_never_spends_its_grant(api):
    b, headers = admin_browser(api, "mfa-noop")
    auth = enroll(api, b, headers, "mfa-noop" + DOMAIN)
    uid = make_user("mfa-noop-target" + DOMAIN, role="streamer")  # already a streamer
    grant = ceremony(b, headers, auth, "admin.user.change", f"user:{uid}", {"role": "streamer"})["grant_id"]
    r = b.patch(f"/api/v1/admin/users/{uid}", headers={**headers, "x-step-up-grant": grant}, json={"role": "streamer"})
    assert r.status_code == 200 and r.json()["changed"] == []
    assert not consumed(grant)


def test_concurrent_requests_with_one_grant_succeed_exactly_once(api):
    b, headers = admin_browser(api, "mfa-race")
    auth = enroll(api, b, headers, "mfa-race" + DOMAIN)
    grant = key_grant(b, headers, auth, "authtest-mfa-race")
    cookies = {"__Host-hc_session": b.cookies.get("__Host-hc_session")}

    async def both():
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver", cookies=cookies) as client:
            return await asyncio.gather(*(client.post(KEYS, headers={**headers, "x-step-up-grant": grant},
                                                      json={"partner_slug": "authtest-mfa-race"}) for _ in range(2)))
    codes = sorted(r.status_code for r in api.portal.call(both))
    assert codes == [201, 403] and keys("authtest-mfa-race") == 1


def test_draft_mode_keeps_existing_admin_access_without_passkeys(api):
    b, headers = admin_browser(api, "mfa-draft")  # no passkey at all
    assert b.get("/api/v1/admin/users").status_code == 200
    r = b.post(KEYS, headers=headers, json={"partner_slug": "authtest-mfa-draft"})
    assert r.status_code == 201 and keys("authtest-mfa-draft") == 1


def test_active_enforcement_requires_assurance_and_a_grant_for_exactly_this_action(api, monkeypatch):
    """Activation itself is S3 and the operator's; here the state is simulated to test the API's behaviour."""
    b, headers = admin_browser(api, "mfa-active")
    auth = enroll(api, b, headers, "mfa-active" + DOMAIN)
    repo = api.app_state.auth.repo
    real_session = repo.stepup_session

    async def active_session(session_id):
        return "active", (await real_session(session_id))[1]

    async def active():
        return "active"
    monkeypatch.setattr(repo, "stepup_session", active_session)
    monkeypatch.setattr(repo, "stepup_enforcement", active)
    r = b.get("/api/v1/admin/users")
    assert (r.status_code, r.json()["reason"]) == (403, "mfa_required")
    assert b.get(f"{MFA}/status").json()["mfa_required"] is True  # enrollment endpoints stay reachable
    ceremony(b, headers, auth, "session.aal2", "session", {})
    assert b.get("/api/v1/admin/users").status_code == 200
    r = b.post(KEYS, headers=headers, json={"partner_slug": "authtest-mfa-active"})
    assert r.status_code == 403 and r.json()["reason"] == "step_up_required"
    assert r.json()["step_up"] == {"action": "partner_key.create", "target": "partner_key",
                                   "params": {"partner_slug": "authtest-mfa-active"}}
    grant = key_grant(b, headers, auth, "authtest-mfa-active")
    assert b.post(KEYS, headers={**headers, "x-step-up-grant": grant},
                  json={"partner_slug": "authtest-mfa-active"}).status_code == 201


def test_revoked_passkeys_end_their_grants_and_assurance(api):
    b, headers = admin_browser(api, "mfa-revoke")
    auth = enroll(api, b, headers, "mfa-revoke" + DOMAIN)
    ceremony(b, headers, auth, "session.aal2", "session", {})
    grant = key_grant(b, headers, auth, "authtest-mfa-revoke")
    fingerprint = b.get(f"{MFA}/credentials").json()["credentials"][0]["fingerprint"]
    with fixture_db() as conn:
        conn.execute("SELECT public.stepup_revoke_credential(%s, %s, 'op', 'lost')", ("mfa-revoke" + DOMAIN, fingerprint))
    r = b.post(KEYS, headers={**headers, "x-step-up-grant": grant}, json={"partner_slug": "authtest-mfa-revoke"})
    assert (r.status_code, r.json()["detail"]) == (403, "credential_revoked")
    assert b.get(f"{MFA}/status").json()["aal"] == 1


def test_the_passkey_endpoints_are_rate_limited_per_admin(api, monkeypatch):
    from app.stepup import service
    monkeypatch.setitem(service.LIMITS, "user", ("auth:mfa", 3, 600))
    b, _ = admin_browser(api, "mfa-limit")
    codes = [b.get(f"{MFA}/credentials").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert b.get(f"{MFA}/credentials").json()["reason"] == "rate_limited"


def test_no_web_server_configuration_exposes_the_verifier_socket():
    from tests.test_deploy_production import DEPLOY
    for conf in (DEPLOY / "nginx").rglob("*.conf"):
        text = conf.read_text()
        assert "stepup" not in text and "unix:" not in text, conf


# ── fail closed ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("api_without_verifier", [None, "/nonexistent/stepup.sock"], indirect=True)
def test_without_the_verifier_ceremonies_fail_closed_and_admin_access_is_unchanged(api_without_verifier):
    api = api_without_verifier
    b, headers = admin_browser(api, "mfa-down")
    expected = "stepup_not_configured" if api.app_state.mfa.client is None else "stepup_unavailable"
    for path, body in (("/registration/options", None), ("/assertion/options",
                       {"action": "session.aal2", "target": "session", "params": {}})):
        r = b.post(MFA + path, headers=headers, json=body)
        assert (r.status_code, r.json()["reason"]) == (503, expected), path
    status = b.get(f"{MFA}/status").json()
    assert status["verifier"] == ("not_configured" if expected == "stepup_not_configured" else "unavailable")
    assert status["credentials"] is None
    assert b.get("/api/v1/admin/users").status_code == 200  # draft: nothing else depends on the verifier


def test_every_mapped_route_consumes_the_presented_grant_in_its_own_transaction(api):
    """A grant id that does not exist is refused by stepup_consume on every mapped route the request reaches the
    database on (proof that each repository consumes it); nothing is changed. Creator creation, application approval
    and activation refuse earlier here for their own reasons (no Razorpay account configured in tests)."""
    from tests.integration import make_streamer
    b, headers = admin_browser(api, "mfa-plumbing")
    user_id = make_user("mfa-plumbing-user" + DOMAIN, role="user")
    owner_id = make_user("mfa-plumbing-owner" + DOMAIN, role="streamer")
    unclaimed = make_streamer("authtest-mfa-unclaimed")
    claimed = make_streamer("authtest-mfa-claimed", user_id=owner_id)
    forged = {**headers, "x-step-up-grant": str(uuid.uuid4())}
    calls = [
        ("PATCH", f"/api/v1/admin/users/{user_id}", {"role": "streamer"}),
        ("PUT", f"/api/v1/admin/users/{user_id}/email", {"email": "mfa-plumbing-new" + DOMAIN}),
        ("POST", f"/api/v1/admin/creators/{unclaimed}/owner", {"user_id": user_id}),
        ("POST", f"/api/v1/admin/creators/{claimed}/owner/transfer", {"user_id": user_id, "expected_owner_id": owner_id}),
        ("POST", f"/api/v1/admin/creators/{claimed}/reactivate", {"expected_status": "suspended"}),
        ("POST", f"/api/v1/admin/applications/{uuid.uuid4()}/invite", None),
        ("POST", "/api/v1/auth/invites", {"email": "mfa-plumbing-admin" + DOMAIN, "role": "admin"}),
        ("POST", KEYS, {"partner_slug": "authtest-mfa-plumbing"}),
    ]
    for method, path, body in calls:
        r = b.request(method, path, headers=forged, json=body)
        assert (r.status_code, r.json().get("reason"), r.json().get("detail")) == (403, "step_up_refused", "not_found"), \
            (path, r.text)
    with db() as conn:
        assert conn.execute("SELECT role, email::text FROM public.users WHERE id = %s", (user_id,)).fetchone() == (
            "user", "mfa-plumbing-user" + DOMAIN)
        assert conn.execute("SELECT count(*) FROM public.user_invites WHERE email = %s",
                            ("mfa-plumbing-admin" + DOMAIN,)).fetchone()[0] == 0
    assert keys("authtest-mfa-plumbing") == 0
    # An ordinary invitation is not a step-up action: the header is ignored and the invite is created.
    r = b.post("/api/v1/auth/invites", headers=forged, json={"email": "mfa-plumbing-plain" + DOMAIN, "role": "user"})
    assert r.status_code == 201, r.text


# ── the route map ───────────────────────────────────────────────────────────────

def test_every_action_is_bound_to_a_route_or_explained(api):
    actions = {a for a, purpose in ACTIONS.items() if purpose == "action"}
    assert set(ROUTES.values()) | set(UNBOUND) == actions and not set(ROUTES.values()) & set(UNBOUND)
    from app.routers import admin, auth, partners
    routes = {(m, r.path): r for router in (admin.router, auth.router, partners.admin_router)
              for r in router.routes for m in r.methods}
    for (method, path), action in ROUTES.items():
        route = routes[(method, path)]
        names = {getattr(d.call, "__qualname__", "") for d in route.dependant.dependencies}
        assert any(n.startswith("step_up.") for n in names), (path, names)
