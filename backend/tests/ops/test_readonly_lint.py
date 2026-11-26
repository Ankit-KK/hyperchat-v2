"""OPS-2 repo tooling (incident 2026-10-28): the read-only lint (S3) and the local nginx drift check (S4).

Pure unit tests, no database, no server: the lint runs on temporary fixture scripts and on repository scripts; the
drift check runs only in local mode (never --nginx-test)."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
LINT = REPO / "deploy/tools/readonly_lint.py"
DRIFT = REPO / "deploy/tools/nginx_drift_check.sh"
# On Windows `bash` can resolve to the WSL launcher; these tests never touch WSL.
needs_bash = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="needs bash (Linux)")


def lint(tmp_path: Path, text: str) -> subprocess.CompletedProcess:
    script = tmp_path / "check.sh"
    script.write_text(text, encoding="utf-8")
    return run(script)


def run(*paths: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-B", str(LINT), *map(str, paths)], capture_output=True, text=True,
                          timeout=60)


def test_the_incident_command_with_its_quotes_stripped_fails(tmp_path):
    # What the server ran after PowerShell 5.1 dropped the double quotes: `-> $(...)` is an output redirect.
    r = lint(tmp_path, "for f in /etc/nginx/snippets/*; do\n"
                       "  echo $(sha256sum < $(readlink -f $f) | cut -c1-16) $f -> $(readlink -f $f)\n"
                       "done\n")
    assert r.returncode == 1
    assert "check.sh:2: redirect `>`" in r.stdout


def test_the_intended_quoted_command_passes(tmp_path):
    r = lint(tmp_path, 'echo "$(sha256sum < $(readlink -f $f) | cut -c1-16) $f -> $(readlink -f $f)"\n')
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("line", [
    "echo hi 2>&1", "echo hi >&2", "ls x >/dev/null", "ls x 2>/dev/null", "ls x > /dev/null 2>&1",
    "grep -c x <(sort f)", "echo 'a > b' \"c >> d\"", "echo x # writes > nothing, a comment",
    "n=${#arr[@]}", "systemctl show hyperchat-api -p NRestarts -p ExecMainStartTimestamp",
    "systemctl status nginx", "sed -n 1,5p f", "nginx -t", "find / -name x -printf '%p > %s\\n'",
    "x=$'a\\'b > c'", "awk '{ print $1 > \"/tmp/x\" }' f",
])
def test_read_only_constructs_pass(tmp_path, line):
    r = lint(tmp_path, line + "\n")
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("line, construct", [
    ("echo x > /etc/nginx/a", "redirect `>`"), ("echo x >> f", "redirect `>>`"), ("echo x 2> err.log", "redirect"),
    ("echo x >| f", "redirect"), ("cmd &> f", "redirect"), ('echo "$(date > /root/x)"', "redirect"),
    ("echo x | tee /etc/x", "tee"), ("sed -i s/a/b/ f", "sed -i"), ("sed -ri.bak s/a/b/ f", "sed -i"),
    ("mv a /etc/b", "mv"), ("rm -f /etc/x", "rm"), ("/bin/rm x", "rm"), ("cp a /opt/b", "cp"),
    ("install -m 644 a /etc/b", "install"), ("chmod 600 f", "chmod"), ("chown root f", "chown"),
    ("systemctl restart nginx", "systemctl restart"), ("systemctl daemon-reload", "systemctl daemon-reload"),
    ("sudo systemctl enable --now x", "systemctl enable"), ("nginx -s reload", "nginx -s"),
    ("apt-get install -y x", "apt-get"), ("apt upgrade", "apt"), ("dpkg -i x.deb", "dpkg"),
    ("pip install x", "pip install"), ("truncate -s 0 f", "truncate"), ("dd if=/dev/zero of=/dev/sda", "dd of="),
    ("x=$(rm -rf /tmp/y)", "rm"), ("if true; then chmod 777 f; fi", "chmod"),
])
def test_write_constructs_fail_with_file_and_line(tmp_path, line, construct):
    r = lint(tmp_path, "# header\n" + line + "\n")
    assert r.returncode == 1, r.stdout
    assert f"check.sh:2: {construct}" in r.stdout


def test_a_reviewed_exception_is_reported_as_allowed_and_passes(tmp_path):
    r = lint(tmp_path, "echo x > /tmp/scratch  # ro-lint: allow\n")
    assert r.returncode == 0
    assert "check.sh:1: allowed: redirect `>`" in r.stdout


def test_an_allow_marker_covers_only_its_own_line(tmp_path):
    r = lint(tmp_path, "true  # ro-lint: allow\nrm x\n")
    assert r.returncode == 1 and "check.sh:2: rm" in r.stdout


def test_quotes_spanning_lines_keep_line_numbers(tmp_path):
    r = lint(tmp_path, "awk '\n  { print > \"x\" }\n' f\nrm y\n")
    assert r.returncode == 1
    assert "check.sh:4: rm" in r.stdout and "check.sh:2" not in r.stdout


def test_a_missing_file_never_passes(tmp_path):
    assert run(tmp_path / "missing.sh").returncode == 2


@pytest.mark.parametrize("script", ["deploy/check_isolation.sh",  # header: "it changes nothing"
                                    "docs/operations/dev-permission-repair/reconcile.sh",  # header: READ-ONLY
                                    "deploy/tools/nginx_drift_check.sh"])
def test_repository_read_only_scripts_pass(script):
    r = run(REPO / script)
    assert r.returncode == 0, r.stdout


@needs_bash
def test_the_drift_check_classifies_every_nginx_file():
    r = subprocess.run(["bash", str(DRIFT)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    files = sorted(p.relative_to(REPO / "deploy/nginx").as_posix() for p in (REPO / "deploy/nginx").rglob("*")
                   if p.is_file())
    lines = r.stdout.splitlines()
    assert sorted(line.split()[1] for line in lines) == files  # exactly one line per file
    assert {line.split()[0] for line in lines} <= {"match", "drift", "unrecorded"}
    for line in lines:
        if line.startswith("drift"):
            assert "repo differs from recorded deployed" in line


def test_fingerprints_hold_only_recorded_deployed_prefixes():
    rows = [line.split()[:2] for line in (REPO / "deploy/tools/nginx_fingerprints.txt").read_text().splitlines()
            if line.strip() and not line.startswith("#")]
    recorded = {path: value for value, path in rows if value != "unrecorded"}
    assert recorded == {"00-catchall.conf": "b7a4736e", "api.quicktestspace.site.conf": "83309724",
                        "api.hyperchat.space.conf": "b14edf78",
                        "snippets/hyperchat-public-limit-zone.conf": "f9618548",
                        "snippets/hyperchat-public-limit.conf": "73ee342b",
                        "snippets/hyperchat-prod-public-limit-zone.conf": "2158db82",
                        "snippets/hyperchat-prod-public-limit.conf": "8ae65880"}
    record = "".join((REPO / "docs/operations" / name).read_text(encoding="utf-8") for name in
                     ("incident-2026-10-28-nginx-config.md", "nginx-f1-production-install-2026-11-26.md"))
    assert all(f"`{prefix}" in record for prefix in recorded.values())  # the install record lists full hashes
