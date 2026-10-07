"""Operator commands, run on the server (never exposed over HTTP).

    python -m app.cli --env-file /etc/hyperchat/api.env bootstrap-admin --email you@example.com
    python -m app.cli --env-file /etc/hyperchat/api.env cleanup [--dry-run]

bootstrap-admin creates the first admin account (or activates a password-less one) and refuses to run
while any active admin can already sign in. The password is read without echo (or from stdin with
--password-stdin) and is never printed or logged.
cleanup deletes expired sessions, reset tokens, nonces, rate-limit windows, old unused invites, finished
jobs (30 days) and TTS moderation audit rows (90 days) (see app.maintenance); --dry-run only counts. It runs
daily from deploy/systemd/hyperchat-cleanup.timer.

enqueue-donation-audio <donation id> queues one donation's audio job for the worker (manual/controlled cases;
historical donations are never enqueued automatically).
"""

import argparse
import asyncio
import getpass
import sys
import uuid
from contextlib import asynccontextmanager

import psycopg

from app.admin.repository import AdminRepository, BootstrapRefused
from app.auth.passwords import hash_password, validate_new_password
from app.core.config import Settings
from app.core.logging import configure_logging
from app.jobs.queue import enqueue
from app.maintenance import run_cleanup
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


async def enqueue_donation_audio(settings: Settings, donation_id: str) -> int:
    """Controlled/manual audio job for one donation (never enqueued automatically for historical rows). The
    worker re-checks eligibility (paid, approved, no audio yet) when it runs the job."""
    try:
        donation_id = str(uuid.UUID(donation_id))
    except (ValueError, AttributeError):
        print("A donation UUID is required.", file=sys.stderr)
        return 2
    async with connection_factory(settings)() as conn, conn.transaction(), conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM public.donations WHERE id = %s", (donation_id,))
        if await cur.fetchone() is None:
            print("No such donation.", file=sys.stderr)
            return 1
        job_id = await enqueue(cur, "donation_audio", donation_id, {"donation_id": donation_id})
    if job_id is None:
        print(f"An audio job for donation {donation_id} is already queued or running.")
    else:
        security_event("audio_job_enqueued", level=20, reason="cli")
        print(f"Queued audio job {job_id} for donation {donation_id} (database label: {settings.db_label}).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__.split("\n\n")[0])
    parser.add_argument("--env-file", help="read HYPERCHAT_* settings from this file (e.g. /etc/hyperchat/api.env)")
    sub = parser.add_subparsers(dest="command", required=True)
    boot = sub.add_parser("bootstrap-admin", help="create the first admin account")
    boot.add_argument("--email", required=True)
    boot.add_argument("--password-stdin", action="store_true", help="read the password from stdin (no prompt)")
    clean = sub.add_parser("cleanup", help="delete expired sessions, tokens, nonces and rate-limit windows")
    clean.add_argument("--dry-run", action="store_true", help="only count what would be deleted")
    audio = sub.add_parser("enqueue-donation-audio", help="queue the audio job of one donation (manual cases)")
    audio.add_argument("donation_id")
    args = parser.parse_args(argv)

    settings = Settings(_env_file=args.env_file) if args.env_file else Settings()
    # Same JSON-lines logging as the API, so audit events (maintenance_cleanup, admin_bootstrapped)
    # reach stdout -> journald when run from systemd.
    configure_logging(settings.log_level)
    if not settings.db_enabled:
        print("Database is disabled by configuration.", file=sys.stderr)
        return 2
    if args.command == "cleanup":
        return asyncio.run(run_cleanup(settings, connection_factory(settings), dry_run=args.dry_run))
    if args.command == "enqueue-donation-audio":
        return asyncio.run(enqueue_donation_audio(settings, args.donation_id))
    return asyncio.run(bootstrap_admin(settings, args.email, read_password(args.password_stdin)))


if __name__ == "__main__":
    raise SystemExit(main())
