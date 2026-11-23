"""PAY-15 stage 6a-4 without a database: the 0035 precondition on a scripted connection, the writer-rule marker of a
release tree, and the deploy check's refusals before any connection."""

import asyncio
import json
from argparse import Namespace
from contextlib import asynccontextmanager

import pytest

from app.payments import operator, switch

GOOD = {"scope_covers": True, "ack_guard": True, "foreign_keys": True}


def _connect(row: dict | None = None, error: Exception | None = None):
    class Cur:
        description = [Namespace(name=k) for k in (row or GOOD)]

        async def fetchone(self):
            return tuple((row or GOOD).values())

    class Conn:
        async def execute(self, sql):
            assert sql == switch.HARDENING
            if error:
                raise error
            return Cur()

    @asynccontextmanager
    async def connect():
        yield Conn()
    return connect


def test_the_precondition_passes_only_with_every_0035_object():
    asyncio.run(switch.require_hardening(_connect()))
    for name in GOOD:
        with pytest.raises(switch.PreconditionError, match=f"lacks migration 0035.*{name}"):
            asyncio.run(switch.require_hardening(_connect({**GOOD, name: False})))


def test_the_precondition_fails_closed_when_the_database_cannot_be_checked():
    with pytest.raises(switch.PreconditionError, match="could not be checked.*OSError"):
        asyncio.run(switch.require_hardening(_connect(error=OSError("down"))))


def test_payment_reconcile_refuses_with_the_setting_on_and_no_0035(monkeypatch, capsys):
    from app import cli
    from app.core.config import Settings

    async def missing(connect):
        raise switch.PreconditionError("lacks migration 0035's hardening (missing: ack_guard)")
    monkeypatch.setattr(cli, "require_hardening", missing)
    args = Namespace(min_age_minutes=15, max_age_days=1, limit=10, apply=False, confirm_database=None, report_file=None)
    assert asyncio.run(cli.payment_reconcile(Settings(db_enabled=False, payment_accounting_enabled=True), args)) == 2
    assert "missing: ack_guard" in capsys.readouterr().err


def test_a_release_carries_the_writer_rule_iff_its_tree_has_migration_0032(tmp_path):
    old, new = "a" * 40, "b" * 40
    for commit in (old, new):
        (tmp_path / commit / "database" / "migrations" / "versions").mkdir(parents=True)
    (tmp_path / new / switch.WRITER_MIGRATION).write_text("# 0032\n")
    assert switch.carries_writer_rule(str(tmp_path), old) is False
    assert switch.carries_writer_rule(str(tmp_path), new) is True
    for bad in ("", "abc", "A" * 40, "c" * 40, "../" + "a" * 37):  # malformed, uppercase, missing, traversal
        with pytest.raises(ValueError):
            switch.carries_writer_rule(str(tmp_path), bad)


@pytest.mark.parametrize("argv", [["deploy-check"], ["deploy-check", "--target-release", "nope"],
                                  ["deploy-check", "--target-release", "a" * 40, "--releases-root", "/nonexistent"],
                                  ["deploy-check", "--bogus"]])
def test_the_deploy_check_refuses_before_connecting(argv, monkeypatch, capsys):
    monkeypatch.delenv("HYPERCHAT_READONLY_DSN", raising=False)
    assert operator.main(argv) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "refused" and out["exit_code"] == 2


def test_the_deploy_check_needs_the_readonly_dsn(tmp_path, monkeypatch, capsys):
    (tmp_path / ("a" * 40) / "database" / "migrations" / "versions").mkdir(parents=True)
    monkeypatch.delenv("HYPERCHAT_READONLY_DSN", raising=False)
    assert operator.main(["deploy-check", "--target-release", "a" * 40, "--releases-root", str(tmp_path)]) == 2
    assert "HYPERCHAT_READONLY_DSN" in json.loads(capsys.readouterr().out)["reason"]
