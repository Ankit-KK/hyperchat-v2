"""Record who lifted a donor ban, and when (dashboard ban list + unban by id, D-R8 / D-M6).

Revision ID: 0016
Revises: 0015

An unban deactivates the creator's active ban(s) of a donor name (is_active = false); the row is kept as history.
moderation_actions cannot hold it (its donation_id is NOT NULL and an unban by id concerns a donor, not a
donation), so the ban row itself records it:

  unbanned_at          when the ban was lifted
  unbanned_by_label    the dashboard label (username or "Dashboard"), as banned_by_name
  unbanned_by_user_id  the signed-in user (ON DELETE SET NULL, as moderation_actions.actor_user_id)

Nullable, no default: no table rewrite and existing rows keep NULL; the foreign key checks the existing rows once
(all NULL) and briefly locks users against writes (bounded by lock_timeout in env.py). Grants come from the
table-level grants (0007). The previous release ignores the columns.
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE public.banned_donors ADD COLUMN unbanned_at timestamptz, "
               "ADD COLUMN unbanned_by_label text CHECK (char_length(unbanned_by_label) <= 100), "
               "ADD COLUMN unbanned_by_user_id uuid REFERENCES public.users(id) ON DELETE SET NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE public.banned_donors DROP COLUMN IF EXISTS unbanned_by_user_id, "
               "DROP COLUMN IF EXISTS unbanned_by_label, DROP COLUMN IF EXISTS unbanned_at")
