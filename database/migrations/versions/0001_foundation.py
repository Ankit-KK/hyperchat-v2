"""Foundation: extensions, shared trigger function, enum types.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    # One updated_at trigger function replaces the five identical copies in the source schema
    # (update_updated_at_column, update_updated_at_column_sbtnc, update_platform_voice_tiers_updated_at,
    # touch_soundboard_updated_at). Same body: NEW.updated_at = now().
    op.execute("""
        CREATE FUNCTION public.set_updated_at() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public AS $$
        BEGIN
          NEW.updated_at = now();
          RETURN NEW;
        END;
        $$
    """)

    # Enum types copied from the source schema (same labels, same order).
    op.execute("CREATE TYPE public.moderator_role AS ENUM ('owner', 'moderator', 'viewer')")
    op.execute("CREATE TYPE public.soundboard_image_type AS ENUM ('gif', 'png', 'jpg', 'webp')")
    op.execute("CREATE TYPE public.soundboard_slot_source AS ENUM ('platform', 'custom')")


def downgrade() -> None:
    op.execute("DROP TYPE IF EXISTS public.soundboard_slot_source")
    op.execute("DROP TYPE IF EXISTS public.soundboard_image_type")
    op.execute("DROP TYPE IF EXISTS public.moderator_role")
    op.execute("DROP FUNCTION IF EXISTS public.set_updated_at()")
    op.execute("DROP EXTENSION IF EXISTS citext")
    op.execute("DROP EXTENSION IF EXISTS pgcrypto")
