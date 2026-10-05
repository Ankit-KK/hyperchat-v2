"""Record the signed-in web user behind dashboard moderation actions.

Revision ID: 0009
Revises: 0008

moderation_actions only had bot-oriented actor columns (moderator_id, moderator_telegram_id,
moderator_name). Dashboard actions (Phase 5) store the session user here. Nullable, no default: no
table rewrite and existing rows keep NULL; adding the foreign key checks the existing rows once (all
NULL) and briefly locks users against writes (bounded by lock_timeout in env.py). Grants come from
the table-level grants (0007).
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE public.moderation_actions ADD COLUMN actor_user_id uuid "
               "REFERENCES public.users(id) ON DELETE SET NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE public.moderation_actions DROP COLUMN IF EXISTS actor_user_id")
