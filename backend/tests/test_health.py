import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_DB_ENABLED", "false")
    monkeypatch.setenv("HYPERCHAT_APP_ENV", "test")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def test_health_ok_without_database(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"]["status"] == "disabled"
    assert body["database"]["label"] == "DEV"
    assert "X-Request-ID" in r.headers


def test_docs_disabled_by_default(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


@pytest.mark.parametrize("label", ["TEST/RESTORE", "DEV hyperchat_dev", "dev"])
def test_production_refuses_test_or_dev_database(label):
    from app.core.config import Settings

    with pytest.raises(ValueError):
        Settings(app_env="production", db_label=label)


def test_password_never_in_repr():
    from app.core.config import Settings

    s = Settings(db_password="not-a-real-secret")
    assert "not-a-real-secret" not in repr(s)
