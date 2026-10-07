"""SQL for the job queue (public.jobs; runs as hyperchat_app).

Claiming is `FOR UPDATE SKIP LOCKED`: concurrent workers never pick the same row. A claimed job holds a
lease (locked_by, locked_until). A job whose lease expired (worker crash, kill -9, lost connection) is
claimed again by any worker, which counts as an attempt. Every later write (finish, retry, release) is
fenced on `locked_by = <this worker>` AND status = 'running': a worker that lost its lease can never
complete, retry or release a job another worker now owns.
"""

import re
from dataclasses import dataclass

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.jobs.retry import Effects

KINDS = {"donation_audio": 4, "voice_transcription": 3}  # kind -> max attempts
LEASE_SECONDS = 180
PAYLOAD_KEYS = frozenset({"donation_id"})  # every job kind identifies its subject by database ids only
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    dedupe_key: str
    payload: dict
    attempts: int  # including the current one
    max_attempts: int
    locked_by: str
    reclaimed: bool  # its previous lease expired while running


class LeaseLost(Exception):
    """This worker no longer owns the job (its lease expired and another worker claimed it)."""


def _check_payload(payload: dict) -> dict:
    """Ids only: never donor text, tokens or secrets in a job row."""
    if not isinstance(payload, dict) or not payload or len(payload) > len(PAYLOAD_KEYS):
        raise ValueError("job payload must be a small dict of ids")
    for key, value in payload.items():
        if key not in PAYLOAD_KEYS or not isinstance(value, str) or not _UUID.match(value):
            raise ValueError("job payload values must be known id fields holding UUIDs")
    return payload


async def enqueue(cur, kind: str, dedupe_key: str, payload: dict, *, delay_seconds: float = 0) -> str | None:
    """Insert a job using the CALLER's cursor (so it commits or rolls back with the caller's transaction).
    Returns the new job id, or None when an active (queued/running) job with this dedupe key exists."""
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}")
    await cur.execute("""
        INSERT INTO public.jobs (kind, dedupe_key, payload, max_attempts, available_at)
        VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s))
        ON CONFLICT (kind, dedupe_key) WHERE status IN ('queued', 'running') DO NOTHING
        RETURNING id::text
    """, (kind, dedupe_key, Jsonb(_check_payload(payload)), KINDS[kind], delay_seconds))
    row = await cur.fetchone()
    if row is None:
        return None
    return row["id"] if isinstance(row, dict) else row[0]


class JobQueue:
    def __init__(self, connection_factory, lease_seconds: int = LEASE_SECONDS) -> None:
        self._conn, self.lease_seconds = connection_factory, lease_seconds

    async def enqueue(self, kind: str, dedupe_key: str, payload: dict) -> str | None:
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            return await enqueue(cur, kind, dedupe_key, payload)

    async def claim(self, worker_id: str, kinds: list[str]) -> Job | None:
        async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("""
                WITH picked AS (
                    SELECT id, status AS previous FROM public.jobs
                    WHERE kind = ANY(%(kinds)s)
                      AND ((status = 'queued' AND available_at <= now())
                           OR (status = 'running' AND locked_until < now()))
                    ORDER BY available_at, created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE public.jobs j SET status = 'running', attempts = j.attempts + 1, locked_by = %(worker)s,
                    locked_until = now() + make_interval(secs => %(lease)s), started_at = now(),
                    last_error = CASE WHEN picked.previous = 'running' THEN 'lease_expired' ELSE j.last_error END
                FROM picked WHERE j.id = picked.id
                RETURNING j.id::text AS id, j.kind, j.dedupe_key, j.payload, j.attempts, j.max_attempts,
                          j.locked_by, picked.previous = 'running' AS reclaimed
            """, {"kinds": list(kinds), "worker": worker_id, "lease": self.lease_seconds})
            row = await cur.fetchone()
        return Job(**row) if row else None

    async def finish(self, job: Job, status: str, *, result: dict | None, last_error: str | None,
                     effects: Effects | None = None) -> bool:
        """Mark the job succeeded/failed and run `effects` in the SAME transaction. False (and nothing
        written) when this worker lost the lease."""
        assert status in ("succeeded", "failed")
        try:
            async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("""
                    UPDATE public.jobs SET status = %s, completed_at = now(), locked_by = NULL, locked_until = NULL,
                        result = %s, last_error = COALESCE(%s, last_error)  -- a success keeps the earlier error
                    WHERE id = %s AND status = 'running' AND locked_by = %s
                """, (status, Jsonb(result) if result is not None else None, last_error, job.id, job.locked_by))
                if cur.rowcount != 1:
                    raise LeaseLost()
                if effects is not None:
                    await effects(cur)
        except LeaseLost:
            return False
        return True

    async def retry(self, job: Job, delay_seconds: float, reason: str) -> bool:
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute("""
                UPDATE public.jobs SET status = 'queued', available_at = now() + make_interval(secs => %s),
                    locked_by = NULL, locked_until = NULL, last_error = %s
                WHERE id = %s AND status = 'running' AND locked_by = %s
            """, (delay_seconds, reason, job.id, job.locked_by))
            return cur.rowcount == 1

    async def release(self, job: Job) -> bool:
        """Graceful shutdown: hand an unfinished job back without counting the interrupted attempt."""
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute("""
                UPDATE public.jobs SET status = 'queued', available_at = now(), locked_by = NULL,
                    locked_until = NULL, attempts = GREATEST(attempts - 1, 0)
                WHERE id = %s AND status = 'running' AND locked_by = %s
            """, (job.id, job.locked_by))
            return cur.rowcount == 1

    async def latest(self, kind: str, dedupe_key: str) -> dict | None:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id::text AS id, status, attempts, last_error, result FROM public.jobs "
                              "WHERE kind = %s AND dedupe_key = %s ORDER BY created_at DESC LIMIT 1", (kind, dedupe_key))
            return await cur.fetchone()
