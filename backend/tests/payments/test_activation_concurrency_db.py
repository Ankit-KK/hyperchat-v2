"""PAY-15 (D17) stage 6a-1 on PostgreSQL: lock order L-1 under concurrency (design revision 2 §7, C1-C13), and the
refunds-only cases of §8 through the signed webhook route.

L-1: every transaction that may change accounting takes the accounting state row FOR SHARE first (the API through
public.payment_accounting_lock_state(), the database functions themselves), then the donation, the donor total, jobs
and the realtime outbox. A freeze takes the state row FOR UPDATE and nothing else. Moderation never takes it.

Every scenario runs N = 50 times (revision 2 §7), except C13 (N_TIMEOUT: each iteration necessarily waits out the
5 s lock timeout). Lock waits are observed in pg_locks (granted = false): pg_stat_activity hides other roles' wait
events from a non-superuser. Every thread join, round and lock wait has an explicit timeout. These tests show
behaviour on the PostgreSQL version CI runs; they do not prove deadlock freedom in general."""

import asyncio
import collections
import secrets
import threading
import time

import httpx
import psycopg
import pytest

from app.payments.repository import PaymentRepository
from app.storage.memory import InMemoryObjectStore
from tests.integration import db, fixture_db, make_donation, make_streamer, requires_db, set_accounting
from tests.jobs.test_jobs_db import factory
from tests.jobs.test_jobs_db import run as run_async
from tests.payments.fakes import integrity_body, sign, webhook_body
from tests.payments.test_accounting_db import (
    APPLY,
    _apply,
    _creator,
    _evidence,
    _ledger,
    _paid,
    _ref,
    _settle,
    _state,
    _total,
)
from tests.payments.test_activation_db import _evaluation, _fp, _keep, _reconcile, _revert, _run
from tests.payments.test_integrity_db import paid
from tests.payments.test_payments_db import (  # noqa: F401 (fixtures)
    W,
    aged,
    checkout,
    client,
    rz_env,
    totals,
    wire,
)

pytestmark = [requires_db, pytest.mark.usefixtures("env")]
N = 50          # revision 2 §7: every scenario repeated 50 times
N_TIMEOUT = 3   # C13: each iteration waits the full 5 s (50 would add ~250 s for a deterministic timeout)
JOIN_TIMEOUT = 60
ROUND_TIMEOUT = 60
DEADLOCK = "40P01"
SET_STATE = "SELECT public.payment_accounting_set_state(%s, NULL, 'op-test', 'a concurrency test change', NULL)"


@pytest.fixture(autouse=True)
def _active_then_frozen():
    set_accounting("active", "apply")
    yield
    set_accounting("frozen")


# ── helpers ─────────────────────────────────────────────────────────────────────

def _blocked(pid: int) -> bool:
    with fixture_db() as conn:
        return conn.execute("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted)", (pid,)).fetchone()[0]


def _wait_blocked(pid: int, timeout: float = 15) -> None:
    """Until the backend `pid` waits for a lock (pg_locks is readable by every role)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _blocked(pid):
            return
        time.sleep(0.02)
    raise AssertionError(f"backend {pid} never waited for a lock")


def _background(fn) -> tuple[threading.Thread, list]:
    out: list = []

    def go():
        try:
            out.append(("ok", fn()))
        except Exception as exc:  # noqa: BLE001 (the test inspects it)
            out.append(("error", exc))
    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    return thread, out


def _join(*threads) -> None:
    for t in threads:
        t.join(JOIN_TIMEOUT)
    assert not any(t.is_alive() for t in threads), "a concurrent call hung"


def _together(*fns) -> list:
    """Run fns at once (a barrier), each in its own thread; [("ok", result) | ("error", exception)]."""
    barrier = threading.Barrier(len(fns), timeout=JOIN_TIMEOUT)
    results: list = [None] * len(fns)

    def wrap(i, fn):
        barrier.wait()
        try:
            results[i] = ("ok", fn())
        except Exception as exc:  # noqa: BLE001
            results[i] = ("error", exc)
    threads = [threading.Thread(target=wrap, args=(i, fn), daemon=True) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    _join(*threads)
    return results


def _tx(role: str | None = "hyperchat_app") -> psycopg.Connection:
    conn = fixture_db() if role is None else db(role)
    conn.autocommit = False
    return conn


def _owner_call(sql: str, params=()):
    with fixture_db() as conn:
        return conn.execute(sql, params).fetchone()[0]


def _state_now() -> tuple:
    with db() as conn:
        return conn.execute("SELECT state, dispute_mode FROM public.payment_accounting_state").fetchone()


def _event_count() -> int:
    with db() as conn:
        return conn.execute("SELECT count(*) FROM public.payment_accounting_events").fetchone()[0]


def _chain(did: str) -> None:
    """The history chain of one donation: each row starts where the previous ended (reversal and hold)."""
    rows = _ledger(did)
    for before, after in zip(rows, rows[1:]):
        assert (after[2], after[4]) == (before[3], before[5]), rows


def _bucket(sid: str, name: str = "Fan") -> None:
    """I6: the donor total equals the sum of its credit history."""
    with fixture_db() as conn:
        history = conn.execute("SELECT COALESCE(sum(delta_inr), 0) FROM public.donation_credit_events WHERE "
                               "streamer_id = %s AND donator_name_lower = lower(%s)", (sid, name)).fetchone()[0]
    assert (_total(sid, name) or 0) == history


def _pending_v2(sid: str, name: str = "Fan") -> str:
    """A V2 checkout donation awaiting its payment (provenance as the checkout writes it)."""
    did = make_donation(sid, 100, payment="pending", moderation="pending", name=name)
    with fixture_db() as conn:
        conn.execute("UPDATE public.donations SET payment_account = 'primary', provider_order_id = %s, "
                     "legacy_source_table = NULL WHERE id = %s", ("order_TESTconc" + secrets.token_hex(5), did))
    return did


def _app_rounds(client, rounds) -> list:
    """The real app in its own loop and pool, evaluation on. Each round is a callable (http client) -> awaitables,
    run concurrently; rounds run one after another (one app start for all of them). Results per round."""
    async def scenario():
        from app.main import create_app

        application = create_app()
        async with application.router.lifespan_context(application):
            wire(application.state, client.fake, InMemoryObjectStore())
            application.state.payments.repo.accounting = True
            transport = httpx.ASGITransport(app=application, client=("203.0.113.9", 4000))
            async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
                out = []
                for make in rounds:
                    out.append(await asyncio.wait_for(asyncio.gather(*make(c)), ROUND_TIMEOUT))
                return out
    return asyncio.run(scenario())


def _app_requests(client, coros_for) -> list:
    return _app_rounds(client, [coros_for])[0]


def _post(c, client, raw: bytes, event_id: str):
    return c.post(W, content=raw, headers={
        "x-razorpay-signature": sign(client.env["HYPERCHAT_RAZORPAY_PRIMARY_WEBHOOK_SECRET"], raw),
        "x-razorpay-event-id": event_id, "content-type": "application/json"})


def _donation_of(order_id: str) -> str:
    with db() as conn:
        return conn.execute("SELECT id::text FROM public.donations WHERE order_id = %s", (order_id,)).fetchone()[0]


def _unthrottled() -> None:
    """Checkout is limited to 10 a minute per client IP and every test request comes from one IP: clear the
    synthetic checkout counter before each of the N checkouts (cleanup_synthetic_rows removes payment:* too)."""
    with db() as conn:
        conn.execute("DELETE FROM public.rate_limits WHERE endpoint = 'payment:checkout'")


def _checkout(client, slug: str) -> dict:
    _unthrottled()
    return checkout(client, slug)


def _paid_http(client, slug: str):
    _unthrottled()
    return paid(client, slug)


def _tag() -> str:
    return secrets.token_hex(4)


def _causes(did: str) -> list[str]:
    return [r[0] for r in _ledger(did)]


# ── C1-C3, C8: the automatic paths through the real app ─────────────────────────

def test_c1_two_refund_webhooks_for_one_donation(client, record_property):
    """Both evidence rows, the final reversal = their sum, one evaluation history row per change, no 40P01."""
    tag = _tag()
    setups = [_paid_http(client, f"authtest-acct-c1{tag}{i:02d}") for i in range(N)]
    bodies = [[(integrity_body("refund.processed", ref=f"rfnd_TESTc1{tag}{i:02d}{n}", payment_id=payment.id,
                               order_id=out["razorpay_order_id"], amount=1500 * (n + 1)),
                f"authtest-acct-c1-{tag}-{i}-{n}") for n in range(2)] for i, (_, out, payment) in enumerate(setups)]
    results = _app_rounds(client, [lambda c, b=b: [_post(c, client, raw, eid) for raw, eid in b] for b in bodies])
    assert len(results) == N
    for (sid, out, _), responses in zip(setups, results):
        assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
        assert sorted(r.json()["status"] for r in responses) == ["integrity_recorded"] * 2
        did = _donation_of(out["order_id"])
        assert _state(did)[:2] == (4500, None) and totals(sid)[0][1] == 55
        assert _causes(did).count("evaluation") == 2  # serialised on the donation: each delivery is one change
        _chain(did)
    record_property("iterations", N)


def test_c2_the_capture_webhook_racing_a_refund_webhook(client, record_property):
    """Either order: the final reversal is correct; no deadlock."""
    tag = _tag()
    setups = []
    for i in range(N):
        slug = f"authtest-acct-c2{tag}{i:02d}"
        sid = make_streamer(slug)
        out = _checkout(client, slug)
        setups.append((sid, out, client.fake.captured(out["razorpay_order_id"], 10000)))

    def round_for(i, out, payment):
        refund = integrity_body("refund.processed", ref=f"rfnd_TESTc2{tag}{i:02d}", payment_id=payment.id,
                                order_id=out["razorpay_order_id"], amount=4000)
        capture = webhook_body("payment.captured", out["razorpay_order_id"], payment.id, 10000, "INR", "captured")
        return lambda c: [_post(c, client, refund, f"authtest-acct-c2-{tag}-{i}-r"),
                          _post(c, client, capture, f"authtest-acct-c2-{tag}-{i}-c")]
    results = _app_rounds(client, [round_for(i, out, payment) for i, (_, out, payment) in enumerate(setups)])
    assert len(results) == N
    for (sid, out, _), responses in zip(setups, results):
        assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
        did = _donation_of(out["order_id"])
        assert _state(did)[:2] == (4000, None) and totals(sid) == [("Ravi", 60, 1)]
        _chain(did)
    record_property("iterations", N)


def test_c3_a_capture_and_a_refund_on_two_donations_of_one_donor_bucket(client, record_property):
    """Same creator, same donor name: the capture of A and the evaluation of B both reach the one total row."""
    tag = _tag()
    slug = f"authtest-acct-c3{tag}"
    sid, out_b, payment_b = _paid_http(client, slug)
    rounds = []
    for i in range(N):
        out_a = _checkout(client, slug)
        payment_a = client.fake.captured(out_a["razorpay_order_id"], 10000)
        capture = webhook_body("payment.captured", out_a["razorpay_order_id"], payment_a.id, 10000, "INR", "captured")
        refund = integrity_body("refund.processed", ref=f"rfnd_TESTc3{tag}{i:02d}", payment_id=payment_b.id,
                                order_id=out_b["razorpay_order_id"], amount=100)
        rounds.append(lambda c, capture=capture, refund=refund, i=i: [
            _post(c, client, capture, f"authtest-acct-c3-{tag}-{i}-c"),
            _post(c, client, refund, f"authtest-acct-c3-{tag}-{i}-r")])
    results = _app_rounds(client, rounds)
    assert len(results) == N
    for responses in results:
        assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    did_b = _donation_of(out_b["order_id"])
    assert _state(did_b)[:2] == (100 * N, None)
    assert totals(sid) == [("Ravi", 100 * (N + 1) - N, N + 1)]  # each refund of 1.00 reverses 1.00 of B
    _bucket(sid, "Ravi")
    _chain(did_b)
    record_property("iterations", N)


def test_c8_payment_reconcile_racing_a_webhook_capture_completes_and_accounts_once(client, record_property):
    """A refund stored before the capture; then the reconcile run (G1) and the capture webhook race: one
    completion, the refund accounted exactly once."""
    from app.cli import connection_factory
    from app.core.config import Settings
    from app.payments.accounts import PaymentAccounts
    from app.payments.reconcile import Reconciler

    tag = _tag()
    setups = []
    for i in range(N):
        slug = f"authtest-acct-c8{tag}{i:02d}"
        sid = make_streamer(slug)
        out = _checkout(client, slug)
        setups.append((sid, out, client.fake.captured(out["razorpay_order_id"], 10000)))

    async def no_wait(_):
        pass
    reconciler = Reconciler(PaymentRepository(connection_factory(Settings()), accounting=True),
                            PaymentAccounts.from_settings(Settings()), client.fake, sleep=no_wait)
    rounds = []
    for i, (_, out, payment) in enumerate(setups):
        refund = integrity_body("refund.processed", ref=f"rfnd_TESTc8{tag}{i:02d}", payment_id=payment.id,
                                order_id=out["razorpay_order_id"], amount=4000)
        capture = webhook_body("payment.captured", out["razorpay_order_id"], payment.id, 10000, "INR", "captured")

        def race(c, out=out, capture=capture, i=i):
            aged(out["order_id"])  # only this round's order is old enough for the reconcile run
            return [_post(c, client, capture, f"authtest-acct-c8-{tag}-{i}-c"), reconciler.run(apply=True)]
        rounds += [lambda c, refund=refund, i=i: [_post(c, client, refund, f"authtest-acct-c8-{tag}-{i}-r")], race]
    results = _app_rounds(client, rounds)
    assert len(results) == 2 * N
    for i, (sid, out, _) in enumerate(setups):
        stored, (capture_response, _report) = results[2 * i], results[2 * i + 1]
        assert stored[0].json()["status"] in ("integrity_recorded", "integrity_mismatch")
        assert capture_response.status_code == 200, capture_response.text
        did = _donation_of(out["order_id"])
        assert totals(sid) == [("Ravi", 60, 1)] and _state(did)[:2] == (4000, None)
        assert _causes(did).count("evaluation") == 1  # accounted once
        _chain(did)
    record_property("iterations", N)


# ── C4, C5, C13: the freeze ─────────────────────────────────────────────────────

def test_c4_a_freeze_waits_for_evaluations_holding_the_state_and_later_ones_are_inactive(record_property):
    """The freeze waits (lock wait seen) and stays blocked, with no event and the state still active, until the
    LAST of three share holders commits: its event commits after all three (commit order observed directly), and
    its txid is above theirs. Evaluations after it return 'inactive'."""
    for _ in range(N):
        set_accounting("active", "apply")
        holders, donations, txids = [], [], []
        for _ in range(3):
            did, pid = _paid(_creator())
            ref = _ref("refund")
            _evidence(did, pid, "refund", ref, "processed", 2000)
            conn = _tx()
            conn.execute("SELECT * FROM public.payment_accounting_lock_state()")  # L-1, as the payment paths
            assert conn.execute(APPLY, (did, 2000, None, [], [ref])).fetchone()[0] == "evaluation"
            txids.append(conn.execute("SELECT txid_current()").fetchone()[0])
            holders.append(conn)
            donations.append(did)
        freezer = fixture_db()
        try:
            events = _event_count()
            thread, out = _background(lambda: freezer.execute(SET_STATE, ("frozen",)).fetchone()[0])
            _wait_blocked(freezer.info.backend_pid)
            for conn in holders:
                assert _blocked(freezer.info.backend_pid), "the freeze stopped waiting before every holder committed"
                assert _state_now()[0] == "active" and _event_count() == events
                conn.commit()
            _join(thread)
            assert out == [("ok", "frozen")] and _event_count() == events + 1
        finally:
            for conn in holders:
                conn.close()
            freezer.close()
        with db() as conn:
            event_txid = conn.execute("SELECT txid FROM public.payment_accounting_events ORDER BY id DESC LIMIT 1"
                                      ).fetchone()[0]
        assert event_txid > max(txids)
        for did in donations:  # committed before the freeze, in the holders' own transactions
            assert _state(did)[0] == 2000 and [r[7] for r in _ledger(did) if r[0] == "evaluation"][0] in txids
        late, pid = _paid(_creator())
        ref = _ref("refund")
        _evidence(late, pid, "refund", ref, "processed", 2000)
        assert _apply(late, 2000, refs=(ref,)) == "inactive" and _settle(late) == "inactive"
        assert _state(late)[0] == 0
    record_property("iterations", N)


def test_c5_moderation_never_waits_for_the_state_row(record_property):
    for _ in range(N):
        set_accounting("active", "apply")
        sid = _creator()
        did, pid = _paid(sid)
        _evidence(did, pid, "refund", _ref("refund"), "processed", 4000)
        _settle(did)
        holder, freezer = _tx(), fixture_db()
        try:
            holder.execute("SELECT * FROM public.payment_accounting_lock_state()")
            thread, out = _background(lambda: freezer.execute(SET_STATE, ("frozen",)).fetchone()[0])
            _wait_blocked(freezer.info.backend_pid)  # a freeze is queued behind a share holder
            with db() as mod:
                mod.execute("SET lock_timeout = '3s'")  # moderation must not queue behind it
                mod.execute("UPDATE public.donations SET moderation_status = 'rejected' WHERE id = %s", (did,))
            assert _blocked(freezer.info.backend_pid)  # the moderation went through while the freeze still waits
            holder.commit()
            _join(thread)
            assert out == [("ok", "frozen")]
        finally:
            holder.close()
            freezer.close()
        assert _state(did) == (4000, None, [], 0) and (_total(sid) or 0) == 0
        _bucket(sid)
    record_property("iterations", N)


@pytest.mark.parametrize("attempt", range(N_TIMEOUT))
def test_c13_a_freeze_with_a_lock_timeout_reports_and_changes_nothing(attempt):
    """The database part of C13 (the CLI's exit 1 is 6a-3): 55P03 after the function's own 5 s, whatever shorter
    timeout the caller set; no state change, no event."""
    holder = _tx()
    try:
        holder.execute("SELECT * FROM public.payment_accounting_lock_state()")
        events = _event_count()
        with fixture_db() as owner, pytest.raises(psycopg.errors.LockNotAvailable):
            owner.execute("SET lock_timeout = '500ms'")
            started = time.monotonic()
            try:
                owner.execute(SET_STATE, ("frozen",))
            finally:
                waited = time.monotonic() - started
        assert 4.5 <= waited < 30, waited  # the function's 5 s, not the caller's 0.5 s
        assert _state_now()[0] == "active" and _event_count() == events
    finally:
        holder.rollback()
        holder.close()


# ── C6, C9-C11: operator operations against each other and the automatic path ───

@pytest.mark.parametrize("attempt", range(N))
def test_c6_keep_racing_a_webhook_evaluation_serialises_on_the_donation(attempt):
    set_accounting("active", "hold")
    did, pid = _paid(_creator())
    _evidence(did, pid, "refund", _ref("refund"), "processed", 2000)
    _settle(did)
    _evidence(did, pid, "dispute", _ref("dispute"), "lost", 10000, deducted=5000)
    assert _settle(did) == "hold"
    run = _run("resolve-keep")
    results = _together(lambda: _keep(did, run), lambda: _settle(did))
    assert [r[0] for r in results] == ["ok", "ok"], results
    # keep first -> the evaluation sees its acknowledgement; evaluation first -> unchanged, then kept
    assert (results[0][1], results[1][1]) in (("kept", "acknowledged"), ("kept", "unchanged"))
    assert _state(did)[:2] == (2000, None)
    _chain(did)


def test_c9_two_reconcile_runs_at_once_write_history_once(record_property):
    outcomes = collections.Counter()
    for _ in range(N):
        did, pid = _paid(_creator())
        ref = _ref("refund")
        _evidence(did, pid, "refund", ref, "processed", 2500)
        runs = [_run("reconcile"), _run("reconcile")]
        results = _together(*(lambda r=r: _reconcile(did, 2500, (ref,), r) for r in runs))
        assert sorted(r[1] for r in results) == ["activation", "unchanged"], results
        assert _causes(did).count("activation") == 1 and _state(did)[:2] == (2500, None)
        with fixture_db() as conn:
            assert conn.execute("SELECT count(*) FROM public.payment_accounting_runs WHERE id = ANY (%s)",
                                (runs,)).fetchone()[0] == 2
        outcomes[tuple(r[1] for r in results)] += 1
    record_property("iterations", N)
    record_property("orders", dict(outcomes))


@pytest.mark.parametrize("attempt", range(N))
def test_c10_two_state_changes_at_once_serialise_without_a_lost_update(attempt):
    with db() as conn:
        last = conn.execute("SELECT COALESCE(max(id), 0) FROM public.payment_accounting_events").fetchone()[0]
    results = _together(lambda: _owner_call(SET_STATE, ("frozen",)), lambda: _owner_call(SET_STATE, ("active",)))
    assert all(r[0] == "ok" for r in results), results
    frozen, active = results[0][1], results[1][1]
    with db() as conn:
        log = conn.execute("SELECT to_state FROM public.payment_accounting_events WHERE id > %s ORDER BY id",
                           (last,)).fetchall()
    # frozen then active: two events, active; active ('unchanged') then frozen: one event, frozen
    assert (frozen, active) in (("frozen", "active"), ("frozen", "unchanged"))
    assert _state_now()[0] == log[-1][0] and len(log) == (2 if active == "active" else 1)


def test_c11_a_state_change_waits_for_a_revert_and_a_revert_after_activation_is_refused(record_property):
    for _ in range(N):
        set_accounting("active", "apply")
        sid = _creator()
        did, pid = _paid(sid)
        _evidence(did, pid, "refund", _ref("refund"), "processed", 4000)
        _settle(did)
        set_accounting("frozen")
        reverter, activator = _tx(None), fixture_db()
        try:
            assert reverter.execute("SELECT public.payment_accounting_revert(%s, %s)",
                                    (did, _run("revert"))).fetchone()[0] == "reverted"
            thread, out = _background(lambda: activator.execute(SET_STATE, ("active",)).fetchone()[0])
            _wait_blocked(activator.info.backend_pid)
            assert _state_now()[0] == "frozen"
            reverter.commit()
            _join(thread)
            assert out == [("ok", "active")]
        finally:
            reverter.close()
            activator.close()
        assert _state(did)[:2] == (0, None) and _total(sid) == 100
        other, other_pid = _paid(_creator())
        _evidence(other, other_pid, "refund", _ref("refund"), "processed", 1000)
        _settle(other)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):  # active committed first: revert refused
            _revert(other, _run("revert"))
        assert _state(other)[0] == 1000
    record_property("iterations", N)


# ── C7, C12: the revision 1 deadlock pattern and defence in depth ───────────────

def test_c7_the_revision_1_deadlock_pattern_never_deadlocks_under_l1(record_property):
    """Capture of A (auto-approve, credit), apply-evaluation of B (same donor bucket) and a queued freeze, 50 times.
    The only accepted error: apply-evaluation refused because the freeze committed first (nothing written)."""
    sid = _creator()
    outcomes = collections.Counter()
    for _ in range(N):
        set_accounting("active", "apply")
        a = _pending_v2(sid)
        b, b_pid = _paid(sid)
        ref = _ref("refund")
        _evidence(b, b_pid, "refund", ref, "processed", 1000)
        _apply(b, None, hold="H-CURRENCY")
        run = _run("resolve-apply")
        before_b = (_state(b), _ledger(b))
        repo = PaymentRepository(factory(), accounting=True)
        results = _together(
            lambda: run_async(repo.apply(a, "captured", "pay_TESTc7" + a[:8])),
            lambda: _owner_call("SELECT public.payment_accounting_apply_evaluation(%s, 1000, '{}', %s, %s)",
                                (b, [ref], run)),
            lambda: _owner_call(SET_STATE, ("frozen",)))
        for kind, value in results:
            if kind == "error":
                assert getattr(value, "sqlstate", None) != DEADLOCK, value
                assert isinstance(value, psycopg.errors.InsufficientPrivilege), value
        assert results[0][0] == "ok" and results[2] == ("ok", "frozen"), results
        if results[1][0] == "error":
            assert (_state(b), _ledger(b)) == before_b
        else:
            assert results[1][1] == "manual" and _state(b)[:2] == (1000, None)
        outcomes[results[1][0]] += 1
        _chain(b)
        _bucket(sid)
    record_property("iterations", N)
    record_property("apply_evaluation", dict(outcomes))


@pytest.mark.parametrize("attempt", range(N))
def test_c12_a_misordered_transaction_completes_or_aborts_cleanly(attempt, record_property):
    """A caller skipping L-1 (donation first, then apply) + a queued freeze + an S-first holder that wants the
    donation. Accepted: every transaction completes, or PostgreSQL aborts one or more of them with 40P01 (any of
    the three may be the victim). Any other error fails. The end state is checked against what committed: an
    aborted transaction leaves no reversal, history, total or event behind."""
    sid = _creator()
    did, pid = _paid(sid)
    ref = _ref("refund")
    _evidence(did, pid, "refund", ref, "processed", 3000)
    before = (_state(did), _ledger(did), _total(sid))
    events = _event_count()
    misordered, holder, freezer = _tx(), _tx(), fixture_db()

    def finish(conn, sql, params):
        try:
            value = conn.execute(sql, params).fetchone()
            conn.commit()
            return value[0] if value else None
        except psycopg.Error:
            conn.rollback()
            raise
    try:
        misordered.execute("SELECT 1 FROM public.donations WHERE id = %s FOR UPDATE", (did,))  # D first: wrong
        holder.execute("SELECT * FROM public.payment_accounting_lock_state()")                # S first: right
        f_thread, f_out = _background(lambda: freezer.execute(SET_STATE, ("frozen",)).fetchone()[0])
        _wait_blocked(freezer.info.backend_pid)
        m_thread, m_out = _background(lambda: finish(misordered, APPLY, (did, 3000, None, [], [ref])))
        h_thread, h_out = _background(lambda: finish(
            holder, "SELECT 1 FROM public.donations WHERE id = %s FOR UPDATE", (did,)))
        _join(m_thread, h_thread, f_thread)
    finally:
        for conn in (misordered, holder, freezer):
            conn.close()
    outcomes = {"misordered": m_out[0], "holder": h_out[0], "freeze": f_out[0]}
    for name, (kind, value) in outcomes.items():
        assert kind == "ok" or getattr(value, "sqlstate", None) == DEADLOCK, (name, value)
    record_property("outcome", {k: v[1] if v[0] == "ok" else v[1].sqlstate for k, v in outcomes.items()})

    frozen = outcomes["freeze"][0] == "ok"
    assert _state_now()[0] == ("frozen" if frozen else "active") and _event_count() == events + frozen
    kind, value = outcomes["misordered"]
    if kind == "ok":
        assert value in ("evaluation", "inactive"), value
    if kind == "ok" and value == "evaluation":  # committed: exactly its own reversal, history and total
        assert _state(did)[:2] == (3000, None) and _total(sid) == 70
        assert _ledger(did)[:len(before[1])] == before[1]
        assert [r[0] for r in _ledger(did)[len(before[1]):]] == ["evaluation", "writer"]
    else:  # aborted (40P01) or ran after the freeze ('inactive'): nothing written
        assert (_state(did), _ledger(did), _total(sid)) == before
    _chain(did)
    _bucket(sid)


# ── refunds-only through the signed webhook route (revision 2 §8) ───────────────

def _report_held(did: str) -> set:
    held = run_async(PaymentRepository(factory()).accounting_report())["held"]
    return {(h["hold"], h["applied_reversed_paise"]) for h in held if h["donation_id"] == did}


def test_refunds_only_a_lost_dispute_webhook_is_held_and_moves_no_money(client):
    """Mode hold: a signed payment.dispute.lost delivery reaches the repository and is held as H-DISPUTE-MODE:
    no reversal, the donor total unchanged, one 'hold' history row naming no evidence, listed as held."""
    set_accounting("active", "hold")
    tag = _tag()
    sid, out, payment = _paid_http(client, f"authtest-acct-rh{tag}")
    did = _donation_of(out["order_id"])
    history = _ledger(did)
    lost = integrity_body("payment.dispute.lost", ref=f"disp_TESTrh{tag}", payment_id=payment.id,
                          order_id=out["razorpay_order_id"], amount=10000, amount_deducted=4000, currency="INR")
    assert _app_requests(client, lambda c: [_post(c, client, lost, f"authtest-acct-rh-{tag}")])[0].json() == {
        "status": "integrity_recorded"}
    assert _state(did) == (0, "H-DISPUTE-MODE", [], 100) and totals(sid) == [("Ravi", 100, 1)]
    added = _ledger(did)[len(history):]
    assert [(r[0], r[1], r[2:7]) for r in added] == [("hold", 0, (0, 0, None, "H-DISPUTE-MODE", None))]
    assert _evaluation(did)["outcome"] == "hold" and _evaluation(did)["evidence_fp"] == _fp(did)
    assert _report_held(did) == {("H-DISPUTE-MODE", 0)}


def test_refunds_only_a_refund_webhook_after_a_lost_dispute_stays_frozen_and_reported(client):
    """S6A-D3a through the route: the refund delivered after the loss is NOT applied (frozen at 0), no history row,
    the evaluation records the new evidence, and the item stays held in the report; the unapplied difference is the
    processed refund (never classified as reconciled)."""
    set_accounting("active", "hold")
    tag = _tag()
    sid, out, payment = _paid_http(client, f"authtest-acct-rr{tag}")
    did = _donation_of(out["order_id"])
    lost = integrity_body("payment.dispute.lost", ref=f"disp_TESTrr{tag}", payment_id=payment.id,
                          order_id=out["razorpay_order_id"], amount=10000, amount_deducted=4000, currency="INR")
    refund = integrity_body("refund.processed", ref=f"rfnd_TESTrr{tag}", payment_id=payment.id,
                            order_id=out["razorpay_order_id"], amount=2500)
    first, second = _app_rounds(client, [lambda c: [_post(c, client, lost, f"authtest-acct-rr-{tag}-1")],
                                         lambda c: [_post(c, client, refund, f"authtest-acct-rr-{tag}-2")]])
    assert first[0].json() == second[0].json() == {"status": "integrity_recorded"}
    held_history = _ledger(did)
    assert _state(did) == (0, "H-DISPUTE-MODE", [], 100) and totals(sid) == [("Ravi", 100, 1)]
    assert _causes(did).count("hold") == 1 and "evaluation" not in _causes(did)  # the refund wrote no history
    assert _evaluation(did)["outcome"] == "unchanged" and _evaluation(did)["evidence_fp"] == _fp(did)
    with db() as conn:
        processed = conn.execute("SELECT COALESCE(sum(amount_paise), 0) FROM public.payment_integrity_current WHERE "
                                 "donation_id = %s AND kind = 'refund' AND status = 'processed'", (did,)).fetchone()[0]
    assert processed - _state(did)[0] == 2500                                   # the unresolved difference
    assert _report_held(did) == {("H-DISPUTE-MODE", 0)} and _ledger(did) == held_history
