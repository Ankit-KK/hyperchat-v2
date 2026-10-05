"""Phase 5 pure logic and HTTP plumbing (no database)."""

import pytest
from fastapi.testclient import TestClient

from app.dashboard.validation import parse_uuid
from app.routers.dashboard import is_private_path


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_DB_ENABLED", "false")
    monkeypatch.setenv("HYPERCHAT_APP_ENV", "test")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app(), base_url="https://testserver") as c:
        yield c
    get_settings.cache_clear()


# ── Cache-Control: no-store, scoped to dashboard + terms ────────────────────────

PRIVATE = ["/api/v1/dashboard/streamers/x/settings", "/api/v1/dashboard/streamers/x/donations/export.csv",
           "/api/v1/dashboard/anything", "/api/v1/me/terms", "/api/v1/me/terms/accept"]
NOT_PRIVATE = ["/health", "/api/v1/me/streamers", "/api/v1/me/termsx", "/api/v1/auth/session",
               "/api/v1/public/streamers/x", "/api/v1/metrics/platform", "/api/v1/streamers/x/access",
               "/api/v1/dashboardx", "/api/v1/admin/users"]


def test_private_path_scope():
    assert all(is_private_path(p) for p in PRIVATE)
    assert not any(is_private_path(p) for p in NOT_PRIVATE)


def test_no_store_on_dashboard_and_terms_only(client):
    for path in PRIVATE:
        for method in ("GET", "POST", "PATCH", "DELETE"):
            r = client.request(method, path)
            assert r.headers.get("cache-control") == "no-store", (method, path, r.status_code)
    for path in NOT_PRIVATE:
        r = client.get(path)
        assert r.headers.get("cache-control") != "no-store", (path, r.status_code)


# ── ids ─────────────────────────────────────────────────────────────────────────

def test_parse_uuid():
    assert parse_uuid("6F9619FF-8B86-D011-B42D-00C04FC964FF") == "6f9619ff-8b86-d011-b42d-00c04fc964ff"
    for bad in ("", "nope", "123", None, "6f9619ff-8b86-d011-b42d-00c04fc964f", "' OR 1=1 --"):
        assert parse_uuid(bad) is None
