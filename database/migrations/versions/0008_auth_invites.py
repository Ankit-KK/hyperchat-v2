"""Invite-only onboarding: user_invites.

Revision ID: 0008
Revises: 0007

Replaces the manual "create user in the Supabase dashboard + insert auth_users row" procedure
(docs/ADD_NEW_STREAMER.md step 16). Only the SHA-256 of the invite token is stored.
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE public.user_invites (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email citext NOT NULL,
            role text NOT NULL DEFAULT 'streamer' CHECK (role IN ('streamer', 'admin', 'user')),
            streamer_id uuid REFERENCES public.streamers(id) ON DELETE CASCADE,
            token_hash text NOT NULL UNIQUE,
            invited_by uuid REFERENCES public.users(id) ON DELETE SET NULL,
            expires_at timestamptz NOT NULL,
            accepted_at timestamptz,
            accepted_user_id uuid REFERENCES public.users(id) ON DELETE SET NULL,
            revoked_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX user_invites_email_idx ON public.user_invites (email)")
    # Grants for the new table come from the default privileges set in 0007.


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.user_invites")
