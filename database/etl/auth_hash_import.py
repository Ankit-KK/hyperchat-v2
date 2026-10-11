"""Supabase Auth password-hash import into HyperChat V2 (decision D1, phase 2). DRY RUN BY DEFAULT.

Copies bcrypt hashes from the live Supabase auth.users straight into public.users.password_hash, in memory only (no
export file, nothing logged or printed), for accounts that match on BOTH auth.users.id = legacy_supabase_user_id AND
the exact top-level email, whose source hash is a bcrypt hash the V2 verifier accepts and whose target hash is still
empty. It never matches by email alone, never creates or deletes accounts, and changes no other column: roles,
creator ownership, activation and email verification stay as they are (email_confirmed_at is only counted).

Without --apply it only reads (READ ONLY transactions on both sides) and prints aggregate counts. --apply also needs a
typed confirmation, refuses on any blocker (duplicate mappings, unsupported hash formats), re-checks every planned row
under a row lock and writes all rows in ONE transaction, each conditional on the hash still being empty; any change
since the plan rolls everything back. Operator guide: database/README.md. Run:  python -m etl.auth_hash_import
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
    assert counts["eligible"] + sum(counts[e] for e in EXCLUSIONS) == counts["target_with_legacy_id"]
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


def apply(conn: psycopg.Connection, p: Plan) -> int:
    """All planned rows in ONE transaction: lock them (users rows, the app's first lock), re-check that each is still
    exactly as planned, then write each conditionally. Any difference raises and rolls back every row."""
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
    return len(p.updates)


def render(counts: dict[str, int], title: str) -> str:
    assert all(type(v) is int for v in counts.values()), "the summary holds counts only"
    width = max(map(len, counts))
    return "\n".join([title] + [f"  {k.ljust(width)}  {v}" for k, v in counts.items()])


def connect_source(params: dict) -> psycopg.Connection:
    conn = open_readonly(application_name="hyperchat-auth-hash-import", **params)
    if not conn.pgconn.ssl_in_use:
        conn.close()
        raise Refused("source: the connection is not using TLS")
    return conn


def read_plan(src: psycopg.Connection, tgt: psycopg.Connection, accept_writable_source_role: bool) -> Plan:
    try:
        check_session(src, SOURCE_TABLE, SOURCE_COLUMNS, "source", accept_writable_source_role)
        sources = read_sources(src)
    finally:
        src.rollback()
    try:
        check_columns(tgt, TARGET_TABLE, TARGET_COLUMNS, "target")
        return plan(sources, read_targets(tgt))
    finally:
        tgt.rollback()


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
    args = ap.parse_args(argv)
    print("APPLY: hashes will be written after confirmation." if args.apply
          else "DRY RUN: nothing will be written.", file=sys.stderr)
    p = None
    try:
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
                if not args.apply:
                    print("dry run: nothing was written", file=sys.stderr)
                    return 0
                if p.blocked:
                    raise Refused(f"blocked by {', '.join(p.blocked)}; nothing was written")
                if not p.updates:
                    print("nothing to import", file=sys.stderr)
                    return 0
                phrase = f"IMPORT {len(p.updates)} HASHES INTO {target['dbname']}"
                if input(f'Type exactly "{phrase}" to write, anything else cancels: ').strip() != phrase:
                    print("cancelled: nothing was written", file=sys.stderr)
                    return 3
                print(render({"updated": apply(tgt, p)}, "APPLIED (one transaction, committed)"))
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except ValueError:
        print("refused: a port must be a number", file=sys.stderr)
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
