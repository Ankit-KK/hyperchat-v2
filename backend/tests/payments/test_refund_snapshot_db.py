"""PAY-15 (D17, D9 / R6) stage 5b on PostgreSQL: the payment's refunded-amount snapshot (migration 0033).

Captured from the signed webhook only (the real route), validated by app.payments.integrity (else NULL, never 0),
classified by payment-integrity-report per provider payment: S (latest valid snapshot by provider event time) vs P / Q
(processed / pending amounts of that payment's current refund references). Diagnostic only: no payment status,
evaluation, reversal, hold, credit, donor total, goal, revenue or CSV value ever depends on it."""

import asyncio
import json
from argparse import Namespace
from contextlib import asynccontextmanager
from decimal import Decimal

import psycopg
import pytest

from app.cli import connection_factory, payment_integrity_report
from app.core.config import Settings
from app.payments.repository import PaymentRepository
from tests.integration import db, requires_db, update_streamer
from tests.payments.fakes import PAYER_CONTACT, PAYER_EMAIL, integrity_body
from tests.payments.test_figures_db import _csv, _goal, _stats
from tests.payments.test_integrity_db import paid, post
from tests.payments.test_payments_db import (  # noqa: F401 (fixtures)
    W,
    checkout,
    client,
    donation,
    rz_env,
    totals,
    webhook,
    wire,
)

pytestmark = requires_db


def _refund(client, out, payment_id, event_id, *, ref, amount, refunded, event="refund.processed", at=1_800_000_000,
            **payment):
    raw = integrity_body(event, ref=ref, payment_id=payment_id, order_id=out["razorpay_order_id"], amount=amount,
                         created_at=at, payment={"amount": 10000, "amount_refunded": refunded, **payment})
    return post(client, raw, event_id).json()["status"]


def _report() -> dict:
    return asyncio.run(PaymentRepository(connection_factory(Settings())).refund_snapshot_report())


def _category(payment_id: str) -> tuple[str, dict]:
    for name, rows in _report().items():
        for row in rows:
            if row["provider_payment_id"] == payment_id:
                return name, row
    raise AssertionError(f"{payment_id} not in the report")


def _stored(ref: str) -> list:
    with db() as conn:
        return [r for (r,) in conn.execute("SELECT payment_amount_refunded_paise FROM public.payment_integrity_events "
                                           "WHERE provider_ref = %s ORDER BY recorded_at, id", (ref,))]


# ── capture through the signed route ────────────────────────────────────────────

def test_the_snapshot_is_stored_from_the_signed_webhook_for_refunds_and_disputes(client):
    sid, out, payment = paid(client, "authtest-snap-store")
    assert _refund(client, out, payment.id, "authtest-snap-store-1", ref="rfnd_TESTsnap1", amount=2500,
                   refunded=2500) == "integrity_recorded"
    raw = integrity_body("payment.dispute.created", ref="disp_TESTsnap1", payment_id=payment.id,
                         order_id=out["razorpay_order_id"], payment={"amount": 10000, "amount_refunded": 2500})
    assert post(client, raw, "authtest-snap-store-2").json()["status"] == "integrity_recorded"
    assert _stored("rfnd_TESTsnap1") == [2500] and _stored("disp_TESTsnap1") == [2500]


@pytest.mark.parametrize("payment", [{"id": "pay_TESTnotthisone"}, {"amount_refunded": True},
                                     {"amount_refunded": 20000}, {"currency": "USD"}])
def test_an_unusable_snapshot_is_stored_as_null_and_the_event_is_recorded_as_before(client, payment):
    sid, out, pay = paid(client, "authtest-snap-null")
    ref = "rfnd_TESTnull" + str(abs(hash(json.dumps(payment))) % 10**6)
    status = _refund(client, out, pay.id, f"authtest-snap-null-{ref}", ref=ref, amount=2500, refunded=2500, **payment)
    assert status == "integrity_recorded" and _stored(ref) == [None]  # PAY-13 outcome unchanged
    name, row = _category(pay.id)
    assert name == "no_snapshot" and row["processed_refunds_paise"] == 2500


# ── classification ──────────────────────────────────────────────────────────────

def test_consistent_missing_pending_and_behind_are_classified_per_payment(client):
    _, out_a, a = paid(client, "authtest-snap-ok")
    _, out_b, b = paid(client, "authtest-snap-miss")
    _, out_c, c = paid(client, "authtest-snap-pend")
    _refund(client, out_a, a.id, "authtest-snap-ok-1", ref="rfnd_TESTok1", amount=2500, refunded=2500)
    _refund(client, out_b, b.id, "authtest-snap-miss-1", ref="rfnd_TESTmiss1", amount=2500, refunded=6000)
    _refund(client, out_c, c.id, "authtest-snap-pend-1", ref="rfnd_TESTpend1", amount=3000, refunded=3000,
            event="refund.created")
    assert _category(a.id)[0] == "consistent"
    name, row = _category(b.id)
    assert name == "missing_refund_evidence" and (row["snapshot_refunded_paise"], row["processed_refunds_paise"],
                                                  row["pending_refunds_paise"]) == (6000, 2500, 0)
    assert _category(c.id)[0] == "pending_refunds"


def test_the_latest_snapshot_follows_the_provider_event_time_not_the_delivery_order(client):
    """Out of order: the newer event (t=2) is delivered first; the older one (t=1) arrives later and must not win.
    Then S (2500) < P (3500): snapshot_behind."""
    _, out, pay = paid(client, "authtest-snap-order")
    _refund(client, out, pay.id, "authtest-snap-order-2", ref="rfnd_TESTord2", amount=2500, refunded=2500,
            at=1_800_000_200)
    _refund(client, out, pay.id, "authtest-snap-order-1", ref="rfnd_TESTord1", amount=1000, refunded=1000,
            at=1_800_000_100)
    name, row = _category(pay.id)
    assert (name, row["snapshot_refunded_paise"], row["processed_refunds_paise"]) == ("snapshot_behind", 2500, 3500)


def test_a_snapshot_without_a_provider_time_ranks_after_timed_ones(client):
    _, out, pay = paid(client, "authtest-snap-notime")
    _refund(client, out, pay.id, "authtest-snap-notime-1", ref="rfnd_TESTnt1", amount=2500, refunded=2500,
            at=1_800_000_100)
    _refund(client, out, pay.id, "authtest-snap-notime-2", ref="rfnd_TESTnt2", amount=1000, refunded=9999, at=None)
    name, row = _category(pay.id)  # the untimed 9999 does not override the timed 2500
    assert row["snapshot_refunded_paise"] == 2500 and row["processed_refunds_paise"] == 3500


def test_duplicates_and_repeated_states_count_once(client):
    """A replayed event id records nothing; one reference moving pending -> processed counts once, in P only."""
    _, out, pay = paid(client, "authtest-snap-dup")
    _refund(client, out, pay.id, "authtest-snap-dup-1", ref="rfnd_TESTdup1", amount=2500, refunded=2500,
            event="refund.created", at=1_800_000_100)
    _refund(client, out, pay.id, "authtest-snap-dup-2", ref="rfnd_TESTdup1", amount=2500, refunded=2500,
            at=1_800_000_200)
    assert _refund(client, out, pay.id, "authtest-snap-dup-2", ref="rfnd_TESTdup1", amount=2500,
                   refunded=7777) == "duplicate"
    name, row = _category(pay.id)
    assert (name, row["processed_refunds_paise"], row["pending_refunds_paise"], row["snapshot_refunded_paise"]) == (
        "consistent", 2500, 0, 2500)


def test_one_payment_never_affects_another(client):
    _, out_a, a = paid(client, "authtest-snap-iso-a")
    _, out_b, b = paid(client, "authtest-snap-iso-b")
    _refund(client, out_a, a.id, "authtest-snap-iso-a1", ref="rfnd_TESTisoA", amount=4000, refunded=9000)
    _refund(client, out_b, b.id, "authtest-snap-iso-b1", ref="rfnd_TESTisoB", amount=1000, refunded=1000)
    assert _category(a.id)[0] == "missing_refund_evidence"
    name, row = _category(b.id)
    assert (name, row["processed_refunds_paise"], row["snapshot_refunded_paise"]) == ("consistent", 1000, 1000)


# ── report: exit code, privacy, read-only ───────────────────────────────────────

def test_the_report_exits_1_for_missing_refund_evidence_and_prints_no_names_or_payloads(client, capsys):
    _, out, pay = paid(client, "authtest-snap-cli")
    _refund(client, out, pay.id, "authtest-snap-cli-1", ref="rfnd_TESTcli1", amount=2500, refunded=8000)
    code = asyncio.run(payment_integrity_report(Settings(), Namespace(since=None)))
    text = capsys.readouterr().out
    report = json.loads(text)
    snaps = report["refund_snapshots"]
    assert code == 1 and report["read_only"] is True
    assert pay.id in {r["provider_payment_id"] for r in snaps["missing_refund_evidence"]}
    assert set(snaps["counts"]) == {"missing_refund_evidence", "pending_refunds", "snapshot_behind", "consistent",
                                    "no_snapshot"}
    for secret in ("Ravi", PAYER_EMAIL, PAYER_CONTACT, "acc_TESTONLY", '"entity"'):
        assert secret not in text, secret
    assert {"items", "accounting", "attention", "total", "since"} <= set(report)  # existing sections kept
    assert asyncio.run(payment_integrity_report(Settings(), Namespace(since="nope"))) == 2


def test_the_snapshot_report_runs_on_a_read_only_session():
    """Right after the report's own READ ONLY setting, a write on the same connection is refused (25006)."""
    refused: list[str] = []
    real = connection_factory(Settings())

    class Probe:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        async def execute(self, sql, *args, **kwargs):
            result = await self._conn.execute(sql, *args, **kwargs)
            if str(sql).startswith("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"):
                try:
                    await self._conn.execute("DELETE FROM public.rate_limits WHERE false")
                except psycopg.errors.ReadOnlySqlTransaction as exc:
                    refused.append(exc.sqlstate)
            return result

    @asynccontextmanager
    async def probing():
        async with real() as conn:
            yield Probe(conn)
    asyncio.run(PaymentRepository(probing).refund_snapshot_report())
    assert refused == ["25006"]


# ── financial isolation ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("accounting", [False, True])
def test_a_snapshot_mismatch_changes_no_money(client, accounting):
    """The same refund (Rs 25) with an honest snapshot and with a wildly wrong one (Rs 90): every money value is the
    same; with accounting off nothing is reversed, with it injected on only the evidence's Rs 25 is."""
    client.app_state.payments.repo.accounting = accounting
    results = []
    for slug, refunded in (("authtest-snap-iso-ok", 2500), ("authtest-snap-iso-bad", 9000)):
        sid, out, pay = paid(client, f"{slug}-{int(accounting)}")
        update_streamer(sid, goal_is_active=True, goal_activated_at="2026-01-01T00:00:00+00:00",
                        goal_target_amount=1000, goal_name="Mic")
        _refund(client, out, pay.id, f"{slug}-{int(accounting)}-1", ref=f"rfnd_TEST{slug[-2:]}{int(accounting)}x",
                amount=2500, refunded=refunded)
        d = donation(out["order_id"])
        with db() as conn:
            rev = conn.execute("SELECT applied_reversed_paise, hold, flags FROM public.donation_reversals "
                               "WHERE donation_id = %s", (d["id"],)).fetchone()
            credited = conn.execute("SELECT credited_inr FROM public.donations WHERE id = %s", (d["id"],)).fetchone()[0]
        stats = _stats(sid)
        results.append((d["payment_status"], d["moderation_status"], rev, credited, totals(sid), _goal(sid),
                        stats["total_revenue_inr"], stats["total_reversed_inr"],
                        [(r["amount_inr"], r["reversed_inr"], r["net_inr"]) for r in _csv(sid)]))
    honest, wrong = results
    assert honest == wrong  # the snapshot changes nothing
    expected = Decimal("75.00") if accounting else Decimal("100.00")
    assert honest[0] == "success" and honest[3] == expected and honest[4] == [("Ravi", expected, 1)]
    assert honest[2] == ((2500, None, []) if accounting else None) and honest[5] == expected
