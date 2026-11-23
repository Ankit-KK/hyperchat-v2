"""PAY-15 stage 6a-3 on PostgreSQL 15 / 17: the operator CLI (state, reconcile, resolve, revert) end to end.

Every write goes through 0034's owner functions, bound to its run's scope by 0035 (F-11). The shared test database
holds other suites' donations, so every call names its donations (--donation)."""

import json
import os
import secrets
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.payments import operator
from tests.integration import PG_HOST, db, fixture_db, requires_db, set_accounting, target
from tests.payments.test_accounting_db import _apply, _creator, _evidence, _ledger, _paid, _ref, _settle, _state

pytestmark = [requires_db, pytest.mark.usefixtures("env", "accounting_active")]  # 0034 state active, mode apply
OPERATOR = "op-cli-test"
REASON = "a 6a-3 CLI test run"


@pytest.fixture
def cli(monkeypatch, capsys):
    name, port = target()
    monkeypatch.setenv("HYPERCHAT_ACCOUNTING_OWNER_DSN", f"host={PG_HOST} port={port} dbname={name} "
                                                         "user=hyperchat_migrator")

    def run(*argv, apply=False, confirm=None):
        args = list(argv)
        if apply:
            args += ["--apply", "--confirm-database", confirm or name, "--operator", OPERATOR, "--reason", REASON]
        code = operator.main(args)
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert out["exit_code"] == code
        return code, out
    return run


def _runs_by_cli() -> list[tuple]:
    with fixture_db() as conn:
        return conn.execute("SELECT id, command, scope, created_at FROM public.payment_accounting_runs "
                            "WHERE operator = %s ORDER BY id", (OPERATOR,)).fetchall()


def _assert_scoped(run_id: int, dids: list[str]):
    """OD-6a3-S: the CLI opens only "donations" runs naming exactly its donations, expiring within the hour."""
    (row,) = [r for r in _runs_by_cli() if r[0] == run_id]
    scope, created = row[2], row[3]
    assert set(scope) == {"kind", "donations", "expires_at"} and scope["kind"] == "donations", scope
    assert scope["donations"] == dids
    expires = datetime.fromisoformat(scope["expires_at"])
    assert created <= expires <= created + timedelta(hours=1, minutes=1)


def test_reconcile_reports_applies_exactly_once_and_its_second_report_is_zero(cli):
    """F13: report -> apply -> the second report plans zero; the run is scoped to exactly the planned donation."""
    did, pid = _paid(_creator())
    ref = _ref("refund")
    _evidence(did, pid, "refund", ref, "processed", 2500)  # recorded, never evaluated
    code, report = cli("reconcile", "--donation", did)
    assert code == 3 and report["status"] == "planned" and report["mode"] == "report"
    (item,) = report["planned"]
    assert item["donation"] == did and item["reversed"] == 2500 and item["refs"] == [ref]
    assert _state(did)[:2] == (0, None)  # a report writes nothing
    code, applied = cli("reconcile", "--donation", did, apply=True)
    assert code == 0 and applied["results"] == [{"donation": did, "outcome": "activation"}], applied
    assert applied["second_report"] == []
    _assert_scoped(applied["run"], [did])
    assert _state(did)[:2] == (2500, None)
    assert [r[0] for r in _ledger(did)].count("activation") == 1
    code, again = cli("reconcile", "--donation", did)
    assert code == 0 and again["planned"] == []  # nothing left to reconcile


def test_apply_without_its_confirmations_or_on_another_database_is_refused_and_writes_nothing(cli, capsys):
    did, pid = _paid(_creator())
    _evidence(did, pid, "refund", _ref("refund"), "processed", 2500)
    before = len(_runs_by_cli())
    for argv in (["reconcile", "--donation", did, "--apply"],
                 ["reconcile", "--donation", did, "--apply", "--confirm-database", "x", "--operator", OPERATOR],
                 ["reconcile", "--donation", did, "--apply", "--confirm-database", "x", "--operator", "Bad Op",
                  "--reason", REASON]):
        assert operator.main(argv) == 2
        capsys.readouterr()
    code, out = cli("reconcile", "--donation", did, apply=True, confirm="hyperchat_not_this_one")
    assert code == 2 and "confirm-database" in out["reason"]
    assert len(_runs_by_cli()) == before and _state(did)[:2] == (0, None)


def test_resolve_keep_releases_only_a_dispute_mode_hold(cli):
    set_accounting("active", "hold")
    did, pid = _paid(_creator())
    _evidence(did, pid, "dispute", _ref("dispute"), "lost", 10000, deducted=4000)
    assert _settle(did) == "hold"
    code, report = cli("resolve", "keep", "--donation", did)
    assert code == 3 and report["planned"] == [{"donation": did, "hold": "H-DISPUTE-MODE"}]
    code, applied = cli("resolve", "keep", "--donation", did, apply=True)
    assert code == 0 and applied["results"] == [{"donation": did, "outcome": "kept"}], applied
    _assert_scoped(applied["run"], [did])
    assert _state(did)[:2] == (0, None)


def test_resolve_apply_applies_a_hold_whose_evidence_is_now_clear(cli):
    did, pid = _paid(_creator())
    ref = _ref("refund")
    _evidence(did, pid, "refund", ref, "processed", 2500)
    _apply(did, None, hold="H-CURRENCY")  # held earlier although the evidence is now clear
    code, report = cli("resolve", "apply", "--donation", did)
    assert code == 3 and report["planned"] == [{"donation": did, "reversed": 2500, "flags": [], "refs": [ref]}]
    code, applied = cli("resolve", "apply", "--donation", did, apply=True)
    assert code == 0 and applied["results"] == [{"donation": did, "outcome": "manual"}], applied
    assert _state(did)[:2] == (2500, None)


def test_revert_is_refused_while_active_and_restores_the_credit_when_frozen(cli):
    did, pid = _paid(_creator())
    _evidence(did, pid, "refund", _ref("refund"), "processed", 4000)
    assert _settle(did) == "evaluation" and _state(did)[:2] == (4000, None)
    code, report = cli("revert", "--donation", did)
    assert code == 3 and report["planned"] == [{"donation": did, "from": [4000, None], "to": [0, None]}]
    runs = len(_runs_by_cli())
    code, out = cli("revert", "--donation", did, apply=True)
    assert code == 2 and "frozen or off" in out["reason"] and len(_runs_by_cli()) == runs  # nothing opened
    set_accounting("frozen")
    code, applied = cli("revert", "--donation", did, apply=True)
    assert code == 0 and applied["results"] == [{"donation": did, "outcome": "reverted"}], applied
    assert _state(did) == (0, None, [], Decimal("100.00"))


def test_a_donation_the_database_refuses_stops_the_run_with_exit_1(cli, monkeypatch):
    """Failure injection: a planned amount the stored evidence does not support (the core re-derives it): the database
    refuses, nothing is written for it, the CLI stops with exit 1; the run it opened stays scoped."""
    from app.payments import accounting_ops
    did, pid = _paid(_creator())
    _evidence(did, pid, "refund", _ref("refund"), "processed", 2500)
    real = accounting_ops.plan_item
    monkeypatch.setattr(accounting_ops, "plan_item", lambda *a: {**real(*a), "reversed": 9999})
    code, out = cli("reconcile", "--donation", did, apply=True)
    assert code == 1 and out["status"] == "failed" and out["results"][0]["donation"] == did, out
    assert "refused" in out["results"][0] and _state(did)[:2] == (0, None)
    _assert_scoped(out["run"], [did])
    set_accounting("frozen")
    code, out = cli("reconcile", "--donation", did, apply=True)
    assert code == 2 and "active" in out["reason"]  # the state is checked before any run is opened


def test_state_reports_and_changes_the_switch_with_its_reason(cli):
    code, report = cli("state")
    assert code == 0 and report["state"] == "active" and report["planned"] is None
    code, report = cli("state", "--to", "frozen")
    assert code == 3 and report["planned"] == {"to": "frozen", "dispute_mode": None}
    code, applied = cli("state", "--to", "frozen", apply=True)
    assert code == 0 and applied["results"] == [{"outcome": "frozen"}]
    with db() as conn:  # 0034 records the change in an event and clears change_operator on the state row
        assert conn.execute("SELECT state FROM public.payment_accounting_state").fetchone() == ("frozen",)
        assert conn.execute("SELECT to_state, operator, reason FROM public.payment_accounting_events "
                            "ORDER BY id DESC LIMIT 1").fetchone() == ("frozen", OPERATOR, REASON)


def test_the_cli_never_opens_an_unscoped_run(cli):
    """OD-6a3-S condition: whatever ran above, every run this CLI opened is a non-empty donations scope."""
    did, pid = _paid(_creator())
    _evidence(did, pid, "refund", _ref("refund"), "processed", 1000 + secrets.randbelow(1000))
    cli("reconcile", "--donation", did, apply=True)
    runs = _runs_by_cli()
    assert runs
    for run_id, command, scope, created in runs:
        assert scope.get("kind") == "donations" and scope.get("donations"), (run_id, command, scope)
    assert os.environ["HYPERCHAT_ACCOUNTING_OWNER_DSN"]
