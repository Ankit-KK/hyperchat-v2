"""The realtime outbox (public.realtime_outbox; runs as hyperchat_app).

Writing (emit*): always with the CALLER's cursor, inside the transaction of the change that caused the event,
so the event exists if and only if that change commits. A failed or slow Pusher can therefore never fail or
delay a payment, a moderation action or a settings save. Rows hold ids and small non-PII facts only.

Publishing (OutboxQueue, used by app.realtime.publisher):
  claim    pending + due (or publishing with an expired lease) rows, at most one per creator and only when no
           OLDER row of that creator is still open: per-creator FIFO. FOR UPDATE SKIP LOCKED + a lease.
  finish   published / failed / skipped; retry: back to pending after a backoff; release: on shutdown,
           without counting the attempt. All fenced on locked_by + status 'publishing'.
  expire   open rows older than the max age (10 min; audio-now-playing 30 s) become 'expired' (a stale alert is
           worse than none).
  skip     realtime disabled: pending rows become 'skipped' (realtime_disabled).
"""

import re
from dataclasses import dataclass
from datetime import datetime

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

EVENT_CHANNELS = {
    "new-donation": "dashboard",
    "donation-updated": "dashboard",
    "audio-ready": "audio",
    "settings-updated": "settings",
    "leaderboard-updated": "leaderboard",
    "goal-progress": "goal",
    "audio-now-playing": "alerts",   # Phase 10: written by the OBS audio claim
    "vaani-celebration": "alerts",   # Phase 10: written by payment completion (configured creators only)
}
COALESCED = frozenset({"settings-updated", "leaderboard-updated", "goal-progress"})  # only the latest state matters
FACT_KEYS = frozenset({"action", "previous_status", "kind"})  # kind: hypersound | voice | tts | silence
MAX_ATTEMPTS = 6
LEASE_SECONDS = 30
MAX_AGE_SECONDS = 600
# A late "now playing" alert would show long after its audio: Phase 10 (P10-D11) expires it after 30 s.
EVENT_MAX_AGE_SECONDS = {"audio-now-playing": 30}
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")  # used with fullmatch
_FACT = re.compile(r"[a-z_]{1,32}")

# Settings whose change is pushed live, per event (D4).
OVERLAY_SETTINGS = ("brand_color", "alert_box_scale", "alert_overlay_id", "leaderboard_overlay_id", "goal_overlay_id",
                    "overlay_adjust_mode", "leaderboard_widget_enabled", "leaderboard_bg_enabled")
GOAL_SETTINGS = ("goal_is_active", "goal_name", "goal_target_amount", "goal_activated_at")
LEADERBOARD_SETTINGS = ("leaderboard_widget_enabled",)


def _check(streamer_id: str, event: str, subject_id: str | None, facts: dict | None) -> dict:
    if event not in EVENT_CHANNELS:
        raise ValueError(f"unknown realtime event {event!r}")
    if not isinstance(streamer_id, str) or not _UUID.fullmatch(streamer_id):
        raise ValueError("streamer_id must be a UUID")
    if subject_id is not None and (not isinstance(subject_id, str) or not _UUID.fullmatch(subject_id)):
        raise ValueError("subject_id must be a UUID")
    facts = facts or {}
    for key, value in facts.items():  # ids and fixed codes only: never donor text, tokens or secrets
        if key not in FACT_KEYS or not isinstance(value, str) or not _FACT.fullmatch(value):
            raise ValueError("realtime facts must be known keys holding short codes")
    return facts


async def emit(cur, streamer_id: str, event: str, *, subject_id: str | None = None,
               facts: dict | None = None) -> bool:
    """Insert one event with the caller's cursor. Coalesced events (goal, leaderboard, settings) are skipped
    when the creator already has one PENDING: that one is rendered later with the latest state anyway.
    Returns whether a row was inserted."""
    facts = _check(streamer_id, event, subject_id, facts)
    coalesce_key = f"{event}:{streamer_id}" if event in COALESCED else None
    await cur.execute("""
        INSERT INTO public.realtime_outbox (streamer_id, channel, event, subject_id, facts, coalesce_key, max_attempts)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (coalesce_key) WHERE status = 'pending' DO NOTHING
    """, (streamer_id, EVENT_CHANNELS[event], event, subject_id, Jsonb(facts), coalesce_key, MAX_ATTEMPTS))
    return cur.rowcount == 1


async def emit_totals_changed(cur, streamer_id: str) -> None:
    """Approved totals changed (approve, auto-approve, rejecting an approved donation): goal + leaderboard."""
    await emit(cur, streamer_id, "goal-progress")
    await emit(cur, streamer_id, "leaderboard-updated")


async def emit_settings_changed(cur, streamer_id: str, changed: list[str]) -> None:
    if any(c in OVERLAY_SETTINGS for c in changed):
        await emit(cur, streamer_id, "settings-updated")
    if any(c in GOAL_SETTINGS for c in changed):
        await emit(cur, streamer_id, "goal-progress")
    if any(c in LEADERBOARD_SETTINGS for c in changed):
        await emit(cur, streamer_id, "leaderboard-updated")


async def emit_audio_ready(cur, donation_id: str) -> bool:
    """Phase 8 -> realtime: the donation's audio is resolved (its tts_audio_url is set, or it plays its own voice
    message / hypersound) and it is still paid + approved. Runs in the donation_audio job's completion
    transaction, so the event exists if and only if that commit does."""
    if not isinstance(donation_id, str) or not _UUID.fullmatch(donation_id):
        raise ValueError("donation_id must be a UUID")
    await cur.execute("""
        INSERT INTO public.realtime_outbox (streamer_id, channel, event, subject_id, facts, max_attempts)
        SELECT d.streamer_id, 'audio', 'audio-ready', d.id, '{}'::jsonb, %s FROM public.donations d
        WHERE d.id = %s AND d.payment_status = 'success' AND d.moderation_status IN ('approved', 'auto_approved')
          AND (d.tts_audio_url IS NOT NULL OR d.voice_message_url IS NOT NULL OR d.hypersound_url IS NOT NULL)
    """, (MAX_ATTEMPTS, donation_id))
    return cur.rowcount == 1


@dataclass(frozen=True)
class Event:
    id: int
    event_id: str
    streamer_id: str
    channel: str
    event: str
    subject_id: str | None
    facts: dict
    attempts: int  # including the current one
    max_attempts: int
    created_at: datetime
    locked_by: str
    reclaimed: bool


class OutboxQueue:
    def __init__(self, connection_factory, *, lease_seconds: int = LEASE_SECONDS,
                 max_age_seconds: int = MAX_AGE_SECONDS) -> None:
        self._conn, self.lease_seconds, self.max_age_seconds = connection_factory, lease_seconds, max_age_seconds

    async def expire(self) -> int:
        """Open rows older than the max age (pending, or publishing with an expired lease) -> 'expired'."""
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute("""
                UPDATE public.realtime_outbox SET status = 'expired', locked_by = NULL, locked_until = NULL,
                    last_error = 'max_age'
                WHERE created_at < now() - make_interval(
                        secs => CASE event WHEN 'audio-now-playing' THEN %s ELSE %s END)
                  AND (status = 'pending' OR (status = 'publishing' AND locked_until < now()))
            """, (min(EVENT_MAX_AGE_SECONDS["audio-now-playing"], self.max_age_seconds), self.max_age_seconds))
            return cur.rowcount

    async def skip_pending(self, reason: str) -> int:
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute("""
                UPDATE public.realtime_outbox SET status = 'skipped', locked_by = NULL, locked_until = NULL,
                    last_error = %s
                WHERE status = 'pending' OR (status = 'publishing' AND locked_until < now())
            """, (reason,))
            return cur.rowcount

    async def claim(self, worker_id: str, limit: int) -> list[Event]:
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("""
                WITH picked AS (
                    SELECT o.id, o.status AS previous FROM public.realtime_outbox o
                    WHERE ((o.status = 'pending' AND o.available_at <= now())
                           OR (o.status = 'publishing' AND o.locked_until < now()))
                      AND NOT EXISTS (SELECT 1 FROM public.realtime_outbox e
                                      WHERE e.streamer_id = o.streamer_id AND e.id < o.id
                                        AND e.status IN ('pending', 'publishing'))
                    ORDER BY o.id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %(limit)s
                )
                UPDATE public.realtime_outbox o SET status = 'publishing', attempts = o.attempts + 1,
                    locked_by = %(worker)s, locked_until = now() + make_interval(secs => %(lease)s),
                    last_error = CASE WHEN picked.previous = 'publishing' THEN 'lease_expired' ELSE o.last_error END
                FROM picked WHERE o.id = picked.id
                RETURNING o.id, o.event_id::text AS event_id, o.streamer_id::text AS streamer_id, o.channel, o.event,
                          o.subject_id::text AS subject_id, o.facts, o.attempts, o.max_attempts, o.created_at,
                          o.locked_by, picked.previous = 'publishing' AS reclaimed
            """, {"limit": limit, "worker": worker_id, "lease": self.lease_seconds})
            rows = await cur.fetchall()
        return sorted((Event(**r) for r in rows), key=lambda e: e.id)

    async def finish(self, event: Event, status: str, reason: str | None = None) -> bool:
        assert status in ("published", "failed", "skipped")
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute("""
                UPDATE public.realtime_outbox SET status = %s, locked_by = NULL, locked_until = NULL,
                    published_at = CASE WHEN %s = 'published' THEN now() END,
                    last_error = COALESCE(%s, last_error)
                WHERE id = %s AND status = 'publishing' AND locked_by = %s
            """, (status, status, reason, event.id, event.locked_by))
            return cur.rowcount == 1

    async def retry(self, event: Event, delay_seconds: float, reason: str) -> bool:
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute("""
                UPDATE public.realtime_outbox SET status = 'pending', locked_by = NULL, locked_until = NULL,
                    available_at = now() + make_interval(secs => %s), last_error = %s
                WHERE id = %s AND status = 'publishing' AND locked_by = %s
            """, (delay_seconds, reason, event.id, event.locked_by))
            return cur.rowcount == 1

    async def release(self, event: Event) -> bool:
        """Graceful shutdown: hand the row back without counting the interrupted attempt."""
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute("""
                UPDATE public.realtime_outbox SET status = 'pending', locked_by = NULL, locked_until = NULL,
                    available_at = now(), attempts = GREATEST(attempts - 1, 0)
                WHERE id = %s AND status = 'publishing' AND locked_by = %s
            """, (event.id, event.locked_by))
            return cur.rowcount == 1
