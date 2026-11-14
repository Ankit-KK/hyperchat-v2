"""PAY-15 (D17) stage 5a on PostgreSQL: what creators and operators see of reversals, disputes and holds.

Creators (owner, linked account, admin with admin assurance) get three additive fields on the dashboard donation list
and the moderation queue: reversed_inr, net_inr (the stage 4 figures) and a coarse reversal_flag (under_review >
reversed > dispute_open > null) from the accounting state only. Never provider references, payment / order ids,
reason codes, hold codes or F-BOTH. Operators get the accounting state in the read-only payment-integrity-report.
Reversals are created through the real path (evidence, evaluator, donation_accounting_apply)."""

import asyncio
import json
from argparse import Namespace
from decimal import Decimal

import pytest

from app.cli import payment_integrity_report
from app.core.config import Settings
from tests.integration import (
    db,
    fixture_db,
    login,
    make_donation,
    make_streamer,
    make_user,
    new_browser,
    requires_db,
    update_streamer,
)
from tests.payments.test_accounting_db import _apply, _evidence, _ref, _settle
from tests.payments.test_figures_db import _csv, _v2
from tests.stepup.test_stepup_enforcement_db import set_enforcement

pytestmark = [requires_db, pytest.mark.usefixtures("env", "accounting_active")]  # 0034 state active
S = "/api/v1/dashboard/streamers"
DOMAIN = "@authtest.invalid"
SENSITIVE = ("rfnd_", "disp_", "pay_", "order_", "H-CONFLICT", "H-CURRENCY", "H-NO-DEDUCTION", "H-OVER-REFUND",
             "F-BOTH", "F-OPEN", "reason_code", "chargeback")


def _creator_with_owner(slug: str, email: str) -> str:
    return make_streamer(slug, user_id=make_user(email))


def _browser(app, email: str):
    browser = new_browser(app)
    assert login(browser, email).status_code == 200
    return browser


def _rows(browser, slug: str, queue: bool = False) -> dict:
    """{donation_id: row} of the list (approved) or the moderation queue (every paid status)."""
    r = browser.get(f"{S}/{slug}/{'moderation/queue' if queue else 'donations'}",
                    params={"status": "all"} if queue else {})
    assert r.status_code == 200, r.text
    assert not any(s in r.text for s in SENSITIVE), "provider or internal accounting detail reached a creator"
    return {d["donation_id"]: d for d in r.json()["donations"]}


def _creator_fields(row: dict) -> tuple:
    return row["reversed_inr"], row["net_inr"], row["reversal_flag"]


# ── the flag and the figures ────────────────────────────────────────────────────

def test_each_accounting_state_gives_its_coarse_flag_and_the_stage4_figures(app):
    sid = _creator_with_owner("authtest-vis-flags", "owner" + DOMAIN)
    plain, _ = _v2(sid, "100")
    partial, partial_pid = _v2(sid, "100")
    full, full_pid = _v2(sid, "100")
    opened, opened_pid = _v2(sid, "100")
    held, held_pid = _v2(sid, "100")
    both, both_pid = _v2(sid, "100")
    _evidence(partial, partial_pid, "refund", _ref("refund"), "processed", 2500)
    _evidence(full, full_pid, "refund", _ref("refund"), "processed", 10000)
    _evidence(opened, opened_pid, "dispute", _ref("dispute"), "open", 10000)
    _evidence(held, held_pid, "refund", _ref("refund"), "processed", 4000)
    _evidence(both, both_pid, "refund", _ref("refund"), "processed", 3000)
    _evidence(both, both_pid, "dispute", _ref("dispute"), "lost", 10000, deducted=4000)
    for did in (partial, full, opened, held, both):
        _settle(did)
    _apply(held, None, hold="H-CONFLICT")
    rows = _rows(_browser(app, "owner" + DOMAIN), "authtest-vis-flags", queue=True)
    assert _creator_fields(rows[plain]) == (0.0, 100.0, None)                 # no accounting state
    assert _creator_fields(rows[partial]) == (25.0, 75.0, "reversed")
    assert _creator_fields(rows[full]) == (100.0, 0.0, "reversed")
    assert _creator_fields(rows[opened]) == (0.0, 100.0, "dispute_open")      # F-OPEN, nothing applied
    assert _creator_fields(rows[held]) == (40.0, 60.0, "under_review")         # hold wins; frozen figure kept
    assert _creator_fields(rows[both]) == (40.0, 60.0, "reversed")            # F-BOTH is never shown as such


def test_a_resolved_hold_returns_to_its_plain_flag(app):
    sid = _creator_with_owner("authtest-vis-resolve", "owner" + DOMAIN)
    did, pid = _v2(sid, "100")
    ref = _ref("refund")
    _evidence(did, pid, "refund", ref, "processed", 2500)
    _settle(did)
    _apply(did, None, hold="H-CURRENCY")
    browser = _browser(app, "owner" + DOMAIN)
    assert _creator_fields(_rows(browser, "authtest-vis-resolve")[did]) == (25.0, 75.0, "under_review")
    assert _apply(did, 2500, refs=(ref,)) == "resolved"
    assert _creator_fields(_rows(browser, "authtest-vis-resolve")[did]) == (25.0, 75.0, "reversed")


def test_hold_beats_reversed_beats_open_dispute(app):
    """Precedence on one donation: an open dispute alone, then a refund on top, then a hold on top."""
    sid = _creator_with_owner("authtest-vis-order", "owner" + DOMAIN)
    did, pid = _v2(sid, "100")
    browser = _browser(app, "owner" + DOMAIN)
    _evidence(did, pid, "dispute", _ref("dispute"), "open", 10000)
    _settle(did)
    assert _rows(browser, "authtest-vis-order")[did]["reversal_flag"] == "dispute_open"
    _evidence(did, pid, "refund", _ref("refund"), "processed", 1000)
    _settle(did)
    assert _rows(browser, "authtest-vis-order")[did]["reversal_flag"] == "reversed"
    _apply(did, None, hold="H-OVER-REFUND")
    assert _rows(browser, "authtest-vis-order")[did]["reversal_flag"] == "under_review"


@pytest.mark.parametrize("provenance", ["imported", "ambiguous"])
def test_imported_and_ambiguous_donations_never_carry_a_flag(app, provenance):
    sid = _creator_with_owner(f"authtest-vis-{provenance}", "owner" + DOMAIN)
    did = make_donation(sid, 100, payment="pending", moderation="pending", name="Fan")
    if provenance == "imported":
        with fixture_db() as conn:
            conn.execute("UPDATE public.donations SET legacy_source_table = 'authtest_donations' WHERE id = %s", (did,))
    with db() as conn:
        conn.execute("UPDATE public.donations SET payment_status = 'success', provider_payment_id = 'pay_TESTvis1x', "
                     "moderation_status = 'approved' WHERE id = %s", (did,))
    _evidence(did, "pay_TESTvis1x", "refund", _ref("refund"), "processed", 10000)
    assert _settle(did) is None
    assert _creator_fields(_rows(_browser(app, "owner" + DOMAIN), f"authtest-vis-{provenance}")[did]) == (
        0.0, 100.0, None)


def test_list_queue_and_stage4_figures_agree(app):
    sid = _creator_with_owner("authtest-vis-same", "owner" + DOMAIN)
    a, a_pid = _v2(sid, "100")
    b, b_pid = _v2(sid, "1.50", currency="USD")
    _evidence(a, a_pid, "refund", _ref("refund"), "processed", 3333)
    _evidence(b, b_pid, "refund", _ref("refund"), "processed", 50, currency="USD")
    _settle(a), _settle(b)
    browser = _browser(app, "owner" + DOMAIN)
    listed, queued = _rows(browser, "authtest-vis-same"), _rows(browser, "authtest-vis-same", queue=True)
    exported = {r["net_inr"]: r for r in _csv(sid)}
    for did in (a, b):
        assert _creator_fields(listed[did]) == _creator_fields(queued[did])
        assert listed[did]["net_inr"] + listed[did]["reversed_inr"] == listed[did]["amount_inr"]
    assert sorted(float(n) for n in exported) == sorted(listed[d]["net_inr"] for d in (a, b))
    assert _creator_fields(listed[a]) == (33.33, 66.67, "reversed") and _creator_fields(listed[b]) == (
        44.5, 89.0, "reversed")


def test_the_moderation_result_carries_the_same_fields(app):
    """POST moderation returns the donation (QueueItem): it is selected with the reversal join too."""
    sid = _creator_with_owner("authtest-vis-mod", "owner" + DOMAIN)
    did, pid = _v2(sid, "100")
    _evidence(did, pid, "refund", _ref("refund"), "processed", 2500)
    _settle(did)
    browser = new_browser(app)
    resp = login(browser, "owner" + DOMAIN)
    headers = {"origin": "https://hyperchat.space", "x-csrf-token": resp.json()["csrf_token"]}
    r = browser.post(f"{S}/authtest-vis-mod/donations/{did}/moderation", json={"action": "reject"}, headers=headers)
    assert r.status_code == 200, r.text
    assert _creator_fields(r.json()["donation"]) == (25.0, 75.0, "reversed")
    assert not any(s in r.text for s in SENSITIVE)


# ── authorization and privacy ───────────────────────────────────────────────────

def test_owner_linked_and_admin_see_the_fields_a_stranger_sees_nothing(app):
    sid = _creator_with_owner("authtest-vis-matrix", "owner" + DOMAIN)
    make_user("linked" + DOMAIN, streamer_id=sid)
    make_user("boss" + DOMAIN, role="admin")
    make_user("stranger" + DOMAIN)
    other = _creator_with_owner("authtest-vis-other", "other" + DOMAIN)
    did, pid = _v2(sid, "100")
    mine, mine_pid = _v2(other, "100")
    _evidence(did, pid, "refund", _ref("refund"), "processed", 2500)
    _evidence(mine, mine_pid, "refund", _ref("refund"), "processed", 10000)
    _settle(did), _settle(mine)
    for who in ("owner", "linked", "boss"):
        assert _creator_fields(_rows(_browser(app, who + DOMAIN), "authtest-vis-matrix")[did]) == (25.0, 75.0,
                                                                                                    "reversed")
    stranger = _browser(app, "stranger" + DOMAIN)
    for path in ("donations", "moderation/queue"):
        r = stranger.get(f"{S}/authtest-vis-matrix/{path}")
        assert (r.status_code, r.json()) == (403, {"error": "Forbidden"})
    other_owner = _browser(app, "other" + DOMAIN)  # another creator's owner: their own list only
    assert set(_rows(other_owner, "authtest-vis-other")) == {mine}
    r = other_owner.get(f"{S}/authtest-vis-matrix/donations")
    assert r.status_code == 403 and "25.0" not in r.text


def test_admin_access_to_creator_figures_needs_admin_assurance_once_enforced(app):
    sid = _creator_with_owner("authtest-vis-mfa", "owner" + DOMAIN)
    make_user("boss" + DOMAIN, role="admin")
    did, pid = _v2(sid, "100")
    _evidence(did, pid, "refund", _ref("refund"), "processed", 2500)
    _settle(did)
    admin = _browser(app, "boss" + DOMAIN)  # a password session: no passkey assurance (aal1)
    owner = _browser(app, "owner" + DOMAIN)
    set_enforcement("active")
    try:
        r = admin.get(f"{S}/authtest-vis-mfa/donations")
        assert r.status_code == 403 and ("mfa_required" in r.text or "passkey" in r.text) and "25.0" not in r.text
        assert _creator_fields(_rows(owner, "authtest-vis-mfa")[did]) == (25.0, 75.0, "reversed")  # owners unaffected
    finally:
        set_enforcement("draft")


def test_public_and_obs_surfaces_gain_nothing(app):
    sid = _creator_with_owner("authtest-vis-public", "owner" + DOMAIN)
    update_streamer(sid, goal_is_active=True, goal_activated_at="2026-01-01T00:00:00+00:00", goal_target_amount=1000,
                    goal_name="Mic")
    did, pid = _v2(sid, "100")
    _evidence(did, pid, "refund", _ref("refund"), "processed", 2500)
    _settle(did)
    anonymous = new_browser(app)
    goal = anonymous.get("/api/v1/public/streamers/authtest-vis-public/goal")
    assert goal.status_code == 200 and goal.json()["progress_inr"] == 75.0  # the stage 4 figure, nothing more
    assert set(goal.json()) == {"active", "name", "target_amount_inr", "progress_inr", "brand_color", "goal_overlay_id"}
    assert not any(s in goal.text for s in (*SENSITIVE, "reversal_flag", "reversed_inr", "under_review"))
    assert anonymous.get(f"{S}/authtest-vis-public/donations").status_code == 401


# ── operator report (payment-integrity-report) ──────────────────────────────────

def test_the_operator_report_shows_accounting_state_read_only_without_names(app, capsys):
    sid = _creator_with_owner("authtest-vis-report", "owner" + DOMAIN)
    held, held_pid = _v2(sid, "100", name="Secret Donor Name")
    both, both_pid = _v2(sid, "100", name="Secret Donor Name")
    imported = make_donation(sid, 50, moderation="approved", name="Secret Donor Name")
    with fixture_db() as conn:
        conn.execute("UPDATE public.donations SET legacy_source_table = 'authtest_donations', "
                     "provider_payment_id = 'pay_TESTvisimp1' WHERE id = %s", (imported,))
    held_ref = _ref("refund")
    _evidence(held, held_pid, "refund", held_ref, "processed", 2500)
    _settle(held)
    _apply(held, None, hold="H-CONFLICT")
    _evidence(both, both_pid, "refund", _ref("refund"), "processed", 3000)
    _evidence(both, both_pid, "dispute", _ref("dispute"), "lost", 10000, deducted=4000)
    _settle(both)
    _evidence(imported, "pay_TESTvisimp1", "refund", _ref("refund"), "processed", 5000)
    with db() as conn:
        before = conn.execute("SELECT (SELECT count(*) FROM public.donation_credit_events), "
                              "(SELECT count(*) FROM public.donation_reversals)").fetchone()
    code = asyncio.run(payment_integrity_report(Settings(), Namespace(since=None)))
    out = capsys.readouterr().out
    report = json.loads(out)
    assert code == 1 and report["read_only"] is True and "Secret Donor Name" not in out
    item = next(i for i in report["items"] if i["provider_ref"] == held_ref)
    assert item["accounting"] == {"applied_reversed_paise": 2500, "hold": "H-CONFLICT", "flags": []}
    assert item["attention"] is True and item["imported"] is False
    acc = report["accounting"]
    assert held in {h["donation_id"] for h in acc["held"]}
    assert next(h for h in acc["held"] if h["donation_id"] == held)["hold"] == "H-CONFLICT"
    assert both in {b["donation_id"] for b in acc["review_both"]}
    assert imported in {i["donation_id"] for i in acc["imported_with_evidence"]}
    assert acc["counts"] == {k: len(acc[k]) for k in ("held", "review_both", "imported_with_evidence")}
    with db() as conn:  # read-only: nothing written
        assert conn.execute("SELECT (SELECT count(*) FROM public.donation_credit_events), "
                            "(SELECT count(*) FROM public.donation_reversals)").fetchone() == before


def test_the_report_keeps_its_existing_attention_rule_and_flags_holds(capsys):
    """An item that needs attention by the PAY-13 rules still does; a held item does even if PAY-13 is satisfied."""
    sid = make_streamer("authtest-vis-exit")
    did, pid = _v2(sid, "100")
    ref = _ref("refund")
    _evidence(did, pid, "refund", ref, "processed", 2500)  # 'processed' is an attention state (PAY-13)
    _settle(did)
    asyncio.run(payment_integrity_report(Settings(), Namespace(since=None)))
    item = next(i for i in json.loads(capsys.readouterr().out)["items"] if i["provider_ref"] == ref)
    assert item["attention"] is True and item["accounting"]["hold"] is None
    assert asyncio.run(payment_integrity_report(Settings(), Namespace(since="not-a-date"))) == 2  # unchanged


def test_the_accounting_switch_stays_off():
    from pydantic import ValidationError

    assert Settings().payment_accounting_enabled is False
    with pytest.raises(ValidationError):
        Settings(payment_accounting_enabled=True)


def test_the_report_connection_is_read_only_at_the_database_level():
    """The report's own session setting (PaymentRepository.integrity_report / accounting_report, on the CLI's
    connection factory) makes PostgreSQL itself refuse writes: right after each method sets its session READ ONLY,
    a harmless UPDATE of a test-owned donation on that same connection fails with read_only_sql_transaction (25006)
    and changes nothing."""
    from contextlib import asynccontextmanager

    import psycopg

    from app.cli import connection_factory
    from app.payments.repository import PaymentRepository

    sid = make_streamer("authtest-vis-readonly")
    did, _ = _v2(sid, "100")
    with db() as conn:
        before = conn.execute("SELECT message_visible, updated_at FROM public.donations WHERE id = %s", (did,)).fetchone()
    refused: list[str] = []
    real = connection_factory(Settings())

    class Probe:
        """The real connection; right after the repository's own READ ONLY setting, try one write on it."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        async def execute(self, sql, *args, **kwargs):
            result = await self._conn.execute(sql, *args, **kwargs)
            if str(sql).startswith("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"):
                try:
                    await self._conn.execute("UPDATE public.donations SET message_visible = NOT message_visible "
                                             "WHERE id = %s", (did,))
                except psycopg.errors.ReadOnlySqlTransaction as exc:
                    refused.append(exc.sqlstate)
            return result

    @asynccontextmanager
    async def probing():
        async with real() as conn:
            yield Probe(conn)

    async def run_reports():
        repo = PaymentRepository(probing)
        await repo.integrity_report(None)
        await repo.accounting_report()
    asyncio.run(run_reports())
    assert refused == ["25006", "25006"]  # both report queries ran on read-only sessions
    with db() as conn:
        assert conn.execute("SELECT message_visible, updated_at FROM public.donations WHERE id = %s",
                            (did,)).fetchone() == before
