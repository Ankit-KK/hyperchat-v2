"""deploy/backup/hyperchat-pgbackup.sh against REAL databases at the schemas it must back up: production's 0009 and DEV's
0019 (before the KYC and step-up enforcement tables) and the head (with them). Each schema is a disposable
hc_backuptest_* database migrated with Alembic on the test server; the set is then restored by restore_check into a
disposable database on the same server. Needs the test server and PostgreSQL client tools of its major version
(HYPERCHAT_TEST_PG_BIN or /usr/lib/postgresql/<major>/bin); skipped otherwise. Nothing touches a live database."""

import getpass
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from app.backup import restore_check
from app.backup.sets import load
from tests.backup.helpers import REPO, env_for, manifest, needs_bash, run_script, sets_in
from tests.integration import PG_HOST, requires_db, target

pytestmark = [requires_db, needs_bash]
HEAD = restore_check.expected_head()
GRANTEES = ("hyperchat_migrator, hyperchat_app, hyperchat_worker, hyperchat_readonly, hyperchat_lifecycle, "
            "hyperchat_auth, hyperchat_stepup")


def _admin() -> psycopg.Connection:
    _, port = target()
    return psycopg.connect(host=PG_HOST, port=port, dbname="postgres", user=os.environ.get("PGUSER", "postgres"),
                           autocommit=True)


def _client_bin(major: int) -> Path | None:
    for candidate in (os.environ.get("HYPERCHAT_TEST_PG_BIN"), f"/usr/lib/postgresql/{major}/bin"):
        if candidate and all(Path(candidate, t).is_file() for t in ("pg_dump", "pg_restore", "psql")):
            out = subprocess.run([str(Path(candidate, "pg_dump")), "--version"], capture_output=True, text=True).stdout
            if restore_check._major(out) == major:
                return Path(candidate)
    return None


def _migrate(db: str, revision: str) -> None:
    _, port = target()
    where = f"@/{db}?host={PG_HOST}&port={port}" if PG_HOST.startswith("/") else f"@{PG_HOST}:{port}/{db}"
    env = {**os.environ, "HYPERCHAT_MIGRATION_DSN": f"postgresql+psycopg://hyperchat_migrator{where}",
           "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", revision], cwd=REPO / "database", env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]


@pytest.fixture
def schema_db(tmp_path):
    """make(revision): a disposable database migrated to `revision`, and the script's environment for it."""
    with _admin() as admin:
        major = int(admin.execute("SHOW server_version_num").fetchone()[0]) // 10000
    pg_bin = _client_bin(major)
    if pg_bin is None or not shutil.which("flock"):
        pytest.skip(f"needs PostgreSQL {major} client tools matching the test server, and flock")
    created: list[str] = []

    def make(revision: str) -> tuple[str, dict, Path]:
        db = f"hc_backuptest_{secrets.token_hex(4)}"
        with _admin() as admin:
            admin.execute(f'CREATE DATABASE "{db}" OWNER hyperchat_owner')
            created.append(db)
            admin.execute(f'REVOKE ALL ON DATABASE "{db}" FROM PUBLIC')
            admin.execute(f'GRANT CONNECT ON DATABASE "{db}" TO {GRANTEES}')
        _migrate(db, revision)
        _, port = target()
        (tmp_path / "test.witness").write_text("")  # a fresh witness: nothing enforced yet
        env = env_for(tmp_path, pg_bin, HYPERCHAT_BACKUP_DB=db, HYPERCHAT_BACKUP_PORT=port,
                      HYPERCHAT_BACKUP_CLUSTER=f"{major}/test", HYPERCHAT_BACKUP_HOST=PG_HOST,
                      HYPERCHAT_BACKUP_OWNER=getpass.getuser(), PGUSER=os.environ.get("PGUSER", "postgres"))
        return db, env, pg_bin

    yield make
    with _admin() as admin:
        for db in created:
            admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        leftovers = admin.execute("SELECT count(*) FROM pg_database WHERE datname LIKE 'hc_restorecheck_%'").fetchone()
    assert leftovers == (0,)  # every disposable restore target was dropped


def _restore(tmp: Path, base: str, pg_bin: Path, revision: str) -> tuple[int, dict]:
    import contextlib
    import io
    import json
    _, port = target()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = restore_check.main(["--dir", str(tmp / "backups"), "--set", base, "--env", "test", "--pg-bin",
                                   str(pg_bin), "--server", f"host={PG_HOST} port={port} "
                                   f"user={os.environ.get('PGUSER', 'postgres')}", "--disposable-server",
                                   "--expect-head", revision])
    return code, json.loads(out.getvalue())


@pytest.mark.parametrize("revision", ["0009", "0019"])
def test_a_schema_before_the_enforcement_tables_backs_up_and_restores(schema_db, tmp_path, revision):
    db, env, pg_bin = schema_db(revision)
    with psycopg.connect(host=PG_HOST, port=env["HYPERCHAT_BACKUP_PORT"], dbname=db, user=env["PGUSER"]) as conn:
        assert conn.execute("SELECT to_regclass('public.kyc_enforcement'), to_regclass('public.stepup_enforcement')"
                            ).fetchone() == (None, None)  # really the old schema
    r = run_script(env, timeout=300)
    assert r.returncode == 0, r.stderr
    (base,) = sets_in(tmp_path / "backups")
    m = manifest(tmp_path / "backups", base)
    assert m["alembic_version"] == revision
    assert m["kyc_enforcement"] == {"state": "", "generation": 0} and m["stepup_enforcement"] == ""
    assert m["witness_status"] == "copied" and load(tmp_path / "backups", base, expect_env="test")
    code, out = _restore(tmp_path, base, pg_bin, revision)
    assert code == 0 and out["structurally_valid"] and out["backup_valid"] and out["restore_valid"], out
    assert out["invariants"]["alembic_version"] == revision and out["invariants"]["alembic_matches_manifest"]
    assert "kyc_enforcement" not in out["invariants"] and "stepup_enforcement" not in out["invariants"]
    assert out["kyc_witness"] == "not_applicable" and out["witness"]["reason"] == "no_kyc_enforcement_table"
    assert out["live_operation_approved"] is False


def test_the_head_schema_records_and_restores_its_enforcement_rows(schema_db, tmp_path):
    db, env, pg_bin = schema_db(HEAD)
    r = run_script(env, timeout=300)
    assert r.returncode == 0, r.stderr
    (base,) = sets_in(tmp_path / "backups")
    m = manifest(tmp_path / "backups", base)
    assert m["alembic_version"] == HEAD
    assert m["kyc_enforcement"] == {"state": "draft", "generation": 0} and m["stepup_enforcement"] == "draft"
    code, out = _restore(tmp_path, base, pg_bin, HEAD)
    assert code == 0 and out["structurally_valid"] and out["restore_valid"], out
    assert out["invariants"]["kyc_enforcement"] == {"state": "draft", "generation": 0}
    assert out["invariants"]["stepup_enforcement"] == "draft" and out["kyc_witness"] == "consistent"
    assert out["live_operation_approved"] is False


@pytest.mark.parametrize("rows", [0, 2])
def test_an_enforcement_table_without_exactly_one_row_fails_and_leaves_nothing(schema_db, tmp_path, rows):
    db, env, _ = schema_db("0019")
    with psycopg.connect(host=PG_HOST, port=env["HYPERCHAT_BACKUP_PORT"], dbname=db, user=env["PGUSER"],
                         autocommit=True) as conn:  # an inconsistent database: the table exists, its row does not
        conn.execute("CREATE TABLE public.kyc_enforcement (state text NOT NULL, generation bigint NOT NULL)")
        for _ in range(rows):
            conn.execute("INSERT INTO public.kyc_enforcement VALUES ('draft', 0)")
    r = run_script(env, timeout=300)
    assert r.returncode == 1 and "public.kyc_enforcement must hold exactly one row" in r.stderr, r.stderr
    d = tmp_path / "backups"
    assert sets_in(d) == [] and not list(d.glob("*.dump")) and not list(d.glob(".partial-*"))
