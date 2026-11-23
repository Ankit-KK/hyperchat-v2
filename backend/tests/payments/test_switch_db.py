"""PAY-15 (D17) stage 6a-4 on PostgreSQL 15 / 17: the switch gate (F12), activation idempotence (F13), the
counterfactual revert (F14), the 0035 startup precondition and the L4 deploy check.

Evaluation needs BOTH the setting (PaymentRepository(accounting=...)) and the database state 'active'. The shared test
database holds other suites' donations and reversals, so every operator call names its donations."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from psycopg.rows import dict_row

from app.payments import operator
from app.payments.repository import PaymentRepository
from app.payments.switch import WRITER_MIGRATION, require_hardening
from tests.integration import PG_HOST, db, requires_db, set_accounting, target
from tests.jobs.test_jobs_db import factory, run
from tests.payments.test_accounting_db import _creator, _evidence, _ledger, _paid, _ref, _state, _total
from tests.payments.test_operator_db import cli  # noqa: F401 (fixture)

pytestmark = [requires_db, pytest.mark.usefixtures("env", "accounting_active")]  # 0034 state active, mode apply


def _evaluate(did: str, enabled: bool) -> str | None:
    """The application path (PaymentRepository._settle_accounting) with the setting `enabled`, in one transaction."""
    async def go():
        repo = PaymentRepository(factory(), accounting=enabled)
        async with repo._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            return await repo._settle_accounting(cur, did)
    return run(go())


def _moderate(did: str, *statuses: str) -> None:
    with db() as conn:
        for status in statuses:
            conn.execute("UPDATE public.donations SET moderation_status = %s WHERE id = %s", (status, did))


def test_the_0035_precondition_passes_on_this_database():
    run(require_hardening(factory()))  # raises PreconditionError otherwise


def test_f12_switch_off_evaluates_nothing_keeps_applied_reversals_and_moderation_uses_the_frozen_value():
    sid = _creator()
    did, pid = _paid(sid)
    _evidence(did, pid, "refund", _ref("refund"), "processed", 4000)
    assert _evaluate(did, True) == "evaluation" and _state(did)[:2] == (4000, None)
    assert _total(sid) == Decimal("60.00")
    _evidence(did, pid, "refund", _ref("refund"), "processed", 2000, at=1)  # new evidence after the switch goes off
    history = _ledger(did)
    assert _evaluate(did, False) is None  # setting off: no evaluation at all
    assert _state(did)[:2] == (4000, None) and _ledger(did) == history
    _moderate(did, "rejected")
    assert _state(did)[3] == Decimal("0.00")  # a rejected donation credits nothing
    _moderate(did, "approved")  # the writer uses the frozen applied reversal (4000), not the new evidence
    assert _state(did) == (4000, None, [], Decimal("60.00")) and _total(sid) == Decimal("60.00")
    set_accounting("frozen")  # setting on, state frozen: still nothing is evaluated, nothing moves
    history = _ledger(did)
    assert _evaluate(did, True) == "inactive" and _state(did)[:2] == (4000, None) and _ledger(did) == history


def test_f13_activation_is_idempotent_through_the_6a3_reconcile(cli):
    set_accounting("frozen")  # evidence arrives while nothing is evaluated
    sid = _creator()
    late, late_pid = _paid(sid, name="FanLate")
    current, current_pid = _paid(sid, name="FanCurrent")
    late_ref = _ref("refund")
    _evidence(late, late_pid, "refund", late_ref, "processed", 3000)
    set_accounting("active")
    _evidence(current, current_pid, "refund", _ref("refund"), "processed", 1000)
    assert _evaluate(current, True) == "evaluation"  # already consistent: never planned
    code, report = cli("reconcile", "--donation", late, "--donation", current)
    assert code == 3 and [(x["donation"], x["reversed"], x["refs"]) for x in report["planned"]] == [
        (late, 3000, [late_ref])]
    code, applied = cli("reconcile", "--donation", late, "--donation", current, apply=True)
    assert code == 0 and applied["second_report"] == [], applied
    code, again = cli("reconcile", "--donation", late, "--donation", current)
    assert code == 0 and again["planned"] == []  # a second reconcile plans zero deltas
    assert _state(late)[:2] == (3000, None) and _state(current)[:2] == (1000, None)


def test_f14_revert_restores_exactly_the_counterfactual_and_leaves_everything_else(cli):
    sid = _creator()
    a, a_pid = _paid(sid, name="FanA")
    b, b_pid = _paid(sid, name="FanB")
    _evidence(a, a_pid, "refund", _ref("refund"), "processed", 4000)
    _evidence(b, b_pid, "refund", _ref("refund"), "processed", 2500)
    assert _evaluate(a, True) == _evaluate(b, True) == "evaluation"
    _moderate(a, "rejected", "approved")  # interleaved moderation and a new payment by the same donor
    a2, _ = _paid(sid, amount=50, name="FanA")
    assert (_total(sid, "FanA"), _total(sid, "FanB")) == (Decimal("110.00"), Decimal("75.00"))
    before = {d: _ledger(d) for d in (a, a2, b)}
    set_accounting("frozen")
    code, applied = cli("revert", "--donation", a, apply=True)
    assert code == 0 and applied["results"] == [{"donation": a, "outcome": "reverted"}], applied
    # the counterfactual: the same history with no evidence-based reversal (A approved, A2 paid, B untouched)
    assert _state(a) == (0, None, [], Decimal("100.00")) and _state(a2)[3] == Decimal("50.00")
    assert _state(b) == (2500, None, [], Decimal("75.00"))
    assert (_total(sid, "FanA"), _total(sid, "FanB")) == (Decimal("150.00"), Decimal("75.00"))
    after = {d: _ledger(d) for d in (a, a2, b)}
    assert after[a][:len(before[a])] == before[a] and [r[0] for r in after[a][len(before[a]):]] == ["revert"]
    assert after[a2] == before[a2] and after[b] == before[b]  # ledger rows only grow; --donation scope respected


def _release(root: Path, commit: str, writer: bool) -> None:
    versions = root / commit / "database" / "migrations" / "versions"
    versions.mkdir(parents=True)
    if writer:
        (root / commit / WRITER_MIGRATION).write_text("# 0032\n")


def test_l4_refuses_a_rollback_below_the_writer_while_a_reversal_is_applied(tmp_path, monkeypatch, capsys):
    did, pid = _paid(_creator())
    _evidence(did, pid, "refund", _ref("refund"), "processed", 1500)
    assert _evaluate(did, True) == "evaluation"  # at least one applied reversal exists
    name, port = target()
    monkeypatch.setenv("HYPERCHAT_READONLY_DSN", f"host={PG_HOST} port={port} dbname={name} user=hyperchat_readonly")
    old, new = "a" * 40, "b" * 40
    _release(tmp_path, old, writer=False)
    _release(tmp_path, new, writer=True)

    def check(commit):
        code = operator.main(["deploy-check", "--target-release", commit, "--releases-root", str(tmp_path)])
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert out["exit_code"] == code
        return code, out
    code, out = check(old)
    assert code == 2 and out["carries_writer_rule"] is False and out["applied_reversals"] >= 1
    assert "revert them first" in out["reason"]
    code, out = check(new)
    assert code == 0 and out["carries_writer_rule"] is True and out["status"] == "clean"
    monkeypatch.setenv("HYPERCHAT_READONLY_DSN", f"host={PG_HOST} port={port} dbname={name} user=nobody_at_all")
    assert check(new)[0] == 2  # a database it cannot read refuses (fail closed)
