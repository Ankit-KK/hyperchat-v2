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
