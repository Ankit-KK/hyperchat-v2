"""Restore verification of one backup set into a DISPOSABLE PostgreSQL (docs/backup-recovery.md).

    python -m app.backup.restore_check --dir D --set BASE --pg-bin /usr/lib/postgresql/17/bin --scratch
        [--expect-head 0028] [--witness FILE] [--output result.json]
    python -m app.backup.restore_check ... --server "host=127.0.0.1 port=55432 user=postgres" --disposable-server

  --scratch   initdb a private cluster in a temporary directory (Unix socket there, no TCP), restore, check, stop and
              delete it. As root, add --scratch-user postgres: the cluster's processes run as that user.
  --server    restore into a NEW database hc_restorecheck_<random> on an existing server that the caller declares
              disposable (--disposable-server); the database is dropped afterwards. Refused for the live clusters'
              socket directory and for the live database names.

Three separate answers, never conflated:
  structurally_valid  backup_valid (the set is complete, its files match their SHA-256, the archive is readable) AND
                      restore_valid (it restored completely into the same PostgreSQL major version, at the expected
                      migration head, with the security invariants intact: ownership, guard triggers, runtime-role
                      privileges, enforcement rows)
  kyc_witness         consistent | not_applicable (no KYC history to protect) | inconsistent (restore_suspected: the
                      EXPECTED result for an older backup; new donations stay blocked) | missing (no usable witness
                      for a database with KYC history) | not_checked -- judged against the newest witness copy given,
                      exactly as the API would
  live_operation_approved   always false: this tool never approves resuming live operation, never acknowledges and
                      never replaces a witness. live_operation_blockers lists what the operator must still clear
                      (docs/backup-recovery.md).
Output: one JSON object. Exit 0: structurally valid and the witness consistent / not applicable; 3: structurally valid
but the witness inconsistent, missing or not checked; 1: not structurally valid (or the check failed).
"""

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import psycopg

from app.backup.sets import BackupSet, SetError, load
from app.kyc.witness import KycWitness

REPO = Path(__file__).resolve().parents[3]
LIVE_SOCKET_DIRS = ("/var/run/postgresql", "/run/postgresql")
LIVE_DATABASES = {"hyperchat", "hyperchat_dev", "hyperchat_restore_test", "postgres", "template0", "template1"}
RUNTIME_ROLES = ("hyperchat_app", "hyperchat_worker", "hyperchat_auth", "hyperchat_lifecycle", "hyperchat_readonly",
                 "hyperchat_stepup")
# Owner / operator-only functions no runtime role may execute (checked when present in the restored schema).
OWNER_ONLY_FUNCTIONS = ("public.stepup_enforcement_draft(text, text)",
                        "public.stepup_approve_credential(text, text, text, text)",
                        "public.stepup_revoke_credential(text, text, text, text)",
                        "public.stepup_recover_admin(text, text, text)",
                        "public.admin_link_google_identity(text, text, text, text, text, jsonb)",
                        "public.stepup_claim(uuid, uuid, text, text, text, text)",
                        "public.stepup_authorize(text, text, jsonb, text)")
# Tables whose writes belong to one dedicated role or the owner only.
NO_RUNTIME_WRITES = ("stepup_grants", "stepup_events", "stepup_enforcement", "webauthn_credentials",
                     "kyc_enforcement", "kyc_enforcement_events")


class CheckError(Exception):
    """A refused or failed verification step (message: a reason code)."""


def _major(text: str) -> int:
    match = re.search(r"(\d+)(?:\.\d+)?", text)
    if not match:
        raise CheckError("unknown_version")
    return int(match.group(1))


def tool_major(pg_bin: Path, tool: str) -> int:
    exe = pg_bin / tool
    if not exe.is_file():
        raise CheckError(f"{tool}_not_found")
    return _major(subprocess.run([str(exe), "--version"], capture_output=True, text=True, check=True).stdout)


def expected_head() -> str:
    """The newest Alembic revision in this checkout (the release being verified against)."""
    revisions = sorted(p.name.split("_", 1)[0] for p in (REPO / "database/migrations/versions").glob("[0-9]*_*.py"))
    if not revisions:
        raise CheckError("no_migrations_found")
    return revisions[-1]


# ── disposable targets ─────────────────────────────────────────────────────────────────────────────────────

@contextmanager
def scratch_cluster(pg_bin: Path, scratch_user: str | None = None):
    """A private cluster: temp data dir, Unix socket in it, no TCP. Stopped and deleted whatever happens. PostgreSQL
    refuses to run as root: as root, its processes run as `scratch_user` (e.g. postgres, never root)."""
    as_root = hasattr(os, "geteuid") and os.geteuid() == 0
    if as_root and (not scratch_user or scratch_user == "root"):
        raise CheckError("scratch_cluster_needs_--scratch-user_when_run_as_root")
    prefix = ["runuser", "-u", scratch_user, "--"] if as_root else []
    work = Path(tempfile.mkdtemp(prefix="hc-restorecheck-"))
    data, sock = work / "data", work / "sock"
    sock.mkdir()
    if as_root:
        shutil.chown(work, scratch_user)
        shutil.chown(sock, scratch_user)
    started = False
    try:
        subprocess.run([*prefix, str(pg_bin / "initdb"), "-D", str(data), "-A", "trust", "-U", "postgres",
                        "--no-sync"], capture_output=True, check=True)
        with socket.socket() as s:  # a port number for the socket file name only (listen_addresses is empty)
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        subprocess.run([*prefix, str(pg_bin / "pg_ctl"), "-D", str(data), "-w", "-l", str(data / "server.log"), "-o",
                        f"-p {port} -k {sock} -c listen_addresses='' -c fsync=off", "start"],
                       capture_output=True, check=True)
        started = True
        yield {"host": str(sock), "port": port, "user": "postgres", "mode": "scratch"}
    finally:
        if started:
            subprocess.run([*prefix, str(pg_bin / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
                           capture_output=True)
        shutil.rmtree(work, ignore_errors=True)


def server_target(dsn: str) -> dict:
    params = psycopg.conninfo.conninfo_to_dict(dsn)
    host = str(params.get("host") or "/var/run/postgresql")
    if any(host.rstrip("/") == d for d in LIVE_SOCKET_DIRS):
        raise CheckError("refused_live_socket_directory")
    if params.get("dbname") and params["dbname"] in LIVE_DATABASES - {"postgres"}:
        raise CheckError("refused_live_database")
    return {"host": host, "port": int(params.get("port") or 5432), "user": params.get("user") or "postgres",
            "mode": "server"}


def _connect(target: dict, dbname: str, **kw) -> psycopg.Connection:
    return psycopg.connect(host=target["host"], port=target["port"], user=target["user"], dbname=dbname, **kw)


def _psql_file(pg_bin: Path, target: dict, dbname: str, sql_file: Path) -> None:
    subprocess.run([str(pg_bin / "psql"), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-h", target["host"], "-p",
                    str(target["port"]), "-U", target["user"], "-d", dbname, "-f", str(sql_file)],
                   capture_output=True, check=True)


# ── the checks ─────────────────────────────────────────────────────────────────────────────────────────────

def invariants(conn: psycopg.Connection, bset: BackupSet, head: str | None) -> dict:
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    out: dict = {}
    version = q("SELECT version_num FROM public.alembic_version")
    out["alembic_version"] = version
    out["alembic_matches_manifest"] = version == bset.manifest.get("alembic_version")
    out["alembic_matches_expected_head"] = head is None or version == head
    out["objects_not_owned_by_hyperchat_owner"] = q("""
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'S', 'm', 'p')
          AND pg_get_userbyid(c.relowner) <> 'hyperchat_owner'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e')""") + q("""
        SELECT count(*) FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace
          AND pg_get_userbyid(p.proowner) <> 'hyperchat_owner'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')""")
    out["public_table_privileges"] = q("""
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a
        WHERE n.nspname = 'public' AND c.relkind = 'r' AND a.grantee = 0""")
    guards = conn.execute("""SELECT count(*) FILTER (WHERE tgenabled <> 'D'), count(*) FILTER (WHERE tgenabled = 'D')
                             FROM pg_trigger WHERE NOT tgisinternal AND tgname ~ '^aa[abc]_'""").fetchone()
    out["guard_triggers_enabled"], out["guard_triggers_disabled"] = guards
    roles = {r[0] for r in conn.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(RUNTIME_ROLES),))}
    leaks = []
    for fn in OWNER_ONLY_FUNCTIONS:
        if q("SELECT to_regprocedure(%s) IS NOT NULL", fn):
            leaks += [f"{role} EXECUTE {fn}" for role in sorted(roles)
                      if q("SELECT has_function_privilege(%s, %s, 'EXECUTE')", role, fn)]
    for table in NO_RUNTIME_WRITES:
        if q("SELECT to_regclass(%s) IS NOT NULL", f"public.{table}"):
            leaks += [f"{role} {priv} {table}" for role in sorted(roles) for priv in ("UPDATE", "DELETE", "TRUNCATE")
                      if q("SELECT has_table_privilege(%s, %s, %s)", role, f"public.{table}", priv)]
    out["runtime_role_privilege_leaks"] = leaks
    out["runtime_roles_present"] = sorted(roles)
    if q("SELECT to_regclass('public.stepup_enforcement') IS NOT NULL"):
        out["stepup_enforcement"] = q("SELECT state FROM public.stepup_enforcement")
    if q("SELECT to_regclass('public.kyc_enforcement') IS NOT NULL"):
        state, generation = conn.execute("SELECT state, generation FROM public.kyc_enforcement").fetchone()
        out["kyc_enforcement"] = {"state": state, "generation": generation}
        recorded = (bset.manifest.get("kyc_enforcement") or {}).get("generation", 0)
        out["kyc_generation_not_behind_manifest"] = generation >= recorded  # the dump's snapshot is not older
    out["row_counts"] = {t: q(f"SELECT count(*) FROM public.{t}")
                         for t in ("users", "streamers", "donations") if q("SELECT to_regclass(%s)", f"public.{t}")}
    out["ok"] = (out["alembic_matches_manifest"] and out["alembic_matches_expected_head"]
                 and out["objects_not_owned_by_hyperchat_owner"] == 0 and out["public_table_privileges"] == 0
                 and out["guard_triggers_disabled"] == 0 and not leaks
                 and out.get("kyc_generation_not_behind_manifest", True)
                 and out.get("stepup_enforcement", "draft") in ("draft", "active"))
    return out


def witness_status(conn: psycopg.Connection, witness: Path | None) -> dict:
    """How the restored database compares with the given (newest) witness copy, exactly as the API would judge it."""
    if conn.execute("SELECT to_regclass('public.kyc_enforcement') IS NULL").fetchone()[0]:
        return {"checked": False, "verdict": "not_applicable", "reason": "no_kyc_enforcement_table"}
    w = KycWitness(str(witness) if witness else None)
    state, generation, anchor_chain = conn.execute(
        "SELECT e.state, e.generation, (SELECT ev.chain FROM public.kyc_enforcement_events ev WHERE ev.generation = %s)"
        " FROM public.kyc_enforcement e", (w.anchor_generation(),)).fetchone()
    problem = w.problem(state, generation, anchor_chain)
    if problem is None:
        verdict = "consistent" if witness else "not_applicable"  # without a witness only the untouched draft passes
    else:
        verdict = "inconsistent" if problem == "restore_suspected" else "missing"
    return {"checked": True, "verdict": verdict, "problem": problem, "database_generation": generation,
            "witness_generation": w.anchor_generation(),
            "meaning": ("consistent: new donations allowed" if problem is None else
                        "new donations stay blocked until the operator re-applies lost changes and runs "
                        "acknowledge-restore (never automatic)")}


def run(args: argparse.Namespace) -> dict:
    result: dict = {"set": args.set, "backup_valid": False, "restore_valid": False}
    pg_bin = Path(args.pg_bin)
    try:
        bset = load(args.dir, args.set, expect_env=args.env)
        result.update(environment=bset.environment, database=bset.database,
                      source_server_version_num=bset.manifest.get("server_version_num"))
        source_major = int(str(bset.manifest.get("server_version_num", "0"))[:2])
        if tool_major(pg_bin, "pg_restore") != source_major:
            raise CheckError("pg_restore_major_differs_from_the_source")
        listing = subprocess.run([str(pg_bin / "pg_restore"), "-l", str(bset.dump)], capture_output=True, text=True)
        if listing.returncode != 0:
            raise CheckError("archive_unreadable")
        entries = sum(1 for line in listing.stdout.splitlines() if line and not line.startswith(";"))
        if entries != bset.manifest["dump"].get("toc_entries"):
            raise CheckError("archive_toc_differs_from_manifest")
        result["toc_entries"] = entries
        result["backup_valid"] = True
    except (SetError, CheckError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        result["error"] = str(exc) or type(exc).__name__
        return result

    head = None if args.expect_head == "none" else (args.expect_head or expected_head())
    if args.scratch:
        if not Path(args.roles_sql).is_file():  # a server release has no deploy/: --roles-sql is required there
            result["error"] = "roles_sql_not_found"
            return result
        target_cm = scratch_cluster(pg_bin, args.scratch_user)
    else:
        if not args.disposable_server:
            result["error"] = "server_mode_needs_--disposable-server"
            return result
        target_cm = _server(server_target(args.server))
    try:
        with target_cm as target:
            dbname = f"hc_restorecheck_{secrets.token_hex(6)}"
            result["target"] = {"mode": target["mode"]}
            with _connect(target, "postgres", autocommit=True) as admin:
                target_major = _major(admin.execute("SHOW server_version").fetchone()[0])
                if target_major != source_major:
                    raise CheckError("target_major_differs_from_the_source")
                if target["mode"] == "scratch":
                    _psql_file(pg_bin, target, "postgres", Path(args.roles_sql))
                admin.execute(f'CREATE DATABASE "{dbname}" OWNER hyperchat_owner')
            try:
                started = time.monotonic()
                restore = subprocess.run([str(pg_bin / "pg_restore"), "--exit-on-error", "--single-transaction",
                                          "-h", target["host"], "-p", str(target["port"]), "-U", target["user"],
                                          "-d", dbname, str(bset.dump)], capture_output=True, text=True)
                result["restore_seconds"] = round(time.monotonic() - started, 1)
                if restore.returncode != 0:
                    raise CheckError("restore_failed")
                with _connect(target, dbname) as conn:
                    result["invariants"] = invariants(conn, bset, head)
                    witness = Path(args.witness) if args.witness else bset.witness
                    result["witness"] = witness_status(conn, witness)
                result["restore_valid"] = bool(result["invariants"]["ok"])
            finally:
                with _connect(target, "postgres", autocommit=True) as admin:
                    admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    except (CheckError, OSError, subprocess.CalledProcessError, psycopg.Error) as exc:
        result["error"] = str(exc) if isinstance(exc, CheckError) else type(exc).__name__
    return result


@contextmanager
def _server(target: dict):
    yield target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backup.restore_check", description=__doc__.splitlines()[0])
    parser.add_argument("--dir", required=True)
    parser.add_argument("--set", required=True)
    parser.add_argument("--env", choices=["prod", "dev", "test"], help="refuse a set of another environment")
    parser.add_argument("--pg-bin", required=True, help="the target PostgreSQL major's bin directory")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scratch", action="store_true")
    mode.add_argument("--server")
    parser.add_argument("--disposable-server", action="store_true")
    parser.add_argument("--scratch-user", help="as root: the unprivileged user the scratch cluster runs as (postgres)")
    parser.add_argument("--expect-head", help="Alembic head the restore must be at (default: this checkout's; "
                                              "'none' to skip)")
    parser.add_argument("--witness", help="the NEWEST witness copy (app.backup.witness select); default: the set's own")
    parser.add_argument("--roles-sql", default=str(REPO / "deploy/postgres/roles.sql"))
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    result = run(args)
    structural = bool(result["backup_valid"] and result["restore_valid"])
    verdict = result.get("witness", {}).get("verdict", "not_checked")
    result["structurally_valid"] = structural
    result["kyc_witness"] = verdict
    result["live_operation_approved"] = False  # never decided here (see the docstring)
    result["live_operation_blockers"] = (
        ([] if structural else ["not_structurally_valid"])
        + ([] if verdict in ("consistent", "not_applicable") else [f"kyc_witness_{verdict}"])
        + ["external_reconciliation_required", "owner_approval_required"])
    text = json.dumps(result, default=str, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    if not structural:
        return 1
    return 0 if verdict in ("consistent", "not_applicable") else 3


if __name__ == "__main__":
    sys.exit(main())
