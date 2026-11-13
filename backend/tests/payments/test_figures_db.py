"""PAY-15 (D17) stage 4 on PostgreSQL: reversal-adjusted live figures (app.payments.figures).

Goal progress (public and realtime), dashboard revenue (net, plus the reversed sums) and the CSV export
(amount_inr gross, reversed_inr, net_inr appended) all use one per-donation reversal: the stored applied reversal of
migration 0032, pro rata of the checkout-time amount_inr, rounded down to the paisa before any sum, exactly as
app.payments.accounting.reversed_inr(). Reversals are created through the real path (evidence, the PR 1 evaluator,
public.donation_accounting_apply), never by writing donation_reversals. Each figure keeps its own eligible rows:
cross-figure checks use scenarios where those row sets are identical."""

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.dashboard.repository import DashboardRepository
from app.payments import accounting, figures, rules
from app.public.pricing import EXCHANGE_RATES_TO_INR
from app.public.repository import PublicRepository
from app.realtime.outbox import Event
from app.realtime.payloads import RealtimeRepository, Renderer
from tests.integration import db, fixture_db, make_donation, requires_db, update_streamer
from tests.jobs.test_jobs_db import factory, run
from tests.payments.test_accounting_db import _apply, _creator, _evidence, _ref, _settle, _total

pytestmark = [requires_db, pytest.mark.usefixtures("env")]
WINDOW = (datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC))


# ── fixtures ────────────────────────────────────────────────────────────────────

def _v2(sid: str, amount="100", *, currency="INR", moderation="approved", at=None, name="Fan") -> tuple[str, str]:
    """A V2 checkout donation (provenance as checkout writes it), paid as the payment path does."""
    amount = Decimal(amount)
    did = make_donation(sid, rules.to_inr(amount, currency), payment="pending", moderation="pending", name=name,
                        currency=currency, amount=amount, at=at)
    with fixture_db() as conn:
        conn.execute("UPDATE public.donations SET payment_account = 'primary', provider_order_id = %s WHERE id = %s",
                     ("order_TESTfig" + secrets.token_hex(5), did))
    pid = "pay_TESTfig" + secrets.token_hex(5)
    with db() as conn:
        conn.execute("UPDATE public.donations SET payment_status = 'success', provider_payment_id = %s, "
                     "moderation_status = %s WHERE id = %s", (pid, moderation, did))
    return did, pid


def _refund(did, pid, paise, currency="INR", status="processed"):
    _evidence(did, pid, "refund", _ref("refund"), status, paise, currency=currency)
    return _settle(did)


def _lost(did, pid, deducted, *, ref=None, at=1, phase="chargeback"):
    ref = ref or _ref("dispute")
    _evidence(did, pid, "dispute", ref, "lost", 10000, deducted=deducted, at=at, phase=phase)
    _settle(did)
    return ref


def _goal(sid: str, since=WINDOW[0]) -> Decimal:
    return Decimal(str(run(PublicRepository(factory()).goal_progress_inr(sid, since))))


def _stats(sid: str, day=WINDOW, month=WINDOW) -> dict:
    row = run(DashboardRepository(factory()).donation_stats(sid, day=day, month=month))
    return {k: (v if k == "total_donations" else Decimal(v)) for k, v in row.items()}


def _csv(sid: str) -> list[dict]:
    return run(DashboardRepository(factory()).export_rows(sid, start=WINDOW[0], end=WINDOW[1], limit=1000))


def _goal_active(sid: str, since=datetime(2026, 1, 1, tzinfo=UTC)) -> None:
    update_streamer(sid, goal_is_active=True, goal_activated_at=since, goal_target_amount=Decimal("1000"),
                    goal_name="Mic")


# ── the formula: SQL == Python, exact, in every supported currency ─────────────

AMOUNTS = ("0.01", "1", "1.50", "99.99", "100", "250", "333.33", "9999.99", "4999.95")


@pytest.mark.parametrize("currency", sorted(EXCHANGE_RATES_TO_INR))
def test_the_sql_reversal_equals_the_python_reference_in_every_currency(currency):
    """S4-D1: one definition. Includes the largest amounts the checkout allows (cap Rs 500000), where a plain numeric
    division could round up before floor(); figures uses div() (exact)."""
    sql = (f"SELECT {figures.REVERSED_INR}, {figures.NET_INR} FROM (VALUES (%s::numeric, %s::numeric)) "
           "AS d(amount, amount_inr) LEFT JOIN (VALUES (%s::bigint)) AS dr(applied_reversed_paise) ON %s")
    cap = (rules.AMOUNT_CAP_INR / Decimal(str(EXCHANGE_RATES_TO_INR[currency]))).quantize(Decimal("0.01"),
                                                                                           rounding="ROUND_DOWN")
    with db() as conn:
        for amount in (*(Decimal(a) for a in AMOUNTS), cap):
            inr = rules.to_inr(amount, currency)
            captured = rules.subunits(amount)
            for applied in sorted({0, 1, captured // 3, captured // 2, captured - 1, captured, 7, 33333} - {-1}):
                applied = min(applied, captured)
                got = conn.execute(sql, (amount, inr, applied, True)).fetchone()
                want = accounting.reversed_inr({"amount": amount, "amount_inr": inr}, applied)
                assert got == (want, inr - want), (currency, amount, applied)
            assert conn.execute(sql, (amount, inr, 0, False)).fetchone() == (Decimal("0.00"), inr)  # no reversal row


# ── no reversal: byte-identical to the pre-stage-4 figures ──────────────────────

def test_without_any_reversal_every_figure_equals_the_gross_formulas():
    sid = _creator()
    _goal_active(sid)
    _v2(sid, "100"), _v2(sid, "1.50", currency="USD"), _v2(sid, "40", moderation="rejected"), _v2(sid, "25")
    make_donation(sid, 999, payment="pending", moderation="pending", name="Fan")  # unpaid: in no figure
    with db() as conn:
        gross_goal = conn.execute("SELECT COALESCE(SUM(amount_inr), 0) FROM public.donations WHERE streamer_id = %s "
                                  "AND payment_status = 'success' AND moderation_status IN ('approved', 'auto_approved') "
                                  "AND created_at >= %s", (sid, WINDOW[0])).fetchone()[0]
        gross_revenue, count = conn.execute("SELECT COALESCE(SUM(amount_inr), 0), count(*) FROM public.donations "
                                            "WHERE streamer_id = %s AND payment_status = 'success'", (sid,)).fetchone()
    assert _goal(sid) == gross_goal
    s = _stats(sid)
    assert (s["total_revenue_inr"], s["today_revenue_inr"], s["month_revenue_inr"], s["total_donations"]) == (
        gross_revenue, gross_revenue, gross_revenue, count)
    assert (s["total_reversed_inr"], s["today_reversed_inr"], s["month_reversed_inr"]) == (0, 0, 0)
    assert all(r["reversed_inr"] == 0 and r["net_inr"] == r["amount_inr"] for r in _csv(sid))
    assert len(_csv(sid)) == count


# ── each kind of reversal, through the real path ───────────────────────────────

@pytest.mark.parametrize("currency, amount, refund, reversed_inr", [
    ("INR", "100", 3333, "33.33"),       # partial, rounded down
    ("INR", "100", 10000, "100.00"),     # full
    ("USD", "1.50", 50, "44.50"),        # Rs 133.50 x 50/150, rounded down
    ("PHP", "99.99", 1, "0.01"),         # Rs 149.98 (checkout rounding) x 1/9999 = 0.0149.. -> 0.01
])
def test_a_refund_reduces_goal_revenue_and_csv_by_the_same_rounded_amount(currency, amount, refund, reversed_inr):
    sid = _creator()
    _goal_active(sid)
    did, pid = _v2(sid, amount, currency=currency)
    gross = rules.to_inr(Decimal(amount), currency)
    before = (_goal(sid), _stats(sid)["total_donations"])
    assert _refund(did, pid, refund, currency=currency) == "evaluation"
    rev = Decimal(reversed_inr)
    assert _goal(sid) == before[0] - rev == gross - rev
    s = _stats(sid)
    assert (s["total_revenue_inr"], s["total_reversed_inr"], s["total_donations"]) == (gross - rev, rev, before[1])
    (row,) = _csv(sid)
    assert (row["amount_inr"], row["reversed_inr"], row["net_inr"]) == (gross, rev, gross - rev)


def test_a_lost_dispute_then_a_win_at_a_later_phase_restores_every_figure_exactly():
    sid = _creator()
    _goal_active(sid)
    did, pid = _v2(sid, "100")
    ref = _lost(did, pid, 4000)
    assert (_goal(sid), _stats(sid)["total_revenue_inr"], _total(sid)) == (Decimal("60.00"), Decimal("60.00"),
                                                                          Decimal("60.00"))
    _evidence(did, pid, "dispute", ref, "won", 10000, deducted=4000, at=2, phase="pre_arbitration")
    assert _settle(did) == "evaluation"
    assert (_goal(sid), _stats(sid)["total_revenue_inr"], _stats(sid)["total_reversed_inr"], _total(sid)) == (
        Decimal("100.00"), Decimal("100.00"), Decimal("0.00"), Decimal("100.00"))


def test_a_refund_and_a_lost_dispute_reduce_figures_by_the_larger_never_the_sum():
    sid = _creator()
    _goal_active(sid)
    did, pid = _v2(sid, "100")
    _evidence(did, pid, "refund", _ref("refund"), "processed", 3000)
    _evidence(did, pid, "dispute", _ref("dispute"), "lost", 10000, deducted=4000)
    assert _settle(did) == "evaluation"
    assert (_goal(sid), _stats(sid)["total_reversed_inr"], _csv(sid)[0]["net_inr"], _total(sid)) == (
        Decimal("60.00"), Decimal("40.00"), Decimal("60.00"), Decimal("60.00"))


def test_a_held_donation_keeps_its_frozen_reversal_in_every_figure():
    """Worked example C: refund 25 applied, then held; goal and leaderboard follow moderation with the frozen 25."""
    sid = _creator()
    _goal_active(sid)
    did, pid = _v2(sid, "100")
    _refund(did, pid, 2500)
    _apply(did, None, hold="H-CONFLICT")
    assert (_goal(sid), _stats(sid)["total_reversed_inr"], _total(sid)) == (Decimal("75.00"), Decimal("25.00"),
                                                                          Decimal("75.00"))
    with db() as conn:
        conn.execute("UPDATE public.donations SET moderation_status = 'rejected' WHERE id = %s", (did,))
    s = _stats(sid)  # revenue counts every paid donation, rejected included (D-R3): still net of the frozen 25
    assert (_goal(sid), _total(sid), s["total_revenue_inr"], s["total_reversed_inr"]) == (
        Decimal("0.00"), Decimal("0.00"), Decimal("75.00"), Decimal("25.00"))
    with db() as conn:
        conn.execute("UPDATE public.donations SET moderation_status = 'approved' WHERE id = %s", (did,))
    assert (_goal(sid), _total(sid)) == (Decimal("75.00"), Decimal("75.00"))


# ── rows that are never reversed, and rows whose credit differs from gross ─────

@pytest.mark.parametrize("provenance", ["imported", "ambiguous"])
def test_imported_and_ambiguous_donations_are_never_reversed_in_any_figure(provenance):
    sid = _creator()
    _goal_active(sid)
    did = make_donation(sid, 100, payment="pending", moderation="pending", name="Fan")
    if provenance == "imported":
        with fixture_db() as conn:
            conn.execute("UPDATE public.donations SET legacy_source_table = 'authtest_donations' WHERE id = %s", (did,))
    pid = "pay_TESTfigx" + secrets.token_hex(4)
    with db() as conn:
        conn.execute("UPDATE public.donations SET payment_status = 'success', provider_payment_id = %s, "
                     "moderation_status = 'approved' WHERE id = %s", (pid, did))
    _evidence(did, pid, "refund", _ref("refund"), "processed", 10000)
    assert _settle(did) is None  # never evaluated (PR 3)
    assert (_goal(sid), _stats(sid)["total_revenue_inr"], _stats(sid)["total_reversed_inr"]) == (
        Decimal("100.00"), Decimal("100.00"), Decimal("0.00"))
    assert (_csv(sid)[0]["reversed_inr"], _csv(sid)[0]["net_inr"]) == (Decimal("0.00"), Decimal("100.00"))


def test_a_short_backfilled_row_differs_between_figures_and_leaderboard_as_designed():
    """S4-D5 (known, pre-existing): figures use amount_inr (gross 100, net 0 after a full refund); the leaderboard
    uses the row's backfilled credit ceiling (50 -> 0). Pinned, not equalised."""
    sid = _creator()
    _goal_active(sid)
    pid = "pay_TESTfigshort" + secrets.token_hex(3)
    with fixture_db() as conn, conn.transaction():  # as donations_credit_backfill() leaves a short row (audited)
        conn.execute("ALTER TABLE public.donations DISABLE TRIGGER donations_credit_writer")
        did = conn.execute(
            "INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, amount_inr, payment_status, "
            "moderation_status, provider_payment_id, payment_account, provider_order_id, creditable_inr, credited_inr, "
            "created_at) VALUES (%s, %s, 'Fan', 100, 'INR', 100, 'success', 'approved', %s, 'primary', %s, 50, 50, "
            "now()) RETURNING id::text",
            (sid, "authtest-" + secrets.token_hex(6), pid, "order_TESTfigshort" + secrets.token_hex(3))).fetchone()[0]
        conn.execute("ALTER TABLE public.donations ENABLE TRIGGER donations_credit_writer")
        conn.execute("INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause, "
                     "payment_status, moderation_status, credited_before, credited_after, delta_inr, delta_count, actor) "
                     "VALUES (%s, %s, 'fan', 'backfill', 'success', 'approved', 0, 50, 50, 0, session_user)", (did, sid))
        conn.execute("INSERT INTO public.streamer_donator_totals (streamer_id, donator_name, donator_name_lower, "
                     "total_amount, donation_count) VALUES (%s, 'Fan', 'fan', 50, 1)", (sid,))
    assert (_goal(sid), _total(sid)) == (Decimal("100.00"), Decimal("50.00"))  # differs before any reversal too
    _refund(did, pid, 10000)
    assert (_goal(sid), _stats(sid)["total_reversed_inr"], _total(sid)) == (Decimal("0.00"), Decimal("100.00"),
                                                                          Decimal("0.00"))


# ── windows and buckets ─────────────────────────────────────────────────────────

def test_a_reversal_outside_the_goal_window_does_not_move_the_goal():
    """D5: a donation made before the goal was activated is outside its window, reversed or not."""
    sid = _creator()
    start = datetime(2026, 6, 1, tzinfo=UTC)
    _goal_active(sid, since=start)
    old, old_pid = _v2(sid, "100", at=start - timedelta(seconds=1))
    _v2(sid, "50", at=start)
    assert _goal(sid, start) == Decimal("50.00")
    _refund(old, old_pid, 10000)
    assert _goal(sid, start) == Decimal("50.00") and _stats(sid)["total_reversed_inr"] == Decimal("100.00")


def test_a_reversal_reduces_the_ist_day_and_month_of_the_donation():
    """S4-D4: the donation's created_at bucket (IST bounds as the service computes them)."""
    sid = _creator()
    day = (datetime(2026, 10, 4, 18, 30, tzinfo=UTC), datetime(2026, 10, 5, 18, 30, tzinfo=UTC))      # 5 Oct IST
    month = (datetime(2026, 9, 30, 18, 30, tzinfo=UTC), datetime(2026, 10, 31, 18, 30, tzinfo=UTC))  # Oct IST
    today, today_pid = _v2(sid, "100", at=datetime(2026, 10, 4, 18, 30, tzinfo=UTC))               # 5 Oct 00:00 IST
    earlier, earlier_pid = _v2(sid, "100", at=datetime(2026, 10, 4, 18, 29, 59, tzinfo=UTC))       # 4 Oct IST
    september, sep_pid = _v2(sid, "100", at=datetime(2026, 9, 30, 18, 29, 59, tzinfo=UTC))         # 30 Sep IST
    for did, pid, paise in ((today, today_pid, 1000), (earlier, earlier_pid, 2000), (september, sep_pid, 4000)):
        _refund(did, pid, paise)
    s = _stats(sid, day, month)
    assert (s["today_reversed_inr"], s["month_reversed_inr"], s["total_reversed_inr"]) == (
        Decimal("10.00"), Decimal("30.00"), Decimal("70.00"))
    assert (s["today_revenue_inr"], s["month_revenue_inr"], s["total_revenue_inr"]) == (
        Decimal("90.00"), Decimal("170.00"), Decimal("230.00"))


# ── cross-figure consistency on identical eligible rows (F5) ────────────────────

def test_every_figure_agrees_when_the_eligible_rows_are_the_same():
    """One donor, V2 donations only, all paid AND approved AND inside the goal window and the export range: goal, revenue,
    CSV and leaderboard cover the same rows, so net goal = net revenue = sum of CSV net = donor total, and the reversed
    sums agree, after partial, full and overlapping reversals."""
    sid = _creator()
    _goal_active(sid)
    a, a_pid = _v2(sid, "100")
    b, b_pid = _v2(sid, "1.50", currency="USD")
    c, c_pid = _v2(sid, "99.99", currency="PHP")
    d, d_pid = _v2(sid, "250")
    _refund(a, a_pid, 3333)
    _refund(b, b_pid, 150, currency="USD")
    _refund(c, c_pid, 1, currency="PHP")
    _evidence(d, d_pid, "refund", _ref("refund"), "processed", 5000)
    _evidence(d, d_pid, "dispute", _ref("dispute"), "lost", 25000, deducted=7000)
    _settle(d)
    rows = _csv(sid)
    s = _stats(sid)
    net = sum(r["net_inr"] for r in rows)
    assert _goal(sid) == s["total_revenue_inr"] == net == _total(sid)
    assert s["total_reversed_inr"] == sum(r["reversed_inr"] for r in rows) == sum(r["amount_inr"] for r in rows) - net
    with db() as conn:
        credited = conn.execute("SELECT sum(credited_inr) FROM public.donations WHERE streamer_id = %s", (sid,)).fetchone()[0]
    assert credited == net  # non-short V2 rows: each credit equals its net figure


# ── realtime: the goal-progress payload and the worker role ────────────────────

def test_the_realtime_goal_payload_follows_an_evaluation_and_the_worker_can_read_it():
    sid = _creator()
    _goal_active(sid)
    did, pid = _v2(sid, "100")

    def goal_payload():
        event = Event(id=1, event_id="evt-" + secrets.token_hex(4), streamer_id=sid, channel="goal",
                      event="goal-progress", subject_id=None, facts={}, attempts=1, max_attempts=5,
                      created_at=datetime.now(UTC), locked_by="test", reclaimed=False)
        return run(Renderer(RealtimeRepository(factory())).render(event)).data
    assert goal_payload()["progress_inr"] == 100.0
    assert _refund(did, pid, 2500) == "evaluation"  # emits goal-progress + leaderboard-updated (PR 3)
    with db() as conn:
        events = [e for (e,) in conn.execute("SELECT event FROM public.realtime_outbox WHERE streamer_id = %s "
                                             "ORDER BY id", (sid,))]
    assert events[-2:] == ["goal-progress", "leaderboard-updated"]
    assert goal_payload()["progress_inr"] == 75.0
    with db("hyperchat_worker") as conn:  # the realtime worker's role reads the reversal state (0032 grant)
        assert conn.execute(figures.GOAL_PROGRESS, (sid, WINDOW[0])).fetchone()[0] == Decimal("75.00")
