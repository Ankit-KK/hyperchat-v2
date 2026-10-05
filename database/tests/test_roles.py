"""Least-privilege checks per runtime role, against an Alembic-migrated database.

Enabled when HYPERCHAT_TEST_ROLE_DB is set to "<dbname>:<port>" and the test runs as an OS user
mapped to the roles by pg_ident (root on the VPS). Every write is rolled back.
"""

import os

import psycopg
import pytest

TARGET = os.environ.get("HYPERCHAT_TEST_ROLE_DB", "")
# Unix-socket directory (peer auth on the VPS) or a TCP host (CI's PostgreSQL service container).
PG_HOST = os.environ.get("HYPERCHAT_TEST_PG_HOST", "/var/run/postgresql")
pytestmark = pytest.mark.skipif(not TARGET, reason="HYPERCHAT_TEST_ROLE_DB not set")


def _conn(role: str) -> psycopg.Connection:
    db, port = TARGET.split(":")
    return psycopg.connect(f"host={PG_HOST} port={port} dbname={db} user={role}")


@pytest.fixture(scope="module", autouse=True)
def _scratch_only():
    with _conn("hyperchat_readonly") as conn:
        name = conn.execute("SELECT current_database()").fetchone()[0]
    assert name not in ("hyperchat", "hyperchat_restore_test", "hyperchat_dev"), "tests must use a scratch DB"


def _fails(role: str, statement: str) -> bool:
    with _conn(role) as conn:
        try:
            conn.execute(statement)
        except psycopg.errors.InsufficientPrivilege:
            return True
        except psycopg.errors.ReadOnlySqlTransaction:
            return True
        finally:
            conn.rollback()
    return False


@pytest.mark.parametrize("role", ["hyperchat_app", "hyperchat_worker", "hyperchat_readonly"])
def test_runtime_roles_cannot_run_ddl(role):
    assert _fails(role, "CREATE TABLE public.p1_should_fail (id int)")
    assert _fails(role, "ALTER TABLE public.donations ADD COLUMN x int")
    assert _fails(role, "DROP TABLE public.reviews")


@pytest.mark.parametrize("role", ["hyperchat_app", "hyperchat_worker", "hyperchat_readonly"])
def test_runtime_roles_cannot_touch_alembic_version(role):
    assert _fails(role, "UPDATE public.alembic_version SET version_num = version_num")
    assert _fails(role, "DELETE FROM public.alembic_version")


@pytest.mark.parametrize("role", ["hyperchat_app", "hyperchat_worker"])
def test_runtime_roles_can_do_dml(role):
    with _conn(role) as conn:
        conn.execute("INSERT INTO public.contact_submissions (name, email, message) "
                     "VALUES ('Phase1 test', 'phase1@example.invalid', 'role test')")
        assert conn.execute("SELECT count(*) FROM public.contact_submissions "
                            "WHERE email = 'phase1@example.invalid'").fetchone()[0] == 1
        conn.rollback()


def test_readonly_cannot_write():
    assert _fails("hyperchat_readonly", "INSERT INTO public.contact_submissions (name, email, message) "
                                        "VALUES ('x', 'x@example.invalid', 'x')")


def test_migrator_does_not_inherit_owner_privileges():
    # Without SET ROLE the migrator cannot write application tables.
    assert _fails("hyperchat_migrator", "INSERT INTO public.contact_submissions (name, email, message) "
                                        "VALUES ('x', 'x@example.invalid', 'x')")


def test_roles_have_no_superpowers():
    with _conn("hyperchat_readonly") as conn:
        rows = conn.execute("""
            SELECT rolname FROM pg_roles WHERE rolname LIKE 'hyperchat\\_%'
              AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
        """).fetchall()
    assert rows == []
