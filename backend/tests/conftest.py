import logging
import os

import pytest
from fastapi.testclient import TestClient

from app.auth.tokens import new_token
from app.core.config import get_settings
from tests.integration import (
    FRONT,
    NEVER_TOUCH,
    CaptureSender,
    FakeGoogle,
    LogCapture,
    PG_HOST,
    cleanup_synthetic_rows,
    current_database,
    target,
)


@pytest.fixture(scope="session")
def scratch_db() -> str:
    """Name of the integration database, after refusing production and the restore source.
    hyperchat_dev (the dev API's database) needs an explicit opt-in."""
    name = current_database()
    assert name not in NEVER_TOUCH, "never production or the restore source"
    if name == "hyperchat_dev":
        assert os.environ.get("HYPERCHAT_TEST_AUTH_ALLOW_DEV") == "1", "set HYPERCHAT_TEST_AUTH_ALLOW_DEV=1"
    return name


@pytest.fixture
def strict_scratch_db(scratch_db) -> str:
    """For tests that affect rows they did not create (e.g. cleanup): never on hyperchat_dev."""
    assert scratch_db != "hyperchat_dev", "this test only runs on a scratch database"
    return scratch_db


@pytest.fixture
def env(monkeypatch, scratch_db):
    name, port = target()
    for key, value in {
        "HYPERCHAT_DB_NAME": name, "HYPERCHAT_DB_PORT": port, "HYPERCHAT_DB_USER": "hyperchat_app",
        "HYPERCHAT_DB_LIFECYCLE_USER": "hyperchat_lifecycle",
        "HYPERCHAT_DB_HOST": PG_HOST, "HYPERCHAT_DB_LABEL": "TEST-AUTH-SCRATCH",
        "HYPERCHAT_AUTH_SECRET": new_token(32),  # random per test, never printed
        "HYPERCHAT_FRONTEND_URL": FRONT, "HYPERCHAT_EMAIL_BACKEND": "disabled",
        "HYPERCHAT_AUTH_RETURN_INVITE_LINKS": "false", "HYPERCHAT_LOG_LEVEL": "DEBUG",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    cleanup_synthetic_rows()
    yield
    get_settings.cache_clear()
    cleanup_synthetic_rows()


@pytest.fixture
def app(env):
    """The real app (create_app + lifespan) with in-memory email and Google fakes."""
    from app.main import create_app

    application = create_app()
    capture = LogCapture()
    logging.getLogger().addHandler(capture)
    with TestClient(application, base_url="https://testserver") as client:
        application.state.auth.sender = CaptureSender()
        application.state.auth.google = FakeGoogle()
        application.state.log_capture = capture
        client.app_state = application.state
        yield client
    logging.getLogger().removeHandler(capture)
