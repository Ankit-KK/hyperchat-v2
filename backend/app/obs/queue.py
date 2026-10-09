"""The OBS audio queue (runs as hyperchat_app): the v2 equivalent of legacy claim_next_audio, plus replay.

claim (P10-D4, P10-D7), ONE transaction:
    UPDATE donations SET audio_played_at = now() WHERE id = (the creator's next eligible donation
        ORDER BY audio_scheduled_at, id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING ...
  eligible: audio_played_at IS NULL, paid, approved / auto-approved, audio_scheduled_at <= now() and at most
  QUEUE_WINDOW_HOURS (24) old, and a hypersound, voice message or TTS URL. Index: donations_audio_queue_idx
  (streamer_id, audio_scheduled_at) WHERE audio_played_at IS NULL AND paid AND approved.
  The item counts as played the moment it is claimed (legacy): two OBS clients never get the same item, a crash
  mid-item does not bring it back (replay does). An item whose URL is not servable (app.obs.media) is marked
  played and skipped; the next one is tried (at most MAX_SKIPS per claim). The served item's
  `audio-now-playing` outbox event is written in the same transaction (Phase 9 outbox).

replay (P10-D15), ONE transaction: the creator's own paid + approved donation with audio gets
  audio_played_at = NULL and audio_scheduled_at = now(), a moderation_actions 'replay' row, and the outbox events
  donation-updated {action: replay} and audio-ready.
"""

from dataclasses import dataclass

from psycopg.rows import dict_row

from app.obs.media import choose
from app.realtime import outbox

QUEUE_WINDOW_HOURS = 24
MAX_SKIPS = 10
RETRY_HINT_SECONDS = 60  # a 204 says when the next item is due if that is within this many seconds
_DONATION = """d.id::text AS id, d.streamer_id::text AS streamer_id, d.donor_name AS name, d.amount,
    d.currency::text AS currency, d.message, d.message_visible, d.moderation_status, d.payment_status,
    d.is_hyperemote, d.voice_message_url, d.tts_audio_url, d.hypersound_url, d.media_url, d.media_type,
    d.created_at"""


@dataclass(frozen=True)
class Claimed:
    donation: dict
    kind: str
    audio_url: str


@dataclass(frozen=True)
class ClaimResult:
    item: Claimed | None
    skipped: int
    retry_after: int | None  # seconds until the next item is due (only when within RETRY_HINT_SECONDS)


class NotReplayable(Exception):
    """Only the creator's own paid + approved donations with audio can be replayed."""


class AudioQueue:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def claim(self, streamer_id: str, slug: str, public_base: str | None) -> ClaimResult:
        skipped = 0
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            for _ in range(MAX_SKIPS + 1):
                await cur.execute(f"""
                    UPDATE public.donations d SET audio_played_at = now()
                    WHERE d.id = (
                        SELECT q.id FROM public.donations q
                        WHERE q.streamer_id = %(sid)s AND q.audio_played_at IS NULL AND q.payment_status = 'success'
                          AND q.moderation_status IN ('approved', 'auto_approved')
                          AND q.audio_scheduled_at <= now()
                          AND q.audio_scheduled_at >= now() - make_interval(hours => %(window)s)
                          AND (q.hypersound_url IS NOT NULL OR q.voice_message_url IS NOT NULL
                               OR q.tts_audio_url IS NOT NULL)
                        ORDER BY q.audio_scheduled_at, q.id
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED)
                    RETURNING {_DONATION}
                """, {"sid": streamer_id, "window": QUEUE_WINDOW_HOURS})
                row = await cur.fetchone()
                if row is None:
                    break
                chosen = choose(row, public_base, slug)
                if chosen is None:
                    skipped += 1  # not servable: stays played, never redirected to
                    continue
                kind, url = chosen
                await outbox.emit(cur, streamer_id, "audio-now-playing", subject_id=row["id"], facts={"kind": kind})
                return ClaimResult(Claimed(row, kind, url), skipped, None)
            await cur.execute("""
                SELECT ceil(extract(epoch FROM min(audio_scheduled_at) - now()))::int AS due FROM public.donations
                WHERE streamer_id = %s AND audio_played_at IS NULL AND payment_status = 'success'
                  AND moderation_status IN ('approved', 'auto_approved') AND audio_scheduled_at > now()
                  AND audio_scheduled_at <= now() + make_interval(secs => %s)
                  AND (hypersound_url IS NOT NULL OR voice_message_url IS NOT NULL OR tts_audio_url IS NOT NULL)
            """, (streamer_id, RETRY_HINT_SECONDS))
            due = (await cur.fetchone())["due"]
        return ClaimResult(None, skipped, max(1, due) if due is not None else None)

    async def replay(self, streamer_id: str, donation_id: str, *, actor_user_id: str | None, actor_label: str) -> dict:
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"SELECT {_DONATION} FROM public.donations d WHERE d.id = %s AND d.streamer_id = %s "
                              "FOR UPDATE", (donation_id, streamer_id))
            d = await cur.fetchone()
            if d is None:
                raise LookupError(donation_id)
            if d["payment_status"] != "success" or d["moderation_status"] not in ("approved", "auto_approved") \
                    or not (d["hypersound_url"] or d["voice_message_url"] or d["tts_audio_url"]):
                raise NotReplayable()
            await cur.execute("UPDATE public.donations SET audio_played_at = NULL, audio_scheduled_at = now() "
                              "WHERE id = %s", (donation_id,))
            await cur.execute("""
                INSERT INTO public.moderation_actions (streamer_id, moderator_id, moderator_telegram_id, moderator_name,
                    donation_id, action_type, action_source, previous_status, new_status, notes, actor_user_id, created_at)
                VALUES (%s, NULL, NULL, %s, %s, 'replay', 'dashboard', %s, %s, NULL, %s, clock_timestamp())
            """, (streamer_id, actor_label, donation_id, d["moderation_status"], d["moderation_status"],
                  actor_user_id))
            await outbox.emit(cur, streamer_id, "donation-updated", subject_id=donation_id,
                              facts={"action": "replay", "previous_status": d["moderation_status"]})
            await outbox.emit_audio_ready(cur, donation_id)
            return d
