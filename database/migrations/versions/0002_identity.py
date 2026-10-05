"""Identity: users (from auth_users), Google identities, sessions, password reset tokens.

Revision ID: 0002
Revises: 0001

users.id keeps the legacy auth_users.id: every existing FK (streamers.user_id, payout_accounts,
terms_acceptances, soundboard T&C) already points at it. Passwords are NOT migrated here;
current hashes are imported from Supabase Auth at cutover (password_hash stays nullable).
admin_emails is folded into users.role = 'admin' by the data migration.
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE public.users (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email citext NOT NULL UNIQUE,
            password_hash text,
            username text,
            role text NOT NULL DEFAULT 'user' CHECK (role IN ('streamer', 'admin', 'user')),
            streamer_id uuid,
            is_active boolean NOT NULL DEFAULT true,
            failed_login_attempts integer NOT NULL DEFAULT 0,
            locked_until timestamptz,
            legacy_supabase_user_id uuid UNIQUE,
            email_verified_at timestamptz,
            password_changed_at timestamptz,
            last_login_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE UNIQUE INDEX users_username_ci_key ON public.users (lower(username)) WHERE username IS NOT NULL")
    op.execute("CREATE INDEX users_locked_until_idx ON public.users (locked_until) WHERE locked_until IS NOT NULL")
    op.execute("CREATE TRIGGER users_set_updated_at BEFORE UPDATE ON public.users "
               "FOR EACH ROW EXECUTE FUNCTION public.set_updated_at()")

    # External login identities (Google OAuth). Email/password lives on users itself.
    op.execute("""
        CREATE TABLE public.user_identities (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
            provider text NOT NULL CHECK (provider IN ('google')),
            provider_subject text NOT NULL,
            email citext,
            last_login_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (provider, provider_subject)
        )
    """)
    op.execute("CREATE INDEX user_identities_user_idx ON public.user_identities (user_id)")

    # Server-side browser sessions (opaque token -> SHA-256 hash stored), cookie + CSRF.
    op.execute("""
        CREATE TABLE public.sessions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
            token_hash text NOT NULL UNIQUE,
            csrf_token_hash text NOT NULL,
            ua_hash text,
            ip_address inet,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            idle_expires_at timestamptz NOT NULL,
            absolute_expires_at timestamptz NOT NULL,
            revoked_at timestamptz
        )
    """)
    op.execute("CREATE INDEX sessions_user_active_idx ON public.sessions (user_id) WHERE revoked_at IS NULL")
    op.execute("CREATE INDEX sessions_expiry_idx ON public.sessions (absolute_expires_at)")

    op.execute("""
        CREATE TABLE public.password_reset_tokens (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
            token_hash text NOT NULL UNIQUE,
            expires_at timestamptz NOT NULL,
            used_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX password_reset_tokens_user_idx ON public.password_reset_tokens (user_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.password_reset_tokens")
    op.execute("DROP TABLE IF EXISTS public.sessions")
    op.execute("DROP TABLE IF EXISTS public.user_identities")
    op.execute("DROP TABLE IF EXISTS public.users")
