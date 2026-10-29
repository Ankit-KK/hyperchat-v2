"""C1 payment integrity evidence on PostgreSQL (migration 0029): the real app, webhook route and repository against a
migrated scratch database, with the fake Razorpay. Synthetic rows only (authtest-*)."""

import asyncio
import json
from argparse import Namespace

import httpx
import psycopg
import pytest

from app.cli import payment_integrity_report
from app.core.config import Settings
from app.storage.memory import InMemoryObjectStore
from tests.integration import creator_state, db, fixture_db, make_streamer, requires_db, update_streamer
from tests.payments.fakes import FakeRazorpay, integrity_body, sign, webhook_body
from tests.payments.test_payments_db import (  # noqa: F401 (fixtures)
    W,
    checkout,
    client,
    donation,
    rz_env,
    secret,
    totals,
    webhook,
    wire,
)

pytestmark = requires_db


def post(client, raw: bytes, event_id: str, secret_value: str | None = None):
    signature = sign(secret_value or secret(client, "PRIMARY_WEBHOOK_SECRET"), raw)
    return client.post(W, content=raw, headers={"x-razorpay-signature": signature, "x-razorpay-event-id": event_id,
                                                "content-type": "application/json"})


def paid(client, slug: str) -> tuple[str, dict, object]:
    sid = make_streamer(slug)
    out = checkout(client, slug)
    payment = client.fake.captured(out["razorpay_order_id"], 10000)
    capture_id = slug.replace("authtest-", "authtest-cap-", 1)  # outside the test's own evidence prefix
    assert webhook(client, "payment.captured", out, payment.id, event_id=capture_id).json() == {"status": "completed"}
    return sid, out, payment


def financial_state(sid: str) -> tuple:
    """Donations, moderators, bans, donor totals (leaderboard), moderation audit, plus jobs and realtime events."""
    with db() as conn:
        extra = conn.execute("SELECT (SELECT count(*) FROM public.jobs j JOIN public.donations d ON d.id::text = "
                             "j.dedupe_key WHERE d.streamer_id = %(s)s), (SELECT count(*) FROM public.realtime_outbox "
                             "WHERE streamer_id = %(s)s)", {"s": sid}).fetchone()
    return creator_state(sid), extra


def evidence(prefix: str) -> list[dict]:
    with db() as conn:
        cur = conn.execute("""
            SELECT w.event_id, w.outcome, w.donation_id::text AS event_donation, i.donation_id::text AS donation_id,
                   i.match_type, i.account, i.kind, i.event_type, i.provider_ref, i.provider_payment_id, i.phase,
                   i.status, i.amount_paise, i.currency, i.consistent
            FROM public.payment_webhook_events w
            LEFT JOIN public.payment_integrity_events i ON i.webhook_event_id = w.id
            WHERE w.event_id LIKE %s ORDER BY w.processed_at, w.event_id""", (prefix + "%",))
        return [dict(zip([c.name for c in cur.description], r)) for r in cur.fetchall()]


def current(ref: str) -> dict | None:
    with db() as conn:
        cur = conn.execute("SELECT * FROM public.payment_integrity_current WHERE provider_ref = %s", (ref,))
        row = cur.fetchone()
        return dict(zip([c.name for c in cur.description], row)) if row else None


def dispute(client, event: str, out, payment_id: str, event_id: str, ref: str = "disp_TESTa1", **kw):
    raw = integrity_body(event, ref=ref, payment_id=payment_id, order_id=out["razorpay_order_id"] if out else
                         kw.pop("order_id", None), **kw)
    return post(client, raw, event_id)


# ── recording ───────────────────────────────────────────────────────────────

def test_every_supported_event_is_evidence_and_no_financial_row_changes(client):
    sid, out, payment = paid(client, "authtest-int-all")
    before, row = financial_state(sid), donation(out["order_id"])
    events = [f"payment.dispute.{s}" for s in ("created", "under_review", "action_required", "won", "lost", "closed")]
    for i, event in enumerate(events):
        r = dispute(client, event, out, payment.id, f"authtest-int-all-d{i}")
        assert (r.status_code, r.json()) == (200, {"status": "integrity_recorded"}), (event, r.text)
    for i, event in enumerate(("refund.created", "refund.processed", "refund.failed")):
        r = dispute(client, event, out, payment.id, f"authtest-int-all-r{i}", ref="rfnd_TESTa1", amount=4000)
        assert (r.status_code, r.json()) == (200, {"status": "integrity_recorded"}), (event, r.text)
    rows = evidence("authtest-int-all-")
    assert len(rows) == 9 and {r["outcome"] for r in rows} == {"integrity_recorded"}
    assert {(r["match_type"], r["donation_id"], r["event_donation"], r["account"], r["consistent"]) for r in rows} == {
        ("payment", row["id"], row["id"], "primary", True)}
    assert [r["event_type"] for r in rows] == events + ["refund.created", "refund.processed", "refund.failed"]
    assert rows[0]["phase"] == "chargeback" and rows[6]["phase"] is None and rows[6]["amount_paise"] == 4000
    assert financial_state(sid) == before  # donation, totals, goal inputs, leaderboard, audits, jobs, realtime
    assert donation(out["order_id"]) == row
    assert totals(sid) == [("Ravi", 100, 1)]


def test_refund_speed_changed_and_unknown_events_stay_ignored(client):
    sid, out, payment = paid(client, "authtest-int-ign")
    r = dispute(client, "refund.speed_changed", out, payment.id, "authtest-int-ign-1", ref="rfnd_TESTi1")
    assert r.json() == {"status": "ignored"} and evidence("authtest-int-ign-")[0]["match_type"] is None


def test_replays_are_duplicates_and_distinct_events_are_all_kept(client):
    sid, out, payment = paid(client, "authtest-int-dup")
    assert dispute(client, "payment.dispute.created", out, payment.id, "authtest-int-dup-1").json() == {
        "status": "integrity_recorded"}
    assert dispute(client, "payment.dispute.created", out, payment.id, "authtest-int-dup-1").json() == {
        "status": "duplicate"}
    assert dispute(client, "payment.dispute.created", out, payment.id, "authtest-int-dup-2").json() == {
        "status": "integrity_recorded"}  # another delivery of the same news: kept, the view counts both
    assert len(evidence("authtest-int-dup-")) == 2 and current("disp_TESTa1")["events"] >= 2


def test_bad_signatures_store_nothing(client):
    sid, out, payment = paid(client, "authtest-int-sig")
    raw = integrity_body("payment.dispute.lost", ref="disp_TESTs1", payment_id=payment.id,
                         order_id=out["razorpay_order_id"])
    assert post(client, raw, "authtest-int-sig-1", "TEST-ONLY-not-the-secret").status_code == 401
    r = client.post(W, content=raw, headers={"x-razorpay-event-id": "authtest-int-sig-2"})
    assert r.status_code == 401 and evidence("authtest-int-sig-") == [] and current("disp_TESTs1") is None


def test_unknown_payments_are_recorded_without_a_donation(client):
    r = dispute(client, "payment.dispute.created", None, "pay_TESTunknown1", "authtest-int-unk-1", ref="disp_TESTu1",
                order_id="order_TESTunknown1")
    assert r.json() == {"status": "integrity_unknown_payment"}
    (row,) = evidence("authtest-int-unk-")
    assert (row["match_type"], row["donation_id"], row["event_donation"], row["account"]) == ("none", None, None,
                                                                                               "primary")


def test_a_duplicate_capture_is_associated_by_order_never_attributed(client):
    sid, out, first = paid(client, "authtest-int-g3")
    second = client.fake.captured(out["razorpay_order_id"], 10000)  # a second capture on the paid order (G3)
    assert webhook(client, "payment.captured", out, second.id, event_id="authtest-int-g3-cap2").json() == {
        "status": "already_paid"}
    before = financial_state(sid)
    r = dispute(client, "payment.dispute.lost", out, second.id, "authtest-int-g3-d1", ref="disp_TESTg1",
                amount_deducted=10000)
    assert r.json() == {"status": "integrity_recorded"}
    row = evidence("authtest-int-g3-d")[0]
    assert (row["match_type"], row["donation_id"]) == ("order", donation(out["order_id"])["id"])
    assert current("disp_TESTg1")["match_type"] == "order" and financial_state(sid) == before


@pytest.mark.parametrize("kw,expected", [
    ({"currency": "USD"}, "integrity_mismatch"),
    ({"amount": 10001}, "integrity_mismatch"),        # more than the payment
    ({"omit": ("phase",)}, "integrity_mismatch"),     # an essential field missing
    ({"status": "reversed"}, "integrity_mismatch"),   # an unknown status: the event's own status is used
])
def test_mismatches_are_recorded_as_evidence_and_flagged(client, kw, expected):
    sid, out, payment = paid(client, "authtest-int-mm")
    before = financial_state(sid)
    assert dispute(client, "payment.dispute.lost", out, payment.id, "authtest-int-mm-1", ref="disp_TESTm1",
                   **kw).json() == {"status": expected}
    row = evidence("authtest-int-mm-1")[0]
    assert (row["consistent"], row["match_type"], row["status"]) == (False, "payment", "lost")
    assert current("disp_TESTm1")["all_consistent"] is False and financial_state(sid) == before


def test_a_dispute_on_an_unpaid_order_is_a_mismatch_and_changes_nothing(client):
    sid = make_streamer("authtest-int-unpaid")
    out = checkout(client, "authtest-int-unpaid")
    before = financial_state(sid)
    assert dispute(client, "payment.dispute.created", out, "pay_TESTnotours1", "authtest-int-unpaid-1",
                   ref="disp_TESTn1").json() == {"status": "integrity_mismatch"}
    row = evidence("authtest-int-unpaid-")[0]
    assert (row["match_type"], row["consistent"]) == ("order", False)
    assert donation(out["order_id"])["payment_status"] == "pending" and financial_state(sid) == before


def test_a_donation_of_another_or_no_account_is_refused_and_stores_no_evidence(client):
    sid = make_streamer("authtest-int-acct")
    out = checkout(client, "authtest-int-acct")
    payment = client.fake.captured(out["razorpay_order_id"], 10000)
    assert webhook(client, "payment.captured", out, payment.id, event_id="authtest-int-acct-cap").json() == {
        "status": "completed"}
    with fixture_db() as owner:  # a legacy row: no order account, and the creator has none any more
        owner.execute("UPDATE public.donations SET payment_account = NULL WHERE order_id = %s", (out["order_id"],))
    update_streamer(sid, razorpay_account=None)
    before = financial_state(sid)
    r = dispute(client, "payment.dispute.lost", out, payment.id, "authtest-int-acct-1", ref="disp_TESTx1")
    assert r.json() == {"status": "account_mismatch"}
    assert [(e["outcome"], e["match_type"]) for e in evidence("authtest-int-acct-1")] == [("account_mismatch", None)]
    assert current("disp_TESTx1") is None and financial_state(sid) == before


# ── ordering ────────────────────────────────────────────────────────────────

def test_out_of_order_reopened_and_conflicting_disputes_have_one_deterministic_state(client):
    sid, out, payment = paid(client, "authtest-int-ord")
    ref = "disp_TESTo1"

    def send(event, n, **kw):
        assert dispute(client, event, out, payment.id, f"authtest-int-ord-{n}", ref=ref, **kw).status_code == 200

    send("payment.dispute.won", 1, created_at=1_800_000_300)
    send("payment.dispute.created", 2, created_at=1_800_000_100)  # late: never undoes the later state
    assert (current(ref)["phase"], current(ref)["status"]) == ("chargeback", "won")
    send("payment.dispute.created", 3, phase="pre_arbitration", created_at=1_800_000_500)  # a won dispute reopens
    assert (current(ref)["phase"], current(ref)["status"]) == ("pre_arbitration", "open")
    send("payment.dispute.lost", 4, phase="chargeback", created_at=1_800_000_900)  # an earlier phase stays below
    assert (current(ref)["phase"], current(ref)["status"]) == ("pre_arbitration", "open")
    send("payment.dispute.lost", 5, phase="pre_arbitration", created_at=1_800_000_700)
    send("payment.dispute.won", 6, phase="pre_arbitration", created_at=1_800_000_800)  # contradicts the loss
    state = current(ref)
    assert (state["phase"], state["status"], state["conflicting"], state["events"]) == ("pre_arbitration", "won",
                                                                                        True, 6)
    send("payment.dispute.lost", 7, phase="pre_arbitration", created_at=1_800_000_800)  # a tie on provider time
    tied = current(ref)
    assert tied["status"] == "lost" and tied["conflicting"] is True  # later receipt wins the tie, deterministically


def test_refund_states_rank_processed_above_failed_above_pending(client):
    sid, out, payment = paid(client, "authtest-int-ref")
    for n, event in enumerate(("refund.processed", "refund.created", "refund.failed")):
        dispute(client, event, out, payment.id, f"authtest-int-ref-{n}", ref="rfnd_TESTr1", amount=2500)
    assert (current("rfnd_TESTr1")["status"], current("rfnd_TESTr1")["phase"]) == ("processed", None)


# ── concurrency ─────────────────────────────────────────────────────────────

def _concurrently(rz: dict, requests, fake: FakeRazorpay | None = None):
    """The real app in this test's own event loop (its own pool, as test_payments_db.race), requests in parallel."""
    async def scenario():
        from app.main import create_app

        application = create_app()
        async with application.router.lifespan_context(application):
            wire(application.state, fake or FakeRazorpay(), InMemoryObjectStore())
            transport = httpx.ASGITransport(app=application, client=("203.0.113.9", 4000))
            async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
                return await asyncio.gather(*(c.post(W, content=raw, headers={
                    "x-razorpay-signature": sign(rz["HYPERCHAT_RAZORPAY_PRIMARY_WEBHOOK_SECRET"], raw),
                    "x-razorpay-event-id": event_id, "content-type": "application/json"}) for raw, event_id in requests))
    return asyncio.run(scenario())


def test_concurrent_deliveries_record_once_and_concurrent_events_all_once(client):
    sid, out, payment = paid(client, "authtest-int-conc")
    before = financial_state(sid)
    raw = integrity_body("payment.dispute.created", ref="disp_TESTc1", payment_id=payment.id,
                         order_id=out["razorpay_order_id"])
    same = _concurrently(client.env, [(raw, "authtest-int-conc-1")] * 4)
    assert sorted(r.json()["status"] for r in same) == ["duplicate"] * 3 + ["integrity_recorded"]
    distinct = _concurrently(client.env, [
        (integrity_body(e, ref="disp_TESTc1", payment_id=payment.id, order_id=out["razorpay_order_id"]),
         f"authtest-int-conc-x{n}") for n, e in enumerate(("payment.dispute.under_review", "payment.dispute.lost",
                                                            "payment.dispute.closed"))])
    assert [r.json()["status"] for r in distinct] == ["integrity_recorded"] * 3
    assert len(evidence("authtest-int-conc-")) == 4 and financial_state(sid) == before


def test_a_dispute_racing_the_capture_completes_once_and_records_once(client):
    sid = make_streamer("authtest-int-race")
    out = checkout(client, "authtest-int-race")
    payment = client.fake.captured(out["razorpay_order_id"], 10000)
    captured, disputed = _concurrently(client.env, [
        (webhook_body("payment.captured", out["razorpay_order_id"], payment.id, 10000), "authtest-int-race-c"),
        (integrity_body("payment.dispute.created", ref="disp_TESTk1", payment_id=payment.id,
                        order_id=out["razorpay_order_id"]), "authtest-int-race-d")], fake=client.fake)
    assert captured.json() == {"status": "completed"}
    # before the completion commits it is an order match on an unpaid order (a mismatch), after it a payment match
    assert disputed.json()["status"] in ("integrity_recorded", "integrity_mismatch")
    assert totals(sid) == [("Ravi", 100, 1)] and donation(out["order_id"])["payment_status"] == "success"
    (row,) = evidence("authtest-int-race-d")
    assert (row["match_type"], row["consistent"]) in (("payment", True), ("order", False))


# ── append-only for every role ──────────────────────────────────────────────

def _refused(conn, sql: str, params=()) -> str:
    with pytest.raises(psycopg.Error) as exc, conn.transaction(force_rollback=True):
        conn.execute(sql, params)
    return exc.value.sqlstate


def test_evidence_is_append_only_for_every_role_and_never_truncated(client):
    sid, out, payment = paid(client, "authtest-int-ro")
    dispute(client, "payment.dispute.lost", out, payment.id, "authtest-int-ro-1", ref="disp_TESTro1")
    did = donation(out["order_id"])["id"]
    with db() as app_conn:  # hyperchat_app: SELECT / INSERT only
        for table in ("payment_integrity_events", "payment_webhook_events"):
            assert _refused(app_conn, f"UPDATE public.{table} SET received_at = now()") == "42501", table
            assert _refused(app_conn, f"DELETE FROM public.{table}") == "42501", table
            assert _refused(app_conn, f"TRUNCATE public.{table}") == "42501", table
        assert _refused(app_conn, "DELETE FROM public.donations WHERE id = %s", (did,)) == "23503"  # RESTRICT
    with db("hyperchat_worker") as worker:
        for table in ("payment_integrity_events", "payment_webhook_events", "payment_integrity_current"):
            assert _refused(worker, f"SELECT 1 FROM public.{table}") == "42501", table
    with fixture_db() as owner:  # the schema owner too: the guard trigger and the foreign keys
        for table in ("payment_integrity_events", "payment_webhook_events"):
            assert _refused(owner, f"UPDATE public.{table} SET received_at = now() WHERE donation_id = %s", (did,)) \
                == "42501", table
            assert _refused(owner, f"DELETE FROM public.{table} WHERE donation_id = %s", (did,)) == "42501", table
            assert _refused(owner, f"TRUNCATE public.{table} CASCADE") == "42501", table
        assert _refused(owner, "TRUNCATE public.donations CASCADE") == "42501"  # cascades into the evidence
        assert _refused(owner, "DELETE FROM public.donations WHERE id = %s", (did,)) == "23503"
    assert len(evidence("authtest-int-ro-")) == 1 and donation(out["order_id"])["id"] == did


def test_report_cli_is_read_only_and_flags_what_needs_attention(client, capsys):
    sid, out, payment = paid(client, "authtest-int-rep")
    dispute(client, "payment.dispute.lost", out, payment.id, "authtest-int-rep-1", ref="disp_TESTrep1",
            amount_deducted=10000)
    before = financial_state(sid)
    with db() as conn:
        events_before = conn.execute("SELECT count(*) FROM public.payment_webhook_events").fetchone()[0]
    code = asyncio.run(payment_integrity_report(Settings(), Namespace(since=None)))
    report = json.loads(capsys.readouterr().out)
    item = next(i for i in report["items"] if i["provider_ref"] == "disp_TESTrep1")
    assert code == 1 and report["read_only"] is True and item["attention"] is True
    assert (item["status"], item["amount_deducted_paise"], item["streamer_slug"], item["match_type"]) == (
        "lost", 10000, "authtest-int-rep", "payment")
    assert asyncio.run(payment_integrity_report(Settings(), Namespace(since="yesterday"))) == 2
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.payment_webhook_events").fetchone()[0] == events_before
    assert financial_state(sid) == before
