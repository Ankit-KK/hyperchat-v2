"""Operator commands, run on the server (never exposed over HTTP).

    python -m app.cli --env-file /etc/hyperchat/api.env bootstrap-admin --email you@example.com
    python -m app.cli --env-file /etc/hyperchat/api.env cleanup [--dry-run]
    python -m app.cli --env-file /etc/hyperchat/api.env payment-health

bootstrap-admin creates the first admin account as a NEW account (an existing account with the address is
refused) and refuses to run while any active admin can already sign in. The password is read without echo
(or from stdin with --password-stdin) and is never printed or logged.
cleanup deletes expired sessions, reset tokens, nonces, rate-limit windows, old unused invites, finished
jobs (30 days) and TTS moderation audit rows (90 days), then donor uploads that no donation uses after 24
hours when R2 is configured (D9) (see app.maintenance); --dry-run only counts. It runs
daily from deploy/systemd/hyperchat-cleanup.timer.

enqueue-donation-audio <donation id> queues one donation's audio job for the worker (manual/controlled cases;
historical donations are never enqueued automatically).

bots-set-telegram-webhook --url URL --expect-bot USERNAME and bots-set-discord-endpoint --url URL
--expect-application-id ID register this environment's bot webhooks (Phase 11). Both refuse unless the bot behind
the configured token is exactly the one named (so the legacy production bot / application can never be re-pointed
by mistake, D14) and the URL is this API's https webhook path. Tokens and secrets are never printed.

seed-platform-soundboard [--dry-run] fills the empty platform soundboard from this environment's R2 library
(hypersounds/ + hypersound-thumbs/, see app.soundboard.seed); it does nothing once any platform slot exists.
--dry-run lists what would be inserted and writes nothing.
"""

import argparse
import asyncio
import getpass
import sys
import uuid
from contextlib import asynccontextmanager

import psycopg

from app.admin.repository import AdminRepository, BootstrapRefused
from app.bots.clients import DiscordClient, TelegramClient
from app.auth.passwords import hash_password, validate_new_password
from app.core.config import Settings
from app.core.logging import configure_logging
from app.jobs.queue import enqueue
from app.maintenance import run_cleanup
from app.payments.accounts import AccountHealth, PaymentAccounts, health_report
from app.payments.razorpay import RazorpayClient
from app.security.audit import security_event
from app.soundboard import seed
from app.storage.r2 import StorageError, build_object_store

REFUSALS = {
    "admin_exists": "An active admin who can sign in already exists. Use the admin API instead.",
    "account_exists": "An account with this email already exists. Bootstrap only creates a new account: use another "
                      "email, or send an admin invitation from the admin API.",
}


def connection_factory(settings: Settings, role: str | None = None):
    @asynccontextmanager
    async def connect():
        async with await psycopg.AsyncConnection.connect(settings.db_conninfo(role=role), autocommit=True) as conn:
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
    repo = AdminRepository(connection_factory(settings),  # migration 0024: admins are written by hyperchat_auth
                           auth_factory=connection_factory(settings, "auth") if settings.db_auth_user else None)
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


def _webhook_url(url: str, path: str) -> bool:
    return url.startswith("https://") and url.endswith(path) and "?" not in url and "#" not in url


async def set_telegram_webhook(settings: Settings, url: str, expect_bot: str) -> int:
    if not settings.telegram_configured:
        print("HYPERCHAT_TELEGRAM_BOT_TOKEN and HYPERCHAT_TELEGRAM_WEBHOOK_SECRET must be set.", file=sys.stderr)
        return 2
    if not _webhook_url(url, "/api/v1/webhooks/telegram"):
        print("The URL must be https://<api host>/api/v1/webhooks/telegram.", file=sys.stderr)
        return 2
    client = TelegramClient(settings.telegram_bot_token.get_secret_value().strip())
    me = await client.get_me()
    username = str((me or {}).get("username") or "")
    if username.lower() != expect_bot.lstrip("@").lower():
        print(f"Refusing: the configured token belongs to @{username}, not @{expect_bot.lstrip('@')}.", file=sys.stderr)
        return 1
    await client.set_webhook(url, settings.telegram_webhook_secret.get_secret_value().strip())
    security_event("bot_webhook_registered", level=20, reason="telegram")
    print(f"Telegram webhook of @{username} set to {url} (with its secret token).")
    return 0


async def set_discord_endpoint(settings: Settings, url: str, expect_application_id: str) -> int:
    if not settings.discord_configured:
        print("HYPERCHAT_DISCORD_BOT_TOKEN and HYPERCHAT_DISCORD_PUBLIC_KEY must be set.", file=sys.stderr)
        return 2
    if not _webhook_url(url, "/api/v1/webhooks/discord"):
        print("The URL must be https://<api host>/api/v1/webhooks/discord.", file=sys.stderr)
        return 2
    client = DiscordClient(settings.discord_bot_token.get_secret_value().strip())
    app_id = str((await client.get_application()).get("id") or "")
    if app_id != expect_application_id.strip():
        print(f"Refusing: the configured token belongs to application {app_id}, not {expect_application_id}.",
              file=sys.stderr)
        return 1
    await client.set_interactions_endpoint(url)
    security_event("bot_webhook_registered", level=20, reason="discord")
    print(f"Discord interactions endpoint of application {app_id} set to {url}.")
    return 0


async def seed_platform_soundboard(settings: Settings, dry_run: bool, store=None) -> int:
    store = store or build_object_store(settings)
    if store is None:
        print("R2 storage is not configured (HYPERCHAT_R2_* variables).", file=sys.stderr)
        return 2
    connect = connection_factory(settings)
    existing = await seed.existing_slots(connect)
    if existing:
        print(f"Nothing to do: {existing} platform soundboard slots already exist "
              f"(database label: {settings.db_label}).")
        return 0
    try:
        rows, unpaired = seed.plan(await store.list_keys(seed.SOUNDS), await store.list_keys(seed.THUMBS),
                                   store.public_url)
    except StorageError:
        print("Could not list the R2 sound library.", file=sys.stderr)
        return 1
    for key in unpaired:
        print(f"skipped (no thumbnail): {key}")
    for row in rows:
        print(f"{row['default_order_index']:2d}  {row['display_name']}  {row['audio_key']}  {row['image_key']}")
    if not rows:
        print("Refusing: no sound has a matching thumbnail.", file=sys.stderr)
        return 1
    if len(rows) > seed.MAX_SLOTS:
        print(f"Refusing: {len(rows)} paired sounds, but the platform soundboard has {seed.MAX_SLOTS} slots.",
              file=sys.stderr)
        return 1
    if dry_run:
        print(f"Dry run: would insert {len(rows)} platform slots (database label: {settings.db_label}).")
        return 0
    existing = await seed.insert_if_empty(connect, rows)
    if existing:
        print(f"Nothing inserted: {existing} platform soundboard slots appeared meanwhile.")
        return 0
    security_event("platform_soundboard_seeded", level=20, count=len(rows))
    print(f"Inserted {len(rows)} platform soundboard slots (database label: {settings.db_label}).")
    return 0


async def payment_health(settings: Settings) -> int:
    """D7: one credential probe of the Razorpay account (GET /payments?count=1: no order, no payment), for cutover
    and key-rotation checks over SSH. Prints names and states only. Exit 0 when usable, 1 otherwise."""
    report = await health_report(PaymentAccounts.from_settings(settings), AccountHealth(RazorpayClient()))
    print(" ".join(f"{k}={v}" for k, v in report.items()))
    return 0 if report["status"] == "ok" else 1


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
    tg = sub.add_parser("bots-set-telegram-webhook", help="register this environment's Telegram bot webhook")
    tg.add_argument("--url", required=True)
    tg.add_argument("--expect-bot", required=True, help="username of the bot the configured token must belong to")
    dc = sub.add_parser("bots-set-discord-endpoint", help="register this environment's Discord interactions URL")
    dc.add_argument("--url", required=True)
    dc.add_argument("--expect-application-id", required=True)
    sb = sub.add_parser("seed-platform-soundboard", help="fill the empty platform soundboard from the R2 library")
    sb.add_argument("--dry-run", action="store_true", help="only list what would be inserted")
    sub.add_parser("payment-health", help="check the Razorpay credentials, no order created (exit 1 if unusable)")
    args = parser.parse_args(argv)

    settings = Settings(_env_file=args.env_file) if args.env_file else Settings()
    # Same JSON-lines logging as the API, so audit events (maintenance_cleanup, admin_bootstrapped)
    # reach stdout -> journald when run from systemd.
    configure_logging(settings.log_level)
    if args.command == "bots-set-telegram-webhook":
        return asyncio.run(set_telegram_webhook(settings, args.url, args.expect_bot))
    if args.command == "bots-set-discord-endpoint":
        return asyncio.run(set_discord_endpoint(settings, args.url, args.expect_application_id))
    if args.command == "payment-health":
        return asyncio.run(payment_health(settings))
    if not settings.db_enabled:
        print("Database is disabled by configuration.", file=sys.stderr)
        return 2
    if args.command == "cleanup":
        return asyncio.run(run_cleanup(settings, connection_factory(settings), dry_run=args.dry_run))
    if args.command == "seed-platform-soundboard":
        return asyncio.run(seed_platform_soundboard(settings, args.dry_run))
    if args.command == "enqueue-donation-audio":
        return asyncio.run(enqueue_donation_audio(settings, args.donation_id))
    return asyncio.run(bootstrap_admin(settings, args.email, read_password(args.password_stdin)))


if __name__ == "__main__":
    raise SystemExit(main())
