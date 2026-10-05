"""Platform tables: replay protection, rate limits, alerts, signups, visits, reviews, queries, contact.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE public.request_nonces (
            nonce text PRIMARY KEY,
            user_id uuid REFERENCES public.users(id) ON DELETE CASCADE,
            expires_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX request_nonces_expires_at_idx ON public.request_nonces (expires_at)")

    op.execute("""
        CREATE TABLE public.rate_limits (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            ip_address text NOT NULL,
            endpoint text NOT NULL,
            request_count integer DEFAULT 1,
            window_start timestamptz DEFAULT now(),
            created_at timestamptz DEFAULT now(),
            UNIQUE (ip_address, endpoint)
        )
    """)
    op.execute("CREATE INDEX rate_limits_cleanup_idx ON public.rate_limits (created_at)")

    op.execute("""
        CREATE TABLE public.system_alerts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            source text NOT NULL,
            severity text NOT NULL CHECK (severity IN ('info', 'warning', 'error', 'critical')),
            message text NOT NULL,
            context jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            notified_at timestamptz
        )
    """)
    op.execute("CREATE INDEX system_alerts_source_created_idx ON public.system_alerts (source, created_at DESC)")
    op.execute("CREATE INDEX system_alerts_unnotified_idx ON public.system_alerts (created_at DESC) "
               "WHERE notified_at IS NULL")

    op.execute("""
        CREATE TABLE public.user_signups (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL,
            mobile_number text NOT NULL,
            youtube_channel text,
            email text NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now(),
            instagram_handle text
        )
    """)
    op.execute("""
        CREATE TABLE public.visits (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            ip_address text NOT NULL UNIQUE,
            visited_at timestamptz DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE public.reviews (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL,
            content text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE public.user_queries (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid REFERENCES public.users(id) ON DELETE SET NULL,
            query_text text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    # Missing in the source (submit-contact writes name, email, phone, message to it and fails today).
    op.execute("""
        CREATE TABLE public.contact_submissions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL CHECK (length(name) <= 100),
            email text NOT NULL CHECK (length(email) <= 254),
            phone text,
            message text NOT NULL CHECK (length(message) <= 1000),
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    for table in ("contact_submissions", "user_queries", "reviews", "visits", "user_signups", "system_alerts",
                  "rate_limits", "request_nonces"):
        op.execute(f"DROP TABLE IF EXISTS public.{table}")
