"""PAY-15 (D17) stage 3, migration 0032 on PostgreSQL: reversal state, its append-only history, the reversal-aware
credit writer and public.donation_accounting_apply.

The function is the only writer of donation_reversals (EXECUTE: hyperchat_app). A clear result must equal what the
stored evidence it names supports (max of the processed refunds and the lost disputes, each current, attributed to
this donation AND its completing payment); a hold freezes the applied reversal; the total moves only in the one
writer's own ledger row. End-to-end tests run the real webhook route and repository with evaluation injected on
(the setting itself refuses to be enabled at this stage)."""

import asyncio
import random
import secrets
import threading
from decimal import Decimal

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from app.payments.repository import V2_PROVENANCE, PaymentRepository
from app.storage.memory import InMemoryObjectStore
from tests.jobs.test_jobs_db import factory, run
from tests.integration import db, fixture_db, make_donation, make_streamer, requires_db
from tests.payments.fakes import FakeRazorpay, integrity_body, sign
from tests.payments.test_integrity_db import paid, post
from tests.payments.test_payments_db import (  # noqa: F401 (fixtures)
    W,
    checkout,
    client,
    rz_env,
    totals,
    webhook,
    wire,
)

pytestmark = [requires_db, pytest.mark.usefixtures("env")]
REFUSED = psycopg.errors.InsufficientPrivilege
INVALID = psycopg.errors.InvalidParameterValue
APPLY = "SELECT public.donation_accounting_apply(%s, %s, %s, %s, %s)"


# ── fixtures ────────────────────────────────────────────────────────────────────

def _creator() -> str:
    return make_streamer(f"authtest-acct-{secrets.token_hex(4)}")


def _paid(sid: str, amount=100, *, moderation="approved", currency="INR", name="Fan",
          provenance="v2") -> tuple[str, str]:
    """A donation completed as the payment path does (pending -> success with its payment id, app role). provenance:
    "v2" = as the checkout creates it (payment_account and provider_order_id set, no legacy marker); "imported" = as
    the ETL loads it (legacy marker, no account); "none" / "both" = ambiguous (neither, or both markers)."""
    did = make_donation(sid, amount, payment="pending", moderation="pending", name=name, currency=currency)
    with fixture_db() as conn:  # provenance columns are written by checkout / the ETL only, as the owner here
        conn.execute("UPDATE public.donations SET payment_account = %s, provider_order_id = %s, legacy_source_table = %s "
                     "WHERE id = %s", ({"v2": "primary", "both": "primary"}.get(provenance),
                                       "order_TESTacct" + secrets.token_hex(5) if provenance in ("v2", "both") else None,
                                       "authtest_donations" if provenance in ("imported", "both") else None, did))
    pid = "pay_TESTacct" + secrets.token_hex(5)
    with db() as conn:
        conn.execute("UPDATE public.donations SET payment_status = 'success', provider_payment_id = %s, "
                     "moderation_status = %s WHERE id = %s", (pid, moderation, did))
    return did, pid


def _evidence(did: str | None, pid: str, kind: str, ref: str, status: str, amount: int = 10000, *,
              currency="INR", deducted=None, match="payment", at: int = 0, phase="chargeback") -> None:
    """One recorded evidence event (as record_integrity writes it, hyperchat_app); `at` orders same-rank states."""
    event_type = {"processed": "refund.processed", "failed": "refund.failed", "pending": "refund.created",
                  "lost": "payment.dispute.lost", "won": "payment.dispute.won", "open": "payment.dispute.created",
                  "closed": "payment.dispute.closed"}[status]
    with db() as conn:
        wid = conn.execute(
            "INSERT INTO public.payment_webhook_events (event_id, event_type, account, provider_payment_id, donation_id, "
            "outcome, received_at) VALUES (%s, %s, 'primary', %s, %s, 'integrity_recorded', now()) RETURNING id",
            ("authtest-acct-" + secrets.token_hex(6), event_type, pid, did)).fetchone()[0]
        conn.execute(
            "INSERT INTO public.payment_integrity_events (webhook_event_id, donation_id, match_type, account, kind, "
            "event_type, provider_ref, provider_payment_id, phase, status, amount_paise, currency, amount_deducted_paise, "
            "provider_event_at, consistent, received_at) VALUES (%s, %s, %s, 'primary', %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, to_timestamp(1800000000 + %s), true, now())",
            (wid, did, match if did else "none", kind, event_type, ref, pid, phase if kind == "dispute" else None,
             status, amount, currency, deducted, at))


def _settle(did: str) -> str | None:
    """The application path (PaymentRepository._settle_accounting, evaluation on) in one transaction."""
    async def go():
        repo = PaymentRepository(factory(), accounting=True)
        async with repo._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            return await repo._settle_accounting(cur, did)
    return run(go())


def _apply(did, reversed_paise, hold=None, flags=(), refs=(), role="hyperchat_app") -> str:
    with (fixture_db() if role is None else db(role)) as conn:
        return conn.execute(APPLY, (did, reversed_paise, hold, list(flags), list(refs))).fetchone()[0]


def _state(did: str) -> tuple:
    """(applied reversal, hold, flags, credited_inr) of one donation."""
    with db() as conn:
        rev = conn.execute("SELECT applied_reversed_paise, hold, flags FROM public.donation_reversals "
                           "WHERE donation_id = %s", (did,)).fetchone()
        credited = conn.execute("SELECT credited_inr FROM public.donations WHERE id = %s", (did,)).fetchone()[0]
    return (*(rev or (0, None, [])), credited)


def _total(sid: str, name: str = "Fan") -> Decimal | None:
    with db() as conn:
        row = conn.execute("SELECT total_amount FROM public.streamer_donator_totals WHERE streamer_id = %s AND "
                           "donator_name_lower = lower(%s)", (sid, name)).fetchone()
    return row[0] if row else None


def _ledger(did: str) -> list[tuple]:
    with fixture_db() as conn:
        return conn.execute(
            "SELECT cause, delta_inr, reversed_before_paise, reversed_after_paise, hold_before, hold_after, "
            "evidence_refs, txid FROM public.donation_credit_events WHERE donation_id = %s ORDER BY id", (did,)).fetchall()


def _ref(kind: str) -> str:
    return ("rfnd_TEST" if kind == "refund" else "disp_TEST") + secrets.token_hex(5)


# ── privileges ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["hyperchat_worker", "hyperchat_auth", "hyperchat_lifecycle", "hyperchat_stepup",
                                  "hyperchat_readonly"])
def test_only_the_api_role_may_call_the_accounting_function(role):
    did, _ = _paid(_creator())
    with pytest.raises(REFUSED):
        _apply(did, 0, role=role)
    assert _state(did) == (0, None, [], Decimal("100.00"))


@pytest.mark.parametrize("role", ["hyperchat_app", "hyperchat_worker"])
@pytest.mark.parametrize("sql", [
    "INSERT INTO public.donation_reversals (donation_id, captured_paise, applied_reversed_paise) "
    "VALUES (%(d)s, 10000, 10000)",
    "UPDATE public.donation_reversals SET applied_reversed_paise = 0 WHERE donation_id = %(d)s",
    "DELETE FROM public.donation_reversals WHERE donation_id = %(d)s",
    "UPDATE public.donation_credit_events SET hold_after = NULL WHERE donation_id = %(d)s",
    "DELETE FROM public.donation_credit_events WHERE donation_id = %(d)s"])
def test_runtime_roles_cannot_write_reversal_state_or_history(role, sql):
    sid = _creator()
    did, pid = _paid(sid)
    _apply(did, None, hold="H-CONFLICT")  # a row exists to target
    with db(role) as conn, pytest.raises(REFUSED):
        conn.execute(sql, {"d": did})
    assert _state(did) == (0, "H-CONFLICT", [], Decimal("100.00"))


def test_the_new_history_is_append_only_for_the_owner_too():
    did, _ = _paid(_creator())
    _apply(did, None, hold="H-CURRENCY")
    for sql in ("UPDATE public.donation_credit_events SET hold_after = NULL WHERE donation_id = %s",
                "DELETE FROM public.donation_credit_events WHERE donation_id = %s"):
        with fixture_db() as conn, pytest.raises(REFUSED):
            conn.execute(sql, (did,))
    assert [r[0] for r in _ledger(did)] == ["writer", "hold"]


# ── the amount is re-derived from the stored evidence, never trusted ────────────

def _bad_calls(sid):
    did, pid = _paid(sid)
    other, other_pid = _paid(sid)
    pending, processed, other_ref, dup_ref = (_ref("refund") for _ in range(4))
    _evidence(did, pid, "refund", pending, "pending", 2000)
    _evidence(did, pid, "refund", processed, "processed", 2500)
    _evidence(other, other_pid, "refund", other_ref, "processed", 2500)                    # another donation
    _evidence(did, "pay_TESTdup" + secrets.token_hex(4), "refund", dup_ref, "processed", 2500, match="order")  # 2nd capture
    return did, processed, [
        (REFUSED, (2500, None, (), ())),                       # an amount with no evidence
        (REFUSED, (2000, None, (), (pending,))),               # a refund that is not processed
        (REFUSED, (2500, None, (), (other_ref,))),             # another donation's refund
        (REFUSED, (2500, None, (), (dup_ref,))),               # another payment of the order
        (REFUSED, (5000, None, (), (processed,))),             # more than the named evidence supports
        (REFUSED, (1000, None, (), (processed,))),             # less than it supports
        (REFUSED, (0, None, (), (processed,))),                # naming evidence it does not apply
        (REFUSED, (0, None, (), ())),                          # a clear 0 while counted evidence exists
        (INVALID, (2500, None, (), (processed, processed))),   # a duplicate reference
        (INVALID, (-1, None, (), ())),
        (INVALID, (10001, None, (), ())),                      # above the captured amount
        (INVALID, (2500, "H-CONFLICT", (), ())),               # both an amount and a hold
        (INVALID, (None, None, (), ())),                       # neither
        (INVALID, (None, "H-WHATEVER", (), ())),
        (INVALID, (None, "H-CONFLICT", (), (processed,))),     # a hold names no evidence
        (INVALID, (0, None, ("F-UNKNOWN",), ())),
    ]


def test_bad_calls_are_refused_and_change_nothing():
    sid = _creator()
    did, processed, calls = _bad_calls(sid)
    before = (_state(did), _total(sid), _ledger(did))
    for error, args in calls:
        with pytest.raises(error):
            _apply(did, *args)
    assert (_state(did), _total(sid), _ledger(did)) == before
    assert _apply(did, 2500, refs=(processed,)) == "evaluation"  # the one call the evidence supports


@pytest.mark.parametrize("kind, kw", [("refund", {"currency": "USD"}), ("dispute", {"deducted": 0}),
                                      ("dispute", {"currency": "USD", "deducted": 2500})])
def test_a_clear_result_is_refused_while_an_item_must_be_held(kind, kw):
    """A processed refund in another currency, a loss without a deduction, a non-INR loss: hold only (D3, D4)."""
    sid = _creator()
    did, pid = _paid(sid)
    good, bad = _ref("refund"), _ref(kind)
    _evidence(did, pid, "refund", good, "processed", 2500)
    _evidence(did, pid, kind, bad, "processed" if kind == "refund" else "lost", 2500, **kw)
    for args in ((2500, None, (), (good,)), (0, None, (), ()), (5000, None, (), (good, bad))):
        with pytest.raises(REFUSED):
            _apply(did, *args)
    assert _apply(did, None, hold="H-CURRENCY") == "hold" and _state(did) == (0, "H-CURRENCY", [], Decimal("100.00"))


def test_the_one_clear_call_the_evidence_supports_is_accepted():
    sid = _creator()
    did, pid = _paid(sid)
    a, b, pending = _ref("refund"), _ref("refund"), _ref("refund")
    _evidence(did, pid, "refund", a, "processed", 1000)
    _evidence(did, pid, "refund", b, "processed", 1500)
    _evidence(did, pid, "refund", pending, "pending", 9000)
    with pytest.raises(REFUSED):  # naming only part of the counted evidence
        _apply(did, 1000, refs=(a,))
    assert _apply(did, 2500, refs=(b, a)) == "evaluation" and _state(did)[:2] == (2500, None)


@pytest.mark.parametrize("provenance", ["imported", "none", "both"])
def test_only_v2_checkout_donations_are_ever_reversed(provenance):
    """PAY15-D8 and the reconciliation provenance rule: imported rows and ambiguous ones (no legacy marker but no
    checkout account / order, or both markers) are refused by the database and skipped by the application, even
    with clear evidence."""
    sid = _creator()
    did, pid = _paid(sid, provenance=provenance)
    ref = _ref("refund")
    _evidence(did, pid, "refund", ref, "processed", 2500)
    for args in ((2500, None, (), (ref,)), (None, "H-CONFLICT", (), ()), (0, None, (), ())):
        with pytest.raises(REFUSED):
            _apply(did, *args)
    assert _settle(did) is None  # the application path never calls the function for it
    assert _state(did) == (0, None, [], Decimal("100.00")) and _total(sid) == Decimal("100.00")


def test_an_unpaid_v2_donation_has_nothing_to_reverse():
    sid = _creator()
    unpaid = make_donation(sid, 100, payment="pending", moderation="pending", name="Fan")
    with fixture_db() as conn:
        conn.execute("UPDATE public.donations SET payment_account = 'primary', provider_order_id = %s WHERE id = %s",
                     ("order_TESTacct" + secrets.token_hex(5), unpaid))
    with pytest.raises(INVALID):
        _apply(unpaid, 2500)
    assert _apply(unpaid, 0) == "unchanged" and _settle(unpaid) == "unchanged"
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.donation_reversals WHERE donation_id = %s",
                            (unpaid,)).fetchone()[0] == 0


# ── attribution (as the evaluator): payment-matched evidence of another payment conflicts; order-matched is ignored ──

def test_payment_matched_evidence_naming_another_payment_prevents_a_clear_reversal():
    sid = _creator()
    did, pid = _paid(sid)
    good, alien = _ref("refund"), _ref("refund")
    _evidence(did, pid, "refund", good, "processed", 2500)
    _evidence(did, "pay_TESTalien" + secrets.token_hex(4), "refund", alien, "processed", 2500, match="payment")
    for args in ((2500, None, (), (good,)), (0, None, (), ()), (5000, None, (), (good, alien))):
        with pytest.raises(REFUSED):
            _apply(did, *args)
    assert _settle(did) == "hold"  # the evaluator holds it too (H-CONFLICT)
    assert _state(did) == (0, "H-CONFLICT", [], Decimal("100.00")) and _total(sid) == Decimal("100.00")


def test_order_matched_evidence_of_another_payment_is_never_counted():
    """A second payment of the same order (duplicate capture): its refund is not this donation's money."""
    sid = _creator()
    did, pid = _paid(sid)
    good, other = _ref("refund"), _ref("refund")
    _evidence(did, "pay_TESTsecond" + secrets.token_hex(4), "refund", other, "processed", 9000, match="order")
    assert _settle(did) == "unchanged" and _state(did)[:2] == (0, None)
    _evidence(did, pid, "refund", good, "processed", 2500)
    with pytest.raises(REFUSED):  # naming the other payment's refund is refused
        _apply(did, 9000, refs=(other,))
    assert _settle(did) == "evaluation"
    assert _state(did) == (2500, None, [], Decimal("75.00")) and _total(sid) == Decimal("75.00")


# ── money: pro rata, exact restoration, max not sum, freeze ────────────────────

def test_a_lost_then_won_dispute_moves_the_credit_and_restores_it_exactly():
    """F8. The win comes at a later phase (pre-arbitration): within one phase PAY-13's current-state view marks a
    lost and a won row as conflicting, which is a hold (next test)."""
    sid = _creator()
    did, pid = _paid(sid)
    ref = _ref("dispute")
    _evidence(did, pid, "dispute", ref, "lost", deducted=2500, at=1)
    assert _apply(did, 2500, refs=(ref,)) == "evaluation"
    assert _state(did) == (2500, None, [], Decimal("75.00")) and _total(sid) == Decimal("75.00")
    assert _apply(did, 2500, refs=(ref,)) == "unchanged"  # replay: nothing written
    _evidence(did, pid, "dispute", ref, "won", deducted=2500, at=2, phase="pre_arbitration")
    assert _apply(did, 0) == "evaluation"
    assert _state(did) == (0, None, [], Decimal("100.00")) and _total(sid) == Decimal("100.00")
    rows = _ledger(did)
    assert [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows] == [
        ("writer", Decimal("100.00"), 0, 0, None, None),                       # the completion
        ("evaluation", Decimal("0.00"), 0, 2500, None, None), ("writer", Decimal("-25.00"), 2500, 2500, None, None),
        ("evaluation", Decimal("0.00"), 2500, 0, None, None), ("writer", Decimal("25.00"), 0, 0, None, None)]
    assert rows[1][7] == rows[2][7] and rows[3][7] == rows[4][7] and rows[1][7] != rows[3][7]  # paired by txid
    assert rows[1][6] == [ref] and rows[3][6] == []


def test_a_win_in_the_same_phase_as_the_loss_is_conflicting_and_held():
    """PAY-13 semantics (0029 view): lost and won in one phase are 'conflicting'; the evaluator holds (H-CONFLICT)
    and the database refuses any clear result, so the applied reversal stays frozen until resolved (stage 6)."""
    sid = _creator()
    did, pid = _paid(sid)
    ref = _ref("dispute")
    _evidence(did, pid, "dispute", ref, "lost", deducted=2500, at=1)
    _apply(did, 2500, refs=(ref,))
    _evidence(did, pid, "dispute", ref, "won", deducted=2500, at=2)
    for args in ((0, None, (), ()), (2500, None, (), (ref,))):
        with pytest.raises(REFUSED):
            _apply(did, *args)
    assert _apply(did, None, hold="H-CONFLICT") == "hold"
    assert _state(did) == (2500, "H-CONFLICT", [], Decimal("75.00"))


@pytest.mark.parametrize("amount, refund, credit", [(100, 3333, "66.67"), (99.99, 1, "99.98"), (250, 25000, "0.00")])
def test_a_partial_refund_is_pro_rata_rounded_down_on_the_reversal(amount, refund, credit):
    sid = _creator()
    did, pid = _paid(sid, amount)
    ref = _ref("refund")
    _evidence(did, pid, "refund", ref, "processed", refund)
    _apply(did, refund, refs=(ref,))
    assert _state(did)[3] == Decimal(credit) and _total(sid) == Decimal(credit)


def test_a_refund_and_a_lost_dispute_apply_the_larger_never_the_sum():
    sid = _creator()
    did, pid = _paid(sid)
    refund, lost = _ref("refund"), _ref("dispute")
    _evidence(did, pid, "refund", refund, "processed", 3000)
    _evidence(did, pid, "dispute", lost, "lost", deducted=10000)
    with pytest.raises(INVALID):  # the sum exceeds the captured 10000: refused by the bounds check first
        _apply(did, 13000, flags=("F-BOTH",), refs=(refund, lost))
    assert _apply(did, 10000, flags=("F-BOTH",), refs=(refund, lost)) == "evaluation"
    assert _state(did) == (10000, None, ["F-BOTH"], Decimal("0.00")) and _total(sid) == Decimal("0.00")


def test_overlapping_partial_refund_and_loss_apply_the_larger_below_the_captured_amount():
    """F11 on its own: refund 3000 and loss 4000 of a 10000 capture. Their sum (7000) is within the captured amount,
    so only the evidence check can refuse it; the larger (4000) applies once, flagged F-BOTH; a replay writes nothing."""
    sid = _creator()
    did, pid = _paid(sid)
    refund, lost = _ref("refund"), _ref("dispute")
    _evidence(did, pid, "refund", refund, "processed", 3000)
    _evidence(did, pid, "dispute", lost, "lost", deducted=4000)
    with pytest.raises(REFUSED):  # the evidence control, not the captured-amount bound
        _apply(did, 7000, flags=("F-BOTH",), refs=(refund, lost))
    assert _state(did) == (0, None, [], Decimal("100.00")) and len(_ledger(did)) == 1
    assert _apply(did, 4000, flags=("F-BOTH",), refs=(refund, lost)) == "evaluation"
    assert _state(did) == (4000, None, ["F-BOTH"], Decimal("60.00")) and _total(sid) == Decimal("60.00")
    history = _ledger(did)
    assert [(r[0], r[1], r[2], r[3]) for r in history] == [
        ("writer", Decimal("100.00"), 0, 0), ("evaluation", Decimal("0.00"), 0, 4000),
        ("writer", Decimal("-40.00"), 4000, 4000)]
    assert sorted(history[1][6]) == sorted([refund, lost]) and history[1][7] == history[2][7]
    assert sum(r[1] for r in history) == _state(did)[3] == _total(sid)
    assert _apply(did, 4000, flags=("F-BOTH",), refs=(refund, lost)) == "unchanged"
    assert _ledger(did) == history and _total(sid) == Decimal("60.00")


def test_a_hold_freezes_the_reversal_and_moderation_still_moves_the_frozen_credit():
    """Worked example C (F10): refund 25 of 100 applied, then a conflicting dispute holds the donation."""
    sid = _creator()
    did, pid = _paid(sid)
    ref = _ref("refund")
    _evidence(did, pid, "refund", ref, "processed", 2500)
    _apply(did, 2500, refs=(ref,))
    assert _apply(did, None, hold="H-CONFLICT") == "hold"
    assert _apply(did, None, hold="H-CONFLICT") == "unchanged"         # the same hold again: nothing written
    assert _apply(did, None, hold="H-OVER-REFUND") == "hold"           # another reason: recorded, still frozen
    assert _state(did) == (2500, "H-OVER-REFUND", [], Decimal("75.00"))
    with db() as conn:
        conn.execute("UPDATE public.donations SET moderation_status = 'rejected' WHERE id = %s", (did,))
    assert _state(did)[3] == Decimal("0.00") and _total(sid) == Decimal("0.00")
    with db() as conn:
        conn.execute("UPDATE public.donations SET moderation_status = 'approved' WHERE id = %s", (did,))
    assert _state(did)[3] == Decimal("75.00") and _total(sid) == Decimal("75.00")
    assert _apply(did, 2500, refs=(ref,)) == "resolved"                # clear again: the hold is lifted once
    assert _state(did) == (2500, None, [], Decimal("75.00"))
    holds = [(r[0], r[2], r[3], r[4], r[5], r[6]) for r in _ledger(did) if r[0] != "writer"]
    assert holds == [("evaluation", 0, 2500, None, None, [ref]), ("hold", 2500, 2500, None, "H-CONFLICT", None),
                     ("hold", 2500, 2500, "H-CONFLICT", "H-OVER-REFUND", None),
                     ("resolved", 2500, 2500, "H-OVER-REFUND", None, [ref])]


def test_flags_alone_are_state_not_history():
    did, _ = _paid(_creator())
    assert _apply(did, 0, flags=("F-OPEN",)) == "unchanged"
    assert _state(did)[:3] == (0, None, ["F-OPEN"]) and [r[0] for r in _ledger(did)] == ["writer"]


def test_a_short_backfilled_row_reverses_only_its_ceiling():
    """F16: a row the 0031 backfill credited 50 of 100 (its ceiling): a full refund removes 50, never 100."""
    sid = _creator()
    whole, _ = _paid(sid)
    pid = "pay_TESTshort" + secrets.token_hex(4)
    with fixture_db() as conn, conn.transaction():  # as donations_credit_backfill() leaves a short row (audited)
        conn.execute("ALTER TABLE public.donations DISABLE TRIGGER donations_credit_writer")
        short = conn.execute(
            "INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, amount_inr, "
            "payment_status, moderation_status, provider_payment_id, payment_account, provider_order_id, creditable_inr, "
            "credited_inr) VALUES (%s, %s, 'Fan', 100, 'INR', 100, 'success', 'approved', %s, 'primary', %s, 50, 50) "
            "RETURNING id::text",
            (sid, "authtest-" + secrets.token_hex(6), pid, "order_TESTshort" + secrets.token_hex(4))).fetchone()[0]
        conn.execute("ALTER TABLE public.donations ENABLE TRIGGER donations_credit_writer")
        conn.execute("INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause, "
                     "payment_status, moderation_status, credited_before, credited_after, delta_inr, delta_count, actor) "
                     "VALUES (%s, %s, 'fan', 'backfill', 'success', 'approved', 0, 50, 50, 0, session_user)", (short, sid))
        conn.execute("UPDATE public.streamer_donator_totals SET total_amount = 150 WHERE streamer_id = %s", (sid,))
    ref = _ref("refund")
    _evidence(short, pid, "refund", ref, "processed", 10000)
    _apply(short, 10000, refs=(ref,))
    assert _state(short)[3] == Decimal("0.00") and _total(sid) == Decimal("100.00") and _state(whole)[3] == 100


def test_random_evidence_sequences_keep_every_invariant():
    """F1, F2, F3, F7, F8, F15 over random clear / held evaluations and moderation, against the stored truth."""
    r = random.Random(20261115)
    sid = _creator()
    did, pid = _paid(sid, 100)
    refs = [_ref("refund") for _ in range(3)]
    amounts = [r.choice([1, 2500, 3333, 4000]) for _ in refs]
    for ref, amount in zip(refs, amounts):
        _evidence(did, pid, "refund", ref, "pending", amount)
    processed: set[str] = set()
    for step in range(40):
        move = r.choice(["process", "evaluate", "evaluate", "hold", "moderate"])
        before_total = _total(sid)
        if move == "process" and len(processed) < len(refs):
            ref = r.choice([x for x in refs if x not in processed])
            _evidence(did, pid, "refund", ref, "processed", amounts[refs.index(ref)])
            processed.add(ref)
        elif move == "evaluate":
            want = sum(amounts[refs.index(x)] for x in processed)
            if want > 10000:
                _apply(did, None, hold="H-OVER-REFUND")
            else:
                _apply(did, want, refs=sorted(processed))
                assert _apply(did, want, refs=sorted(processed)) == "unchanged"  # F2
            if _total(sid) is not None and before_total is not None:
                assert _total(sid) <= before_total  # F15: refunds only ever remove money here
        elif move == "hold":
            _apply(did, None, hold=r.choice(["H-CONFLICT", "H-CURRENCY"]))
        else:
            with db() as conn:
                conn.execute("UPDATE public.donations SET moderation_status = %s WHERE id = %s",
                             (r.choice(["approved", "rejected"]), did))
        rev, hold, _, credited = _state(did)
        with db() as conn:
            mod = conn.execute("SELECT moderation_status FROM public.donations WHERE id = %s", (did,)).fetchone()[0]
        target = Decimal(0) if mod != "approved" else Decimal(100) - (Decimal(100 * 100 * rev // 10000) / 100)
        assert 0 <= rev <= 10000 and credited == target                                   # F1, F3
        rows = _ledger(did)
        assert sum(x[1] for x in rows) == credited == (_total(sid) or 0)                   # F7 / F4 (one donation)
        for x in rows:
            assert (x[0] == "hold") <= (x[2] == x[3])                                       # holds freeze
            assert x[0] != "writer" or x[2] == x[3]                                          # writers never move it


# ── end to end: the webhook route, the repository and concurrency ───────────────

def _accounting_on(client) -> None:
    client.app_state.payments.repo.accounting = True  # injected: the setting itself refuses to be enabled


def test_with_the_switch_off_evidence_changes_no_money_and_writes_no_accounting(client):
    sid, out, payment = paid(client, "authtest-acct-off")
    raw = integrity_body("refund.processed", ref="rfnd_TESToff1", payment_id=payment.id,
                         order_id=out["razorpay_order_id"], amount=2500)
    assert post(client, raw, "authtest-acct-off-1").json() == {"status": "integrity_recorded"}
    with db() as conn:
        did = conn.execute("SELECT id::text FROM public.donations WHERE order_id = %s", (out["order_id"],)).fetchone()[0]
    assert _state(did) == (0, None, [], Decimal("100.00")) and totals(sid) == [("Ravi", Decimal("100.00"), 1)]
    assert [r[0] for r in _ledger(did)] == ["writer"]


def test_a_processed_refund_webhook_reduces_the_donor_total_once(client):
    _accounting_on(client)
    sid, out, payment = paid(client, "authtest-acct-e2e")
    raw = integrity_body("refund.processed", ref="rfnd_TESTe2e1", payment_id=payment.id,
                         order_id=out["razorpay_order_id"], amount=2500)
    assert post(client, raw, "authtest-acct-e2e-1").json() == {"status": "integrity_recorded"}
    assert post(client, raw, "authtest-acct-e2e-1").json() == {"status": "duplicate"}
    with db() as conn:
        did = conn.execute("SELECT id::text FROM public.donations WHERE order_id = %s", (out["order_id"],)).fetchone()[0]
    assert _state(did) == (2500, None, [], Decimal("75.00")) and totals(sid) == [("Ravi", Decimal("75.00"), 1)]
    lost = integrity_body("payment.dispute.lost", ref="disp_TESTe2e1", payment_id=payment.id,
                          order_id=out["razorpay_order_id"], amount=10000, amount_deducted=10000, currency="INR")
    assert post(client, lost, "authtest-acct-e2e-2").json() == {"status": "integrity_recorded"}
    assert _state(did) == (10000, None, ["F-BOTH"], Decimal("0.00"))  # max(25, 100), never the sum


def test_a_lost_dispute_without_a_deduction_is_held(client):
    _accounting_on(client)
    sid, out, payment = paid(client, "authtest-acct-nod")
    raw = integrity_body("payment.dispute.lost", ref="disp_TESTnod1", payment_id=payment.id,
                         order_id=out["razorpay_order_id"], amount_deducted=0)
    assert post(client, raw, "authtest-acct-nod-1").json() == {"status": "integrity_recorded"}
    with db() as conn:
        did = conn.execute("SELECT id::text FROM public.donations WHERE order_id = %s", (out["order_id"],)).fetchone()[0]
    assert _state(did) == (0, "H-NO-DEDUCTION", [], Decimal("100.00")) and totals(sid) == [("Ravi", Decimal("100.00"), 1)]


def _concurrently(client, requests):
    """The real app in its own loop and pool (as test_integrity_db), evaluation on, requests in parallel."""
    async def scenario():
        from app.main import create_app

        application = create_app()
        async with application.router.lifespan_context(application):
            wire(application.state, client.fake, InMemoryObjectStore())
            application.state.payments.repo.accounting = True
            transport = httpx.ASGITransport(app=application, client=("203.0.113.9", 4000))
            async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
                return await asyncio.gather(*(c.post(W, content=raw, headers={
                    "x-razorpay-signature": sign(client.env["HYPERCHAT_RAZORPAY_PRIMARY_WEBHOOK_SECRET"], raw),
                    "x-razorpay-event-id": event_id, "content-type": "application/json"}) for raw, event_id in requests))
    return asyncio.run(scenario())


def test_concurrent_refunds_for_one_payment_all_count_once(client):
    """F2: three refund deliveries and a replay in parallel; whichever evaluation commits last sees all evidence."""
    sid, out, payment = paid(client, "authtest-acct-conc")
    bodies = [(integrity_body("refund.processed", ref=f"rfnd_TESTconc{n}", payment_id=payment.id,
                              order_id=out["razorpay_order_id"], amount=1000 * (n + 1)), f"authtest-acct-conc-{n}")
              for n in range(3)]
    results = _concurrently(client, bodies + [bodies[0]])
    assert sorted(r.json()["status"] for r in results) == ["duplicate"] + ["integrity_recorded"] * 3
    with db() as conn:
        did = conn.execute("SELECT id::text FROM public.donations WHERE order_id = %s", (out["order_id"],)).fetchone()[0]
    assert _state(did) == (6000, None, [], Decimal("40.00")) and totals(sid) == [("Ravi", Decimal("40.00"), 1)]
    assert sum(r[1] for r in _ledger(did)) == Decimal("40.00")


def test_a_refund_racing_the_capture_is_applied_once_whichever_commits_first(client):
    """Evidence may arrive before the capture (stored order-matched): the capture re-evaluates it."""
    _accounting_on(client)
    sid = make_streamer("authtest-acct-race")
    out = checkout(client, "authtest-acct-race")
    payment = client.fake.captured(out["razorpay_order_id"], 10000)
    refund = integrity_body("refund.processed", ref="rfnd_TESTrace1", payment_id=payment.id,
                            order_id=out["razorpay_order_id"], amount=4000)
    from tests.payments.fakes import webhook_body
    capture = webhook_body("payment.captured", out["razorpay_order_id"], payment.id, 10000, "INR", "captured")
    results = _concurrently(client, [(refund, "authtest-acct-race-1"), (capture, "authtest-acct-race-2")])
    assert {r.json()["status"] for r in results} <= {"integrity_recorded", "integrity_mismatch", "completed"}
    with db() as conn:
        did = conn.execute("SELECT id::text FROM public.donations WHERE order_id = %s", (out["order_id"],)).fetchone()[0]
    assert _state(did) == (4000, None, [], Decimal("60.00")) and totals(sid) == [("Ravi", Decimal("60.00"), 1)]


def test_a_moderation_racing_an_evaluation_serialises_on_the_donation():
    sid = _creator()
    did, pid = _paid(sid)
    ref = _ref("refund")
    _evidence(did, pid, "refund", ref, "processed", 5000)
    holder = db()
    holder.autocommit = False
    try:
        holder.execute(APPLY, (did, 5000, None, [], [ref]))  # holds the donation lock, total not yet committed
        done = threading.Event()

        def reject():
            with db() as conn:
                conn.execute("UPDATE public.donations SET moderation_status = 'rejected' WHERE id = %s", (did,))
            done.set()
        worker = threading.Thread(target=reject)
        worker.start()
        assert not done.wait(0.5)
        holder.commit()
        worker.join(30)
        assert done.is_set()
    finally:
        holder.close()
    assert _state(did) == (5000, None, [], Decimal("0.00")) and _total(sid) == Decimal("0.00")
    assert sum(r[1] for r in _ledger(did)) == 0


# ── checkout provenance is fixed for runtime roles (0032) ──────────────────────

def _provenance(did: str) -> tuple:
    with db() as conn:
        return conn.execute(f"SELECT d.payment_account, d.provider_order_id, d.legacy_source_table, ({V2_PROVENANCE}), "
                            "d.credited_inr FROM public.donations d WHERE d.id = %s", (did,)).fetchone()


def _in_state(sid: str, state: str) -> str:
    if state == "pending":  # a V2 checkout donation awaiting payment
        did = make_donation(sid, 100, payment="pending", moderation="pending", name="Fan")
        with fixture_db() as conn:
            conn.execute("UPDATE public.donations SET payment_account = 'primary', provider_order_id = %s WHERE id = %s",
                         ("order_TESTprov" + secrets.token_hex(5), did))
        return did
    return _paid(sid, moderation="rejected" if state == "rejected" else "approved",
                 provenance="none" if state == "ambiguous" else "v2")[0]


@pytest.mark.parametrize("role", ["hyperchat_app", "hyperchat_worker"])
@pytest.mark.parametrize("state", ["pending", "paid", "rejected", "ambiguous"])
def test_runtime_roles_never_change_the_checkout_provenance(role, state):
    sid = _creator()
    did = _in_state(sid, state)
    before = (_provenance(did), _total(sid))
    account, order = before[0][:2]
    changes = [{"payment_account": None if account else "primary"},             # value -> NULL, or NULL -> value
               {"provider_order_id": None if order else "order_TESTnew" + secrets.token_hex(4)},
               {"provider_order_id": "order_TESTother" + secrets.token_hex(4)}]  # to another value
    if account:
        changes.append({"payment_account": "secondary"})                       # refused before any CHECK runs
    for change in changes:
        (col, value), = change.items()
        with db(role) as conn, pytest.raises(REFUSED):
            conn.execute(f"UPDATE public.donations SET {col} = %s WHERE id = %s", (value, did))
    assert (_provenance(did), _total(sid)) == before


@pytest.mark.parametrize("role", ["hyperchat_app", "hyperchat_worker"])
def test_a_runtime_role_cannot_turn_an_ambiguous_donation_into_a_v2_one(role):
    sid = _creator()
    did, pid = _paid(sid, provenance="none")
    ref = _ref("refund")
    _evidence(did, pid, "refund", ref, "processed", 2500)
    with db(role) as conn, pytest.raises(REFUSED):
        conn.execute("UPDATE public.donations SET payment_account = 'primary', provider_order_id = %s WHERE id = %s",
                     ("order_TESTforged" + secrets.token_hex(4), did))
    assert _provenance(did)[:4] == (None, None, None, False)
    assert _settle(did) is None
    with pytest.raises(REFUSED):
        _apply(did, 2500, refs=(ref,))
    assert _state(did) == (0, None, [], Decimal("100.00")) and _total(sid) == Decimal("100.00")


def test_payment_completion_and_moderation_leave_the_provenance_untouched(client):
    """The legitimate runtime path: checkout INSERTs both fields, completion and moderation never change them."""
    sid, out, payment = paid(client, "authtest-acct-prov")
    with db() as conn:
        did = conn.execute("SELECT id::text FROM public.donations WHERE order_id = %s", (out["order_id"],)).fetchone()[0]
        conn.execute("UPDATE public.donations SET moderation_status = 'rejected' WHERE id = %s", (did,))
        conn.execute("UPDATE public.donations SET moderation_status = 'approved' WHERE id = %s", (did,))
    account, order, legacy, v2, credited = _provenance(did)
    assert (account, order, legacy, v2, credited) == ("primary", out["razorpay_order_id"], None, True, Decimal("100.00"))
