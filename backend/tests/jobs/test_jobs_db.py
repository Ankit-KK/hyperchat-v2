"""Phase 8 on PostgreSQL (scratch database, see tests/integration.py): the queue (dedupe, transactional
enqueue, SKIP LOCKED under real concurrency, leases, fencing, retries, release, crash recovery), the enqueue at
both approval points, the worker end to end with the real handlers (R2 in memory, providers faked), moderation
audit rows, command limits, the transcription endpoints, the CLI and log safety."""

import asyncio
import json
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal

import psycopg
import pytest

from app.audio.handlers import DonationAudioHandler, VoiceTranscriptionHandler
from app.audio.repository import AudioRepository
from app.audio.silence import ensure_silence, silent_mp3
from app.dashboard.repository import DashboardRepository
from app.jobs.queue import JobQueue, enqueue
from app.jobs.retry import Backoff, Done, Retryable
from app.jobs.runner import Worker
from app.payments.repository import PaymentRepository
from app.storage.memory import InMemoryObjectStore, StoredObject
from tests.integration import (
    DOMAIN,
    ORIGIN,
    PG_HOST,
    db,
    login,
    make_donation,
    make_streamer,
    make_user,
    requires_db,
    target,
    update_streamer,
)

pytestmark = requires_db
MP3 = silent_mp3()
SECRET_MESSAGE = "a very private donor message 7f3k"
MOD_KEY = "TEST-ONLY-" + secrets.token_hex(8)


def factory():
    @asynccontextmanager
    async def connect():
        name, port = target()
        conn = await psycopg.AsyncConnection.connect(f"host={PG_HOST} port={port} dbname={name} user=hyperchat_app",
                                                     autocommit=True)
        try:
            yield conn
        finally:
            await conn.close()
    return connect


def run(coro):
    return asyncio.run(coro)


def key() -> str:
    return "authtest-" + secrets.token_hex(6)


def jobs(dedupe: str) -> list[tuple]:
    with db() as conn:
        return conn.execute("SELECT status, attempts, last_error, kind FROM public.jobs WHERE dedupe_key = %s "
                            "ORDER BY created_at", (dedupe,)).fetchall()


@pytest.fixture
def clean(env):  # env: scratch database settings + synthetic-row cleanup before and after
    yield


# ── queue ───────────────────────────────────────────────────────────────────

def test_enqueue_is_idempotent_while_active_and_reopens_after_finish(clean):
    q, k, did = JobQueue(factory()), key(), str(uuid.uuid4())

    async def scenario():
        first = await q.enqueue("donation_audio", k, {"donation_id": did})
        assert first and await q.enqueue("donation_audio", k, {"donation_id": did}) is None
        job = await q.claim("w1", ["donation_audio"])
        assert job.id == first and await q.enqueue("donation_audio", k, {"donation_id": did}) is None  # running
        assert await q.finish(job, "succeeded", result={"outcome": "x"}, last_error=None)
        return await q.enqueue("donation_audio", k, {"donation_id": did})
    assert run(scenario()) is not None
    assert [r[0] for r in jobs(k)] == ["succeeded", "queued"]


def test_enqueue_commits_or_rolls_back_with_the_callers_transaction(clean):
    k = key()

    async def scenario():
        async with factory()() as conn:
            with pytest.raises(RuntimeError):
                async with conn.transaction(), conn.cursor() as cur:
                    await enqueue(cur, "donation_audio", k, {"donation_id": str(uuid.uuid4())})
                    raise RuntimeError("the approval failed")
    run(scenario())
    assert jobs(k) == []
    with pytest.raises(ValueError):
        run(enqueue(None, "unknown_kind", k, {}))


def test_concurrent_workers_never_claim_the_same_job(clean):
    keys = [key() for _ in range(30)]
    q = JobQueue(factory())

    async def scenario():
        for k in keys:
            await q.enqueue("donation_audio", k, {"donation_id": str(uuid.uuid4())})
        claimed: list[str] = []

        async def worker(n):
            while (job := await q.claim(f"w{n}", ["donation_audio"])) is not None:
                if job.dedupe_key in keys:
                    claimed.append(job.dedupe_key)
                await asyncio.sleep(0)
        await asyncio.gather(*(worker(n) for n in range(8)))
        return claimed
    claimed = run(scenario())
    assert sorted(claimed) == sorted(keys) and len(set(claimed)) == len(keys)


def test_skip_locked_does_not_block_on_a_locked_row(clean):
    a, b = key(), key()
    q = JobQueue(factory())

    async def scenario():
        await q.enqueue("donation_audio", a, {"donation_id": str(uuid.uuid4())})
        await q.enqueue("donation_audio", b, {"donation_id": str(uuid.uuid4())})
        async with factory()() as holder, holder.transaction():  # someone holds the oldest job's row lock
            await holder.execute("SELECT 1 FROM public.jobs WHERE dedupe_key = %s FOR UPDATE", (a,))
            job = await asyncio.wait_for(q.claim("w2", ["donation_audio"]), timeout=5)
            return job.dedupe_key
    assert run(scenario()) == b


def test_expired_lease_is_reclaimed_and_the_old_worker_is_fenced_out(clean):
    k = key()
    q = JobQueue(factory(), lease_seconds=1)

    async def scenario():
        await q.enqueue("donation_audio", k, {"donation_id": str(uuid.uuid4())})
        first = await q.claim("dead-worker", ["donation_audio"])
        assert await q.claim("w2", ["donation_audio"]) is None  # lease still valid
        await asyncio.sleep(1.3)
        second = await q.claim("w2", ["donation_audio"])
        assert second.id == first.id and second.reclaimed and second.attempts == 2
        assert not await q.finish(first, "succeeded", result={}, last_error=None)   # fenced
        assert not await q.retry(first, 5, "x") and not await q.release(first)
        assert await q.finish(second, "succeeded", result={"outcome": "ok"}, last_error=None)
    run(scenario())
    assert jobs(k) == [("succeeded", 2, "lease_expired", "donation_audio")]


def test_retry_backoff_and_release(clean):
    k = key()
    q = JobQueue(factory())

    async def scenario():
        await q.enqueue("donation_audio", k, {"donation_id": str(uuid.uuid4())})
        job = await q.claim("w1", ["donation_audio"])
        assert job.dedupe_key == k and await q.retry(job, 30, "provider_timeout")
        assert await q.claim("w1", ["donation_audio"]) is None  # not available for another 30 s
        async with factory()() as conn:
            row = await (await conn.execute("SELECT status, available_at - now() > interval '25 seconds', last_error, "
                                            "locked_by FROM public.jobs WHERE id = %s", (job.id,))).fetchone()
            assert row == ("queued", True, "provider_timeout", None)
            await conn.execute("UPDATE public.jobs SET available_at = now() WHERE id = %s", (job.id,))
        again = await q.claim("w1", ["donation_audio"])
        assert again.id == job.id and again.attempts == 2 and await q.release(again)
    run(scenario())
    assert jobs(k)[0][:2] == ("queued", 1)  # the released attempt is not counted


def test_restarted_worker_recovers_a_crashed_job(clean):
    """A worker dies mid-job (lease left running). After the lease expires a new worker completes it."""
    k, did = key(), str(uuid.uuid4())
    ran = []

    class Handler:
        kind = "donation_audio"
        backoff = Backoff(0.01, 0.02)

        async def run(self, job):
            ran.append(job.attempts)
            return Done({"outcome": "recovered"})

        async def fallback(self, job, reason):
            return None

    async def scenario():
        q = JobQueue(factory(), lease_seconds=1)
        await q.enqueue("donation_audio", k, {"donation_id": did})
        crashed = await q.claim("crashed", ["donation_audio"])
        assert crashed.dedupe_key == k
        await asyncio.sleep(1.3)  # the crashed worker never finishes; its lease expires
        assert (await Worker(q, {"donation_audio": Handler()}).run_once()).id == crashed.id
    run(scenario())
    assert ran == [2] and jobs(k) == [("succeeded", 2, "lease_expired", "donation_audio")]


# ── the real handlers end to end ────────────────────────────────────────────

class FakeSpeech:
    def __init__(self, result=MP3):
        self.result, self.calls = result, []

    async def speech(self, text, voice, boost):
        self.calls.append(text)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def audio_handler(store, speech=None, enabled=True, mod_key=MOD_KEY, commands=None):
    speech = speech or FakeSpeech()
    h = DonationAudioHandler(AudioRepository(factory()), store, speech={"minimax": speech, "elevenlabs": speech},
                             sound_effects=commands, music=commands, moderation_key=mod_key,
                             default_voice="moss_audio_default", providers_enabled=enabled)
    h.backoff = Backoff(0.01, 0.02)
    return h, speech


def work(handler, target_key: str, rounds: int = 60):
    """Run a worker until the target job is finished (other synthetic jobs may be processed too)."""
    async def scenario():
        w = Worker(JobQueue(factory()), {handler.kind: handler}, poll_seconds=0.01)
        for _ in range(rounds):
            job = await w.run_once()
            state = jobs(target_key)
            if state and state[-1][0] in ("succeeded", "failed"):
                return
            if job is None:
                await asyncio.sleep(0.03)
    run(scenario())


def paid_donation(sid, *, message=SECRET_MESSAGE, amount=Decimal("100"), name="Ravi", moderation="pending", **cols):
    did = make_donation(sid, amount, payment="success", moderation=moderation, name=name, message=message)
    if cols:
        sets = ", ".join(f"{k} = %({k})s" for k in cols)
        with db() as conn:
            conn.execute(f"UPDATE public.donations SET {sets} WHERE id = %(id)s", {**cols, "id": did})
    return did


def donation_audio(did) -> str | None:
    with db() as conn:
        return conn.execute("SELECT tts_audio_url FROM public.donations WHERE id = %s", (did,)).fetchone()[0]


def test_payment_auto_approval_enqueues_atomically_and_the_worker_speaks(clean):
    sid = make_streamer("authtest-p8-pay")
    did = paid_donation(sid, moderation="pending")
    with db() as conn:  # back to an unpaid order, then complete it through the Phase 7 repository
        conn.execute("UPDATE public.donations SET payment_status = 'pending' WHERE id = %s", (did,))
    applied = run(PaymentRepository(factory()).apply(did, "captured", "pay_TESTp8" + secrets.token_hex(4)))
    assert applied.auto_approved and jobs(did) == [("queued", 0, None, "donation_audio")]
    store = InMemoryObjectStore()
    handler, speech = audio_handler(store)
    work(handler, did)
    assert jobs(did)[0][0] == "succeeded" and len(speech.calls) == 1 and SECRET_MESSAGE in speech.calls[0]
    assert donation_audio(did) == f"https://media.example.test/tts-audio/{sid}/{did}.mp3"
    assert store.objects[f"tts-audio/{sid}/{did}.mp3"].data == MP3


def test_approval_rolls_back_when_the_enqueue_fails(clean, monkeypatch):
    sid = make_streamer("authtest-p8-atomic")
    did = paid_donation(sid)
    with db() as conn:
        conn.execute("UPDATE public.donations SET payment_status = 'pending' WHERE id = %s", (did,))

    async def broken(*a, **k):
        raise psycopg.errors.CheckViolation("injected")
    monkeypatch.setattr("app.payments.repository.enqueue", broken)
    with pytest.raises(psycopg.Error):
        run(PaymentRepository(factory()).apply(did, "captured", "pay_TESTp8" + secrets.token_hex(4)))
    with db() as conn:  # neither the payment nor the approval was committed
        assert conn.execute("SELECT payment_status, moderation_status FROM public.donations WHERE id = %s",
                            (did,)).fetchone() == ("pending", "pending")
    assert jobs(did) == []


def test_manual_approval_enqueues_rejection_does_not(clean):
    sid = make_streamer("authtest-p8-manual")
    update_streamer(sid, moderation_mode="manual")
    did, other = paid_donation(sid), paid_donation(sid)
    repo = DashboardRepository(factory())
    run(repo.moderate(sid, did, "approve", actor_user_id=None, actor_label="Dashboard", notes=None))
    run(repo.moderate(sid, other, "reject", actor_user_id=None, actor_label="Dashboard", notes=None))
    assert jobs(did) == [("queued", 0, None, "donation_audio")] and jobs(other) == []
    run(repo.moderate(sid, did, "approve", actor_user_id=None, actor_label="Dashboard", notes=None))  # unchanged
    assert len(jobs(did)) == 1


def test_rejected_before_processing_is_skipped(clean):
    sid = make_streamer("authtest-p8-skip")
    did = paid_donation(sid, moderation="auto_approved")
    run(JobQueue(factory()).enqueue("donation_audio", did, {"donation_id": did}))
    with db() as conn:
        conn.execute("UPDATE public.donations SET moderation_status = 'rejected' WHERE id = %s", (did,))
    handler, speech = audio_handler(InMemoryObjectStore())
    work(handler, did)
    assert jobs(did)[0][0] == "succeeded" and speech.calls == [] and donation_audio(did) is None


def test_providers_disabled_gives_silence(clean):
    sid = make_streamer("authtest-p8-off")
    did = paid_donation(sid, moderation="auto_approved")
    run(JobQueue(factory()).enqueue("donation_audio", did, {"donation_id": did}))
    store = InMemoryObjectStore()
    handler, speech = audio_handler(store, enabled=False)
    work(handler, did)
    assert donation_audio(did) == "https://media.example.test/system/silence.mp3" and speech.calls == []
    assert store.objects["system/silence.mp3"].data == MP3


def test_retries_then_silent_fallback_after_max_attempts(clean):
    sid = make_streamer("authtest-p8-retry")
    did = paid_donation(sid, moderation="auto_approved")
    run(JobQueue(factory()).enqueue("donation_audio", did, {"donation_id": did}))
    store = InMemoryObjectStore()
    handler, speech = audio_handler(store, FakeSpeech(Retryable("provider_unavailable")))
    run(ensure_silence(store))  # as the worker does at start
    work(handler, did)
    assert jobs(did) == [("failed", 4, "provider_unavailable", "donation_audio")] and len(speech.calls) == 4
    assert donation_audio(did) == "https://media.example.test/system/silence.mp3"


def test_retry_after_success_reuses_the_stored_object(clean):
    """The provider succeeded and the audio reached R2, but the database write never happened: the retry
    finds the object and never pays the provider again."""
    sid = make_streamer("authtest-p8-reuse")
    did = paid_donation(sid, moderation="auto_approved")
    run(JobQueue(factory()).enqueue("donation_audio", did, {"donation_id": did}))
    store = InMemoryObjectStore()
    store.objects[f"tts-audio/{sid}/{did}.mp3"] = StoredObject(MP3, "audio/mpeg", "", {})
    handler, speech = audio_handler(store)
    work(handler, did)
    assert speech.calls == [] and donation_audio(did).endswith(f"/tts-audio/{sid}/{did}.mp3")


def test_existing_audio_is_never_overwritten(clean):
    sid = make_streamer("authtest-p8-keep")
    did = paid_donation(sid, moderation="auto_approved", tts_audio_url="https://media.example.test/kept.mp3")
    run(JobQueue(factory()).enqueue("donation_audio", did, {"donation_id": did}))
    handler, speech = audio_handler(InMemoryObjectStore())
    work(handler, did)
    assert donation_audio(did) == "https://media.example.test/kept.mp3" and speech.calls == []


def test_moderation_block_writes_a_hashed_audit_row(clean):
    sid = make_streamer("authtest-p8-mod")
    did = paid_donation(sid, moderation="auto_approved", message="you are a nigger", name="Supporter Rahul")
    run(JobQueue(factory()).enqueue("donation_audio", did, {"donation_id": did}))
    handler, speech = audio_handler(InMemoryObjectStore())
    work(handler, did)
    assert speech.calls == [] and donation_audio(did).endswith("/system/silence.mp3")
    with db() as conn:
        rows = conn.execute("SELECT blocked, source, category, moderation_version, supporter_identity_hash "
                            "FROM public.tts_moderation_audit WHERE donation_id = %s", (did,)).fetchall()
    assert len(rows) == 1 and rows[0][:4] == (True, "tts", "racial", "mod-1.1.0+dict-1.2.0")
    assert len(rows[0][4]) == 64 and "rahul" not in rows[0][4]


def test_probe_limit_from_the_database(clean):
    sid = make_streamer("authtest-p8-probe")
    bad = [paid_donation(sid, moderation="auto_approved", message="nigger", name="Prober") for _ in range(3)]
    handler, speech = audio_handler(InMemoryObjectStore())
    for did in bad:
        run(JobQueue(factory()).enqueue("donation_audio", did, {"donation_id": did}))
        work(handler, did)
    clean_did = paid_donation(sid, moderation="auto_approved", message="great stream", name="Prober")
    run(JobQueue(factory()).enqueue("donation_audio", clean_did, {"donation_id": clean_did}))
    work(handler, clean_did)
    assert speech.calls == [] and donation_audio(clean_did).endswith("/system/silence.mp3")
    with db() as conn:
        assert conn.execute("SELECT rule_id FROM public.tts_moderation_audit WHERE donation_id = %s",
                            (clean_did,)).fetchone() == ("probe_limit",)


def test_sfx_limit_counts_recent_paid_commands_in_the_database(clean):
    class Commands:
        def __init__(self):
            self.calls = []

        async def sound_effect(self, prompt):
            self.calls.append(prompt)
            return MP3

        async def music(self, prompt):
            self.calls.append(prompt)
            return MP3
    sid = make_streamer("authtest-p8-sfx")
    for _ in range(10):
        paid_donation(sid, moderation="auto_approved", message="!sfx boom", name="Loud", tts_audio_url="https://x/a.mp3")
    did = paid_donation(sid, moderation="auto_approved", message="!sfx boom", name="Loud")
    run(JobQueue(factory()).enqueue("donation_audio", did, {"donation_id": did}))
    commands = Commands()
    handler, _ = audio_handler(InMemoryObjectStore(), commands=commands)
    work(handler, did)
    assert commands.calls == [] and donation_audio(did).endswith("/system/silence.mp3")
    other = paid_donation(sid, moderation="auto_approved", message="!sfx boom", name="Someone Else")
    run(JobQueue(factory()).enqueue("donation_audio", other, {"donation_id": other}))
    work(handler, other)
    assert commands.calls == ["boom"] and donation_audio(other).endswith(f"/tts-audio/{sid}/{other}.mp3")


def test_worker_logs_contain_no_donor_content_or_keys(clean, caplog):
    caplog.set_level(logging.DEBUG)
    sid = make_streamer("authtest-p8-logs")
    did = paid_donation(sid, moderation="auto_approved", name="Hidden Donor Name")
    run(JobQueue(factory()).enqueue("donation_audio", did, {"donation_id": did}))
    handler, _ = audio_handler(InMemoryObjectStore())
    work(handler, did)
    text = "\n".join(str(r.__dict__) for r in caplog.records)
    assert "job succeeded" in text and SECRET_MESSAGE not in text and "Hidden Donor Name" not in text
    assert MOD_KEY not in text


# ── transcription endpoints + job ───────────────────────────────────────────

class FakeSarvam:
    model = "saaras:v3"

    def __init__(self):
        self.calls = []

    async def transcribe(self, audio):
        self.calls.append(audio)
        return "namaste, great stream"


@pytest.fixture
def tx_client(app):
    store = InMemoryObjectStore()
    service = app.app_state.transcriptions
    service.available = True
    app.store = store
    yield app


def voice_donation(client, slug, email):
    uid = make_user(email)
    sid = make_streamer(slug, user_id=uid)
    voice_key = f"voice-messages/{slug}_1800000000000_{secrets.token_hex(16)}.webm"
    client.store.objects[voice_key] = StoredObject(b"OPUS-BYTES", "audio/webm", "", {})
    did = paid_donation(sid, moderation="approved", voice_message_url=f"https://media.example.test/{voice_key}")
    r = login(client, email)
    assert r.status_code == 200
    return sid, did, {**ORIGIN, "x-csrf-token": r.json()["csrf_token"]}


def url(slug, did):
    return f"/api/v1/dashboard/streamers/{slug}/donations/{did}/transcription"


def test_transcription_request_runs_once_and_is_served_from_storage(tx_client):
    slug = "authtest-p8-tx"
    sid, did, headers = voice_donation(tx_client, slug, "p8tx" + DOMAIN)
    assert tx_client.post(url(slug, did), headers=ORIGIN).status_code == 403          # no CSRF token
    assert tx_client.get(url(slug, did)).json() == {"status": "none", "transcript": None}
    r = tx_client.post(url(slug, did), headers=headers)
    assert (r.status_code, r.json()) == (202, {"status": "queued", "transcript": None})
    assert r.headers["cache-control"] == "no-store"
    assert tx_client.post(url(slug, did), headers=headers).json()["status"] == "queued"  # deduplicated
    sarvam = FakeSarvam()
    handler = VoiceTranscriptionHandler(AudioRepository(factory()), tx_client.store, sarvam)
    work(handler, did)
    assert sarvam.calls == [b"OPUS-BYTES"]
    assert tx_client.get(url(slug, did)).json() == {"status": "completed", "transcript": "namaste, great stream"}
    r = tx_client.post(url(slug, did), headers=headers)
    assert (r.status_code, r.json()["status"]) == (200, "completed") and len(jobs(did)) == 1  # never re-run
    with db() as conn:
        assert conn.execute("SELECT count(*), max(model) FROM public.donation_transcripts WHERE donation_id = %s",
                            (did,)).fetchone() == (1, "saaras:v3")


def test_transcription_authorization_and_scoping(tx_client):
    slug = "authtest-p8-txa"
    sid, did, headers = voice_donation(tx_client, slug, "p8txa" + DOMAIN)
    other_sid = make_streamer("authtest-p8-txb", user_id=make_user("p8txb" + DOMAIN))
    foreign = paid_donation(other_sid, moderation="approved", voice_message_url="https://media.example.test/x.webm")
    text_only = paid_donation(sid, moderation="approved")
    assert tx_client.post(url(slug, foreign), headers=headers).status_code == 404       # another creator's donation
    assert tx_client.post(url(slug, "not-a-uuid"), headers=headers).status_code == 404
    assert tx_client.post(url(slug, text_only), headers=headers).status_code == 409    # no voice message
    assert tx_client.post(url("authtest-p8-txb", foreign), headers=headers).status_code == 403  # not their creator
    tx_client.cookies.clear()
    assert tx_client.get(url(slug, did)).status_code == 401 and tx_client.post(url(slug, did), headers=ORIGIN).status_code in (401, 403)


def test_transcription_rate_limit_and_unavailable(tx_client):
    slug = "authtest-p8-txr"
    sid, did, headers = voice_donation(tx_client, slug, "p8txr" + DOMAIN)
    with db() as conn:
        conn.execute("INSERT INTO public.rate_limits (ip_address, endpoint, request_count, window_start) "
                     "VALUES (%s, 'transcription:request', 30, now())", (f"streamer:{sid}",))
    r = tx_client.post(url(slug, did), headers=headers)
    assert (r.status_code, r.json()) == (429, {"error": "Too many transcription requests. Please try again later."})
    tx_client.app_state.transcriptions.available = False
    assert tx_client.post(url(slug, did), headers=headers).status_code == 503
    assert jobs(did) == []


def test_transcription_job_never_reads_a_foreign_url(clean):
    sid = make_streamer("authtest-p8-ssrf")
    did = paid_donation(sid, moderation="approved", voice_message_url="http://169.254.169.254/latest/meta-data")
    run(JobQueue(factory()).enqueue("voice_transcription", did, {"donation_id": did}))
    sarvam = FakeSarvam()
    work(VoiceTranscriptionHandler(AudioRepository(factory()), InMemoryObjectStore(), sarvam), did)
    assert jobs(did) == [("failed", 1, "voice_invalid", "voice_transcription")] and sarvam.calls == []


# ── CLI ─────────────────────────────────────────────────────────────────────

def test_cli_enqueue_donation_audio(clean, capsys):
    from app.cli import main
    sid = make_streamer("authtest-p8-cli")
    did = paid_donation(sid, moderation="auto_approved")
    assert main(["enqueue-donation-audio", did]) == 0
    assert jobs(did) == [("queued", 0, None, "donation_audio")]
    assert main(["enqueue-donation-audio", did]) == 0 and "already queued" in capsys.readouterr().out
    assert main(["enqueue-donation-audio", "nope"]) == 2
    assert main(["enqueue-donation-audio", str(uuid.uuid4())]) == 1
    out = capsys.readouterr()
    assert SECRET_MESSAGE not in out.out + out.err


def test_job_rows_hold_ids_only(clean):
    sid = make_streamer("authtest-p8-payload")
    did = paid_donation(sid)
    run(DashboardRepository(factory()).moderate(sid, did, "approve", actor_user_id=None, actor_label="D", notes=None))
    with db() as conn:
        payload, kind, max_attempts = conn.execute(
            "SELECT payload, kind, max_attempts FROM public.jobs WHERE dedupe_key = %s", (did,)).fetchone()
    assert payload == {"donation_id": did} and kind == "donation_audio" and max_attempts == 4
    assert SECRET_MESSAGE not in json.dumps(payload)
