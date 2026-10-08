"""Moderation bots (Phase 11): Telegram / Discord notifications and button callbacks.

Revision ID: 0014
Revises: 0013

* jobs.kind CHECK + 'moderator_notify' (one job per newly completed donation, enqueued in the payment-completion
  transaction; historical donations are never enqueued).
* moderation_actions.action_source CHECK + 'discord' (legacy wrote 'discord' against a CHECK that forbade it, so
  Discord moderation was never audited).
* moderator_notifications: one row per donation x recipient (a Telegram DM, a Discord DM or a Discord channel
  post). The UNIQUE key makes the notification job idempotent: a retry only sends what is not 'sent' yet.
  provider_message_id keeps the platform's message id; last_error is a short reason code. No donor text.
* telegram_callback_mapping / discord_callback_mapping (0004, empty): short_id becomes text (16-character CSPRNG
  ids instead of 8), plus recipient_id (the DM chat / Discord channel the buttons were sent to) and moderator_id.

No existing row changes; the previous release ignores the new columns, table and CHECK values.
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

JOB_KINDS_OLD = "('donation_audio', 'voice_transcription')"
JOB_KINDS_NEW = "('donation_audio', 'voice_transcription', 'moderator_notify')"
SOURCES_OLD = "('telegram', 'dashboard', 'system')"
SOURCES_NEW = "('telegram', 'discord', 'dashboard', 'system')"
CALLBACK_TABLES = ("telegram_callback_mapping", "discord_callback_mapping")


def upgrade() -> None:
    op.execute("ALTER TABLE public.jobs DROP CONSTRAINT jobs_kind_check")
    op.execute(f"ALTER TABLE public.jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN {JOB_KINDS_NEW})")
    op.execute("ALTER TABLE public.moderation_actions DROP CONSTRAINT moderation_actions_action_source_check")
    op.execute(f"ALTER TABLE public.moderation_actions ADD CONSTRAINT moderation_actions_action_source_check "
               f"CHECK (action_source IN {SOURCES_NEW})")

    op.execute("""
        CREATE TABLE public.moderator_notifications (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            donation_id uuid NOT NULL REFERENCES public.donations(id) ON DELETE CASCADE,
            streamer_id uuid NOT NULL REFERENCES public.streamers(id) ON DELETE CASCADE,
            moderator_id uuid REFERENCES public.streamers_moderators(id) ON DELETE SET NULL,
            platform text NOT NULL CHECK (platform IN ('telegram', 'discord')),
            target_kind text NOT NULL CHECK (target_kind IN ('dm', 'channel')),
            target_id text NOT NULL CHECK (char_length(target_id) BETWEEN 1 AND 40),
            status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed', 'skipped')),
            attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            provider_message_id text CHECK (char_length(provider_message_id) <= 40),
            last_error text CHECK (char_length(last_error) <= 200),
            sent_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT moderator_notifications_target UNIQUE (donation_id, platform, target_kind, target_id)
        )
    """)
    op.execute("CREATE INDEX moderator_notifications_streamer_idx ON public.moderator_notifications (streamer_id)")
    op.execute("CREATE INDEX moderator_notifications_created_idx ON public.moderator_notifications (created_at)")
    op.execute("CREATE TRIGGER moderator_notifications_set_updated_at BEFORE UPDATE ON public.moderator_notifications "
               "FOR EACH ROW EXECUTE FUNCTION public.set_updated_at()")

    for table in CALLBACK_TABLES:
        op.execute(f"ALTER TABLE public.{table} ALTER COLUMN short_id TYPE text")
        op.execute(f"ALTER TABLE public.{table} ADD COLUMN recipient_id text "
                   f"CHECK (char_length(recipient_id) BETWEEN 1 AND 40)")
        op.execute(f"ALTER TABLE public.{table} ADD COLUMN moderator_id uuid "
                   f"REFERENCES public.streamers_moderators(id) ON DELETE SET NULL")
        op.execute(f"ALTER TABLE public.{table} ADD CONSTRAINT {table}_short_id_len "
                   f"CHECK (char_length(short_id) BETWEEN 1 AND 32)")


def downgrade() -> None:
    for table in CALLBACK_TABLES:
        op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT IF EXISTS {table}_short_id_len")
        op.execute(f"ALTER TABLE public.{table} DROP COLUMN IF EXISTS moderator_id")
        op.execute(f"ALTER TABLE public.{table} DROP COLUMN IF EXISTS recipient_id")
    # Ids longer than the original varchar(8) cannot go back: delete them (callbacks are transient).
    op.execute("DELETE FROM public.telegram_callback_mapping WHERE char_length(short_id) > 8")
    op.execute("ALTER TABLE public.telegram_callback_mapping ALTER COLUMN short_id TYPE varchar(8)")
    op.execute("DROP TABLE IF EXISTS public.moderator_notifications")
    op.execute("DELETE FROM public.moderation_actions WHERE action_source = 'discord'")
    op.execute("ALTER TABLE public.moderation_actions DROP CONSTRAINT moderation_actions_action_source_check")
    op.execute(f"ALTER TABLE public.moderation_actions ADD CONSTRAINT moderation_actions_action_source_check "
               f"CHECK (action_source IN {SOURCES_OLD})")
    op.execute("DELETE FROM public.jobs WHERE kind = 'moderator_notify'")
    op.execute("ALTER TABLE public.jobs DROP CONSTRAINT jobs_kind_check")
    op.execute(f"ALTER TABLE public.jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN {JOB_KINDS_OLD})")
