"""Operator commands, run on the server (never exposed over HTTP).

    python -m app.cli --env-file /etc/hyperchat/api.env bootstrap-admin --email you@example.com

bootstrap-admin creates the first admin account (or activates a password-less one) and refuses to run
while any active admin can already sign in. The password is read without echo (or from stdin with
--password-stdin) and is never printed or logged.
"""

import argparse
import asyncio
import getpass
import sys
from contextlib import asynccontextmanager

import psycopg

from app.admin.repository import AdminRepository, BootstrapRefused
from app.auth.passwords import hash_password, validate_new_password
from app.core.config import Settings
from app.security.audit import security_event

REFUSALS = {
    "admin_exists": "An active admin who can sign in already exists. Use the admin API instead.",
    "account_has_password": "This account already has a password. Sign in with it, or have an admin change its role.",
}


def connection_factory(settings: Settings):
    @asynccontextmanager
    async def connect():
        async with await psycopg.AsyncConnection.connect(settings.db_conninfo(), autocommit=True) as conn:
            yield conn
    return connect


def read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("New admin password: ")
    if first != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match.")
    return first


async def bootstrap_admin(settings: Settings, email: str, password: str) -> int:
    email = (email or "").strip()
    if "@" not in email or len(email) > 254:
        print("A valid email is required.", file=sys.stderr)
        return 2
    problem = validate_new_password(password, settings.password_min_length, settings.password_max_length)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    repo = AdminRepository(connection_factory(settings))
    try:
        user_id = await repo.bootstrap_admin(email, hash_password(password))
    except BootstrapRefused as exc:
        print(REFUSALS[exc.reason], file=sys.stderr)
        return 1
    security_event("admin_bootstrapped", level=20, user_id=user_id)
    print(f"Admin account ready for {email} (database label: {settings.db_label}). Sign in with that email.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__.split("\n\n")[0])
    parser.add_argument("--env-file", help="read HYPERCHAT_* settings from this file (e.g. /etc/hyperchat/api.env)")
    sub = parser.add_subparsers(dest="command", required=True)
    boot = sub.add_parser("bootstrap-admin", help="create the first admin account")
    boot.add_argument("--email", required=True)
    boot.add_argument("--password-stdin", action="store_true", help="read the password from stdin (no prompt)")
    args = parser.parse_args(argv)

    settings = Settings(_env_file=args.env_file) if args.env_file else Settings()
    if not settings.db_enabled:
        print("Database is disabled by configuration.", file=sys.stderr)
        return 2
    return asyncio.run(bootstrap_admin(settings, args.email, read_password(args.password_stdin)))


if __name__ == "__main__":
    raise SystemExit(main())
