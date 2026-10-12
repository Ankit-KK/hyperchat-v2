"""Supabase Auth password-hash import into HyperChat V2 (decision D1, phase 2). DRY RUN BY DEFAULT.

Copies bcrypt hashes from the live Supabase auth.users straight into public.users.password_hash, in memory only (no
export file, nothing logged or printed), for accounts that match on BOTH auth.users.id = legacy_supabase_user_id AND
the exact top-level email, whose source hash is a bcrypt hash the V2 verifier accepts and whose target hash is still
empty. It never matches by email alone, never creates or deletes accounts, and changes no other column: roles,
creator ownership, activation and email verification stay as they are (email_confirmed_at is only counted).

Without --apply it only reads (READ ONLY transactions on both sides) and prints aggregate counts. --apply also needs a
typed confirmation, refuses on any blocker (duplicate mappings, unsupported hash formats), re-checks every planned row
under a row lock and writes all rows in ONE transaction, each conditional on the hash still being empty; any change
since the plan rolls everything back. --apply also needs --expected-fingerprint from the reviewed dry run and the D1
source freeze (docs/operations/d1-auth-freeze/) in place. The plan (freeze check, fingerprint, hashes) comes from one
source snapshot; after the confirmation the source is locked (SHARE on auth.users), verified again under the lock, and
kept locked until the target transaction has committed, so no source row can change between the verification and the
commit. Losing the lock before the commit rolls everything back; losing it in the last instant is reported (exit 4).
The lock is released right after. A privileged operator can still change the source after that; see that folder's
README for the remaining risk and the operator controls. Operator guide: database/README.md.
Run:  python -m etl.auth_hash_import
"""

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

# The bcrypt format the sign-in verifier accepts (cost 04-12, exact shape) lives in the backend, next to the verifier;
# load that one definition instead of keeping a copy here. Missing backend code is an ImportError: the tool won't run.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app.auth.bcrypt_format import is_supported_bcrypt  # noqa: E402

from etl.auth_readonly_report import (  # noqa: E402
    _DIGEST,
    STATEMENT_TIMEOUT_MS,
    Refused,
    check_columns,
    check_session,
    open_readonly,
    source_params,
    target_params,
)

SOURCE_TABLE = ("auth", "users")
SOURCE_COLUMNS = {"id": {"uuid"}, "email": {"varchar", "text"}, "encrypted_password": {"varchar", "text"},
                  "email_confirmed_at": {"timestamptz"}, "deleted_at": {"timestamptz"},
                  "banned_until": {"timestamptz"}}
TARGET_TABLE = ("public", "users")
TARGET_COLUMNS = {"id": {"uuid"}, "legacy_supabase_user_id": {"uuid"}, "email": {"citext", "varchar", "text"},
                  "password_hash": {"text", "varchar"}}

SOURCE_SQL = (f"SELECT id::text, {_DIGEST.format('email')}, {_DIGEST.format('lower(btrim(email))')}, "
              "nullif(encrypted_password, ''), email_confirmed_at IS NOT NULL, deleted_at IS NOT NULL, "
              "coalesce(banned_until > now(), false) FROM auth.users")
_TARGET_SELECT = (f"SELECT id::text, legacy_supabase_user_id::text, {_DIGEST.format('email::text')}, "
                  f"{_DIGEST.format('lower(btrim(email::text))')}, coalesce(password_hash, '') <> '' FROM public.users")

LOCK_TIMEOUT = "5s"

# ── source freeze and fingerprint (D1 cutover safeguards) ──────────────────
# The freeze is the trigger in docs/operations/d1-auth-freeze/freeze.sql. It counts as active only if the trigger is
# enabled ('O' or 'A'), a row-level before trigger on insert, update and delete (tgtype 31) with no column list or
# WHEN, and calls
# hyperchat_ops.d1_auth_freeze_guard (plpgsql) whose body hashes to FREEZE_FUNCTION_SHA256 (pinned by a test against
# freeze.sql). A trigger name alone proves nothing.
FREEZE_TRIGGER = "hyperchat_d1_auth_freeze"
FREEZE_FUNCTION = ("hyperchat_ops", "d1_auth_freeze_guard", "plpgsql")
FREEZE_FUNCTION_SHA256 = "243e5a50f507b66ece282566ff76419e9c99963a841a09faa0b9003be0e0d760"
FREEZE_TGTYPE = 1 | 2 | 4 | 8 | 16  # row | before | insert | delete | update
FREEZE_SQL = """
    SELECT t.tgenabled::text, t.tgtype::int, t.tgattr::text, t.tgqual IS NOT NULL, pn.nspname::text, p.proname::text,
           l.lanname::text, encode(sha256(convert_to(replace(p.prosrc, chr(13), ''), 'UTF8')), 'hex')
    FROM pg_catalog.pg_trigger t
    JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_proc p ON p.oid = t.tgfoid
    JOIN pg_catalog.pg_namespace pn ON pn.oid = p.pronamespace
    JOIN pg_catalog.pg_language l ON l.oid = p.prolang
    WHERE n.nspname = 'auth' AND c.relname = 'users' AND t.tgname = %s AND NOT t.tgisinternal
"""

# The fingerprint covers exactly what decides matching and eligibility: id, the exact email, the password hash (empty
# = none, as above), and the deleted / banned flags as the importer evaluates them. One row per account in id order,
# each field length-prefixed ("<chars>:<value>|", "-|" for NULL), so no two different sets serialize alike. Computed
# inside PostgreSQL, so no hash leaves the server for it; email_confirmed_at is only counted, so it is not included.
FINGERPRINT_VERSION = "hyperchat-d1-source-v1"


def _field(expr: str) -> str:
    return f"coalesce(length({expr})::text || ':' || {expr}, '-') || '|'"


FINGERPRINT_SQL = (
    "SELECT encode(sha256(convert_to(%s || E'\\n' || coalesce(string_agg("
    + " || ".join([_field("id::text"), _field("email::text"), _field("nullif(encrypted_password, '')"),
                   "CASE WHEN deleted_at IS NOT NULL THEN 'd1|' ELSE 'd0|' END",
                   "CASE WHEN coalesce(banned_until > now(), false) THEN 'b1' ELSE 'b0' END"])
    + " || E'\\n', '' ORDER BY id), ''), 'UTF8')), 'hex') FROM auth.users")

# Exclusion reasons, in the order they are tested; each target row with a legacy id gets exactly one outcome.
EXCLUSIONS = ("target_legacy_id_not_in_source", "target_email_used_by_other_source_id", "id_match_email_mismatch",
              "source_deleted", "source_banned", "target_already_has_password", "source_without_password",
              "unsupported_hash_format")
BLOCKERS = ("duplicate_source_ids", "duplicate_target_legacy_ids", "unsupported_hash_format")


@dataclass(frozen=True)
class Source:
    id: str
    email: str | None  # hex SHA-256 digests of the email, computed by the server
    email_norm: str | None
    password_hash: str | None = field(repr=False)  # never in a repr, traceback or log
    email_confirmed: bool
    deleted: bool
    banned: bool


@dataclass(frozen=True)
class Target:
    id: str
    legacy_id: str | None
    email: str | None
    email_norm: str | None
    has_password: bool


@dataclass(frozen=True)
class Update:
    target_id: str
    legacy_id: str
    email: str
    password_hash: str = field(repr=False)


@dataclass
class Plan:
    counts: dict[str, int]
    updates: list[Update] = field(repr=False)
    fingerprint: str = ""  # of the source snapshot the updates were read from (read_plan)
    freeze_problem: str | None = "not checked"  # None = the freeze is active on the source

    @property
    def blocked(self) -> list[str]:
        return [b for b in BLOCKERS if self.counts[b]]


def plan(source: list[Source], target: list[Target]) -> Plan:
    src_ids = Counter(s.id for s in source)
    by_id = {s.id: s for s in source}
    tgt_ids = Counter(t.legacy_id for t in target if t.legacy_id)
    ids_by_email: dict[str, set[str]] = {}
    for s in source:
        if s.email_norm:
            ids_by_email.setdefault(s.email_norm, set()).add(s.id)
    c = Counter({k: 0 for k in ("eligible", "eligible_email_confirmed", *EXCLUSIONS)})
    updates = []
    for t in target:
        if not t.legacy_id:
            continue
        s = by_id.get(t.legacy_id)
        if s is None:
            reason = "target_legacy_id_not_in_source"
        elif t.email_norm and ids_by_email.get(t.email_norm, set()) - {t.legacy_id}:
            reason = "target_email_used_by_other_source_id"
        elif s.email is None or s.email != t.email:  # exact top-level email; case-only differences count too
            reason = "id_match_email_mismatch"
        elif s.deleted:
            reason = "source_deleted"
        elif s.banned:
            reason = "source_banned"
        elif t.has_password:
            reason = "target_already_has_password"
        elif not s.password_hash:
            reason = "source_without_password"
        elif not is_supported_bcrypt(s.password_hash):
            reason = "unsupported_hash_format"
        else:
            reason = "eligible"
            c["eligible_email_confirmed"] += s.email_confirmed
            updates.append(Update(t.id, t.legacy_id, t.email, s.password_hash))
        c[reason] += 1
    counts = {
        "source_accounts": len(source), "target_accounts": len(target),
        "target_with_legacy_id": sum(tgt_ids.values()),
        "target_without_legacy_id": sum(1 for t in target if not t.legacy_id),
        **dict(c),
        "source_ids_not_in_target": sum(1 for i in src_ids if i not in tgt_ids),
        "duplicate_source_ids": sum(1 for n in src_ids.values() if n > 1),
        "duplicate_target_legacy_ids": sum(1 for n in tgt_ids.values() if n > 1),
    }
    if counts["eligible"] + sum(counts[e] for e in EXCLUSIONS) != counts["target_with_legacy_id"]:
        raise Refused("internal error: the plan's counts do not add up; nothing was written")
    return Plan(counts, updates)


def read_sources(conn: psycopg.Connection) -> list[Source]:
    conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
    return [Source(*row) for row in conn.execute(SOURCE_SQL).fetchall()]


def read_targets(conn: psycopg.Connection) -> list[Target]:
    conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
    return [Target(*row) for row in conn.execute(_TARGET_SELECT).fetchall()]


def check_writer(conn: psycopg.Connection) -> None:
    """The apply role: the expected columns, UPDATE on password_hash, and not a superuser."""
    check_columns(conn, TARGET_TABLE, TARGET_COLUMNS, "target")
    ok = conn.execute("SELECT NOT r.rolsuper AND has_column_privilege('public.users', 'password_hash', 'UPDATE') "
                      "FROM pg_roles r WHERE r.rolname = current_user").fetchone()[0]
    if not ok:
        raise Refused("target: the role needs UPDATE on public.users.password_hash and must not be a superuser")


def _write(cur: psycopg.Cursor, u: Update) -> int:
    cur.execute(f"UPDATE public.users SET password_hash = %s WHERE id = %s AND legacy_supabase_user_id = %s "
                f"AND {_DIGEST.format('email::text')} = %s AND coalesce(password_hash, '') = ''",
                (u.password_hash, u.target_id, u.legacy_id, u.email))
    return cur.rowcount


def apply(conn: psycopg.Connection, p: Plan, before_commit=None) -> int:
    """All planned rows in ONE transaction: lock them (users rows, the app's first lock), re-check that each is still
    exactly as planned, then write each conditionally. Any difference raises and rolls back every row. `before_commit`
    runs last inside that transaction (main() checks the source lock there); if it raises, nothing is committed."""
    if p.blocked:
        raise Refused(f"blocked by {', '.join(p.blocked)}; nothing was written")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        cur.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
        check_writer(conn)
        ids = [u.target_id for u in p.updates]
        cur.execute(f"{_TARGET_SELECT} WHERE id = ANY(%s::uuid[]) ORDER BY id FOR UPDATE", (ids,))
        now = {row[0]: Target(*row) for row in cur.fetchall()}
        for u in p.updates:
            t = now.get(u.target_id)
            if t is None or (t.legacy_id, t.email, t.has_password) != (u.legacy_id, u.email, False):
                raise Refused("target changed since the plan; nothing was written")
        for u in p.updates:
            if _write(cur, u) != 1:
                raise Refused("target changed while writing; nothing was written")
        if before_commit:
            before_commit()
    return len(p.updates)


def render(counts: dict[str, int], title: str) -> str:
    if not all(type(v) is int for v in counts.values()):  # a real check, not assert: `python -O` drops asserts
        raise ValueError("the summary holds counts only")
    width = max(map(len, counts))
    return "\n".join([title] + [f"  {k.ljust(width)}  {v}" for k, v in counts.items()])


def connect_source(params: dict) -> psycopg.Connection:
    conn = open_readonly(application_name="hyperchat-auth-hash-import", **params)
    if not conn.pgconn.ssl_in_use:
        conn.close()
        raise Refused("source: the connection is not using TLS")
    return conn


def freeze_problem(conn: psycopg.Connection) -> str | None:
    """None when the freeze trigger is in place exactly as reviewed; otherwise why not (safe to print)."""
    rows = conn.execute(FREEZE_SQL, (FREEZE_TRIGGER,)).fetchall()
    if not rows:
        return f"the trigger {FREEZE_TRIGGER} is missing on auth.users (run freeze.sql)"
    enabled, tgtype, columns, has_when, schema, name, language, body_sha256 = rows[0]
    if enabled not in ("O", "A"):
        return f"the trigger {FREEZE_TRIGGER} is not enabled (tgenabled={enabled!r}; 'D' disabled, 'R' replica only)"
    if tgtype != FREEZE_TGTYPE or columns.strip() or has_when:
        return (f"the trigger {FREEZE_TRIGGER} is malformed: it must fire before every insert, update and delete, for "
                "each row, without a column list or WHEN condition")
    if (schema, name, language) != FREEZE_FUNCTION:
        return f"the trigger {FREEZE_TRIGGER} calls {schema}.{name} ({language}), not hyperchat_ops.d1_auth_freeze_guard"
    if body_sha256 != FREEZE_FUNCTION_SHA256:
        return "the freeze function's body differs from the reviewed freeze.sql"
    return None


def source_fingerprint(conn: psycopg.Connection) -> str:
    return conn.execute(FINGERPRINT_SQL, (FINGERPRINT_VERSION,)).fetchone()[0]


def read_plan(src: psycopg.Connection, tgt: psycopg.Connection, accept_writable_source_role: bool) -> Plan:
    """The freeze check, the fingerprint and the hashes come from ONE source transaction (REPEATABLE READ, see
    open_readonly): the fingerprint describes exactly the rows this plan would import. That snapshot cannot see changes
    committed after it began, so --apply verifies the source again under a lock that it holds through the target
    commit (apply_under_source_lock)."""
    try:
        check_session(src, SOURCE_TABLE, SOURCE_COLUMNS, "source", accept_writable_source_role)
        problem = freeze_problem(src)
        fingerprint = source_fingerprint(src)
        sources = read_sources(src)
    finally:
        src.rollback()
    try:
        check_columns(tgt, TARGET_TABLE, TARGET_COLUMNS, "target")
        p = plan(sources, read_targets(tgt))
    finally:
        tgt.rollback()
    p.fingerprint, p.freeze_problem = fingerprint, problem
    return p


# ── the source lock around the target commit ───────────────────────────────
# After the operator confirms, a fresh source transaction takes SHARE on auth.users. SHARE conflicts with every insert,
# update, delete and truncate on the table (also with session_replication_role = replica) and with enabling, disabling,
# dropping or replacing its triggers, but not with reads: until it is released no source row can change. It is
# released only after the target transaction has committed or rolled back. It does NOT stop a superuser from replacing
# the trigger's function or editing pg_trigger directly; those cannot change data while the lock is held, only weaken
# the freeze for later. Waiting for in-flight writes is bounded (SOURCE_LOCK_TIMEOUT). If the importer stalls while
# holding it, the server ends the session (SOURCE_LOCK_IDLE_LIMIT), which frees the lock: the import then rolls back,
# or, in the last instant, reports it (exit 4). The source role needs MAINTAIN on auth.users (PostgreSQL 17+) or table
# update / delete / truncate (earlier versions, with --accept-writable-source-role); the transaction stays READ ONLY.
SOURCE_LOCK_TIMEOUT = "5s"
SOURCE_LOCK_IDLE_LIMIT = "20s"
_LOCK_HELD_SQL = ("SELECT count(*) FROM pg_catalog.pg_locks WHERE pid = pg_backend_pid() AND locktype = 'relation' "
                  "AND relation = 'auth.users'::regclass AND mode = 'ShareLock' AND granted")


def lock_source(src: psycopg.Connection, expected_fingerprint: str, p: Plan) -> None:
    """Take SHARE on auth.users in a FRESH source transaction, then verify under it: the freeze is exactly the reviewed
    one, the fingerprint equals the reviewed value, and every planned hash is still the source's current hash for an
    eligible account. The snapshot is taken after the lock, so it is the current state, and it cannot change while the
    lock is held. Leaves the transaction (and the lock) open on success; callers must release_source() in every case.
    Raises Refused (or a database error) before anything is written to the target."""
    src.execute(f"SET LOCAL lock_timeout = '{SOURCE_LOCK_TIMEOUT}'; "
                f"SET LOCAL idle_in_transaction_session_timeout = '{SOURCE_LOCK_IDLE_LIMIT}'; "
                f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}; "
                "SET LOCAL row_security = off")  # row-level security raises instead of hiding rows (check_row_security)
    try:
        src.execute("LOCK TABLE auth.users IN SHARE MODE")
    except psycopg.errors.LockNotAvailable:
        raise Refused(f"could not lock the source within {SOURCE_LOCK_TIMEOUT} (writes to auth.users in progress); "
                      "nothing was written; try again") from None
    except psycopg.errors.InsufficientPrivilege:
        raise Refused("the source role may not lock auth.users: it needs MAINTAIN on it (PostgreSQL 17+) or table "
                      "update, delete or truncate (earlier versions, with --accept-writable-source-role); nothing was "
                      "written") from None
    problem = freeze_problem(src)
    if problem:
        raise Refused(f"the source freeze is not active under the lock: {problem}; nothing was written")
    if source_fingerprint(src) != expected_fingerprint:
        raise Refused("the source changed after the plan was read (under the lock it no longer matches the reviewed "
                      "fingerprint); nothing was written")
    current = {s.id: s for s in read_sources(src)}
    for u in p.updates:
        s = current.get(u.legacy_id)
        if s is None or s.email != u.email or s.deleted or s.banned or s.password_hash != u.password_hash:
            raise Refused("a planned hash no longer matches the locked source; nothing was written")


def source_lock_held(src: psycopg.Connection) -> bool:
    """True only if this session's transaction is alive and still holds the SHARE lock. Locks are kept until the
    transaction ends, and any error in it ends them at once, so True also proves the lock was held continuously since
    lock_source; any failure (the session ended, the query failed) means it may not have been."""
    try:
        return src.execute(_LOCK_HELD_SQL).fetchone()[0] == 1
    except psycopg.Error:
        return False


def require_source_lock(src: psycopg.Connection) -> None:
    """Run inside the target transaction just before its COMMIT."""
    if not source_lock_held(src):
        raise Refused("the source lock was lost before the target commit (the source session ended or failed); "
                      "nothing was written")


def release_source(src: psycopg.Connection) -> None:
    try:
        src.rollback()
    except psycopg.Error:
        src.close()  # the session is already gone, and its lock with it; never reuse the connection


def apply_under_source_lock(src: psycopg.Connection, tgt: psycopg.Connection, p: Plan,
                            expected_fingerprint: str) -> tuple[int, bool]:
    """Lock and verify the source, write the target in one transaction and commit it while the source lock is still
    held, then release the lock. Returns (rows updated, lock proven held through the target commit). Nothing is
    written if locking or verification fails or the lock is lost before the commit."""
    try:
        lock_source(src, expected_fingerprint, p)
        updated = apply(tgt, p, before_commit=lambda: require_source_lock(src))
        return updated, source_lock_held(src)
    finally:
        release_source(src)


def check_apply_guard(p: Plan, expected_fingerprint: str | None) -> None:
    """--apply only for the exact source set that was reviewed, and only while the freeze holds it still."""
    if not expected_fingerprint:
        raise Refused("--apply needs --expected-fingerprint (the source_fingerprint of the reviewed dry run)")
    if p.freeze_problem:
        raise Refused(f"the source freeze is not active: {p.freeze_problem}; nothing was written")
    if p.fingerprint != expected_fingerprint:
        raise Refused("the source fingerprint differs from the reviewed dry run: the source changed since it was "
                      "reviewed; run the report and the dry run again; nothing was written")


def _fingerprint_arg(value: str) -> str:
    value = value.strip().lower()
    if len(value) != 64 or not all(ch in "0123456789abcdef" for ch in value):
        raise argparse.ArgumentTypeError("a SHA-256 hex digest (64 hex characters) is required")
    return value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m etl.auth_hash_import",
        description="Import Supabase bcrypt password hashes into HyperChat V2 for accounts matching on legacy id AND "
                    "email. DRY RUN unless --apply. Connection details are prompted for.")
    ap.add_argument("--apply", action="store_true", help="write the hashes (after a typed confirmation)")
    ap.add_argument("--allow-dev-target", action="store_true",
                    help="permit --apply into hyperchat_dev (real hashes stay out of DEV without separate approval)")
    ap.add_argument("--accept-writable-source-role", action="store_true",
                    help="allow a source role that has write privileges (the source transaction is still READ ONLY)")
    ap.add_argument("--expected-fingerprint", type=_fingerprint_arg, metavar="SHA256",
                    help="with --apply: the source_fingerprint printed by the reviewed dry run (required)")
    args = ap.parse_args(argv)
    print("APPLY: hashes will be written after confirmation." if args.apply
          else "DRY RUN: nothing will be written.", file=sys.stderr)
    p = None
    try:
        if args.apply and not args.expected_fingerprint:
            raise Refused("--apply needs --expected-fingerprint (the source_fingerprint of the reviewed dry run)")
        source = source_params()
        target = target_params(role="hyperchat_app" if args.apply else "hyperchat_readonly",
                               dbname=None if args.apply else "hyperchat_dev")
        if args.apply and target["dbname"] == "hyperchat_dev" and not args.allow_dev_target:
            raise Refused("--apply into hyperchat_dev needs --allow-dev-target (and the owner's explicit approval)")
        with connect_source(source) as src:
            with psycopg.connect(**target, application_name="hyperchat-auth-hash-import", connect_timeout=15) as tgt:
                tgt.read_only = not args.apply
                p = read_plan(src, tgt, args.accept_writable_source_role)
                print(render(p.counts, "PLAN (aggregate counts only)"))
                print(f"source_fingerprint  {p.fingerprint}")
                print("source_freeze       " + ("active" if p.freeze_problem is None
                                                else f"NOT ACTIVE: {p.freeze_problem}"))
                if not args.apply:
                    print("dry run: nothing was written", file=sys.stderr)
                    return 0
                if p.blocked:
                    raise Refused(f"blocked by {', '.join(p.blocked)}; nothing was written")
                check_apply_guard(p, args.expected_fingerprint)
                if not p.updates:
                    print("nothing to import", file=sys.stderr)
                    return 0
                phrase = f"IMPORT {len(p.updates)} HASHES INTO {target['dbname']}"
                if input(f'Type exactly "{phrase}" to write, anything else cancels: ').strip() != phrase:
                    print("cancelled: nothing was written", file=sys.stderr)
                    return 3
                updated, lock_held = apply_under_source_lock(src, tgt, p, args.expected_fingerprint)
                print(render({"updated": updated}, "APPLIED (one transaction, committed)"))
                if not lock_held:  # detection only: the commit has already happened
                    print("ALERT: the import committed, but the source lock could not be shown to have been held "
                          "through the commit (the source session ended at the last moment), so the source may have "
                          "changed. Keep V2 sign-in off and legacy frozen, run a fresh dry run now and compare "
                          "source_fingerprint; if it differs or the freeze is not active, follow the abort rules.",
                          file=sys.stderr)
                    return 4
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except psycopg.Error as exc:  # messages may name hosts or roles: print only the class and SQLSTATE
        print(f"failed: {type(exc).__name__} (SQLSTATE {exc.sqlstate or 'n/a'}); nothing was written by this run "
              "unless APPLIED was printed", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("cancelled", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - never let a traceback print values
        print(f"failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        p = None  # drop the in-memory hashes
    return 0


if __name__ == "__main__":
    sys.exit(main())
