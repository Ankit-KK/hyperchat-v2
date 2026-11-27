"""Phase 9 on PostgreSQL (scratch database, see tests/integration.py):

  * the outbox rows are written in the SAME transaction as payment completion, moderation, settings and the
    Phase 8 audio job (and roll back with them)
  * the queue: per-creator FIFO, SKIP LOCKED under real concurrency, leases, fencing, retry, release, expiry,
    coalescing of pending rows, disabled -> skipped, cleanup retention
  * the publisher end to end against a mocked Pusher transport (no real Pusher, no real credentials)
  * the channel-auth and config endpoints: owner / linked / admin / other creator / unclaimed, CSRF + Origin,
    OBS tokens and scopes, malformed input, rate limits, and log safety
"""

import asyncio
import hashlib
import hmac
import json
import logging
import random
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from app.audio.silence import ensure_silence
from app.core.config import get_settings
from app.dashboard.repository import DashboardRepository
from app.jobs.queue import JobQueue
from app.maintenance import cleanup
from app.payments.repository import PaymentRepository, WebhookEvent
from app.obs.tokens import TokenRepository
from app.realtime import outbox
from app.realtime.channels import dashboard_channel_name, token_tag
from app.realtime.outbox import OutboxQueue
from app.realtime.payloads import Renderer, RealtimeRepository
from app.realtime.publisher import OutboxPublisher
from app.realtime.pusher import PusherApp, PusherApps, PusherClient
from app.security.crypto import sha256_hex
from app.storage.memory import InMemoryObjectStore
from tests.integration import (
    DOMAIN,
    ORIGIN,
    CaptureSender,
    FakeGoogle,
    LogCapture,
    db,
    fixture_db,
    login,
    make_donation,
    make_streamer,
    make_user,
    requires_db,
    update_streamer,
)
from tests.jobs.test_jobs_db import audio_handler, factory, run, work
from tests.stepup.test_stepup_enforcement_db import set_enforcement

pytestmark = requires_db
SECRET = "TEST-ONLY-pusher-secret-" + secrets.token_hex(6)
APP = PusherApp(1, "424242", "testkey4242", SECRET, "ap2")
MESSAGE = "a private donor message zq81"


def outbox_rows(streamer_id: str) -> list[tuple]:
    with db() as conn:
        return conn.execute("SELECT event, channel, subject_id::text, facts, status FROM public.realtime_outbox "
                            "WHERE streamer_id = %s ORDER BY id", (streamer_id,)).fetchall()


def events(streamer_id: str) -> list[str]:
    return [r[0] for r in outbox_rows(streamer_id)]


@pytest.fixture
def clean(env):
    yield


@pytest.fixture
def only(strict_scratch_db, env):
    """Queue/publisher tests claim whatever is pending: scratch database only, outbox emptied first."""
    with db() as conn:
        conn.execute("DELETE FROM public.realtime_outbox")
    yield


def pending_payment(sid, **cols):
    did = make_donation(sid, 100, payment="pending", moderation="pending", name="Ravi", message=MESSAGE)
    if cols:
        with db() as conn:
            sets = ", ".join(f"{k} = %({k})s" for k in cols)
            conn.execute(f"UPDATE public.donations SET {sets} WHERE id = %(id)s", {**cols, "id": did})
    return did


# ── the outbox commits with the change that caused it ───────────────────────

def test_payment_completion_writes_events_in_the_same_transaction(clean):
    sid = make_streamer("authtest-p9-pay")
    did = pending_payment(sid)
    applied = run(PaymentRepository(factory()).apply(did, "captured", "pay_TESTp9" + secrets.token_hex(4)))
    assert applied.auto_approved
    assert outbox_rows(sid) == [("new-donation", "dashboard", did, {}, "pending"),
                                ("goal-progress", "goal", None, {}, "pending"),
                                ("leaderboard-updated", "leaderboard", None, {}, "pending")]


def test_manual_moderation_payment_announces_the_donation_only(clean):
    sid = make_streamer("authtest-p9-manual")
    update_streamer(sid, text_approval_required=True)
    did = pending_payment(sid)
    run(PaymentRepository(factory()).apply(did, "captured", "pay_TESTp9" + secrets.token_hex(4)))
    assert events(sid) == ["new-donation"]
    run(PaymentRepository(factory()).apply(did, "captured", "pay_TESTp9" + secrets.token_hex(4)))  # already paid
    assert events(sid) == ["new-donation"]


def test_failed_payment_writes_nothing(clean):
    sid = make_streamer("authtest-p9-fail")
    did = pending_payment(sid)
    run(PaymentRepository(factory()).apply(did, "failed", None))
    assert events(sid) == []


def test_duplicate_webhook_rolls_the_events_back(clean):
    sid = make_streamer("authtest-p9-dup")
    did = pending_payment(sid)
    event = WebhookEvent("authtest-evt-" + secrets.token_hex(4), "payment.captured", "primary", None, None,
                         datetime.now(UTC))
    repo = PaymentRepository(factory())
    assert run(repo.record_event(event, "completed"))  # the same event id was processed before
    assert run(repo.apply(did, "captured", "pay_TESTp9" + secrets.token_hex(4), event)).result == "duplicate"
    assert events(sid) == []
    with db() as conn:
        assert conn.execute("SELECT payment_status FROM public.donations WHERE id = %s", (did,)).fetchone()[0] == "pending"


def test_a_failing_emit_rolls_the_payment_back(clean, monkeypatch):
    sid = make_streamer("authtest-p9-atomic")
    did = pending_payment(sid)

    async def broken(*a, **k):
        raise psycopg.errors.CheckViolation("injected")
    monkeypatch.setattr("app.payments.repository.outbox.emit", broken)
    with pytest.raises(psycopg.Error):
        run(PaymentRepository(factory()).apply(did, "captured", "pay_TESTp9" + secrets.token_hex(4)))
    with db() as conn:
        assert conn.execute("SELECT payment_status FROM public.donations WHERE id = %s", (did,)).fetchone()[0] == "pending"
        assert conn.execute("SELECT count(*) FROM public.jobs WHERE dedupe_key = %s", (did,)).fetchone()[0] == 0
    assert events(sid) == []


def test_moderation_events(clean):
    sid = make_streamer("authtest-p9-mod")
    did = make_donation(sid, 100, payment="success", moderation="pending", message=MESSAGE)
    repo = DashboardRepository(factory())

    def moderate(action):
        run(repo.moderate(sid, did, action, actor_user_id=None, actor_label="Dashboard", notes=None))
    moderate("approve")
    assert outbox_rows(sid) == [
        ("donation-updated", "dashboard", did, {"action": "approve", "previous_status": "pending"}, "pending"),
        ("goal-progress", "goal", None, {}, "pending"), ("leaderboard-updated", "leaderboard", None, {}, "pending")]
    moderate("approve")  # unchanged: nothing written
    moderate("hide_message")  # no totals change: the dashboard only
    assert events(sid)[3:] == ["donation-updated"]
    moderate("reject")  # debit: goal + leaderboard again (the first ones are still pending: coalesced)
    assert events(sid)[4:] == ["donation-updated"]
    assert outbox_rows(sid)[4][3] == {"action": "reject", "previous_status": "approved"}


def test_settings_events_and_coalescing(clean):
    sid = make_streamer("authtest-p9-set")
    repo = DashboardRepository(factory())
    run(repo.update_settings(sid, {"tts_enabled": False, "text_approval_required": True}))
    assert events(sid) == []  # not pushed live
    run(repo.update_settings(sid, {"brand_color": "#112233"}))
    run(repo.update_settings(sid, {"brand_color": "#445566", "goal_name": "Mic"}))
    run(repo.update_settings(sid, {"brand_color": "#445566"}))  # no change: no UPDATE, no event
    assert events(sid) == ["settings-updated", "goal-progress"]  # the second brand change coalesced
    with db() as conn:  # once the first is no longer pending, a new change gets its own row
        conn.execute("UPDATE public.realtime_outbox SET status = 'published' WHERE streamer_id = %s", (sid,))
    run(repo.update_settings(sid, {"brand_color": "#778899"}))
    assert events(sid) == ["settings-updated", "goal-progress", "settings-updated"]


def test_audio_job_writes_audio_ready_with_its_completion(clean):
    sid = make_streamer("authtest-p9-audio")
    did = make_donation(sid, 100, payment="success", moderation="auto_approved", message=MESSAGE)
    run(JobQueue(factory()).enqueue("donation_audio", did, {"donation_id": did}))
    store = InMemoryObjectStore()
    run(ensure_silence(store))
    handler, _ = audio_handler(store, enabled=False)  # providers off: silence
    work(handler, did)
    assert outbox_rows(sid) == [("audio-ready", "audio", did, {}, "pending")]


def test_audio_job_for_a_rejected_donation_announces_nothing(clean):
    sid = make_streamer("authtest-p9-audio2")
    did = make_donation(sid, 100, payment="success", moderation="auto_approved", message=MESSAGE)
    run(JobQueue(factory()).enqueue("donation_audio", did, {"donation_id": did}))
    with db() as conn:
        conn.execute("UPDATE public.donations SET moderation_status = 'rejected' WHERE id = %s", (did,))
    store = InMemoryObjectStore()
    handler, _ = audio_handler(store, enabled=False)
    work(handler, did)
    assert events(sid) == []


def test_audio_ready_is_conditional_in_sql(clean):
    sid = make_streamer("authtest-p9-cond")
    ready = make_donation(sid, 100, payment="success", moderation="approved")
    no_audio = make_donation(sid, 100, payment="success", moderation="approved")
    unpaid = make_donation(sid, 100, payment="pending", moderation="pending")
    with db() as conn:
        conn.execute("UPDATE public.donations SET tts_audio_url = 'https://x/y.mp3' WHERE id = ANY(%s::uuid[])",
                     ([ready, unpaid],))

    async def go():
        async with factory()() as conn, conn.transaction(), conn.cursor() as cur:
            return [await outbox.emit_audio_ready(cur, d) for d in (ready, no_audio, unpaid)]
    assert run(go()) == [True, False, False] and events(sid) == ["audio-ready"]


def test_emit_rolls_back_with_its_transaction(clean):
    sid = make_streamer("authtest-p9-rb")

    async def go():
        async with factory()() as conn:
            with pytest.raises(RuntimeError):
                async with conn.transaction(), conn.cursor() as cur:
                    await outbox.emit(cur, sid, "settings-updated")
                    raise RuntimeError("the caller's change failed")
    run(go())
    assert events(sid) == []


# ── the queue ───────────────────────────────────────────────────────────────

def emit_rows(sid, *names):
    async def go():
        async with factory()() as conn, conn.transaction(), conn.cursor() as cur:
            for name in names:
                await outbox.emit(cur, sid, name, subject_id=None if name in outbox.COALESCED else str(uuid.uuid4()))
    run(go())


def test_per_creator_fifo_and_one_row_per_creator_per_claim(only):
    a, b = make_streamer("authtest-p9-fa"), make_streamer("authtest-p9-fb")
    emit_rows(a, "new-donation", "donation-updated", "settings-updated")
    emit_rows(b, "new-donation")
    q = OutboxQueue(factory())
    first = run(q.claim("w1", 10))
    assert sorted((e.streamer_id, e.event) for e in first) == sorted([(a, "new-donation"), (b, "new-donation")])
    assert run(q.claim("w2", 10)) == []  # a's next event waits for its head
    head = next(e for e in first if e.streamer_id == a)
    assert run(q.finish(head, "published"))
    (second,) = run(q.claim("w2", 10))
    assert (second.streamer_id, second.event) == (a, "donation-updated")


def test_concurrent_claimers_never_share_a_row(only):
    sids = [make_streamer(f"authtest-p9-c{i}") for i in range(12)]
    for sid in sids:
        emit_rows(sid, "new-donation")

    async def go():
        q = OutboxQueue(factory())
        results = await asyncio.gather(*(q.claim(f"w{i}", 3) for i in range(6)))
        return [e.id for batch in results for e in batch]
    ids = run(go())
    assert len(ids) == len(set(ids)) == 12


def test_expired_lease_is_reclaimed_and_the_old_worker_fenced_out(only):
    sid = make_streamer("authtest-p9-lease")
    emit_rows(sid, "new-donation")
    q = OutboxQueue(factory(), lease_seconds=1)
    (old,) = run(q.claim("w1", 5))
    with db() as conn:
        conn.execute("UPDATE public.realtime_outbox SET locked_until = now() - interval '1 second' WHERE id = %s",
                     (old.id,))
    (new,) = run(q.claim("w2", 5))
    assert new.id == old.id and new.reclaimed and new.attempts == 2
    assert run(q.finish(old, "published")) is False and run(q.retry(old, 1, "x")) is False
    assert run(q.finish(new, "published")) is True
    with db() as conn:
        assert conn.execute("SELECT status, last_error, published_at IS NOT NULL FROM public.realtime_outbox "
                            "WHERE id = %s", (old.id,)).fetchone() == ("published", "lease_expired", True)


def test_retry_release_expire_and_skip(only):
    sid = make_streamer("authtest-p9-states")
    emit_rows(sid, "new-donation")
    q = OutboxQueue(factory(), max_age_seconds=60)
    (e,) = run(q.claim("w1", 5))
    assert run(q.retry(e, 30, "pusher_server_error"))
    assert run(q.claim("w1", 5)) == []  # not due yet (and it blocks the creator's later rows)
    with db() as conn:
        conn.execute("UPDATE public.realtime_outbox SET available_at = now() WHERE id = %s", (e.id,))
    (e,) = run(q.claim("w1", 5))
    assert e.attempts == 2 and run(q.release(e))
    with db() as conn:
        assert conn.execute("SELECT status, attempts FROM public.realtime_outbox WHERE id = %s",
                            (e.id,)).fetchone() == ("pending", 1)
        conn.execute("UPDATE public.realtime_outbox SET created_at = now() - interval '2 minutes' WHERE id = %s", (e.id,))
    assert run(q.expire()) == 1
    emit_rows(sid, "new-donation")
    assert run(q.skip_pending("realtime_disabled")) == 1
    assert [r[4] for r in outbox_rows(sid)] == ["expired", "skipped"]


def test_constraints(clean):
    sid = make_streamer("authtest-p9-ck")
    ins = "INSERT INTO public.realtime_outbox (streamer_id, channel, event, facts, status, locked_by) VALUES (%s, %s, %s, %s, %s, %s)"
    with db() as conn:
        for bad in (("overlay", "new-donation", "{}", "pending", None), ("dashboard", "stats-update", "{}", "pending", None),
                    ("dashboard", "new-donation", "[]", "pending", None),
                    ("dashboard", "new-donation", json.dumps({"x": "y" * 600}), "pending", None),
                    ("dashboard", "new-donation", "{}", "publishing", None), ("dashboard", "new-donation", "{}", "sent", None)):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(ins, (sid, *bad))
        conn.execute("DELETE FROM public.streamers WHERE id = %s", (sid,))  # rows go with their creator


def test_cleanup_keeps_open_rows_and_recent_finished_rows(only):
    sid = make_streamer("authtest-p9-clean")
    emit_rows(sid, "new-donation", "donation-updated", "settings-updated")
    with db() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM public.realtime_outbox WHERE streamer_id = %s ORDER BY id",
                                          (sid,))]
        conn.execute("UPDATE public.realtime_outbox SET status = 'published', created_at = now() - interval '8 days' "
                     "WHERE id = %s", (ids[0],))
        conn.execute("UPDATE public.realtime_outbox SET status = 'failed' WHERE id = %s", (ids[1],))  # recent
        conn.execute("UPDATE public.realtime_outbox SET created_at = now() - interval '8 days' WHERE id = %s",
                     (ids[2],))  # old but still pending: never deleted
    counts = run(cleanup(factory(), dry_run=False))
    assert counts["realtime_outbox"] >= 1
    assert [r[4] for r in outbox_rows(sid)] == ["failed", "pending"]


# ── the publisher end to end (mocked Pusher) ────────────────────────────────

def publisher(handler, *, enabled=True, apps=None):
    return OutboxPublisher(OutboxQueue(factory()), Renderer(RealtimeRepository(factory())),
                           PusherClient(httpx.MockTransport(handler)), apps or PusherApps({1: APP}), enabled=enabled,
                           rate=1000, rng=random.Random(2), ident="w-test")


def test_publisher_end_to_end(only):
    owner = make_user("e2e-owner" + DOMAIN)
    sid = make_streamer("authtest-p9-e2e", user_id=owner)
    token = obs_token(sid)  # overlay events go to the channels of active tokens (NV-4)
    owner_session = session_row(owner)  # dashboard events go to the sessions with access (NV-4b)
    update_streamer(sid, status="active", goal_is_active=True, goal_activated_at=datetime(2026, 1, 1, tzinfo=UTC),
                    goal_target_amount=Decimal("1000"), goal_name="Mic")
    did = make_donation(sid, 100, payment="success", moderation="pending", message=MESSAGE, name="Ravi")
    run(DashboardRepository(factory()).moderate(sid, did, "approve", actor_user_id=None, actor_label="D", notes=None))
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append((body["channels"][0], body["name"], json.loads(body["data"])))
        return httpx.Response(200, json={})
    p = publisher(handler)
    for _ in range(5):
        run(p.run_once())
    assert [(c, n) for c, n, _ in sent] == [(dashboard_channel_name("authtest-p9-e2e", owner_session),
                                             "donation-updated"),
                                            ("authtest-p9-e2e-goal", "goal-progress"),
                                            (own("private-authtest-p9-e2e-leaderboard", token), "leaderboard-updated")]
    dashboard, goal, board = (d for _, _, d in sent)
    assert dashboard["donation"]["id"] == did and dashboard["donation"]["message"] == MESSAGE
    assert dashboard["action"] == "approve" and goal["progress_inr"] == 100.0 and goal["target_amount_inr"] == 1000.0
    assert board["top_donor"] == {"name": "Ravi", "total_amount": 100.0}
    assert MESSAGE not in json.dumps(goal) and MESSAGE not in json.dumps(board) and sid not in json.dumps(sent)
    assert [r[4] for r in outbox_rows(sid)] == ["published"] * 3


def test_publisher_transient_failure_then_success(only):
    sid = make_streamer("authtest-p9-retry")
    obs_token(sid)
    emit_rows(sid, "settings-updated")
    responses = [httpx.Response(503), httpx.Response(200)]
    p = publisher(lambda request: responses.pop(0))
    p.backoff = type(p.backoff)(0.01, 0.02)
    run(p.run_once())
    with db() as conn:
        assert conn.execute("SELECT status, attempts, last_error FROM public.realtime_outbox WHERE streamer_id = %s",
                            (sid,)).fetchone() == ("pending", 1, "pusher_server_error")
    asyncio.run(asyncio.sleep(0.05))
    run(p.run_once())
    assert [r[4] for r in outbox_rows(sid)] == ["published"]


def test_publisher_permanent_failure_and_disabled(only):
    sid = make_streamer("authtest-p9-perm")
    obs_token(sid)
    emit_rows(sid, "settings-updated")
    run(publisher(lambda request: httpx.Response(401)).run_once())
    with db() as conn:
        assert conn.execute("SELECT status, last_error FROM public.realtime_outbox WHERE streamer_id = %s",
                            (sid,)).fetchone() == ("failed", "pusher_auth_failed")
    emit_rows(sid, "settings-updated")
    calls = []
    run(publisher(lambda request: calls.append(1) or httpx.Response(200), enabled=False).run_once())
    assert calls == [] and [r[4] for r in outbox_rows(sid)] == ["failed", "skipped"]
    emit_rows(sid, "settings-updated")
    run(publisher(lambda request: calls.append(1) or httpx.Response(200), apps=PusherApps({})).run_once())
    with db() as conn:
        assert conn.execute("SELECT status, last_error FROM public.realtime_outbox WHERE streamer_id = %s "
                            "ORDER BY id DESC LIMIT 1", (sid,)).fetchone() == ("skipped", "pusher_not_configured")
    assert calls == []


# ── NV-4: a revoked OBS token's channels get no further events ─────────────

def token_id(token: str) -> str:
    with db() as conn:
        return str(conn.execute("SELECT id FROM public.obs_tokens WHERE token_hash = %s", (sha256_hex(token),)).fetchone()[0])


def test_a_revoked_token_gets_no_further_overlay_events_and_a_new_token_does(only):
    """Pusher can never drop a subscription it signed, so revoking works by not publishing to the token's channels:
    an overlay still connected with the revoked token receives nothing from the next event on."""
    sid = make_streamer("authtest-p9-revoke")
    base = "private-authtest-p9-revoke-settings"
    old, keep = obs_token(sid), obs_token(sid)
    sent = []

    def handler(request):
        sent.append(sorted(json.loads(request.content)["channels"]))
        return httpx.Response(200, json={})
    p = publisher(handler)
    emit_rows(sid, "settings-updated")
    run(p.run_once())
    assert sent == [sorted([own(base, old), own(base, keep)])]  # one Pusher request, one channel per active token
    assert run(TokenRepository(factory()).revoke(sid, token_id(old)))["state"] == "revoked"  # the dashboard's revoke
    emit_rows(sid, "settings-updated")
    run(p.run_once())
    assert sent[-1] == [own(base, keep)]  # the revoked token's channel is no longer published to
    new = obs_token(sid)
    emit_rows(sid, "settings-updated")
    run(p.run_once())
    assert sent[-1] == sorted([own(base, keep), own(base, new)])  # a new token is served at once
    for t in (keep, new):
        run(TokenRepository(factory()).revoke(sid, token_id(t)))
    emit_rows(sid, "settings-updated")
    run(p.run_once())
    assert len(sent) == 3 and [r[4] for r in outbox_rows(sid)][-1] == "skipped"  # no active token: nothing sent
    with db() as conn:
        assert conn.execute("SELECT last_error FROM public.realtime_outbox WHERE streamer_id = %s ORDER BY id DESC "
                            "LIMIT 1", (sid,)).fetchone() == ("no_active_obs_token",)


def dashboard_publish(p, sid: str, sent: list) -> list[str]:
    """Approve a new donation of `sid` and publish: the sorted channels its donation-updated event was sent to
    ([] when it went nowhere)."""
    before = len(sent)
    did = make_donation(sid, 100, payment="success", moderation="pending", name="Ravi")
    run(DashboardRepository(factory()).moderate(sid, did, "approve", actor_user_id=None, actor_label="D", notes=None))
    for _ in range(3):
        run(p.run_once())
    return sorted(c for name, channels in sent[before:] if name == "donation-updated" for c in channels)


def test_losing_dashboard_access_stops_delivery_at_the_next_event_and_a_new_grant_works_at_once(only):
    """NV-4b: the dashboard channel is per session and published only to sessions with access right now, so every
    way access ends (app.authz.access and the session rules) reaches a connection that is already open."""
    slug = "authtest-p9-dash"
    owner, linked, admin = (make_user(e + DOMAIN, **kw) for e, kw in (
        ("dash-owner", {}), ("dash-linked", {}), ("dash-admin", {"role": "admin"})))
    sid = make_streamer(slug, user_id=owner)
    with fixture_db() as conn:
        conn.execute("UPDATE public.users SET streamer_id = %s WHERE id = %s", (sid, linked))
    stranger = make_user("dash-stranger" + DOMAIN)
    o1, o2, ln, ad, st = (session_row(u) for u in (owner, owner, linked, admin, stranger))
    ch = {k: dashboard_channel_name(slug, v) for k, v in {"o1": o1, "o2": o2, "ln": ln, "ad": ad, "st": st}.items()}
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append((body["name"], body["channels"]))
        return httpx.Response(200, json={})
    p = publisher(handler)
    assert dashboard_publish(p, sid, sent) == sorted([ch["o1"], ch["o2"], ch["ln"], ch["ad"]])  # never the stranger
    with fixture_db() as conn:
        conn.execute("UPDATE public.sessions SET revoked_at = now() WHERE id = %s", (o1,))  # logout / revoke
        conn.execute("UPDATE public.users SET streamer_id = NULL WHERE id = %s", (linked,))  # link removed
    assert dashboard_publish(p, sid, sent) == sorted([ch["o2"], ch["ad"]])
    set_enforcement("active")  # an administrator now needs passkey assurance (aal2), which this session lacks
    try:
        assert dashboard_publish(p, sid, sent) == [ch["o2"]]
    finally:
        set_enforcement("draft")
    with fixture_db() as conn:
        conn.execute("UPDATE public.users SET role = 'streamer' WHERE id = %s", (admin,))  # admin role removed
    assert dashboard_publish(p, sid, sent) == [ch["o2"]]
    with fixture_db() as conn:
        conn.execute("UPDATE public.sessions SET idle_expires_at = now() - interval '1 minute' WHERE id = %s", (o2,))
    assert dashboard_publish(p, sid, sent) == []  # expired: nothing sent at all
    o3 = session_row(owner)
    with fixture_db() as conn:
        conn.execute("UPDATE public.users SET is_active = false WHERE id = %s", (owner,))  # deactivated
    assert dashboard_publish(p, sid, sent) == []
    new_owner = make_user("dash-new-owner" + DOMAIN)
    n1 = session_row(new_owner)
    with fixture_db() as conn:
        conn.execute("UPDATE public.users SET is_active = true WHERE id = %s", (owner,))
    update_streamer(sid, user_id=new_owner)  # owner transfer: the old owner's live session loses access
    assert dashboard_publish(p, sid, sent) == [dashboard_channel_name(slug, n1)]  # the new owner at once
    staff = make_user("dash-new-staff" + DOMAIN, streamer_id=sid)
    s1 = session_row(staff)
    assert dashboard_publish(p, sid, sent) == sorted([dashboard_channel_name(slug, n1),
                                                      dashboard_channel_name(slug, s1)])  # a new link at once
    assert dashboard_channel_name(slug, o3) not in sum((c for _, c in sent), [])


def test_dashboard_and_goal_channels_are_not_per_token(only):
    sid = make_streamer("authtest-p9-shared")
    update_streamer(sid, status="active", goal_is_active=True, goal_activated_at=datetime(2026, 1, 1, tzinfo=UTC),
                    goal_target_amount=Decimal("1000"), goal_name="Mic")
    sent = []

    def handler(request):
        sent.append(json.loads(request.content)["channels"])
        return httpx.Response(200, json={})
    emit_rows(sid, "goal-progress")
    run(publisher(handler).run_once())
    assert sent == [["authtest-p9-shared-goal"]]  # no OBS token needed, the same public name as before


# ── channel authorization (the real app) ────────────────────────────────────

@pytest.fixture
def rt(env, monkeypatch):
    """The real app with realtime enabled and TEST-ONLY Pusher values for group 1 (signing only: no network)."""
    for key, value in {"HYPERCHAT_REALTIME_ENABLED": "true", "HYPERCHAT_PUSHER_APP_ID_1": APP.app_id,
                       "HYPERCHAT_PUSHER_KEY_1": APP.key, "HYPERCHAT_PUSHER_SECRET_1": SECRET,
                       "HYPERCHAT_PUSHER_CLUSTER_1": APP.cluster}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    from app.main import create_app
    application = create_app()
    capture = LogCapture()
    logging.getLogger().addHandler(capture)
    with TestClient(application, base_url="https://testserver") as client:
        application.state.auth.sender = CaptureSender()
        application.state.auth.google = FakeGoogle()
        client.capture = capture
        yield client
    logging.getLogger().removeHandler(capture)
    with db() as conn:
        conn.execute("DELETE FROM public.rate_limits WHERE endpoint LIKE 'realtime:%'")


AUTH = "/api/v1/realtime/pusher/auth"


def expected(socket_id, channel):
    return f"{APP.key}:" + hmac.new(SECRET.encode(), f"{socket_id}:{channel}".encode(), hashlib.sha256).hexdigest()


def session(client, slug, email, *, role="streamer", owner=True, **user):
    uid = make_user(email, role=role, **user)
    sid = make_streamer(slug, user_id=uid if owner else None)
    r = login(client, email)
    assert r.status_code == 200, r.text
    return sid, {**ORIGIN, "x-csrf-token": r.json()["csrf_token"]}


def obs_token(sid, **cols) -> str:
    token = "authtest-obs-" + secrets.token_urlsafe(24)
    values = {"is_active": True, "expires_at": None, "revoked_at": None, "pusher_channel_scope": None, **cols}
    with db() as conn:
        conn.execute("INSERT INTO public.obs_tokens (streamer_id, token_hash, is_active, expires_at, revoked_at, "
                     "pusher_channel_scope) VALUES (%s, %s, %s, %s, %s, %s)",
                     (sid, sha256_hex(token), values["is_active"], values["expires_at"], values["revoked_at"],
                      values["pusher_channel_scope"]))
    return token


def session_id_of(cookies) -> str:
    """The id of the session behind a cookie jar (a TestClient's or a Browser's)."""
    token = cookies.get(get_settings().session_cookie_name)
    with db() as conn:
        return conn.execute("SELECT id::text FROM public.sessions WHERE token_hash = %s",
                            (sha256_hex(token),)).fetchone()[0]


def dash(cookies, slug: str) -> str:
    """This session's own dashboard channel name (NV-4b): what GET .../realtime hands it."""
    return dashboard_channel_name(slug, session_id_of(cookies))


def session_row(user_id: str, **cols) -> str:
    """A valid session for `user_id` written directly (publisher tests need no browser); returns its id."""
    values = {"idle": "1 day", "absolute": "2 days", **cols}
    with fixture_db() as conn:
        return str(conn.execute(
            "INSERT INTO public.sessions (user_id, token_hash, csrf_token_hash, idle_expires_at, absolute_expires_at) "
            "VALUES (%s, %s, %s, now() + %s::interval, now() + %s::interval) RETURNING id",
            (user_id, secrets.token_hex(32), secrets.token_hex(32), values["idle"], values["absolute"])).fetchone()[0])


def own(channel: str, token: str) -> str:
    """The per-token name of an overlay channel (NV-4): what GET /obs/session hands this token."""
    return f"{channel}-t{token_tag(sha256_hex(token))}"


def auth(client, channel, headers=None, socket_id="1234.5678", **kw):
    return client.post(AUTH, data={"socket_id": socket_id, "channel_name": channel}, headers=headers or {}, **kw)


def test_owner_gets_a_valid_dashboard_signature(rt):
    sid, h = session(rt, "authtest-p9-own", "own" + DOMAIN)
    channel = dash(rt.cookies, "authtest-p9-own")
    cfg = rt.get("/api/v1/dashboard/streamers/authtest-p9-own/realtime")
    assert cfg.status_code == 200 and cfg.json() == {"key": APP.key, "cluster": "ap2", "auth_endpoint": AUTH,
                                                     "channel": channel}  # the client contract (NV-4b)
    assert cfg.headers["cache-control"] == "no-store"
    r = auth(rt, channel, h)
    assert r.status_code == 200 and r.json() == {"auth": expected("1234.5678", channel)}
    assert r.headers["cache-control"] == "no-store"
    assert auth(rt, "private-authtest-p9-own-dashboard", h).status_code == 403  # the untagged name (NV-4b)
    other = dashboard_channel_name("authtest-p9-own", "00000000-0000-4000-8000-000000000000")
    assert auth(rt, other, h).status_code == 403  # another session's name
    json_body = rt.post(AUTH, json={"socket_id": "1.2", "channel_name": channel}, headers=h)
    assert json_body.status_code == 200


def test_linked_and_admin_access(rt):
    sid = make_streamer("authtest-p9-lnk", user_id=make_user("lnkowner" + DOMAIN))
    make_user("linked" + DOMAIN, streamer_id=sid)
    r = login(rt, "linked" + DOMAIN)
    h = {**ORIGIN, "x-csrf-token": r.json()["csrf_token"]}
    assert auth(rt, dash(rt.cookies, "authtest-p9-lnk"), h).status_code == 200
    rt.cookies.clear()
    make_user("admin" + DOMAIN, role="admin")
    r = login(rt, "admin" + DOMAIN)
    h = {**ORIGIN, "x-csrf-token": r.json()["csrf_token"]}
    assert auth(rt, dash(rt.cookies, "authtest-p9-lnk"), h).status_code == 200
    make_streamer("authtest-p9-unclaimed")
    assert auth(rt, dash(rt.cookies, "authtest-p9-unclaimed"), h).status_code == 200  # admin-only, admin ok


def test_cross_creator_and_unclaimed_are_refused(rt):
    make_streamer("authtest-p9-victim", user_id=make_user("victim" + DOMAIN))
    make_streamer("authtest-p9-free")
    _, h = session(rt, "authtest-p9-attacker", "attacker" + DOMAIN)
    for channel in (dash(rt.cookies, "authtest-p9-victim"), dash(rt.cookies, "authtest-p9-free"),  # own session
                    dash(rt.cookies, "authtest-p9-nosuch")):
        r = auth(rt, channel, h)
        assert r.status_code == 403 and "auth" not in r.json()
    assert any("realtime_channel_denied" in rec for rec in rt.capture.records)


def test_session_needs_csrf_and_origin_and_only_gets_the_dashboard(rt):
    _, h = session(rt, "authtest-p9-csrf", "csrf" + DOMAIN)
    channel = dash(rt.cookies, "authtest-p9-csrf")
    assert auth(rt, channel, ORIGIN).status_code == 403  # no CSRF token
    assert auth(rt, channel, {**h, "x-csrf-token": "wrong"}).status_code == 403
    assert auth(rt, channel, {**h, "origin": "https://evil.example"}).status_code == 403
    for suffix in ("alerts", "audio", "settings", "leaderboard"):  # overlay channels need an OBS token
        assert auth(rt, f"private-authtest-p9-csrf-{suffix}", h).status_code == 403
    rt.cookies.clear()
    assert auth(rt, channel, h).status_code == 401  # no credential at all


def test_inactive_user_is_refused(rt):
    _, h = session(rt, "authtest-p9-inact", "inact" + DOMAIN)
    channel = dash(rt.cookies, "authtest-p9-inact")
    with db() as conn:
        conn.execute("UPDATE public.users SET is_active = false WHERE email = %s", ("inact" + DOMAIN,))
    assert auth(rt, channel, h).status_code in (401, 403)


def test_obs_token_overlay_channels(rt):
    sid = make_streamer("authtest-p9-obs")
    token = obs_token(sid)
    hdr = {"x-obs-token": token}
    for suffix in ("alerts", "audio", "settings", "leaderboard"):
        channel = own(f"private-authtest-p9-obs-{suffix}", token)
        r = auth(rt, channel, hdr)
        assert r.status_code == 200 and r.json()["auth"] == expected("1234.5678", channel)
        assert auth(rt, f"private-authtest-p9-obs-{suffix}", hdr).status_code == 403  # the untagged name (NV-4)
    assert auth(rt, own("private-authtest-p9-obs-alerts", token), params={"token": token}).status_code == 200
    second = obs_token(sid)  # the same creator's other token: its channels are not this token's
    assert auth(rt, own("private-authtest-p9-obs-alerts", second), hdr).status_code == 403
    assert auth(rt, "private-authtest-p9-obs-dashboard", hdr).status_code == 403  # never the dashboard
    other = make_streamer("authtest-p9-obs2")
    assert auth(rt, own("private-authtest-p9-obs2-alerts", token), hdr).status_code == 403  # another creator
    assert auth(rt, own("private-authtest-p9-obs-alerts", token),
                {**hdr, "origin": "https://evil.example"}).status_code == 403
    assert other


def test_obs_token_scopes_and_states(rt):
    sid = make_streamer("authtest-p9-scope")
    other = make_streamer("authtest-p9-scope2")
    narrow = obs_token(sid, pusher_channel_scope=["private-authtest-p9-scope-alerts"])
    assert auth(rt, own("private-authtest-p9-scope-alerts", narrow), {"x-obs-token": narrow}).status_code == 200
    assert auth(rt, own("private-authtest-p9-scope-settings", narrow), {"x-obs-token": narrow}).status_code == 403
    wide = obs_token(sid, pusher_channel_scope=["private-authtest-p9-scope-dashboard",
                                                "private-authtest-p9-scope2-alerts"])
    assert auth(rt, "private-authtest-p9-scope-dashboard", {"x-obs-token": wide}).status_code == 403
    assert auth(rt, own("private-authtest-p9-scope2-alerts", wide), {"x-obs-token": wide}).status_code == 403
    for state in ({"revoked_at": datetime.now(UTC)}, {"is_active": False},
                  {"expires_at": datetime.now(UTC) - timedelta(minutes=1)}):
        t = obs_token(sid, **state)
        assert auth(rt, own("private-authtest-p9-scope-alerts", t), {"x-obs-token": t}).status_code == 401
    assert auth(rt, "private-authtest-p9-scope-alerts", {"x-obs-token": "authtest-unknown"}).status_code == 401
    assert other


def test_malformed_requests(rt):
    sid = make_streamer("authtest-p9-bad")
    token = obs_token(sid)
    hdr = {"x-obs-token": token}
    for socket_id in ("1234", "a.b", "1.2.3", ""):
        assert auth(rt, "private-authtest-p9-bad-alerts", hdr, socket_id=socket_id).status_code == 400
    for channel in ("presence-authtest-p9-bad-alerts", "private-encrypted-authtest-p9-bad-alerts",
                    "authtest-p9-bad-goal", "private-authtest-p9-bad-alerts\n", "private-authtest-p9-bad-other"):
        assert auth(rt, channel, hdr).status_code == 403
    assert rt.post(AUTH, content=b"{not json", headers={**hdr, "content-type": "application/json"}).status_code == 400
    assert rt.post(AUTH, data={"socket_id": "1.2"}, headers=hdr).status_code == 400
    assert rt.post(AUTH, content=b"x" * 3000, headers=hdr).status_code == 413
    _, h = session(rt, "authtest-p9-both", "both" + DOMAIN)  # a session cookie AND a token
    assert auth(rt, "private-authtest-p9-bad-alerts", {**h, **hdr}).status_code == 400


def test_rate_limits(rt):
    sid = make_streamer("authtest-p9-rate")
    token = obs_token(sid)
    statuses = [auth(rt, own("private-authtest-p9-rate-alerts", token), {"x-obs-token": token}).status_code
                for _ in range(32)]
    assert statuses[:30] == [200] * 30 and statuses[30:] == [429, 429]  # 30/min per credential
    with db() as conn:
        conn.execute("DELETE FROM public.rate_limits WHERE endpoint = 'realtime:auth:credential'")
    statuses = [auth(rt, own("private-authtest-p9-rate-alerts", t), {"x-obs-token": t}).status_code
                for t in (obs_token(sid) for _ in range(30))]
    assert 429 in statuses  # 60/min per IP across tokens


def test_config_endpoint(rt):
    make_streamer("authtest-p9-cfg")
    r = rt.get("/api/v1/realtime/config", params={"streamer": "authtest-p9-cfg"})
    assert r.status_code == 200 and r.json() == {"key": APP.key, "cluster": "ap2", "auth_endpoint": AUTH}
    assert r.headers["cache-control"] == "public, max-age=300" and SECRET not in r.text
    unknown = rt.get("/api/v1/realtime/config", params={"streamer": "authtest-p9-none"})
    assert unknown.status_code == 404
    # D3: pending, rejected, unknown and malformed creators are indistinguishable (no status oracle).
    update_streamer(make_streamer("authtest-p9-cfg-pending"), status="pending")
    update_streamer(make_streamer("authtest-p9-cfg-rejected"), status="rejected", rejection_reason="nope")
    for ref in ("authtest-p9-cfg-pending", "authtest-p9-cfg-rejected", "../x", "Bad Slug", "", "x" * 70):
        r = rt.get("/api/v1/realtime/config", params={"streamer": ref})
        assert (r.status_code, r.json(), r.headers.get("cache-control")) == (
            404, unknown.json(), unknown.headers.get("cache-control")), ref


def test_no_secrets_or_tokens_in_logs(rt):
    sid, h = session(rt, "authtest-p9-log", "log" + DOMAIN)
    token = obs_token(sid)
    auth(rt, "private-authtest-p9-log-dashboard", h)
    auth(rt, own("private-authtest-p9-log-alerts", token), {"x-obs-token": token})  # 400: the session cookie is also sent
    auth(rt, "private-authtest-p9-log-dashboard", {"x-obs-token": token})
    auth(rt, own("private-authtest-p9-log-alerts", token), params={"token": token})
    text = "\n".join(rt.capture.records)
    for value in (SECRET, token, h["x-csrf-token"], expected("1234.5678", own("private-authtest-p9-log-alerts", token))):
        assert value not in text


def test_session_hands_out_the_tokens_own_channels_and_revoke_ends_them(rt):
    """NV-4: GET /obs/session names this token's channels; another token's or the untagged names are refused, and
    after the revoke the token can neither subscribe again nor read its session."""
    sid = make_streamer("authtest-p9-sess")
    token, other = obs_token(sid), obs_token(sid)
    s = rt.get("/api/v1/obs/session", headers={"x-obs-token": token})
    assert s.status_code == 200, s.text
    channels = s.json()["realtime"]["channels"]
    assert channels == {sfx: own(f"private-authtest-p9-sess-{sfx}", token) for sfx in ("alerts", "audio", "settings",
                                                                                       "leaderboard")}
    assert s.json()["realtime"]["audio_channel"] == channels["audio"]
    for channel in channels.values():
        assert auth(rt, channel, {"x-obs-token": token}).status_code == 200
        assert auth(rt, channel, {"x-obs-token": other}).status_code == 403  # the other token's names differ
    assert run(TokenRepository(factory()).revoke(sid, token_id(token)))["state"] == "revoked"
    assert auth(rt, channels["alerts"], {"x-obs-token": token}).status_code == 401
    assert rt.get("/api/v1/obs/session", headers={"x-obs-token": token}).status_code == 401
    assert auth(rt, own("private-authtest-p9-sess-alerts", other), {"x-obs-token": other}).status_code == 200


def test_disabled_by_default(app):
    sid = make_streamer("authtest-p9-off")
    token = obs_token(sid)
    assert auth(app, "private-authtest-p9-off-alerts", {"x-obs-token": token}).status_code == 503
    assert app.get("/api/v1/realtime/config", params={"streamer": "authtest-p9-off"}).status_code == 503
