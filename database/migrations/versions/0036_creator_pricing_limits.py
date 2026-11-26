"""PAY-14 creator pricing: the D-M3b limits (owner, 2026-10-29; build approved 2026-11-25) also hold in the database.

Revision ID: 0036
Revises: 0035

The dashboard settings PATCH (#149) lets a creator set their minimums (min_text_amount_inr, min_voice_amount_inr,
media_min_amount), message_char_tiers and their streamer_voice_tiers rows. The app checks every rule (400 naming the
field); this migration makes the bounds and tier counts hold for every write by the runtime roles too, with the same
numbers as app.dashboard.validation:

* streamers: a changed minimum is NULL or floor-10,000, the floors being the PATCH floors of the owner decision of
  2026-11-25 (text 40, voice 150, media 100); a changed message_char_tiers is NULL or an array of at most 5 objects
  with numeric min_amount 0-10,000 and max_chars 50-500.
* streamer_voice_tiers: an active row has min_amount_inr 0-10,000 and max_duration_seconds 5-30, and a creator has
  at most 5 active rows (the count takes the creator row lock; the dashboard writer already holds it).

Triggers, not CHECK constraints, and only for the runtime roles (as 0020's streamers_protect_kyc_columns): a CHECK,
even NOT VALID, is re-checked on every UPDATE of a row, so a legacy creator holding a value from before V2 (e.g. a
voice minimum below 150) could no longer change an unrelated setting. On streamers the trigger checks only a column
the statement changes, which is the PATCH-only rule: stored values are never re-judged, and checkout keeps the platform
floors. Inactive voice tiers are never served, so they are judged when they are activated. Migrations and the legacy
import (owner / migrator) are not restricted. Nothing existing is rewritten or validated, so no existing row can stop
this upgrade. Trigger functions run without an EXECUTE grant, so nobody gets one (REVOKE from PUBLIC, as 0022 / 0023).
Tier ordering stays an application rule.

The downgrade only drops the two triggers and their functions (no data), so it is never refused.
"""

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

_RUNTIME_SQL = "('hyperchat_app', 'hyperchat_worker')"
_REFUSE = "USING ERRCODE = 'check_violation'"
_BAD_CHAR_TIER = ('$[*] ? (@.type() != "object" || !exists(@.min_amount) || !exists(@.max_chars) '
                  '|| @.min_amount.type() != "number" || @.max_chars.type() != "number" '
                  '|| @.min_amount < 0 || @.min_amount > 10000 || @.max_chars < 50 || @.max_chars > 500)')


def upgrade() -> None:
    op.execute(f"""
        CREATE FUNCTION public.streamers_check_pricing() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            amount numeric;
            low numeric;
            col text;
        BEGIN
            IF current_user NOT IN {_RUNTIME_SQL} THEN
                RETURN NEW;
            END IF;
            FOREACH col IN ARRAY ARRAY['min_text_amount_inr', 'min_voice_amount_inr', 'media_min_amount'] LOOP
                amount := CASE col WHEN 'min_text_amount_inr' THEN NEW.min_text_amount_inr
                                    WHEN 'min_voice_amount_inr' THEN NEW.min_voice_amount_inr
                                    ELSE NEW.media_min_amount END;
                low := CASE col WHEN 'min_text_amount_inr' THEN 40 WHEN 'min_voice_amount_inr' THEN 150 ELSE 100 END;
                IF (TG_OP = 'INSERT' OR amount IS DISTINCT FROM CASE col
                        WHEN 'min_text_amount_inr' THEN OLD.min_text_amount_inr
                        WHEN 'min_voice_amount_inr' THEN OLD.min_voice_amount_inr
                        ELSE OLD.media_min_amount END)
                   AND amount IS NOT NULL AND NOT amount BETWEEN low AND 10000 THEN
                    RAISE EXCEPTION 'streamers.% must be %-10000', col, low {_REFUSE};
                END IF;
            END LOOP;
            IF (TG_OP = 'INSERT' OR NEW.message_char_tiers IS DISTINCT FROM OLD.message_char_tiers)
               AND NEW.message_char_tiers IS NOT NULL
               AND (jsonb_typeof(NEW.message_char_tiers) <> 'array' OR jsonb_array_length(NEW.message_char_tiers) > 5
                    OR jsonb_path_exists(NEW.message_char_tiers, '{_BAD_CHAR_TIER}')) THEN
                RAISE EXCEPTION 'streamers.message_char_tiers: at most 5 tiers, min_amount 0-10000, max_chars 50-500'
                    {_REFUSE};
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER streamers_check_pricing BEFORE INSERT OR UPDATE ON public.streamers "
               "FOR EACH ROW EXECUTE FUNCTION public.streamers_check_pricing()")

    op.execute(f"""
        CREATE FUNCTION public.streamer_voice_tiers_check() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF current_user NOT IN {_RUNTIME_SQL} OR NOT NEW.is_active THEN
                RETURN NEW;
            END IF;
            IF NOT NEW.min_amount_inr BETWEEN 0 AND 10000 OR NOT NEW.max_duration_seconds BETWEEN 5 AND 30 THEN
                RAISE EXCEPTION 'streamer_voice_tiers: min_amount_inr 0-10000, max_duration_seconds 5-30' {_REFUSE};
            END IF;
            -- one count at a time per creator; NO KEY UPDATE does not block donation inserts (their FK KEY SHARE)
            PERFORM 1 FROM public.streamers WHERE id = NEW.streamer_id FOR NO KEY UPDATE;
            IF (SELECT count(*) FROM public.streamer_voice_tiers
                WHERE streamer_id = NEW.streamer_id AND is_active AND id <> NEW.id) >= 5 THEN
                RAISE EXCEPTION 'streamer_voice_tiers: at most 5 active tiers per creator' {_REFUSE};
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER streamer_voice_tiers_check BEFORE INSERT OR UPDATE ON public.streamer_voice_tiers "
               "FOR EACH ROW EXECUTE FUNCTION public.streamer_voice_tiers_check()")
    op.execute("REVOKE ALL ON FUNCTION public.streamers_check_pricing(), public.streamer_voice_tiers_check() "
               "FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP TRIGGER streamer_voice_tiers_check ON public.streamer_voice_tiers")
    op.execute("DROP FUNCTION public.streamer_voice_tiers_check()")
    op.execute("DROP TRIGGER streamers_check_pricing ON public.streamers")
    op.execute("DROP FUNCTION public.streamers_check_pricing()")
