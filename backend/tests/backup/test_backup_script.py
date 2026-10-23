"""deploy/backup/hyperchat-pgbackup.sh, run for real with stub PostgreSQL tools in a temporary directory: profiles and
refusals, atomic sets, integrity, overlap, retention and the KYC witness copy. No database, no live path."""

import fcntl
import hashlib
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from app.backup.sets import SetError, load
from tests.backup.helpers import SCRIPT, env_for, manifest, needs_bash, run_script, sets_in, stub_bin

pytestmark = needs_bash


@pytest.fixture
def box(tmp_path):
    bin_dir = stub_bin(tmp_path)
    (tmp_path / "test.witness").write_text('{"chain": "c1", "generation": 1, "state": "active"}\n')
    return tmp_path, bin_dir


# ── profiles and refusals ───────────────────────────────────────────────────────

def test_the_production_profile_is_unchanged_and_dev_is_separate():
    text = SCRIPT.read_text()
    assert re.search(r"prod\) P_DB=hyperchat; +P_PORT=5432; P_CLUSTER=17/hyperchat; +P_DIR=/var/backups/hyperchat\n", text)
    assert re.search(r"dev\) +P_DB=hyperchat_dev; P_PORT=5433; P_CLUSTER=15/restoretest; P_DIR=/var/backups/hyperchat-dev\n",
                     text)
    assert 'ENV_NAME="${HYPERCHAT_BACKUP_ENV:-prod}"' in text  # the installed unit without settings = production
    assert "KEEP_DAYS=\"${HYPERCHAT_BACKUP_KEEP_DAYS:-14}\"" in text  # 14-day retention, as before


@pytest.mark.parametrize("over,reason", [
    ({"HYPERCHAT_BACKUP_ENV": "staging"}, "must be prod, dev or test"),
    ({"HYPERCHAT_BACKUP_ENV": "prod", "HYPERCHAT_BACKUP_DB": "hyperchat_dev"}, "prod backs up hyperchat"),
    ({"HYPERCHAT_BACKUP_ENV": "prod", "HYPERCHAT_BACKUP_DIR": "/var/backups/hyperchat-dev"}, "prod backs up hyperchat"),
    ({"HYPERCHAT_BACKUP_ENV": "dev", "HYPERCHAT_BACKUP_DB": "hyperchat"}, "dev backs up hyperchat_dev"),
    ({"HYPERCHAT_BACKUP_ENV": "dev", "HYPERCHAT_BACKUP_DIR": "/var/backups/hyperchat"}, "dev backs up hyperchat_dev"),
    ({"HYPERCHAT_BACKUP_ENV": "dev", "HYPERCHAT_BACKUP_CLUSTER": "17/hyperchat"}, "dev backs up hyperchat_dev"),
    ({"HYPERCHAT_BACKUP_ENV": "dev", "HYPERCHAT_BACKUP_WITNESS": "/var/lib/hyperchat-kyc/prod.witness"},
     "dev backs up hyperchat_dev"),
    ({"HYPERCHAT_BACKUP_DB": "hyperchat"}, "test backs up hc_backuptest_"),
    ({"HYPERCHAT_BACKUP_DB": "hc_backuptest_x;rm -rf /"}, "invalid database name"),
    ({"HYPERCHAT_BACKUP_DB": "hc_backuptest_X"}, "invalid database name"),
    ({"HYPERCHAT_BACKUP_DIR": "relative/dir"}, "absolute, normalised"),
    ({"HYPERCHAT_BACKUP_DIR": "/tmp/a/../b"}, "absolute, normalised"),
    ({"HYPERCHAT_BACKUP_DIR": "/"}, "absolute, normalised"),
    ({"HYPERCHAT_BACKUP_DIR": "/var/backups/hyperchat-test"}, "never writes under /var/backups"),
    ({"HYPERCHAT_BACKUP_WITNESS": "/var/lib/hyperchat-kyc/prod.witness"}, "never reads a real witness"),
    ({"HYPERCHAT_BACKUP_KEEP_DAYS": "0"}, "KEEP_DAYS"),
    ({"HYPERCHAT_BACKUP_KEEP_MIN": "0"}, "KEEP_MIN"),
    ({"HYPERCHAT_BACKUP_CLUSTER": "17"}, "invalid cluster"),
    ({"HYPERCHAT_BACKUP_PORT": "54x"}, "invalid port"),
])
def test_invalid_or_crossed_configurations_are_refused_before_anything_runs(box, over, reason):
    tmp, bin_dir = box
    r = run_script(env_for(tmp, bin_dir, **over))
    assert r.returncode == 2 and reason in r.stderr, r.stderr
    assert not (tmp / "backups").exists() or not any((tmp / "backups").iterdir())


def test_outside_test_the_owner_host_and_run_as_are_fixed(box):
    tmp, bin_dir = box
    r = run_script(env_for(tmp, bin_dir, HYPERCHAT_BACKUP_ENV="prod", HYPERCHAT_BACKUP_DB="hyperchat",
                           HYPERCHAT_BACKUP_PORT="5432", HYPERCHAT_BACKUP_CLUSTER="17/hyperchat",
                           HYPERCHAT_BACKUP_DIR="/var/backups/hyperchat",
                           HYPERCHAT_BACKUP_WITNESS="/var/lib/hyperchat-kyc/prod.witness"))
    assert r.returncode == 2 and "fixed outside test" in r.stderr


@pytest.mark.parametrize("over,reason", [
    ({"STUB_DUMP_MAJOR": "16"}, "pg_dump is version 16"),          # tools of another major than the cluster
    ({"STUB_MAJOR": "15"}, "pg_dump is version 15"),
    ({"STUB_SERVER_NUM": "150019"}, "server is version 150019"),  # the cluster answering is not the one named
    ({"STUB_CURRENT_DB": "hyperchat"}, "wrong database"),
])
def test_postgresql_versions_and_the_connected_database_must_match_the_cluster(box, over, reason):
    tmp, bin_dir = box
    r = run_script(env_for(tmp, bin_dir, **over))
    assert r.returncode == 2 and reason in r.stderr, r.stderr
    assert sets_in(tmp / "backups") == [] if (tmp / "backups").exists() else True


def test_the_directory_must_be_private_and_never_a_symlink(box):
    tmp, bin_dir = box
    (tmp / "real").mkdir(mode=0o700)
    (tmp / "link").symlink_to(tmp / "real")
    r = run_script(env_for(tmp, bin_dir, HYPERCHAT_BACKUP_DIR=str(tmp / "link")))
    assert r.returncode == 2 and "not a plain directory" in r.stderr
    open_dir = tmp / "open"
    open_dir.mkdir()
    open_dir.chmod(0o755)
    r = run_script(env_for(tmp, bin_dir, HYPERCHAT_BACKUP_DIR=str(open_dir)))
    assert r.returncode == 2 and "mode 700" in r.stderr


# ── a set: atomic, verified, private ────────────────────────────────────────────

def test_a_successful_run_writes_one_verified_private_set_with_the_witness(box):
    tmp, bin_dir = box
    r = run_script(env_for(tmp, bin_dir))
    assert r.returncode == 0, r.stderr
    d = tmp / "backups"
    (base,) = sets_in(d)
    m = manifest(d, base)
    assert (m["environment"], m["database"], m["cluster"], m["server_version_num"]) == ("test", "hc_backuptest_a",
                                                                                          "17/test", 170004)
    assert m["alembic_version"] == "0028" and m["kyc_enforcement"] == {"state": "draft", "generation": 0}
    assert m["dump"]["toc_entries"] == 2 and m["witness_status"] == "copied"
    loaded = load(d, base, expect_env="test")
    assert loaded.witness.read_bytes() == (tmp / "test.witness").read_bytes()
    assert (d / f"{base}.dump.sha256").read_text() == f"{hashlib.sha256(loaded.dump.read_bytes()).hexdigest()}  {base}.dump\n"
    for f in loaded.files:
        assert f.stat().st_mode & 0o077 == 0, f
    assert not list(d.glob(".partial-*")) and oct(d.stat().st_mode)[-3:] == "700"
    assert "good archive body" not in r.stdout and "sha256=" in r.stdout  # names and hashes only


@pytest.mark.parametrize("over,reason", [
    ({"STUB_DUMP_FAIL": "1"}, "pg_dump failed"),
    ({"STUB_DUMP_BODY": "CORRUPT body"}, "cannot read the new archive"),
])
def test_a_failed_or_unreadable_dump_leaves_no_set_behind(box, over, reason):
    tmp, bin_dir = box
    r = run_script(env_for(tmp, bin_dir, **over))
    assert r.returncode == 1 and reason in r.stderr, r.stderr
    d = tmp / "backups"
    assert sets_in(d) == [] and not list(d.glob("*.dump")) and not list(d.glob(".partial-*"))


def test_a_set_altered_after_it_was_written_is_never_trusted(box):
    tmp, bin_dir = box
    assert run_script(env_for(tmp, bin_dir)).returncode == 0
    d = tmp / "backups"
    (base,) = sets_in(d)
    dump = d / f"{base}.dump"
    dump.write_bytes(dump.read_bytes()[:-3])  # truncated
    with pytest.raises(SetError, match="dump_checksum_mismatch"):
        load(d, base)
    (d / f"{base}.manifest.json").unlink()  # a set without its manifest is not complete
    with pytest.raises(SetError, match="incomplete_set"):
        load(d, base)


def test_an_overlapping_run_is_refused_without_touching_anything(box):
    tmp, bin_dir = box
    d = tmp / "backups"
    d.mkdir(mode=0o700)
    with open(d / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = run_script(env_for(tmp, bin_dir))
    assert r.returncode == 75 and "another backup" in r.stderr
    assert sets_in(d) == []


def test_two_concurrent_runs_produce_exactly_one_set(box):
    tmp, bin_dir = box
    env = env_for(tmp, bin_dir, STUB_DUMP_SLEEP="2")
    first = subprocess.Popen(["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.7)
    second = run_script(env)
    first.communicate(timeout=30)
    assert (first.returncode, second.returncode) == (0, 75)
    assert len(sets_in(tmp / "backups")) == 1


def test_a_missing_witness_keeps_the_database_set_but_fails_visibly(box):
    tmp, bin_dir = box
    (tmp / "test.witness").unlink()
    r = run_script(env_for(tmp, bin_dir))
    assert r.returncode == 3 and "witness" in r.stderr
    d = tmp / "backups"
    (base,) = sets_in(d)
    assert manifest(d, base)["witness_status"] == "missing" and load(d, base).witness is None


def test_a_witness_symlink_is_never_followed(box):
    tmp, bin_dir = box
    (tmp / "test.witness").unlink()
    (tmp / "elsewhere").write_text("secret\n")
    (tmp / "test.witness").symlink_to(tmp / "elsewhere")
    r = run_script(env_for(tmp, bin_dir))
    assert r.returncode == 3
    assert not list((tmp / "backups").glob("*.witness"))


# ── retention ───────────────────────────────────────────────────────────────────

def _old_set(d: Path, db: str, stamp: str, days: int) -> None:
    for suffix in (".dump", ".dump.sha256", ".witness", ".manifest.json"):
        p = d / f"{db}_{stamp}{suffix}"
        p.write_text("x")
        t = time.time() - days * 86400
        os.utime(p, (t, t))


def test_retention_keeps_the_newest_sets_and_touches_nothing_else(box):
    tmp, bin_dir = box
    d = tmp / "backups"
    d.mkdir(mode=0o700)
    for i in range(1, 6):
        _old_set(d, "hc_backuptest_a", f"2026010{i}T020000Z", 40)
    _old_set(d, "hc_backuptest_other", "20260101T020000Z", 40)          # another database: never touched
    (d / "hc_backuptest_a_20251201T020000Z.dump").write_text("old layout")  # before manifests
    (d / "notes.txt").write_text("keep")
    (d / "hc_backuptest_a_20251101T020000Z.manifest.json").symlink_to(tmp / "test.witness")  # a symlink: never followed
    stale = d / ".partial-20250101T000000Z-abc"
    stale.mkdir()
    os.utime(stale, (time.time() - 3 * 86400,) * 2)
    for p in d.iterdir():
        if p.name.startswith("hc_backuptest_a_2025") and not p.is_symlink():
            os.utime(p, (time.time() - 60 * 86400,) * 2)
    r = run_script(env_for(tmp, bin_dir, HYPERCHAT_BACKUP_KEEP_MIN=3))
    assert r.returncode == 0, r.stderr
    left = [s for s in sets_in(d) if s.startswith("hc_backuptest_a_")]
    assert len(left) == 3 + 1  # the new set, the two newest old ones, and the untouchable symlinked name
    assert "hc_backuptest_a_20260105T020000Z" in left and "hc_backuptest_a_20260104T020000Z" in left
    assert "hc_backuptest_a_20260101T020000Z" not in left
    assert (d / "hc_backuptest_other_20260101T020000Z.dump").exists() and (d / "notes.txt").exists()
    assert not (d / "hc_backuptest_a_20251201T020000Z.dump").exists()  # old layout, superseded
    assert (tmp / "test.witness").exists() and (d / "hc_backuptest_a_20251101T020000Z.manifest.json").is_symlink()
    assert not stale.exists()


def test_retention_never_removes_the_last_sets_however_old(box):
    tmp, bin_dir = box
    d = tmp / "backups"
    d.mkdir(mode=0o700)
    for i in range(1, 3):
        _old_set(d, "hc_backuptest_a", f"2026010{i}T020000Z", 400)
    r = run_script(env_for(tmp, bin_dir, HYPERCHAT_BACKUP_KEEP_MIN=3, HYPERCHAT_BACKUP_KEEP_DAYS=1))
    assert r.returncode == 0 and len(sets_in(d)) == 3
