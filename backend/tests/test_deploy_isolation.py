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

GENERIC = ("# default\n"
           "local   all             postgres                                peer\n"
           "# TYPE  DATABASE        USER            ADDRESS                 METHOD\n"
           "local   all             all                                     peer\n"
           "host    all             all             127.0.0.1/32            scram-sha-256\n"
           "local   replication     all                                     peer\n")
GENERIC_RULE = "local   all             all                                     peer"
WANT = [line for line in read("postgres/pg_hba.conf").splitlines() if line and not line.startswith("#")][0]
STALE = "local all hyperchat_migrator,hyperchat_app peer map=hyperchat"


def sync(tmp_path: Path, current: str) -> subprocess.CompletedProcess:
    (tmp_path / "pg_hba.conf").write_text(current)
    return subprocess.run(["bash", str(DEPLOY / "postgres/sync_pg_hba.sh"), str(DEPLOY / "postgres/pg_hba.conf"),
                           str(tmp_path / "pg_hba.conf")], capture_output=True, text=True)


def without_hyperchat(text: str) -> list[str]:
    return [line for line in text.splitlines() if "map=hyperchat" not in line]


def placed_first(text: str) -> bool:
    """The HyperChat rule is the first local rule whose user column could match a HyperChat role."""
    for line in text.splitlines():
        fields = line.split("#")[0].split()
        if fields[:1] == ["local"] and (fields[2] == "all" or fields[2][0] in "+@" or "hyperchat_" in fields[2]):
            return line == WANT
    return False


@needs_bash
def test_sync_inserts_above_the_generic_rule_and_is_idempotent(tmp_path):
    first = sync(tmp_path, GENERIC)
    assert first.returncode == 0, first.stderr
    assert placed_first(first.stdout) and without_hyperchat(first.stdout) == GENERIC.splitlines()
    assert sync(tmp_path, first.stdout).stdout == first.stdout


@needs_bash
def test_sync_replaces_a_stale_rule_that_already_takes_effect_in_place(tmp_path):
    stale = GENERIC.replace("# TYPE", STALE + "\n# TYPE")  # above the generic rule, a comment in between
    out = sync(tmp_path, stale)
    assert out.returncode == 0 and placed_first(out.stdout) and STALE not in out.stdout
    assert out.stdout.splitlines() == stale.replace(STALE, WANT).splitlines()  # same position, nothing else touched


@needs_bash
@pytest.mark.parametrize("earlier", [None, "local all hyperchat_auth md5", "local all +staff peer",
                                     "local all @admins peer"])
def test_sync_moves_a_shadowed_rule_above_the_first_rule_that_could_match(tmp_path, earlier):
    shadowed = GENERIC if earlier is None else GENERIC.replace(GENERIC_RULE, earlier + "\n" + GENERIC_RULE)
    shadowed += STALE + "\n# trailing comment\n"  # below every rule that could match: PostgreSQL never uses it
    out = sync(tmp_path, shadowed)
    assert out.returncode == 0, out.stderr
    assert placed_first(out.stdout) and out.stdout.count("map=hyperchat") == 1
    assert out.stdout.splitlines().index(WANT) + 1 == out.stdout.splitlines().index(earlier or GENERIC_RULE)
    assert without_hyperchat(out.stdout) == without_hyperchat(shadowed)  # unrelated rules and comments preserved
    assert sync(tmp_path, out.stdout).stdout == out.stdout  # idempotent once moved


@needs_bash
@pytest.mark.parametrize("text", [
    "host all all 127.0.0.1/32 scram-sha-256\n",  # nowhere to place the rule
    GENERIC + STALE + "\n" + WANT + "\n",  # ambiguous: two HyperChat rules
    "include_dir conf.d\n" + GENERIC,  # rules this script cannot order
    GENERIC.replace(GENERIC_RULE, "local   all   \\\n   all   peer"),  # a continuation line
])
def test_sync_fails_closed_on_files_it_cannot_order(tmp_path, text):
    out = sync(tmp_path, text)
    assert out.returncode == 1 and out.stdout == "" and "pg_hba.conf" in out.stderr


# ── check_isolation.sh ──────────────────────────────────────────────────────────

RELEASE = "opt/hyperchat/releases/abc123"


def link(path: Path, target: Path) -> None:
    """A relative symlink (an absolute one would leave the fake root)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(os.path.relpath(target, path.parent))


def fake_dev(root: Path) -> None:
    """A correct DEV layout under `root`, owned by the test user (the audit's owner/group are overridden): a release
    with a venv whose python links outside the release, as `python -m venv` makes it, and `current`."""
    release = root / RELEASE
    (release / "backend/app").mkdir(parents=True)
    (release / "backend/app/main.py").write_text("")
    (root / "usr/bin").mkdir(parents=True)
    (root / "usr/bin/python3.14").write_text("")
    link(release / "venv/bin/python3.14", root / "usr/bin/python3.14")
    link(release / "venv/bin/python", release / "venv/bin/python3.14")
    link(root / "opt/hyperchat/current", release)
    (root / "etc/hyperchat").mkdir(parents=True)
    (root / "etc/hyperchat/api.env").write_text("HYPERCHAT_APP_ENV=development\n")
    (root / "etc/systemd/system").mkdir(parents=True)
    for name in ("hyperchat-api", "hyperchat-worker"):
        shutil.copy(DEPLOY / f"systemd/{name}.service", root / f"etc/systemd/system/{name}.service")
    pg = root / "etc/postgresql/15/restoretest"
    pg.mkdir(parents=True)
    (pg / "pg_hba.conf").write_text(GENERIC.replace("# TYPE", WANT + "\n# TYPE"))
    (pg / "pg_ident.conf").write_text(read("postgres/pg_ident.conf"))
    (root / "mountinfo").write_text("22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n")
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(0o750 if path.is_dir() else 0o640)


def audit(root: Path) -> subprocess.CompletedProcess:
    import grp
    import pwd
    env = {**os.environ, "HYPERCHAT_CHECK_PREFIX": str(root),
           "HYPERCHAT_CHECK_OWNER": pwd.getpwuid(os.getuid()).pw_name,
           "HYPERCHAT_CHECK_GROUP": grp.getgrgid(os.getgid()).gr_name,
           "HYPERCHAT_CHECK_MOUNTINFO": str(root / "mountinfo")}
    return subprocess.run(["bash", str(DEPLOY / "check_isolation.sh"), "dev"], capture_output=True, text=True, env=env)


@needs_bash
def test_the_audit_passes_a_correct_layout(tmp_path):
    fake_dev(tmp_path)
    out = audit(tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "FAIL" not in out.stdout and "pg_ident maps hyperchat to" in out.stdout
    assert "OK    every symlink under /opt/hyperchat resolves" in out.stdout
    assert "OK    current -> /opt/hyperchat/releases/abc123" in out.stdout


def _group_writable_code(root):
    (root / RELEASE / "backend/app/main.py").chmod(0o660)


def _world_readable_code(root):
    (root / RELEASE / "backend/app/main.py").chmod(0o644)


def _current_outside_releases(root):
    (root / "opt/hyperchat/current").unlink()
    link(root / "opt/hyperchat/current", root / "etc")


def _outside_file(root, mode: int) -> Path:
    (root / "srv").mkdir(exist_ok=True)
    (root / "srv/hook.py").write_text("")
    (root / "srv/hook.py").chmod(mode)
    return root / "srv/hook.py"


def _symlink_to_writable_file_outside(root):
    """A release file that is a link to a file anyone can write, outside the protected tree."""
    link(root / RELEASE / "backend/app/hook.py", _outside_file(root, 0o666))


def _symlink_to_group_writable_file_outside(root):
    link(root / RELEASE / "backend/app/hook.py", _outside_file(root, 0o664))  # writable by the service group


def _symlink_to_other_writable_only_file(root):
    link(root / RELEASE / "backend/app/hook.py", _outside_file(root, 0o602))  # writable by "other" only


def _symlink_into_writable_directory(root):
    """The target file looks safe, but its directory is writable, so it can be swapped."""
    (root / "srv/shared").mkdir(parents=True)
    (root / "srv/shared/plugin.py").write_text("")
    (root / "srv/shared/plugin.py").chmod(0o640)
    (root / "srv/shared").chmod(0o1777)  # sticky does not help: whoever creates the new file owns it
    link(root / RELEASE / "backend/plugins", root / "srv/shared")


def _venv_python_retargeted(root):
    (root / RELEASE / "venv/bin/python3.14").unlink()
    link(root / RELEASE / "venv/bin/python3.14", _outside_file(root, 0o666))


def _dangling_symlink(root):
    link(root / RELEASE / "backend/app/later.py", root / "srv/not-yet.py")


def _writable_ancestor(root):
    (root / "opt").chmod(0o777)  # the service user could move /opt/hyperchat away and plant its own


def _other_writable_ancestor(root):
    (root / "opt").chmod(0o757)  # writable by "other" only


def _mount_point_inside(root):
    with open(root / "mountinfo", "a") as f:
        f.write(f"40 22 0:50 / {root / RELEASE / 'backend'} rw - tmpfs tmpfs rw\n")


def _world_readable_env(root):
    (root / "etc/hyperchat/api.env").chmod(0o644)


def _unit_as_root(root):
    path = root / "etc/systemd/system/hyperchat-worker.service"
    path.write_text(path.read_text().replace("User=hyperchat", "User=root"))


def _hba(root) -> Path:
    return root / "etc/postgresql/15/restoretest/pg_hba.conf"


def _hba_missing_auth(root):
    _hba(root).write_text(_hba(root).read_text().replace(",hyperchat_auth", ""))


def _hba_rule_below_generic(root):
    _hba(root).write_text(GENERIC + WANT + "\n")


def _hba_shadowed_by_a_role_rule(root):
    _hba(root).write_text("local all hyperchat_auth md5\n" + _hba(root).read_text())


def _hba_two_rules(root):
    _hba(root).write_text(_hba(root).read_text() + STALE + "\n")


def _hba_include(root):
    _hba(root).write_text("include_dir conf.d\n" + _hba(root).read_text())


def _hba_continuation(root):
    _hba(root).write_text("local all \\\n  all peer\n" + _hba(root).read_text())


def _ident_extra_role(root):
    path = root / "etc/postgresql/15/restoretest/pg_ident.conf"
    path.write_text(path.read_text() + "hyperchat   hyperchat         hyperchat_migrator\n")


def _ident_maps_prod_user(root):
    path = root / "etc/postgresql/15/restoretest/pg_ident.conf"
    path.write_text(path.read_text() + "hyperchat   hyperchat-prod    hyperchat_app\n")


LINKS = "every symlink under /opt/hyperchat"
TAKES_EFFECT = "pg_hba HyperChat rule takes effect"


@needs_bash
@pytest.mark.parametrize("break_it,expected", [
    (_group_writable_code, "release tree"), (_world_readable_code, "release tree"),
    (_current_outside_releases, "current does not point"),
    (_symlink_to_writable_file_outside, LINKS), (_symlink_to_group_writable_file_outside, LINKS),
    (_symlink_to_other_writable_only_file, LINKS), (_symlink_into_writable_directory, LINKS),
    (_venv_python_retargeted, LINKS), (_dangling_symlink, LINKS),
    (_writable_ancestor, "no writable directory on the way to /opt/hyperchat"),
    (_other_writable_ancestor, "no writable directory on the way to /opt/hyperchat"),
    (_mount_point_inside, "no mount point inside"),
    (_world_readable_env, "api.env is"), (_unit_as_root, "hyperchat-worker runs as 'root'"),
    (_hba_missing_auth, "missing: hyperchat_auth"), (_hba_rule_below_generic, TAKES_EFFECT),
    (_hba_shadowed_by_a_role_rule, TAKES_EFFECT), (_hba_two_rules, TAKES_EFFECT), (_hba_include, TAKES_EFFECT),
    (_hba_continuation, TAKES_EFFECT),
    (_ident_extra_role, "pg_ident maps hyperchat to"), (_ident_maps_prod_user, "never maps hyperchat-prod"),
])
def test_the_audit_fails_each_broken_boundary(tmp_path, break_it, expected):
    fake_dev(tmp_path)
    break_it(tmp_path)
    out = audit(tmp_path)
    assert out.returncode == 1
    assert any(line.startswith("FAIL") and expected in line for line in out.stdout.splitlines()), out.stdout
