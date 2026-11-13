"""PAY-15 (D17, PAY15-D9 / risk R6) stage 5b: the payment's refunded-amount snapshot on integrity evidence.

Revision ID: 0033
Revises: 0032

Every signed dispute / refund webhook carries the payment entity, whose `amount_refunded` is Razorpay's cumulative
refunded amount for that payment at the event. Stored here, validated by app/payments/integrity.py (the payment
entity is the evidence's own payment, a non-negative integer not above the payment amount, in the evidence currency;
otherwise NULL, never a guessed 0), it lets payment-integrity-report flag a payment for which Razorpay reports more
refunded than our recorded refund evidence: a likely missed refund webhook.

* payment_integrity_events.payment_amount_refunded_paise  bigint, nullable, CHECK >= 0. Existing rows stay NULL (no
  default, no rewrite, no backfill). The 0029 append-only guard is unchanged (ADD COLUMN is DDL; the guard refuses
  UPDATE / DELETE / TRUNCATE). Table-level grants cover the column (app SELECT + INSERT, readonly SELECT, worker
  nothing). The view payment_integrity_current is NOT changed: its explicit column list leaves the snapshot out of
  every money path (evaluator, donation_accounting_apply, the credit writer, the live figures). Diagnostic only.

Downgrade refuses while any row holds a snapshot (dropping the column would destroy evidence); otherwise it drops
the constraint and the column.
"""

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE public.payment_integrity_events
            ADD COLUMN payment_amount_refunded_paise bigint,
            ADD CONSTRAINT payment_integrity_refund_snapshot CHECK (payment_amount_refunded_paise >= 0)
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.payment_integrity_events WHERE payment_amount_refunded_paise IS NOT NULL) THEN
                RAISE EXCEPTION 'downgrade of 0033 stopped: % evidence row(s) hold a refund snapshot (nothing was '
                                'changed)', (SELECT count(*) FROM public.payment_integrity_events
                                             WHERE payment_amount_refunded_paise IS NOT NULL);
            END IF;
        END $$
    """)
    op.execute("ALTER TABLE public.payment_integrity_events DROP CONSTRAINT payment_integrity_refund_snapshot, "
               "DROP COLUMN payment_amount_refunded_paise")
