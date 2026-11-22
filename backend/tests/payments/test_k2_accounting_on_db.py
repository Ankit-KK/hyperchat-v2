"""PAY-15 K2 with accounting ON (amendment B stage A6k-CI; OD-13 Alternative 1; OD-14 CI venue).

The dispute webhook labels its evidence with two separate lookups (integrity_target: by payment id, then by order)
and records it in its own transaction (record_integrity); the capture webhook completes the donation in one
transaction (apply). K2 r3 (package 6b16958f) inferred from the code, without execution, that with accounting on
no interleaving can mis-apply money: attribution is by donation_id + provider_payment_id at evaluation time, never by
the stored match_type (OD-13 Alternative 1: the label is provenance; ('order', True) is a legitimate stored value),
and both transactions serialise on the donation row. These tests check that inference on PostgreSQL:

  R-1 / R-2  each commit order FORCED (S1 dispute first, S2 capture first, S3 the K2 interleaving between the two
             lookups, S4 a label taken before the capture with the record after it), for a lost dispute in dispute
             mode apply and in mode hold: the exact reversal / hold, exactly one accounting ledger row, the evidence
             attributed to this donation and payment, no effect on another donation, and the same end state as the
             sequential executions.
  R-4        a duplicate delivery of the racing dispute is a duplicate and changes nothing.
  repeated   the unforced race, ITERATIONS rounds bounded by ROUND_SECONDS (as the 6a-1 C13 tests).

R-3 (pre-capture evidence, then activation through the 6a-3 reconcile) needs the 6a-3 CLI, which does not exist
yet: out of scope here. Schedules are forced with asyncio events around the repository methods of the real app (no
production code changes); a schedule that did not occur fails the test (never a pass)."""

import asyncio
import contextvars
import secrets
import time
from decimal import Decimal

import httpx
import pytest

from app.storage.memory import InMemoryObjectStore
from tests.integration import db, fixture_db, make_streamer, requires_db, set_accounting
from tests.payments.fakes import integrity_body, sign, webhook_body
from tests.payments.test_payments_db import (  # noqa: F401 (fixtures)
    W,
    checkout,
    client,
    rz_env,
    totals,
    webhook,
    wire,
)

pytestmark = [requires_db, pytest.mark.usefixtures("env", "accounting_active")]  # 0034 state active, mode apply

DEDUCTED = 7000                  # paise; the donation is ₹100 (10000 paise)
ITERATIONS = 20                  # repeated unforced rounds
ROUND_SECONDS = 120              # the repeated variant stops early once this budget is spent
WAIT = 15                        # seconds a forced schedule may wait for its other side before failing
LABEL_OF = {"S1": ("order", False), "S2": ("payment", True), "S3": ("order", True), "S4": ("order", False)}
_in_target = contextvars.ContextVar("k2_in_integrity_target", default=False)


class ScheduleNotMet(AssertionError):
    pass


def _instrument(repo, schedule: str, log: list):
    """Wrap the repository methods of the app under test so the two webhooks interleave as `schedule` says."""
    cap_done, d_done = asyncio.Event(), asyncio.Event()
    apply_, target_, record_, by_order_ = repo.apply, repo.integrity_target, repo.record_integrity, \
        repo.by_provider_order

    async def wait(event, what):
        try:
            await asyncio.wait_for(event.wait(), WAIT)
        except TimeoutError:
            raise ScheduleNotMet(f"{schedule}: {what} never happened") from None

    async def apply(*a, **kw):
        if schedule == "S1":
            await wait(d_done, "the dispute commit")
        result = await apply_(*a, **kw)
        log.append("C committed")
        cap_done.set()
        return result

    async def integrity_target(*a, **kw):
        if schedule == "S2":
            await wait(cap_done, "the capture commit")
        token = _in_target.set(True)
        try:
            log.append("D L1")
            return await target_(*a, **kw)
        finally:
            _in_target.reset(token)

    async def by_provider_order(*a, **kw):
        if _in_target.get():
            if schedule == "S3":
                await wait(cap_done, "the capture commit between L1 and L2")
            log.append("D L2")
        return await by_order_(*a, **kw)

    async def record_integrity(*a, **kw):
        if schedule == "S4":
            await wait(cap_done, "the capture commit after the label")
        result = await record_(*a, **kw)
        log.append("D committed")
        d_done.set()
        return result

    repo.apply, repo.integrity_target, repo.record_integrity, repo.by_provider_order = \
        apply, integrity_target, record_integrity, by_provider_order


def _post_all(client, requests, schedule: str | None = None, log: list | None = None):
    """The real app in its own loop and pool, accounting on, the requests in parallel (as test_accounting_db)."""
    async def scenario():
        from app.main import create_app

        application = create_app()
        async with application.router.lifespan_context(application):
            wire(application.state, client.fake, InMemoryObjectStore())
            repo = application.state.payments.repo
            repo.accounting = True
            if schedule:
                _instrument(repo, schedule, log)
            transport = httpx.ASGITransport(app=application, client=("203.0.113.9", 4000))
            async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
                return await asyncio.gather(*(c.post(W, content=raw, headers={
                    "x-razorpay-signature": sign(client.env["HYPERCHAT_RAZORPAY_PRIMARY_WEBHOOK_SECRET"], raw),
                    "x-razorpay-event-id": event_id, "content-type": "application/json"}) for raw, event_id in requests))
    return asyncio.run(scenario())


def _order(client, slug):
    out = checkout(client, slug)
    payment = client.fake.captured(out["razorpay_order_id"], 10000)
    with db() as conn:
        did = conn.execute("SELECT id::text FROM public.donations WHERE order_id = %s", (out["order_id"],)).fetchone()[0]
    return out, payment, did


def _bodies(out, payment, tag):
    dispute = (integrity_body("payment.dispute.lost", ref="disp_TESTk2" + secrets.token_hex(5), payment_id=payment.id,
                              order_id=out["razorpay_order_id"], amount_deducted=DEDUCTED), f"authtest-k2on-{tag}-d")
    capture = (webhook_body("payment.captured", out["razorpay_order_id"], payment.id, 10000, "INR", "captured"),
               f"authtest-k2on-{tag}-c")
    return dispute, capture


def _state(did):
    """(applied reversal, hold, flags, credited_inr): the money outcome of one donation."""
    with db() as conn:
        rev = conn.execute("SELECT applied_reversed_paise, hold, flags FROM public.donation_reversals "
                           "WHERE donation_id = %s", (did,)).fetchone()
        credited = conn.execute("SELECT credited_inr FROM public.donations WHERE id = %s", (did,)).fetchone()[0]
    return (*(rev or (0, None, [])), credited)


def _ledger(did):
    """The accounting ledger rows (donation_credit_events), with the evidence refs reduced to their count."""
    with fixture_db() as conn:
        rows = conn.execute("SELECT cause, delta_inr, reversed_before_paise, reversed_after_paise, hold_before, "
                            "hold_after, evidence_refs FROM public.donation_credit_events WHERE donation_id = %s "
                            "ORDER BY id", (did,)).fetchall()
    return [(*r[:6], len(r[6] or ())) for r in rows]


def _evidence(event_id):
    with db() as conn:
        return conn.execute(
            "SELECT i.donation_id::text, i.provider_payment_id, i.match_type, i.consistent, w.outcome "
            "FROM public.payment_webhook_events w JOIN public.payment_integrity_events i ON i.webhook_event_id = w.id "
            "WHERE w.event_id = %s", (event_id,)).fetchall()


def _sequential(client, slug, first: str):
    """The reference: the same two webhooks one after the other ("dispute" or "capture" first), no concurrency."""
    sid = make_streamer(slug)
    out, payment, did = _order(client, slug)
    dispute, capture = _bodies(out, payment, slug[-6:])
    for request in ((dispute, capture) if first == "dispute" else (capture, dispute)):
        _post_all(client, [request])
    return sid, did


def _check_race(client, schedule, *, mode):
    """One forced schedule against both sequential references; every money and attribution invariant."""
    if mode != "apply":
        set_accounting("active", mode)
    refs = [_sequential(client, f"authtest-k2on-{mode}-{first[0]}{secrets.token_hex(2)}", first)
            for first in ("dispute", "capture")]
    ref_states = {_state(did) for _, did in refs}
    ref_ledgers = {tuple(_ledger(did)) for _, did in refs}
    assert len(ref_states) == 1 and len(ref_ledgers) == 1, (ref_states, ref_ledgers)  # the two orders agree

    slug = f"authtest-k2on-{schedule.lower()}{secrets.token_hex(3)}"
    sid = make_streamer(slug)
    other_out, other_payment, other = _order(client, slug)  # a second, already-paid donation of the same creator
    assert webhook(client, "payment.captured", other_out, other_payment.id,
                   event_id=f"authtest-k2on-other-{secrets.token_hex(3)}").json() == {"status": "completed"}
    other_before = (_state(other), _ledger(other))
    out, payment, did = _order(client, slug)
    dispute, capture = _bodies(out, payment, slug[-6:])
    log: list = []
    d, c = _post_all(client, [dispute, capture], schedule, log)

    expected_log = {"S1": ["D L1", "D L2", "D committed", "C committed"],
                    "S2": ["C committed", "D L1"],  # L1 hits after the capture: no order lookup, then the record
                    "S3": ["D L1", "C committed", "D L2", "D committed"],
                    "S4": ["D L1", "D L2", "C committed", "D committed"]}[schedule]
    assert [e for e in log if e in expected_log] == expected_log, ("schedule not met", schedule, log)
    assert c.json() == {"status": "completed"}
    assert d.json()["status"] in ("integrity_recorded", "integrity_mismatch")

    (ev,) = _evidence(dispute[1])
    assert ev[0] == did and ev[1] == payment.id, ev                        # attributed to this donation and payment
    assert (ev[2], ev[3]) == LABEL_OF[schedule], (schedule, ev)            # OD-13 Alt 1: provenance, per schedule
    state, ledger = _state(did), _ledger(did)
    assert state in ref_states and tuple(ledger) in ref_ledgers, (schedule, state, ledger, ref_states, ref_ledgers)
    reversal_rows = [r for r in ledger if r[3] != r[2] or r[5] != r[4]]
    assert len(reversal_rows) == 1, (schedule, ledger)                     # exactly one accounting change, once
    assert (_state(other), _ledger(other)) == other_before                 # never the other donation
    return sid, did, state, reversal_rows[0], dispute


@pytest.mark.parametrize("schedule", ["S1", "S2", "S3", "S4"])
def test_k2_lost_dispute_racing_the_capture_applies_exactly_once_in_mode_apply(client, schedule):
    """R-1 / R-2, mode apply: the lost dispute's deduction is applied exactly once whatever the commit order."""
    sid, did, state, row, _ = _check_race(client, schedule, mode="apply")
    assert state == (DEDUCTED, None, [], Decimal("30.00")), state
    assert row[2:6] == (0, DEDUCTED, None, None) and row[1] == Decimal("-70.00") and row[6] == 1, row
    assert totals(sid) == [("Ravi", Decimal("130.00"), 2)]                # the other ₹100 + this ₹100 − ₹70


@pytest.mark.parametrize("schedule", ["S1", "S2", "S3", "S4"])
def test_k2_lost_dispute_racing_the_capture_is_held_exactly_once_in_mode_hold(client, schedule):
    """R-1 / R-2, mode hold (S6A-D3 refunds-only): the lost dispute is held, never deducted, whatever the order."""
    sid, did, state, row, _ = _check_race(client, schedule, mode="hold")
    assert state[0] == 0 and state[1] == "H-DISPUTE-MODE" and state[3] == Decimal("100.00"), state
    assert row[2:6] == (0, 0, None, "H-DISPUTE-MODE") and row[1] == 0, row
    assert totals(sid) == [("Ravi", Decimal("200.00"), 2)]


def test_k2_a_duplicate_of_the_racing_dispute_changes_nothing(client):
    """R-4: after the K2 interleaving (S3), the same dispute delivery again is a duplicate; state and ledger hold."""
    sid, did, state, _, dispute = _check_race(client, "S3", mode="apply")
    before = (_state(did), _ledger(did), totals(sid))
    (again,) = _post_all(client, [dispute])
    assert again.json() == {"status": "duplicate"}
    assert (_state(did), _ledger(did), totals(sid)) == before
    assert len(_evidence(dispute[1])) == 1


def test_k2_unforced_race_repeated_never_double_applies(client):
    """The unforced race, ITERATIONS rounds within ROUND_SECONDS: every round applies the deduction exactly once and
    attributes the evidence to its own donation; any stored label of OD-13 Alternative 1 is allowed."""
    started, labels, rounds = time.monotonic(), set(), 0
    for n in range(ITERATIONS):
        if time.monotonic() - started > ROUND_SECONDS:
            break
        slug = f"authtest-k2on-rep{n:02d}{secrets.token_hex(2)}"
        make_streamer(slug)
        out, payment, did = _order(client, slug)
        dispute, capture = _bodies(out, payment, slug[-6:])
        d, c = _post_all(client, [dispute, capture])
        assert c.json() == {"status": "completed"}, (n, c.json())
        (ev,) = _evidence(dispute[1])
        assert ev[0] == did and ev[1] == payment.id, (n, ev)
        assert (ev[2], ev[3]) in set(LABEL_OF.values()), (n, ev)
        labels.add((ev[2], ev[3]))
        assert _state(did) == (DEDUCTED, None, [], Decimal("30.00")), (n, _state(did))
        assert len([r for r in _ledger(did) if r[3] != r[2]]) == 1, (n, _ledger(did))
        rounds += 1
    assert rounds >= 3, rounds  # the time budget always allows a few rounds; zero rounds would prove nothing
