"""Bot moderation audit: record every applied action, including a repeated one after a reversal.

Revision ID: 0015
Revises: 0014

0004 created moderation_actions_unique_bot, UNIQUE (donation_id, action_type, action_source, moderator_telegram_id)
for Telegram / Discord rows, and the bots insert with ON CONFLICT DO NOTHING. A moderator who rejects, approves and
rejects the same donation again changed it three times but got two audit rows: the third was silently dropped while
the moderator's total_actions still counted it.

The index is not what stops duplicates: a redelivered webhook is dropped by the Telegram update_id / Discord
interaction id nonce, and the same action on a donation already in that state is "unchanged" in the moderation state
machine, which writes nothing (no audit row, no counter). So the index only ever suppressed real, distinct actions.
No query reads by these columns (audit lookups use moderation_actions_donation_idx), so nothing replaces it.

Forward-only for data: no row is changed or removed. downgrade does not recreate the UNIQUE index: rows written
after this migration may legitimately repeat those columns, so it could fail to build, and the previous release's
ON CONFLICT DO NOTHING (no conflict target) works unchanged without it.
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.moderation_actions_unique_bot")


def downgrade() -> None:
    pass  # deliberately not restored (see above)
