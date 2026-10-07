"""Worker runner and retry policy with an in-memory queue (the PostgreSQL queue itself, SKIP LOCKED, leases
and real concurrency are in test_jobs_db.py)."""

import asyncio
import random
import uuid

import psycopg
import pytest

from app.jobs.queue import Job, _check_payload
from app.jobs.retry import Backoff, Done, Permanent, Retryable, reason_code
from app.jobs.runner import Worker


# ── backoff ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("attempt, lo, hi", [(1, 4, 6), (2, 8, 12), (3, 16, 24), (4, 32, 48), (5, 48, 60), (9, 48, 60)])
def test_donation_audio_backoff_bounds(attempt, lo, hi):
    b = Backoff(5, 60, 0.2)
    rng = random.Random(1)
    delays = [b.delay(attempt, rng=rng) for _ in range(300)]
    assert lo <= min(delays) and max(delays) <= hi and len(set(delays)) > 50  # jittered, never above the cap


def test_backoff_respects_retry_after_within_the_cap():
    b = Backoff(5, 60, 0.2)
    assert b.delay(1, retry_after=30) == 30
    assert b.delay(1, retry_after=600) == 60
    assert 4 <= b.delay(1, retry_after=0) <= 6
    assert b.delay(1, rng=random.Random(5)) == b.delay(1, rng=random.Random(5))


def test_reason_codes_are_sanitised():
    assert reason_code("provider_timeout") == "provider_timeout"
    assert reason_code("Bad reason with KEY=abc") == "error" and reason_code("x" * 65) == "error"
    assert Retryable("not a code: secret").reason == "error"


@pytest.mark.parametrize("payload", [{"donation_id": "free text with spaces"}, {"message": "hello"},
                                     {"donation_id": "not-a-uuid"}, {"donation_id": str(uuid.uuid4()).upper()},
                                     {"k": 1}, {"a" * 101: "b"}, "string", {}, {"status_token": "a" * 64},
                                     {"donation_id": str(uuid.uuid4()), "message": "hi"}])
def test_job_payloads_hold_ids_only(payload):
    with pytest.raises(ValueError):
        _check_payload(payload)
    assert _check_payload({"donation_id": str(uuid.uuid4())})


# ── runner ──────────────────────────────────────────────────────────────────

class FakeQueue:
    def __init__(self, *jobs):
        self.jobs, self.finished, self.retried, self.released, self.effects_ran = list(jobs), [], [], [], []
        self.finish_result = True

    async def claim(self, worker, kinds):
        return self.jobs.pop(0) if self.jobs else None

    async def finish(self, job, status, *, result, last_error, effects=None):
        if self.finish_result is True and effects:
            await effects("cursor")
        self.finished.append((job.id, status, result, last_error))
        if isinstance(self.finish_result, Exception):
            raise self.finish_result
        return self.finish_result

    async def retry(self, job, delay, reason):
        self.retried.append((job.id, delay, reason))
        return True

    async def release(self, job):
        self.released.append(job.id)
        return True


class FakeHandler:
    kind = "donation_audio"
    backoff = Backoff(5, 60, 0.2)

    def __init__(self, outcome):
        self.outcome, self.runs, self.fallbacks = outcome, 0, []

    async def run(self, job):
        self.runs += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if callable(self.outcome):
            return await self.outcome(job)
        return self.outcome

    async def fallback(self, job, reason):
        self.fallbacks.append(reason)

        async def fx(cur):
            pass
        return fx


def job(attempts=1, max_attempts=4, reclaimed=False):
    return Job(str(uuid.uuid4()), "donation_audio", "d", {"donation_id": "d"}, attempts, max_attempts, "w", reclaimed)


def run_one(handler, j):
    q = FakeQueue(j)
    asyncio.run(Worker(q, {"donation_audio": handler}, rng=random.Random(0)).run_once())
    return q


def test_success_finishes_with_effects_in_the_same_call():
    ran = []

    async def fx(cur):
        ran.append(cur)
    j = job()
    q = run_one(FakeHandler(Done({"outcome": "speech"}, fx)), j)
    assert q.finished == [(j.id, "succeeded", {"outcome": "speech"}, None)] and ran == ["cursor"]


def test_retryable_error_is_rescheduled_with_backoff():
    j = job(attempts=2)
    q = run_one(FakeHandler(Retryable("provider_timeout")), j)
    ((jid, delay, reason),) = q.retried
    assert jid == j.id and reason == "provider_timeout" and 8 <= delay <= 12 and q.finished == []


def test_retry_after_from_a_429():
    q = run_one(FakeHandler(Retryable("provider_rate_limited", 40)), job(attempts=1))
    assert q.retried[0][1] == 40


def test_last_attempt_fails_with_the_fallback():
    h = FakeHandler(Retryable("provider_unavailable"))
    j = job(attempts=4, max_attempts=4)
    q = run_one(h, j)
    assert q.retried == [] and h.fallbacks == ["provider_unavailable"]
    assert q.finished == [(j.id, "failed", {"reason": "provider_unavailable", "fallback": True}, "provider_unavailable")]


@pytest.mark.parametrize("reason", ["provider_auth", "provider_quota", "bad_prompt", "invalid_voice", "audio_too_long"])
def test_permanent_errors_fail_immediately(reason):
    h = FakeHandler(Permanent(reason))
    q = run_one(h, job(attempts=1))
    assert q.retried == [] and q.finished[0][1:] == ("failed", {"reason": reason, "fallback": True}, reason)


def test_reclaimed_job_without_attempts_left_is_not_run_again():
    h = FakeHandler(Done({}))
    q = run_one(h, job(attempts=5, max_attempts=4, reclaimed=True))
    assert h.runs == 0 and q.finished[0][1:] == ("failed", {"reason": "lease_expired", "fallback": True}, "lease_expired")


def test_unexpected_handler_error_is_retried_as_internal_error():
    q = run_one(FakeHandler(KeyError("boom")), job(attempts=1))
    assert q.retried[0][2] == "internal_error"
    q = run_one(FakeHandler(KeyError("boom")), job(attempts=4))
    assert q.finished[0][3] == "internal_error"


def test_database_error_writes_nothing_and_leaves_the_lease_to_expire():
    q = run_one(FakeHandler(psycopg.OperationalError("down")), job())
    assert q.finished == [] and q.retried == [] and q.released == []


def test_lost_lease_and_unsaved_state_are_survived():
    q = FakeQueue(job())
    q.finish_result = False
    asyncio.run(Worker(q, {"donation_audio": FakeHandler(Done({}))}).run_once())
    q = FakeQueue(job())
    q.finish_result = psycopg.OperationalError("down")
    asyncio.run(Worker(q, {"donation_audio": FakeHandler(Done({}))}).run_once())  # does not raise


def test_fallback_error_still_fails_the_job():
    class Broken(FakeHandler):
        async def fallback(self, job, reason):
            raise RuntimeError("r2 down")
    q = run_one(Broken(Permanent("provider_auth")), job())
    assert q.finished[0][1:] == ("failed", {"reason": "provider_auth", "fallback": False}, "provider_auth")


def test_graceful_shutdown_lets_short_jobs_finish_and_releases_long_ones():
    async def scenario(job_seconds, grace):
        started = asyncio.Event()

        async def slow(j):
            started.set()
            await asyncio.sleep(job_seconds)
            return Done({"outcome": "speech"})
        q = FakeQueue(job())
        w = Worker(q, {"donation_audio": FakeHandler(slow)}, concurrency=1, poll_seconds=0.01, shutdown_grace=grace)
        stop = asyncio.Event()
        runner = asyncio.create_task(w.run(stop))
        await started.wait()
        stop.set()
        await asyncio.wait_for(runner, 5)
        return q
    q = asyncio.run(scenario(0.05, grace=2))
    assert [f[1] for f in q.finished] == ["succeeded"] and q.released == []
    q = asyncio.run(scenario(10, grace=0.1))
    assert q.finished == [] and len(q.released) == 1


def test_idle_worker_polls_and_stops():
    async def scenario():
        q = FakeQueue()
        w = Worker(q, {"donation_audio": FakeHandler(Done({}))}, concurrency=2, poll_seconds=0.01)
        stop = asyncio.Event()
        task = asyncio.create_task(w.run(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 2)
        return w.processed
    assert asyncio.run(scenario()) == 0


def test_claim_errors_do_not_crash_the_worker():
    class Down(FakeQueue):
        calls = 0

        async def claim(self, worker, kinds):
            Down.calls += 1
            raise psycopg.OperationalError("db down")

    async def scenario():
        w = Worker(Down(), {"donation_audio": FakeHandler(Done({}))}, concurrency=1, poll_seconds=0.002)
        stop = asyncio.Event()
        task = asyncio.create_task(w.run(stop))
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, 2)
    asyncio.run(scenario())
    assert Down.calls >= 2
