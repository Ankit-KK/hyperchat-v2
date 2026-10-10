import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_factory(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_DB_ENABLED", "false")
    monkeypatch.setenv("HYPERCHAT_APP_ENV", "test")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    yield create_app
    get_settings.cache_clear()


@pytest.fixture
def client(app_factory):
    with TestClient(app_factory()) as c:  # client address "testclient": not loopback, i.e. a public caller
        yield c


def test_public_health_says_only_status_and_database_reachability(client):
    """Security audit F5: no release hash, environment, database label or service name for public callers."""
    for headers in ({}, {"X-Forwarded-For": "127.0.0.1"}):  # the app itself never trusts the header
        r = client.get("/health", headers=headers)
        assert r.status_code == 200 and "X-Request-ID" in r.headers
        assert r.json() == {"status": "ok", "database": {"status": "disabled"}}
        for leaked in ("release", "environment", "label", "DEV", "test", "hyperchat-api", "dev"):
            assert leaked not in r.text


@pytest.mark.parametrize("address", ["127.0.0.1", "::1"])
def test_health_details_for_the_deploy_checks_on_the_server(app_factory, address):
    """curl 127.0.0.1:<port>/health on the server (deploy/README.md) still sees release, environment and label."""
    with TestClient(app_factory(), client=(address, 50000)) as local:
        body = local.get("/health").json()
    assert body["status"] == "ok" and body["service"] == "hyperchat-api"
    assert body["environment"] == "test" and body["release"]
    assert body["database"] == {"label": "DEV", "status": "disabled"}


def test_docs_disabled_by_default(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


@pytest.mark.parametrize("label", ["TEST/RESTORE", "DEV hyperchat_dev", "dev"])
def test_production_refuses_test_or_dev_database(label):
    from app.core.config import Settings

    with pytest.raises(ValueError):
        Settings(app_env="production", db_label=label)


def test_production_refuses_returning_invite_links():
    """Security audit F6: the invite accept URL never reaches an admin in production."""
    from app.core.config import Settings

    with pytest.raises(ValueError, match="HYPERCHAT_AUTH_RETURN_INVITE_LINKS is not allowed in production"):
        Settings(app_env="production", db_label="PROD", auth_return_invite_links=True)
    assert Settings(app_env="production", db_label="PROD").auth_return_invite_links is False
    assert Settings(app_env="development", auth_return_invite_links=True).auth_return_invite_links is True


def test_password_never_in_repr():
    from app.core.config import Settings

    s = Settings(db_password="not-a-real-secret")
    assert "not-a-real-secret" not in repr(s)
