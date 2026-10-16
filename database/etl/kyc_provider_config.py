"""Set this database's Didit application for KYC webhooks (migration 0022; docs/kyc.md).

The webhook secret is stored only in public.kyc_provider_config, which no runtime role can read: the database verifies
every Didit webhook itself, so the API process never holds the secret. Run by the operator with the migration DSN;
the secret is read from the terminal without echo (or one line of stdin with --secret-stdin), never from argv.

    HYPERCHAT_MIGRATION_DSN=... python -m etl.kyc_provider_config --environment sandbox --workflow-id <uuid> \
        [--application-id <uuid>]
"""

import argparse
import getpass
import os
import sys
import uuid

import psycopg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--environment", choices=("sandbox", "live"), required=True)
    parser.add_argument("--workflow-id", type=uuid.UUID, required=True)
    parser.add_argument("--application-id", type=uuid.UUID)
    parser.add_argument("--secret-stdin", action="store_true", help="read the secret from one line of stdin")
    args = parser.parse_args(argv)
    secret = (sys.stdin.readline() if args.secret_stdin else getpass.getpass("Didit webhook secret: ")).strip()
    if not 16 <= len(secret) <= 512:
        print("Refused: the webhook secret must be 16-512 characters.", file=sys.stderr)
        return 2
    dsn = os.environ["HYPERCHAT_MIGRATION_DSN"].replace("postgresql+psycopg://", "postgresql://", 1)  # as alembic
    with psycopg.connect(dsn) as conn:
        conn.execute("SET ROLE hyperchat_owner")
        conn.execute("""
            INSERT INTO public.kyc_provider_config (environment, workflow_id, application_id, webhook_secret)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (singleton) DO UPDATE SET environment = EXCLUDED.environment,
                workflow_id = EXCLUDED.workflow_id, application_id = EXCLUDED.application_id,
                webhook_secret = EXCLUDED.webhook_secret, updated_at = now()
        """, (args.environment, args.workflow_id, args.application_id, secret))
    print(f"Didit {args.environment} configuration saved (workflow {args.workflow_id}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
