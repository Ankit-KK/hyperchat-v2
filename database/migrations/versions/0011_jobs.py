"""Background jobs (Phase 8): the durable job queue and stored voice transcripts.

Revision ID: 0011
Revises: 0010

* jobs: PostgreSQL-backed queue claimed with FOR UPDATE SKIP LOCKED by the worker (app.jobs). A running
  job holds a lease (locked_by, locked_until); an expired lease is reclaimed by another worker and counts
  as an attempt. The partial unique index on (kind, dedupe_key) over ACTIVE jobs (queued/running) makes
  enqueueing idempotent while still allowing a new job once the previous one finished. payload holds ids
  only (never donor text, tokens or secrets); last_error is a short reason code.
* donation_transcripts: one Sarvam transcript per donation voice message, written once by the
  voice_transcription job (deleted with its donation).

No existing table or row changes. Grants come from the default privileges (0007).
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

KINDS = "('donation_audio', 'voice_transcription')"
STATUSES = "('queued', 'running', 'succeeded', 'failed')"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE public.jobs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            kind text NOT NULL CHECK (kind IN {KINDS}),
            dedupe_key text NOT NULL CHECK (char_length(dedupe_key) BETWEEN 1 AND 200),
            payload jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
            status text NOT NULL DEFAULT 'queued' CHECK (status IN {STATUSES}),
            attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 20),
            available_at timestamptz NOT NULL DEFAULT now(),
            locked_by text,
            locked_until timestamptz,
            started_at timestamptz,
            completed_at timestamptz,
            last_error text CHECK (char_length(last_error) <= 200),
            result jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT jobs_running_has_lease CHECK (status <> 'running' OR (locked_by IS NOT NULL
                                                                             AND locked_until IS NOT NULL))
        )
    """)
    op.execute("CREATE UNIQUE INDEX jobs_active_dedupe_key ON public.jobs (kind, dedupe_key) "
               "WHERE status IN ('queued', 'running')")
    op.execute("CREATE INDEX jobs_queued_idx ON public.jobs (available_at) WHERE status = 'queued'")
    op.execute("CREATE INDEX jobs_running_lease_idx ON public.jobs (locked_until) WHERE status = 'running'")
    op.execute("CREATE INDEX jobs_finished_idx ON public.jobs (completed_at) WHERE status IN ('succeeded', 'failed')")
    op.execute("CREATE INDEX jobs_kind_dedupe_idx ON public.jobs (kind, dedupe_key, created_at DESC)")
    op.execute("CREATE TRIGGER jobs_set_updated_at BEFORE UPDATE ON public.jobs "
               "FOR EACH ROW EXECUTE FUNCTION public.set_updated_at()")

    op.execute("""
        CREATE TABLE public.donation_transcripts (
            donation_id uuid PRIMARY KEY REFERENCES public.donations(id) ON DELETE CASCADE,
            streamer_id uuid NOT NULL REFERENCES public.streamers(id) ON DELETE CASCADE,
            transcript text NOT NULL CHECK (char_length(transcript) <= 5000),
            provider text NOT NULL CHECK (provider IN ('sarvam')),
            model text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX donation_transcripts_streamer_idx ON public.donation_transcripts (streamer_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.donation_transcripts")
    op.execute("DROP TABLE IF EXISTS public.jobs")
