"""Integrity triggers ported from the source schema (bodies copied from hyperchat_restore_test).

Revision ID: 0006
Revises: 0005

Business-logic triggers (the 4 HyperEmote auto-approve triggers) are NOT here: they are
rewritten against the single donations table in the donations phase.
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

UPDATED_AT_TABLES = (
    "streamers", "obs_tokens", "partner_api_keys", "partner_signups", "streamer_voice_tiers",
    "platform_voice_tiers", "platform_soundboard_slots", "streamer_soundboard_slots",
    "soundboard_tnc_acceptances", "streamer_payment_health", "banned_donors",
)


def upgrade() -> None:
    for table in UPDATED_AT_TABLES:
        op.execute(f"CREATE TRIGGER {table}_set_updated_at BEFORE UPDATE ON public.{table} "
                   "FOR EACH ROW EXECUTE FUNCTION public.set_updated_at()")

    op.execute("""
        CREATE FUNCTION public.enforce_payout_account_immutable() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public AS $$
        BEGIN
          IF NEW.user_id IS DISTINCT FROM OLD.user_id
             OR NEW.account_holder_name IS DISTINCT FROM OLD.account_holder_name
             OR NEW.bank_name IS DISTINCT FROM OLD.bank_name
             OR NEW.ifsc_code IS DISTINCT FROM OLD.ifsc_code
             OR NEW.account_number_encrypted IS DISTINCT FROM OLD.account_number_encrypted
             OR NEW.account_number_last4 IS DISTINCT FROM OLD.account_number_last4
             OR NEW.account_type IS DISTINCT FROM OLD.account_type
             OR NEW.pan_number_encrypted IS DISTINCT FROM OLD.pan_number_encrypted
          THEN
            RAISE EXCEPTION 'Payout banking details are immutable once submitted';
          END IF;
          RETURN NEW;
        END;
        $$
    """)
    op.execute("CREATE TRIGGER enforce_payout_account_immutable_trg BEFORE UPDATE ON public.payout_accounts "
               "FOR EACH ROW EXECUTE FUNCTION public.enforce_payout_account_immutable()")

    op.execute("""
        CREATE FUNCTION public.validate_payout_account() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public AS $$
        BEGIN
          NEW.ifsc_code := upper(trim(NEW.ifsc_code));
          IF NEW.ifsc_code !~ '^[A-Z]{4}0[A-Z0-9]{6}$' THEN
            RAISE EXCEPTION 'Invalid IFSC code format';
          END IF;
          IF NEW.account_type NOT IN ('savings', 'current') THEN
            RAISE EXCEPTION 'Invalid account type';
          END IF;
          IF NEW.account_number_last4 !~ '^[0-9]{4}$' THEN
            RAISE EXCEPTION 'Invalid account number';
          END IF;
          IF length(trim(NEW.account_holder_name)) < 2 THEN
            RAISE EXCEPTION 'Account holder name is required';
          END IF;
          IF length(trim(NEW.bank_name)) < 2 THEN
            RAISE EXCEPTION 'Bank name is required';
          END IF;
          IF NEW.verification_status NOT IN ('pending', 'verified', 'rejected') THEN
            RAISE EXCEPTION 'Invalid verification status';
          END IF;
          NEW.updated_at := now();
          RETURN NEW;
        END;
        $$
    """)
    op.execute("CREATE TRIGGER validate_payout_account_trg BEFORE INSERT OR UPDATE ON public.payout_accounts "
               "FOR EACH ROW EXECUTE FUNCTION public.validate_payout_account()")

    op.execute("""
        CREATE FUNCTION public.enforce_soundboard_active_cap() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public AS $$
        DECLARE
          visible_count integer;
        BEGIN
          IF NEW.is_visible = false THEN
            RETURN NEW;
          END IF;
          SELECT count(*) INTO visible_count
          FROM public.streamer_soundboard_slots
          WHERE streamer_slug = NEW.streamer_slug AND is_visible = true AND id <> NEW.id;
          IF visible_count >= 45 THEN
            RAISE EXCEPTION 'Soundboard active cap exceeded for streamer %', NEW.streamer_slug
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END;
        $$
    """)
    op.execute("CREATE TRIGGER enforce_soundboard_active_cap_ins BEFORE INSERT ON public.streamer_soundboard_slots "
               "FOR EACH ROW EXECUTE FUNCTION public.enforce_soundboard_active_cap()")
    op.execute("CREATE TRIGGER enforce_soundboard_active_cap_upd BEFORE UPDATE OF is_visible, streamer_slug "
               "ON public.streamer_soundboard_slots FOR EACH ROW EXECUTE FUNCTION public.enforce_soundboard_active_cap()")

    op.execute("""
        CREATE FUNCTION public.validate_overlay_ids() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public AS $$
        BEGIN
          IF NEW.alert_overlay_id IS NULL OR NEW.alert_overlay_id < 0 OR NEW.alert_overlay_id > 16 THEN
            RAISE EXCEPTION 'alert_overlay_id must be between 0 and 16 (got %)', NEW.alert_overlay_id;
          END IF;
          IF NEW.leaderboard_overlay_id IS NULL OR NEW.leaderboard_overlay_id < 0 OR NEW.leaderboard_overlay_id > 15 THEN
            RAISE EXCEPTION 'leaderboard_overlay_id must be between 0 and 15 (got %)', NEW.leaderboard_overlay_id;
          END IF;
          RETURN NEW;
        END;
        $$
    """)
    op.execute("CREATE TRIGGER streamers_validate_overlay_ids BEFORE INSERT OR UPDATE OF alert_overlay_id, "
               "leaderboard_overlay_id ON public.streamers FOR EACH ROW EXECUTE FUNCTION public.validate_overlay_ids()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS streamers_validate_overlay_ids ON public.streamers")
    op.execute("DROP FUNCTION IF EXISTS public.validate_overlay_ids()")
    op.execute("DROP TRIGGER IF EXISTS enforce_soundboard_active_cap_upd ON public.streamer_soundboard_slots")
    op.execute("DROP TRIGGER IF EXISTS enforce_soundboard_active_cap_ins ON public.streamer_soundboard_slots")
    op.execute("DROP FUNCTION IF EXISTS public.enforce_soundboard_active_cap()")
    op.execute("DROP TRIGGER IF EXISTS validate_payout_account_trg ON public.payout_accounts")
    op.execute("DROP FUNCTION IF EXISTS public.validate_payout_account()")
    op.execute("DROP TRIGGER IF EXISTS enforce_payout_account_immutable_trg ON public.payout_accounts")
    op.execute("DROP FUNCTION IF EXISTS public.enforce_payout_account_immutable()")
    for table in UPDATED_AT_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_set_updated_at ON public.{table}")
