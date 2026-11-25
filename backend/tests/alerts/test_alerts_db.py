"""Operator failure alerts on PostgreSQL (scratch database): the writer behind each source, the worker hook, the
admin read endpoint, and no donor data in any alert row."""

import asyncio
import json
import secrets
import uuid
from decimal import Decimal

import pytest

from app.alerts import Alerts
from app.jobs.queue import JobQueue
from app.jobs.retry import Backoff, Permanent
from app.jobs.runner import Worker
from tests.integration import DOMAIN, db, make_donation, make_streamer, new_browser, requires_db, signed_in
from tests.jobs.test_jobs_db import factory

pytestmark = requires_db
A = "/api/v1/admin/alerts"
DONOR, MESSAGE = "Hidden Donor " + secrets.token_hex(4), "private donor words " + secrets.token_hex(4)


def run(coro):
    return asyncio.run(coro)


def rows(ids: list[str]) -> list[tuple]:
    with db() as conn:
        return conn.execute("SELECT source, severity, message, context, notified_at FROM public.system_alerts "
                            "WHERE id = ANY(%s::uuid[]) ORDER BY created_at", (ids,)).fetchall()


class Failing:
    kind = "donation_audio"
    backoff = Backoff(0.01, 0.02)

    async def run(self, job):
        raise Permanent("provider_auth")

    async def fallback(self, job, reason):
        return None


def failed_job(alerts) -> tuple[str, str]:
    """Run one donation_audio job of a synthetic (secret-bearing) donation to failure; (job id, donation id)."""
    sid = make_streamer("authtest-al-" + secrets.token_hex(3))
    did = make_donation(sid, Decimal("100"), payment="success", moderation="auto_approved", name=DONOR, message=MESSAGE)
    q = JobQueue(factory())
    jid = run(q.enqueue("donation_audio", did, {"donation_id": did}))
    assert run(Worker(q, {"donation_audio": Failing()}, on_failure=alerts.job_failed).run_once()).id == jid
    return jid, did


def test_exhausted_job_writes_an_alert_of_ids_and_codes_only(alerts_clean):
    jid, did = failed_job(Alerts(factory()))
    with db() as conn:
        assert conn.execute("SELECT status, last_error FROM public.jobs WHERE id = %s", (jid,)).fetchone() == (
            "failed", "provider_auth")
        ids = [r[0] for r in conn.execute("SELECT id FROM public.system_alerts WHERE context->>'job_id' = %s", (jid,))]
    assert len(ids) == 1
    ((source, severity, code, context, notified_at),) = rows(ids)
    assert (source, severity, code, notified_at) == ("worker", "error", "provider_auth", None)
    assert context == {"kind": "job_failed", "job_id": jid, "job_kind": "donation_audio", "dedupe_key": did}


def test_an_alert_that_cannot_be_written_never_fails_the_job(alerts_clean, caplog):
    class Broken(Alerts):
        async def record(self, **fields):
            raise RuntimeError("alerts table gone")
    jid, _ = failed_job(Broken(factory()))
    with db() as conn:
        assert conn.execute("SELECT status FROM public.jobs WHERE id = %s", (jid,)).fetchone() == ("failed",)
    assert "alert not recorded" in caplog.text


def test_payment_hold_alert_carries_the_creator_id_and_status(alerts_clean):
    sid = make_streamer("authtest-al-hold")
    run(Alerts(factory()).payment_on_hold(sid, "invalid_key"))
    with db() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM public.system_alerts WHERE context->>'streamer_id' = %s",
                                          (sid,))]
    ((source, severity, code, context, _),) = rows(ids)
    assert (source, severity, code, context) == ("payments", "error", "invalid_key",
                                                 {"kind": "payment_on_hold", "streamer_id": sid})


def test_admin_alerts_endpoint_filters_pages_and_holds_no_donor_data(app, alerts_clean):
    assert new_browser(app).get(A).status_code == 401
    plain = new_browser(app)
    signed_in(plain, "plain" + DOMAIN)
    assert plain.get(A).status_code == 403
    signed_in(app, "boss" + DOMAIN, role="admin")
    alerts = Alerts(factory())
    jid, did = failed_job(alerts)
    sid = make_streamer("authtest-al-page")
    held = run(alerts.payment_on_hold(sid, "missing_key"))  # noqa: F841 - second alert, newest
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
    assert [a["id"] for a in ok(kind="job_failed", limit=1)["alerts"]] == [delivered]
    assert ok(delivered="true", limit=1)["alerts"][0]["id"] == delivered
    assert ok(kind="payment_on_hold", delivered="true", limit=200)["alerts"] == []
    assert ok(delivered="false", limit=1)["alerts"][0]["kind"] == "payment_on_hold"
    assert app.get(A, params={"limit": 201}).status_code == 422
    assert app.get(A, params={"kind": "weird"}).status_code == 400
    with db() as conn:  # the rows themselves hold nothing of the donor either
        text = json.dumps(conn.execute("SELECT source, message, context::text FROM public.system_alerts "
                                       "WHERE context->>'job_id' = %s OR context->>'streamer_id' = %s",
                                       (jid, sid)).fetchall(), default=str)
    assert DONOR not in text and MESSAGE not in text and str(uuid.UUID(did)) in text
