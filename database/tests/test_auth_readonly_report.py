"""D1 phase 1: the read-only Supabase Auth report. Synthetic fixtures only.

Unit tests always run. The PostgreSQL tests build a throwaway fixture database (a fake auth.users and a minimal
public.users) and only run where HYPERCHAT_CI_DB_SETUP=1, i.e. CI's service container or the throwaway cluster.
"""

import hashlib
import os
import re
import uuid
from pathlib import Path

import psycopg
import pytest

from etl import auth_readonly_report as rep
from etl.auth_readonly_report import Account, Refused, summarize

IDS = {k: str(uuid.UUID(int=i)) for i, k in enumerate(("s1", "s2", "s3", "s4", "s5", "s8", "s9", "ghost"), 1)}
D = "@authrec.invalid"
# (id, email, encrypted_password, confirmed, raw_user_meta_data email)
SOURCE = [
    ("s1", "one" + D, "$2a$10$synthetic-not-a-real-hash", True, None),
    ("s2", "Case2" + D, None, True, None),
    ("s3", "a3" + D, "", False, None),
    ("s4", None, "$2a$10$synthetic", False, "four" + D),  # metadata email must NOT be used
    ("s5", "five" + D, "$2a$10$synthetic", True, None),
    ("s8", "twin" + D, "$2a$10$synthetic", True, None),
    ("s9", "TWIN" + D + " ", "", False, None),
]
# (legacy id key or None, email, has password hash, verified)
TARGET = [
    ("s1", "one" + D, False, False),
    ("s2", "case2" + D, True, True),
    ("s3", "other3" + D, False, False),
    ("s4", "four" + D, False, False),
    ("ghost", "six" + D, True, False),
    (None, "FIVE" + D, False, False),
    ("s8", "twin" + D, False, False),
    ("s8", "eleven" + D, False, False),  # duplicate mapping (impossible in the real schema: UNIQUE)
]
EXPECTED = {
    "source_accounts": 7, "target_accounts": 8, "target_with_legacy_id": 7, "target_without_legacy_id": 1,
    "id_and_email_exact": 2, "id_match_email_case_or_space_only": 1, "id_match_email_mismatch": 2,
    "id_match_source_email_missing": 1, "target_legacy_id_not_in_source": 1,
    "target_email_used_by_other_source_id": 2, "matched_source_has_password": 4, "matched_source_confirmed": 4,
    "exact_match_source_password_target_none": 2, "source_ids_not_in_target": 2, "duplicate_source_ids": 0,
    "duplicate_target_legacy_ids": 1, "duplicate_source_emails_normalized": 1,
    "duplicate_target_emails_normalized": 0, "source_with_password": 4, "source_confirmed": 4,
    "target_with_password_hash": 2, "target_email_verified": 1,
}


def _digest(value: str | None) -> str | None:
    return None if value is None else hashlib.sha256(value.encode()).hexdigest()


def _norm(value: str | None) -> str | None:
    return None if value is None else _digest(value.strip(" ").lower())


def _source_accounts() -> list[Account]:
    return [Account(IDS[k], _digest(e), _norm(e), bool(pw), conf) for k, e, pw, conf, _ in SOURCE]


def _target_accounts() -> list[Account]:
    return [Account(IDS[k] if k else None, _digest(e), _norm(e), pw, ver) for k, e, pw, ver in TARGET]


# ── pure logic ──────────────────────────────────────────────────────────────


def test_summary_categories():
    assert summarize(_source_accounts(), _target_accounts()) == EXPECTED


def test_empty_inputs_give_zero_counts():
    assert set(summarize([], []).values()) == {0}


def test_report_prints_counts_only():
    out = rep.render(summarize(_source_accounts(), _target_accounts()))
    assert "READ-ONLY" in out
    for _, email, *_ in SOURCE:
        if email:
            assert email not in out and _digest(email) not in out and _norm(email) not in out
    assert not any(i in out for i in IDS.values()) and "$2a$" not in out
    with pytest.raises(ValueError):
        rep.render({"leak": "one" + D})


def test_the_tool_contains_no_write_statements_and_never_selects_a_hash():
    source = Path(rep.__file__).read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)  # docstrings describe what it does NOT do
    for word in ("INSERT", "UPDATE ", "DELETE ", "TRUNCATE ", "ALTER ", "CREATE ", "DROP ", "COPY ", "GRANT ", "MERGE "):
        assert word not in code.replace("'INSERT, UPDATE, DELETE, TRUNCATE'", ""), word
    assert "coalesce(encrypted_password, '') <> ''" in rep.SOURCE_SQL
    assert rep.SOURCE_SQL.count("encrypted_password") == 1  # presence only, never the value
    assert "raw_user_meta_data" not in rep.SOURCE_SQL
    assert ".commit(" not in code


def _answers(monkeypatch, inputs: list[str], passwords: list[str]):
    it, pw = iter(inputs), iter(passwords)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
    monkeypatch.setattr(rep.getpass, "getpass", lambda prompt="": next(pw))


def test_source_requires_verify_full_tls_with_an_existing_ca(monkeypatch, tmp_path):
    _answers(monkeypatch, ["db.example.invalid", str(tmp_path / "missing.pem")], [])
    with pytest.raises(Refused, match="CA certificate"):
        rep.source_params()
    ca = tmp_path / "ca.pem"
    ca.write_text("synthetic")
    _answers(monkeypatch, ["db.example.invalid", str(ca), "", "", "report_reader"], ["synthetic-secret"])
    params = rep.source_params()
    assert params["sslmode"] == "verify-full" and params["sslrootcert"] == str(ca)
    assert params["port"] == 5432 and params["dbname"] == "postgres"


def test_target_must_be_local(monkeypatch):
    _answers(monkeypatch, ["db.example.invalid"], [])
    with pytest.raises(Refused, match="local"):
        rep.target_params()
    _answers(monkeypatch, ["", "", "", ""], [""])
    assert rep.target_params() == {"host": "/var/run/postgresql", "port": 5433, "dbname": "hyperchat_dev",
                                   "user": "hyperchat_readonly"}


def test_failures_print_no_connection_details(monkeypatch, tmp_path, capsys):
    ca = tmp_path / "ca.pem"
    ca.write_text("synthetic")
    _answers(monkeypatch, ["secret-host.invalid", str(ca), "", "", "secret-role"], ["synthetic-secret-pw"])

    def boom(**params):
        raise psycopg.OperationalError(f"connection to {params['host']} as {params['user']} failed")

    monkeypatch.setattr(rep.psycopg, "connect", boom)
    assert rep.main([]) == 1
    err = capsys.readouterr()
    assert "OperationalError" in err.err
    for secret in ("secret-host", "secret-role", "synthetic-secret-pw"):
        assert secret not in err.err and secret not in err.out


def test_there_is_no_import_option():
    with pytest.raises(SystemExit):
        rep.main(["--import"])


def test_a_non_numeric_port_is_refused_by_name(monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("synthetic")
    _answers(monkeypatch, ["db.example.invalid", str(ca), "54x2"], [])
    with pytest.raises(Refused, match="Supabase port must be a number"):
        rep.source_params()
    _answers(monkeypatch, ["", "\u0665\u0664\u0663\u0663"], [])  # non-ASCII digits are not a port either
    with pytest.raises(Refused, match="Target port must be a number"):
        rep.target_params()


class _Conn:
    pgconn = type("PG", (), {"ssl_in_use": True})()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_summary_that_is_not_counts_only_is_never_printed(monkeypatch, capsys):
    """Security audit F13: the guard is a real check, and main() turns it into a clear failure (exit 1), not a
    traceback and never the value itself."""
    secret = "synthetic-secret-" + D
    monkeypatch.setattr(rep, "source_params", lambda: {})
    monkeypatch.setattr(rep, "target_params", lambda **kw: {})
    monkeypatch.setattr(rep, "open_readonly", lambda **kw: _Conn())
    monkeypatch.setattr(rep, "report", lambda *a, **kw: {"leak": secret})
    assert rep.main([]) == 1
    out = capsys.readouterr()
    assert "failed: the report holds counts only" in out.err and secret not in out.out + out.err


# ── against PostgreSQL (throwaway fixture database) ─────────────────────────

FIXTURE_DB = "hyperchat_authrec_fixture"
READER, PARTIAL = "authrec_reader", "authrec_partial"
db_only = pytest.mark.skipif(os.environ.get("HYPERCHAT_CI_DB_SETUP") != "1",
                             reason="needs the throwaway CI PostgreSQL (HYPERCHAT_CI_DB_SETUP=1)")


def _admin(dbname: str = "postgres") -> psycopg.Connection:
    return psycopg.connect(dbname=dbname, user=os.environ.get("PGUSER", "postgres"), autocommit=True)


@pytest.fixture(scope="module")
def fixture_db():
    with _admin() as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {FIXTURE_DB}")
        for role in (READER, PARTIAL):
            conn.execute(f"DROP ROLE IF EXISTS {role}")
            conn.execute(f"CREATE ROLE {role} LOGIN")
        conn.execute(f"CREATE DATABASE {FIXTURE_DB}")
    try:
        with _admin(FIXTURE_DB) as conn:
            conn.execute("CREATE EXTENSION citext")
            conn.execute("CREATE SCHEMA auth")
            conn.execute("CREATE TABLE auth.users (id uuid PRIMARY KEY, email varchar(255), "
                         "encrypted_password varchar(255), confirmed_at timestamptz, raw_user_meta_data jsonb)")
            conn.execute("CREATE TABLE public.users (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), email citext "
                         "NOT NULL, password_hash text, legacy_supabase_user_id uuid, email_verified_at timestamptz)")
            for k, email, pw, conf, meta in SOURCE:
                conn.execute("INSERT INTO auth.users VALUES (%s, %s, %s, CASE WHEN %s THEN now() END, "
                             "jsonb_build_object('email', %s::text))", (IDS[k], email, pw, conf, meta))
            for k, email, pw, ver in TARGET:
                conn.execute("INSERT INTO public.users (email, password_hash, legacy_supabase_user_id, "
                             "email_verified_at) VALUES (%s, CASE WHEN %s THEN 'synthetic' END, %s, "
                             "CASE WHEN %s THEN now() END)", (email, pw, IDS[k] if k else None, ver))
            conn.execute(f"GRANT USAGE ON SCHEMA auth TO {READER}, {PARTIAL}")
            conn.execute(f"GRANT SELECT ON auth.users, public.users TO {READER}")
            conn.execute(f"GRANT SELECT (id, email) ON auth.users TO {PARTIAL}")
        yield
    finally:
        with _admin() as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {FIXTURE_DB} WITH (FORCE)")
            for role in (READER, PARTIAL):
                conn.execute(f"DROP ROLE IF EXISTS {role}")


def _ro(role: str) -> psycopg.Connection:
    return rep.open_readonly(dbname=FIXTURE_DB, user=role)


def _snapshot() -> list:
    with _admin(FIXTURE_DB) as conn:
        return [conn.execute("SELECT * FROM auth.users ORDER BY id").fetchall(),
                conn.execute("SELECT * FROM public.users ORDER BY id").fetchall()]


@db_only
def test_report_against_postgres_matches_the_synthetic_expectation(fixture_db):
    before = _snapshot()
    with _ro(READER) as src, _ro(READER) as tgt:
        assert rep.report(src, tgt) == EXPECTED
    assert _snapshot() == before  # nothing changed


@db_only
def test_sessions_are_read_only(fixture_db):
    with _ro(READER) as conn:
        with pytest.raises((psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)):
            conn.execute("UPDATE public.users SET email_verified_at = now()")
        conn.rollback()
    with _ro(os.environ.get("PGUSER", "postgres")) as conn:  # even a superuser cannot write in this session
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("UPDATE public.users SET email_verified_at = now()")
        conn.rollback()


@db_only
def test_writable_role_is_refused_unless_accepted(fixture_db):
    superuser = os.environ.get("PGUSER", "postgres")
    with _ro(superuser) as src, _ro(READER) as tgt:
        with pytest.raises(Refused, match="can write"):
            rep.report(src, tgt)
    before = _snapshot()
    with _ro(superuser) as src, _ro(READER) as tgt:
        assert rep.report(src, tgt, accept_writable_source_role=True) == EXPECTED
    assert _snapshot() == before


@db_only
def test_missing_privileges_or_schema_fail_closed(fixture_db):
    with _ro(PARTIAL) as src, _ro(READER) as tgt:
        with pytest.raises(Refused, match="no SELECT privilege"):
            rep.report(src, tgt)
    with _ro(READER) as conn:
        with pytest.raises(Refused, match="missing or has an unexpected type"):
            rep.check_session(conn, rep.SOURCE_TABLE, {**rep.SOURCE_COLUMNS, "email_confirmed_by": {"uuid"}},
                              "source", False)
        conn.rollback()
        with pytest.raises(Refused, match="unexpected type"):
            rep.check_session(conn, rep.TARGET_TABLE, {**rep.TARGET_COLUMNS, "email": {"uuid"}}, "target", False)
        conn.rollback()


@db_only
def test_tls_verification_failure_refuses_to_connect(fixture_db, tmp_path):
    """verify-full with a CA that did not sign the server certificate (or a server without TLS) never connects."""
    ca = tmp_path / "not-the-server-ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\nc3ludGhldGlj\n-----END CERTIFICATE-----\n")
    with pytest.raises(psycopg.OperationalError):
        rep.open_readonly(dbname=FIXTURE_DB, user=READER, sslmode="verify-full", sslrootcert=str(ca))
