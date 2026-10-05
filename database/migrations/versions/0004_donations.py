"""Normalized donations (replaces the 133 <slug>_donations tables) and donation references.

Revision ID: 0004
Revises: 0003

Column mapping (source -> target), verified on hyperchat_restore_test:
  id (globally unique across all 133 tables)      -> id
  streamer_id (never null, always the owner)       -> streamer_id NOT NULL
  order_id (unique on every row)                   -> order_id UNIQUE
  razorpay_order_id (1 duplicate pair)             -> provider_order_id (indexed, not unique)
  (all rows are Razorpay)                          -> payment_provider = 'razorpay'
  name                                             -> donor_name
  approved_by (UUID = streamers_moderators.id | free text) -> approved_by_moderator_id | approved_by_label
  table name                                       -> legacy_source_table (provenance until cutover)
Dropped, verified empty in every table: temp_voice_data, selected_gif_id,
sapphire.{email,payment_id,signature,media_enabled,tts_enabled}. sapphire.status is dropped too:
always the default 'pending' and never written by any code (the reconciliation asserts this).
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE public.donations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            streamer_id uuid NOT NULL REFERENCES public.streamers(id) ON DELETE RESTRICT,
            order_id text NOT NULL UNIQUE,
            payment_provider text NOT NULL DEFAULT 'razorpay' CHECK (payment_provider IN ('razorpay')),
            provider_order_id text,
            donor_name text NOT NULL,
            amount numeric NOT NULL,
            currency char(3) NOT NULL DEFAULT 'INR' CHECK (currency ~ '^[A-Z]{3}$'),
            amount_inr numeric(12,2) NOT NULL,
            message text,
            message_visible boolean NOT NULL DEFAULT true,
            payment_status text NOT NULL DEFAULT 'pending'
                CHECK (payment_status IN ('pending', 'success', 'failed')),
            moderation_status text NOT NULL DEFAULT 'pending'
                CHECK (moderation_status IN ('pending', 'approved', 'auto_approved', 'rejected')),
            approved_at timestamptz,
            approved_by_moderator_id uuid REFERENCES public.streamers_moderators(id) ON DELETE SET NULL,
            approved_by_label text,
            is_hyperemote boolean NOT NULL DEFAULT false,
            voice_message_url text,
            hypersound_url text,
            media_url text,
            media_type text CHECK (media_type IN ('image', 'gif', 'video')),
            tts_audio_url text,
            audio_scheduled_at timestamptz,
            audio_played_at timestamptz,
            mod_notified boolean NOT NULL DEFAULT false,
            status_token_hash text,
            upload_token_hash text,
            legacy_source_table text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX donations_streamer_created_idx ON public.donations (streamer_id, created_at DESC)")
    op.execute("CREATE INDEX donations_streamer_status_idx ON public.donations "
               "(streamer_id, payment_status, moderation_status)")
    op.execute("CREATE INDEX donations_provider_order_idx ON public.donations (provider_order_id)")
    # Atomic OBS audio queue (claim_next_audio): oldest scheduled, unplayed, paid and approved item per streamer.
    op.execute("""
        CREATE INDEX donations_audio_queue_idx ON public.donations (streamer_id, audio_scheduled_at)
        WHERE audio_played_at IS NULL AND payment_status = 'success'
          AND moderation_status IN ('approved', 'auto_approved')
    """)
    op.execute("CREATE INDEX donations_unnotified_idx ON public.donations (created_at) "
               "WHERE mod_notified = false AND payment_status = 'success'")
    op.execute("CREATE TRIGGER donations_set_updated_at BEFORE UPDATE ON public.donations "
               "FOR EACH ROW EXECUTE FUNCTION public.set_updated_at()")

    op.execute("""
        CREATE TABLE public.order_lookup (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            razorpay_order_id text NOT NULL UNIQUE,
            order_id text NOT NULL,
            donation_id uuid NOT NULL REFERENCES public.donations(id) ON DELETE CASCADE,
            created_at timestamptz DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX order_lookup_created_idx ON public.order_lookup (created_at)")

    # Audit trail: donation_id intentionally has no FK (source has actions for since-deleted donations).
    op.execute("""
        CREATE TABLE public.moderation_actions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            streamer_id uuid NOT NULL REFERENCES public.streamers(id) ON DELETE CASCADE,
            moderator_id uuid REFERENCES public.streamers_moderators(id) ON DELETE SET NULL,
            moderator_telegram_id text,
            moderator_name text,
            donation_id uuid NOT NULL,
            action_type text NOT NULL CHECK (action_type IN ('approve', 'reject', 'hide_message',
                'unhide_message', 'ban_donor', 'unban_donor', 'replay')),
            action_source text NOT NULL CHECK (action_source IN ('telegram', 'dashboard', 'system')),
            previous_status text,
            new_status text,
            notes text,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX moderation_actions_created_idx ON public.moderation_actions (created_at DESC)")
    op.execute("CREATE INDEX moderation_actions_donation_idx ON public.moderation_actions (donation_id)")
    op.execute("CREATE INDEX moderation_actions_streamer_idx ON public.moderation_actions (streamer_id)")
    op.execute("""
        CREATE UNIQUE INDEX moderation_actions_unique_dashboard ON public.moderation_actions
        (donation_id, action_type, action_source, moderator_id)
        WHERE moderator_id IS NOT NULL AND action_source = 'dashboard'
    """)
    op.execute("""
        CREATE UNIQUE INDEX moderation_actions_unique_bot ON public.moderation_actions
        (donation_id, action_type, action_source, moderator_telegram_id)
        WHERE moderator_telegram_id IS NOT NULL AND action_source IN ('telegram', 'discord')
    """)

    for table, short_len in (("telegram_callback_mapping", "varchar(8)"), ("discord_callback_mapping", "text")):
        op.execute(f"""
            CREATE TABLE public.{table} (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                short_id {short_len} NOT NULL UNIQUE,
                donation_id uuid NOT NULL REFERENCES public.donations(id) ON DELETE CASCADE,
                streamer_id uuid NOT NULL REFERENCES public.streamers(id) ON DELETE CASCADE,
                action_type text NOT NULL,
                created_at timestamptz DEFAULT now(),
                expires_at timestamptz NOT NULL DEFAULT (now() + interval '24 hours')
            )
        """)
        op.execute(f"CREATE INDEX {table}_donation_idx ON public.{table} (donation_id)")
        op.execute(f"CREATE INDEX {table}_expires_idx ON public.{table} (expires_at)")

    op.execute("""
        CREATE TABLE public.tts_moderation_audit (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            donation_id uuid,
            streamer_id uuid REFERENCES public.streamers(id) ON DELETE SET NULL,
            supporter_identity_hash text NOT NULL,
            blocked boolean NOT NULL,
            source text,
            category text,
            severity text,
            rule_id text,
            moderation_version text NOT NULL,
            normalization_version text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX tts_moderation_audit_created_idx ON public.tts_moderation_audit (created_at)")
    op.execute("CREATE INDEX tts_moderation_audit_probe_idx ON public.tts_moderation_audit "
               "(supporter_identity_hash, created_at DESC) WHERE blocked")

    op.execute("""
        CREATE TABLE public.streamer_donator_totals (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            streamer_id uuid NOT NULL REFERENCES public.streamers(id) ON DELETE CASCADE,
            donator_name text NOT NULL,
            donator_name_lower text NOT NULL,
            total_amount numeric(12,2) NOT NULL DEFAULT 0,
            donation_count integer NOT NULL DEFAULT 0,
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (streamer_id, donator_name_lower)
        )
    """)
    op.execute("CREATE INDEX streamer_donator_totals_top_idx ON public.streamer_donator_totals "
               "(streamer_id, total_amount DESC) INCLUDE (donator_name)")


def downgrade() -> None:
    for table in ("streamer_donator_totals", "tts_moderation_audit", "discord_callback_mapping",
                  "telegram_callback_mapping", "moderation_actions", "order_lookup", "donations"):
        op.execute(f"DROP TABLE IF EXISTS public.{table}")
