"""Payments (Phase 7): the Razorpay account of each creator and order, the payment that completed a
donation, and the webhook event log used for idempotency.

Revision ID: 0010
Revises: 0009

* streamers.razorpay_account: 'primary' | 'secondary' (the API's two Razorpay key sets), NULL = no payment
  account (checkout is on hold). Filled from the two legacy secret-name pairs:
      razorpay-keyid / razorpay-keysecret              -> primary
      razorpay-keyid(test) / razorpay-keysecret(test)  -> secondary
      anything else (or NULL)                          -> NULL
  New creators default to primary, as the legacy name defaults did. razorpay_key_ref / razorpay_secret_ref
  are left unchanged (dropped after cutover); the ETL applies the same mapping (etl.mapping).
  The table has ~133 rows: the one UPDATE below only touches creators not on the primary pair.
* donations.payment_account: the account the Razorpay order was created on (NULL on legacy rows: the
  creator's current account applies). donations.provider_payment_id: the Razorpay payment that completed
  the donation, unique when set.
* payment_webhook_events: one row per processed webhook event id. No payload is stored (it carries the
  payer's contact details).

Adding columns with a constant default does not rewrite the tables and the unique index is built once
over donations (all NULL). No other data changes. Grants come from the default privileges (0007).

Downgrade drops exactly these objects; the legacy name columns were never changed, so nothing is lost
except the Phase 7 payment data (event log, payment ids, order accounts).
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

ACCOUNTS = "('primary', 'secondary')"
OUTCOMES = ("completed", "failed", "already_paid", "already_failed", "ignored", "no_order", "unknown_order",
            "account_mismatch", "amount_mismatch", "currency_mismatch", "payment_mismatch", "payment_not_found")


def upgrade() -> None:
    op.execute(f"ALTER TABLE public.streamers ADD COLUMN razorpay_account text DEFAULT 'primary' "
               f"CONSTRAINT streamers_razorpay_account_check CHECK (razorpay_account IN {ACCOUNTS})")
    op.execute("""
        UPDATE public.streamers SET razorpay_account = CASE
            WHEN (razorpay_key_ref, razorpay_secret_ref) = ('razorpay-keyid(test)', 'razorpay-keysecret(test)')
                THEN 'secondary'
            ELSE NULL END
        WHERE (razorpay_key_ref, razorpay_secret_ref) IS DISTINCT FROM ('razorpay-keyid', 'razorpay-keysecret')
    """)

    op.execute(f"ALTER TABLE public.donations ADD COLUMN payment_account text "
               f"CONSTRAINT donations_payment_account_check CHECK (payment_account IN {ACCOUNTS})")
    op.execute("ALTER TABLE public.donations ADD COLUMN provider_payment_id text")
    op.execute("CREATE UNIQUE INDEX donations_provider_payment_key ON public.donations (provider_payment_id) "
               "WHERE provider_payment_id IS NOT NULL")

    outcomes = ", ".join(f"'{o}'" for o in OUTCOMES)
    op.execute(f"""
        CREATE TABLE public.payment_webhook_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            provider text NOT NULL DEFAULT 'razorpay' CHECK (provider IN ('razorpay')),
            event_id text NOT NULL,
            event_type text NOT NULL,
            account text NOT NULL CHECK (account IN {ACCOUNTS}),
            provider_order_id text,
            provider_payment_id text,
            donation_id uuid REFERENCES public.donations(id) ON DELETE SET NULL,
            outcome text NOT NULL CHECK (outcome IN ({outcomes})),
            received_at timestamptz NOT NULL,
            processed_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT payment_webhook_events_event_key UNIQUE (provider, event_id)
        )
    """)
    op.execute("CREATE INDEX payment_webhook_events_received_idx ON public.payment_webhook_events (received_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.payment_webhook_events")
    op.execute("DROP INDEX IF EXISTS public.donations_provider_payment_key")
    op.execute("ALTER TABLE public.donations DROP COLUMN IF EXISTS provider_payment_id")
    op.execute("ALTER TABLE public.donations DROP COLUMN IF EXISTS payment_account")
    op.execute("ALTER TABLE public.streamers DROP COLUMN IF EXISTS razorpay_account")
