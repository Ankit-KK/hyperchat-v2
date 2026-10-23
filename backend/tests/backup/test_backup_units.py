"""deploy/backup units and templates: each environment backs up its own database with its own tools and directory,
off-site copies are separate, disabled until configured, hardened, and never read the API's environment file."""

import re

import pytest

from tests.backup.helpers import REPO

BACKUP = REPO / "deploy/backup"


def unit(name: str) -> dict[str, str]:
    out, section = {}, None
    for line in (BACKUP / name).read_text().splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            out[f"{section}{key}"] = value
    return out


def test_each_environment_has_its_own_backup_unit_and_time():
    prod, dev = unit("hyperchat-pgbackup.service"), unit("hyperchat-dev-pgbackup.service")
    assert prod["[Service]Environment"] == "HYPERCHAT_BACKUP_ENV=prod"
    assert dev["[Service]Environment"] == "HYPERCHAT_BACKUP_ENV=dev"
    assert prod["[Unit]Requires"] == "postgresql@17-hyperchat.service"
    assert dev["[Unit]Requires"] == "postgresql@15-restoretest.service"
    assert prod["[Service]ExecStart"] == dev["[Service]ExecStart"] == "/usr/local/sbin/hyperchat-pgbackup"
    times = {unit(t)["[Timer]OnCalendar"] for t in ("hyperchat-pgbackup.timer", "hyperchat-dev-pgbackup.timer")}
    assert times == {"*-*-* 02:30:00 UTC", "*-*-* 02:00:00 UTC"}  # production's time unchanged; no overlap


@pytest.mark.parametrize("name,env,release", [("hyperchat-offsite.service", "prod", "/opt/hyperchat-prod/current/"),
                                              ("hyperchat-dev-offsite.service", "dev", "/opt/hyperchat/current/")])
def test_offsite_units_are_per_environment_disabled_until_configured_and_hardened(name, env, release):
    u = unit(name)
    assert u["[Unit]ConditionPathExists"] == f"/etc/hyperchat-backup/offsite-{env}.env"
    exec_start = u["[Service]ExecStart"]
    assert exec_start.startswith(release) and f"--config /etc/hyperchat-backup/offsite-{env}.env --latest" in exec_start
    directives = "\n".join(line for line in (BACKUP / name).read_text().splitlines() if not line.startswith("#"))
    assert "api.env" not in directives  # never the API's credentials
    other = "/opt/hyperchat/current/" if env == "prod" else "/opt/hyperchat-prod/current/"
    assert other not in directives
    for key, value in {"NoNewPrivileges": "yes", "ProtectSystem": "strict", "ProtectHome": "yes", "PrivateTmp": "yes",
                       "CapabilityBoundingSet": "", "AmbientCapabilities": "", "UMask": "0077"}.items():
        assert u[f"[Service]{key}"] == value, key
    assert "EnvironmentFile" not in "".join(u)


def test_offsite_timers_run_after_their_backups():
    assert unit("hyperchat-offsite.timer")["[Timer]OnCalendar"] == "*-*-* 03:30:00 UTC"
    assert unit("hyperchat-dev-offsite.timer")["[Timer]OnCalendar"] == "*-*-* 03:00:00 UTC"


def test_backup_files_hold_no_credentials_or_endpoints():
    for path in BACKUP.iterdir():
        text = path.read_text()
        assert not re.search(r"(?m)^HYPERCHAT_OFFSITE_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|ENDPOINT|BUCKET)=\S", text), path
        assert "r2.cloudflarestorage.com/" not in text and "AKIA" not in text, path
