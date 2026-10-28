"""Step G1 reconciliation (app.payments.reconcile) with in-memory fakes: classification, report-only guarantee,
fail-closed Razorpay handling, interruption, pacing and report redaction. PostgreSQL behaviour (real apply,
legacy exclusion, concurrency) is in test_payments_db.py."""

import asyncio
import json
import logging
from decimal import Decimal

import pytest

from app.payments.reconcile import Reconciler, classify
from app.payments.repository import Applied
from tests.payments.fakes import FakeRazorpay

COUNTS = {"too_recent": 1, "too_old": 2, "legacy_excluded": 3, "ambiguous_provenance": 0, "beyond_limit": 0}


def row(n: int, state: str = "pending", amount="100.00", currency="INR", account="primary") -> dict:
    return {"id": f"don-{n}", "order_id": f"creator_rp_1800000000000_{n:08d}", "provider_order_id": f"order_TESTord{n:04d}",
            "amount": Decimal(amount), "currency": currency, "payment_status": state, "account": account,
            "own_account": account, "streamer_slug": "creator", "status_token_hash": "x", "provider_payment_id": None}


class Repo:
    def __init__(self, rows, counts=None, *, paid_with=None, forbid_apply=False):
        self.rows, self.counts, self.applied = rows, dict(counts or COUNTS), []
        self.paid_with, self.forbid_apply = paid_with, forbid_apply

    async def reconcile_candidates(self, min_age, max_age, limit):
        self.window = (min_age, max_age, limit)
        return list(self.rows), dict(self.counts)

    async def apply(self, donation_id, outcome, payment_id, event=None):
        assert not self.forbid_apply, "report mode must never apply"
        self.applied.append((donation_id, outcome, payment_id))
        if self.paid_with is not None:
            return Applied("already_paid", "success", paid_with=self.paid_with)
        return Applied("completed", "success", True)


class Account:
    name = "primary"


class Accounts:
    def for_payments(self, name):
        return Account() if name == "primary" else None


def run(repo, rz, *, apply=False, sleeps=None, stop_after=None, **kw):
    async def sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)
    reconciler = Reconciler(repo, Accounts(), rz, sleep=sleep)
    if stop_after is not None:
        original = rz.order_payments

        async def order_payments(account, order_id):
            result = await original(account, order_id)
            if rz.count("order_payments") >= stop_after:
                reconciler.request_stop()
            return result
        rz.order_payments = order_payments
    return asyncio.run(reconciler.run(apply=apply, **kw))


def security(caplog):
    return [(r.getMessage(), getattr(r, "reason", None), getattr(r, "order_id", None))
            for r in caplog.records if r.name == "hyperchat.security"]


# ── classification ──────────────────────────────────────────────────────────

def test_classify_requires_exactly_one_captured_payment_that_pays_exactly_this_order():
    rz, r = FakeRazorpay(), row(1)
    ok = rz.captured(r["provider_order_id"], 10000)
    assert classify(r, [ok]) == ("verified", ok.id, None)
    failed = rz.captured(r["provider_order_id"], 10000, status="failed")
    assert classify(r, [failed, ok]) == ("verified", ok.id, None)  # D-F1: failed attempts before the capture
    for status in ("created", "authorized", "failed", "refunded"):
        assert classify(r, [rz.captured(r["provider_order_id"], 10000, status=status)]) == ("no_capture", None, None)
    assert classify(r, []) == ("no_capture", None, None)
    two = rz.captured(r["provider_order_id"], 10000)
    assert classify(r, [ok, two])[0] == "multiple"
    for bad, reason in ((rz.captured(r["provider_order_id"], 9999), "amount_mismatch"),
                        (rz.captured(r["provider_order_id"], 10000, currency="USD"), "currency_mismatch"),
                        (rz.captured("order_TESTother000", 10000), "payment_mismatch")):
        assert classify(r, [bad]) == ("mismatch", bad.id, reason)


# ── report-only guarantee ───────────────────────────────────────────────────

def test_report_mode_never_applies_and_flags_verified_captures(caplog):
    caplog.set_level(logging.INFO)
    rows = [row(1), row(2, "failed"), row(3)]
    rz = FakeRazorpay()
    rz.captured(rows[0]["provider_order_id"], 10000)
    rz.captured(rows[1]["provider_order_id"], 10000)
    report = run(Repo(rows, forbid_apply=True), rz)
    assert [o["outcome"] for o in report.orders] == ["verified", "verified", "no_capture"]
    assert "result" not in json.dumps(report.as_dict()) and report.exit_code() == 1
    assert not [e for e in security(caplog) if e[0] in ("payment_reconciled", "payment_duplicate_capture")]
    assert report.as_dict()["not_examined"] == COUNTS  # outside the window, legacy, ...: counted, not implied done


def test_apply_mode_applies_only_verified_captures_through_apply(caplog):
    caplog.set_level(logging.INFO)
    rows = [row(1), row(2, "failed"), row(3), row(4), row(5)]
    rz = FakeRazorpay()
    p1 = rz.captured(rows[0]["provider_order_id"], 10000)
    p2 = rz.captured(rows[1]["provider_order_id"], 10000)
    rz.captured(rows[3]["provider_order_id"], 10000)
    rz.captured(rows[3]["provider_order_id"], 10000)  # two captures: manual investigation
    rz.captured(rows[4]["provider_order_id"], 5000)   # mismatch
    repo = Repo(rows)
    report = run(repo, rz, apply=True)
    assert repo.applied == [("don-1", "captured", p1.id), ("don-2", "captured", p2.id)]
    assert [(o["outcome"], o.get("result")) for o in report.orders] == [
        ("verified", "completed"), ("verified", "completed"), ("no_capture", None), ("multiple", None), ("mismatch", None)]
    events = security(caplog)
    assert [e for e in events if e[0] == "payment_reconciled"] == [
        ("payment_reconciled", "pending", rows[0]["order_id"]), ("payment_reconciled", "failed", rows[1]["order_id"])]
    assert {(e[1], e[2]) for e in events if e[0] == "payment_reconcile_exception"} == {
        ("multiple", rows[3]["order_id"]), ("amount_mismatch", rows[4]["order_id"])}
    assert report.exit_code() == 1  # findings remain for a human


def test_already_paid_meanwhile_same_payment_is_quiet_other_payment_is_a_duplicate_capture(caplog):
    caplog.set_level(logging.INFO)
    rz = FakeRazorpay()
    r = row(1)
    p = rz.captured(r["provider_order_id"], 10000)
    report = run(Repo([r], paid_with=p.id), rz, apply=True)  # the webhook won the race with the same payment
    assert report.orders[0]["result"] == "already_paid" and not security(caplog) and report.exit_code() == 0
    caplog.clear()
    run(Repo([r], paid_with="pay_TESTotherpaid"), rz, apply=True)
    assert [e[0] for e in security(caplog)] == ["payment_duplicate_capture"]


# ── fail closed ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason", ["network", "auth", "not_found", "bad_response", "rejected"])
def test_a_razorpay_error_stops_the_run_and_nothing_further_is_asked_or_applied(reason):
    rows = [row(1), row(2), row(3)]
    rz = FakeRazorpay()
    rz.captured(rows[0]["provider_order_id"], 10000)
    calls = {"n": 0}
    original = rz.order_payments

    async def flaky(account, order_id):
        calls["n"] += 1
        if calls["n"] == 2:
            rz.fail["order_payments"] = reason
        return await original(account, order_id)
    rz.order_payments = flaky
    repo = Repo(rows)
    report = run(repo, rz, apply=True)
    assert [o["outcome"] for o in report.orders] == ["verified", "error"] and report.orders[1]["reason"] == reason
    assert report.stopped == f"razorpay_{reason}" and report.exit_code() == 2
    assert calls["n"] == 2 and len(repo.applied) == 1  # order 3 never asked
    assert report.as_dict()["not_examined"]["stopped_before"] == 1


def test_an_unconfigured_account_is_inconclusive_and_never_asked():
    rz = FakeRazorpay()
    report = run(Repo([row(1, account="secondary")]), rz, apply=True)
    assert report.orders[0]["outcome"] == "inconclusive" and rz.count("order_payments") == 0 and report.exit_code() == 1
    mismatched = dict(row(2), own_account="other")  # the creator's account must not stand in for the order's own
    report = run(Repo([mismatched]), rz, apply=True)
    assert report.orders[0]["outcome"] == "inconclusive" and rz.count("order_payments") == 0


def test_ambiguous_provenance_rows_are_a_finding_even_when_nothing_is_examined():
    report = run(Repo([], dict(COUNTS, ambiguous_provenance=2)), FakeRazorpay())
    assert report.orders == [] and report.exit_code() == 1


# ── interruption, pacing, window, redaction ─────────────────────────────────

def test_a_stop_request_finishes_the_current_order_and_examines_no_further_one():
    rows = [row(1), row(2), row(3)]
    rz = FakeRazorpay()
    for r in rows:
        rz.captured(r["provider_order_id"], 10000)
    repo = Repo(rows)
    report = run(repo, rz, apply=True, stop_after=1)
    assert [o["outcome"] for o in report.orders] == ["verified"] and len(repo.applied) == 1
    assert report.stopped == "interrupted" and report.exit_code() == 2
    assert report.as_dict()["not_examined"]["stopped_before"] == 2


def test_calls_are_paced_and_the_window_is_passed_in_seconds():
    sleeps, repo = [], Repo([row(1), row(2), row(3)])
    run(repo, FakeRazorpay(), sleeps=sleeps, min_age_minutes=20, max_age_days=3, limit=50, rate_per_second=2.0)
    assert sleeps == [0.5, 0.5] and repo.window == (1200, 259200, 50)


def test_the_report_holds_ids_outcomes_and_counts_only():
    r = dict(row(1), donor_name="SECRET_DONOR", message="SECRET_MESSAGE", status_token_hash="SECRET_HASH")
    rz = FakeRazorpay()
    rz.captured(r["provider_order_id"], 10000)
    text = json.dumps(run(Repo([r]), rz).as_dict())
    assert "SECRET" not in text and r["order_id"] in text
    keys = {k for o in json.loads(text)["orders"] for k in o}
    assert keys <= {"order_id", "state", "outcome", "payment_id", "reason", "attempts", "result"}
