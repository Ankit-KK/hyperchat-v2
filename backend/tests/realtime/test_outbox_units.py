"""Phase 9 units: what emit() writes (ids and codes only, coalescing keys), which settings produce which event,
the payload contracts (allow-listed keys, hidden messages, size guard, public goal) and the publisher's
decisions (mocked queue and Pusher transport)."""

import asyncio
import json
import logging
import random
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import psycopg
import pytest

from app.realtime import outbox
from app.realtime.outbox import Event
from app.realtime.payloads import SIZE_LIMIT, Renderer, Skip
from app.realtime.publisher import OutboxPublisher, RateLimiter
from app.realtime.pusher import PusherApp, PusherApps, PusherClient

SID, DID = str(uuid.uuid4()), str(uuid.uuid4())
SECRET = "test-only-pusher-secret-42"
APP = PusherApp(1, "123", "key123", SECRET, "ap2")
DONOR, MESSAGE = "Ravi Donor", "a very private donor message 9q"


class Cursor:
    def __init__(self) -> None:
        self.calls, self.rowcount = [], 1

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))


def emitted(coro_factory):
    cur = Cursor()
    asyncio.run(coro_factory(cur))
    return [(p[2], p[5]) for _, p in cur.calls], cur  # (event, coalesce_key)


# ── emit ────────────────────────────────────────────────────────────────────

def test_emit_writes_ids_and_codes_with_the_callers_cursor():
    cur = Cursor()
    asyncio.run(outbox.emit(cur, SID, "donation-updated", subject_id=DID,
                            facts={"action": "approve", "previous_status": "pending"}))
    ((sql, params),) = cur.calls
    assert "INSERT INTO public.realtime_outbox" in sql and "ON CONFLICT (coalesce_key) WHERE status = 'pending'" in sql
    assert params[:4] == (SID, "dashboard", "donation-updated", DID) and params[5] is None
    assert params[4].obj == {"action": "approve", "previous_status": "pending"}


@pytest.mark.parametrize("event, key", [("goal-progress", f"goal-progress:{SID}"),
                                        ("leaderboard-updated", f"leaderboard-updated:{SID}"),
                                        ("settings-updated", f"settings-updated:{SID}"),
                                        ("new-donation", None), ("audio-ready", None)])
def test_only_state_events_coalesce(event, key):
    (written,), _ = emitted(lambda cur: outbox.emit(cur, SID, event, subject_id=DID))
    assert written == (event, key)


@pytest.mark.parametrize("kwargs", [
    {"event": "stats-update"}, {"event": "donation-approved"}, {"event": "now-playing"},
    {"streamer_id": "ankit"}, {"subject_id": "not-a-uuid"},
    {"facts": {"message": "hello"}}, {"facts": {"action": "Approve!"}}, {"facts": {"action": DONOR}},
    {"facts": {"action": "x" * 33}}, {"facts": {"token": "abc"}}, {"facts": {"action": 1}},
    {"facts": {"action": "approve\n"}}, {"subject_id": DID + "\n"}, {"streamer_id": SID + "\n"},
])
def test_emit_refuses_anything_but_known_events_ids_and_codes(kwargs):
    args = {"streamer_id": SID, "event": "donation-updated", "subject_id": DID, **kwargs}
    with pytest.raises(ValueError):
        asyncio.run(outbox.emit(Cursor(), args.pop("streamer_id"), args.pop("event"), **args))


@pytest.mark.parametrize("changed, events", [
    (["brand_color"], ["settings-updated"]),
    (["alert_overlay_id", "overlay_adjust_mode"], ["settings-updated"]),
    (["goal_name"], ["goal-progress"]),
    (["goal_is_active", "goal_activated_at", "goal_target_amount"], ["goal-progress"]),
    (["leaderboard_widget_enabled"], ["settings-updated", "leaderboard-updated"]),
    (["brand_color", "goal_name"], ["settings-updated", "goal-progress"]),
    (["tts_enabled", "moderation_mode", "el_voice_id", "media_moderation_enabled"], []),  # not pushed live
])
def test_settings_changes_map_to_events(changed, events):
    written, _ = emitted(lambda cur: outbox.emit_settings_changed(cur, SID, changed))
    assert [e for e, _ in written] == events


def test_totals_change_emits_goal_and_leaderboard():
    written, _ = emitted(lambda cur: outbox.emit_totals_changed(cur, SID))
    assert [e for e, _ in written] == ["goal-progress", "leaderboard-updated"]


def test_audio_ready_is_conditional_on_the_donation_state():
    cur = Cursor()
    asyncio.run(outbox.emit_audio_ready(cur, DID))
    ((sql, params),) = cur.calls
    assert "payment_status = 'success'" in sql and "moderation_status IN ('approved', 'auto_approved')" in sql
    assert "tts_audio_url IS NOT NULL OR d.voice_message_url IS NOT NULL OR d.hypersound_url IS NOT NULL" in sql
    assert params[1] == DID
    with pytest.raises(ValueError):
        asyncio.run(outbox.emit_audio_ready(Cursor(), "x"))


# ── payloads ────────────────────────────────────────────────────────────────

def make_event(event="new-donation", subject_id=DID, facts=None, attempts=1, max_attempts=6, reclaimed=False):
    return Event(id=1, event_id=str(uuid.uuid4()), streamer_id=SID, channel=outbox.EVENT_CHANNELS[event], event=event,
                 subject_id=subject_id, facts=facts or {}, attempts=attempts, max_attempts=max_attempts,
                 created_at=datetime.now(UTC), locked_by="w1", reclaimed=reclaimed)


def streamer_row(**kw):
    row = dict(slug="ankit", status="active", pusher_group=2, brand_color="#ff0000", alert_box_scale=Decimal("1.25"),
               alert_overlay_id=3, leaderboard_overlay_id=1, goal_overlay_id=2, overlay_adjust_mode=False,
               leaderboard_widget_enabled=True, leaderboard_bg_enabled=True, goal_name="New mic",
               goal_target_amount=Decimal("5000"), goal_is_active=True,
               goal_activated_at=datetime(2026, 1, 1, tzinfo=UTC))
    row.update(kw)
    return row


def donation_row(**kw):
    row = dict(id=DID, name=DONOR, amount=Decimal("100.00"), currency="INR", message=MESSAGE, message_visible=True,
               moderation_status="approved", payment_status="success", is_hyperemote=False,
               voice_message_url=None, tts_audio_url="https://cdn.example/tts.mp3", hypersound_url=None,
               media_url=None, media_type=None, created_at=datetime(2026, 1, 2, tzinfo=UTC))
    row.update(kw)
    return row


class FakeRepo:
    def __init__(self, streamer=None, donation=None, top=None, latest=None, progress=1234.5):
        self.s, self.d, self.top, self.latest, self.progress = streamer, donation, top, latest, progress

    async def streamer(self, sid):
        return self.s

    async def donation(self, did, sid):
        return self.d

    async def goal_progress_inr(self, sid, since):
        return self.progress

    async def top_donor(self, sid):
        return self.top

    async def latest_approved(self, sid):
        return self.latest


def render(event, **repo):
    return asyncio.run(Renderer(FakeRepo(**{"streamer": streamer_row(), "donation": donation_row(), **repo}))
                       .render(event))


FORBIDDEN = ("token", "order", "payment", "provider", "streamer_id", "user_id", "email", "phone", "secret", "csrf",
             "session", "moderator", "approved_by", "status_token", "upload", "ip", "hash")


def keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys(v)}
    return set()


def test_dashboard_donation_payload_contract():
    r = render(make_event("donation-updated", facts={"action": "approve", "previous_status": "pending"}))
    assert r.channel == "private-ankit-dashboard" and r.event == "donation-updated" and r.pusher_group == 2
    assert set(r.data) == {"event_id", "v", "action", "previous_status", "donation"}
    assert set(r.data["donation"]) == {"id", "name", "amount", "currency", "message", "message_visible",
                                       "moderation_status", "is_hyperemote", "voice_message_url", "tts_audio_url",
                                       "hypersound_url", "media_url", "media_type", "created_at"}
    assert r.data["donation"]["amount"] == 100.0 and r.data["donation"]["message"] == MESSAGE and r.data["v"] == 1
    json.dumps(r.data)  # serialisable as is


def test_hidden_message_is_never_broadcast():
    r = render(make_event("new-donation"), donation=donation_row(message_visible=False))
    assert r.data["donation"]["message"] is None and MESSAGE not in json.dumps(r.data)


@pytest.mark.parametrize("event", list(outbox.EVENT_CHANNELS))
def test_no_payload_carries_forbidden_keys(event):
    facts = {"donation-updated": {"action": "approve"}, "audio-now-playing": {"kind": "tts"}}.get(event)
    r = render(make_event(event, facts=facts),
               top={"name": DONOR, "total_amount": Decimal("10")},
               latest={"name": DONOR, "amount": Decimal("5"), "currency": "INR", "created_at": datetime.now(UTC)})
    found = {k for k in keys(r.data) for bad in FORBIDDEN if bad in k.lower().split("_")}
    found |= {k for k in keys(r.data) if k == "amount_inr"}  # donation internals (the goal's totals are public)
    assert not found, found
    assert SID not in json.dumps(r.data)  # never the creator's id


def test_unpaid_or_missing_donations_are_skipped():
    assert render(make_event("new-donation"), donation=donation_row(payment_status="pending")) == Skip("not_paid")
    assert render(make_event("new-donation"), donation=None) == Skip("subject_missing")
    assert render(make_event("new-donation", subject_id=None)) == Skip("subject_missing")
    assert render(make_event("new-donation"), streamer=None) == Skip("streamer_missing")


def test_audio_ready_carries_the_donation_id_only():
    r = render(make_event("audio-ready"))
    assert r.channel == "private-ankit-audio" and set(r.data) == {"event_id", "v", "donation_id"}
    assert r.data["donation_id"] == DID
    assert render(make_event("audio-ready"), donation=donation_row(moderation_status="rejected")) == Skip("not_approved")


def test_goal_is_public_and_aggregate_only():
    r = render(make_event("goal-progress", subject_id=None))
    assert r.channel == "ankit-goal"
    assert r.data == {"event_id": r.data["event_id"], "v": 1, "active": True, "name": "New mic",
                      "target_amount_inr": 5000.0, "progress_inr": 1234.5, "brand_color": "#ff0000",
                      "goal_overlay_id": 2}  # exactly GET /public/streamers/{slug}/goal
    inactive = render(make_event("goal-progress", subject_id=None), streamer=streamer_row(goal_is_active=False))
    assert inactive.data == {"event_id": inactive.data["event_id"], "v": 1, "active": False, "name": None,
                             "target_amount_inr": None, "progress_inr": None, "brand_color": None,
                             "goal_overlay_id": None}  # no stale goal on the public channel


@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_no_public_goal_event_for_a_creator_that_is_not_active(status):
    hidden = render(make_event("goal-progress", subject_id=None), streamer=streamer_row(status=status))
    assert hidden == Skip("streamer_not_public")


def test_leaderboard_payload():
    top = {"name": DONOR, "total_amount": Decimal("900.50")}
    latest = {"name": "Asha", "amount": Decimal("50"), "currency": "USD", "created_at": datetime(2026, 1, 3, tzinfo=UTC)}
    r = render(make_event("leaderboard-updated", subject_id=None), top=top, latest=latest)
    assert r.channel == "private-ankit-leaderboard"
    assert r.data["enabled"] is True and r.data["top_donor"] == {"name": DONOR, "total_amount": 900.5}
    assert r.data["latest"] == {"name": "Asha", "amount": 50.0, "currency": "USD",
                                "created_at": "2026-01-03T00:00:00+00:00"}
    off = render(make_event("leaderboard-updated", subject_id=None), top=top, latest=latest,
                 streamer=streamer_row(leaderboard_widget_enabled=False))
    assert off.data["top_donor"] is None and off.data["latest"] is None and DONOR not in json.dumps(off.data)


def test_settings_payload_is_the_overlay_allowlist():
    r = render(make_event("settings-updated", subject_id=None))
    assert r.channel == "private-ankit-settings"
    assert set(r.data) == {"event_id", "v", *outbox.OVERLAY_SETTINGS} and r.data["alert_box_scale"] == 1.25


def test_size_guard():
    big = donation_row(media_url="https://cdn.example/" + "x" * (SIZE_LIMIT + 10))
    r = render(make_event("new-donation"), donation=big)
    assert r.data == {"event_id": r.data["event_id"], "v": 1, "donation_id": DID, "truncated": True}


# ── publisher (fake queue, fake renderer, mocked Pusher transport) ──────────

class FakeQueue:
    def __init__(self, events=()):
        self.events, self.finished, self.retried, self.released = list(events), [], [], []
        self.skipped_reason, self.expired, self.fence = None, 0, True

    async def expire(self):
        return self.expired

    async def skip_pending(self, reason):
        self.skipped_reason = reason
        return 3

    async def claim(self, worker, limit):
        out, self.events = self.events[:limit], self.events[limit:]
        return out

    async def finish(self, event, status, reason=None):
        self.finished.append((event.event, status, reason))
        return self.fence

    async def retry(self, event, delay, reason):
        self.retried.append((event.event, delay, reason))
        return self.fence

    async def release(self, event):
        self.released.append(event.event)
        return True


class FakeRenderer:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    async def render(self, event):
        if self.error:
            raise self.error
        if self.result is not None:
            return self.result
        return await Renderer(FakeRepo(streamer=streamer_row(), donation=donation_row())).render(event)


def publisher(queue, transport=None, *, renderer=None, enabled=True, apps=None):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})
    client = PusherClient(httpx.MockTransport(transport or handler))
    p = OutboxPublisher(queue, renderer or FakeRenderer(), client, apps or PusherApps({1: APP}), enabled=enabled,
                        rate=1000, rng=random.Random(1), ident="w1")
    return p, calls


def test_published():
    q = FakeQueue([make_event()])
    p, calls = publisher(q)
    assert asyncio.run(p.run_once()) == 1
    assert q.finished == [("new-donation", "published", None)] and len(calls) == 1
    assert calls[0].url.host == "api-ap2.pusher.com"  # group 2 not configured -> group 1


def test_disabled_only_marks_pending_rows_skipped():
    q = FakeQueue([make_event()])
    p, calls = publisher(q, enabled=False)
    assert asyncio.run(p.run_once()) == 3 and q.skipped_reason == "realtime_disabled" and calls == [] and q.events


def test_unconfigured_pusher_skips():
    q = FakeQueue([make_event()])
    p, calls = publisher(q, apps=PusherApps({}))
    asyncio.run(p.run_once())
    assert q.finished == [("new-donation", "skipped", "pusher_not_configured")] and calls == []


def test_render_skip_is_recorded():
    q = FakeQueue([make_event()])
    p, calls = publisher(q, renderer=FakeRenderer(Skip("not_paid")))
    asyncio.run(p.run_once())
    assert q.finished == [("new-donation", "skipped", "not_paid")] and calls == []


@pytest.mark.parametrize("response, reason, delay", [
    (httpx.Response(500), "pusher_server_error", None), (httpx.Response(429, headers={"retry-after": "9"}),
                                                         "pusher_rate_limited", 9.0),
])
def test_transient_failures_retry_with_backoff(response, reason, delay):
    q = FakeQueue([make_event(attempts=2)])
    p, _ = publisher(q, lambda request: response)
    asyncio.run(p.run_once())
    ((event, d, r),) = q.retried
    assert r == reason and q.finished == [] and d <= 30
    if delay:
        assert d >= delay
    else:
        assert 2 * 2 * 0.8 <= d <= 2 * 2 * 1.2  # base 2 s, attempt 2


def test_timeouts_retry():
    def handler(request):
        raise httpx.ReadTimeout("t")
    q = FakeQueue([make_event()])
    p, _ = publisher(q, handler)
    asyncio.run(p.run_once())
    assert q.retried[0][2] == "pusher_timeout"


def test_last_attempt_fails():
    q = FakeQueue([make_event(attempts=6)])
    p, _ = publisher(q, lambda request: httpx.Response(503))
    asyncio.run(p.run_once())
    assert q.retried == [] and q.finished == [("new-donation", "failed", "pusher_server_error")]


@pytest.mark.parametrize("status, reason", [(400, "pusher_rejected"), (401, "pusher_auth_failed"),
                                            (403, "pusher_forbidden"), (413, "payload_too_large")])
def test_permanent_failures_fail_at_once(status, reason):
    q = FakeQueue([make_event(attempts=1)])
    p, _ = publisher(q, lambda request: httpx.Response(status, text="body"))
    asyncio.run(p.run_once())
    assert q.finished == [("new-donation", "failed", reason)] and q.retried == []


def test_reclaimed_after_the_last_attempt_fails_without_sending():
    q = FakeQueue([make_event(attempts=7, reclaimed=True)])
    p, calls = publisher(q)
    asyncio.run(p.run_once())
    assert q.finished == [("new-donation", "failed", "lease_expired")] and calls == []


def test_database_errors_leave_the_row_to_its_lease():
    q = FakeQueue([make_event()])
    p, calls = publisher(q, renderer=FakeRenderer(error=psycopg.OperationalError("down")))
    asyncio.run(p.run_once())
    assert q.finished == [] and q.retried == [] and calls == []


def test_lost_lease_is_reported_not_counted():
    q = FakeQueue([make_event()])
    q.fence = False
    p, _ = publisher(q)
    asyncio.run(p.run_once())
    assert p.counts["published"] == 0


def test_duplicate_delivery_keeps_the_event_id():
    sent = []

    def handler(request):
        sent.append(json.loads(json.loads(request.content)["data"])["event_id"])
        return httpx.Response(200)
    event = make_event()
    q = FakeQueue([event, event])  # e.g. published, then the 'published' write was lost and the row reclaimed
    p, _ = publisher(q, handler)
    asyncio.run(p.run_once())
    assert sent == [event.event_id, event.event_id]


def test_production_logging_keeps_httpx_request_urls_out():
    from app.core.logging import configure_logging
    configure_logging("DEBUG")
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING


def test_nothing_sensitive_is_logged(caplog):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.WARNING, logger="httpx")  # as configure_logging does in the worker
    q = FakeQueue([make_event("new-donation"), make_event("donation-updated", facts={"action": "approve"})])
    p, _ = publisher(q, lambda request: httpx.Response(500, text="pusher body"))
    asyncio.run(p.run_once())
    text = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    for value in (DONOR, MESSAGE, SECRET, APP.key, "private-ankit", "pusher body", "auth_signature"):
        assert value not in text


def test_shutdown_releases_the_rest_of_the_batch():
    q = FakeQueue([make_event("new-donation"), make_event("donation-updated", facts={"action": "approve"})])
    p, calls = publisher(q)
    p.stopping = asyncio.Event()
    p.stopping.set()
    asyncio.run(p.run_once())
    assert q.released == ["new-donation", "donation-updated"] and q.finished == [] and calls == []


def test_run_stops_when_asked():
    q = FakeQueue([])
    p, _ = publisher(q)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(p.run(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 2)
    asyncio.run(go())


def test_rate_limiter_spaces_sends():
    now = [0.0]
    slept = []

    async def sleep(seconds):
        slept.append(round(seconds, 3))
        now[0] += seconds
    limiter = RateLimiter(10, clock=lambda: now[0], sleep=sleep)

    async def go():
        for _ in range(3):
            await limiter.wait()
    asyncio.run(go())
    assert slept == [0.1, 0.1]
