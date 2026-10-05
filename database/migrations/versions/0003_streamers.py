"""Streamers and everything owned by a streamer (except donations).

Revision ID: 0003
Revises: 0002

Columns, defaults, CHECKs and indexes are copied from the restored source schema
(hyperchat_restore_test). Changes, all verified against the data first:
  * streamers.donation_table_id dropped (one donations table now);
  * "razorpay-keyid"/"razorpay-keysecret" renamed razorpay_key_ref/razorpay_secret_ref
    (they hold the NAME of a credential set, not a secret);
  * obs_tokens stores token_hash (SHA-256 hex) instead of the plaintext token;
  * new FKs where the source relied on convention (0 dangling rows in the source data):
    streamers.user_id, users.streamer_id, slug-keyed soundboard tables, T&C user_id.
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE public.streamers (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid REFERENCES public.users(id) ON DELETE SET NULL,
            streamer_slug text NOT NULL UNIQUE,
            streamer_name text NOT NULL,
            brand_color text DEFAULT '#3b82f6',
            brand_logo_url text,
            hyperemotes_enabled boolean DEFAULT true,
            hyperemotes_min_amount numeric DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            pusher_group integer DEFAULT 1 CHECK (pusher_group >= 1 AND pusher_group <= 10),
            goal_name text,
            goal_target_amount numeric,
            goal_activated_at timestamptz,
            goal_is_active boolean DEFAULT false,
            alert_box_scale numeric(3,2) DEFAULT 1.0,
            tts_voice_id text DEFAULT 'hindi_female_1_v2',
            tts_enabled boolean DEFAULT true,
            telegram_moderation_enabled boolean DEFAULT true,
            moderation_mode text DEFAULT 'auto_approve' CHECK (moderation_mode IN ('auto_approve', 'manual')),
            leaderboard_widget_enabled boolean DEFAULT true,
            media_upload_enabled boolean DEFAULT false,
            media_moderation_enabled boolean DEFAULT true,
            media_min_amount numeric DEFAULT 100,
            media_max_file_size_mb numeric DEFAULT 10,
            min_text_amount_inr numeric,
            min_tts_amount_inr numeric,
            min_voice_amount_inr numeric,
            min_hypersound_amount_inr numeric,
            tts_volume integer DEFAULT 5 CHECK (tts_volume >= 0 AND tts_volume <= 10),
            tts_language_boost text DEFAULT 'Hindi',
            message_char_tiers jsonb,
            discord_moderation_enabled boolean DEFAULT false,
            alert_overlay_id smallint NOT NULL DEFAULT 0,
            leaderboard_overlay_id smallint NOT NULL DEFAULT 0,
            overlay_adjust_mode boolean NOT NULL DEFAULT false,
            leaderboard_bg_enabled boolean NOT NULL DEFAULT true,
            order_prefix text UNIQUE,
            background_url text,
            custom_page_path text,
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('pending', 'active', 'rejected')),
            rejection_reason text,
            api_enabled boolean NOT NULL DEFAULT false,
            el_voice_id text,
            razorpay_key_ref text DEFAULT 'razorpay-keyid',
            razorpay_secret_ref text DEFAULT 'razorpay-keysecret',
            discord_moderation_channel_id text,
            soundboard_enabled boolean NOT NULL DEFAULT false,
            goal_overlay_id smallint NOT NULL DEFAULT 0 CHECK (goal_overlay_id >= 0 AND goal_overlay_id <= 6)
        )
    """)
    op.execute("CREATE INDEX streamers_status_idx ON public.streamers (status)")
    op.execute("CREATE INDEX streamers_pusher_group_idx ON public.streamers (pusher_group)")
    op.execute("CREATE INDEX streamers_discord_channel_idx ON public.streamers (discord_moderation_channel_id) "
               "WHERE discord_moderation_channel_id IS NOT NULL")
    op.execute("CREATE INDEX streamers_user_idx ON public.streamers (user_id)")
    op.execute("ALTER TABLE public.users ADD CONSTRAINT users_streamer_id_fkey "
               "FOREIGN KEY (streamer_id) REFERENCES public.streamers(id) ON DELETE SET NULL")

    op.execute("""
        CREATE TABLE public.streamers_moderators (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            streamer_id uuid NOT NULL REFERENCES public.streamers(id) ON DELETE CASCADE,
            telegram_user_id text,
            mod_name text NOT NULL,
            is_active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            role public.moderator_role DEFAULT 'moderator',
            can_approve boolean DEFAULT true,
            can_reject boolean DEFAULT true,
            can_hide_message boolean DEFAULT true,
            can_ban boolean DEFAULT false,
            can_replay boolean DEFAULT true,
            can_manage_mods boolean DEFAULT false,
            last_action_at timestamptz,
            total_actions integer DEFAULT 0,
            discord_user_id text
        )
    """)
    op.execute("CREATE UNIQUE INDEX streamers_moderators_unique_active ON public.streamers_moderators "
               "(streamer_id, telegram_user_id) WHERE is_active = true")

    op.execute("""
        CREATE TABLE public.obs_tokens (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            streamer_id uuid NOT NULL REFERENCES public.streamers(id) ON DELETE CASCADE,
            token_hash text NOT NULL UNIQUE,
            is_active boolean DEFAULT true,
            expires_at timestamptz,
            created_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now(),
            last_used_at timestamptz,
            usage_count integer DEFAULT 0,
            pusher_channel_scope text[],
            last_used_ip text,
            revoked_at timestamptz
        )
    """)
    op.execute("CREATE INDEX obs_tokens_revoked_at_idx ON public.obs_tokens (revoked_at)")
    op.execute("CREATE INDEX obs_tokens_streamer_idx ON public.obs_tokens (streamer_id)")

    op.execute("""
        CREATE TABLE public.streamer_payment_health (
            streamer_id uuid PRIMARY KEY REFERENCES public.streamers(id) ON DELETE CASCADE,
            status text NOT NULL DEFAULT 'unknown'
                CHECK (status IN ('ok', 'invalid_key', 'missing_key', 'network_error', 'unknown')),
            error_message text,
            on_hold boolean NOT NULL DEFAULT false,
            last_checked_at timestamptz NOT NULL DEFAULT now(),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX streamer_payment_health_on_hold_idx ON public.streamer_payment_health (on_hold) "
               "WHERE on_hold = true")

    op.execute("""
        CREATE TABLE public.payout_accounts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL UNIQUE REFERENCES public.users(id) ON DELETE CASCADE,
            streamer_id uuid REFERENCES public.streamers(id) ON DELETE SET NULL,
            streamer_name text,
            account_holder_name text NOT NULL,
            bank_name text NOT NULL,
            ifsc_code text NOT NULL,
            account_number_encrypted text NOT NULL,
            account_number_last4 text NOT NULL,
            account_type text NOT NULL,
            upi_id text,
            pan_number_encrypted text,
            verification_status text NOT NULL DEFAULT 'pending',
            verified_at timestamptz,
            submitted_ip text,
            user_agent text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX payout_accounts_status_idx ON public.payout_accounts (verification_status)")
    op.execute("CREATE INDEX payout_accounts_streamer_idx ON public.payout_accounts (streamer_id)")

    op.execute("""
        CREATE TABLE public.platform_voice_tiers (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            min_amount_inr integer NOT NULL UNIQUE,
            max_duration_seconds integer NOT NULL,
            is_active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE public.streamer_voice_tiers (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            streamer_id uuid NOT NULL REFERENCES public.streamers(id) ON DELETE CASCADE,
            min_amount_inr integer NOT NULL,
            max_duration_seconds integer NOT NULL,
            is_active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (streamer_id, min_amount_inr)
        )
    """)
    op.execute("CREATE INDEX streamer_voice_tiers_streamer_idx ON public.streamer_voice_tiers (streamer_id) "
               "WHERE is_active = true")

    op.execute("""
        CREATE TABLE public.platform_soundboard_slots (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            display_name text NOT NULL,
            audio_key text NOT NULL,
            audio_url text NOT NULL,
            audio_duration_s numeric(5,2) NOT NULL,
            image_key text NOT NULL,
            image_url text NOT NULL,
            image_type public.soundboard_image_type NOT NULL,
            default_order_index integer NOT NULL UNIQUE
                CONSTRAINT platform_default_order_range CHECK (default_order_index >= 0 AND default_order_index <= 43),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            is_locked boolean NOT NULL DEFAULT false,
            swappable_index integer
                CONSTRAINT platform_soundboard_swappable_range_chk
                CHECK (swappable_index IS NULL OR (swappable_index >= 1 AND swappable_index <= 45))
        )
    """)
    op.execute("CREATE UNIQUE INDEX platform_soundboard_swappable_uq ON public.platform_soundboard_slots "
               "(swappable_index) WHERE swappable_index IS NOT NULL")

    op.execute("""
        CREATE TABLE public.streamer_soundboard_slots (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            streamer_slug text NOT NULL REFERENCES public.streamers(streamer_slug) ON UPDATE CASCADE ON DELETE CASCADE,
            platform_slot_id uuid REFERENCES public.platform_soundboard_slots(id) ON DELETE CASCADE,
            source public.soundboard_slot_source NOT NULL,
            display_name text,
            audio_key text,
            audio_url text,
            audio_duration_s numeric(5,2),
            image_key text,
            image_url text,
            image_type public.soundboard_image_type,
            is_visible boolean NOT NULL DEFAULT false,
            order_index integer,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            swappable_index integer,
            last_swapped_at timestamptz,
            CONSTRAINT custom_requires_audio CHECK (source <> 'custom' OR (audio_key IS NOT NULL AND audio_url IS NOT NULL
                AND audio_duration_s IS NOT NULL AND image_key IS NOT NULL AND image_url IS NOT NULL
                AND image_type IS NOT NULL AND display_name IS NOT NULL)),
            CONSTRAINT platform_requires_link CHECK (source <> 'platform' OR platform_slot_id IS NOT NULL),
            CONSTRAINT streamer_order_range CHECK (order_index IS NULL OR order_index <= 45),
            CONSTRAINT streamer_soundboard_swappable_range_chk
                CHECK (swappable_index IS NULL OR (swappable_index >= 1 AND swappable_index <= 45)),
            CONSTRAINT visible_requires_order CHECK (is_visible = false OR order_index IS NOT NULL)
        )
    """)
    op.execute("CREATE UNIQUE INDEX streamer_soundboard_custom_index_uq ON public.streamer_soundboard_slots "
               "(streamer_slug, swappable_index) WHERE source = 'custom' AND swappable_index IS NOT NULL")
    op.execute("CREATE INDEX streamer_soundboard_slug_visible_order_idx ON public.streamer_soundboard_slots "
               "(streamer_slug, is_visible, order_index)")
    op.execute("CREATE UNIQUE INDEX streamer_soundboard_unique_platform ON public.streamer_soundboard_slots "
               "(streamer_slug, platform_slot_id) WHERE platform_slot_id IS NOT NULL")
    op.execute("CREATE UNIQUE INDEX streamer_soundboard_unique_visible_order ON public.streamer_soundboard_slots "
               "(streamer_slug, order_index) WHERE is_visible = true")

    op.execute("""
        CREATE TABLE public.soundboard_swap_log (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            streamer_slug text NOT NULL REFERENCES public.streamers(streamer_slug) ON UPDATE CASCADE ON DELETE CASCADE,
            slot_index integer NOT NULL CHECK (slot_index >= 1 AND slot_index <= 45),
            swapped_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX soundboard_swap_log_slug_slot_time_idx ON public.soundboard_swap_log "
               "(streamer_slug, slot_index, swapped_at DESC)")
    op.execute("CREATE INDEX soundboard_swap_log_slug_time_idx ON public.soundboard_swap_log "
               "(streamer_slug, swapped_at DESC)")

    op.execute("""
        CREATE TABLE public.soundboard_tnc_acceptances (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            streamer_slug text NOT NULL,
            streamer_id uuid NOT NULL REFERENCES public.streamers(id) ON DELETE CASCADE,
            streamer_name text,
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
            user_email text,
            terms_version text NOT NULL DEFAULT 'soundboard-2026-07-13',
            accepted_at timestamptz NOT NULL DEFAULT now(),
            access_granted_at timestamptz NOT NULL DEFAULT (now() + interval '24 hours'),
            access_granted boolean NOT NULL DEFAULT false,
            granted_at timestamptz,
            revoked_at timestamptz,
            user_agent text,
            ip_hash text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (streamer_id, terms_version)
        )
    """)
    op.execute("CREATE INDEX soundboard_tnc_pending_idx ON public.soundboard_tnc_acceptances "
               "(access_granted, access_granted_at)")
    op.execute("CREATE INDEX soundboard_tnc_streamer_idx ON public.soundboard_tnc_acceptances (streamer_id)")

    op.execute("""
        CREATE TABLE public.terms_acceptances (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
            accepted_version text NOT NULL,
            accepted_at timestamptz NOT NULL DEFAULT now(),
            user_agent text,
            UNIQUE (user_id, accepted_version)
        )
    """)

    op.execute("""
        CREATE TABLE public.banned_donors (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            streamer_id uuid NOT NULL REFERENCES public.streamers(id) ON DELETE CASCADE,
            donor_name text NOT NULL,
            donor_name_lower text GENERATED ALWAYS AS (lower(donor_name)) STORED,
            banned_by_moderator_id uuid REFERENCES public.streamers_moderators(id) ON DELETE SET NULL,
            banned_by_name text,
            reason text,
            is_active boolean DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX banned_donors_name_idx ON public.banned_donors (donor_name_lower) WHERE is_active = true")
    op.execute("CREATE INDEX banned_donors_streamer_idx ON public.banned_donors (streamer_id)")

    op.execute("""
        CREATE TABLE public.api_keys (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            streamer_slug text NOT NULL REFERENCES public.streamers(streamer_slug) ON UPDATE CASCADE ON DELETE CASCADE,
            key_type text NOT NULL CHECK (key_type IN ('donation', 'audio')),
            key_hash text NOT NULL UNIQUE,
            key_prefix text NOT NULL,
            created_at timestamptz DEFAULT now(),
            last_used_at timestamptz,
            revoked boolean DEFAULT false
        )
    """)
    op.execute("CREATE INDEX api_keys_slug_idx ON public.api_keys (streamer_slug) WHERE revoked = false")

    op.execute("""
        CREATE TABLE public.partner_api_keys (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            partner_slug text NOT NULL,
            key_hash text NOT NULL UNIQUE,
            key_prefix text NOT NULL,
            revoked boolean NOT NULL DEFAULT false,
            last_used_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX partner_api_keys_partner_slug_idx ON public.partner_api_keys (partner_slug)")
    op.execute("""
        CREATE TABLE public.partner_signups (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            partner_slug text NOT NULL,
            channel_name text NOT NULL,
            mobile_number text NOT NULL,
            youtube_channel text NOT NULL,
            email text NOT NULL,
            instagram_handle text,
            status text NOT NULL DEFAULT 'new',
            notes text,
            source_ip text,
            user_agent text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX partner_signups_created_at_idx ON public.partner_signups (created_at DESC)")
    op.execute("CREATE UNIQUE INDEX partner_signups_partner_email_uniq ON public.partner_signups "
               "(partner_slug, lower(email))")


def downgrade() -> None:
    for table in ("partner_signups", "partner_api_keys", "api_keys", "banned_donors", "terms_acceptances",
                  "soundboard_tnc_acceptances", "soundboard_swap_log", "streamer_soundboard_slots",
                  "platform_soundboard_slots", "streamer_voice_tiers", "platform_voice_tiers", "payout_accounts",
                  "streamer_payment_health", "obs_tokens", "streamers_moderators"):
        op.execute(f"DROP TABLE IF EXISTS public.{table}")
    op.execute("ALTER TABLE public.users DROP CONSTRAINT IF EXISTS users_streamer_id_fkey")
    op.execute("DROP TABLE IF EXISTS public.streamers")
