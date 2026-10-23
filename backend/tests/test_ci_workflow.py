"""The CI workflow (.github/workflows/ci.yml): triggers, concurrency, stable check names, and the PostgreSQL 15 job's
scope rule. Pins what the Actions-minutes optimization must not lose."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
SCOPE = REPO / ".github/scripts/pg15-scope.sh"
CHECK_NAMES = {
    "unit": "Unit tests (no database)",
    "deploy-config": "Deployment configuration (nginx syntax + rate limits)",
    "integration": "Database + integration tests (PostgreSQL 17)",
    "postgres15": "PostgreSQL 15 compatibility (migrations + permissions)",
}


def test_pull_requests_and_main_run_and_branch_pushes_do_not():
    on = WORKFLOW[True]  # YAML 1.1 reads the key `on` as a boolean
    assert on["push"] == {"branches": ["main"]}
    assert on["pull_request"] is None  # default types: opened, synchronize, reopened
    assert "workflow_dispatch" in on
    assert "paths" not in str(on) and "paths-ignore" not in str(on)  # never skip a whole run by path


def test_only_superseded_runs_of_the_same_pull_request_are_cancelled():
    group, cancel = WORKFLOW["concurrency"]["group"], WORKFLOW["concurrency"]["cancel-in-progress"]
    assert "github.event.pull_request.number" in group  # one group per PR
    assert "github.sha" in group and "github.ref" not in group  # each main commit / manual run alone
    assert cancel == "${{ github.event_name == 'pull_request' }}"  # main is never cancelled


def test_check_names_are_stable_and_every_suite_still_runs():
    assert {job: spec["name"] for job, spec in WORKFLOW["jobs"].items()} == CHECK_NAMES
    integration = "\n".join(str(s.get("run", "")) for s in WORKFLOW["jobs"]["integration"]["steps"])
    assert "setup-test-db.sh" in integration and integration.count("python -m pytest -q") == 2  # full, unfiltered
    assert WORKFLOW["jobs"]["integration"]["services"]["postgres"]["image"].endswith("postgres:17")
    assert "nginx_ratelimit_check.sh" in str(WORKFLOW["jobs"]["deploy-config"]["steps"])


def test_the_postgres15_job_excludes_only_the_postgres17_importer():
    steps = WORKFLOW["jobs"]["postgres15"]["steps"]
    runs = "\n".join(str(s.get("run", "")) for s in steps)
    assert "postgres:15" in runs and "'^15\\.'" in runs
    assert "--deselect tests/test_auth_hash_import.py" in runs and runs.count("--deselect") == 1
    for suite in ("tests/auth", "tests/kyc", "tests/stepup", "tests/admin", "tests/backup"):
        assert suite in runs
    gated = [s for s in steps if s.get("id") != "scope" and "actions/checkout" not in str(s.get("uses"))]
    assert all(s.get("if") == "steps.scope.outputs.run == 'true'" for s in gated)


needs_git = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("git"), reason="needs bash and git")


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    git("config", "user.email", "ci@example.invalid")
    git("config", "user.name", "ci")
    (tmp_path / "README.md").write_text("x")
    git("add", ".")
    git("commit", "-qm", "base")
    return tmp_path, git


def _scope(repo_dir: Path, base: str) -> str:
    out = repo_dir / "out"
    out.unlink(missing_ok=True)
    subprocess.run(["bash", str(SCOPE), base], cwd=repo_dir, check=True, capture_output=True,
                   env={"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(out)})
    return out.read_text().strip()


@needs_git
@pytest.mark.parametrize("path, expected", [
    ("docs/backup-recovery.md", "run=false"),
    ("deploy/nginx/site.conf", "run=false"),
    ("database/migrations/versions/0029_x.py", "run=true"),
    ("deploy/postgres/roles.sql", "run=true"),
    ("backend/app/kyc/service.py", "run=true"),
    ("backend/tests/stepup/test_x.py", "run=true"),
    (".github/workflows/ci.yml", "run=true"),
    ("backend/requirements.txt", "run=true"),
])
def test_the_scope_rule(repo, path, expected):
    d, git = repo
    base = git("rev-parse", "HEAD").strip()
    (d / path).parent.mkdir(parents=True, exist_ok=True)
    (d / path).write_text("change")
    git("add", ".")
    git("commit", "-qm", "change")
    assert _scope(d, base) == expected


@needs_git
@pytest.mark.parametrize("base", ["", "0" * 40, "not-a-sha", "f" * 40])
def test_the_scope_rule_fails_open_without_a_usable_base(repo, base):
    d, _ = repo
    assert _scope(d, base) == "run=true"  # manual runs, a first push, an unknown base: run
