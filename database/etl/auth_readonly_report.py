"""READ-ONLY REPORT: Supabase Auth accounts vs HyperChat V2 users (decision D1, phase 1).

This tool cannot import passwords, create or update accounts, or change verification status: it contains no write
statements, runs every query inside a READ ONLY transaction on both databases, and never selects a password hash.
It prints aggregate counts only (no ids, emails, hashes or connection details).

  Source: live Supabase Postgres, auth.users (id, email, whether encrypted_password is set, confirmed_at).
  Target: HyperChat V2 public.users (legacy_supabase_user_id, email, whether password_hash is set, email_verified_at).

Accounts are matched by auth.users.id = public.users.legacy_supabase_user_id; the top-level email is cross-checked
(raw_user_meta_data is never read). Emails are compared as SHA-256 digests computed by each server, held in memory
only for the run. Connection details are prompted for interactively (password without echo); see the operator guide
in database/README.md. Run:  python -m etl.auth_readonly_report
"""

import argparse
import getpass
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import psycopg

# Expected columns (pg_type.typname). Anything else: refuse to run.
SOURCE_TABLE = ("auth", "users")
SOURCE_COLUMNS = {"id": {"uuid"}, "email": {"varchar", "text"}, "encrypted_password": {"varchar", "text"},
                  "confirmed_at": {"timestamptz"}}
TARGET_TABLE = ("public", "users")
TARGET_COLUMNS = {"id": {"uuid"}, "legacy_supabase_user_id": {"uuid"}, "email": {"citext", "varchar", "text"},
                  "password_hash": {"text", "varchar"}, "email_verified_at": {"timestamptz"}}

_DIGEST = "encode(sha256(convert_to({}, 'UTF8')), 'hex')"
# Never selects encrypted_password itself: only whether it is set.
SOURCE_SQL = (f"SELECT id::text, {_DIGEST.format('email')}, {_DIGEST.format('lower(btrim(email))')}, "
              "coalesce(encrypted_password, '') <> '', confirmed_at IS NOT NULL FROM auth.users")
TARGET_SQL = (f"SELECT legacy_supabase_user_id::text, {_DIGEST.format('email::text')}, "
              f"{_DIGEST.format('lower(btrim(email::text))')}, coalesce(password_hash, '') <> '', "
              "email_verified_at IS NOT NULL FROM public.users")
STATEMENT_TIMEOUT_MS = 60_000


class Refused(Exception):
    """A safety check failed; the message is safe to print (no secrets, no personal data)."""


@dataclass(frozen=True)
class Account:
    """One row, as held in memory for the run. `email` and `email_norm` are hex SHA-256 digests (or None)."""
    legacy_id: str | None
    email: str | None
    email_norm: str | None
    has_password: bool
    verified: bool


def summarize(source: list[Account], target: list[Account]) -> dict[str, int]:
    """Aggregate counts only. `legacy_id` is auth.users.id for source rows, legacy_supabase_user_id for target rows."""
    src_ids = Counter(a.legacy_id for a in source)
    by_id = {a.legacy_id: a for a in source}
    tgt_ids = Counter(a.legacy_id for a in target if a.legacy_id)
    src_emails = Counter(a.email_norm for a in source if a.email_norm)
    tgt_emails = Counter(a.email_norm for a in target if a.email_norm)
    ids_by_email: dict[str, set[str]] = {}
    for a in source:
        if a.email_norm:
            ids_by_email.setdefault(a.email_norm, set()).add(a.legacy_id)

    s = Counter({k: 0 for k in (
        "id_and_email_exact", "id_match_email_case_or_space_only", "id_match_email_mismatch",
        "id_match_source_email_missing", "target_legacy_id_not_in_source", "target_email_used_by_other_source_id",
        "matched_source_has_password", "matched_source_confirmed", "exact_match_source_password_target_none")})
    for t in target:
        if t.email_norm and ids_by_email.get(t.email_norm, set()) - {t.legacy_id}:
            s["target_email_used_by_other_source_id"] += 1
        if not t.legacy_id:
            continue
        src = by_id.get(t.legacy_id)
        if src is None:
            s["target_legacy_id_not_in_source"] += 1
            continue
        s["matched_source_has_password"] += src.has_password
        s["matched_source_confirmed"] += src.verified
        if src.email is None:
            s["id_match_source_email_missing"] += 1
        elif src.email == t.email:
            s["id_and_email_exact"] += 1
            s["exact_match_source_password_target_none"] += src.has_password and not t.has_password
        elif src.email_norm == t.email_norm:
            s["id_match_email_case_or_space_only"] += 1
        else:
            s["id_match_email_mismatch"] += 1
    return {
        "source_accounts": len(source),
        "target_accounts": len(target),
        "target_with_legacy_id": sum(1 for a in target if a.legacy_id),
        "target_without_legacy_id": sum(1 for a in target if not a.legacy_id),
        **dict(s),
        "source_ids_not_in_target": sum(1 for i in src_ids if i not in tgt_ids),
        "duplicate_source_ids": sum(1 for n in src_ids.values() if n > 1),
        "duplicate_target_legacy_ids": sum(1 for n in tgt_ids.values() if n > 1),
        "duplicate_source_emails_normalized": sum(1 for n in src_emails.values() if n > 1),
        "duplicate_target_emails_normalized": sum(1 for n in tgt_emails.values() if n > 1),
        "source_with_password": sum(a.has_password for a in source),
        "source_confirmed": sum(a.verified for a in source),
        "target_with_password_hash": sum(a.has_password for a in target),
        "target_email_verified": sum(a.verified for a in target),
    }


def render(summary: dict[str, int]) -> str:
    assert all(type(v) is int for v in summary.values()), "the report holds counts only"
    width = max(map(len, summary))
    return "\n".join(["READ-ONLY REPORT (aggregate counts only; nothing was changed)"] +
                     [f"  {k.ljust(width)}  {v}" for k, v in summary.items()])


def check_session(conn: psycopg.Connection, table: tuple[str, str], columns: dict[str, set[str]], label: str,
                  accept_writable_role: bool) -> None:
    """Fail closed: read-only transaction, expected columns and types, SELECT on each column, and (unless accepted)
    a role that cannot write to the table."""
    schema, name = table
    if conn.execute("SELECT current_setting('transaction_read_only')").fetchone()[0] != "on":
        raise Refused(f"{label}: the transaction is not read-only")
    check_columns(conn, table, columns, label)
    writable = conn.execute(
        "SELECT r.rolsuper OR has_table_privilege(%s, 'INSERT, UPDATE, DELETE, TRUNCATE') "
        "FROM pg_roles r WHERE r.rolname = current_user", (f"{schema}.{name}",)).fetchone()[0]
    if writable and not accept_writable_role:
        raise Refused(f"{label}: this role can write to {schema}.{name}; use a read-only role "
                      f"(or pass --accept-writable-{label}-role to rely on the read-only transaction alone)")
    if writable:
        print(f"note: the {label} role can write; relying on the READ ONLY transaction", file=sys.stderr)


def check_columns(conn: psycopg.Connection, table: tuple[str, str], columns: dict[str, set[str]], label: str) -> None:
    """Fail closed unless every expected column exists with an expected type and is readable by this role."""
    schema, name = table
    # pg_catalog, not information_schema: the latter hides columns the role cannot read.
    found = dict(conn.execute(
        "SELECT a.attname, t.typname FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace JOIN pg_catalog.pg_type t ON t.oid = a.atttypid "
        "WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped",
        (schema, name)).fetchall())
    for column, types in columns.items():
        if found.get(column) not in types:
            raise Refused(f"{label}: {schema}.{name}.{column} is missing or has an unexpected type")
        if not conn.execute("SELECT has_column_privilege(%s, %s, 'SELECT')",
                            (f"{schema}.{name}", column)).fetchone()[0]:
            raise Refused(f"{label}: no SELECT privilege on {schema}.{name}.{column}")


def read_accounts(conn: psycopg.Connection, query: str) -> list[Account]:
    conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
    return [Account(*row) for row in conn.execute(query).fetchall()]


def report(source: psycopg.Connection, target: psycopg.Connection, *, accept_writable_source_role: bool = False,
           accept_writable_target_role: bool = False) -> dict[str, int]:
    """Both connections must already be in read-only mode (see open_readonly). Ends both transactions with ROLLBACK."""
    try:
        check_session(source, SOURCE_TABLE, SOURCE_COLUMNS, "source", accept_writable_source_role)
        check_session(target, TARGET_TABLE, TARGET_COLUMNS, "target", accept_writable_target_role)
        return summarize(read_accounts(source, SOURCE_SQL), read_accounts(target, TARGET_SQL))
    finally:
        source.rollback()
        target.rollback()


def open_readonly(application_name: str = "hyperchat-auth-readonly-report", **params) -> psycopg.Connection:
    conn = psycopg.connect(**params, application_name=application_name, connect_timeout=15)
    conn.read_only = True  # every transaction starts as BEGIN READ ONLY
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ  # one consistent snapshot per database
    return conn


def _ask(prompt: str, default: str | None = None) -> str:
    value = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip() or (default or "")
    if not value:
        raise Refused(f"{prompt} is required")
    return value


def source_params() -> dict:
    """Supabase: TLS with certificate AND hostname verification against the CA file the operator downloaded."""
    host = _ask("Supabase host (e.g. db.<project-ref>.supabase.co)")
    ca = Path(_ask("Path to the Supabase CA certificate (PEM)")).expanduser()
    if not ca.is_file():
        raise Refused("the CA certificate file does not exist")
    return {"host": host, "port": int(_ask("Supabase port", "5432")), "dbname": _ask("Supabase database", "postgres"),
            "user": _ask("Supabase role (read-only role recommended)"),
            "password": getpass.getpass("Supabase password (not echoed): ") or None,
            "sslmode": "verify-full", "sslrootcert": str(ca)}


def target_params(role: str = "hyperchat_readonly", dbname: str | None = "hyperchat_dev") -> dict:
    """HyperChat V2 on the same VPS: Unix socket or loopback only, so no credentials cross the network."""
    host = _ask("Target host or socket directory", "/var/run/postgresql")
    if not (host.startswith("/") or host in ("127.0.0.1", "localhost", "::1")):
        raise Refused("the target must be a local socket directory or loopback address")
    params = {"host": host, "port": int(_ask("Target port", "5433")), "dbname": _ask("Target database", dbname),
              "user": _ask("Target role", role)}
    password = getpass.getpass("Target password (not echoed; empty = peer auth / .pgpass): ")
    return params | ({"password": password} if password else {})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m etl.auth_readonly_report",
        description="READ-ONLY aggregate report comparing Supabase auth.users with HyperChat V2 public.users. "
                    "It cannot import passwords or change any account. Connection details are prompted for.")
    ap.add_argument("--accept-writable-source-role", action="store_true",
                    help="allow a source role that has write privileges (the transaction is still READ ONLY)")
    ap.add_argument("--accept-writable-target-role", action="store_true",
                    help="allow a target role that has write privileges (the transaction is still READ ONLY)")
    args = ap.parse_args(argv)
    print("READ-ONLY report: nothing will be imported or changed.", file=sys.stderr)
    try:
        with open_readonly(**source_params()) as src:
            if not src.pgconn.ssl_in_use:
                raise Refused("source: the connection is not using TLS")
            with open_readonly(**target_params()) as tgt:
                summary = report(src, tgt, accept_writable_source_role=args.accept_writable_source_role,
                                 accept_writable_target_role=args.accept_writable_target_role)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except ValueError:
        print("refused: a port must be a number", file=sys.stderr)
        return 2
    except psycopg.Error as exc:  # the message may name hosts or roles: print only the class and SQLSTATE
        print(f"failed: {type(exc).__name__} (SQLSTATE {exc.sqlstate or 'n/a'}); check the host, port, CA file "
              "and credentials", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("cancelled", file=sys.stderr)
        return 130
    print(render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
