"""HYPERCHAT_ALLOWED_ORIGINS end to end: an extra origin is trusted only when configured."""

import pytest
from fastapi.testclient import TestClient

from tests.integration import DOMAIN, make_user, requires_db

pytestmark = requires_db
DEV_ORIGIN = "http://localhost:5173"


def _client(monkeypatch, allowed: str | None):
    if allowed is None:
        monkeypatch.delenv("HYPERCHAT_ALLOWED_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("HYPERCHAT_ALLOWED_ORIGINS", allowed)
    from app.main import create_app

    return TestClient(create_app(), base_url="https://testserver")


def _login(client, origin):
    return client.post("/api/v1/auth/login", json={"email": "origin" + DOMAIN, "password": "Correct-Horse-9"},
                       headers={"origin": origin})


@pytest.mark.parametrize("allowed, dev_ok", [(None, False), (DEV_ORIGIN, True)])
def test_extra_origin_only_when_configured(env, monkeypatch, allowed, dev_ok):
    make_user("origin" + DOMAIN)
    with _client(monkeypatch, allowed) as client:
        r = _login(client, DEV_ORIGIN)
        assert (r.status_code == 200) is dev_ok
        assert (r.headers.get("access-control-allow-origin") == DEV_ORIGIN) is dev_ok
        pre = client.options("/api/v1/auth/session", headers={"origin": DEV_ORIGIN,
                                                              "access-control-request-method": "GET"})
        assert (pre.headers.get("access-control-allow-origin") == DEV_ORIGIN) is dev_ok
        assert _login(client, "https://hyperchat.space").status_code == 200  # the frontend always works
        assert _login(client, "https://www.hyperchat.space").status_code == 403  # not configured here


TIERS = {"HYPERCHAT_ADMIN_ORIGINS": "https://admin.hyperchat.space",
         "HYPERCHAT_SESSION_ORIGINS": "https://auth.hyperchat.space,https://dashboard.hyperchat.space",
         "HYPERCHAT_DONOR_ORIGINS": "https://hyperchat.space"}


def test_tiers_end_to_end_admin_session_answers_only_to_the_admin_host(env, monkeypatch):
    """PDP v2.0 G1 with the real session layer (tests/auth/test_origin_tiers.py covers the rules without a DB)."""
    for key, value in TIERS.items():
        monkeypatch.setenv(key, value)
    make_user("tier-admin" + DOMAIN, role="admin")
    make_user("tier-creator" + DOMAIN)
    admin_host, dash = {"origin": "https://admin.hyperchat.space"}, {"origin": "https://dashboard.hyperchat.space"}
    creds = {"password": "Correct-Horse-9"}
    with _client(monkeypatch, None) as client:
        assert client.post("/api/v1/auth/login", json={"email": "tier-admin" + DOMAIN} | creds,
                           headers={"origin": "https://hyperchat.space"}).status_code == 403  # donor tier: no login
        r = client.post("/api/v1/auth/login", json={"email": "tier-admin" + DOMAIN} | creds, headers=admin_host)
        assert r.status_code == 200 and r.json()["user"]["role"] == "admin" and r.json()["csrf_token"]
        assert client.get("/api/v1/admin/users", headers=admin_host).status_code == 200
        assert client.get("/api/v1/admin/users", headers=dash).status_code == 401  # a session-tier origin
        assert client.get("/api/v1/auth/session", headers=dash).status_code == 401  # cannot even learn it
        assert client.get("/api/v1/auth/session", headers=admin_host).status_code == 200
    with _client(monkeypatch, None) as client:
        r = client.post("/api/v1/auth/login", json={"email": "tier-creator" + DOMAIN} | creds, headers=dash)
        assert r.status_code == 200
        csrf = {"x-csrf-token": r.json()["csrf_token"]}
        donor = {"origin": "https://hyperchat.space"}
        assert client.post("/api/v1/auth/logout", headers=csrf | donor).status_code == 403
        assert client.post("/api/v1/auth/logout", headers=csrf | dash).status_code == 200
