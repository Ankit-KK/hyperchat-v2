"""PAY-15 (D17) PR 1: the read-only reversal evaluator (app.payments.accounting). No database, no network."""

import ast
import copy
import itertools
import random
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from app.payments import accounting
from app.payments.accounting import Evaluation, evaluate, reversed_inr


DID, OTHER_DID = "8d0c6a2e-1111-4a5b-9c3d-000000000001", "8d0c6a2e-1111-4a5b-9c3d-000000000002"
PAY, OTHER_PAY = "pay_TESTcompleting01", "pay_TESTduplicate02"


def donation(**kw):
    return {"id": DID, "provider_payment_id": PAY, "amount": Decimal("100.00"), "currency": "INR",
            "amount_inr": Decimal("100.00"), "payment_status": "success", "legacy_source_table": None, **kw}


def _row(**kw):  # the payment_integrity_current columns the evaluator reads
    return {"donation_id": DID, "provider_payment_id": PAY, "match_type": "payment", "conflicting": False, **kw}


def refund(ref="rfnd_A", status="processed", amount=2500, currency="INR", match="payment", **kw):
    return _row(provider_ref=ref, kind="refund", match_type=match, status=status, phase=None, amount_paise=amount,
                currency=currency, amount_deducted_paise=None, **kw)


def dispute(ref="disp_A", status="lost", deducted=10000, amount=10000, phase="chargeback", currency="INR",
            match="payment", conflicting=False, **kw):
    return _row(provider_ref=ref, kind="dispute", match_type=match, status=status, phase=phase, amount_paise=amount,
                currency=currency, amount_deducted_paise=deducted, conflicting=conflicting, **kw)


def clear(paise, *flags):
    return Evaluation(paise, None, frozenset(flags))


def held(reason, *flags):
    return Evaluation(None, reason, frozenset(flags))


# ── what moves money (PAY15-D2) ────────────────────────────────────────────────

def test_no_evidence_reverses_nothing():
    assert evaluate(donation(), []) == clear(0)


@pytest.mark.parametrize("status, expected", [("processed", 2500), ("pending", 0), ("failed", 0)])
def test_only_processed_refunds_count(status, expected):
    assert evaluate(donation(), [refund(status=status)]) == clear(expected)


def test_partial_refunds_add_up():
    rows = [refund("rfnd_A", amount=2500), refund("rfnd_B", amount=1000), refund("rfnd_C", "failed", 5000)]
    assert evaluate(donation(), rows) == clear(3500)


def test_a_full_refund_reverses_the_captured_amount_exactly():
    assert evaluate(donation(), [refund(amount=10000)]) == clear(10000)


def test_only_lost_disputes_count_with_their_deducted_amount():
    assert evaluate(donation(), [dispute(deducted=7000, amount=10000)]) == clear(7000)


@pytest.mark.parametrize("status, flags", [("open", {"F-OPEN"}), ("under_review", {"F-OPEN"}),
                                           ("action_required", {"F-OPEN"}), ("won", set()), ("closed", set())])
def test_other_dispute_states_count_zero(status, flags):
    assert evaluate(donation(), [dispute(status=status, deducted=0)]) == clear(0, *flags)


def test_a_won_dispute_after_a_loss_is_its_current_state_and_restores():
    # the view gives one current row per dispute: once it is won, nothing is reversed any more
    assert evaluate(donation(), [dispute(status="won", deducted=0)]) == clear(0)


def test_a_refund_with_an_open_dispute_counts_the_refund_and_flags_the_dispute():
    assert evaluate(donation(), [refund(amount=2500), dispute(status="open", deducted=0)]) == clear(2500, "F-OPEN")


# ── refund and lost dispute on one payment (PAY15-D3, risk R1) ─────────────────

@pytest.mark.parametrize("r, l_, expected", [(2500, 10000, 10000), (10000, 10000, 10000), (8000, 3000, 8000)])
def test_both_kinds_apply_the_larger_never_the_sum(r, l_, expected):
    for rows in itertools.permutations([refund(amount=r), dispute(deducted=l_)]):
        assert evaluate(donation(), list(rows)) == clear(expected, "F-BOTH")


# ── holds (PAY15-D3) ───────────────────────────────────────────────────────────

def test_a_conflicting_dispute_is_held():
    assert evaluate(donation(), [refund(), dispute(conflicting=True)]) == held("H-CONFLICT")


@pytest.mark.parametrize("row", [
    dispute(status="reversed"), dispute(phase="appeal"), dispute(phase=None), refund(status="reversed"),
    {**refund(), "kind": "chargeback"}, refund(amount=None), refund(amount=-1), refund(amount=True),
    refund(amount=25.0)])
def test_unreadable_evidence_is_held_as_a_conflict(row):
    assert evaluate(donation(), [row]).hold == "H-CONFLICT"


def test_exact_repeats_change_nothing_but_two_different_current_states_for_one_reference_are_held():
    one = refund("rfnd_A", amount=2500)
    assert evaluate(donation(), [one, dict(one), copy.deepcopy(one)]) == clear(2500)
    assert evaluate(donation(), [one, refund("rfnd_A", amount=3000)]) == held("H-CONFLICT")


@pytest.mark.parametrize("deducted", [0, None])
def test_a_lost_dispute_without_a_deduction_is_held(deducted):
    assert evaluate(donation(), [dispute(deducted=deducted)]) == held("H-NO-DEDUCTION")


@pytest.mark.parametrize("rows", [
    [refund("rfnd_A", amount=6000), refund("rfnd_B", amount=6000)],   # partials above the payment
    [dispute(deducted=10001, amount=10001)]])
def test_more_than_the_captured_amount_is_held(rows):
    assert evaluate(donation(), rows) == held("H-OVER-REFUND")


def test_exactly_the_captured_amount_is_not_an_over_refund():
    rows = [refund("rfnd_A", amount=6000), refund("rfnd_B", amount=4000)]
    assert evaluate(donation(), rows) == clear(10000)


@pytest.mark.parametrize("don, rows", [
    (donation(), [refund(currency="USD")]),                                 # refund in another currency
    (donation(), [refund(currency=None)]),
    (donation(currency="USD", amount_inr=Decimal("8900.00")), [dispute(currency="USD")]),  # non-INR dispute
    (donation(), [dispute(currency="USD")]),
    (donation(currency="JPY"), [refund(currency="JPY")]),                   # outside the supported eight
])
def test_currency_restrictions_hold(don, rows):
    assert evaluate(don, rows).hold == "H-CURRENCY"


def test_a_non_inr_refund_in_the_donation_currency_counts():
    don = donation(currency="USD", amount=Decimal("10.00"), amount_inr=Decimal("890.00"))
    assert evaluate(don, [refund(amount=250, currency="USD")]) == clear(250)


def test_holds_are_reported_in_priority_order_and_keep_flags():
    rows = [refund("rfnd_A", amount=20000, currency="USD"), dispute(deducted=0), dispute("disp_B", conflicting=True),
            dispute("disp_C", status="open", deducted=0)]
    assert evaluate(donation(), rows) == held("H-CONFLICT", "F-OPEN")
    assert evaluate(donation(), rows[:2]).hold == "H-CURRENCY"
    assert evaluate(donation(), rows[1:2]).hold == "H-NO-DEDUCTION"


# ── what never moves money (F6, PAY15-D8) ──────────────────────────────────────

# ── attribution to the completing payment (checked here, not trusted from match_type) ─────

@pytest.mark.parametrize("match", ["order", "none"])
def test_evidence_of_another_payment_of_the_order_is_ignored(match):
    # e.g. a refund or a lost dispute on a duplicate capture: never this donation's money (F6)
    rows = [refund(match=match, amount=10000, provider_payment_id=OTHER_PAY),
            dispute(match=match, conflicting=True, provider_payment_id=OTHER_PAY)]
    assert evaluate(donation(), rows) == clear(0)


def test_early_evidence_stored_as_order_counts_once_the_capture_is_known():
    # recorded before the capture completed, so PAY-13 stored match_type 'order'; it IS the completing payment
    assert evaluate(donation(), [refund(match="order", amount=2500)]) == clear(2500)
    assert evaluate(donation(), [dispute(match="order", deducted=7000)]) == clear(7000)


def test_a_row_claiming_the_completing_payment_for_another_payment_is_held():
    assert evaluate(donation(), [refund(provider_payment_id=OTHER_PAY)]) == held("H-CONFLICT")


@pytest.mark.parametrize("donation_id", [OTHER_DID, None, ""])
def test_another_donations_evidence_passed_by_mistake_is_held_never_applied(donation_id):
    rows = [refund(amount=10000, donation_id=donation_id), refund("rfnd_B", amount=100)]
    assert evaluate(donation(), rows) == held("H-CONFLICT")


@pytest.mark.parametrize("payment_id", [None, ""])
def test_a_paid_donation_without_a_completing_payment_id_cannot_attribute_anything(payment_id):
    assert evaluate(donation(provider_payment_id=payment_id or None), [refund()]) == held("H-CONFLICT")


def test_ids_compare_as_text_so_uuid_objects_from_the_driver_also_match():
    assert evaluate(donation(id=uuid.UUID(DID)), [refund(donation_id=DID)]) == clear(2500)
    assert evaluate(donation(), [refund(donation_id=uuid.UUID(DID))]) == clear(2500)


# ── provider references ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("row", [refund(ref=None), refund(ref=""), refund(ref="rfnd_"), refund(ref="rfnd_A B"),
                                 refund(ref="disp_A"), dispute(ref="rfnd_A"), dispute(ref=None), dispute(ref=7)])
def test_counted_evidence_needs_a_valid_reference_of_its_own_kind(row):
    assert evaluate(donation(), [row]).hold == "H-CONFLICT"


def test_refund_and_dispute_references_are_separate_namespaces_keyed_by_kind():
    # 0029 enforces the prefixes; the evaluator keys rows by (kind, reference), so the same suffix is two items
    rows = [refund("rfnd_X1", amount=2500), dispute("disp_X1", status="won", deducted=0)]
    assert evaluate(donation(), rows) == clear(2500)
    # one reference string used by both kinds is invalid for one of them, so it can never merge two items
    assert evaluate(donation(), [refund("rfnd_X1"), dispute("rfnd_X1")]) == held("H-CONFLICT")


def test_imported_donations_are_reported_never_reversed():
    imported = donation(legacy_source_table="legacy_donations")
    assert evaluate(imported, [refund(amount=10000), dispute()]) == clear(0, "REPORT-IMPORTED")
    assert evaluate(imported, [dispute(conflicting=True)]) == clear(0, "REPORT-IMPORTED")  # never held either
    assert evaluate(imported, []) == clear(0)
    assert evaluate(imported, [refund(match="order", provider_payment_id=OTHER_PAY)]) == clear(0, "REPORT-IMPORTED")


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_an_unpaid_donation_has_nothing_to_reverse_yet(status):
    assert evaluate(donation(payment_status=status), [refund(amount=10000), dispute()]) == clear(0)


# ── INR value (PAY15-D4) ───────────────────────────────────────────────────────

@pytest.mark.parametrize("don, paise, expected", [
    (donation(), 2500, Decimal("25.00")),
    (donation(), 10000, Decimal("100.00")),
    (donation(amount=Decimal("3.00"), amount_inr=Decimal("1.00")), 100, Decimal("0.33")),   # rounded down
    (donation(currency="USD", amount=Decimal("10.00"), amount_inr=Decimal("890.00")), 333, Decimal("296.37")),
    (donation(), 99999, Decimal("100.00")),                                                 # capped
    (donation(), 0, Decimal("0.00")), (donation(), -5, Decimal("0.00")),
])
def test_reversed_inr_is_pro_rata_rounded_down_and_capped(don, paise, expected):
    assert reversed_inr(don, paise) == expected


def test_reversed_inr_works_on_the_cumulative_amount_not_per_event():
    don = donation(amount=Decimal("3.00"), amount_inr=Decimal("1.00"))
    assert reversed_inr(don, 300) == Decimal("1.00")                      # one cumulative conversion
    assert sum(reversed_inr(don, 100) for _ in range(3)) == Decimal("0.99")  # per event would lose a paisa


# ── properties over random evidence sets ───────────────────────────────────────

def _random_rows(r: random.Random):
    rows = []
    for i in range(r.randint(0, 5)):
        if r.random() < .5:
            rows.append(refund(f"rfnd_{i}", r.choice(["processed", "pending", "failed"]), r.choice([100, 2500, 5000, 10000]),
                               r.choice(["INR"] * 9 + ["USD"]), r.choice(["payment"] * 4 + ["order"]),
                               provider_payment_id=r.choice([PAY] * 4 + [OTHER_PAY])))
        else:
            status = r.choice(["open", "under_review", "action_required", "won", "lost", "closed"])
            rows.append(dispute(f"disp_{i}", status, r.choice([0, 3000, 10000]) if status == "lost" else 0,
                                currency=r.choice(["INR"] * 9 + ["USD"]), conflicting=r.random() < .05,
                                match=r.choice(["payment"] * 4 + ["order"]),
                                provider_payment_id=r.choice([PAY] * 4 + [OTHER_PAY])))
    for row in rows:  # keep the schema's rule: a 'payment' match always names the completing payment
        if row["match_type"] == "payment":
            row["provider_payment_id"] = PAY
    return rows


def test_properties_over_random_evidence():
    r = random.Random(20261113)
    for _ in range(20_000):
        don, rows = donation(), _random_rows(r)
        before = copy.deepcopy(rows)
        result = evaluate(don, rows)
        assert rows == before, "pure: the input is never modified"
        # F1: a clear result stays within the captured payment; a held result carries no amount
        assert (result.hold is None) == (result.reversed_paise is not None)
        if result.hold is None:
            assert 0 <= result.reversed_paise <= accounting.captured_paise(don)
        # F2: order and exact repeats change nothing
        shuffled = rows + [dict(rows[0])] if rows else []
        r.shuffle(shuffled)
        assert evaluate(don, shuffled) == result
        # F6: evidence of other payments changes nothing
        assert evaluate(don, rows + [refund("rfnd_x", match="order", amount=10000, provider_payment_id=OTHER_PAY)]) \
            == result
        # F11: with both kinds the result is the larger one, never the sum
        counted = [x for x in rows if x["provider_payment_id"] == PAY]
        refunds = sum(x["amount_paise"] for x in counted if x["kind"] == "refund" and x["status"] == "processed")
        lost = sum(x["amount_deducted_paise"] for x in counted if x["kind"] == "dispute" and x["status"] == "lost")
        if result.hold is None:
            assert result.reversed_paise == max(refunds, lost)
            assert ("F-BOTH" in result.flags) == (refunds > 0 and lost > 0)


def test_a_new_processed_refund_never_lowers_a_clear_reversal():
    r = random.Random(7)
    for _ in range(5_000):
        rows = _random_rows(r)
        first = evaluate(donation(), rows)
        later = evaluate(donation(), rows + [refund("rfnd_new", amount=r.choice([100, 2500]))])
        if first.hold is None and later.hold is None:
            assert later.reversed_paise >= first.reversed_paise


def test_the_evaluator_is_pure_code_without_io():
    tree = ast.parse(Path(accounting.__file__).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert imported <= {"collections.abc", "dataclasses", "decimal", "app.payments", "app.public"}
    assert not any(isinstance(n, (ast.AsyncFunctionDef, ast.Await)) for n in ast.walk(tree))
