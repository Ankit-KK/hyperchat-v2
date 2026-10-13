"""Retire the 'secondary' Razorpay account (owner decision D6): one logical account, 'primary'; test vs live is
the environment's job (DEV = test-mode keys, production = live keys; the app refuses the wrong mode at startup).

Revision ID: 0018
Revises: 0017

1. Safety check first: if any donation was created on 'secondary' (donations.payment_account), stop with an error
   and change nothing. Such an order belongs to another key set and must be reviewed by hand; it is never rewritten.
   (Expected: none. 0010 added the column empty, legacy rows stay NULL, and 'secondary' was never configured.)
2. streamers.razorpay_account 'secondary' -> 'primary'. In the legacy data this is one creator (the owner's testing
   page, whose "(test)" key names held Razorpay test-mode keys). NULL (no payment account) stays NULL.
3. Both CHECK constraints are narrowed to 'primary'. NULL still passes them: legacy orders, which never recorded an
   account, keep NULL (the creator's account applies).

Small tables (~133 creators, ~14k donations): each constraint is checked once under a short lock, bounded by
lock_timeout in env.py. The previous release still works: it only ever writes 'primary' (secondary is unconfigured
everywhere).

Downgrade widens the constraints back; it cannot know which creator was on 'secondary', so it remaps nothing.
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def _check(table: str, column: str, constraint: str, accounts: str) -> None:
    op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT {constraint}, "
               f"ADD CONSTRAINT {constraint} CHECK ({column} IN {accounts})")


def upgrade() -> None:
    op.execute("""
        DO $$
        DECLARE n bigint;
        BEGIN
            SELECT count(*) INTO n FROM public.donations WHERE payment_account = 'secondary';
            IF n > 0 THEN
                RAISE EXCEPTION 'migration 0018 stopped: % donation(s) were created on the secondary Razorpay account; '
                                'review them by hand before retiring it (nothing was changed)', n;
            END IF;
        END $$
    """)
    op.execute("UPDATE public.streamers SET razorpay_account = 'primary' WHERE razorpay_account = 'secondary'")
    _check("streamers", "razorpay_account", "streamers_razorpay_account_check", "('primary')")
    _check("donations", "payment_account", "donations_payment_account_check", "('primary')")


def downgrade() -> None:
    _check("donations", "payment_account", "donations_payment_account_check", "('primary', 'secondary')")
    _check("streamers", "razorpay_account", "streamers_razorpay_account_check", "('primary', 'secondary')")
