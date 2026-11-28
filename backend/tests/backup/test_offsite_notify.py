"""deploy/backup/hyperchat-offsite-notify, the off-site / production backup alert, run for real under bash with a fake
curl and a fake systemctl (no network, no systemd). Credentials stay in curl --config files, never in argv; the label
names the environment; and a failed unit's result comes from systemd's $MONITOR_* variables, because inside the alert
unit's DynamicUser sandbox "systemctl show" returns nothing (F-1, 2026-11-28: every real alert said "unknown")."""

import shutil
import sys
import subprocess
from pathlib import Path

import pytest

from tests.backup.helpers import REPO

NOTIFY = REPO / "deploy/backup/hyperchat-offsite-notify"
pytestmark = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="needs bash (Linux)")


@pytest.fixture
def box(tmp_path):
    bin_dir, cred = tmp_path / "bin", tmp_path / "cred"
    bin_dir.mkdir()
    cred.mkdir()
    (bin_dir / "curl").write_text('#!/bin/bash\n{ printf "ARGS:"; printf " [%s]" "$@"; echo; } >> "$FAKE_LOG"\n'
                                  '[ "${FAKE_CURL_RC:-0}" = 0 ]\n')
    # the sandbox case: systemctl answers nothing unless FAKE_SHOW says what to print
    (bin_dir / "systemctl").write_text('#!/bin/bash\n[ -n "${FAKE_SHOW:-}" ] || exit 1\n'
                                       'case "$*" in *Result*) echo "${FAKE_SHOW% *}";; *ExecMainStatus*) echo "${FAKE_SHOW#* }";; esac\n')
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    (cred / "notify.curl").write_text('url = "https://discord.example.invalid/hook"\n')
    (cred / "heartbeat-fail.curl").write_text("# not configured\n")
    return tmp_path


def run(box: Path, *args, **env):
    full = {"PATH": f"{box / 'bin'}:/usr/bin:/bin", "FAKE_LOG": str(box / "log"), "CREDENTIALS_DIRECTORY": str(box / "cred"),
            **env}
    (box / "log").write_text("")
    r = subprocess.run(["bash", str(NOTIFY), *args], env=full, capture_output=True, text=True, timeout=30)
    return r, (box / "log").read_text()


def test_a_failed_unit_reports_systemds_result_even_when_systemctl_answers_nothing(box):
    r, log = run(box, "fail", "hyperchat-pgbackup.service", MONITOR_UNIT="hyperchat-pgbackup.service",
                 MONITOR_SERVICE_RESULT="exit-code", MONITOR_EXIT_STATUS="3")
    assert r.returncode == 0 and "alert sent" in r.stdout
    assert "[PRODUCTION backup] FAILED: hyperchat-pgbackup.service result=exit-code status=3 " in log
    assert "unknown" not in log


def test_without_monitor_variables_systemctl_is_the_fallback(box):
    _, log = run(box, "fail", "hyperchat-offsite.service", FAKE_SHOW="timeout 0")
    assert "FAILED: hyperchat-offsite.service result=timeout status=0 " in log
    _, log = run(box, "fail", "hyperchat-offsite.service")  # neither source: said honestly, never guessed
    assert "result=unknown status=unknown" in log


def test_monitor_variables_of_another_unit_are_ignored(box):
    _, log = run(box, "fail", "hyperchat-pgbackup.service", MONITOR_UNIT="other.service",
                 MONITOR_SERVICE_RESULT="exit-code", MONITOR_EXIT_STATUS="9", FAKE_SHOW="signal 15")
    assert "result=signal status=15 " in log and "status=9" not in log


@pytest.mark.parametrize("inst,label", [("hyperchat-dev-offsite.service", "DEV off-site backup"),
                                        ("hyperchat-offsite.service", "PRODUCTION backup"),
                                        ("hyperchat-pgbackup.service", "PRODUCTION backup")])
def test_the_label_names_the_environment(box, inst, label):
    _, log = run(box, "fail", inst, MONITOR_UNIT=inst, MONITOR_SERVICE_RESULT="exit-code", MONITOR_EXIT_STATUS="1")
    assert f'[{{"text": "[{label}] FAILED: {inst} result=exit-code status=1 ' in log


def test_test_alerts_and_credentials(box):
    r, log = run(box, "fail", "TEST-prod")
    assert r.returncode == 0 and "[PRODUCTION backup] TEST alert, no failure" in log and log.count("ARGS:") == 1
    assert f"[--config] [{box / 'cred' / 'notify.curl'}]" in log and "example.invalid" not in log  # URL never in argv
    r, _ = run(box, "fail", "hyperchat-pgbackup.service", FAKE_CURL_RC="22")
    assert r.returncode == 1 and "alert NOT sent" in r.stdout


def test_the_templates_run_this_notifier_sandboxed():
    for name, mode in (("hyperchat-offsite-alert@.service", "fail"), ("hyperchat-offsite-ok@.service", "ok")):
        text = (REPO / "deploy/backup" / name).read_text()
        assert f"ExecStart=/usr/local/sbin/hyperchat-offsite-notify {mode} %i" in text
        for line in ("DynamicUser=yes", "NoNewPrivileges=yes", "ProtectSystem=strict", "CapabilityBoundingSet="):
            assert line in text.splitlines(), (name, line)
