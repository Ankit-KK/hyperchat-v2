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
    for job in ("integration", "postgres15"):  # 6a-2: the per-test verify template copies need the superuser
        assert WORKFLOW["jobs"][job]["env"]["HYPERCHAT_TEST_VERIFY_ADMIN"] == \
            "host=localhost port=5432 user=postgres dbname=postgres"
    assert "nginx_ratelimit_check.sh" in str(WORKFLOW["jobs"]["deploy-config"]["steps"])


def test_the_integration_job_fails_when_database_tests_are_skipped():  # audit T-8
    steps = WORKFLOW["jobs"]["integration"]["steps"]
    pytest_runs = [s for s in steps if "python -m pytest" in str(s.get("run", ""))]
    assert len(pytest_runs) == 2 and all(s["run"].endswith(" --junitxml=junit.xml") for s in pytest_runs)
    guard = steps[-1]  # last, after both reports exist; runs even when a pytest step failed
    assert guard["run"] == "python tests/ci_guard.py ../database/junit.xml junit.xml"
    assert guard["working-directory"] == "backend" and guard["if"] == "${{ !cancelled() }}"


def test_the_postgres15_job_excludes_only_the_postgres17_importer():
    steps = WORKFLOW["jobs"]["postgres15"]["steps"]
    runs = "\n".join(str(s.get("run", "")) for s in steps)
    assert "postgres:15" in runs and "'^15\\.'" in runs
    assert "--deselect tests/test_auth_hash_import.py" in runs and runs.count("--deselect") == 1
    args = [a for s in steps if s.get("working-directory") == "backend" for a in str(s.get("run", "")).split()]
    for suite in ("tests/auth", "tests/kyc", "tests/stepup", "tests/admin", "tests/onboarding", "tests/alerts",
                  "tests/backup",
                  "tests/payments/test_donation_history_guard_db.py",  # 0030: the per-role guard on DEV's PG 15
                  "tests/payments/test_donation_credit_db.py", "tests/dashboard/test_moderation_api.py",  # 0031
                  "tests/payments/test_accounting_db.py",  # 0032 reversal accounting
                  "tests/payments/test_figures_db.py",  # PAY-15 stage 4 live figures
                  "tests/payments/test_visibility_db.py",  # PAY-15 stage 5 visibility
                  "tests/payments/test_refund_snapshot_db.py",  # PAY-15 stage 5b refund snapshot
                  "tests/payments/test_activation_db.py",  # PAY-15 6a-1 (0034) accounting state and operations
                  "tests/payments/test_activation_concurrency_db.py",  # PAY-15 6a-1 lock order (C1-C13)
                  "tests/payments/test_k2_accounting_on_db.py",  # PAY-15 A6k-CI: K2 with accounting on
                  "tests/payments/test_operator_db.py",  # PAY-15 6a-3 operator CLI (OD-6a3-S)
                  "tests/payments/test_switch_db.py",  # PAY-15 6a-4 switch gate (F12-F14) and L4
                  "tests/payments/test_verify_db.py",  # PAY-15 6a-2 read-only verifier (OD-14: CI identity capture)
                  "tests/payments/test_integrity_db.py",  # T-9: every payments suite, the 0029 evidence too
                  "tests/payments/test_payments_db.py",
                  "tests/payments/test_dispute_leaves_figures_db.py",  # T-5
                  "tests/obs/test_suspended_creator_access_db.py",  # T-4
                  "tests/obs/test_player_failed_playback_db.py"):  # T-7
        assert any(suite == a or suite.startswith(a + "/") for a in args), suite  # listed, or its directory
    assert "tests/payments" in args  # T-9: the whole directory, not a hand-kept file list
    gated = [s for s in steps if s.get("id") != "scope" and "actions/checkout" not in str(s.get("uses"))]
    assert all(s.get("if") == "steps.scope.outputs.run == 'true'" for s in gated)


def test_both_database_jobs_capture_the_ci_identity_right_before_the_backend_suites():
    """OD-14: the verifier DB tests refuse unless the server matches a capture taken by the workflow itself, after
    the database is created and right before the FIRST backend pytest step (postgres15: the verifier DB step)."""
    for job, major in (("postgres15", "15"), ("integration", "17")):
        steps = WORKFLOW["jobs"][job]["steps"]
        runs = [str(s.get("run", "")) for s in steps]
        capture = runs.index(f"bash .github/scripts/ci-identity-capture.sh {major}")
        setup = next(i for i, r in enumerate(runs) if "setup-test-db.sh" in r)
        backend = min(i for i, s in enumerate(steps) if s.get("working-directory") == "backend"
                      and runs[i].startswith("python -m pytest -q"))
        assert setup < capture == backend - 1, job
    pg15 = [str(s.get("run", "")) for s in WORKFLOW["jobs"]["postgres15"]["steps"]]
    ignored = "--ignore=tests/payments/test_verify_db.py"  # the tests/payments directory run leaves it out
    verify = [r for r in pg15 if "tests/payments/test_verify_db.py" in r.replace(ignored, "")]
    assert all(ignored in r for r in pg15 if "tests/payments " in r + " ")
    diagnostic = "python -m pytest -q -ra --durations=25 --timeout=300" \
                 " --timeout-method=signal -o faulthandler_timeout=240"  # a hang fails with a traceback
    assert verify == [diagnostic + " tests/payments/test_verify_db.py"]  # once, its own step
    integration = [str(s.get("run", "")) for s in WORKFLOW["jobs"]["integration"]["steps"]]
    assert any(r.startswith(diagnostic) for r in integration)  # T-8 appends --junitxml for the skip guard


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
    ("deploy/pay15-6b/sql/verify_0035.sql", "run=true"),  # the 6b packet SQL: proved in database/tests
    ("deploy/pay15-6b/w_window.sh", "run=true"),
    ("backend/app/kyc/service.py", "run=true"),
    ("backend/tests/stepup/test_x.py", "run=true"),
    ("backend/tests/payments/test_donation_history_guard_db.py", "run=true"),
    ("backend/tests/payments/test_donation_credit_db.py", "run=true"),
    ("backend/tests/payments/test_accounting_db.py", "run=true"),
    ("backend/tests/payments/test_figures_db.py", "run=true"),
    ("backend/tests/payments/test_visibility_db.py", "run=true"),
    ("backend/tests/payments/test_refund_snapshot_db.py", "run=true"),
    ("backend/app/payments/figures.py", "run=true"),
    ("backend/tests/payments/test_activation_db.py", "run=true"),
    ("backend/tests/payments/test_activation_concurrency_db.py", "run=true"),
    ("backend/app/payments/repository.py", "run=true"),  # 6a-1: the L-1 lock order lives here
    ("backend/app/payments/verify.py", "run=true"),  # 6a-2: catalog / role-sensitive SQL as hyperchat_readonly
    ("backend/tests/payments/test_verify_db.py", "run=true"),
    ("backend/tests/payments/test_operator_db.py", "run=true"),
    ("backend/app/payments/accounting_ops.py", "run=true"),  # 6a-3: owner writes through 0034/0035
    ("backend/tests/payments/test_operator_units.py", "run=false"),
    ("backend/tests/payments/test_switch_db.py", "run=true"),
    ("backend/app/payments/switch.py", "run=true"),  # 6a-4: catalog precondition and L4 as runtime roles
    ("backend/tests/payments/test_switch_units.py", "run=false"),
    ("backend/tests/payments/test_verify_units.py", "run=false"),
    ("backend/tests/payments/test_integrity_db.py", "run=true"),  # T-9: every payments DB suite runs on PG 15
    ("backend/tests/payments/test_dispute_leaves_figures_db.py", "run=true"),  # T-5
    ("backend/tests/obs/test_suspended_creator_access_db.py", "run=true"),  # T-4
    ("backend/tests/obs/test_player_failed_playback_db.py", "run=true"),  # T-7
    ("backend/tests/obs/test_obs_db.py", "run=false"),
    ("backend/tests/payments/test_accounting_units.py", "run=false"),
    ("backend/tests/dashboard/test_moderation_api.py", "run=true"),
    ("backend/tests/dashboard/test_x.py", "run=false"),
    ("backend/tests/payments/test_x.py", "run=false"),
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
