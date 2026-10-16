"""Process and database-role isolation of the deployment files (S0, before the step-up verifier).

Static: every login role of roles.sql is reachable through the HyperChat pg_hba line (a missing role falls
through to the generic peer rule, which has no map, and cannot connect); each API OS user maps to exactly its
roles; every unit runs hardened as its own non-root user. Behaviour (bash, Linux): deploy/postgres/sync_pg_hba.sh
and the read-only server audit deploy/check_isolation.sh, against temporary files only.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_deploy_production import DEPLOY, REPO, read, unit

SVC_ROLES = {"hyperchat_app", "hyperchat_worker", "hyperchat_lifecycle", "hyperchat_auth"}
UNITS = {"systemd/hyperchat-api.service": "hyperchat", "systemd/hyperchat-worker.service": "hyperchat",
         "systemd/hyperchat-cleanup.service": "hyperchat", "systemd/hyperchat-prod-api.service": "hyperchat-prod",
         "systemd/hyperchat-prod-cleanup.service": "hyperchat-prod"}
needs_bash = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="needs bash (Linux)")


def login_roles() -> set[str]:
    return set(re.findall(r"\('(hyperchat_\w+)',\s*true\)", read("postgres/roles.sql")))


def hba_roles(text: str) -> set[str]:
    rules = [line.split() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    assert len(rules) == 1 and rules[0][0] == "local" and rules[0][-2:] == ["peer", "map=hyperchat"]
    return set(rules[0][2].split(","))


def ident(rel: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for line in read(rel).splitlines():
        if line.strip() and not line.startswith("#"):
            name, system, role = line.split()
            assert name == "hyperchat"
            out.setdefault(system, set()).add(role)
    return out


# ── static ──────────────────────────────────────────────────────────────────────

def test_every_login_role_has_a_pg_hba_rule_and_connect():
    roles = login_roles()
    assert roles == {"hyperchat_migrator", "hyperchat_app", "hyperchat_worker", "hyperchat_readonly",
                     "hyperchat_lifecycle", "hyperchat_auth"}
    assert hba_roles(read("postgres/pg_hba.conf")) == roles
    for script in (DEPLOY / "postgres/setup_cluster.sh", REPO / ".github/scripts/setup-test-db.sh"):
        grant = re.search(r"GRANT CONNECT ON DATABASE .*? TO ([\w, ]+)", script.read_text()).group(1)
        assert {r.strip() for r in grant.split(",")} == roles, script
    audit = re.search(r'LOGIN_ROLES="([^"]+)"', read("check_isolation.sh")).group(1)
    assert set(audit.split()) == roles


@pytest.mark.parametrize("rel,user,other", [("postgres/pg_ident.conf", "hyperchat", "hyperchat-prod"),
                                            ("postgres/pg_ident.production.conf", "hyperchat-prod", "hyperchat")])
def test_each_api_user_maps_to_exactly_its_roles(rel, user, other):
    maps = ident(rel)
    assert maps[user] == SVC_ROLES and other not in maps
    assert set(maps) == {"root", user}  # no third OS user can reach the database
    assert "hyperchat_migrator" not in maps[user] and "hyperchat_readonly" not in maps[user]
    assert set().union(*maps.values()) <= hba_roles(read("postgres/pg_hba.conf"))
    audit = re.search(r'SVC_ROLES="([^"]+)"', read("check_isolation.sh")).group(1)
    assert set(audit.split()) == SVC_ROLES


@pytest.mark.parametrize("rel,user", UNITS.items())
def test_every_unit_runs_hardened_as_its_own_user(rel, user):
    u = unit(rel)
    assert (u["User"], u["Group"]) == (user, user) and user != "root"
    for key, value in {"NoNewPrivileges": "yes", "ProtectSystem": "strict", "ProtectHome": "yes",
                       "PrivateTmp": "yes", "CapabilityBoundingSet": "", "AmbientCapabilities": "",
                       "UMask": "0077"}.items():
        assert u.get(key) == value, (rel, key)
    root = "/opt/hyperchat-prod/" if user == "hyperchat-prod" else "/opt/hyperchat/"
    assert u["ExecStart"].startswith(root) and u["WorkingDirectory"].startswith(root)


# ── sync_pg_hba.sh ──────────────────────────────────────────────────────────────

def sync(tmp_path: Path, current: str) -> subprocess.CompletedProcess:
    (tmp_path / "pg_hba.conf").write_text(current)
    return subprocess.run(["bash", str(DEPLOY / "postgres/sync_pg_hba.sh"), str(DEPLOY / "postgres/pg_hba.conf"),
                           str(tmp_path / "pg_hba.conf")], capture_output=True, text=True)


GENERIC = "# default\nlocal   all             postgres                                peer\n" \
          "local   all             all                                     peer\nhost all all 127.0.0.1/32 scram-sha-256\n"
WANT = [line for line in read("postgres/pg_hba.conf").splitlines() if line and not line.startswith("#")][0]


@needs_bash
def test_sync_inserts_above_the_generic_rule_and_is_idempotent(tmp_path):
    first = sync(tmp_path, GENERIC)
    assert first.returncode == 0, first.stderr
    lines = first.stdout.splitlines()
    assert lines.index(WANT) == lines.index("local   all             all                                     peer") - 1
    assert sync(tmp_path, first.stdout).stdout == first.stdout


@needs_bash
def test_sync_replaces_a_stale_line_in_place(tmp_path):
    stale = GENERIC.replace("local   all             all",
                            "local all hyperchat_migrator,hyperchat_app peer map=hyperchat\nlocal   all             all")
    out = sync(tmp_path, stale)
    assert out.returncode == 0 and WANT in out.stdout and "hyperchat_migrator,hyperchat_app peer" not in out.stdout
    assert len(out.stdout.splitlines()) == len(stale.splitlines())


@needs_bash
@pytest.mark.parametrize("text", ["host all all 127.0.0.1/32 scram-sha-256\n",  # nowhere to insert
                                  "local all a peer map=hyperchat\nlocal all b peer map=hyperchat\n"])  # ambiguous
def test_sync_refuses_files_it_cannot_place_the_line_in(tmp_path, text):
    out = sync(tmp_path, text)
    assert out.returncode == 1 and out.stdout == ""


# ── check_isolation.sh ──────────────────────────────────────────────────────────

def fake_dev(root: Path) -> None:
    """A correct DEV layout under `root`, owned by the test user (the audit's owner/group are overridden)."""
    release = root / "opt/hyperchat/releases/abc123"
    (release / "backend/app").mkdir(parents=True)
    (release / "backend/app/main.py").write_text("")
    (root / "opt/hyperchat/current").symlink_to(release)
    (root / "etc/hyperchat").mkdir(parents=True)
    (root / "etc/hyperchat/api.env").write_text("HYPERCHAT_APP_ENV=development\n")
    (root / "etc/systemd/system").mkdir(parents=True)
    for name in ("hyperchat-api", "hyperchat-worker"):
        shutil.copy(DEPLOY / f"systemd/{name}.service", root / f"etc/systemd/system/{name}.service")
    pg = root / "etc/postgresql/15/restoretest"
    pg.mkdir(parents=True)
    (pg / "pg_hba.conf").write_text(WANT + "\n" + GENERIC)
    (pg / "pg_ident.conf").write_text(read("postgres/pg_ident.conf"))
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(0o750 if path.is_dir() else 0o640)


def audit(root: Path) -> subprocess.CompletedProcess:
    import grp
    import pwd
    env = {**os.environ, "HYPERCHAT_CHECK_PREFIX": str(root), "HYPERCHAT_CHECK_OWNER": pwd.getpwuid(os.getuid()).pw_name,
           "HYPERCHAT_CHECK_GROUP": grp.getgrgid(os.getgid()).gr_name}
    return subprocess.run(["bash", str(DEPLOY / "check_isolation.sh"), "dev"], capture_output=True, text=True, env=env)


@needs_bash
def test_the_audit_passes_a_correct_layout(tmp_path):
    fake_dev(tmp_path)
    out = audit(tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "FAIL" not in out.stdout and "pg_ident maps hyperchat to" in out.stdout


def _group_writable_code(root):
    (root / "opt/hyperchat/releases/abc123/backend/app/main.py").chmod(0o660)


def _world_readable_code(root):
    (root / "opt/hyperchat/releases/abc123/backend/app/main.py").chmod(0o644)


def _current_outside_releases(root):
    (root / "opt/hyperchat/current").unlink()
    (root / "opt/hyperchat/current").symlink_to(root / "etc")


def _world_readable_env(root):
    (root / "etc/hyperchat/api.env").chmod(0o644)


def _unit_as_root(root):
    path = root / "etc/systemd/system/hyperchat-worker.service"
    path.write_text(path.read_text().replace("User=hyperchat", "User=root"))


def _hba_missing_auth(root):
    path = root / "etc/postgresql/15/restoretest/pg_hba.conf"
    path.write_text(path.read_text().replace(",hyperchat_auth", ""))


def _ident_extra_role(root):
    path = root / "etc/postgresql/15/restoretest/pg_ident.conf"
    path.write_text(path.read_text() + "hyperchat   hyperchat         hyperchat_migrator\n")


def _ident_maps_prod_user(root):
    path = root / "etc/postgresql/15/restoretest/pg_ident.conf"
    path.write_text(path.read_text() + "hyperchat   hyperchat-prod    hyperchat_app\n")


@needs_bash
@pytest.mark.parametrize("break_it,expected", [
    (_group_writable_code, "release tree"), (_world_readable_code, "release tree"),
    (_current_outside_releases, "current does not point"), (_world_readable_env, "api.env is"),
    (_unit_as_root, "hyperchat-worker runs as 'root'"), (_hba_missing_auth, "missing: hyperchat_auth"),
    (_ident_extra_role, "pg_ident maps hyperchat to"), (_ident_maps_prod_user, "never maps hyperchat-prod"),
])
def test_the_audit_fails_each_broken_boundary(tmp_path, break_it, expected):
    fake_dev(tmp_path)
    break_it(tmp_path)
    out = audit(tmp_path)
    assert out.returncode == 1
    assert any(line.startswith("FAIL") and expected in line for line in out.stdout.splitlines()), out.stdout
