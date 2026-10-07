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
from app.realtime import outbox
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
    login,
    make_donation,
    make_streamer,
    make_user,
    requires_db,
    update_streamer,
)
from tests.jobs.test_jobs_db import audio_handler, factory, run, work

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
    update_streamer(sid, moderation_mode="manual")
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
    run(repo.update_settings(sid, {"tts_enabled": False, "moderation_mode": "manual"}))
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
    sid = make_streamer("authtest-p9-e2e")
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
    assert [(c, n) for c, n, _ in sent] == [("private-authtest-p9-e2e-dashboard", "donation-updated"),
                                            ("authtest-p9-e2e-goal", "goal-progress"),
                                            ("private-authtest-p9-e2e-leaderboard", "leaderboard-updated")]
    dashboard, goal, board = (d for _, _, d in sent)
    assert dashboard["donation"]["id"] == did and dashboard["donation"]["message"] == MESSAGE
    assert dashboard["action"] == "approve" and goal["progress_inr"] == 100.0 and goal["target_amount_inr"] == 1000.0
    assert board["top_donor"] == {"name": "Ravi", "total_amount": 100.0}
    assert MESSAGE not in json.dumps(goal) and MESSAGE not in json.dumps(board) and sid not in json.dumps(sent)
    assert [r[4] for r in outbox_rows(sid)] == ["published"] * 3


def test_publisher_transient_failure_then_success(only):
    sid = make_streamer("authtest-p9-retry")
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


def auth(client, channel, headers=None, socket_id="1234.5678", **kw):
    return client.post(AUTH, data={"socket_id": socket_id, "channel_name": channel}, headers=headers or {}, **kw)


def test_owner_gets_a_valid_dashboard_signature(rt):
    sid, h = session(rt, "authtest-p9-own", "own" + DOMAIN)
    r = auth(rt, "private-authtest-p9-own-dashboard", h)
    assert r.status_code == 200 and r.json() == {"auth": expected("1234.5678", "private-authtest-p9-own-dashboard")}
    assert r.headers["cache-control"] == "no-store"
    json_body = rt.post(AUTH, json={"socket_id": "1.2", "channel_name": "private-authtest-p9-own-dashboard"}, headers=h)
    assert json_body.status_code == 200


def test_linked_and_admin_access(rt):
    sid = make_streamer("authtest-p9-lnk", user_id=make_user("lnkowner" + DOMAIN))
    make_user("linked" + DOMAIN, streamer_id=sid)
    r = login(rt, "linked" + DOMAIN)
    h = {**ORIGIN, "x-csrf-token": r.json()["csrf_token"]}
    assert auth(rt, "private-authtest-p9-lnk-dashboard", h).status_code == 200
    rt.cookies.clear()
    make_user("admin" + DOMAIN, role="admin")
    r = login(rt, "admin" + DOMAIN)
    h = {**ORIGIN, "x-csrf-token": r.json()["csrf_token"]}
    assert auth(rt, "private-authtest-p9-lnk-dashboard", h).status_code == 200
    make_streamer("authtest-p9-unclaimed")
    assert auth(rt, "private-authtest-p9-unclaimed-dashboard", h).status_code == 200  # admin-only, admin ok


def test_cross_creator_and_unclaimed_are_refused(rt):
    make_streamer("authtest-p9-victim", user_id=make_user("victim" + DOMAIN))
    make_streamer("authtest-p9-free")
    _, h = session(rt, "authtest-p9-attacker", "attacker" + DOMAIN)
    for channel in ("private-authtest-p9-victim-dashboard", "private-authtest-p9-free-dashboard",
                    "private-authtest-p9-nosuch-dashboard"):
        r = auth(rt, channel, h)
        assert r.status_code == 403 and "auth" not in r.json()
    assert any("realtime_channel_denied" in rec for rec in rt.capture.records)


def test_session_needs_csrf_and_origin_and_only_gets_the_dashboard(rt):
    _, h = session(rt, "authtest-p9-csrf", "csrf" + DOMAIN)
    channel = "private-authtest-p9-csrf-dashboard"
    assert auth(rt, channel, ORIGIN).status_code == 403  # no CSRF token
    assert auth(rt, channel, {**h, "x-csrf-token": "wrong"}).status_code == 403
    assert auth(rt, channel, {**h, "origin": "https://evil.example"}).status_code == 403
    for suffix in ("alerts", "audio", "settings", "leaderboard"):  # overlay channels need an OBS token
        assert auth(rt, f"private-authtest-p9-csrf-{suffix}", h).status_code == 403
    rt.cookies.clear()
    assert auth(rt, channel, h).status_code == 401  # no credential at all


def test_inactive_user_is_refused(rt):
    _, h = session(rt, "authtest-p9-inact", "inact" + DOMAIN)
    with db() as conn:
        conn.execute("UPDATE public.users SET is_active = false WHERE email = %s", ("inact" + DOMAIN,))
    assert auth(rt, "private-authtest-p9-inact-dashboard", h).status_code in (401, 403)


def test_obs_token_overlay_channels(rt):
    sid = make_streamer("authtest-p9-obs")
    token = obs_token(sid)
    hdr = {"x-obs-token": token}
    for suffix in ("alerts", "audio", "settings", "leaderboard"):
        channel = f"private-authtest-p9-obs-{suffix}"
        r = auth(rt, channel, hdr)
        assert r.status_code == 200 and r.json()["auth"] == expected("1234.5678", channel)
    assert auth(rt, "private-authtest-p9-obs-alerts", params={"token": token}).status_code == 200  # OBS query form
    assert auth(rt, "private-authtest-p9-obs-dashboard", hdr).status_code == 403  # never the dashboard
    other = make_streamer("authtest-p9-obs2")
    assert auth(rt, "private-authtest-p9-obs2-alerts", hdr).status_code == 403  # another creator
    assert auth(rt, "private-authtest-p9-obs-alerts", {**hdr, "origin": "https://evil.example"}).status_code == 403
    assert other


def test_obs_token_scopes_and_states(rt):
    sid = make_streamer("authtest-p9-scope")
    other = make_streamer("authtest-p9-scope2")
    narrow = obs_token(sid, pusher_channel_scope=["private-authtest-p9-scope-alerts"])
    assert auth(rt, "private-authtest-p9-scope-alerts", {"x-obs-token": narrow}).status_code == 200
    assert auth(rt, "private-authtest-p9-scope-settings", {"x-obs-token": narrow}).status_code == 403
    wide = obs_token(sid, pusher_channel_scope=["private-authtest-p9-scope-dashboard",
                                                "private-authtest-p9-scope2-alerts"])
    assert auth(rt, "private-authtest-p9-scope-dashboard", {"x-obs-token": wide}).status_code == 403
    assert auth(rt, "private-authtest-p9-scope2-alerts", {"x-obs-token": wide}).status_code == 403
    for state in ({"revoked_at": datetime.now(UTC)}, {"is_active": False},
                  {"expires_at": datetime.now(UTC) - timedelta(minutes=1)}):
        assert auth(rt, "private-authtest-p9-scope-alerts", {"x-obs-token": obs_token(sid, **state)}).status_code == 401
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
    statuses = [auth(rt, "private-authtest-p9-rate-alerts", {"x-obs-token": token}).status_code for _ in range(32)]
    assert statuses[:30] == [200] * 30 and statuses[30:] == [429, 429]  # 30/min per credential
    with db() as conn:
        conn.execute("DELETE FROM public.rate_limits WHERE endpoint = 'realtime:auth:credential'")
    statuses = [auth(rt, "private-authtest-p9-rate-alerts", {"x-obs-token": obs_token(sid)}).status_code
                for _ in range(30)]
    assert 429 in statuses  # 60/min per IP across tokens


def test_config_endpoint(rt):
    make_streamer("authtest-p9-cfg")
    r = rt.get("/api/v1/realtime/config", params={"streamer": "authtest-p9-cfg"})
    assert r.status_code == 200 and r.json() == {"key": APP.key, "cluster": "ap2", "auth_endpoint": AUTH}
    assert r.headers["cache-control"] == "public, max-age=300" and SECRET not in r.text
    assert rt.get("/api/v1/realtime/config", params={"streamer": "authtest-p9-none"}).status_code == 404
    assert rt.get("/api/v1/realtime/config", params={"streamer": "../x"}).status_code == 404


def test_no_secrets_or_tokens_in_logs(rt):
    sid, h = session(rt, "authtest-p9-log", "log" + DOMAIN)
    token = obs_token(sid)
    auth(rt, "private-authtest-p9-log-dashboard", h)
    auth(rt, "private-authtest-p9-log-alerts", {"x-obs-token": token})
    auth(rt, "private-authtest-p9-log-dashboard", {"x-obs-token": token})
    auth(rt, "private-authtest-p9-log-alerts", params={"token": token})
    text = "\n".join(rt.capture.records)
    for value in (SECRET, token, h["x-csrf-token"], expected("1234.5678", "private-authtest-p9-log-alerts")):
        assert value not in text


def test_disabled_by_default(app):
    sid = make_streamer("authtest-p9-off")
    token = obs_token(sid)
    assert auth(app, "private-authtest-p9-off-alerts", {"x-obs-token": token}).status_code == 503
    assert app.get("/api/v1/realtime/config", params={"streamer": "authtest-p9-off"}).status_code == 503
