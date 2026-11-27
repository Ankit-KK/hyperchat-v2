"""Manage this database's further Didit accounts (migration 0040; docs/kyc-rollout.md, "Didit accounts").

The primary account stays etl/kyc_provider_config.py's single row. This tool adds or updates the others (their
webhook secret is read from the terminal without echo, or one line of stdin with --secret-stdin, never from argv),
enables or disables any account (primary included; disabling stops NEW sessions only), sets a monthly cap, revokes an
account (final: its approvals stop counting) and moves a user's pin to another account (the only way a user changes
account; it keeps the user's whole history, declines included). Every change is audited in kyc_account_events with
--operator. Run with the migration DSN, from database/:

    HYPERCHAT_MIGRATION_DSN=... python -m etl.kyc_provider_account --operator <name> add --account acct-b \
        --application-id <uuid> --workflow-id <uuid> [--monthly-cap N] [--order N] [--disabled]
    ... update --account acct-b [--workflow-id <uuid>] [--application-id <uuid>] [--new-secret]
    ... enable|disable --account <id|primary>
    ... cap --account <id|primary> --monthly N|none
    ... revoke --account <id|primary> --reason "<why>"
    ... move-user --user <uuid> --to <id|primary> --reason "<why>"
    ... list
"""

import argparse
import getpass
import os
import re
import sys
import uuid

import psycopg

ACCOUNT = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


def _account(value: str) -> str:
    if not ACCOUNT.match(value):
        raise argparse.ArgumentTypeError("account ids are 2-32 of a-z, 0-9 and '-', starting with a letter")
    return value


def _cap(value: str) -> int | None:
    if value == "none":
        return None
    if not value.isdigit():
        raise argparse.ArgumentTypeError("a non-negative number or 'none'")
    return int(value)


def _secret(from_stdin: bool) -> str | None:
    secret = (sys.stdin.readline() if from_stdin else getpass.getpass("Didit webhook secret: ")).strip()
    if not 16 <= len(secret) <= 512:
        print("Refused: the webhook secret must be 16-512 characters.", file=sys.stderr)
        return None
    return secret


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--operator", required=True, help="who makes the change (kept in the audit)")
    sub = p.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add")
    add.add_argument("--account", type=_account, required=True)
    add.add_argument("--application-id", type=uuid.UUID, required=True)
    add.add_argument("--workflow-id", type=uuid.UUID, required=True)
    add.add_argument("--monthly-cap", type=_cap)
    add.add_argument("--order", type=int, default=100)
    add.add_argument("--disabled", action="store_true")
    add.add_argument("--secret-stdin", action="store_true")
    upd = sub.add_parser("update")
    upd.add_argument("--account", type=_account, required=True)
    upd.add_argument("--application-id", type=uuid.UUID)
    upd.add_argument("--workflow-id", type=uuid.UUID, help="a new workflow starts a new epoch for this account")
    upd.add_argument("--order", type=int)
    upd.add_argument("--new-secret", action="store_true")
    upd.add_argument("--secret-stdin", action="store_true")
    for name in ("enable", "disable"):
        sub.add_parser(name).add_argument("--account", type=_account, required=True)
    cap = sub.add_parser("cap")
    cap.add_argument("--account", type=_account, required=True)
    cap.add_argument("--monthly", type=_cap, required=True)
    rev = sub.add_parser("revoke")
    rev.add_argument("--account", type=_account, required=True)
    rev.add_argument("--reason", required=True)
    move = sub.add_parser("move-user")
    move.add_argument("--user", type=uuid.UUID, required=True)
    move.add_argument("--to", type=_account, required=True)
    move.add_argument("--reason", required=True)
    sub.add_parser("list")
    return p


def _table(account: str) -> tuple[str, str]:
    return ("public.kyc_provider_config", "true") if account == "primary" else \
        ("public.kyc_provider_accounts", "account_id = %(account)s")


def run(conn, args) -> int:
    conn.execute("SET ROLE hyperchat_owner")
    conn.execute("SELECT set_config('hyperchat.operator', %s, true)", (args.operator,))
    cmd = args.command
    if cmd == "list":
        rows = conn.execute("SELECT account_id, environment, workflow_id::text, application_id::text, enabled, "
                            "monthly_cap, revoked_at IS NOT NULL, sort_order, public.kyc_account_usage(account_id) "
                            "FROM public.kyc_accounts() ORDER BY sort_order, account_id").fetchall()
        for r in rows:
            print("account={} environment={} workflow={} application={} enabled={} cap={} revoked={} order={} "
                  "used_this_month={}".format(*r))
        return 0
    if cmd == "add":
        secret = _secret(args.secret_stdin)
        if secret is None:
            return 2
        if conn.execute("INSERT INTO public.kyc_provider_accounts (account_id, environment, workflow_id, "
                        "application_id, webhook_secret, enabled, monthly_cap, sort_order) SELECT %s, environment, %s, "
                        "%s, %s, %s, %s, %s FROM public.kyc_provider_config",
                        (args.account, args.workflow_id, args.application_id, secret, not args.disabled,
                         args.monthly_cap, args.order)).rowcount != 1:
            print("Refused: configure the primary first (etl.kyc_provider_config with --application-id).",
                  file=sys.stderr)
            return 2
    elif cmd == "update":
        sets, params = [], {"account": args.account}
        for column, value in (("workflow_id", args.workflow_id), ("application_id", args.application_id),
                              ("sort_order", args.order)):
            if value is not None:
                sets.append(f"{column} = %({column})s")
                params[column] = value
        if args.new_secret:
            secret = _secret(args.secret_stdin)
            if secret is None:
                return 2
            sets.append("webhook_secret = %(secret)s")
            params["secret"] = secret
        if not sets:
            print("Nothing to update.", file=sys.stderr)
            return 2
        if conn.execute(f"UPDATE public.kyc_provider_accounts SET {', '.join(sets)} WHERE account_id = %(account)s",
                        params).rowcount != 1:
            print(f"Refused: no account {args.account!r} (the primary is set with etl.kyc_provider_config).",
                  file=sys.stderr)
            return 2
    elif cmd in ("enable", "disable", "cap"):
        table, where = _table(args.account)
        column, value = ("monthly_cap", args.monthly) if cmd == "cap" else ("enabled", cmd == "enable")
        if conn.execute(f"UPDATE {table} SET {column} = %(value)s WHERE {where}",
                        {"value": value, "account": args.account}).rowcount != 1:
            print(f"Refused: no account {args.account!r}.", file=sys.stderr)
            return 2
    elif cmd == "revoke":
        result = conn.execute("SELECT public.kyc_account_revoke(%s, %s, %s)",
                              (args.account, args.operator, args.reason)).fetchone()[0]
        if result != "revoked":
            print(f"Refused: {result}.", file=sys.stderr)
            return 2
    elif cmd == "move-user":
        result = conn.execute("SELECT public.kyc_account_move_user(%s, %s, %s, %s)",
                              (args.user, args.to, args.operator, args.reason)).fetchone()[0]
        if result not in ("moved", "unchanged"):
            print(f"Refused: {result}.", file=sys.stderr)
            return 2
    print(f"Didit account {cmd}: done.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    dsn = os.environ["HYPERCHAT_MIGRATION_DSN"].replace("postgresql+psycopg://", "postgresql://", 1)  # as alembic
    with psycopg.connect(dsn) as conn:  # one transaction: the change and its audit commit together
        try:
            return run(conn, args)
        except psycopg.Error as exc:
            conn.rollback()
            print(f"Refused by the database: {type(exc).__name__}: {str(exc).splitlines()[0]}", file=sys.stderr)
            return 2


if __name__ == "__main__":
    sys.exit(main())
