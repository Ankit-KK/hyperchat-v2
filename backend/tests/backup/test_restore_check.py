"""app.backup.restore_check: refusals and verification failures without a database (stub tools), and full restores of a
real backup set into disposable targets (a scratch cluster and a declared-disposable test server) when PostgreSQL
binaries of the test server's major version are available. Nothing touches a live database or socket."""

import getpass
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import psycopg
import pytest

from app.backup import restore_check
from app.backup.sets import BackupSet, load
from tests.backup.helpers import REPO, env_for, needs_bash, run_script, sets_in, stub_bin
from tests.integration import PG_HOST, requires_db, target


def check(*argv: str) -> tuple[int, dict]:
    import contextlib
    import io
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = restore_check.main(list(argv))
    return code, json.loads(out.getvalue())


@pytest.fixture
def stub_set(tmp_path):
    if not shutil.which("bash") or not shutil.which("flock"):
        pytest.skip("needs bash and flock")
    bin_dir = stub_bin(tmp_path)
    (tmp_path / "test.witness").write_text("")
    assert run_script(env_for(tmp_path, bin_dir)).returncode == 0
    (base,) = sets_in(tmp_path / "backups")
    return tmp_path, bin_dir, base


# ── refusals and backup validity (no database) ──────────────────────────────────

@pytest.mark.parametrize("dsn,reason", [
    ("host=/var/run/postgresql port=5432 user=postgres", "refused_live_socket_directory"),
    ("host=/run/postgresql/ port=5433", "refused_live_socket_directory"),
    ("port=5432", "refused_live_socket_directory"),  # no host = the default live socket
    ("host=127.0.0.1 port=55432 dbname=hyperchat", "refused_live_database"),
    ("host=127.0.0.1 port=55432 dbname=hyperchat_dev", "refused_live_database"),
])
def test_live_targets_are_refused(dsn, reason):
    with pytest.raises(restore_check.CheckError, match=reason):
        restore_check.server_target(dsn)


def test_server_mode_must_be_declared_disposable(stub_set):
    tmp, bin_dir, base = stub_set
    code, out = check("--dir", str(tmp / "backups"), "--set", base, "--pg-bin", str(bin_dir), "--server",
                      "host=127.0.0.1 port=1")
    assert code == 1 and out["backup_valid"] and out["error"] == "server_mode_needs_--disposable-server"


def test_scratch_mode_without_the_roles_file_stops_before_creating_a_cluster(stub_set, monkeypatch):
    """A server release holds only backend/ and database/, so the default --roles-sql does not exist there: the check
    names it instead of failing inside a half-built scratch cluster."""
    tmp, bin_dir, base = stub_set
    monkeypatch.setattr(restore_check, "scratch_cluster", lambda *a: pytest.fail("a scratch cluster was created"))
    code, out = check("--dir", str(tmp / "backups"), "--set", base, "--pg-bin", str(bin_dir), "--scratch",
                      "--roles-sql", str(tmp / "no-such-roles.sql"))
    assert code == 1 and out["backup_valid"] and out["restore_valid"] is False
    assert out["error"] == "roles_sql_not_found" and out["structurally_valid"] is False


def test_scratch_mode_as_root_needs_an_unprivileged_user(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    with pytest.raises(restore_check.CheckError, match="scratch-user"):
        with restore_check.scratch_cluster(tmp_path, None):
            pass
    with pytest.raises(restore_check.CheckError, match="scratch-user"):
        with restore_check.scratch_cluster(tmp_path, "root"):
            pass


@pytest.mark.parametrize("damage,reason", [
    ("truncate", "dump_checksum_mismatch"),
    ("corrupt_but_rehashed", "archive_unreadable"),
    ("wrong_toc", "archive_toc_differs_from_manifest"),
    ("wrong_env", "wrong_environment"),
    ("traversal", "bad_set_name"),
])
def test_an_untrustworthy_set_is_never_restored(stub_set, damage, reason):
    tmp, bin_dir, base = stub_set
    d = tmp / "backups"
    argv = ["--dir", str(d), "--set", base, "--pg-bin", str(bin_dir), "--server", "host=127.0.0.1 port=1",
            "--disposable-server", "--env", "test"]
    manifest_path = d / f"{base}.manifest.json"
    m = json.loads(manifest_path.read_text())
    if damage == "truncate":
        (d / f"{base}.dump").write_bytes((d / f"{base}.dump").read_bytes()[:-4])
    elif damage == "corrupt_but_rehashed":  # someone replaced the archive AND its hashes: the archive itself is checked
        body = b"PGDMP CORRUPT"
        (d / f"{base}.dump").write_bytes(body)
        import hashlib
        sha = hashlib.sha256(body).hexdigest()
        m["dump"]["sha256"] = sha
        (d / f"{base}.dump.sha256").write_text(f"{sha}  {base}.dump\n")
        manifest_path.write_text(json.dumps(m))
    elif damage == "wrong_toc":
        m["dump"]["toc_entries"] = 99
        manifest_path.write_text(json.dumps(m))
    elif damage == "wrong_env":
        argv[argv.index("test")] = "prod"
    elif damage == "traversal":
        argv[argv.index(base)] = "../../etc/hc_backuptest_a_20260101T000000Z"
    code, out = check(*argv)
    assert code == 1 and out["backup_valid"] is False and out["restore_valid"] is False
    assert out["error"] == reason


def test_restore_tools_must_be_the_source_major(stub_set, monkeypatch):
    tmp, bin_dir, base = stub_set
    monkeypatch.setenv("STUB_MAJOR", "16")  # the set came from a 17 server
    code, out = check("--dir", str(tmp / "backups"), "--set", base, "--pg-bin", str(bin_dir), "--server",
                      "host=127.0.0.1 port=1", "--disposable-server")
    assert code == 1 and out["error"] == "pg_restore_major_differs_from_the_source"


def test_the_expected_head_is_this_checkouts_newest_migration():
    newest = sorted(p.name[:4] for p in (REPO / "database/migrations/versions").glob("[0-9]*_*.py"))[-1]
    assert restore_check.expected_head() == newest


@requires_db
def test_the_invariants_notice_a_disabled_guard_or_a_leaked_privilege(env):
    """Run against the migrated test database inside a transaction that is rolled back: nothing persists."""
    from tests.integration import fixture_db
    with fixture_db() as conn:
        version = conn.execute("SELECT version_num FROM public.alembic_version").fetchone()[0]
        kyc_generation = conn.execute("SELECT generation FROM public.kyc_enforcement").fetchone()[0]
        bset = BackupSet(Path("/nonexistent"), "hc_backuptest_x_20260101T000000Z",
                         {"alembic_version": version, "kyc_enforcement": {"generation": kyc_generation}})
        conn.execute("BEGIN")
        try:
            assert restore_check.invariants(conn, bset, version)["ok"] is True
            conn.execute("ALTER TABLE public.streamers DISABLE TRIGGER aac_stepup_guard_streamers")
            broken = restore_check.invariants(conn, bset, version)
            assert broken["ok"] is False and broken["guard_triggers_disabled"] == 1
            conn.execute("ALTER TABLE public.streamers ENABLE TRIGGER aac_stepup_guard_streamers")
            conn.execute("GRANT EXECUTE ON FUNCTION public.stepup_enforcement_draft(text, text) TO hyperchat_app")
            leaked = restore_check.invariants(conn, bset, version)
            assert leaked["ok"] is False
            assert "hyperchat_app EXECUTE public.stepup_enforcement_draft(text, text)" in leaked["runtime_role_privilege_leaks"]
            conn.execute("REVOKE EXECUTE ON FUNCTION public.stepup_enforcement_draft(text, text) FROM hyperchat_app")
            conn.execute("GRANT UPDATE ON public.kyc_enforcement TO hyperchat_worker")
            assert "hyperchat_worker UPDATE kyc_enforcement" in \
                restore_check.invariants(conn, bset, version)["runtime_role_privilege_leaks"]
            assert restore_check.invariants(conn, bset, "9999")["ok"] is False  # another head
        finally:
            conn.execute("ROLLBACK")


# ── real restores (disposable targets only) ─────────────────────────────────────

def _server_major() -> int:
    name, port = target()
    with psycopg.connect(host=PG_HOST, port=port, dbname=name, user=os.environ.get("PGUSER", "postgres")) as c:
        return int(c.execute("SHOW server_version_num").fetchone()[0]) // 10000


def _pg_bin(major: int) -> Path | None:
    for candidate in (os.environ.get("HYPERCHAT_TEST_PG_BIN"), f"/usr/lib/postgresql/{major}/bin"):
        if candidate and Path(candidate, "pg_dump").is_file() and Path(candidate, "initdb").is_file():
            out = subprocess.run([str(Path(candidate, "pg_dump")), "--version"], capture_output=True, text=True).stdout
            if restore_check._major(out) == major:
                return Path(candidate)
    return None


@pytest.fixture
def real_set(env, tmp_path):
    """A backup set of a disposable copy of the migrated test database, made by the real backup script."""
    if not shutil.which("bash") or not shutil.which("flock"):
        pytest.skip("needs bash and flock")
    major = _server_major()
    pg_bin = _pg_bin(major)
    if pg_bin is None:
        pytest.skip(f"no PostgreSQL {major} binaries matching the test server")
    name, port = target()
    user = os.environ.get("PGUSER", "postgres")
    db = f"hc_backuptest_{secrets.token_hex(4)}"
    conn = dict(host=PG_HOST, port=port, user=user)
    archive = tmp_path / "source.dump"
    subprocess.run([str(pg_bin / "pg_dump"), "-h", PG_HOST, "-p", str(port), "-U", user, "-Fc", "-f", str(archive),
                    name], check=True, capture_output=True)
    with psycopg.connect(dbname="postgres", autocommit=True, **conn) as admin:
        admin.execute(f'CREATE DATABASE "{db}" OWNER hyperchat_owner')
    try:
        subprocess.run([str(pg_bin / "pg_restore"), "--exit-on-error", "-h", PG_HOST, "-p", str(port), "-U", user,
                        "-d", db, str(archive)], check=True, capture_output=True)
        (tmp_path / "test.witness").write_text("")  # a fresh witness: nothing enforced yet
        env_vars = env_for(tmp_path, pg_bin, HYPERCHAT_BACKUP_DB=db, HYPERCHAT_BACKUP_PORT=port,
                           HYPERCHAT_BACKUP_CLUSTER=f"{major}/test", HYPERCHAT_BACKUP_HOST=PG_HOST,
                           HYPERCHAT_BACKUP_OWNER=getpass.getuser(), PGUSER=user)
        r = run_script(env_vars, timeout=300)
        assert r.returncode == 0, r.stderr
        (base,) = sets_in(tmp_path / "backups")
        yield tmp_path, pg_bin, base, f"host={PG_HOST} port={port} user={user}"
    finally:
        with psycopg.connect(dbname="postgres", autocommit=True, **conn) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
            leftovers = admin.execute("SELECT count(*) FROM pg_database WHERE datname LIKE 'hc_restorecheck_%'").fetchone()
        assert leftovers == (0,)  # every disposable restore target was dropped


@requires_db
@needs_bash
def test_a_real_set_restores_at_the_head_with_its_security_invariants(real_set, tmp_path):
    tmp, pg_bin, base, dsn = real_set
    out_file = tmp / "result.json"
    code, out = check("--dir", str(tmp / "backups"), "--set", base, "--env", "test", "--pg-bin", str(pg_bin),
                      "--server", dsn, "--disposable-server", "--output", str(out_file))
    assert code == 0 and out["structurally_valid"] and out["backup_valid"] and out["restore_valid"], out
    assert out["kyc_witness"] == "consistent" and "ok" not in out
    assert out["live_operation_approved"] is False  # never approved by this tool
    assert out["live_operation_blockers"] == ["external_reconciliation_required", "owner_approval_required"]
    inv = out["invariants"]
    assert inv["alembic_version"] == restore_check.expected_head() and inv["alembic_matches_manifest"]
    assert inv["objects_not_owned_by_hyperchat_owner"] == 0 and inv["public_table_privileges"] == 0
    assert inv["guard_triggers_enabled"] > 0 and inv["guard_triggers_disabled"] == 0
    assert inv["runtime_role_privilege_leaks"] == [] and inv["stepup_enforcement"] == "draft"
    assert out["witness"] == {"checked": True, "verdict": "consistent", "problem": None, "database_generation": 0,
                              "witness_generation": 0, "meaning": "consistent: new donations allowed"}
    assert json.loads(out_file.read_text())["structurally_valid"] is True


@requires_db
@needs_bash
def test_a_newer_witness_marks_the_restored_database_as_blocked_without_hiding_validity(real_set):
    tmp, pg_bin, base, dsn = real_set
    newer = tmp / "newest.witness"
    newer.write_text('{"at": "t", "chain": "c4", "generation": 4, "state": "active"}\n')
    before = newer.read_bytes()
    code, out = check("--dir", str(tmp / "backups"), "--set", base, "--pg-bin", str(pg_bin), "--server", dsn,
                      "--disposable-server", "--witness", str(newer))
    assert code == 3 and out["structurally_valid"] and out["restore_valid"]  # the backup itself is fine ...
    assert out["kyc_witness"] == "inconsistent" and out["witness"]["problem"] == "restore_suspected"  # ... blocked
    assert out["live_operation_approved"] is False and "kyc_witness_inconsistent" in out["live_operation_blockers"]
    assert "never automatic" in out["witness"]["meaning"]
    assert newer.read_bytes() == before  # never acknowledged, re-anchored or replaced


@requires_db
@needs_bash
def test_without_a_witness_only_an_untouched_kyc_history_passes(real_set):
    tmp, pg_bin, base, dsn = real_set
    code, out = check("--dir", str(tmp / "backups"), "--set", base, "--pg-bin", str(pg_bin), "--server", dsn,
                      "--disposable-server", "--witness", str(tmp / "absent.witness"))
    assert code == 0 and out["kyc_witness"] == "consistent", out  # draft, generation 0: nothing to protect yet
    assert out["live_operation_approved"] is False


class _RestoredDb:
    """A restored database with KYC history (active, generation 2), for witness_status without PostgreSQL."""

    def execute(self, sql, *_):
        row = (False,) if "to_regclass" in sql else ("active", 2, "c2")
        return type("Row", (), {"fetchone": lambda self: row})()


def test_the_verdict_names_a_missing_witness_for_a_database_with_kyc_history(tmp_path):
    assert restore_check.witness_status(_RestoredDb(), None)["verdict"] == "missing"
    assert restore_check.witness_status(_RestoredDb(), tmp_path / "gone.witness")["verdict"] == "missing"
    same = tmp_path / "same.witness"
    same.write_text('{"at": "t", "chain": "c2", "generation": 2, "state": "active"}\n')
    assert restore_check.witness_status(_RestoredDb(), same)["verdict"] == "consistent"


@requires_db
@needs_bash
def test_a_restore_at_another_head_or_on_another_major_is_not_valid(real_set):
    tmp, pg_bin, base, dsn = real_set
    code, out = check("--dir", str(tmp / "backups"), "--set", base, "--pg-bin", str(pg_bin), "--server", dsn,
                      "--disposable-server", "--expect-head", "9999")
    assert code == 1 and out["backup_valid"] and not out["restore_valid"]
    assert out["invariants"]["alembic_matches_expected_head"] is False
    m = tmp / "backups" / f"{base}.manifest.json"
    data = json.loads(m.read_text())
    data["server_version_num"] = 150019  # claims a 15 source
    m.write_text(json.dumps(data))
    code, out = check("--dir", str(tmp / "backups"), "--set", base, "--pg-bin", str(pg_bin), "--server", dsn,
                      "--disposable-server")
    assert code == 1 and out["error"] == "pg_restore_major_differs_from_the_source"


@requires_db
@needs_bash
def test_a_scratch_cluster_restores_and_is_removed(real_set):
    tmp, pg_bin, base, _ = real_set
    if not shutil.which("runuser") and os.geteuid() == 0:
        pytest.skip("needs runuser as root")
    before = {p.name for p in Path(os.environ.get("TMPDIR", "/tmp")).glob("hc-restorecheck-*")}
    argv = ["--dir", str(tmp / "backups"), "--set", base, "--pg-bin", str(pg_bin), "--scratch"]
    if os.geteuid() == 0:
        argv += ["--scratch-user", "nobody"]
    code, out = check(*argv)
    assert code == 0 and out["structurally_valid"] and out["target"] == {"mode": "scratch"}, out
    assert {p.name for p in Path(os.environ.get("TMPDIR", "/tmp")).glob("hc-restorecheck-*")} == before
    assert load(tmp / "backups", base)  # the set itself is untouched
