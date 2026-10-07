"""SQL for donation audio and transcription (runs as hyperchat_app). Every write is conditional:

  set_audio_url      tts_audio_url only while it is NULL, the donation is still paid + approved and has no
                     voice message / hypersound of its own (those play themselves): never overwritten
  insert_transcript  once per donation (ON CONFLICT DO NOTHING)

Both run inside the job's completion transaction (app.jobs.queue.finish), fenced by the job lease.
"""

import hashlib
import hmac
import unicodedata

from psycopg.rows import dict_row

from app.audio.moderation import MODERATION_VERSION, NORMALIZATION_VERSION, Decision

APPROVED = ("approved", "auto_approved")
PROBE_LIMIT = 3            # blocked messages per supporter per hour before every further one is blocked
PROBE_WINDOW_MINUTES = 60
COMMAND_WINDOW_MINUTES = 60


def supporter_hash(key: str, streamer_id: str, supporter_name: str) -> str:
    """Legacy pseudonym: HMAC-SHA256("tts-moderation:<key>", "<streamer id>:<NFKC, trimmed, lower-cased name>")."""
    if not key:
        raise ValueError("hash_key_missing")
    ident = f"{streamer_id}:{unicodedata.normalize('NFKC', supporter_name or '').strip().lower()}"
    return hmac.new(f"tts-moderation:{key}".encode(), ident.encode(), hashlib.sha256).hexdigest()


class AudioRepository:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def _one(self, sql: str, params) -> dict | None:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()

    async def donation_context(self, donation_id: str) -> dict | None:
        return await self._one("""
            SELECT d.id::text AS id, d.streamer_id::text AS streamer_id, d.donor_name, d.amount,
                   d.currency::text AS currency, d.amount_inr, d.message, d.payment_status, d.moderation_status,
                   d.tts_audio_url, d.voice_message_url, d.hypersound_url, d.media_url, d.media_type, d.is_hyperemote,
                   s.streamer_slug, s.streamer_name, s.tts_enabled, s.min_tts_amount_inr, s.tts_voice_id,
                   s.el_voice_id, s.tts_language_boost,
                   EXISTS (SELECT 1 FROM public.donation_transcripts t WHERE t.donation_id = d.id) AS transcribed
            FROM public.donations d JOIN public.streamers s ON s.id = d.streamer_id WHERE d.id = %s
        """, (donation_id,))

    async def recent_commands(self, streamer_id: str, donor_name: str, prefix: str, donation_id: str) -> tuple[int, bool]:
        """(paid donations by this donor to this creator starting with `prefix` in the last hour, excluding this
        one; whether one of them is still waiting for its audio job). Legacy per-donor command limits."""
        row = await self._one("""
            SELECT count(*) AS n, COALESCE(bool_or(EXISTS (SELECT 1 FROM public.jobs j WHERE j.kind = 'donation_audio'
                       AND j.dedupe_key = d.id::text AND j.status IN ('queued', 'running'))), false) AS in_flight
            FROM public.donations d
            WHERE d.streamer_id = %s AND d.donor_name = %s AND d.id <> %s AND d.payment_status = 'success'
              AND d.message ILIKE %s AND d.created_at >= now() - make_interval(mins => %s)
        """, (streamer_id, donor_name, donation_id, prefix.replace("%", r"\%").replace("_", r"\_") + "%",
              COMMAND_WINDOW_MINUTES))
        return row["n"], row["in_flight"]

    async def blocked_recently(self, identity_hash: str) -> int:
        row = await self._one("""
            SELECT count(*) AS n FROM public.tts_moderation_audit WHERE supporter_identity_hash = %s AND blocked
              AND created_at >= now() - make_interval(mins => %s)
        """, (identity_hash, PROBE_WINDOW_MINUTES))
        return row["n"]

    async def record_block(self, *, donation_id: str, streamer_id: str, identity_hash: str, source: str,
                           decision: Decision) -> None:
        await self._one("""
            INSERT INTO public.tts_moderation_audit (donation_id, streamer_id, supporter_identity_hash, blocked, source,
                category, severity, rule_id, moderation_version, normalization_version)
            VALUES (%s, %s, %s, true, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (donation_id, streamer_id, identity_hash, source, decision.category, decision.severity,
              decision.rule_id, MODERATION_VERSION, NORMALIZATION_VERSION))

    @staticmethod
    async def set_audio_url(cur, donation_id: str, url: str) -> bool:
        await cur.execute("""
            UPDATE public.donations SET tts_audio_url = %s
            WHERE id = %s AND tts_audio_url IS NULL AND voice_message_url IS NULL AND hypersound_url IS NULL
              AND payment_status = 'success' AND moderation_status IN ('approved', 'auto_approved')
        """, (url, donation_id))
        return cur.rowcount == 1

    @staticmethod
    async def insert_transcript(cur, *, donation_id: str, streamer_id: str, transcript: str, model: str) -> None:
        await cur.execute("""
            INSERT INTO public.donation_transcripts (donation_id, streamer_id, transcript, provider, model)
            VALUES (%s, %s, %s, 'sarvam', %s) ON CONFLICT (donation_id) DO NOTHING
        """, (donation_id, streamer_id, transcript, model))

    # ── dashboard transcription endpoints ───────────────────────────────────
    async def voice_donation(self, streamer_id: str, donation_id: str) -> dict | None:
        """The creator's own donation (scoped by streamer_id) with its transcript, if any."""
        return await self._one("""
            SELECT d.id::text AS id, d.payment_status, d.voice_message_url, t.transcript
            FROM public.donations d LEFT JOIN public.donation_transcripts t ON t.donation_id = d.id
            WHERE d.id = %s AND d.streamer_id = %s
        """, (donation_id, streamer_id))
