"""PAY-15 6a-2 Phase 2B guard helpers (pure, no database): the cluster-identity and exclusive-cluster decisions, the
S8 quarantine marker and the single restore-and-verify path. Moved byte-for-byte from test_verify_db.py so the unit
tests (test_verify_rev7_units.py) can import them without the database test module. No behaviour change."""
import os

import pytest

# ── safeguards (plan §3.2 steps 3-4; P-3: inside this file, never tests/conftest.py) ────────────────────────────────

APPROVED_PORTS = frozenset({"55445", "55447", "55455", "55457"})  # E15, E17, X15, X17
EXCLUSIVE_PORTS = frozenset({"55455", "55457"})                   # X15, X17 (DB-X only)
PRE_EXISTING_PORTS = frozenset({"55415", "55417", "55435", "55437"})  # plan §0: outside Phase 2B, never used
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
EXPECT_KEYS = ("sysid", "port", "datadir", "version_num")
NEVER_PRESENT_DATABASES = frozenset({"hyperchat", "hyperchat_dev", "hyperchat_restore_test"})  # PROD / DEV / restore
ROLES_SQL = frozenset({"hyperchat_owner", "hyperchat_migrator", "hyperchat_app", "hyperchat_worker",
                       "hyperchat_readonly", "hyperchat_lifecycle", "hyperchat_auth", "hyperchat_stepup"})

# ── CI database execution: DISABLED (A1 Correction 2; owner direction 2026-11-17) ────────────────────────────────────
# CI_DB_EXECUTION_AUTHORIZED is False: in CI mode the guard always refuses, whatever evidence is present. Nothing
# here enables CI database execution, and ci.yml / setup-test-db.sh are unchanged (the current workflow sets none of
# the CI variables, so the module also refuses for the plain reason that no Phase 2B expectations exist).
#
# Trust boundary, stated plainly. The comparison logic below (I-1 to I-4) checks the live server against an identity
# capture (system_identifier, data_directory, server_version_num, port, plus a nonce for freshness only). That
# capture would be a JSON file written by a workflow step and read by THIS process; it is owned by the same OS user
# that runs pytest, in a directory that user can write, and the same process holds a superuser DSN. So it can only
# DETECT ACCIDENTAL MISROUTING (a wrong port, host, image or leftover server); it is NOT tamper-resistant and is NOT a
# security boundary against repository-controlled code (a test module, plugin or import hook can rewrite the file,
# the environment and the server comment before this guard runs). Resisting a malicious or compromised test process
# needs a different, separately authorized design: a trusted launcher OUTSIDE pytest that verifies the service and
# then runs pytest in a network-isolated namespace whose only reachable PostgreSQL is a disposable service with no
# DEV / production credentials or routes (see the A1 Correction 2 report). Which threat model CI must meet is an
# owner decision (OD-14); until it is taken, CI stays disabled and no CI security acceptance is claimed.
#
# Comparison logic kept (tested offline) for that future decision:
#   I-1  capture (HYPERCHAT_TEST_CI_IDENTITY_FILE): {"source": "hyperchat-ci-provisioning-v1", "sysid", "datadir",
#        "version_num", "port": "5432", "nonce"}
#   I-2  the server's system_identifier, data_directory, server_version_num and port equal the capture EXACTLY, and
#        the major equals HYPERCHAT_TEST_CI_PG_MAJOR
#   I-3  freshness only: hyperchat_ci's comment is 'hyperchat-ci-disposable:<nonce>' and HYPERCHAT_TEST_CI_MARKER
#        equals the capture's nonce
#   I-4  no hyperchat / hyperchat_dev / hyperchat_restore_test; no login role outside postgres, roles.sql and
#        authtest_; admin = superuser postgres; hyperchat_readonly the roles.sql test-only role
# CI mode never enables DB-X and is refused on any port but 5432. The local four-cluster policy is unaffected.
CI_DB_EXECUTION_AUTHORIZED = False
CI_DISABLED = ("CI database execution is disabled (A1 Correction 2): a same-user identity capture only detects "
               "accidental misrouting and is not a boundary against test-process code; owner decision OD-14 pending")
CI_MARKER_PREFIX = "hyperchat-ci-disposable:"
CI_PORT = "5432"
CI_CAPTURE_SOURCE = "hyperchat-ci-provisioning-v1"
CI_CAPTURE_KEYS = ("source", "sysid", "datadir", "version_num", "port", "nonce")


def _norm_dir(p: str) -> str:
    return os.path.normcase(os.path.normpath(p or ""))


def _ci_mode(env) -> bool:
    return env.get("HYPERCHAT_TEST_CI_IDENTITY") == "1"


def _common_problems(ident: dict, env, admin: dict) -> list[str]:
    out = []
    port, host = str(ident.get("port")), admin.get("host", "")
    if port in PRE_EXISTING_PORTS or str(admin.get("port", "")) in PRE_EXISTING_PORTS:
        out.append(f"port {port} belongs to a pre-existing cluster (plan §0): never used")
    if host not in LOOPBACK:
        out.append(f"admin host {host!r} is not loopback (a socket directory or remote host is refused)")
    target_port = str(env.get("HYPERCHAT_TEST_AUTH_DB", ":").split(":")[-1])
    if target_port != port:
        out.append(f"HYPERCHAT_TEST_AUTH_DB port {target_port} differs from the admin connection's port {port}")
    if env.get("HYPERCHAT_TEST_PG_HOST", "") not in LOOPBACK:
        out.append("HYPERCHAT_TEST_PG_HOST must be loopback")
    if NEVER_PRESENT_DATABASES & set(ident.get("databases") or ()):
        out.append("the server holds a production / DEV / restore database: never a test cluster")
    return out


def _hex_nonce(v) -> bool:
    return isinstance(v, str) and len(v) >= 32 and all(ch in "0123456789abcdef" for ch in v)


def _ci_problems(ident: dict, env, admin: dict, capture) -> list[str]:
    """I-1 to I-4: the server's own facts against the provisioning capture; the nonce only as freshness. Always
    refused while CI database execution is not authorized (CI_DB_EXECUTION_AUTHORIZED)."""
    out = [] if CI_DB_EXECUTION_AUTHORIZED else [CI_DISABLED]
    if not isinstance(capture, dict):
        return out + ["CI: no provisioning identity capture (HYPERCHAT_TEST_CI_IDENTITY_FILE): identity not "
                      "established (I-1); environment declarations alone never establish it"]
    missing = [k for k in CI_CAPTURE_KEYS if not isinstance(capture.get(k), str) or not capture.get(k)]
    if missing or capture.get("source") != CI_CAPTURE_SOURCE:
        return out + [f"CI: the identity capture is incomplete or not from the provisioning step ({missing})"]
    for k in ("sysid", "version_num", "port"):
        if str(ident.get(k)) != capture[k]:
            out.append(f"CI: server {k} {ident.get(k)!r} differs from the provisioning capture {capture[k]!r} (I-2)")
    if str(ident.get("datadir")) != capture["datadir"]:
        out.append(f"CI: server data_directory {ident.get('datadir')!r} differs from the capture (I-2)")
    if capture["port"] != CI_PORT or str(ident.get("port")) != CI_PORT:
        out.append(f"CI: port {ident.get('port')} is not the CI service port {CI_PORT}")
    major = env.get("HYPERCHAT_TEST_CI_PG_MAJOR", "")
    if major not in ("15", "17") or capture["version_num"][:2] != major:
        out.append(f"CI: the captured version {capture['version_num']!r} is not the declared major {major!r}")
    if env.get("HYPERCHAT_TEST_AUTH_DB", "") != f"hyperchat_ci:{CI_PORT}":
        out.append("CI: HYPERCHAT_TEST_AUTH_DB must be hyperchat_ci:5432")
    nonce = capture["nonce"]
    if not _hex_nonce(nonce) or env.get("HYPERCHAT_TEST_CI_MARKER") != nonce \
            or ident.get("ci_marker") != CI_MARKER_PREFIX + nonce:
        out.append("CI: the freshness marker does not match this run's capture (replayed, foreign or missing; I-3)")
    if ident.get("admin_user") != "postgres" or ident.get("admin_super") is not True:
        out.append("CI: the admin connection must be the superuser postgres")
    unexpected = sorted(r for r in ident.get("login_roles") or ()
                        if r != "postgres" and r not in ROLES_SQL and not r.startswith("authtest_"))
    if unexpected:
        out.append(f"CI: unexpected login roles {unexpected}: not a freshly provisioned CI server")
    ro = ident.get("readonly") or {}
    if (ro.get("super"), ro.get("bypassrls"), ro.get("read_only_default")) != (False, False, True):
        out.append("CI: hyperchat_readonly is not the roles.sql test-only role")
    if any(env.get(f"HYPERCHAT_TEST_EXPECT_{k.upper()}") is not None for k in EXPECT_KEYS):
        out.append("CI mode and the Phase 2B HYPERCHAT_TEST_EXPECT_* expectations are mutually exclusive")
    return out


def cluster_identity_problems(ident: dict, env, admin: dict, ci_capture=None) -> list[str]:
    """Pure: why the connected cluster is NOT an approved test cluster (empty = approved). Two modes only:
    (1) a Phase 2B cluster (E15 / E17 / X15 / X17) proven by the four HYPERCHAT_TEST_EXPECT_* values;
    (2) CI, only with HYPERCHAT_TEST_CI_IDENTITY=1 AND the server's facts equal to the provisioning capture (I-1
    to I-4).
    Anything else is refused. GITHUB_ACTIONS is never consulted."""
    out = _common_problems(ident, env, admin)
    if _ci_mode(env):
        return out + _ci_problems(ident, env, admin, ci_capture)
    expected = {k: env.get(f"HYPERCHAT_TEST_EXPECT_{k.upper()}") for k in EXPECT_KEYS}
    if any(v is None for v in expected.values()):
        out.append("HYPERCHAT_TEST_EXPECT_SYSID / _PORT / _DATADIR / _VERSION_NUM must all be set (or the CI identity "
                   "contract must be met): no fallback")
        return out
    if str(ident.get("port")) not in APPROVED_PORTS:
        out.append(f"port {ident.get('port')} is not an approved Phase 2B cluster port")
    for k in ("sysid", "port", "version_num"):
        if str(ident.get(k)) != str(expected[k]):
            out.append(f"{k}: server reports {ident.get(k)!r}, expected {expected[k]!r}")
    if _norm_dir(ident.get("datadir")) != _norm_dir(expected["datadir"]):
        out.append(f"datadir: server reports {ident.get('datadir')!r}, expected {expected['datadir']!r}")
    return out


def exclusive_decision(ident: dict | None, env) -> str:
    """Pure (plan §3.2 step 4): 'skip' unless DB-X is requested; 'run' only on X15 / X17 with expectations set;
    'fail' when DB-X is requested anywhere else, and always in CI mode (CI never mutates existing roles)."""
    if env.get("HYPERCHAT_TEST_EXCLUSIVE_CLUSTER") != "1":
        return "skip"
    if _ci_mode(env) or ident is None or str(ident.get("port")) not in EXCLUSIVE_PORTS \
            or not env.get("HYPERCHAT_TEST_EXPECT_SYSID"):
        return "fail"
    return "run"


QUARANTINE_ENV = "HYPERCHAT_TEST_QUARANTINE_DIR"


def quarantine_marker(directory: str, ident: dict) -> str:
    return os.path.join(directory, f"X-{ident.get('port')}-{ident.get('sysid')}.quarantined")


def quarantine_refusal(ident: dict, env) -> str | None:
    """Pure (S8): DB-X needs a quarantine directory, and a cluster with a quarantine marker is never used again."""
    qdir = env.get(QUARANTINE_ENV)
    if not qdir:
        return f"DB-X refused: {QUARANTINE_ENV} must name the quarantine directory (S8)"
    if os.path.exists(quarantine_marker(qdir, ident)):
        return "DB-X refused: this exclusive cluster is QUARANTINED (S8); provision a new one under A7a"
    return None


def restore_and_verify(before, restore, *, connect, read_state, ident, quarantine_dir, exit_=None):
    """S8 (Rev 7 §5.8): run the restore statements (in reverse), then re-read the role state from a NEW session and
    compare. EVERY failure - opening the restore connection, a restore statement, opening the verification session,
    the state query, or a differing state - goes through this one path: the cluster is marked QUARANTINED (a marker
    file that later DB-X runs refuse) and the whole session aborts with exit code 3, carrying the original exception.
    Nothing is swallowed."""
    import traceback
    exit_ = exit_ or pytest.exit
    try:
        with connect() as c:
            for stmt in reversed(restore):
                c.execute(stmt)
        after = [row for row in read_state() if "authtest_" not in row[1]]
        expected = [row for row in before if "authtest_" not in row[1]]
        if after != expected:
            raise AssertionError(f"post-restore role state differs from the pre-state: before={expected!r} "
                                 f"after={after!r}")
    except BaseException as exc:  # noqa: BLE001 - every failure takes the S8 path, by design
        detail = "".join(traceback.format_exception(exc))
        marker = quarantine_marker(quarantine_dir, ident)
        try:
            os.makedirs(quarantine_dir, exist_ok=True)
            with open(marker, "a", encoding="utf-8") as f:
                f.write(f"QUARANTINED: port {ident.get('port')} sysid {ident.get('sysid')}\n{detail}\n")
        except OSError as werr:
            detail += f"\n(the quarantine marker could not be written: {werr!r})"
        exit_(f"S8: DB-X restoration failed or unverifiable; exclusive cluster port {ident.get('port')} sysid "
              f"{ident.get('sysid')} is QUARANTINED ({marker}). Original error:\n{detail}", returncode=3)
