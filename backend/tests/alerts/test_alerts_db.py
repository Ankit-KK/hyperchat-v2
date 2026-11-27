"""Operator failure alerts on PostgreSQL (scratch database): the failed-job hook, the payment-hold scan, Discord
delivery with a fake client (once, retried, given up after 5), the admin read endpoint, and no donor data anywhere."""

import asyncio
import json
import secrets
import time
from decimal import Decimal

from app.alerts import MAX_ATTEMPTS, AlertNotifier, Alerts
from app.jobs.queue import JobQueue
from app.jobs.retry import Backoff, Permanent, Retryable
from app.jobs.runner import Worker
from tests.integration import DOMAIN, db, make_donation, make_streamer, new_browser, requires_db, signed_in
from tests.jobs.test_jobs_db import factory

pytestmark = requires_db
A = "/api/v1/admin/alerts"
CHANNEL = "123456789012345678"
DONOR, MESSAGE = "Hidden Donor " + secrets.token_hex(4), "private donor words " + secrets.token_hex(4)


def run(coro):
    return asyncio.run(coro)


def alert(alert_id: str) -> tuple:
    with db() as conn:
        return conn.execute("SELECT source, severity, message, context, notified_at FROM public.system_alerts "
                            "WHERE id = %s", (alert_id,)).fetchone()


def alert_ids(**context) -> list[str]:
    ((key, value),) = context.items()
    with db() as conn:
        return [r[0] for r in conn.execute("SELECT id::text FROM public.system_alerts WHERE context->>%s = %s "
                                           "ORDER BY created_at", (key, value))]


class Failing:
    kind = "donation_audio"
    backoff = Backoff(0.01, 0.02)

    async def run(self, job):
        raise Permanent("provider_auth")

    async def fallback(self, job, reason):
        return None


def failed_job(alerts) -> tuple[str, str]:
    """Run one donation_audio job of a synthetic (donor-text-bearing) donation to failure: (job id, donation id)."""
    sid = make_streamer("authtest-al-" + secrets.token_hex(3))
    did = make_donation(sid, Decimal("100"), payment="success", moderation="auto_approved", name=DONOR, message=MESSAGE)
    q = JobQueue(factory())
    jid = run(q.enqueue("donation_audio", did, {"donation_id": did}))
    assert run(Worker(q, {"donation_audio": Failing()}, on_failure=alerts.job_failed).run_once()).id == jid
    return jid, did


class FakeDiscord:
    def __init__(self, error: Exception | None = None):
        self.error, self.sent = error, []

    async def create_message(self, channel_id, payload):
        self.sent.append((channel_id, payload))
        if self.error is not None:
            raise self.error
        return "1"


def notifier(discord=None, channel=CHANNEL, batch=20) -> AlertNotifier:
    return AlertNotifier(Alerts(factory()), discord=discord, channel_id=channel, app_env="development", interval=0.01,
                         batch=batch)


def cycles(n: AlertNotifier, times: int) -> list[dict]:
    out = []
    for _ in range(times):
        out.append(run(n.run_once()))
        time.sleep(0.05)  # past the (tiny) retry backoff
    return out


def hold(sid: str, status: str, on_hold: bool = True) -> None:
    with db() as conn:
        conn.execute("INSERT INTO public.streamer_payment_health (streamer_id, status, on_hold) VALUES (%s, %s, %s) "
                     "ON CONFLICT (streamer_id) DO UPDATE SET status = EXCLUDED.status, on_hold = EXCLUDED.on_hold",
                     (sid, status, on_hold))


# ── sources ─────────────────────────────────────────────────────────────────

def test_exhausted_job_writes_an_alert_of_ids_and_codes_only(alerts_clean):
    jid, did = failed_job(Alerts(factory()))
    with db() as conn:
        assert conn.execute("SELECT status, last_error FROM public.jobs WHERE id = %s", (jid,)).fetchone() == (
            "failed", "provider_auth")
    (aid,) = alert_ids(job_id=jid)
    assert alert(aid) == ("worker", "error", "provider_auth",
                          {"kind": "job_failed", "job_id": jid, "job_kind": "donation_audio", "dedupe_key": did}, None)


def test_an_alert_that_cannot_be_written_never_fails_the_job(alerts_clean, caplog):
    class Broken(Alerts):
        async def record(self, **fields):
            raise RuntimeError("alerts table gone")
    jid, _ = failed_job(Broken(factory()))
    with db() as conn:
        assert conn.execute("SELECT status FROM public.jobs WHERE id = %s", (jid,)).fetchone() == ("failed",)
    assert "alert not recorded" in caplog.text


def test_payment_hold_is_alerted_once_per_transition(alerts_clean):
    held, flaky = make_streamer("authtest-al-hold"), make_streamer("authtest-al-net")
    hold(held, "invalid_key")
    hold(flaky, "network_error")  # transient: checkout answers 502, never a hold alert
    n = notifier()
    cycles(n, 3)
    (aid,) = alert_ids(streamer_id=held)
    assert alert(aid)[:4] == ("payments", "error", "invalid_key", {"kind": "payment_on_hold", "streamer_id": held})
    assert alert_ids(streamer_id=flaky) == []
    cycles(notifier(), 1)  # a restarted worker: the same hold already has its alert
    assert len(alert_ids(streamer_id=held)) == 1
    hold(held, "missing_key")  # a changed reason
    cycles(n, 1)
    hold(held, "ok", on_hold=False)
    cycles(n, 1)
    hold(held, "invalid_key")  # cleared, then on hold again
    cycles(n, 2)
    assert [alert(a)[2] for a in alert_ids(streamer_id=held)] == ["invalid_key", "missing_key", "invalid_key"]


# ── delivery ────────────────────────────────────────────────────────────────

def test_delivery_is_off_without_a_channel(alerts_clean):
    aid = run(Alerts(factory()).record(kind="job_failed", source="worker", code="provider_auth", job_id="x"))
    discord = FakeDiscord()
    cycles(notifier(discord, channel=None), 2)
    cycles(notifier(None), 1)  # no bot token: no client
    assert discord.sent == [] and alert(aid)[4] is None


def test_each_alert_is_sent_once_and_marked_delivered(alerts_clean):
    jid, _ = failed_job(Alerts(factory()))
    (aid,) = alert_ids(job_id=jid)
    discord = FakeDiscord()
    counts = cycles(notifier(discord), 3)
    assert [c["sent"] for c in counts] == [1, 0, 0] and len(discord.sent) == 1
    channel, payload = discord.sent[0]
    assert channel == CHANNEL and payload["allowed_mentions"] == {"parse": []}
    assert f"id={aid}" in payload["content"] and "job_failed" in payload["content"]
    assert DONOR not in payload["content"] and MESSAGE not in payload["content"]
    *_, context, notified_at = alert(aid)
    assert notified_at is not None and context["delivery_attempts"] == 1 and "delivery_error" not in context


def test_a_failing_delivery_is_retried_recorded_and_given_up_after_five(alerts_clean):
    aid = run(Alerts(factory()).record(kind="job_failed", source="worker", code="provider_auth", job_id="x"))
    discord = FakeDiscord(Retryable("discord_server_error"))
    # batch=1: with the 10 ms test backoff, a slow runner could otherwise retry the same alert inside one cycle.
    n = notifier(discord, batch=1)
    run(n.run_once())
    *_, context, notified_at = alert(aid)
    assert notified_at is None and context["delivery_attempts"] == 1 and context["delivery_error"] == "discord_server_error"
    for _ in range(40):
        time.sleep(0.05)
        if run(n.run_once())["given_up"]:
            break
    cycles(n, 3)  # never again
    assert len(discord.sent) == MAX_ATTEMPTS == 5
    *_, context, notified_at = alert(aid)
    assert notified_at is None and context["delivery_attempts"] == 5 and context["delivery_error"] == "discord_server_error"


def test_a_permanent_delivery_error_gives_up_at_once(alerts_clean):
    aid = run(Alerts(factory()).record(kind="job_failed", source="worker", code="provider_auth", job_id="x"))
    discord = FakeDiscord(Permanent("discord_forbidden"))
    assert [c["given_up"] for c in cycles(notifier(discord), 3)] == [1, 0, 0] and len(discord.sent) == 1
    assert alert(aid)[3]["delivery_error"] == "discord_forbidden"


def test_a_retry_waits_for_its_backoff(alerts_clean):
    run(Alerts(factory()).record(kind="job_failed", source="worker", code="provider_auth", job_id="x"))
    discord = FakeDiscord(Retryable("discord_timeout"))
    slow = AlertNotifier(Alerts(factory()), discord=discord, channel_id=CHANNEL, app_env="development", interval=60)
    run(slow.run_once())
    run(slow.run_once())  # 60 s backoff: not due yet
    assert len(discord.sent) == 1


# ── admin read ──────────────────────────────────────────────────────────────

def test_admin_alerts_endpoint_filters_pages_and_holds_no_donor_data(app, alerts_clean):
    assert new_browser(app).get(A).status_code == 401
    plain = new_browser(app)
    signed_in(plain, "plain" + DOMAIN)
    assert plain.get(A).status_code == 403
    signed_in(app, "boss" + DOMAIN, role="admin")
    alerts = Alerts(factory())
    jid, did = failed_job(alerts)
    sid = make_streamer("authtest-al-page")
    run(alerts.record(kind="payment_on_hold", source="payments", code="missing_key", streamer_id=sid))  # newest
    with db() as conn:
        delivered = conn.execute("UPDATE public.system_alerts SET notified_at = now() WHERE context->>'job_id' = %s "
                                 "RETURNING id::text", (jid,)).fetchone()[0]

    def ok(**params) -> dict:
        r = app.get(A, params=params)
        assert r.status_code == 200, r.text
        assert r.headers["cache-control"] == "no-store"
        assert DONOR not in r.text and MESSAGE not in r.text
        return r.json()

    assert [a["kind"] for a in ok(limit=2)["alerts"]] == ["payment_on_hold", "job_failed"]
    assert ok(limit=1)["alerts"][0]["context"] == {"streamer_id": sid}
    page = ok(limit=1, offset=1)
    assert page["limit"] == 1 and page["offset"] == 1 and page["alerts"][0]["id"] == delivered
    assert set(page["alerts"][0]) == {"id", "source", "severity", "code", "kind", "context", "created_at", "notified_at"}
    assert [a["id"] for a in ok(kind="job_failed")["alerts"]] == [delivered]
    assert [a["id"] for a in ok(delivered="true")["alerts"]] == [delivered]
    assert ok(kind="payment_on_hold", delivered="true")["alerts"] == []
    assert [a["kind"] for a in ok(delivered="false")["alerts"]] == ["payment_on_hold"]
    for params, code in (({"limit": 201}, 422), ({"limit": 0}, 422), ({"offset": -1}, 422), ({"kind": "weird"}, 400),
                         ({"delivered": "maybe"}, 422)):
        assert app.get(A, params=params).status_code == code, params
    with db() as conn:  # the rows themselves hold nothing of the donor either
        text = json.dumps(conn.execute("SELECT source, message, context::text FROM public.system_alerts").fetchall())
    assert DONOR not in text and MESSAGE not in text and did in text
