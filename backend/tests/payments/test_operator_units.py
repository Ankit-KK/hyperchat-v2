"""PAY-15 stage 6a-3 without a database: argument validation, the pure plan, and refusing before any connection."""

import json
from decimal import Decimal

import pytest

from app.payments import accounting_ops as ops
from app.payments import operator

DID = "11111111-1111-4111-8111-111111111111"
PAY = "pay_TESTops1"


def donation(applied=0, hold=None, flags=()):
    return {"id": DID, "streamer_id": "22222222-2222-4222-8222-222222222222", "amount": Decimal("100.00"),
            "amount_inr": Decimal("100.00"), "currency": "INR", "payment_status": "success", "provider_payment_id": PAY,
            "legacy_source_table": None, "v2": True, "applied": applied, "hold": hold, "flags": list(flags)}


def row(kind, ref, status, amount=2500, deducted=None):
    return {"kind": kind, "provider_ref": ref, "provider_payment_id": PAY, "donation_id": DID, "match_type": "payment",
            "phase": "chargeback" if kind == "dispute" else None, "status": status, "amount_paise": amount,
            "currency": "INR", "amount_deducted_paise": deducted, "conflicting": False}


@pytest.mark.parametrize("kw, why", [
    ({"apply": True, "confirm": None, "operator": "op", "reason": "a long enough reason"}, "confirm-database"),
    ({"apply": True, "confirm": "db", "operator": None, "reason": "a long enough reason"}, "operator"),
    ({"apply": True, "confirm": "db", "operator": "Op Bad", "reason": "a long enough reason"}, "operator"),
    ({"apply": True, "confirm": "db", "operator": "op", "reason": "short"}, "reason"),
    ({"apply": True, "confirm": "db", "operator": "op", "reason": "x" * 501}, "reason"),
])
def test_apply_needs_every_confirmation(kw, why):
    with pytest.raises(ops.Refusal, match=why):
        ops.validate(donations=None, **kw)


def test_donation_filters_are_uuids_without_repeats_and_report_needs_nothing():
    assert ops.validate(apply=False, confirm=None, operator=None, reason=None, donations=None) is None
    assert ops.validate(apply=False, confirm=None, operator=None, reason=None, donations=[DID.upper()]) == [DID]
    for bad in (["not-a-uuid"], [DID, DID]):
        with pytest.raises(ops.Refusal):
            ops.validate(apply=False, confirm=None, operator=None, reason=None, donations=bad)


def test_the_reconcile_plan_is_the_application_evaluator_and_nothing_when_already_applied():
    rows = [row("refund", "rfnd_TESTa", "processed", 2500)]
    item = ops.plan_item("reconcile", donation(), rows, "apply")
    assert (item["reversed"], item["hold"], item["refs"], item["to"]) == (2500, None, ["rfnd_TESTa"], [2500, None, []])
    assert ops.plan_item("reconcile", donation(applied=2500), rows, "apply") is None  # the second report: zero
    held = ops.plan_item("reconcile", donation(), [row("dispute", "disp_TESTa", "lost", 10000, 4000)], "hold")
    assert held["hold"] == "H-DISPUTE-MODE" and held["refs"] == [] and held["to"] == [0, "H-DISPUTE-MODE", []]


def test_resolve_and_revert_plans():
    lost = [row("dispute", "disp_TESTb", "lost", 10000, 4000)]
    assert ops.plan_item("keep", donation(hold="H-DISPUTE-MODE"), lost, "hold") == {"donation": DID,
                                                                                  "hold": "H-DISPUTE-MODE"}
    assert ops.plan_item("keep", donation(hold="H-CONFLICT"), lost, "hold") is None  # never an integrity hold
    clear = [row("refund", "rfnd_TESTc", "processed", 2500)]
    assert ops.plan_item("apply", donation(hold="H-CURRENCY"), clear, "apply") == {
        "donation": DID, "reversed": 2500, "flags": [], "refs": ["rfnd_TESTc"]}
    assert ops.plan_item("apply", donation(), clear, "apply") is None  # not held
    assert ops.plan_item("revert", donation(applied=2500), clear, "apply") == {"donation": DID, "from": [2500, None],
                                                                               "to": [0, None]}
    assert ops.plan_item("revert", donation(), clear, "apply") is None


@pytest.mark.parametrize("argv", [["state"], ["reconcile"], ["resolve"], ["resolve", "keep"], ["revert"],
                                  ["reconcile", "--apply"], ["revert", "--donation", "x"], ["reconcile", "--bogus"]])
def test_every_writer_refuses_before_connecting_without_its_inputs(argv, monkeypatch, capsys):
    monkeypatch.delenv(ops.OWNER_DSN_ENV, raising=False)
    assert operator.main(argv) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "refused" and out["exit_code"] == 2


def test_the_exit_codes_are_d7():
    assert ops.EXIT == {"clean": 0, "failed": 1, "refused": 2, "planned": 3}
