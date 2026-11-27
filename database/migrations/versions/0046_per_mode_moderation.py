"""Per-mode donation moderation (owner 2026-11-28, prompted by security audit run-4 NV4-5): one "require approval"
switch each for text messages, audio (voice) messages and image/GIF media, and only a text donation carries a message.

Revision ID: 0046
Revises: 0045

* streamers gets text_approval_required, voice_approval_required and media_approval_required (boolean NOT NULL). They
  replace the legacy Moderation Mode and Require Approval for Media switches (moderation_mode, media_moderation_enabled;
  the columns stay, for rollback and as the ETL source shape, but nothing reads them). Existing creators keep exactly
  today's rule (app.payments.rules before this migration), NULL read as the column default ('auto_approve' / true):
    text  approval = moderation_mode = 'manual'
    voice approval = moderation_mode = 'manual' OR media_moderation_enabled
    media approval = moderation_mode = 'manual' OR media_moderation_enabled
  The column defaults (text off, voice and media on: owner "Keep manual approval") apply to a row inserted without
  them; a new creator takes app.admin.repository.NEW_CREATOR_MODERATION (the same values), and the cutover ETL maps
  each legacy creator with these expressions (etl.mapping.MODERATION_FROM_LEGACY; test_etl_mapping pins them equal).
  HyperSounds have no switch: they are always auto-approved.
* donations gets the CHECK donations_attachment_without_message: a donation with a voice message, a HyperSound or
  media carries no message (NULL or ''), as the owner decided ("hypersounds dont carry a message, it can only have a
  name from user. Same for audio message + image/gifs"). Rows created before this migration (a UTC timestamptz literal
  rendered when it runs) are exempt, so an approval, the 0031 credit writer and 0032's donation_accounting_apply can
  still update an older row that has both. A CHECK, not a trigger: the PAY-15 verifier pins the exact trigger set of
  donations (app.payments.verify.TRIGGER_PINS) and never reads pg_constraint. No pinned function, trigger or grant
  changes.

Downgrade drops the CHECK and the three columns. The old columns were never changed, so they still hold the settings as
they were at the upgrade (a creator's later per-mode changes are lost with the columns).
"""

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

COLUMNS = {"text_approval_required": "false", "voice_approval_required": "true", "media_approval_required": "true"}
# today's rule, per mode (etl.mapping.MODERATION_FROM_LEGACY = BACKFILL)
_MANUAL = "coalesce(moderation_mode, 'auto_approve') = 'manual'"
_HELD = f"({_MANUAL} OR coalesce(media_moderation_enabled, true))"
BACKFILL = {"text_approval_required": _MANUAL, "voice_approval_required": _HELD, "media_approval_required": _HELD}
CHECK = "donations_attachment_without_message"


def upgrade() -> None:
    op.execute("ALTER TABLE public.streamers " + ", ".join(
        f"ADD COLUMN {column} boolean NOT NULL DEFAULT {default}" for column, default in COLUMNS.items()))
    # the settings as they are; no creator's updated_at moves (as 0020's backfill)
    op.execute("ALTER TABLE public.streamers DISABLE TRIGGER streamers_set_updated_at")
    op.execute("UPDATE public.streamers SET " + ", ".join(f"{c} = {e}" for c, e in BACKFILL.items()))
    op.execute("ALTER TABLE public.streamers ENABLE TRIGGER streamers_set_updated_at")
    op.execute(f"""
        DO $$
        BEGIN
            EXECUTE format('ALTER TABLE public.donations ADD CONSTRAINT {CHECK} CHECK (message IS NULL OR message = '''' '
                           'OR (voice_message_url IS NULL AND hypersound_url IS NULL AND media_url IS NULL) '
                           'OR created_at < %L::timestamptz)',
                           to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') || '+00');
        END $$
    """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE public.donations DROP CONSTRAINT {CHECK}")
    op.execute("ALTER TABLE public.streamers " + ", ".join(f"DROP COLUMN {c}" for c in COLUMNS))
