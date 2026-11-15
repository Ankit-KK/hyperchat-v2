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


@pytest.fixture
def accounting_active():
    """PAY-15 (migration 0034): accounting state 'active' with dispute mode 'apply' for tests of the evaluation path
    (their assertions predate the switch); frozen afterwards, so the next test starts from a non-active state."""
    from tests.integration import set_accounting

    set_accounting("active", "apply")
    yield
    set_accounting("frozen")


VERIFY_TEMPLATE = "hyperchat_verify_tmpl"
VERIFY_ROLES = ("hyperchat_migrator, hyperchat_app, hyperchat_worker, hyperchat_readonly, hyperchat_lifecycle, "
                "hyperchat_auth, hyperchat_stepup")


@pytest.fixture
def verify_db():
    """PAY-15 6a-2 (plan §9): a fresh database copied from the pristine template at head (setup-test-db.sh), dropped
    afterwards. HYPERCHAT_TEST_VERIFY_ADMIN is the throwaway server's superuser connection (CI only)."""
    import secrets

    import psycopg

    admin = os.environ.get("HYPERCHAT_TEST_VERIFY_ADMIN", "")
    if not admin:
        pytest.skip("HYPERCHAT_TEST_VERIFY_ADMIN not set")
    name = f"hyperchat_verify_{secrets.token_hex(5)}"
    with psycopg.connect(admin, autocommit=True) as conn:
        assert conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]
        conn.execute(f'CREATE DATABASE "{name}" TEMPLATE {VERIFY_TEMPLATE} OWNER hyperchat_owner')
        conn.execute(f'REVOKE ALL ON DATABASE "{name}" FROM PUBLIC')
        conn.execute(f'GRANT CONNECT ON DATABASE "{name}" TO {VERIFY_ROLES}')
    try:
        yield name
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


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
        "HYPERCHAT_DB_LIFECYCLE_USER": "hyperchat_lifecycle", "HYPERCHAT_DB_AUTH_USER": "hyperchat_auth",
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
