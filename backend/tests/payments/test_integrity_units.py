"""C1 payment integrity evidence: parsing and the recording decision, without a database (app.payments.integrity)."""

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.payments import integrity
from tests.payments.fakes import PAYER_CONTACT, PAYER_EMAIL, integrity_body

PAY, ORDER = "pay_TESTabc123", "order_TESTxyz789"


def _parse(event: str, **kw):
    return integrity.parse(event, json.loads(integrity_body(event, ref=kw.pop("ref", "disp_TEST1" if "dispute" in event
                                                                               else "rfnd_TEST1"),
                                                            payment_id=kw.pop("payment_id", PAY),
                                                            order_id=kw.pop("order_id", ORDER), **kw)))


def test_the_supported_events_are_exactly_six_dispute_and_three_refund_events():
    assert integrity.INTEGRITY_EVENTS == {
        "payment.dispute.created", "payment.dispute.under_review", "payment.dispute.action_required",
        "payment.dispute.won", "payment.dispute.lost", "payment.dispute.closed",
        "refund.created", "refund.processed", "refund.failed"}
    assert "refund.speed_changed" not in integrity.INTEGRITY_EVENTS  # no integrity meaning: stays "ignored"


@pytest.mark.parametrize("event,status", [
    ("payment.dispute.created", "open"), ("payment.dispute.under_review", "under_review"),
    ("payment.dispute.action_required", "open"), ("payment.dispute.won", "won"), ("payment.dispute.lost", "lost"),
    ("payment.dispute.closed", "closed")])
def test_dispute_events_parse_ids_enums_amounts_and_times_only(event, status):
    e = _parse(event, amount=12345, amount_deducted=500, phase="pre_arbitration")
    assert (e.kind, e.event_type, e.provider_ref, e.payment_id, e.order_id) == ("dispute", event, "disp_TEST1", PAY,
                                                                                ORDER)
    assert (e.phase, e.status, e.amount_paise, e.currency, e.amount_deducted_paise) == (
        "pre_arbitration", status, 12345, "INR", 500)
    assert e.reason_code == "chargeback_fraud" and e.complete
    assert e.respond_by == datetime.fromtimestamp(1_800_600_000, UTC)
    assert e.provider_event_at == datetime.fromtimestamp(1_800_000_000, UTC)
    assert PAYER_EMAIL not in repr(e) and PAYER_CONTACT not in repr(e)  # payer details are never kept


@pytest.mark.parametrize("event,status", [("refund.created", "pending"), ("refund.processed", "processed"),
                                          ("refund.failed", "failed")])
def test_refund_events_have_no_phase_deduction_or_deadline(event, status):
    e = _parse(event, amount=5000)
    assert (e.kind, e.status, e.amount_paise, e.phase, e.amount_deducted_paise, e.respond_by) == (
        "refund", status, 5000, None, None, None)
    assert e.complete


@pytest.mark.parametrize("kw", [{"ref": "rfnd_TEST1"}, {"ref": "disp_"}, {"ref": "disp_x y"}, {"ref": "x" * 60},
                                {"payment_id": "pay_"}, {"payment_id": "order_TEST1"}, {"payment_id": "pay_a b"}])
def test_an_unusable_reference_or_payment_id_is_not_evidence(kw):
    assert _parse("payment.dispute.created", **kw) is None


def test_missing_entity_and_unknown_events_are_not_evidence():
    assert integrity.parse("payment.dispute.created", {"payload": {}}) is None
    assert integrity.parse("payment.dispute.created", {"payload": {"dispute": {"entity": "x"}}}) is None
    assert integrity.parse("payment.captured", json.loads(integrity_body(
        "payment.dispute.created", ref="disp_TEST1", payment_id=PAY, order_id=ORDER))) is None


@pytest.mark.parametrize("kw,field", [
    ({"omit": ("status",)}, "status"), ({"status": "reversed"}, "status"), ({"omit": ("phase",)}, "phase"),
    ({"phase": "appeal"}, "phase"), ({"omit": ("amount",)}, "amount"), ({"omit": ("currency",)}, "currency"),
    ({"currency": "inr"}, "currency")])
def test_missing_or_unknown_fields_make_the_evidence_incomplete_but_keep_it(kw, field):
    e = _parse("payment.dispute.lost", **kw)
    assert e is not None and not e.complete, field
    assert e.status == "lost" if field == "status" else True  # the event's own status when the entity's is unusable


@pytest.mark.parametrize("value", [True, -1, 1.5, "100", None])
def test_amounts_must_be_non_negative_integers(value):
    raw = json.loads(integrity_body("refund.processed", ref="rfnd_TEST1", payment_id=PAY, order_id=ORDER))
    raw["payload"]["refund"]["entity"]["amount"] = value
    e = integrity.parse("refund.processed", raw)
    assert e.amount_paise is None and not e.complete


def test_provider_times_outside_a_sane_range_are_dropped():
    raw = json.loads(integrity_body("payment.dispute.created", ref="disp_TEST1", payment_id=PAY, order_id=ORDER))
    raw["created_at"], raw["payload"]["dispute"]["entity"]["respond_by"] = 99_999_999_999, True
    e = integrity.parse("payment.dispute.created", raw)
    assert (e.provider_event_at, e.respond_by) == (None, None)


def test_a_missing_order_id_is_kept_as_none():
    assert _parse("payment.dispute.created", order_id=None).order_id is None


DONATION = {"id": "d1", "order_id": "authtest-o1", "amount": Decimal("100.00"), "currency": "INR",
            "payment_status": "success", "account": "primary"}


@pytest.mark.parametrize("evidence_kw,donation,expected", [
    ({}, DONATION, True),
    ({}, None, True),                                                   # unknown payment: complete is enough
    ({"amount": 10001}, DONATION, False),                               # more than the payment
    ({"amount": 10000}, DONATION, True),                                # exactly the payment
    ({"currency": "USD"}, DONATION, False),
    ({}, {**DONATION, "payment_status": "pending"}, False),             # never a dispute on an unpaid donation
    ({"omit": ("phase",)}, DONATION, False),
])
def test_consistency(evidence_kw, donation, expected):
    assert integrity.consistent(_parse("payment.dispute.created", **evidence_kw), donation) is expected


@dataclass
class FakeRepo:
    target: tuple
    recorded: list
    events: list
    duplicate: bool = False

    async def integrity_target(self, payment_id, order_id):
        return self.target

    async def record_integrity(self, event, outcome, evidence, donation_id, match_type, ok):
        if self.duplicate:
            return False
        self.recorded.append((event.account, outcome, donation_id, match_type, ok))
        return True

    async def record_event(self, event, outcome, donation_id=None):
        self.events.append((outcome, donation_id))
        return not self.duplicate


@dataclass(frozen=True)
class Event:
    account: str = "primary"


def _record(target, signed_by=("primary",), duplicate=False, event="payment.dispute.lost", **kw):
    repo = FakeRepo(target, [], [], duplicate)
    outcome = asyncio.run(integrity.record(repo, Event(), _parse(event, **kw) if event else None, list(signed_by)))
    return outcome, repo


def test_a_payment_match_is_recorded_against_its_donation():
    outcome, repo = _record((DONATION, "payment"))
    assert outcome == "integrity_recorded" and repo.recorded == [("primary", "integrity_recorded", "d1", "payment", True)]


def test_another_accounts_donation_is_refused_and_never_stored_as_evidence():
    outcome, repo = _record(({**DONATION, "account": "secondary"}, "payment"))
    assert outcome == "account_mismatch" and repo.recorded == [] and repo.events == [("account_mismatch", "d1")]
    outcome, repo = _record(({**DONATION, "account": None}, "order"))  # a creator without an account
    assert outcome == "account_mismatch" and repo.recorded == []


def test_unknown_payments_are_recorded_under_the_signing_account_only():
    outcome, repo = _record((None, "none"))
    assert outcome == "integrity_unknown_payment" and repo.recorded == [("primary", outcome, None, "none", True)]


def test_order_only_matches_and_mismatches_keep_their_labels():
    assert _record((DONATION, "order"))[1].recorded == [("primary", "integrity_recorded", "d1", "order", True)]
    outcome, repo = _record((DONATION, "payment"), currency="USD")
    assert outcome == "integrity_mismatch" and repo.recorded[0][4] is False


def test_duplicates_and_unusable_events():
    assert _record((DONATION, "payment"), duplicate=True)[0] == "duplicate"
    outcome, repo = _record((DONATION, "payment"), event=None)
    assert outcome == "no_order" and repo.events == [("no_order", None)] and repo.recorded == []
