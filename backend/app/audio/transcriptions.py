"""Dashboard voice transcription (D1; replaces legacy transcribe-voice-sarvam).

Legacy fetched ANY URL the browser sent and re-ran the paid transcription on every dashboard view. Here the
browser only names a donation of a creator it may access; the worker reads that donation's own voice object
from this environment's bucket; the transcript is stored once (donation_transcripts) and served from there.
"""

import re

from app.audio.repository import AudioRepository
from app.authz.dependencies import StreamerAccess
from app.core.errors import ApiError
from app.jobs.queue import JobQueue
from app.security.audit import security_event

RATE = ("transcription:request", 30, 3600)  # per creator: 30 requests an hour
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class TranscriptionService:
    def __init__(self, repo: AudioRepository, queue: JobQueue, limiter, *, available: bool) -> None:
        self.repo, self.queue, self.limiter, self.available = repo, queue, limiter, available

    async def _donation(self, access: StreamerAccess, donation_id: str) -> dict:
        row = None
        if _UUID.match(donation_id or ""):
            row = await self.repo.voice_donation(access.streamer.id, donation_id)
        if row is None:  # unknown, malformed or another creator's donation: identical 404
            raise ApiError(404, "Donation not found", "donation_not_found")
        if row["payment_status"] != "success" or not row["voice_message_url"]:
            raise ApiError(409, "This donation has no voice message to transcribe", "no_voice_message")
        return row

    async def _job_status(self, donation_id: str) -> str:
        job = await self.queue.latest("voice_transcription", donation_id)
        return job["status"] if job else "none"

    async def status(self, access: StreamerAccess, donation_id: str) -> dict:
        row = await self._donation(access, donation_id)
        if row["transcript"] is not None:
            return {"status": "completed", "transcript": row["transcript"]}
        status = await self._job_status(donation_id)
        return {"status": "failed" if status == "succeeded" else status, "transcript": None}

    async def request(self, access: StreamerAccess, donation_id: str) -> tuple[int, dict]:
        row = await self._donation(access, donation_id)
        if row["transcript"] is not None:
            return 200, {"status": "completed", "transcript": row["transcript"]}
        if not self.available:
            raise ApiError(503, "Transcription is not available", "transcription_unavailable")
        bucket, limit, window = RATE
        if not await self.limiter.hit_rate_limit(f"streamer:{access.streamer.id}", bucket, limit, window):
            security_event("transcription_rejected", reason="rate_limited", streamer_id=access.streamer.id)
            raise ApiError(429, "Too many transcription requests. Please try again later.", "transcription_rate_limited")
        job_id = await self.queue.enqueue("voice_transcription", donation_id, {"donation_id": donation_id})
        status = "queued" if job_id else await self._job_status(donation_id)
        return 202, {"status": status, "transcript": None}
