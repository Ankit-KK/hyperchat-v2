"""PAY-15 stage 6a-2 on PostgreSQL 15 / 17: the read-only accounting verifier against real 0034 data.

Every test gets a FRESH database copied from the pristine template at head (conftest.verify_db, plan §9), builds its
scenario through the real functions (the app's apply path, the owner's runs / keep / revert / reconcile, the one
credit writer) and runs `python -m app.payments.operator verify`'s core as hyperchat_readonly. Corruption fixtures
write as the schema owner in that throwaway database only. Asserted: classes, offenders, status, exit code, gates, the
SELECT-only statement log, unchanged data checksums (the verifier modifies nothing), the D-1 fingerprint equality,
the view replica, Erratum 1 cases A-C and A-1 to A-5, I1-I15 fixtures, the M-series, IR5-T1 (CE-2 with a real lock
wait) and IR6 on real reports."""

import json
import os
import random
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from psycopg.rows import dict_row

from app.payments import accounting, operator, verify
from tests.integration import PG_HOST, requires_db, target

pytestmark = [requires_db, pytest.mark.skipif(not os.environ.get("HYPERCHAT_TEST_VERIFY_ADMIN"),
                                              reason="HYPERCHAT_TEST_VERIFY_ADMIN not set")]
FIN_TABLES = ("donations", "donation_credit_events", "donation_reversals", "donation_accounting_evaluations",
              "streamer_donator_totals", "payment_accounting_state", "payment_accounting_events",
              "payment_accounting_runs", "payment_integrity_events", "payment_webhook_events", "streamers")


class DB:
    """One throwaway database: the roles connect as in production (app, owner via the migrator, readonly)."""

    def __init__(self, name: str):
        self.name, self.port = name, target()[1]

    def conn(self, role: str = "hyperchat_app", **kw) -> psycopg.Connection:
        return psycopg.connect(f"host={PG_HOST} port={self.port} dbname={self.name} user={role}", autocommit=True,
                               **kw)

    def owner(self) -> psycopg.Connection:
        conn = self.conn("hyperchat_migrator")
        conn.execute("SET ROLE hyperchat_owner")
        return conn

    def one(self, sql, params=(), role=None):
        with (self.owner() if role is None else self.conn(role)) as c:
            return c.execute(sql, params).fetchone()[0]

    # ── state ──
    def state(self) -> tuple:
        with self.conn() as c:
            return c.execute("SELECT state, dispute_mode FROM public.payment_accounting_state").fetchone()

    def set_state(self, state=None, mode=None):
        return self.one("SELECT public.payment_accounting_set_state(%s, %s, 'op-verify', %s, NULL)",
                        (state, mode, f"verify test: {state} / {mode}"))

    def go(self, state: str, mode: str | None = None):
        """Walk the 0034 state machine to (state, mode) along allowed transitions."""
        cur, cur_mode = self.state()
        mode = mode or cur_mode
        if cur == "active" and (state != "active" or mode != cur_mode):
            self.set_state("frozen")
            cur = "frozen"
        if mode != cur_mode:
            if cur == "off":
                pass  # the mode changes while off too
            self.set_state(None, mode)
        if state == "active" and cur != "active":
            self.set_state("active")
        elif state == "frozen" and cur == "off":
            self.set_state("active")
            self.set_state("frozen")

    # ── data ──
    def creator(self) -> str:
        return self.one("INSERT INTO public.streamers (streamer_slug, streamer_name, status, kyc_track) "
                        "VALUES (%s, 'Verify Test', 'active', 'legacy') RETURNING id::text",
                        (f"authtest-verify-{secrets.token_hex(4)}",))

    def donation(self, sid, amount=100, *, currency="INR", amount_inr=None, name="Fan", provenance="v2",
                 moderation="approved", paid=True) -> tuple[str, str]:
        with self.owner() as c:
            did = c.execute(
                "INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, amount_inr, "
                "payment_status, moderation_status) VALUES (%s, %s, %s, %s, %s, %s, 'pending', 'pending') "
                "RETURNING id::text", (sid, "authtest-" + secrets.token_hex(6), name, amount, currency,
                                       amount_inr if amount_inr is not None else amount)).fetchone()[0]
            c.execute("UPDATE public.donations SET payment_account = %s, provider_order_id = %s, "
                      "legacy_source_table = %s WHERE id = %s",
                      ({"v2": "primary"}.get(provenance), "order_TESTvrf" + secrets.token_hex(5)
                       if provenance == "v2" else None, "authtest_donations" if provenance == "imported" else None,
                       did))
        pid = "pay_TESTvrf" + secrets.token_hex(5)
        if paid:
            with self.conn() as c:
                c.execute("UPDATE public.donations SET payment_status = 'success', provider_payment_id = %s, "
                          "moderation_status = %s WHERE id = %s", (pid, moderation, did))
        return did, pid

    def evidence(self, did, pid, kind, ref, status, amount=10000, *, deducted=None, currency="INR",
                 match="payment", at=0, phase="chargeback"):
        event_type = {"processed": "refund.processed", "pending": "refund.created", "lost": "payment.dispute.lost",
                      "won": "payment.dispute.won", "open": "payment.dispute.created"}[status]
        with self.conn() as c:
            wid = c.execute(
                "INSERT INTO public.payment_webhook_events (event_id, event_type, account, provider_payment_id, "
                "donation_id, outcome, received_at) VALUES (%s, %s, 'primary', %s, %s, 'integrity_recorded', now()) "
                "RETURNING id", ("authtest-vrf-" + secrets.token_hex(6), event_type, pid, did)).fetchone()[0]
            c.execute(
                "INSERT INTO public.payment_integrity_events (webhook_event_id, donation_id, match_type, account, kind, "
                "event_type, provider_ref, provider_payment_id, phase, status, amount_paise, currency, "
                "amount_deducted_paise, provider_event_at, consistent, received_at) VALUES (%s, %s, %s, 'primary', %s, "
                "%s, %s, %s, %s, %s, %s, %s, %s, to_timestamp(1800000000 + %s), true, now())",
                (wid, did, match if did else "none", kind, event_type, ref, pid,
                 phase if kind == "dispute" else None, status, amount, currency, deducted, at))

    def _current(self, cur, did):
        cur.execute("SELECT kind, provider_ref, provider_payment_id, donation_id::text AS donation_id, match_type, "
                    "phase, status, amount_paise, currency, amount_deducted_paise, conflicting "
                    "FROM public.payment_integrity_current WHERE donation_id = %s", (did,))
        return cur.fetchall()

    def _donation(self, cur, did):
        cur.execute("SELECT d.id::text AS id, d.amount, d.amount_inr, d.currency::text AS currency, d.payment_status, "
                    "d.provider_payment_id, d.legacy_source_table FROM public.donations d WHERE d.id = %s", (did,))
        return cur.fetchone()

    def settle(self, did, conn=None) -> str:
        """The app path (repository._settle_accounting's rule): state first, evaluate_in_mode, apply."""
        own = conn is None
        conn = conn or self.conn()
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT state, dispute_mode FROM public.payment_accounting_lock_state()")
                switch = cur.fetchone()
                if switch["state"] != "active":
                    return "inactive"
                d, rows = self._donation(cur, did), self._current(cur, did)
                result = accounting.evaluate_in_mode(d, rows, switch["dispute_mode"])
                refs = [] if result.hold else accounting.counted_refs(d, rows)
                cur.execute("SELECT public.donation_accounting_apply(%s, %s, %s, %s, %s) AS outcome",
                            (did, result.reversed_paise, result.hold, sorted(result.flags & {"F-BOTH", "F-OPEN"}),
                             refs))
                return cur.fetchone()["outcome"]
        finally:
            if own:
                conn.close()

    def run(self, command, dids, scope=None) -> int:
        """An operator run with a rev. 3 §6 scope (kind donations, expiring in 1 h) unless `scope` is given."""
        if scope is None:
            scope = {"kind": "donations", "donations": list(dids),
                     "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
        return self.one("SELECT public.payment_accounting_open_run(%s, 'op-verify', 'a verify test run', NULL, %s)",
                        (command, json.dumps(scope)))

    def reconcile(self, did, run):
        with self.owner() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT dispute_mode FROM public.payment_accounting_state")
            mode = cur.fetchone()["dispute_mode"]
            d, rows = self._donation(cur, did), self._current(cur, did)
            result = accounting.evaluate_in_mode(d, rows, mode)
            refs = [] if result.hold else accounting.counted_refs(d, rows)
            cur.execute("SELECT public.payment_accounting_reconcile(%s, %s, %s, %s, %s, %s) AS o",
                        (did, result.reversed_paise, result.hold, sorted(result.flags & {"F-BOTH", "F-OPEN"}), refs,
                         run))
            return cur.fetchone()["o"]

    def keep(self, did, run):
        return self.one("SELECT public.payment_accounting_keep(%s, %s)", (did, run))

    def revert(self, did, run):
        return self.one("SELECT public.payment_accounting_revert(%s, %s)", (did, run))

    # ── the verifier ──
    def verify(self, role="hyperchat_readonly", **kw) -> dict:
        with self.conn(role) as c:
            return verify.verify(c, **kw)

    def checksum(self) -> dict:
        with self.owner() as c:
            return {t: c.execute(f"SELECT md5(COALESCE(string_agg(x::text, '|' ORDER BY x::text), '')) "
                                 f"FROM public.{t} x").fetchone()[0] for t in FIN_TABLES}


@pytest.fixture
def vdb(verify_db) -> DB:
    return DB(verify_db)


def found(report, invariant=None, cls=None, problem=None) -> list[dict]:
    return [f for f in report["findings"] if (invariant is None or f["invariant"] == invariant)
            and (cls is None or f["class"] == cls) and (problem is None or f["detail"].get("problem") == problem)]


def classes_of(report) -> set[str]:
    return {f["class"] for f in report["findings"]}


# ── statuses, gates and the read-only guarantee ───────────────────────────────────────────────────────────────────

def test_a_fresh_0034_database_is_clean_and_not_activation_complete(vdb):
    report = vdb.verify()
    assert report["status"] == "clean" and report["exit_code"] == 0 and report["findings"] == []
    assert report["database"]["head"] == "0034" and report["accounting"] == {"state": "off", "dispute_mode": "hold"}
    assert report["gates"] == dict.fromkeys(verify.GATE_KEYS, False)  # off: nothing is activation-ready


def test_m16_cr1_a_clean_active_full_report_is_verification_ready_and_never_activation_complete(vdb):
    vdb.go("active", "apply")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)
    assert vdb.settle(did) == "evaluation"
    other, _ = vdb.donation(sid, name="Other")  # no evidence: not accounting-relevant
    assert other
    report = vdb.verify()
    assert report["status"] == "clean" and report["exit_code"] == 0, report["findings"]
    assert report["counts"] == {"processed": 1}
    # CR-1: no 6a-3 second reconcile result exists, so activation is never certified complete by verify
    assert report["gates"] == {"reconcile_precondition": True, "recovery_precondition": True,
                               "activation_verification_ready": True, "activation_complete": False}
    assert report["not_attested"] == list(verify.NOT_ATTESTED)
    assert vdb.state() == ("active", "apply")  # the report changed and authorized nothing


def test_the_verifier_modifies_nothing_and_issues_only_selects(vdb):
    vdb.go("active", "hold")
    sid = vdb.creator()
    for i in range(3):
        did, pid = vdb.donation(sid, name=f"fan{i}")
        vdb.evidence(did, pid, "refund", f"rfnd_{secrets.token_hex(4)}", "processed", 1000 * (i + 1))
        vdb.evidence(did, pid, "dispute", f"disp_{secrets.token_hex(4)}", "lost", deducted=3000)
        vdb.settle(did)
    vdb.go("frozen")
    before = vdb.checksum()
    statements = []

    class Recording(psycopg.Cursor):
        def execute(self, query, params=None, **kw):
            statements.append(str(query))
            return super().execute(query, params, **kw)

    with vdb.conn("hyperchat_readonly", cursor_factory=Recording) as c:
        report = verify.verify(c)
    assert report["status"] != "refused"
    assert vdb.checksum() == before
    heads = [s.lstrip().split(None, 1)[0].upper() for s in statements]
    assert heads[:3] == ["BEGIN", "SET", "SET"] and heads[-1] == "ROLLBACK"
    assert set(heads[3:-1]) <= {"SELECT", "WITH"}, set(heads)
    assert statements[0] == "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"


@pytest.mark.parametrize("role", ["hyperchat_app", "hyperchat_worker", "hyperchat_migrator"])
def test_verify_refuses_any_role_but_readonly(vdb, role):
    report = vdb.verify(role=role)
    assert report["status"] == "refused" and report["exit_code"] == 2 and not any(report["gates"].values())
    assert report["findings"] == []


def test_verify_refuses_a_connection_with_an_open_transaction_or_a_bad_scope(vdb):
    with vdb.conn("hyperchat_readonly") as c:
        c.autocommit = False
        assert verify.verify(c)["status"] == "refused"
    assert vdb.verify(creator="not-a-uuid")["status"] == "refused"
    assert vdb.verify(creator=vdb.creator(), donation=vdb.creator())["status"] == "refused"


def test_6a2_refuses_a_head_other_than_0034(vdb):
    with vdb.owner() as c:
        c.execute("UPDATE public.alembic_version SET version_num = '0035'")
    report = vdb.verify()
    assert report["status"] == "refused" and "0034" in report["reason"]


def test_the_readonly_role_itself_cannot_write(vdb):
    with vdb.conn("hyperchat_readonly") as c, pytest.raises(psycopg.Error):
        c.execute("UPDATE public.payment_accounting_state SET state = state")


def test_a_scoped_report_judges_its_scope_and_sets_every_gate_false(vdb):
    vdb.go("active", "apply")
    a, b = vdb.creator(), vdb.creator()
    da, pa = vdb.donation(a)
    vdb.evidence(da, pa, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)
    vdb.settle(da)
    db_, pb = vdb.donation(b)
    vdb.evidence(db_, pb, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)  # never settled
    full = vdb.verify()
    assert full["status"] == "attention"
    scoped = vdb.verify(creator=a)
    assert scoped["status"] == "clean" and scoped["scope"] == {"kind": "creator", "id": a, "complete": False}
    assert not any(scoped["gates"].values())
    assert vdb.verify(donation=db_)["status"] == "attention"


def test_the_operator_cli_prints_the_canonical_report_and_exits_with_its_code(vdb, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HYPERCHAT_READONLY_DSN", f"host={PG_HOST} port={vdb.port} dbname={vdb.name} "
                                                 "user=hyperchat_readonly")
    out = tmp_path / "report.json"
    assert operator.main(["verify", "--output", str(out)]) == 0
    printed = capsys.readouterr().out.strip()
    report = json.loads(printed)
    assert printed == out.read_text().strip() == verify.canonical(report)
    body = {k: v for k, v in report.items() if k != "digest"}
    assert report["digest"] == __import__("hashlib").sha256(verify.canonical(body).encode()).hexdigest()
    monkeypatch.setenv("HYPERCHAT_READONLY_DSN", f"host={PG_HOST} port={vdb.port} dbname={vdb.name} "
                                                 "user=hyperchat_app")
    assert operator.main(["verify"]) == 2
    monkeypatch.delenv("HYPERCHAT_READONLY_DSN")
    assert operator.main(["verify"]) == 2
    for command in ("state", "reconcile", "resolve", "revert"):
        assert operator.main([command]) == 2


# ── classification on real flows ──────────────────────────────────────────────────────────────────────────────────

def test_evidence_while_off_is_awaiting_exit_3(vdb):
    sid = vdb.creator()  # state off, no transition ever: nothing near a boundary
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)
    assert vdb.settle(did) == "inactive"
    report = vdb.verify()
    assert report["counts"] == {"awaiting_reconciliation": 1} and report["exit_code"] == 3
    assert report["findings"][0]["entity_id"] == did and not any(report["gates"].values())


def test_evidence_just_after_a_freeze_is_boundary_uncertain_never_clean(vdb):
    vdb.go("frozen", "apply")  # seconds ago: inside the D-15 margin of the freeze
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)
    report = vdb.verify()
    assert report["counts"] == {"boundary_uncertain": 1} and report["exit_code"] == 3


def test_reconcile_after_reactivation_reconciles_and_mode_change_is_o_mode(vdb):
    vdb.go("active", "apply")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)
    vdb.settle(did)
    vdb.go("frozen", "hold")  # F-9: the mode is in the fingerprint
    report = vdb.verify()
    (f,) = report["findings"]
    # the mode change is the origin; the freeze seconds before it puts it inside the D-15 margin
    assert f["class"] in ("awaiting_reconciliation", "boundary_uncertain") and f["detail"]["origin_kind"] == "o_mode"
    assert report["exit_code"] == 3
    vdb.go("active")
    run = vdb.run("reconcile", [did])
    vdb.reconcile(did, run)
    after = vdb.verify()
    assert after["counts"] == {"processed": 1}, after["findings"]


def test_m11_refunds_only_hold_is_processed_and_reports_its_unapplied_refunds(vdb):
    vdb.go("active", "hold")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)
    vdb.settle(did)
    vdb.evidence(did, pid, "dispute", "disp_" + secrets.token_hex(4), "lost", deducted=4000)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 1000)
    assert vdb.settle(did) == "hold"
    report = vdb.verify()
    assert report["counts"] == {"processed": 1}, report["findings"]


def test_keep_exception_and_forged_ack(vdb):
    vdb.go("active", "hold")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "dispute", "disp_" + secrets.token_hex(4), "lost", deducted=4000)
    assert vdb.settle(did) == "hold"
    run = vdb.run("resolve-keep", [did])
    assert vdb.keep(did, run) == "kept"
    genuine = vdb.verify()  # A1 (Decision 5): possibly valid, never a proven exception on 0034
    (amb,) = found(genuine, "I12", "historical_ambiguous")
    assert amb["detail"]["reason"] == "ack_mode_order" and amb["detail"]["basis"] == "evidence", genuine["findings"]
    assert "processed" not in genuine["counts"] and not genuine["gates"]["activation_verification_ready"]
    with vdb.owner() as c:  # M14: an owner rewrites the acknowledged fingerprint
        c.execute("UPDATE public.donation_accounting_evaluations SET ack_fp = md5('forged'), evidence_fp = "
                  "md5('forged') WHERE donation_id = %s", (did,))
    report = vdb.verify()
    assert found(report, "I12", "audit_integrity", "forged_ack") and report["exit_code"] == 1
    # rev. 3 N2: a forged ack is audit_integrity, never also a pending / timing class for that donation
    assert not [f for f in report["findings"] if f["entity_id"] == did and f["invariant"] == "I13"]


def test_a_keep_under_an_unscoped_run_is_an_audit_violation(vdb):
    vdb.go("active", "hold")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "dispute", "disp_" + secrets.token_hex(4), "lost", deducted=4000)
    vdb.settle(did)
    run = vdb.run("resolve-keep", [], scope={})  # the 6a-1 tests' runs: used with an empty scope
    vdb.keep(did, run)
    unused = vdb.run("revert", [], scope={})
    report = vdb.verify()
    assert found(report, "D-10", "audit_integrity", "unscoped_used_run")[0]["entity_id"] == str(run)
    assert found(report, "D-10", "unscoped_unused_run")[0]["entity_id"] == str(unused)
    assert report["status"] == "violation"


def test_a_valid_revert_agrees_while_frozen_and_needs_reconciliation_once_active(vdb):
    vdb.go("active", "apply")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)
    vdb.settle(did)
    vdb.go("frozen")
    assert vdb.revert(did, vdb.run("revert", [did])) == "reverted"
    frozen = vdb.verify()
    assert frozen["counts"] == {"processed": 1} and frozen["findings"][0]["detail"]["exception"] == "revert"
    vdb.go("active")
    active = vdb.verify()
    (f,) = active["findings"]
    assert f["class"] in ("pending_reconciliation", "boundary_uncertain") and active["exit_code"] == 3


def test_ir5_t1_an_evaluation_that_waited_for_the_activation_lock_is_reconciled(vdb):
    """CE-2: R's transaction starts before the activation A, waits for the state lock, evaluates once A commits.
    ev.evaluated_at (R's start) precedes A.created_at; W6 (C-IR5) still sees it reconciled: ev.state = 'active'."""
    vdb.go("frozen", "apply")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)
    r = vdb.conn()
    r.autocommit = False
    r.execute("SELECT 1")  # R's transaction (and its now()) starts here
    time.sleep(0.05)
    a = vdb.owner()
    a.autocommit = False
    a.execute("SELECT public.payment_accounting_set_state('active', NULL, 'op-verify', 'activation (IR5-T1)', NULL)")
    outcome = {}
    t = threading.Thread(target=lambda: outcome.update(o=vdb.settle(did, conn=r)))
    t.start()
    time.sleep(0.5)
    with psycopg.connect(os.environ["HYPERCHAT_TEST_VERIFY_ADMIN"], autocommit=True) as m:  # superuser: sees waits
        waiting = m.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND wait_event_type = 'Lock'",
                            (vdb.name,)).fetchone()[0]
    a.commit()
    t.join(10)
    r.commit()
    r.close()
    a.close()
    assert waiting >= 1 and outcome["o"] == "evaluation"
    with vdb.owner() as c:
        ev_at = c.execute("SELECT evaluated_at FROM public.donation_accounting_evaluations WHERE donation_id = %s",
                          (did,)).fetchone()[0]
        a_at = c.execute("SELECT max(created_at) FROM public.payment_accounting_events WHERE to_state = 'active'"
                         ).fetchone()[0]
    assert ev_at < a_at  # the old rule (evaluated_at >= t_W) would have called this unreconciled
    report = vdb.verify()
    assert report["counts"] == {"processed": 1}, report["findings"]


def test_ir2_t4_evidence_during_an_uncommitted_activation_is_never_missed(vdb):
    vdb.go("frozen", "apply")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    a = vdb.owner()
    a.autocommit = False
    a.execute("SELECT public.payment_accounting_set_state('active', NULL, 'op-verify', 'a long activation', NULL)")
    time.sleep(1.0)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)  # flag-off path: no S lock
    time.sleep(1.0)
    a.commit()
    a.close()
    report = vdb.verify()
    assert not classes_of(report) & {"missed_evaluation", "overdue_reconciliation"}
    assert report["exit_code"] == 3 and classes_of(report) <= {"in_flight", "boundary_uncertain"}


# ── Erratum 1: cases A, B, C and A-1 to A-5 ───────────────────────────────────────────────────────────────────────

def _erratum_start(vdb):
    """Creator C, bucket (C, 'fan'): r1 D1 +100, r2 D2 +50, r3 D1 rejected -100; totals 50."""
    sid = vdb.creator()
    d1, _ = vdb.donation(sid, 100, name="Fan")
    d2, _ = vdb.donation(sid, 50, name="Fan")
    with vdb.conn() as c:
        c.execute("UPDATE public.donations SET moderation_status = 'rejected' WHERE id = %s", (d1,))
    return sid, d1, d2


def _delete_donations(vdb, *dids, guard_off=False):
    with vdb.owner() as c, c.transaction():
        if guard_off:
            c.execute("ALTER TABLE public.donations DISABLE TRIGGER donations_credit_delete_guard")
        c.execute("DELETE FROM public.donations WHERE id = ANY(%s::uuid[])", (list(dids),))
        if guard_off:
            c.execute("ALTER TABLE public.donations ENABLE TRIGGER donations_credit_delete_guard")


def _no_violation(report):
    return not [f for f in report["findings"] if verify.SEVERITY[f["class"]] == "violation"]


def test_erratum_case_a_deleting_an_uncredited_donation_passes(vdb):
    sid, d1, d2 = _erratum_start(vdb)
    with vdb.conn() as c:
        c.execute("DELETE FROM public.donations WHERE id = %s", (d1,))  # credited 0: the guard allows it
    report = vdb.verify()
    assert report["status"] == "clean", report["findings"]


def test_erratum_case_b_whole_creator_cleanup_is_excluded_and_counted(vdb):
    sid, d1, d2 = _erratum_start(vdb)
    _delete_donations(vdb, d1, d2, guard_off=True)
    with vdb.owner() as c:
        c.execute("DELETE FROM public.streamers WHERE id = %s", (sid,))  # the totals cascade
    report = vdb.verify()
    assert _no_violation(report), report["findings"]
    assert report["orphans"] == {"orphan_buckets": 1, "orphan_rows": 3}  # Erratum 1: r1, r2, r3
    (bucket,) = report["excluded_buckets"]
    assert bucket["accounting_rows"] == 0 and Decimal(bucket["net_credit"]) == Decimal("50.00")
    assert classes_of(report) == {"excluded_bucket_net_credit"} and report["exit_code"] == 3


def test_erratum_case_c_deleting_a_credited_donation_is_an_i3o_violation(vdb):
    sid, d1, d2 = _erratum_start(vdb)
    _delete_donations(vdb, d2, guard_off=True)
    report = vdb.verify()
    (f,) = found(report, "I3-O")
    assert f["entity_id"] == d2 and Decimal(f["detail"]["net"]) == Decimal("50.00") and report["exit_code"] == 1


def test_erratum_a1_a_deleted_totals_row_is_p2_and_i5(vdb):
    sid, d1, d2 = _erratum_start(vdb)
    with vdb.conn() as c:
        c.execute("DELETE FROM public.donations WHERE id = %s", (d1,))
    with vdb.owner() as c:
        c.execute("DELETE FROM public.streamer_donator_totals WHERE streamer_id = %s", (sid,))
    report = vdb.verify()
    assert found(report, "I6", problem="P2") and found(report, "I5")


def test_erratum_a2_an_out_of_band_total_edit_is_p2(vdb):
    sid, d1, d2 = _erratum_start(vdb)
    with vdb.owner() as c:
        c.execute("UPDATE public.streamer_donator_totals SET total_amount = total_amount + 1 WHERE streamer_id = %s",
                  (sid,))
    assert found(vdb.verify(), "I6", problem="P2")


def test_erratum_a3_an_injected_writer_row_breaking_the_chain_is_p1(vdb):
    sid, d1, d2 = _erratum_start(vdb)
    with vdb.owner() as c:
        c.execute("INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause, "
                  "payment_status, moderation_status, credited_before, credited_after, delta_inr, delta_count, "
                  "total_after, actor, reversed_before_paise, reversed_after_paise) "
                  "VALUES (%s, %s, 'fan', 'writer', 'success', 'approved', 50, 60, 10, 0, 999, 'x', 0, 0)", (d2, sid))
    assert found(vdb.verify(), "I6", problem="P1")


def test_erratum_a4_an_orphan_with_two_buckets_is_an_identity_violation(vdb):
    sid, d1, d2 = _erratum_start(vdb)
    other = vdb.creator()
    with vdb.owner() as c:
        c.execute("INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause, "
                  "payment_status, moderation_status, credited_before, credited_after, delta_inr, delta_count, "
                  "total_after, actor) VALUES (%s, %s, 'fan', 'writer', 'success', 'approved', 0, 0, 0, 1, 0, 'x')",
                  (d1, other))
    with vdb.conn() as c:
        c.execute("DELETE FROM public.donations WHERE id = %s", (d1,))
    assert found(vdb.verify(), "I9", problem="orphan_moved_bucket")


def test_erratum_a5_a_missing_totals_row_in_a_live_bucket_is_never_excluded(vdb):
    sid, d1, d2 = _erratum_start(vdb)
    with vdb.owner() as c:
        c.execute("DELETE FROM public.streamer_donator_totals WHERE streamer_id = %s", (sid,))
    report = vdb.verify()
    assert found(report, "I6", problem="P2") and report["orphans"]["orphan_buckets"] == 0


def test_m15_a_deleted_creator_with_accounting_history_blocks_activation(vdb):
    vdb.go("active", "apply")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)
    vdb.settle(did)
    with vdb.owner() as c, c.transaction():
        for t, g in (("payment_integrity_events", "payment_integrity_events_guard"),
                     ("payment_webhook_events", "payment_webhook_events_guard")):
            c.execute(f"ALTER TABLE public.{t} DISABLE TRIGGER {g}")
        c.execute("DELETE FROM public.payment_integrity_events WHERE donation_id = %s", (did,))
        c.execute("DELETE FROM public.payment_webhook_events WHERE donation_id = %s", (did,))
        for t, g in (("payment_integrity_events", "payment_integrity_events_guard"),
                     ("payment_webhook_events", "payment_webhook_events_guard")):
            c.execute(f"ALTER TABLE public.{t} ENABLE TRIGGER {g}")
        c.execute("DELETE FROM public.donation_reversals WHERE donation_id = %s", (did,))
        c.execute("DELETE FROM public.donation_accounting_evaluations WHERE donation_id = %s", (did,))
        c.execute("ALTER TABLE public.donations DISABLE TRIGGER donations_credit_delete_guard")
        c.execute("DELETE FROM public.donations WHERE id = %s", (did,))
        c.execute("ALTER TABLE public.donations ENABLE TRIGGER donations_credit_delete_guard")
        c.execute("DELETE FROM public.streamers WHERE id = %s", (sid,))
    report = vdb.verify()
    assert "deleted_creator_with_accounting_history" in classes_of(report)
    assert report["exit_code"] == 3 and not report["gates"]["activation_complete"]


# ── I1-I15 corruption fixtures (no false pass) ────────────────────────────────────────────────────────────────────

def _paid_reversed(vdb, amount=100, refund=2500, **kw):
    vdb.go("active", "apply")
    sid = vdb.creator()
    did, pid = vdb.donation(sid, amount, **kw)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", refund,
                 currency=kw.get("currency", "INR"))
    assert vdb.settle(did) == "evaluation"
    return sid, did, pid


def test_i1_i2_a_credit_outside_its_bounds_with_the_check_dropped(vdb):
    sid, did, _ = _paid_reversed(vdb)
    with vdb.owner() as c, c.transaction():
        c.execute("ALTER TABLE public.donations DROP CONSTRAINT donations_credited_bounds")
        c.execute("ALTER TABLE public.donations DISABLE TRIGGER donations_credit_writer")
        c.execute("UPDATE public.donations SET credited_inr = amount_inr + 5 WHERE id = %s", (did,))
        c.execute("ALTER TABLE public.donations ENABLE TRIGGER donations_credit_writer")
    report = vdb.verify()
    assert found(report, "I1") and found(report, "I2") and found(report, "I3")


def test_i2_i4_a_reversal_changed_without_touching_the_donation(vdb):
    sid, did, _ = _paid_reversed(vdb)
    with vdb.owner() as c:
        c.execute("UPDATE public.donation_reversals SET applied_reversed_paise = 5000 WHERE donation_id = %s", (did,))
    report = vdb.verify()
    assert found(report, "I2") and found(report, "I4", problem="reversal_state")
    assert report["exit_code"] == 1 and not report["gates"]["reconcile_precondition"]


def test_m12_i12h_an_arbitrary_amount_with_a_matching_history_row_is_unsupported(vdb):
    sid, did, _ = _paid_reversed(vdb, refund=2500)
    with vdb.owner() as c:
        refs = c.execute("SELECT evidence_refs FROM public.donation_credit_events WHERE donation_id = %s AND "
                         "cause = 'evaluation'", (did,)).fetchone()[0]
        c.execute("INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause, "
                  "payment_status, moderation_status, credited_before, credited_after, delta_inr, delta_count, "
                  "total_after, actor, reversed_before_paise, reversed_after_paise, evidence_refs) "
                  "SELECT id, streamer_id, lower(donor_name), 'evaluation', payment_status, moderation_status, "
                  "credited_inr, credited_inr, 0, 0, NULL, 'x', 2500, 5000, %s FROM public.donations WHERE id = %s",
                  (refs, did))
        c.execute("UPDATE public.donation_reversals SET applied_reversed_paise = 5000 WHERE donation_id = %s", (did,))
        c.execute("UPDATE public.donations SET updated_at = now() WHERE id = %s", (did,))  # the writer follows
    report = vdb.verify()
    assert found(report, "I12-H", "structural", "unsupported_amount")


def test_i8_accounting_state_on_an_imported_donation(vdb):
    sid = vdb.creator()
    did, _ = vdb.donation(sid, provenance="imported")
    with vdb.owner() as c:
        c.execute("INSERT INTO public.donation_accounting_evaluations (donation_id, evidence_fp, evaluated_at, "
                  "state, dispute_mode, outcome) VALUES (%s, 'x', now(), 'active', 'hold', 'evaluation')", (did,))
    assert found(vdb.verify(), "I8")[0]["entity_id"] == did


def test_i9_a_history_row_of_another_creator(vdb):
    sid, did, _ = _paid_reversed(vdb)
    other = vdb.creator()
    with vdb.owner() as c:
        c.execute("INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause, "
                  "payment_status, moderation_status, credited_before, credited_after, delta_inr, delta_count, "
                  "total_after, actor, reversed_before_paise, reversed_after_paise, hold_before, hold_after) "
                  "SELECT id, %s, lower(donor_name), 'hold', payment_status, moderation_status, credited_inr, "
                  "credited_inr, 0, 0, NULL, 'x', 2500, 2500, NULL, 'H-CONFLICT' FROM public.donations WHERE id = %s",
                  (other, did))
    assert found(vdb.verify(), "I9")


@pytest.mark.parametrize("sabotage, problem", [
    ("ALTER TABLE public.payment_accounting_runs DISABLE TRIGGER payment_accounting_runs_guard", "enabled=D"),
    ("GRANT INSERT ON public.donation_reversals TO hyperchat_app", "insert_granted"),
    ("ALTER FUNCTION public.payment_accounting_set_state(text, text, text, text, text) RESET lock_timeout",
     "lock_timeout"),
    ("GRANT EXECUTE ON FUNCTION public.payment_accounting_keep(uuid, bigint) TO hyperchat_worker",
     "execute_granted"),
    ("GRANT EXECUTE ON FUNCTION public.donation_accounting_core(uuid, bigint, text, text[], text[], text, bigint) "
     "TO PUBLIC", "public_execute"),
    ("ALTER FUNCTION public.payment_evidence_fp(uuid) SECURITY INVOKER", "security"),
])
def test_i10_catalog_sabotage_is_structural(vdb, sabotage, problem):
    assert vdb.verify()["status"] == "clean"
    with vdb.owner() as c:
        c.execute(sabotage)
    report = vdb.verify()
    assert found(report, "I10", "structural", problem), report["findings"]


def test_i10_a_redefined_function_body_is_drift(vdb):
    with vdb.owner() as c:
        src = c.execute("SELECT prosrc FROM pg_proc WHERE proname = 'payment_accounting_open_run'").fetchone()[0]
        c.execute("CREATE OR REPLACE FUNCTION public.payment_accounting_open_run(p_command text, p_operator text, "
                  "p_reason text, p_ref text, p_scope jsonb) RETURNS bigint LANGUAGE sql SECURITY DEFINER "
                  "SET search_path = pg_catalog, public AS $f$" + src + " $f$")
    assert found(vdb.verify(), "I10", problem="body_drift")


def test_i11_a_reversal_above_the_captured_amount(vdb):
    sid, did, _ = _paid_reversed(vdb)
    with vdb.owner() as c:
        c.execute("ALTER TABLE public.donation_reversals DROP CONSTRAINT donation_reversals_bounds")
        c.execute("UPDATE public.donation_reversals SET applied_reversed_paise = 20000 WHERE donation_id = %s", (did,))
    assert found(vdb.verify(), "I11")


def test_i14_two_accounting_rows_in_one_transaction(vdb):
    sid, did, _ = _paid_reversed(vdb)
    with vdb.owner() as c, c.transaction():
        for hold in ("H-CONFLICT", "H-CURRENCY"):
            c.execute("INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, "
                      "cause, payment_status, moderation_status, credited_before, credited_after, delta_inr, "
                      "delta_count, total_after, actor, reversed_before_paise, reversed_after_paise, hold_before, "
                      "hold_after) SELECT id, streamer_id, lower(donor_name), 'hold', payment_status, "
                      "moderation_status, credited_inr, credited_inr, 0, 0, NULL, 'x', 2500, 2500, NULL, %s "
                      "FROM public.donations WHERE id = %s", (hold, did))
    assert found(vdb.verify(), "I14")


def test_i15_non_inr_figures_agree_with_the_python_reference(vdb):
    sid, did, _ = _paid_reversed(vdb, amount=50, refund=1234, currency="USD", amount_inr=4183)
    report = vdb.verify()
    assert not found(report, "I15") and report["counts"] == {"processed": 1}, report["findings"]


# ── D-1 fingerprint equality and the view replica ─────────────────────────────────────────────────────────────────

def test_d1_the_inline_fingerprint_equals_payment_evidence_fp_in_both_modes(vdb):
    vdb.go("frozen", "hold")
    r = random.Random(20261115)
    sid = vdb.creator()
    dids = []
    for i in range(8):
        did, pid = vdb.donation(sid, name=f"fan{i}", paid=r.random() < .8)
        dids.append(did)
        for j in range(r.randint(0, 4)):
            kind = r.choice(["refund", "dispute"])
            vdb.evidence(did, pid if r.random() < .8 else "pay_TESTother" + secrets.token_hex(3), kind,
                         f"{'rfnd' if kind == 'refund' else 'disp'}_{secrets.token_hex(4)}",
                         r.choice(["processed", "pending"]) if kind == "refund" else r.choice(["lost", "won", "open"]),
                         r.choice([100, 2500]), deducted=r.choice([None, 0, 3000]), at=j,
                         match=r.choice(["payment", "order"]))
    empty, _ = vdb.donation(sid, name="empty")
    dids.append(empty)
    for mode in ("hold", "apply"):
        vdb.set_state(None, mode) if vdb.state()[1] != mode else None
        with vdb.owner() as c:
            owner_fp = {d: c.execute("SELECT public.payment_evidence_fp(%s)", (d,)).fetchone()[0] for d in dids}
        with vdb.conn("hyperchat_readonly") as c, c.cursor(row_factory=dict_row) as cur:
            mine = {row["id"]: row["fp"] for row in verify._select(cur, verify.FP_NOW, {"mode": mode, "ids": dids})}
        assert mine == owner_fp, mode
    assert not found(vdb.verify(), "I10", problem="view_replica_mismatch")


def test_the_cut_replica_equals_the_view_and_hides_later_evidence(vdb):
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    ref = "disp_" + secrets.token_hex(4)
    vdb.evidence(did, pid, "dispute", ref, "lost", deducted=3000, at=1)
    with vdb.owner() as c:
        cut = c.execute("SELECT clock_timestamp()").fetchone()[0]
    time.sleep(0.05)
    vdb.evidence(did, pid, "dispute", ref, "won", at=2, phase="pre_arbitration")
    with vdb.conn("hyperchat_readonly") as c, c.cursor(row_factory=dict_row) as cur:
        now = verify._select(cur, verify.CUT_ROWS, {"d": did, "cut": "infinity"})
        then = verify._select(cur, verify.CUT_ROWS, {"d": did, "cut": cut})
        view = verify._select(cur, verify.CURRENT + " AND donation_id = %(d)s::uuid", {"d": did})
    assert [dict(x) for x in now] == [dict(x) for x in view] and now[0]["status"] == "won"
    assert then[0]["status"] == "lost"


def test_the_dirty_shared_6a1_style_runs_are_reported_as_contamination(vdb):
    """Plan §9 contamination rule on a database with the 6a-1 suites' runs (open_run(..., '{}') then used)."""
    vdb.go("frozen", "apply")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)  # arrives while frozen
    vdb.go("active")
    run = vdb.run("reconcile", [], scope={})
    assert vdb.reconcile(did, run) == "activation"  # a history row now names the unscoped run
    report = vdb.verify()
    assert found(report, "D-10", "audit_integrity", "unscoped_used_run") and report["exit_code"] == 1


# ── plan rev. 3 N- and S-series on PostgreSQL (adapted to revision 10) ────────────────────────────────────────────

def _owner_run(vdb, command, scope, created_at) -> int:
    """An operator run as the owner could insert it directly (runs are append-only, not insert-guarded)."""
    with vdb.owner() as c:
        return c.execute("INSERT INTO public.payment_accounting_runs (command, operator, reason, scope, db_user, "
                         "created_at) VALUES (%s, 'op-verify', 'a verify test run', %s, 'x', %s) RETURNING id",
                         (command, json.dumps(scope), created_at)).fetchone()[0]


def _dm_hold(vdb):
    vdb.go("active", "hold")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "dispute", "disp_" + secrets.token_hex(4), "lost", deducted=4000)
    assert vdb.settle(did) == "hold"
    return sid, did


def test_a_keep_stays_valid_after_its_run_expires_and_later_acknowledged_evaluations(vdb):
    """The ack check is anchored on the keep's history row, not on evaluated_at (refreshed by every later
    'acknowledged' evaluation): an ordinary keep never turns into a scope violation once its run expires."""
    sid, did = _dm_hold(vdb)
    run = vdb.run("resolve-keep", [], scope={"kind": "donations", "donations": [did],
                                             "expires_at": (datetime.now(UTC) + timedelta(seconds=2)).isoformat()})
    assert vdb.keep(did, run) == "kept"
    time.sleep(2.5)
    assert vdb.settle(did) == "acknowledged"  # the same evidence: the core refreshes evaluated_at
    report = vdb.verify()
    assert not found(report, "D-10") and report["counts"] == {"historical_ambiguous": 1}, report["findings"]  # A1


def test_s5_s8_s9_keeps_outside_their_run_scope_are_violations_and_never_exceptions(vdb):
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    for scope in ({"kind": "donations", "donations": [vdb.creator()], "expires_at": expires},     # S5: another id
                  {"kind": "creator", "creator": vdb.creator(), "expires_at": expires},           # S8
                  {"kind": "all_v2", "expires_at": expires}):                                     # S9: keep
        sid, did = _dm_hold(vdb)
        vdb.keep(did, vdb.run("resolve-keep", [], scope=scope))
        report = vdb.verify(donation=did)
        assert found(report, "D-10", "audit_integrity", "scope_mismatch"), scope["kind"]
        assert not [f for f in report["findings"] if f["class"] == "processed"]  # not an I12 exception


def test_s6_a_use_after_expires_at_is_a_violation(vdb):
    sid, did = _dm_hold(vdb)
    opened = datetime.now(UTC) - timedelta(hours=2)
    run = _owner_run(vdb, "resolve-keep", {"kind": "donations", "donations": [did],
                                           "expires_at": (opened + timedelta(hours=1)).isoformat()}, opened)
    vdb.keep(did, run)
    assert found(vdb.verify(), "D-10", "audit_integrity", "scope_mismatch")


def test_s10_a_recovery_scope_used_beyond_its_listed_donations_flags_the_extras(vdb):
    vdb.go("frozen", "apply")
    sid = vdb.creator()
    listed, lp = vdb.donation(sid, name="listed")
    extra, ep = vdb.donation(sid, name="extra")
    for did, pid in ((listed, lp), (extra, ep)):
        vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)
    vdb.go("active")
    run = vdb.run("reconcile", [], scope={"kind": "recovery", "donations": [listed], "verify_report_sha256": "a" * 64,
                                          "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()})
    assert vdb.reconcile(listed, run) == vdb.reconcile(extra, run) == "activation"
    mismatched = {f["detail"]["donation"] for f in found(vdb.verify(), "D-10", "audit_integrity", "scope_mismatch")}
    assert mismatched == {extra}


def test_s4_a_malformed_scope_is_a_violation_even_unused(vdb):
    vdb.run("revert", [], scope={"kind": "donations", "donations": []})
    assert found(vdb.verify(), "D-10", "audit_integrity", "malformed_scope")


def test_n1_a_pending_backlog_with_a_corrupted_credit_is_structural_with_every_gate_false(vdb):
    vdb.go("frozen", "apply")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)  # a backlog item
    vdb.go("active")
    with vdb.owner() as c, c.transaction():
        c.execute("ALTER TABLE public.donations DISABLE TRIGGER donations_credit_writer")
        c.execute("UPDATE public.donations SET credited_inr = credited_inr - 1 WHERE id = %s", (did,))
        c.execute("ALTER TABLE public.donations ENABLE TRIGGER donations_credit_writer")
    report = vdb.verify()
    assert found(report, "I2") and not [f for f in report["findings"] if f["invariant"] == "I13"]
    assert report["exit_code"] == 1 and not any(report["gates"].values())


def test_n3_a_revert_row_naming_a_missing_run_is_audit_integrity(vdb):
    sid, did, _ = _paid_reversed(vdb)
    with vdb.owner() as c:
        c.execute("INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause, "
                  "payment_status, moderation_status, credited_before, credited_after, delta_inr, delta_count, "
                  "total_after, actor, reversed_before_paise, reversed_after_paise, run_id) "
                  "SELECT id, streamer_id, lower(donor_name), 'revert', payment_status, moderation_status, "
                  "credited_inr, credited_inr, 0, 0, NULL, 'x', 2500, 0, 987654 FROM public.donations WHERE id = %s",
                  (did,))
    assert found(vdb.verify(), "D-10", "audit_integrity", "missing_run")


def test_n16_a_commit_during_verify_is_invisible_to_its_snapshot(vdb):
    vdb.go("active", "apply")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)
    vdb.settle(did)
    seen = []

    class CommitMidway(psycopg.Cursor):
        def execute(self, query, params=None, **kw):
            seen.append(str(query))
            if len(seen) == 6:  # after the snapshot-taking first statement and the first data reads
                vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 1000)
            return super().execute(query, params, **kw)

    with vdb.conn("hyperchat_readonly", cursor_factory=CommitMidway) as c:
        during = verify.verify(c)
    assert len(seen) > 6 and during["counts"] == {"processed": 1}, during["findings"]  # the new evidence: not seen
    after = vdb.verify()
    assert "processed" not in after["counts"] and after["exit_code"] == 3  # the next snapshot sees it


# ── CR-2: historical dispute mode under transaction-start timestamps (real concurrency) ───────────────────────────

def _long_mode_change(vdb):
    """An owner transaction that STARTS now (its now(), hence the event's created_at, is fixed here) and changes
    the mode only later: the 0034 event then carries a timestamp earlier than accounting work that committed
    before it."""
    m = vdb.conn("hyperchat_migrator")
    m.autocommit = False
    m.execute("SET ROLE hyperchat_owner")  # the transaction (and its now()) starts here
    time.sleep(0.2)
    return m


def _commit_mode(m, mode):
    m.execute("SELECT public.payment_accounting_set_state(NULL, %s, 'op-verify', 'a long mode change (CR-2)', NULL)",
              (mode,))
    m.commit()
    m.close()


def _apply_mode_loss(vdb):
    """Active in mode apply: a refund and a lost dispute, evaluated clear at max(R, L) = 4000."""
    vdb.go("active", "apply")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)
    vdb.evidence(did, pid, "dispute", "disp_" + secrets.token_hex(4), "lost", deducted=4000)
    return sid, did


def _event_times(vdb):
    with vdb.owner() as c:
        return c.execute("SELECT id, from_state, to_state, from_mode, to_mode, created_at FROM "
                         "public.payment_accounting_events ORDER BY id").fetchall()


def test_cr2_a_an_evaluation_under_the_previous_mode_stays_supported_despite_an_earlier_stamped_mode_change(vdb):
    """Reachable under 0034: M starts, R evaluates (mode apply) and commits, a freeze commits, M changes the mode to
    hold and commits. M's event carries M's start time, earlier than R's row. R was legitimately evaluated in mode
    apply and no hold-mode active period exists: the row is supported, and nothing is a violation."""
    sid, did = _apply_mode_loss(vdb)
    m = _long_mode_change(vdb)
    assert vdb.settle(did) == "evaluation"  # R: clear 4000 under apply
    vdb.set_state("frozen")
    _commit_mode(m, "hold")
    with vdb.owner() as c:
        r_at = c.execute("SELECT created_at FROM public.donation_credit_events WHERE donation_id = %s AND cause = "
                         "'evaluation'", (did,)).fetchone()[0]
    mode_event = [e for e in _event_times(vdb) if e[3] != e[4]][-1]
    assert mode_event[5] < r_at  # the reachability proof: the mode change's stamp precedes R's row
    report = vdb.verify()
    violations = [f for f in report["findings"] if verify.SEVERITY[f["class"]] == "violation"]
    assert not violations, violations
    assert not found(report, "I12-H") and report["exit_code"] == 3
    (timing,) = found(report, "I13")  # the proven mode change, stamped before the evaluation: an uncertain origin
    assert timing["class"] == "boundary_uncertain" and timing["detail"]["origin_kind"] == "o_mode_uncertain"


def test_cr2_b_d2_a_lost_dispute_ref_proves_apply_so_the_row_is_exact_despite_a_hold_period(vdb):
    """As A, then reactivation in mode hold within the margin. Both active-period modes are possible by timestamps,
    but R's refs name a lost dispute: the core refuses a clear result in hold while one is current (Decision 2:
    rule-proven exclusion, not timestamp proximity), so R was written under apply and is exact (r4: mode_order)."""
    sid, did = _apply_mode_loss(vdb)
    m = _long_mode_change(vdb)
    assert vdb.settle(did) == "evaluation"
    vdb.set_state("frozen")
    _commit_mode(m, "hold")
    vdb.set_state("active")
    report = vdb.verify()
    assert not found(report, "I12-H"), report["findings"]
    assert not [f for f in report["findings"] if verify.SEVERITY[f["class"]] == "violation"], report["findings"]
    with vdb.owner() as c:  # M12-style: an owner-written row with the same refs and a forged amount is corruption
        refs = c.execute("SELECT evidence_refs FROM public.donation_credit_events WHERE donation_id = %s AND "
                         "cause = 'evaluation'", (did,)).fetchone()[0]
        c.execute("INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause, "
                  "payment_status, moderation_status, credited_before, credited_after, delta_inr, delta_count, "
                  "total_after, actor, reversed_before_paise, reversed_after_paise, evidence_refs) "
                  "SELECT id, streamer_id, lower(donor_name), 'evaluation', payment_status, moderation_status, "
                  "credited_inr, credited_inr, 0, 0, NULL, 'x', 4000, 3000, %s FROM public.donations WHERE id = %s",
                  (refs, did))
    assert found(vdb.verify(), "I12-H", "structural", "unsupported_amount")


def test_cr2_c_d3_a_keep_around_a_mode_transition_is_ack_mode_order_never_an_exception(vdb):
    vdb.go("active", "hold")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "dispute", "disp_" + secrets.token_hex(4), "lost", deducted=4000)
    assert vdb.settle(did) == "hold"
    vdb.set_state("frozen")
    m = _long_mode_change(vdb)
    assert vdb.keep(did, vdb.run("resolve-keep", [did])) == "kept"  # acknowledged under hold
    _commit_mode(m, "apply")      # stamped before the keep, committed after it
    vdb.set_state(None, "hold")   # back to hold: the ack is current again, so it is reconstructed
    report = vdb.verify()
    assert not found(report, "I12", "audit_integrity", "forged_ack"), report["findings"]
    assert not [f for f in report["findings"] if verify.SEVERITY[f["class"]] == "violation"], report["findings"]
    # Decision 3: the ack names hold, but the state also had apply and the keep's commit is unbounded: possibly
    # valid, never proven, so no keep exception and a permanent readiness block (r4: the exception was granted)
    (amb,) = [f for f in found(report, "I12", "historical_ambiguous") if f["detail"].get("reason") == "ack_mode_order"]
    assert not found(report, "I13", "processed") and report["exit_code"] == 3
    assert not report["gates"]["activation_verification_ready"]
    with vdb.owner() as c:  # the same timeline with a tampered acknowledgement: still forged
        c.execute("UPDATE public.donation_accounting_evaluations SET ack_fp = md5('tampered') WHERE donation_id = %s",
                  (did,))
    assert found(vdb.verify(), "I12", "audit_integrity", "forged_ack")


def test_cr2_d_refund_only_evidence_is_exact_despite_a_nearby_mode_change(vdb):
    vdb.go("active", "apply")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)
    m = _long_mode_change(vdb)
    assert vdb.settle(did) == "evaluation"
    vdb.set_state("frozen")
    _commit_mode(m, "hold")
    vdb.set_state("active")
    report = vdb.verify()
    assert not found(report, "I12-H"), report["findings"]  # the mode cannot change a refund-only amount


# ── Decision 4: a long reconcile whose evidence committed after its transaction start (real concurrency) ──────────

def test_d4_6_a_long_reconcile_on_evidence_committed_after_its_start_is_evidence_order(vdb):
    """Reachable under 0034: R starts (its now() stamps the row), waits longer than M, a second refund commits, R
    reconciles on the current evidence (READ COMMITTED) and commits. No evidence state within +-M of the stamp has
    R's refs, so the strict rule calls it corruption (r4: structural); the amended rule finds the later state and
    reports evidence_order - uncertain, permanent, blocking - never exact."""
    vdb.go("active", "apply")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)
    assert vdb.settle(did) == "evaluation"
    run = vdb.run("reconcile", [did])
    r = vdb.conn("hyperchat_migrator")
    r.autocommit = False
    r.execute("SET ROLE hyperchat_owner")  # R's transaction (and its now()) starts here
    time.sleep(verify.MARGIN.total_seconds() + 20)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 1000)  # committed after M
    with r.cursor(row_factory=dict_row) as cur:
        d, rows = vdb._donation(cur, did), vdb._current(cur, did)
        result = accounting.evaluate_in_mode(d, rows, "apply")
        assert result.reversed_paise == 3500 and len(rows) == 2  # R sees the late refund
        cur.execute("SELECT public.payment_accounting_reconcile(%s, %s, %s, %s, %s, %s) AS o",
                    (did, 3500, None, [], accounting.counted_refs(d, rows), run))
        r_start = cur.execute("SELECT now()").fetchone()["now"]
    r.commit()
    r.close()
    with vdb.owner() as c:
        row_at, late_at = c.execute(
            "SELECT (SELECT created_at FROM public.donation_credit_events WHERE donation_id = %s "
            "AND cause = 'activation' AND reversed_after_paise = 3500), "
            "(SELECT max(recorded_at) FROM public.payment_integrity_events WHERE donation_id = %s)",
            (did, did)).fetchone()
    assert row_at == r_start and late_at - row_at > verify.MARGIN  # the reachability proof
    report = vdb.verify()
    assert not found(report, "I12-H", "structural"), report["findings"]
    (amb,) = found(report, "I12-H", "historical_ambiguous")
    assert amb["detail"]["reason"] == "evidence_order" and amb["detail"]["stored"] == 3500
    assert report["exit_code"] == 3 and not report["gates"]["activation_verification_ready"]


# ── Decision 5 investigation: evidence committed out of recorded_at order (real lock waits, PG15 / PG17) ─────────
# payment_integrity_events.id is a random uuid (0029) and recorded_at is the transaction start, so neither orders
# commits. These pin the counterexamples to the recorded_at-prefix cut contract; the asserted outcome is the safe one
# (Decision 5, approved: S2, A1). Test B (rows) is the accepted limitation and stays strict-xfail.

def _app_tx(vdb):
    c = vdb.conn()
    c.autocommit = False
    return c


def _lock_path(c, did):
    """record_integrity's lock order (L-1): the accounting state first, then the donation."""
    c.execute("SELECT * FROM public.payment_accounting_lock_state()")
    c.execute("SELECT 1 FROM public.donations WHERE id = %s FOR UPDATE", (did,))


def _insert_evidence(c, did, pid, ref, amount):
    """The webhook event and its processed-refund evidence row inside the caller's transaction."""
    wid = c.execute(
        "INSERT INTO public.payment_webhook_events (event_id, event_type, account, provider_payment_id, donation_id, "
        "outcome, received_at) VALUES (%s, 'refund.processed', 'primary', %s, %s, 'integrity_recorded', now()) "
        "RETURNING id", ("authtest-vrf-" + secrets.token_hex(6), pid, did)).fetchone()[0]
    c.execute(
        "INSERT INTO public.payment_integrity_events (webhook_event_id, donation_id, match_type, account, kind, "
        "event_type, provider_ref, provider_payment_id, status, amount_paise, currency, provider_event_at, "
        "consistent, received_at) VALUES (%s, %s, 'payment', 'primary', 'refund', 'refund.processed', %s, %s, "
        "'processed', %s, 'INR', now(), true, now())", (wid, did, ref, pid, amount))


def _wait_for_lock(vdb, backend_pid):
    with vdb.conn() as c:
        for _ in range(200):
            row = c.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (backend_pid,)).fetchone()
            if row and row[0] == "Lock":
                return
            time.sleep(0.05)
    raise AssertionError("the second evidence transaction never waited on a lock")


def _late_starter(c, did, pid, ref, amount, *, after_state, go, done, settle):
    """E2: started first; waits (state lock, then the test's go) before taking the donation lock."""
    try:
        c.execute("SELECT * FROM public.payment_accounting_lock_state()")  # may wait on a freeze: now() is fixed
        after_state.set()
        go.wait(30)
        c.execute("SELECT 1 FROM public.donations WHERE id = %s FOR UPDATE", (did,))
        _insert_evidence(c, did, pid, ref, amount)
        done["outcome"] = settle(did, conn=c)
        c.commit()
    except BaseException as exc:  # surfaced by the test
        done["error"] = exc
        c.rollback()
    finally:
        c.close()


def _out_of_order_refunds(vdb):
    """E2 starts first and waits on the donation lock E1 holds; E1 records r1, settles on {r1} and commits; E2 then
    records r2 (stamped before r1) and settles on {r1, r2}. Commit order E1, E2; recorded_at order r2, r1."""
    vdb.go("active", "apply")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    r1, r2 = "rfnd_" + secrets.token_hex(4), "rfnd_" + secrets.token_hex(4)
    e2 = _app_tx(vdb)
    e2_pid = e2.execute("SELECT pg_backend_pid()").fetchone()[0]  # E2's transaction (and its now()) starts here
    time.sleep(0.2)
    e1 = _app_tx(vdb)
    _lock_path(e1, did)
    after_state, go, done = threading.Event(), threading.Event(), {}
    go.set()
    t = threading.Thread(target=_late_starter, args=(e2, did, pid, r2, 1000),
                         kwargs={"after_state": after_state, "go": go, "done": done, "settle": vdb.settle})
    t.start()
    _wait_for_lock(vdb, e2_pid)  # E2 genuinely waits on E1's donation lock
    _insert_evidence(e1, did, pid, r1, 2500)
    assert vdb.settle(did, conn=e1) == "evaluation"
    e1.commit()
    e1.close()
    t.join(30)
    assert "error" not in done and done["outcome"] == "evaluation", done
    with vdb.owner() as c:
        rows = c.execute("SELECT id, created_at, evidence_refs, reversed_after_paise "
                         "FROM public.donation_credit_events "
                         "WHERE donation_id = %s AND cause = 'evaluation' ORDER BY id", (did,)).fetchall()
        rec = dict(c.execute("SELECT provider_ref, recorded_at FROM public.payment_integrity_events "
                             "WHERE donation_id = %s", (did,)).fetchall())
    (id1, t1, refs1, amt1), (id2, t2, refs2, amt2) = rows
    assert (refs1, amt1, sorted(refs2), amt2) == ([r1], 2500, sorted([r1, r2]), 3500)
    assert t2 < t1 and rec[r2] < rec[r1] and id1 < id2  # stamps say r2 first; the commit order was E1 then E2
    return did, id1, (r1, r2)


def test_d5_a_a_legitimate_row_settled_before_an_earlier_stamped_commit_is_never_structural(vdb):
    did, id1, _ = _out_of_order_refunds(vdb)
    report = vdb.verify()
    assert not found(report, "I12-H", "structural"), ("E1 row", id1, [(f["entity_id"], f["detail"]) for f in
                                                       found(report, "I12-H")])
    (amb,) = found(report, "I12-H", "historical_ambiguous")  # S2: no prefix within +-M shows it: never exact
    assert amb["entity_id"] == str(id1) and amb["detail"]["reason"] == "evidence_order"
    assert amb["detail"]["basis"] == "evidence" and not report["gates"]["activation_verification_ready"]


@pytest.mark.xfail(strict=True, reason="accepted Decision 5 limitation: a refs-set's feasibility at a given time is "
                                       "unprovable on 0034 (kept as a regression test, never weakened)")
def test_d5_b_a_forged_row_matching_a_never_committed_prefix_state_is_never_exact(vdb):
    """The prefix at r2's stamp ({r2} alone) never existed: E1 committed {r1} first. An owner-inserted row naming
    {r2} with 1000 matches it within +-M. The data cannot exclude that E2 committed first, so no sound I12-H rule
    flags it; this fixture is caught elsewhere (I4 reversal_state), which is not a general guarantee."""
    did, _, (r1, r2) = _out_of_order_refunds(vdb)
    with vdb.owner() as c:
        forged = c.execute(
            "INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause, "
            "payment_status, moderation_status, credited_before, credited_after, delta_inr, delta_count, total_after, "
            "actor, reversed_before_paise, reversed_after_paise, evidence_refs) SELECT id, streamer_id, "
            "lower(donor_name), 'evaluation', payment_status, moderation_status, credited_inr, credited_inr, 0, 0, "
            "NULL, 'x', 3500, 1000, %s FROM public.donations WHERE id = %s RETURNING id", ([r2], did)).fetchone()[0]
    report = vdb.verify()
    on_row = [f for f in report["findings"] if f["invariant"] == "I12-H" and f["entity_id"] == str(forged)]
    assert on_row, ("forged row", forged, [(f["invariant"], f["class"], f["entity_id"], f["detail"].get("problem"))
                                           for f in report["findings"]])


def _frozen_keep_race(vdb, extra_before_keep: bool):
    """Hold mode, a lost dispute held; a freeze F holds the state lock; E2 starts (now() fixed) and waits on F; F
    commits; [X is recorded]; the keep acknowledges what it sees; then E2 records r2 (stamped before X and the keep)
    and settles (inactive). The keep never saw r2."""
    vdb.go("active", "hold")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "dispute", "disp_" + secrets.token_hex(4), "lost", deducted=4000)
    assert vdb.settle(did) == "hold"
    f = vdb.conn("hyperchat_migrator")
    f.autocommit = False
    f.execute("SET ROLE hyperchat_owner")
    f.execute("SELECT public.payment_accounting_set_state('frozen', NULL, 'op-verify', 'a long freeze (D5)', NULL)")
    e2 = _app_tx(vdb)
    e2_pid = e2.execute("SELECT pg_backend_pid()").fetchone()[0]
    after_state, go, done = threading.Event(), threading.Event(), {}
    t = threading.Thread(target=_late_starter, args=(e2, did, pid, "rfnd_" + secrets.token_hex(4), 1000),
                         kwargs={"after_state": after_state, "go": go, "done": done, "settle": vdb.settle})
    t.start()
    _wait_for_lock(vdb, e2_pid)  # E2 waits on F's state lock; its transaction start is already fixed
    f.commit()
    f.close()
    assert after_state.wait(30)
    if extra_before_keep:
        vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 700)
    run = vdb.run("resolve-keep", [did])
    assert vdb.keep(did, run) == "kept"
    go.set()
    t.join(30)
    assert "error" not in done and done["outcome"] == "inactive", done
    with vdb.owner() as c:
        keep_at, e2_at = c.execute(
            "SELECT (SELECT created_at FROM public.donation_credit_events WHERE donation_id = %s "
            "AND cause = 'manual'), "
            "(SELECT recorded_at FROM public.payment_integrity_events WHERE donation_id = %s AND amount_paise = 1000)",
            (did, did)).fetchone()
    assert e2_at < keep_at  # r2 is stamped before the keep it was never visible to
    return did


def test_d5_a_ack_a_genuine_keep_on_a_non_prefix_state_is_never_forged(vdb):
    did = _frozen_keep_race(vdb, extra_before_keep=True)  # the keep saw {dispute, X}: no recorded_at prefix
    report = vdb.verify()
    assert not found(report, "I12", "audit_integrity", "forged_ack"), [(f["class"], f["detail"]) for f in
                                                                       report["findings"]]


def test_d5_b_ack_an_ack_rewritten_to_a_state_the_keep_never_saw_is_never_a_proven_exception(vdb):
    did = _frozen_keep_race(vdb, extra_before_keep=False)
    with vdb.owner() as c:  # the owner rewrites the ack to the current fingerprint, which includes r2
        c.execute("UPDATE public.donation_accounting_evaluations SET ack_fp = public.payment_evidence_fp(donation_id), "
                  "evidence_fp = public.payment_evidence_fp(donation_id) WHERE donation_id = %s", (did,))
    report = vdb.verify()
    mine = [f for f in report["findings"] if f["entity_id"] == did or f["detail"].get("donation") == did]
    assert any(f["detail"].get("reason") == "ack_mode_order" or f["detail"].get("problem") == "forged_ack"
               for f in mine), [(f["invariant"], f["class"], f["detail"]) for f in mine]


def test_fc3_a_report_on_the_accepted_test_b_limitation_discloses_it(vdb):
    """FC-3: the Test B forged row passes I12-H (the accepted Decision 5 limitation, pinned strict-xfail above); the
    generated report must disclose exactly that, and never claim a false historical pass is impossible."""
    did, _, (r1, r2) = _out_of_order_refunds(vdb)
    with vdb.owner() as c:
        forged = c.execute(
            "INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause, "
            "payment_status, moderation_status, credited_before, credited_after, delta_inr, delta_count, total_after, "
            "actor, reversed_before_paise, reversed_after_paise, evidence_refs) SELECT id, streamer_id, "
            "lower(donor_name), 'evaluation', payment_status, moderation_status, credited_inr, credited_inr, 0, 0, "
            "NULL, 'x', 3500, 1000, %s FROM public.donations WHERE id = %s RETURNING id", ([r2], did)).fetchone()[0]
    report = vdb.verify()
    assert not [f for f in report["findings"] if f["invariant"] == "I12-H" and f["entity_id"] == str(forged)]
    assert report["limitations"] == list(verify.LIMITATIONS)
    text = " ".join(report["limitations"])
    assert "a false historical pass is possible" in text and "remain undetected by I12-H" in text
    assert "never a pass" not in text and not report["gates"]["activation_verification_ready"]
