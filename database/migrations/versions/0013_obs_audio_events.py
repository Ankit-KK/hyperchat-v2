"""OBS audio (Phase 10): realtime events for the alerts overlay.

Revision ID: 0013
Revises: 0012

Widens two CHECK constraints of public.realtime_outbox (0012):
  channel  + 'alerts'                                  private-{slug}-alerts (OBS alerts overlay)
  event    + 'audio-now-playing', 'vaani-celebration'  written by the OBS audio claim / payment completion

Nothing else changes: the audio queue uses existing donation columns (audio_scheduled_at, audio_played_at) and
the existing partial index donations_audio_queue_idx; OBS tokens use the existing obs_tokens table. The previous
release ignores the new values, so a code rollback needs no schema downgrade. The downgrade first deletes rows
using the new values (realtime events are transient).
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

OLD_CHANNELS = "('dashboard', 'audio', 'settings', 'leaderboard', 'goal')"
NEW_CHANNELS = "('dashboard', 'alerts', 'audio', 'settings', 'leaderboard', 'goal')"
OLD_EVENTS = ("('new-donation', 'donation-updated', 'audio-ready', 'settings-updated', 'leaderboard-updated', "
              "'goal-progress')")
NEW_EVENTS = ("('new-donation', 'donation-updated', 'audio-ready', 'settings-updated', 'leaderboard-updated', "
              "'goal-progress', 'audio-now-playing', 'vaani-celebration')")


def _constraints(channels: str, events: str) -> None:
    op.execute("ALTER TABLE public.realtime_outbox DROP CONSTRAINT realtime_outbox_channel_check")
    op.execute("ALTER TABLE public.realtime_outbox DROP CONSTRAINT realtime_outbox_event_check")
    op.execute(f"ALTER TABLE public.realtime_outbox ADD CONSTRAINT realtime_outbox_channel_check "
               f"CHECK (channel IN {channels})")
    op.execute(f"ALTER TABLE public.realtime_outbox ADD CONSTRAINT realtime_outbox_event_check "
               f"CHECK (event IN {events})")


def upgrade() -> None:
    _constraints(NEW_CHANNELS, NEW_EVENTS)


def downgrade() -> None:
    op.execute("DELETE FROM public.realtime_outbox WHERE channel = 'alerts' "
               "OR event IN ('audio-now-playing', 'vaani-celebration')")
    _constraints(OLD_CHANNELS, OLD_EVENTS)
