"""Shared fixtures for the backup / recovery tests: stub PostgreSQL tools and a runner for the real backup script."""

import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "deploy/backup/hyperchat-pgbackup.sh"
needs_bash = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash") or not shutil.which("flock"),
                                reason="needs bash and flock (Linux)")

PSQL = r"""#!/usr/bin/env bash
[[ "$1" == --version ]] && { echo "psql (PostgreSQL) ${STUB_MAJOR:-17}.4"; exit 0; }
[[ -n "${STUB_PSQL_FAIL:-}" ]] && exit 2
sql=""; db=""
while (( $# )); do case "$1" in -c) sql="$2"; shift 2 ;; -d) db="$2"; shift 2 ;; *) shift ;; esac; done
case "$sql" in
  "SHOW server_version_num") echo "${STUB_SERVER_NUM:-170004}" ;;
  "SELECT current_database()") echo "${STUB_CURRENT_DB:-$db}" ;;
  *alembic_version*) echo "${STUB_ALEMBIC:-0028}" ;;
  *kyc_enforcement*) echo "${STUB_KYC:-draft 0}" ;;
  *stepup_enforcement*) echo "draft" ;;
  *) echo "unexpected sql" >&2; exit 3 ;;
esac
"""
PG_DUMP = r"""#!/usr/bin/env bash
[[ "$1" == --version ]] && { echo "pg_dump (PostgreSQL) ${STUB_DUMP_MAJOR:-${STUB_MAJOR:-17}}.4"; exit 0; }
[[ -n "${STUB_DUMP_SLEEP:-}" ]] && sleep "$STUB_DUMP_SLEEP"
printf 'PGDMP%s' "${STUB_DUMP_BODY:-good archive body}"
[[ -n "${STUB_DUMP_FAIL:-}" ]] && exit 1
exit 0
"""
PG_RESTORE = r"""#!/usr/bin/env bash
[[ "$1" == --version ]] && { echo "pg_restore (PostgreSQL) ${STUB_MAJOR:-17}.4"; exit 0; }
f="${@: -1}"
head -c 5 "$f" | grep -q PGDMP || { echo "pg_restore: error: input file does not appear to be a valid archive" >&2; exit 1; }
grep -q CORRUPT "$f" && { echo "pg_restore: error: could not read" >&2; exit 1; }
printf ';\n; Archive created\n;\n1; 0 0 TABLE public users hyperchat_owner\n2; 0 0 TABLE public streamers hyperchat_owner\n'
"""


def stub_bin(tmp: Path) -> Path:
    bin_dir = tmp / "pgbin"
    bin_dir.mkdir()
    for name, text in (("psql", PSQL), ("pg_dump", PG_DUMP), ("pg_restore", PG_RESTORE)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    return bin_dir


def env_for(tmp: Path, bin_dir: Path, **over) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HYPERCHAT_BACKUP_", "STUB_"))}
    env.update({"HYPERCHAT_BACKUP_ENV": "test", "HYPERCHAT_BACKUP_DB": "hc_backuptest_a",
                "HYPERCHAT_BACKUP_PORT": "5555", "HYPERCHAT_BACKUP_CLUSTER": "17/test",
                "HYPERCHAT_BACKUP_DIR": str(tmp / "backups"), "HYPERCHAT_BACKUP_WITNESS": str(tmp / "test.witness"),
                "HYPERCHAT_BACKUP_HOST": "/tmp", "HYPERCHAT_BACKUP_RUN_AS": "", "HYPERCHAT_BACKUP_OWNER": getpass.getuser(),
                "HYPERCHAT_BACKUP_PG_BIN": str(bin_dir)})
    env.update({k: str(v) for k, v in over.items()})
    return env


def run_script(env: dict, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=timeout)


def sets_in(directory: Path) -> list[str]:
    return sorted(p.name[: -len(".manifest.json")] for p in directory.glob("*.manifest.json"))


def manifest(directory: Path, base: str) -> dict:
    return json.loads((directory / f"{base}.manifest.json").read_text())
