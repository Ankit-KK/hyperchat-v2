"""PAY-15 (D17) stage 6a-1 on PostgreSQL: the accounting switch (migration 0034), its audit and operator runs,
evaluation metadata and fingerprints, refunds-only dispute mode, and the owner-only keep / apply-evaluation / revert /
reconcile operations.

The state is created 'off'. Every financial change still goes through the one credit writer, and every manual,
activation or revert change names an operator run. Reversals come from real evidence through the repository path
(_settle) or the bounded database functions, never from writes to donation_reversals."""

import secrets
import time
from decimal import Decimal

import psycopg
import pytest

from app.payments.repository import PaymentRepository
from tests.integration import db, fixture_db, requires_db, set_accounting
from tests.jobs.test_jobs_db import factory
from tests.jobs.test_jobs_db import run as run_async
from tests.payments.test_accounting_db import _apply, _creator, _evidence, _ledger, _paid, _ref, _settle, _state, _total

pytestmark = [requires_db, pytest.mark.usefixtures("env")]
REFUSED = psycopg.errors.InsufficientPrivilege
INVALID = psycopg.errors.InvalidParameterValue
RUNTIME = ("hyperchat_app", "hyperchat_worker", "hyperchat_readonly")


@pytest.fixture(autouse=True)
def _leave_frozen():
    yield
    set_accounting("frozen")


# ── helpers (owner = the operator tools of PR 6a-3) ─────────────────────────────

def _owner(sql: str, params=()):
    with fixture_db() as conn:
        return conn.execute(sql, params).fetchone()[0]


def _set(state=None, mode=None, operator="op-test", reason="a test state change", ref=None):
    return _owner("SELECT public.payment_accounting_set_state(%s, %s, %s, %s, %s)", (state, mode, operator, reason, ref))


def _current_state() -> tuple:
    with db() as conn:
        return conn.execute("SELECT state, dispute_mode FROM public.payment_accounting_state").fetchone()


def _events() -> int:
    with db() as conn:
        return conn.execute("SELECT count(*) FROM public.payment_accounting_events").fetchone()[0]


def _run(command: str, reason: str = "a test operator run") -> int:
    return _owner("SELECT public.payment_accounting_open_run(%s, 'op-test', %s, NULL, '{}'::jsonb)", (command, reason))


def _keep(did, run):
    return _owner("SELECT public.payment_accounting_keep(%s, %s)", (did, run))


def _revert(did, run):
    return _owner("SELECT public.payment_accounting_revert(%s, %s)", (did, run))


def _apply_evaluation(did, reversed_paise, refs, run, flags=()):
    return _owner("SELECT public.payment_accounting_apply_evaluation(%s, %s, %s, %s, %s)",
                  (did, reversed_paise, list(flags), list(refs), run))


def _reconcile(did, reversed_paise, refs, run, hold=None, flags=()):
    return _owner("SELECT public.payment_accounting_reconcile(%s, %s, %s, %s, %s, %s)",
                  (did, reversed_paise, hold, list(flags), list(refs), run))


def _evaluation(did) -> dict | None:
    with db() as conn:
        cur = conn.execute("SELECT evidence_fp, evaluated_at, state, dispute_mode, outcome, ack_fp, ack_run FROM "
                           "public.donation_accounting_evaluations WHERE donation_id = %s", (did,))
        row = cur.fetchone()
        return dict(zip([c.name for c in cur.description], row)) if row else None


def _fp(did) -> str:
    return _owner("SELECT public.payment_evidence_fp(%s)", (did,))


def _runs_of(did) -> list:
    with fixture_db() as conn:
        return conn.execute("SELECT cause, run_id FROM public.donation_credit_events WHERE donation_id = %s AND "
                            "cause IN ('manual', 'activation', 'revert') ORDER BY id", (did,)).fetchall()


def _outbox(sid: str) -> list[tuple]:
    """The creator's realtime events (event, channel, coalesce key, status, max attempts), oldest first."""
    with db() as conn:
        return conn.execute("SELECT event, channel, coalesce_key, status, max_attempts FROM public.realtime_outbox "
                            "WHERE streamer_id = %s ORDER BY id", (sid,)).fetchall()


def _published(sid: str) -> None:
    """As the publisher does once it sent them (owner here): the pending events are no longer open."""
    with fixture_db() as conn:
        conn.execute("UPDATE public.realtime_outbox SET status = 'published', published_at = now() "
                     "WHERE streamer_id = %s AND status = 'pending'", (sid,))


def _totals_changed(sid: str) -> list[tuple]:
    """The two rows app/realtime/outbox.emit_totals_changed would insert for this creator."""
    from app.realtime import outbox
    return [(event, outbox.EVENT_CHANNELS[event], f"{event}:{sid}", "pending", outbox.MAX_ATTEMPTS)
            for event in ("goal-progress", "leaderboard-updated")]


def _no_deduction(did, refunds_paise: int) -> None:
    """Refunds-only: the applied reversal never exceeds the processed refunds (no dispute deduction in it)."""
    assert _state(did)[0] <= refunds_paise


# ── the state machine and its audit ─────────────────────────────────────────────

def test_transitions_are_audited_idempotent_and_freeze_first():
    set_accounting("frozen", "hold")
    before = _events()
    assert _set("active") == "active" and _events() == before + 1
    assert _set("active") == "unchanged" and _events() == before + 1             # idempotent, no event
    with pytest.raises(INVALID):
        _set("off")                                                               # active -> off: freeze first
    with pytest.raises(INVALID):
        _set(None, "apply")                                                       # the mode never changes while active
    assert _set("frozen") == "frozen"
    assert _set(None, "apply") == "frozen" and _current_state() == ("frozen", "apply")
    with db() as conn:
        last = conn.execute("SELECT from_state, to_state, from_mode, to_mode, operator, reason, db_user FROM "
                            "public.payment_accounting_events ORDER BY id DESC LIMIT 1").fetchone()
    assert last[:6] == ("frozen", "frozen", "hold", "apply", "op-test", "a test state change") and last[6]


@pytest.mark.parametrize("operator, reason", [("", "a long enough reason"), ("op", "too short"), ("op", "x" * 501)])
def test_a_change_needs_an_operator_and_a_meaningful_reason(operator, reason):
    set_accounting("frozen")
    before = (_current_state(), _events())
    with pytest.raises(REFUSED):
        _set("active", operator=operator, reason=reason)
    assert (_current_state(), _events()) == before


def test_off_needs_every_reversal_and_hold_reverted():
    set_accounting("active", "apply")
    did, _ = _paid(_creator())
    _apply(did, None, hold="H-CONFLICT")
    _set("frozen")
    with pytest.raises(INVALID):
        _set("off")


def test_a_direct_owner_update_is_audited_or_refused_and_the_row_is_fixed():
    set_accounting("frozen")
    with fixture_db() as conn, pytest.raises(REFUSED), conn.transaction(force_rollback=True):
        conn.execute("UPDATE public.payment_accounting_state SET state = 'active'")  # no operator / reason
    before = _events()
    with fixture_db() as conn:
        conn.execute("UPDATE public.payment_accounting_state SET state = 'active', change_operator = 'op-direct', "
                     "change_reason = 'direct owner update, audited'")
        assert conn.execute("SELECT change_reason FROM public.payment_accounting_state").fetchone()[0] is None
    assert _events() == before + 1 and _current_state()[0] == "active"
    for sql in ("INSERT INTO public.payment_accounting_state (id, state, changed_by) VALUES (false, 'off', 'x')",
                "DELETE FROM public.payment_accounting_state", "TRUNCATE public.payment_accounting_state"):
        with fixture_db() as conn, pytest.raises(psycopg.Error), conn.transaction(force_rollback=True):
            conn.execute(sql)


@pytest.mark.parametrize("table", ["payment_accounting_events", "payment_accounting_runs"])
def test_events_and_runs_are_append_only_for_the_owner_too(table):
    set_accounting("frozen")
    _run("revert")
    _set(None, "apply" if _current_state()[1] == "hold" else "hold")
    for sql in (f"UPDATE public.{table} SET operator = 'x'", f"DELETE FROM public.{table}", f"TRUNCATE public.{table}"):
        with fixture_db() as conn, pytest.raises(REFUSED), conn.transaction(force_rollback=True):
            conn.execute(sql)


@pytest.mark.parametrize("role", RUNTIME)
def test_runtime_roles_read_the_switch_but_change_nothing(role):
    set_accounting("frozen")
    did, _ = _paid(_creator())
    calls = [
        "UPDATE public.payment_accounting_state SET state = 'active', change_operator = 'x', change_reason = 'xxxxxxxxxxxx'",
        "INSERT INTO public.payment_accounting_runs (command, operator, reason, scope, db_user) VALUES "
        "('revert', 'x', 'xxxxxxxxxxxx', '{}', 'x')",
        "INSERT INTO public.donation_accounting_evaluations (donation_id, evidence_fp, evaluated_at, state, "
        "dispute_mode, outcome) VALUES (%(d)s, 'x', now(), 'off', 'hold', 'x')",
        "SELECT public.payment_accounting_set_state('active', NULL, 'x', 'xxxxxxxxxxxx', NULL)",
        "SELECT public.payment_accounting_open_run('revert', 'x', 'xxxxxxxxxxxx', NULL, '{}')",
        "SELECT public.payment_accounting_keep(%(d)s, 1)", "SELECT public.payment_accounting_revert(%(d)s, 1)",
        "SELECT public.payment_accounting_reconcile(%(d)s, 0, NULL, '{}', '{}', 1)",
        "SELECT public.payment_accounting_apply_evaluation(%(d)s, 0, '{}', '{}', 1)",
        "SELECT public.donation_accounting_core(%(d)s, 0, NULL, '{}', '{}', 'manual', 1)",
        "SELECT public.payment_evidence_fp(%(d)s)",
    ]
    for sql in calls:
        with db(role) as conn, pytest.raises(psycopg.Error) as refused:
            conn.execute(sql, {"d": did})
        assert refused.value.sqlstate in ("42501", "25006"), sql  # readonly may refuse as a read-only transaction
    with db(role) as conn:
        assert conn.execute("SELECT state FROM public.payment_accounting_state").fetchone()[0] == "frozen"
        conn.execute("SELECT (SELECT count(*) FROM public.payment_accounting_events), (SELECT count(*) FROM "
                     "public.payment_accounting_runs), (SELECT count(*) FROM public.donation_accounting_evaluations)")


def test_only_the_api_role_takes_the_state_lock_and_only_through_the_owner_function():
    """L-1: SELECT ... FOR SHARE needs UPDATE privilege, which no runtime role has; the API locks through the
    owner function instead, and holds the row lock until its transaction ends (a freeze waits for it)."""
    set_accounting("frozen")
    with db() as conn, pytest.raises(REFUSED):
        conn.execute("SELECT state FROM public.payment_accounting_state FOR SHARE")
    for role in ("hyperchat_worker", "hyperchat_auth", "hyperchat_lifecycle", "hyperchat_stepup", "hyperchat_readonly"):
        with db(role) as conn, pytest.raises(REFUSED):
            conn.execute("SELECT * FROM public.payment_accounting_lock_state()")
    holder = db()
    holder.autocommit = False
    try:
        assert holder.execute("SELECT state, dispute_mode FROM public.payment_accounting_lock_state()"
                              ).fetchone()[0] == "frozen"
        with fixture_db() as conn:  # the share lock is a row lock held by the API transaction, visible to all
            assert conn.execute("SELECT count(*) FROM pg_locks WHERE pid = %s AND granted AND locktype = "
                                "'transactionid'", (holder.info.backend_pid,)).fetchone()[0] == 1
    finally:
        holder.rollback()
        holder.close()


@pytest.mark.parametrize("caller_timeout", ["0", "60s"])  # unset (wait forever) and a looser caller setting
def test_a_state_change_waits_at_most_five_seconds_then_fails_and_records_nothing(caller_timeout):
    """S6A-D6 (F-4): set_state's own lock_timeout = 5 s bounds the wait for the state row whatever the caller set;
    it fails with 55P03, the transaction rolls back (no state change, no event), and the caller's setting is back
    afterwards."""
    set_accounting("active")
    holder = db()
    holder.autocommit = False
    try:
        holder.execute("SELECT * FROM public.payment_accounting_lock_state()")  # an evaluation in flight
        before = (_current_state(), _events())
        with fixture_db() as owner:
            owner.execute(f"SET lock_timeout = '{caller_timeout}'")
            started = time.monotonic()
            with pytest.raises(psycopg.errors.LockNotAvailable):
                owner.execute("SELECT public.payment_accounting_set_state('frozen', NULL, 'op-test', "
                              "'blocked by an evaluation', NULL)")
            waited = time.monotonic() - started
            restored = owner.execute("SHOW lock_timeout").fetchone()[0]
        assert 4.5 <= waited < 30, waited
        assert restored == ("0" if caller_timeout == "0" else "1min")  # the function did not leak its setting
        assert (_current_state(), _events()) == before
        with fixture_db() as owner, owner.transaction(force_rollback=True):  # inside a caller's transaction
            owner.execute("SET LOCAL lock_timeout = '60s'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                owner.execute("SELECT public.payment_accounting_set_state('frozen', NULL, 'op-test', "
                              "'blocked by an evaluation', NULL)")
        assert (_current_state(), _events()) == before
    finally:
        holder.rollback()
        holder.close()
    assert _set("frozen") == "frozen"  # unblocked: the change goes through at once


# ── apply only while active; metadata is not history ────────────────────────────

def test_nothing_is_evaluated_unless_the_state_is_active():
    set_accounting("active", "apply")
    did, pid = _paid(_creator())
    ref = _ref("refund")
    _evidence(did, pid, "refund", ref, "processed", 2500)
    set_accounting("frozen")
    before = (_state(did), _ledger(did), _total(_creator_of(did)))
    assert _apply(did, 2500, refs=(ref,)) == "inactive"
    assert _settle(did) == "inactive"
    assert (_state(did), _ledger(did), _total(_creator_of(did))) == before and _evaluation(did) is None
    set_accounting("active")
    assert _settle(did) == "evaluation" and _state(did)[0] == 2500


def _creator_of(did) -> str:
    with db() as conn:
        return conn.execute("SELECT streamer_id::text FROM public.donations WHERE id = %s", (did,)).fetchone()[0]


def test_a_no_op_evaluation_refreshes_metadata_but_writes_no_history_and_moves_no_total():
    set_accounting("active", "apply")
    did, pid = _paid(_creator())
    ref = _ref("refund")
    _evidence(did, pid, "refund", ref, "processed", 2500)
    assert _settle(did) == "evaluation"
    first = _evaluation(did)
    history, total = _ledger(did), _total(_creator_of(did))
    assert _settle(did) == "unchanged"
    second = _evaluation(did)
    assert second["evaluated_at"] > first["evaluated_at"] and second["outcome"] == "unchanged"
    assert second["evidence_fp"] == first["evidence_fp"] == _fp(did)
    assert _ledger(did) == history and _total(_creator_of(did)) == total


# ── the evidence fingerprint ────────────────────────────────────────────────────

def test_the_fingerprint_changes_with_every_fact_that_matters():
    set_accounting("frozen", "hold")
    did, pid = _paid(_creator())
    seen = {_fp(did)}

    def changed(note):
        fp = _fp(did)
        assert fp not in seen, note
        seen.add(fp)
    refund, dispute = _ref("refund"), _ref("dispute")
    _evidence(did, pid, "refund", refund, "pending", 2000)
    changed("a new refund reference")
    _evidence(did, pid, "refund", refund, "processed", 2000)
    changed("a refund status change")
    _evidence(did, pid, "dispute", dispute, "open", 10000, at=1)
    changed("a new dispute")
    _evidence(did, pid, "dispute", dispute, "lost", 10000, deducted=3000, at=2)
    changed("a dispute status change")
    _evidence(did, pid, "dispute", dispute, "lost", 10000, deducted=3000, at=3, phase="pre_arbitration")
    changed("a dispute phase change")
    _evidence(did, pid, "dispute", dispute, "lost", 10000, deducted=4000, at=4, phase="pre_arbitration")
    changed("a deduction change (same phase and status, later event)")
    _evidence(did, pid, "dispute", dispute, "won", 10000, deducted=4000, at=5, phase="pre_arbitration")
    changed("a conflict (lost and won in one phase)")
    _evidence(did, "pay_TESTfpother" + secrets.token_hex(3), "refund", refund, "processed", 2000, match="order", at=6)
    changed("an attribution change (the same refund now names another payment, order-matched)")
    _set(None, "apply")
    changed("a dispute-mode change")
    assert _fp(did) == _fp(did)  # stable while nothing changes


# ── refunds-only: dispute_mode = hold ───────────────────────────────────────────

def _hold_mode():
    set_accounting("active", "hold")


def test_refund_only_applies():
    _hold_mode()
    did, pid = _paid(_creator())
    _evidence(did, pid, "refund", _ref("refund"), "processed", 2500)
    assert _settle(did) == "evaluation" and _state(did)[:2] == (2500, None)


def test_a_lost_dispute_alone_is_held_and_moves_no_money():
    _hold_mode()
    sid = _creator()
    did, pid = _paid(sid)
    _evidence(did, pid, "dispute", _ref("dispute"), "lost", 10000, deducted=4000)
    assert _settle(did) == "hold"
    assert _state(did) == (0, "H-DISPUTE-MODE", [], Decimal("100.00")) and _total(sid) == Decimal("100.00")


def test_a_refund_then_a_lost_dispute_freezes_at_the_refund():
    _hold_mode()
    did, pid = _paid(_creator())
    _evidence(did, pid, "refund", _ref("refund"), "processed", 2500)
    _settle(did)
    _evidence(did, pid, "dispute", _ref("dispute"), "lost", 10000, deducted=4000)
    assert _settle(did) == "hold" and _state(did)[:2] == (2500, "H-DISPUTE-MODE")
    _no_deduction(did, 2500)


def test_a_refund_after_a_lost_dispute_stays_frozen_and_is_reported_not_reconciled():
    """S6A-D3a (full freeze): the later refund is NOT applied; the difference stays visible as a held item and the
    evaluation outcome is a hold, never a clear / reconciled one."""
    _hold_mode()
    sid = _creator()
    did, pid = _paid(sid)
    _evidence(did, pid, "dispute", _ref("dispute"), "lost", 10000, deducted=4000)
    _settle(did)
    _evidence(did, pid, "refund", _ref("refund"), "processed", 2500)
    assert _settle(did) == "unchanged"                                  # still held, frozen at 0
    assert _state(did)[:2] == (0, "H-DISPUTE-MODE") and _total(sid) == Decimal("100.00")
    assert _evaluation(did)["outcome"] == "unchanged" and _evaluation(did)["evidence_fp"] == _fp(did)
    with db() as conn:  # the unresolved difference: 2500 processed refunds, 0 applied, held
        processed = conn.execute("SELECT COALESCE(sum(amount_paise), 0) FROM public.payment_integrity_current WHERE "
                                 "donation_id = %s AND kind = 'refund' AND status = 'processed'", (did,)).fetchone()[0]
    assert processed - _state(did)[0] == 2500
    held = run_async(PaymentRepository(factory()).accounting_report())["held"]  # the operator report: held
    assert {(h["hold"], h["applied_reversed_paise"]) for h in held if h["donation_id"] == did} == {("H-DISPUTE-MODE", 0)}


def test_several_refunds_and_an_open_dispute_apply_the_refunds():
    _hold_mode()
    did, pid = _paid(_creator())
    _evidence(did, pid, "refund", _ref("refund"), "processed", 1000)
    _evidence(did, pid, "refund", _ref("refund"), "processed", 1500)
    _evidence(did, pid, "dispute", _ref("dispute"), "open", 10000)
    assert _settle(did) == "evaluation" and _state(did)[:3] == (2500, None, ["F-OPEN"])


def test_several_refunds_then_a_lost_dispute_freeze():
    _hold_mode()
    did, pid = _paid(_creator())
    for amount in (1000, 1500):
        _evidence(did, pid, "refund", _ref("refund"), "processed", amount)
    _settle(did)
    _evidence(did, pid, "dispute", _ref("dispute"), "lost", 10000, deducted=9000)
    _settle(did)
    assert _state(did)[:2] == (2500, "H-DISPUTE-MODE")
    _no_deduction(did, 2500)


def test_a_win_after_the_loss_at_a_later_phase_resolves_to_the_refunds():
    _hold_mode()
    did, pid = _paid(_creator())
    dispute = _ref("dispute")
    _evidence(did, pid, "refund", _ref("refund"), "processed", 2000)
    _evidence(did, pid, "dispute", dispute, "lost", 10000, deducted=4000, at=1)
    _settle(did)
    _evidence(did, pid, "dispute", dispute, "won", 10000, deducted=4000, at=2, phase="pre_arbitration")
    assert _settle(did) == "resolved" and _state(did)[:2] == (2000, None)


def test_a_win_in_the_same_phase_is_a_conflict_hold():
    _hold_mode()
    did, pid = _paid(_creator())
    dispute = _ref("dispute")
    _evidence(did, pid, "dispute", dispute, "lost", 10000, deducted=4000, at=1)
    _settle(did)
    _evidence(did, pid, "dispute", dispute, "won", 10000, deducted=4000, at=2)
    assert _settle(did) == "hold" and _state(did)[:2] == (0, "H-CONFLICT")


def test_new_evidence_while_held_changes_no_applied_reversal():
    _hold_mode()
    did, pid = _paid(_creator())
    _evidence(did, pid, "refund", _ref("refund"), "processed", 1000)
    _settle(did)
    _evidence(did, pid, "dispute", _ref("dispute"), "lost", 10000, deducted=5000)
    _settle(did)
    for amount in (500, 700):
        _evidence(did, pid, "refund", _ref("refund"), "processed", amount)
        _settle(did)
        assert _state(did)[:2] == (1000, "H-DISPUTE-MODE")


def test_the_database_itself_refuses_a_dispute_deduction_in_hold_mode():
    _hold_mode()
    did, pid = _paid(_creator())
    refund, lost = _ref("refund"), _ref("dispute")
    _evidence(did, pid, "refund", refund, "processed", 1000)
    _evidence(did, pid, "dispute", lost, "lost", 10000, deducted=4000)
    for amount, refs in ((4000, (refund, lost)), (1000, (refund, lost))):  # any clear result while a loss exists
        with pytest.raises(REFUSED):
            _apply(did, amount, refs=refs)
    other, _ = _paid(_creator())
    with pytest.raises(INVALID):  # H-DISPUTE-MODE without a lost dispute
        _apply(other, None, hold="H-DISPUTE-MODE")
    set_accounting("active", "apply")
    with pytest.raises(INVALID):  # nor in mode apply
        _apply(did, None, hold="H-DISPUTE-MODE")


# ── manual keep (only H-DISPUTE-MODE; acknowledgement by fingerprint) ───────────

def test_keep_releases_only_a_dispute_mode_hold_and_needs_its_run():
    _hold_mode()
    conflicted, pid = _paid(_creator())
    _apply(conflicted, None, hold="H-CONFLICT")
    run = _run("resolve-keep")
    with pytest.raises(REFUSED):
        _keep(conflicted, run)                                         # an evidence-integrity hold is never kept
    plain, _ = _paid(_creator())
    assert _keep(plain, run) == "unchanged"                             # not held
    with pytest.raises(REFUSED):
        _keep(plain, _run("revert"))                                    # a run of another command


def test_keep_is_acknowledged_for_exactly_this_evidence_and_new_evidence_invalidates_it():
    _hold_mode()
    sid = _creator()
    did, pid = _paid(sid)
    _evidence(did, pid, "refund", _ref("refund"), "processed", 2000)
    _settle(did)
    _evidence(did, pid, "dispute", _ref("dispute"), "lost", 10000, deducted=4000)
    _settle(did)
    history = len(_ledger(did))
    run = _run("resolve-keep")
    assert _keep(did, run) == "kept"
    assert _state(did)[:2] == (2000, None) and _total(sid) == Decimal("80.00")          # no money moved
    assert _runs_of(did) == [("manual", run)] and len(_ledger(did)) == history + 1
    ack = _evaluation(did)
    assert (ack["ack_fp"], ack["ack_run"], ack["outcome"]) == (_fp(did), run, "kept")
    assert _settle(did) == "acknowledged" and _state(did)[:2] == (2000, None)            # same evidence: respected
    assert len(_ledger(did)) == history + 1
    _evidence(did, pid, "refund", _ref("refund"), "processed", 500)                     # new evidence
    assert _settle(did) == "hold" and _state(did)[:2] == (2000, "H-DISPUTE-MODE")
    assert _evaluation(did)["ack_fp"] is None                                           # the acknowledgement is gone


def test_a_dispute_mode_change_invalidates_a_keep():
    _hold_mode()
    did, pid = _paid(_creator())
    _evidence(did, pid, "dispute", _ref("dispute"), "lost", 10000, deducted=4000)
    _settle(did)
    _keep(did, _run("resolve-keep"))
    set_accounting("active", "apply")                                                     # mode is in the fingerprint
    assert _settle(did) == "evaluation" and _state(did)[:2] == (4000, None)


# ── manual apply-evaluation (only a clear, evidence-supported result) ───────────

def test_apply_evaluation_applies_only_what_the_evidence_supports():
    set_accounting("active", "apply")
    sid = _creator()
    did, pid = _paid(sid)
    ref = _ref("refund")
    _evidence(did, pid, "refund", ref, "processed", 2500)
    _apply(did, None, hold="H-CURRENCY")                    # held earlier although the evidence is now clear
    run = _run("resolve-apply")
    with pytest.raises(REFUSED):
        _apply_evaluation(did, 3000, (ref,), run)            # not what the evidence supports
    assert _apply_evaluation(did, 2500, (ref,), run) == "manual"
    assert _state(did)[:2] == (2500, None) and _total(sid) == Decimal("75.00")
    assert _runs_of(did) == [("manual", run)]
    assert _apply_evaluation(did, 2500, (ref,), run) == "unchanged"   # not held any more


def test_apply_evaluation_is_refused_for_a_lost_dispute_in_hold_mode():
    _hold_mode()
    did, pid = _paid(_creator())
    lost = _ref("dispute")
    _evidence(did, pid, "dispute", lost, "lost", 10000, deducted=4000)
    _settle(did)
    with pytest.raises(REFUSED):
        _apply_evaluation(did, 4000, (lost,), _run("resolve-apply"))
    assert _state(did)[:2] == (0, "H-DISPUTE-MODE")


# ── counterfactual revert ───────────────────────────────────────────────────────

def test_revert_restores_the_no_reversal_world_through_the_writer():
    set_accounting("active", "apply")
    sid = _creator()
    did, pid = _paid(sid)
    _evidence(did, pid, "refund", _ref("refund"), "processed", 4000)
    _settle(did)
    run = _run("revert")
    with pytest.raises(REFUSED):
        _revert(did, run)                                             # never while active
    set_accounting("frozen")
    with db() as conn:  # moderation in between: reject then approve (the writer keeps using the frozen 4000)
        conn.execute("UPDATE public.donations SET moderation_status = 'rejected' WHERE id = %s", (did,))
        conn.execute("UPDATE public.donations SET moderation_status = 'approved' WHERE id = %s", (did,))
    assert _total(sid) == Decimal("60.00")
    assert _revert(did, run) == "reverted"
    assert _state(did) == (0, None, [], Decimal("100.00")) and _total(sid) == Decimal("100.00")  # counterfactual
    assert _runs_of(did)[-1] == ("revert", run)
    assert _revert(did, run) == "unchanged"


def test_revert_also_clears_a_hold_and_drops_a_keep_acknowledgement():
    _hold_mode()
    sid = _creator()
    did, pid = _paid(sid)
    _evidence(did, pid, "refund", _ref("refund"), "processed", 4000)
    _settle(did)
    _evidence(did, pid, "dispute", _ref("dispute"), "lost", 10000, deducted=6000)
    _settle(did)
    _keep(did, _run("resolve-keep"))
    _evidence(did, pid, "refund", _ref("refund"), "processed", 500)
    assert _settle(did) == "hold"                                        # held again: (4000, H-DISPUTE-MODE)
    set_accounting("frozen")
    assert _revert(did, _run("revert")) == "reverted"
    assert _state(did)[:2] == (0, None) and _total(sid) == Decimal("100.00")
    history = _ledger(did)
    revert = [r for r in history if r[0] == "revert"]
    assert len(revert) == 1 and revert[0][1:6] == (0, 4000, 0, "H-DISPUTE-MODE", None)  # no money of its own
    writer = history[history.index(revert[0]) + 1:]                   # the one writer restores the credit after it
    assert [(r[0], r[1], r[2:6]) for r in writer] == [("writer", Decimal("40.00"), (0, 0, None, None))]
    assert writer[0][7] == revert[0][7]                                 # same transaction (txid)
    assert sum(r[1] for r in history) == _state(did)[3] == Decimal("100.00")
    assert (_evaluation(did)["outcome"], _evaluation(did)["ack_fp"]) == ("reverted", None)


def test_a_manual_operation_that_cannot_move_the_total_rolls_back_entirely():
    """Failure injection: apply-evaluation needs a debit the drifted total cannot take (HC002): nothing at all is
    written: no reversal, no history row, no metadata change."""
    set_accounting("active", "apply")
    sid = _creator()
    did, pid = _paid(sid)
    ref = _ref("refund")
    _evidence(did, pid, "refund", ref, "processed", 9000)
    _apply(did, None, hold="H-CURRENCY")
    with fixture_db() as conn:
        conn.execute("UPDATE public.streamer_donator_totals SET total_amount = 10 WHERE streamer_id = %s", (sid,))
    before = (_state(did), _ledger(did), _evaluation(did), _total(sid), _outbox(sid))
    with pytest.raises(psycopg.Error) as failed:
        _apply_evaluation(did, 9000, (ref,), _run("resolve-apply"))
    assert failed.value.sqlstate == "HC002"
    assert (_state(did), _ledger(did), _evaluation(did), _total(sid), _outbox(sid)) == before  # no event either


# ── reconcile (activation) ──────────────────────────────────────────────────────

def test_reconcile_needs_active_and_its_run_and_is_idempotent():
    set_accounting("frozen", "apply")
    did, pid = _paid(_creator())
    ref = _ref("refund")
    _evidence(did, pid, "refund", ref, "processed", 2500)
    run = _run("reconcile")
    with pytest.raises(REFUSED):
        _reconcile(did, 2500, (ref,), run)
    set_accounting("active")
    with pytest.raises(REFUSED):
        _reconcile(did, 2500, (ref,), _run("revert"))
    assert _reconcile(did, 2500, (ref,), run) == "activation"
    assert _runs_of(did) == [("activation", run)]
    assert _reconcile(did, 2500, (ref,), run) == "unchanged"


def test_an_accounting_row_of_a_manual_cause_needs_a_run():
    did, _ = _paid(_creator())
    with fixture_db() as conn, pytest.raises(psycopg.errors.CheckViolation) as refused, \
            conn.transaction(force_rollback=True):
        conn.execute("INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause, "
                     "payment_status, moderation_status, credited_before, credited_after, delta_inr, delta_count, "
                     "actor, reversed_before_paise, reversed_after_paise, hold_before, hold_after) SELECT id, "
                     "streamer_id, 'fan', 'manual', 'success', 'approved', credited_inr, credited_inr, 0, 0, 'x', 0, "
                     "0, 'H-CONFLICT', NULL FROM public.donations WHERE id = %s", (did,))
    assert refused.value.diag.constraint_name == "donation_credit_events_run"


# ── realtime events of owner operations (Revision 2 §3.3, F-5) ─────────────────

def test_owner_operations_that_move_money_emit_goal_and_leaderboard_like_the_app():
    """apply-evaluation, reconcile and revert insert exactly emit_totals_changed's two rows after the writer moved
    the total; a repeat ('unchanged') emits nothing; a pending event coalesces a second change."""
    set_accounting("active", "apply")
    sid = _creator()
    did, pid = _paid(sid)
    ref = _ref("refund")
    _evidence(did, pid, "refund", ref, "processed", 2500)
    _apply(did, None, hold="H-CURRENCY")
    assert _outbox(sid) == []                                           # a hold moves no money: no event
    run = _run("resolve-apply")
    assert _apply_evaluation(did, 2500, (ref,), run) == "manual" and _total(sid) == Decimal("75.00")
    assert _outbox(sid) == _totals_changed(sid)
    assert _apply_evaluation(did, 2500, (ref,), run) == "unchanged" and _outbox(sid) == _totals_changed(sid)

    other, other_pid = _paid(sid, name="Fan2")                          # same creator, events still pending
    other_ref = _ref("refund")
    _evidence(other, other_pid, "refund", other_ref, "processed", 1000)
    assert _reconcile(other, 1000, (other_ref,), _run("reconcile")) == "activation"
    assert _outbox(sid) == _totals_changed(sid)                         # coalesced into the pending pair

    _published(sid)
    set_accounting("frozen")
    revert = _run("revert")
    assert _revert(did, revert) == "reverted" and _total(sid) == Decimal("100.00")
    published = [(e, c, k, "published", m) for e, c, k, _, m in _totals_changed(sid)]
    assert _outbox(sid) == published + _totals_changed(sid)             # a new pair once the first was sent
    assert _revert(did, revert) == "unchanged" and len(_outbox(sid)) == 4


def test_operations_that_move_no_money_emit_nothing():
    """keep (money unchanged), a revert of a hold-only donation, the automatic SQL path (the app emits for it) and a
    reconcile that changes nothing insert no realtime event."""
    set_accounting("active", "hold")
    sid = _creator()
    did, pid = _paid(sid)
    _evidence(did, pid, "dispute", _ref("dispute"), "lost", 10000, deducted=4000)
    assert _apply(did, None, hold="H-DISPUTE-MODE") == "hold"
    assert _keep(did, _run("resolve-keep")) == "kept"
    clear, clear_pid = _paid(sid, name="Fan2")
    clear_ref = _ref("refund")
    _evidence(clear, clear_pid, "refund", clear_ref, "processed", 3000)
    assert _apply(clear, 3000, refs=(clear_ref,)) == "evaluation" and _total(sid, "Fan2") == Decimal("70.00")
    assert _reconcile(clear, 3000, (clear_ref,), _run("reconcile")) == "unchanged"
    held, _ = _paid(sid, name="Fan3")
    _apply(held, None, hold="H-CONFLICT")
    set_accounting("frozen")
    assert _revert(held, _run("revert")) == "reverted"                  # hold cleared, reversal was 0
    assert _outbox(sid) == []
