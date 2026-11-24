"""PAY-15 stage 6b: the DEV packet's gate scripts (deploy/pay15-6b) on a FAKE host, no database, no Docker.

The real run_gate.sh / common.sh / gate files run under bash with fakes on PATH (systemctl, psql, curl, sudo, id,
hostname, df, free) and fake release trees, backup sets and api.env in a temporary directory (B6_TEST=1, refused on a
real server). The fake psql answers only the queries the gates make, so a renamed query fails loudly. Proved here:
the package/manifest guard, G0 passing and refusing (identity, switch on), W refusing without the owner's decisions,
W restoring the OLD services on a failure before the migration, W leaving services DOWN after a failed check past the
migration, a complete W, and the time-window guard. The SQL itself is proved on PostgreSQL in database/tests
(test_pay15_6b_packet_sql_on_a_fresh_0029_to_0035_upgrade)."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bash"),
                                reason="the 6b gates are bash for a Linux host")
SRC = Path(__file__).resolve().parents[2] / "deploy" / "pay15-6b"
OLD, NEW = "0963113493c7", "26a458af0c8b"
IDENTITY = "hyperchat_dev 5433 15/restoretest 1 /var/lib/postgresql/15/restoretest"
EXPECTED = re.search(r'^VERIFY_EXPECTED="(.*?)"$', (SRC / "common.sh").read_text(), re.S | re.M).group(1)
V11 = "V11 backfill rows 4 credited 270.00"
PRESTATE = [f"{k} fake" for k in ("totals", "donations", "donation rows", "webhook events", "webhook rows", "evidence",
                                   "evidence rows", "users")] + ["jobs done=3"]

FAKE = r'''
import json, os, sys
from pathlib import Path
S = Path(os.environ["FAKE_STATE"]); name = Path(sys.argv[1]).name; args = sys.argv[2:]
state = json.loads(S.read_text())
def save(): S.write_text(json.dumps(state))
def log(*a): open(state["log"], "a").write(" ".join((name,) + a) + "\n")
if name == "systemctl":
    if args[0] in ("start", "stop"):
        for unit in args[1:]:
            log(args[0], unit)
            if args[0] == "start" and unit == state["backup_unit"]:
                if state.get("backup_fail"): sys.exit(1)
                base = "hyperchat_dev_20261124T110000Z"; d = Path(state["backup_dir"])
                (d / (base + ".dump")).write_bytes(b"window dump")
                h = __import__("hashlib").sha256(b"window dump").hexdigest()
                (d / (base + ".dump.sha256")).write_text(f"{h}  {base}.dump\n")
                (d / (base + ".manifest.json")).write_text(json.dumps({"dump": {"file": base + ".dump", "sha256": h}}))
                continue
            state["units"][unit] = "active" if args[0] == "start" else "inactive"
        save(); sys.exit(0)
    if args[0] == "is-active":
        print(state["units"].get(args[1], "inactive")); sys.exit(0)
    if args[0] == "show":
        print({"MainPID": "4242", "NRestarts": "0"}.get(args[2], "n/a")); sys.exit(0)
    sys.exit(0)  # list-timers and friends: nothing
if name == "curl":
    if state["units"].get("hyperchat-api") != "active": sys.exit(7)
    rel = Path(os.path.realpath(state["current"])).name
    print(json.dumps({"status": "ok", "release": rel, "database": {"status": "ok"}})); sys.exit(0)
if name == "psql":
    sql = args[args.index("-Atc") + 1] if "-Atc" in args else sys.stdin.read()
    migrated = Path(state["marker"]).exists()
    if "pg_control_system" in sql: print(state["identity"])
    elif "alembic_version" in sql: print("0035" if migrated else "0029")
    elif "FROM jobs WHERE status" in sql or "realtime_outbox" in sql or "pg_stat_activity" in sql: print("0")
    elif "datacl" in sql or "pg_db_role_setting" in sql: print("-")
    elif "FROM payment_accounting_state" in sql: print("off")
    elif "'totals '" in sql: print("\n".join(state["prestate"]))
    elif "'V1 head '" in sql: print("\n".join(state["verify"]))
    else: print("fake psql: unexpected query: " + sql[:120], file=sys.stderr); sys.exit(3)
    sys.exit(0)
sys.exit(f"fake {name}: not emulated")
'''
STUBS = r'''
api_cwd() { echo "$(readlink -f "$APP_CURRENT")/backend"; }
proc_cwd() { echo "$(readlink -f "$APP_CURRENT")/backend"; }
file_mode() { echo "root:hyperchat 600"; }
tree_fp() { echo "$NEW_FP"; }
pins_ok() { return 0; }
audit_summary() { for i in $(seq 1 23); do echo "OK    check $i"; done; }
'''


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _set(backup: Path, base: str, data: bytes) -> None:
    (backup / f"{base}.dump").write_bytes(data)
    (backup / f"{base}.dump.sha256").write_text(f"{_sha(data)}  {base}.dump\n")
    (backup / f"{base}.manifest.json").write_text(json.dumps({"dump": {"file": f"{base}.dump", "sha256": _sha(data)}}))


@pytest.fixture
def host(tmp_path):
    """A fake DEV host; returns run(gate, **state changes) -> (exit code, output, state, log lines)."""
    tmp_path = Path(os.path.realpath(tmp_path))  # the gates compare resolved paths
    bin_, rels, st, backup = tmp_path / "bin", tmp_path / "releases", tmp_path / "st", tmp_path / "backups"
    for d in (bin_, rels, st / "pkg" / "sql", backup):
        d.mkdir(parents=True)
    for f in SRC.glob("*.sh"):
        shutil.copy(f, st / "pkg" / f.name)
    for f in (SRC / "sql").glob("*.sql"):
        shutil.copy(f, st / "pkg" / "sql" / f.name)
    (st / "pkg" / "release.env").write_text(
        f"NEW_ID={NEW}\nCOMMIT={NEW}6a3c11e8b9ada7ed3ac3fd825ce0\nTAR_SHA={'1' * 64}\nNEW_FP={'2' * 64}\nNEW_FILES=380\n"
        f"SMOKE_SHA={'3' * 64}\nLOCK_NAME=lock.txt\nLOCK_SHA={'4' * 64}\nPINS='x backend/app/main.py'\n")
    names = sorted(str(p.relative_to(st / "pkg")) for p in (st / "pkg").rglob("*") if p.is_file())
    (st / "pkg" / "MANIFEST.sha256").write_text("".join(f"{_sha((st / 'pkg' / n).read_bytes())} *{n}\n" for n in names))
    _release(rels / OLD)  # the running release; the new one appears only once B has built it (_w_ready)
    pip = rels / OLD / "venv" / "bin" / "pip"
    pip.write_text("#!/bin/sh\necho 'fastapi==0.142.2'\n")
    pip.chmod(0o755)
    marker = tmp_path / "migrated"
    (tmp_path / "current").symlink_to(rels / OLD)
    (tmp_path / "api.env").write_text("HYPERCHAT_APP_ENV=development\n")
    (tmp_path / "machine-id").write_text("fake-machine\n")
    (tmp_path / "roles.sql").write_text("-- roles\n")
    (tmp_path / "audit.sh").write_text("echo audit\n")
    _set(backup, "hyperchat_dev_20261124T020000Z", b"nightly dump")
    (bin_ / "fake.py").write_text(FAKE)
    for name in ("systemctl", "curl", "psql"):
        (bin_ / name).write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_ / "fake.py"}" {name} "$@"\n')
    (bin_ / "python3").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (bin_ / "sudo").write_text('#!/bin/sh\n[ "$1" = -u ] && shift 2\nexec "$@"\n')
    (bin_ / "id").write_text("#!/bin/sh\necho 0\n")
    (bin_ / "hostname").write_text("#!/bin/sh\necho fakehost\n")
    (bin_ / "df").write_text('#!/bin/sh\necho Avail\necho " 100G"\n')
    (bin_ / "free").write_text('#!/bin/sh\necho "Mem: 8000 1 1 1 1 4000"\n')
    (bin_ / "fuser").write_text("#!/bin/sh\nexit 1\n")
    for f in bin_.iterdir():
        f.chmod(0o755)
    (tmp_path / "stubs.sh").write_text(STUBS)
    state_file = tmp_path / "state.json"
    base_state = {"units": {"hyperchat-api": "active", "hyperchat-worker": "active", "hyperchat-cleanup.timer": "active"},
                  "identity": IDENTITY, "prestate": PRESTATE, "verify": EXPECTED.splitlines() + [V11],
                  "current": str(tmp_path / "current"), "marker": str(marker), "log": str(tmp_path / "calls.log"),
                  "backup_dir": str(backup), "backup_unit": "hyperchat-dev-pgbackup.service"}
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "B6_TEST": "1", "B6_STUBS": str(tmp_path / "stubs.sh"),
           "B6_NOW": "2026-11-24T10:00:00+00:00", "FAKE_STATE": str(state_file),
           "RELEASES": str(rels), "REL_NEW": str(rels / NEW), "APP_CURRENT": str(tmp_path / "current"), "ST": str(st),
           "API_ENV": str(tmp_path / "api.env"), "BACKUP_DIR": str(backup), "BACKUP_UNIT": "hyperchat-dev-pgbackup.service",
           "ROLES": str(tmp_path / "roles.sql"), "PGBIN": "/nonexistent", "MACHINE_ID_FILE": str(tmp_path / "machine-id"),
           "AUDIT": str(tmp_path / "audit.sh"), "SCRATCH_ROOT": str(tmp_path), "DEV_IDENTITY": IDENTITY,
           "HOST_ID": f"fakehost {_sha(b'fake-machine' + chr(10).encode())[:16]}",
           "ROLES_SHA": _sha(b"-- roles\n")[:16]}

    def run(gate, extra_env=None, **changes):
        state = {**base_state, **changes}
        if state_file.exists():  # keep unit states across gates
            state["units"] = {**json.loads(state_file.read_text())["units"], **changes.get("units", {})}
        state_file.write_text(json.dumps(state))
        proc = subprocess.run(["bash", str(st / "pkg" / "run_gate.sh"), gate], env={**env, **(extra_env or {})},
                              capture_output=True, text=True, timeout=180)
        calls = Path(state["log"]).read_text().splitlines() if Path(state["log"]).exists() else []
        return proc.returncode, proc.stdout + proc.stderr, json.loads(state_file.read_text()), calls
    run.st, run.marker, run.tmp = st, marker, tmp_path
    return run


def _release(path: Path) -> None:
    for sub in ("backend", "database/migrations/versions", "venv/bin"):
        (path / sub).mkdir(parents=True)


def _w_ready(host, worker="stopped"):
    """The new release B built (its fake Alembic marks the migration) and the files G0 and R leave behind."""
    st, new = host.st, host.tmp / "releases" / NEW
    _release(new)
    alembic = new / "venv" / "bin" / "python"
    alembic.write_text(f"#!/bin/sh\n: > '{host.marker}'\nexit 0\n")
    alembic.chmod(0o755)
    (st / "G0-passed").write_text("x")
    (st / "G0-facts.env").write_text(
        f"OLD_ID={OLD}\nOLD_REL={host.tmp / 'releases' / OLD}\nENV_SHA={_sha((host.tmp / 'api.env').read_bytes())}\n"
        "DB_ACL=-\nDB_SETTINGS=-\n")
    for marker in ("B-passed", "R-passed"):
        (st / marker).write_text("x")
    (st / "R-prestate.txt").write_text("\n".join(PRESTATE) + "\n")
    (st / "R-recorded.txt").write_text(V11 + "\n")
    (st / "decisions.env").write_text(f"OD_6B_WINDOW_APPROVED=yes\nOD_6B_WORKER={worker}\n")


def test_a_modified_package_is_refused_before_anything_runs(host):
    (host.st / "pkg" / "w_window.sh").write_text("echo changed\n")
    code, out, _, calls = host("G0")
    assert code == 1 and "does not match its manifest" in out and calls == []


def test_g0_passes_on_the_expected_host_and_records_the_facts(host):
    code, out, _, calls = host("G0")
    assert code == 0, out
    facts = (host.st / "G0-facts.env").read_text()
    assert f"OLD_ID={OLD}\n" in facts and "DB_ACL=-\n" in facts and (host.st / "G0-passed").exists()
    assert (host.st / "prestate-G0.txt").read_text().splitlines() == PRESTATE
    assert not [c for c in calls if " start " in f" {c} " or " stop " in f" {c} "]  # G0 changed nothing


def test_g0_stops_on_another_database_cluster(host):
    code, out, _, _ = host("G0", identity="hyperchat_dev 5433 17/hyperchat 9 /var/lib/postgresql/17/hyperchat")
    assert code == 1 and "not live DEV" in out and not (host.st / "G0-passed").exists()


def test_g0_fails_when_api_env_enables_the_accounting_switch(host):
    (host.tmp / "api.env").write_text("HYPERCHAT_PAYMENT_ACCOUNTING_ENABLED=true\n")
    code, out, _, _ = host("G0")
    assert code == 1 and "enables HYPERCHAT_PAYMENT_ACCOUNTING_ENABLED" in out and not (host.st / "G0-passed").exists()


@pytest.mark.parametrize("decisions", [None, "OD_6B_WINDOW_APPROVED=no\nOD_6B_WORKER=start\n",
                                       "OD_6B_WINDOW_APPROVED=yes\nOD_6B_WORKER=maybe\n"])
def test_w_refuses_without_the_owners_decisions_and_touches_nothing(host, decisions):
    _w_ready(host)
    (host.st / "decisions.env").unlink()
    if decisions:
        (host.st / "decisions.env").write_text(decisions)
    code, out, state, calls = host("W")
    assert code == 1 and "decisions.env" in out and calls == [] and not host.marker.exists()


def test_w_puts_the_old_services_back_when_the_backup_fails_before_the_migration(host):
    _w_ready(host)
    code, out, state, calls = host("W", backup_fail=True)
    assert code == 1 and "W NOT DONE" in out and not host.marker.exists(), out
    assert calls[-3:] == ["systemctl start hyperchat-api", "systemctl start hyperchat-worker",
                          "systemctl start hyperchat-cleanup.timer"], calls
    assert state["units"]["hyperchat-api"] == "active" and os.path.realpath(host.tmp / "current").endswith(OLD)


def test_w_leaves_the_services_down_and_names_x_after_a_failed_check_past_the_migration(host):
    _w_ready(host)
    bad = EXPECTED.replace("V5 donations whose history differs from credited 0",
                           "V5 donations whose history differs from credited 1").splitlines() + [V11]
    code, out, state, calls = host("W", verify=bad)
    assert code == 1 and "NO-GO" in out and "run X" in out and host.marker.exists(), out
    assert state["units"]["hyperchat-api"] == "inactive" and "systemctl start hyperchat-api" not in calls
    assert os.path.realpath(host.tmp / "current").endswith(OLD)  # never switched, never the old code on 0035


def test_a_complete_window_migrates_switches_and_starts_only_the_new_release(host):
    _w_ready(host, worker="stopped")
    code, out, state, calls = host("W")
    assert code == 0 and "W OK" in out and host.marker.exists(), out
    assert os.path.realpath(host.tmp / "current").endswith(NEW)
    assert calls.index("systemctl stop hyperchat-api") < calls.index("systemctl start hyperchat-dev-pgbackup.service") \
        < calls.index("systemctl start hyperchat-api")
    assert state["units"]["hyperchat-worker"] == "inactive" and (host.st / "W-set.txt").read_text().strip() \
        == "hyperchat_dev_20261124T110000Z"


@pytest.mark.parametrize("now, allowed", [("2026-11-24T10:00:00+00:00", True), ("2026-11-24T02:00:00+00:00", False),
                                          ("2026-11-24T16:20:00+00:00", False), ("2026-11-24T00:30:00+00:00", False)])
def test_the_window_guard_refuses_the_backup_monitor_and_offsite_times(host, now, allowed):
    _w_ready(host)
    code, out, _, calls = host("W", extra_env={"B6_NOW": now})
    assert ("overlap a blocked time" in out) is (not allowed), out  # 00:30 + 75 min reaches the 01:20 backups
    if not allowed:
        assert code == 1 and calls == []
