"""backend/requirements.lock (security audit run-4, NV4-7): the release venv's complete hash lock. It pins exactly the
distributions of requirements.txt, every one with SHA-256 hashes, and every documented or scripted install of a hash
lock is binary-only (no sdist build, so nothing unhashed is fetched or run at install time)."""

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
LOCK = REPO / "backend/requirements.lock"
FLAGS = ("--isolated", "--require-hashes", "--no-deps", "--only-binary :all:")


def _name(raw: str) -> str:
    return re.sub(r"[-_.]+", "-", raw).lower()


def pins(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;#\\]+)", line)
        if m:
            out[_name(m[1])] = m[2]
    return out


def test_the_lock_pins_exactly_the_requirements_and_hashes_every_entry():
    required, locked = pins(REPO / "backend/requirements.txt"), pins(LOCK)
    assert locked == required  # regenerate with the command in the lock's header after changing requirements.txt
    entries = re.split(r"\n(?=[A-Za-z0-9])", LOCK.read_text(encoding="utf-8").split("\n", 2)[2])
    for entry in entries:
        if entry.strip():
            assert re.search(r"--hash=sha256:[0-9a-f]{64}", entry), entry.splitlines()[0]
    assert "opentelemetry-api" in required  # the distribution the DEV venv had beyond the pins (via fastapi)


def test_the_lock_was_resolved_wheels_only_for_the_servers_python():
    header = LOCK.read_text(encoding="utf-8").split("\n", 2)[1]
    for part in ("--only-binary :all:", "--generate-hashes", "--python-version 3.14", "x86_64-manylinux"):
        assert part in header, part


def test_every_lock_install_is_hash_checked_and_binary_only():
    readme = (REPO / "deploy/README.md").read_text(encoding="utf-8")
    installs = [line for line in readme.splitlines() if "pip install" in line and "backend/requirements" in line]
    assert installs and all("requirements.lock" in line and all(f in line for f in FLAGS) for line in installs)
    gate_b = (REPO / "deploy/pay15-6b/b_build.sh").read_text(encoding="utf-8")
    (lock_line,) = [line for line in gate_b.splitlines() if '--require-hashes' in line]
    assert "--only-binary :all:" in lock_line and "--isolated" in lock_line
    ci = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    run = "\n".join(str(step.get("run", "")) for step in ci["jobs"]["unit"]["steps"])
    (ci_line,) = [line for line in run.splitlines() if "requirements.lock" in line]
    assert all(f in ci_line for f in FLAGS) and re.search(r'lock-venv/bin/pip"? check', run)
