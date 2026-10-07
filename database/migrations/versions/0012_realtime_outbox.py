"""Realtime (Phase 9): the transactional outbox for Pusher events.

Revision ID: 0012
Revises: 0011

* realtime_outbox: one row per realtime event, inserted in the SAME transaction as the database change that
  caused it (payment success, moderation, settings, the Phase 8 audio job). The worker (app.realtime)
  publishes it to Pusher afterwards; Pusher is never called inside a request.
  - ids and small non-PII facts only (subject_id, facts {action, previous_status}); the payload is rendered
    from the current database state at publish time, so no donor name or message is copied here.
  - id (bigint identity) orders events; the publisher keeps per-creator FIFO order.
  - coalesce_key is unique among PENDING rows: a burst of goal / leaderboard / settings changes becomes one
    event carrying the latest state.
  - a publishing row holds a lease (locked_by, locked_until); an expired lease is reclaimed.
  - last_error is a short reason code. Finished rows are deleted 7 days after creation (app.maintenance).

No existing table or row changes. Grants come from the default privileges (0007), including the identity
sequence.
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

CHANNELS = "('dashboard', 'audio', 'settings', 'leaderboard', 'goal')"
EVENTS = ("('new-donation', 'donation-updated', 'audio-ready', 'settings-updated', 'leaderboard-updated', "
          "'goal-progress')")
STATUSES = "('pending', 'publishing', 'published', 'failed', 'expired', 'skipped')"
FINISHED = "('published', 'failed', 'expired', 'skipped')"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE public.realtime_outbox (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            event_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
            streamer_id uuid NOT NULL REFERENCES public.streamers(id) ON DELETE CASCADE,
            channel text NOT NULL CHECK (channel IN {CHANNELS}),
            event text NOT NULL CHECK (event IN {EVENTS}),
            subject_id uuid,
            facts jsonb NOT NULL DEFAULT '{{}}'::jsonb
                CHECK (jsonb_typeof(facts) = 'object' AND pg_column_size(facts) <= 512),
            coalesce_key text CHECK (char_length(coalesce_key) BETWEEN 1 AND 100),
            status text NOT NULL DEFAULT 'pending' CHECK (status IN {STATUSES}),
            attempts smallint NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            max_attempts smallint NOT NULL DEFAULT 6 CHECK (max_attempts BETWEEN 1 AND 20),
            available_at timestamptz NOT NULL DEFAULT now(),
            locked_by text,
            locked_until timestamptz,
            published_at timestamptz,
            last_error text CHECK (char_length(last_error) <= 200),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT realtime_outbox_publishing_has_lease CHECK (
                status <> 'publishing' OR (locked_by IS NOT NULL AND locked_until IS NOT NULL))
        )
    """)
    op.execute("CREATE UNIQUE INDEX realtime_outbox_coalesce ON public.realtime_outbox (coalesce_key) "
               "WHERE status = 'pending'")
    op.execute("CREATE INDEX realtime_outbox_open ON public.realtime_outbox (streamer_id, id) "
               "WHERE status IN ('pending', 'publishing')")
    op.execute("CREATE INDEX realtime_outbox_due ON public.realtime_outbox (available_at, id) WHERE status = 'pending'")
    op.execute("CREATE INDEX realtime_outbox_lease ON public.realtime_outbox (locked_until) "
               "WHERE status = 'publishing'")
    op.execute(f"CREATE INDEX realtime_outbox_finished ON public.realtime_outbox (created_at) "
               f"WHERE status IN {FINISHED}")
    op.execute("CREATE TRIGGER realtime_outbox_set_updated_at BEFORE UPDATE ON public.realtime_outbox "
               "FOR EACH ROW EXECUTE FUNCTION public.set_updated_at()")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.realtime_outbox")
