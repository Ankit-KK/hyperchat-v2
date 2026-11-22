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
        kw.setdefault("max_seconds", MAX_SECONDS)  # revision 7 §2.7.1: required, no production default
        with self.conn(role) as c:
            return verify.verify(c, **kw)

    def checksum(self) -> dict:
        with self.owner() as c:
            return {t: c.execute(f"SELECT md5(COALESCE(string_agg(x::text, '|' ORDER BY x::text), '')) "
                                 f"FROM public.{t} x").fetchone()[0] for t in FIN_TABLES}


@pytest.fixture
def vdb(verify_db) -> DB:
    return DB(verify_db)


MAX_SECONDS = 600  # test budget only (owner item 8: the production budget is pending)


def found(report, invariant=None, cls=None, problem=None) -> list[dict]:
    return [f for f in report["findings"] if (invariant is None or f["invariant"] == invariant)
            and (cls is None or f["class"] == cls) and (problem is None or f["detail"].get("problem") == problem)]


def classes_of(report) -> set[str]:
    return {f["class"] for f in report["findings"]}


def pre_a5c(report):
    """§6A rule 6 / amendment B v1: until A5c, I10 fails closed with EXACTLY ONE I10 finding, problem
    baseline_unverified, naming the remaining unverified components (the C0-b condition of test_rev7_c0_db). Assert
    exactly that, and return the report without that one finding (counts adjusted) so a test's content assertions run
    unchanged before A5c; its status / exit_code / gate assertions run under after_activation. After A5c (flag set,
    nothing skipped) the report is returned unchanged."""
    if _v("I10_BASELINE_CONFIRMED", False) and not _v("SKIPPED_COMPARISONS", ()):
        return report
    i10 = [f for f in report["findings"] if f["invariant"] == "I10"]
    assert len(i10) == 1 and i10[0]["detail"].get("problem") == "baseline_unverified", ("pre-A5c", report["findings"])
    expected = [*([] if _v("I10_BASELINE_CONFIRMED", False) else _v("I10_UNVERIFIED", ())),
                *_v("SKIPPED_COMPARISONS", ())]
    assert i10[0]["detail"]["components"] == expected, ("pre-A5c components", i10[0]["detail"])
    counts = dict(report["counts"])
    counts[i10[0]["class"]] -= 1
    counts = {k: v for k, v in counts.items() if v}
    return {**report, "findings": [f for f in report["findings"] if f is not i10[0]], "counts": counts}


# ── statuses, gates and the read-only guarantee ───────────────────────────────────────────────────────────────────

def test_a_fresh_0034_database_is_clean_and_not_activation_complete(vdb):
    report = vdb.verify()
    assert pre_a5c(report)["findings"] == []
    assert report["database"]["head"] == "0034" and report["accounting"] == {"state": "off", "dispute_mode": "hold"}

    def strict():
        assert report["status"] == "clean" and report["exit_code"] == 0 and report["findings"] == []
        assert report["gates"] == dict.fromkeys(verify.GATE_KEYS, False)  # off: nothing is activation-ready
    after_activation(strict, why="clean, exit 0 and every gate false")


def test_m16_cr1_a_clean_active_full_report_is_verification_ready_and_never_activation_complete(vdb):
    vdb.go("active", "apply")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)
    assert vdb.settle(did) == "evaluation"
    other, _ = vdb.donation(sid, name="Other")  # no evidence: not accounting-relevant
    assert other
    report = vdb.verify()
    assert pre_a5c(report)["counts"] == {"processed": 1}
    assert report["not_attested"] == list(verify.NOT_ATTESTED)
    assert vdb.state() == ("active", "apply")  # the report changed and authorized nothing

    def strict():
        assert report["status"] == "clean" and report["exit_code"] == 0, report["findings"]
        assert report["counts"] == {"processed": 1}
        # CR-1: no 6a-3 second reconcile result exists, so activation is never certified complete by verify
        assert report["gates"] == {"reconcile_precondition": True, "recovery_precondition": True,
                                   "activation_verification_ready": True, "activation_complete": False}
    after_activation(strict, why="clean, exit 0 and the three readiness gates true")


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
        report = verify.verify(c, max_seconds=MAX_SECONDS)
    assert report["status"] != "refused"
    assert vdb.checksum() == before
    # R15 (revision 7 §2.7.2, MOD): every statement is an approved module constant, a DECLARE wrapping one, or a
    # fixed protocol statement with validated integers and generated cursor names; anything else fails the test
    assert all(verify.statement_allowed(s) for s in statements), ("MOD:R15", [s for s in statements
                                                                              if not verify.statement_allowed(s)])
    assert statements[:3] == ["BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY", "SET LOCAL lock_timeout = '5s'",
                              "SET LOCAL row_security = off"] and statements[-1] == "ROLLBACK"
    assert not verify.statement_allowed("SELECT 1")  # an injected non-approved SELECT would fail the line above


@pytest.mark.parametrize("role", ["hyperchat_app", "hyperchat_worker", "hyperchat_migrator"])
def test_verify_refuses_any_role_but_readonly(vdb, role):
    report = vdb.verify(role=role)
    assert report["status"] == "refused" and report["exit_code"] == 2 and not any(report["gates"].values())
    assert report["findings"] == []


def test_verify_refuses_a_connection_with_an_open_transaction_or_a_bad_scope(vdb):
    with vdb.conn("hyperchat_readonly") as c:
        c.autocommit = False
        assert verify.verify(c, max_seconds=MAX_SECONDS)["status"] == "refused"
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
    pre_a5c(full)
    scoped = vdb.verify(creator=a)
    pre_a5c(scoped)
    assert scoped["scope"] == {"kind": "creator", "id": a, "complete": False}
    single = vdb.verify(donation=db_)
    pre_a5c(single)

    def strict():
        assert full["status"] == "attention"
        assert scoped["status"] == "clean" and not any(scoped["gates"].values())
        assert single["status"] == "attention"
    after_activation(strict, why="the full / scoped / single statuses and the scoped gates")


def test_the_operator_cli_prints_the_canonical_report_and_exits_with_its_code(vdb, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HYPERCHAT_READONLY_DSN", f"host={PG_HOST} port={vdb.port} dbname={vdb.name} "
                                                 "user=hyperchat_readonly")
    out = tmp_path / "report.json"
    code = operator.main(["verify", "--max-seconds", str(MAX_SECONDS), "--output", str(out)])
    printed = capsys.readouterr().out.strip()
    report = json.loads(printed)
    if not operator._POSIX_SECURE or not os.path.isdir("/proc/self/fd"):  # MOD: refused where it cannot be secured
        assert code == 2 and report["reason"] == "output_not_published: unsupported_platform" and not out.exists()
        return
    pre_a5c(report)
    assert printed == out.read_text().strip() == verify.canonical(report)
    body = {k: v for k, v in report.items() if k != "digest"}
    assert report["digest"] == __import__("hashlib").sha256(verify.canonical(body).encode()).hexdigest()
    monkeypatch.setenv("HYPERCHAT_READONLY_DSN", f"host={PG_HOST} port={vdb.port} dbname={vdb.name} "
                                                 "user=hyperchat_app")
    assert operator.main(["verify", "--max-seconds", str(MAX_SECONDS)]) == 2
    monkeypatch.delenv("HYPERCHAT_READONLY_DSN")
    assert operator.main(["verify", "--max-seconds", str(MAX_SECONDS)]) == 2
    for command in ("state", "reconcile", "resolve", "revert"):
        assert operator.main([command]) == 2

    def strict():
        assert code == 0  # Phase 2B: clean only once the pristine baseline is pinned (else baseline_unverified, 1)
    after_activation(strict, why="exit code 0 for a clean report")


# ── classification on real flows ──────────────────────────────────────────────────────────────────────────────────

def test_evidence_while_off_is_awaiting_exit_3(vdb):
    sid = vdb.creator()  # state off, no transition ever: nothing near a boundary
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)
    assert vdb.settle(did) == "inactive"
    report = vdb.verify()
    core = pre_a5c(report)
    assert core["counts"] == {"awaiting_reconciliation": 1} and core["findings"][0]["entity_id"] == did

    def strict():
        assert report["exit_code"] == 3 and not any(report["gates"].values())
    after_activation(strict, why="exit 3 and every gate false")


def test_evidence_just_after_a_freeze_is_boundary_uncertain_never_clean(vdb):
    vdb.go("frozen", "apply")  # seconds ago: inside the D-15 margin of the freeze
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)
    report = vdb.verify()
    assert pre_a5c(report)["counts"] == {"boundary_uncertain": 1}

    def strict():
        assert report["exit_code"] == 3
    after_activation(strict, why="exit 3")


def test_reconcile_after_reactivation_reconciles_and_mode_change_is_o_mode(vdb):
    vdb.go("active", "apply")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    vdb.evidence(did, pid, "refund", "rfnd_" + secrets.token_hex(4), "processed", 2500)
    vdb.settle(did)
    vdb.go("frozen", "hold")  # F-9: the mode is in the fingerprint
    report = vdb.verify()
    (f,) = pre_a5c(report)["findings"]
    # the mode change is the origin; the freeze seconds before it puts it inside the D-15 margin
    assert f["class"] in ("awaiting_reconciliation", "boundary_uncertain") and f["detail"]["origin_kind"] == "o_mode"
    vdb.go("active")
    run = vdb.run("reconcile", [did])
    vdb.reconcile(did, run)
    after = vdb.verify()
    assert pre_a5c(after)["counts"] == {"processed": 1}, after["findings"]

    def strict():
        assert report["exit_code"] == 3
        assert after["counts"] == {"processed": 1}, after["findings"]
    after_activation(strict, why="exit 3, then the processed-only counts")


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
    assert pre_a5c(report)["counts"] == {"processed": 1}, report["findings"]

    def strict():
        assert report["counts"] == {"processed": 1}, report["findings"]
    after_activation(strict, why="the processed-only counts")


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
    fcore = pre_a5c(frozen)
    assert fcore["counts"] == {"processed": 1} and fcore["findings"][0]["detail"]["exception"] == "revert"
    vdb.go("active")
    active = vdb.verify()
    (f,) = pre_a5c(active)["findings"]
    assert f["class"] in ("pending_reconciliation", "boundary_uncertain")

    def strict():
        assert frozen["counts"] == {"processed": 1} and active["exit_code"] == 3
    after_activation(strict, why="the processed-only counts and exit 3")


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
    assert pre_a5c(report)["counts"] == {"processed": 1}, report["findings"]

    def strict():
        assert report["counts"] == {"processed": 1}, report["findings"]
    after_activation(strict, why="the processed-only counts")


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
    core = pre_a5c(report)
    assert not classes_of(core) & {"missed_evaluation", "overdue_reconciliation"}
    assert classes_of(core) <= {"in_flight", "boundary_uncertain"}

    def strict():
        assert report["exit_code"] == 3 and classes_of(report) <= {"in_flight", "boundary_uncertain"}
    after_activation(strict, why="exit 3")


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
    pre_a5c(report)

    def strict():
        assert report["status"] == "clean", report["findings"]
    after_activation(strict, why="clean")


def test_erratum_case_b_whole_creator_cleanup_is_excluded_and_counted(vdb):
    sid, d1, d2 = _erratum_start(vdb)
    _delete_donations(vdb, d1, d2, guard_off=True)
    with vdb.owner() as c:
        c.execute("DELETE FROM public.streamers WHERE id = %s", (sid,))  # the totals cascade
    report = vdb.verify()
    core = pre_a5c(report)
    assert _no_violation(core), report["findings"]
    assert report["orphans"] == {"orphan_buckets": 1, "orphan_rows": 3}  # Erratum 1: r1, r2, r3
    (bucket,) = report["excluded_buckets"]
    assert bucket["accounting_rows"] == 0 and Decimal(bucket["net_credit"]) == Decimal("50.00")
    assert classes_of(core) == {"excluded_bucket_net_credit"}

    def strict():
        assert _no_violation(report) and classes_of(report) == {"excluded_bucket_net_credit"}
        assert report["exit_code"] == 3
    after_activation(strict, why="no violation and exit 3")


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
    assert "deleted_creator_with_accounting_history" in classes_of(pre_a5c(report))

    def strict():
        assert report["exit_code"] == 3 and not report["gates"]["activation_complete"]
    after_activation(strict, why="exit 3 and activation_complete false")


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
    ("ALTER TABLE public.payment_accounting_runs DISABLE TRIGGER payment_accounting_runs_guard",
     "trigger_binding:enabled"),
    ("GRANT INSERT ON public.donation_reversals TO hyperchat_app", "table_insert_granted:hyperchat_app"),
    ("ALTER FUNCTION public.payment_accounting_set_state(text, text, text, text, text) RESET lock_timeout",
     "lock_timeout"),
    ("GRANT EXECUTE ON FUNCTION public.payment_accounting_keep(uuid, bigint) TO hyperchat_worker",
     "function_acl:payment_accounting_keep("),
    ("GRANT EXECUTE ON FUNCTION public.donation_accounting_core(uuid, bigint, text, text[], text[], text, bigint) "
     "TO PUBLIC", "public_execute"),
    ("ALTER FUNCTION public.payment_evidence_fp(uuid) SECURITY INVOKER", "security"),
])
def test_i10_catalog_sabotage_is_structural(vdb, sabotage, problem):
    # MOD (rev. 7 §5.7 codes; signature texts matched by prefix until Phase 2B pins them). The clean precondition
    # passes only once Phase 2B has pinned the pristine baseline (until then I10 reports baseline_unverified).
    before = vdb.verify()
    assert pre_a5c(before)["findings"] == []
    with vdb.owner() as c:
        c.execute(sabotage)
    report = vdb.verify()
    hits = [f for f in found(report, "I10", "structural") if f["detail"]["problem"].startswith(problem)]
    assert hits, report["findings"]

    def strict():
        assert before["status"] == "clean"
    after_activation(strict, why="the clean precondition")


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
    assert not found(report, "I15") and pre_a5c(report)["counts"] == {"processed": 1}, report["findings"]

    def strict():
        assert report["counts"] == {"processed": 1}, report["findings"]
    after_activation(strict, why="the processed-only counts")


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
    assert not found(report, "D-10") and pre_a5c(report)["counts"] == {"historical_ambiguous": 1}, report["findings"]

    def strict():
        assert report["counts"] == {"historical_ambiguous": 1}, report["findings"]  # A1
    after_activation(strict, why="the historical_ambiguous-only counts")


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
        during = verify.verify(c, max_seconds=MAX_SECONDS)
    assert len(seen) > 6 and pre_a5c(during)["counts"] == {"processed": 1}, during["findings"]  # new evidence: unseen
    after = vdb.verify()
    assert "processed" not in pre_a5c(after)["counts"]  # the next snapshot sees it

    def strict():
        assert during["counts"] == {"processed": 1} and after["exit_code"] == 3
    after_activation(strict, why="the processed-only counts and exit 3")


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
    violations = [f for f in pre_a5c(report)["findings"] if verify.SEVERITY[f["class"]] == "violation"]
    assert not violations, violations
    assert not found(report, "I12-H")
    (timing,) = found(report, "I13")  # the proven mode change, stamped before the evaluation: an uncertain origin
    assert timing["class"] == "boundary_uncertain" and timing["detail"]["origin_kind"] == "o_mode_uncertain"

    def strict():
        assert report["exit_code"] == 3
    after_activation(strict, why="exit 3")


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
    assert not [f for f in pre_a5c(report)["findings"] if verify.SEVERITY[f["class"]] == "violation"], \
        report["findings"]
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

    def strict():
        assert not [f for f in report["findings"] if verify.SEVERITY[f["class"]] == "violation"], report["findings"]
    after_activation(strict, why="no violation before the forged row")


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
    assert not [f for f in pre_a5c(report)["findings"] if verify.SEVERITY[f["class"]] == "violation"], \
        report["findings"]
    # Decision 3: the ack names hold, but the state also had apply and the keep's commit is unbounded: possibly
    # valid, never proven, so no keep exception and a permanent readiness block (r4: the exception was granted)
    (amb,) = [f for f in found(report, "I12", "historical_ambiguous") if f["detail"].get("reason") == "ack_mode_order"]
    assert not found(report, "I13", "processed")
    with vdb.owner() as c:  # the same timeline with a tampered acknowledgement: still forged
        c.execute("UPDATE public.donation_accounting_evaluations SET ack_fp = md5('tampered') WHERE donation_id = %s",
                  (did,))
    assert found(vdb.verify(), "I12", "audit_integrity", "forged_ack")

    def strict():
        assert not [f for f in report["findings"] if verify.SEVERITY[f["class"]] == "violation"], report["findings"]
        assert report["exit_code"] == 3 and not report["gates"]["activation_verification_ready"]
    after_activation(strict, why="no violation, exit 3 and activation_verification_ready false")


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

@pytest.mark.timeout(int(verify.MARGIN.total_seconds()) + 180)  # sleeps M + 20 s by design (D-15: M = 5 min)
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


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# Phase 2B, stage A1 (accepted validation plan df1da1e1): written offline, NEVER RUN in A1. Every Rev 7 §7 row whose
# "Run on" contains DB has a test below, named test_rev7_<id>_db; plan-defined checks are test_2b_*; the I10
# activation rows (§6A) are test_i10_act_*. Assertion messages carry the row's exact Rev 7 type as a tag
# ("FF:<id>", "CC:<id>", "NC:<id>", "MOD:<id>"); a mixed row carries two separately tagged assertions. The 2B-7
# fail-first harness reads these tags: only an "FF:" assertion is subject to S9 (plan §5.10).
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════

from contextlib import contextmanager  # noqa: E402

from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

from tests.payments.verify_helpers import (  # noqa: E402
    APPROVED_PORTS, EXCLUSIVE_PORTS, PRE_EXISTING_PORTS, LOOPBACK, EXPECT_KEYS, NEVER_PRESENT_DATABASES, ROLES_SQL,
    CI_DB_EXECUTION_AUTHORIZED, CI_DISABLED, CI_MARKER_PREFIX, CI_PORT, CI_CAPTURE_SOURCE, CI_CAPTURE_KEYS, _norm_dir,
    _ci_mode, _common_problems, _hex_nonce, _ci_problems, cluster_identity_problems, exclusive_decision,
    QUARANTINE_ENV, quarantine_marker, quarantine_refusal, restore_and_verify,
)


def _identity(admin: str) -> dict:
    """What the server says about itself (read-only catalog queries), for both modes."""
    with psycopg.connect(admin, autocommit=True) as c:
        row = c.execute("SELECT (SELECT system_identifier::text FROM pg_catalog.pg_control_system()), "
                        "inet_server_port()::text, current_setting('data_directory'), "
                        "current_setting('server_version_num')").fetchone()
        ident = dict(zip(EXPECT_KEYS, row))
        ident["databases"] = [r[0] for r in c.execute("SELECT datname FROM pg_catalog.pg_database").fetchall()]
        marker = c.execute("SELECT pg_catalog.shobj_description(oid, 'pg_database') FROM pg_catalog.pg_database "
                           "WHERE datname = 'hyperchat_ci'").fetchone()
        ident["ci_marker"] = marker[0] if marker else None
        ident["admin_user"], ident["admin_super"] = c.execute(
            "SELECT current_user::text, rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user").fetchone()
        ident["login_roles"] = [r[0] for r in c.execute(
            "SELECT rolname::text FROM pg_catalog.pg_roles WHERE rolcanlogin").fetchall()]
        ro = c.execute("SELECT r.rolsuper, r.rolbypassrls, EXISTS (SELECT 1 FROM pg_catalog.pg_db_role_setting s "
                       "WHERE s.setrole = r.oid AND s.setdatabase = 0 AND "
                       "'default_transaction_read_only=on' = ANY (s.setconfig)) FROM pg_catalog.pg_roles r "
                       "WHERE r.rolname = 'hyperchat_readonly'").fetchone()
        ident["readonly"] = dict(zip(("super", "bypassrls", "read_only_default"), ro)) if ro else {}
    return ident


@pytest.fixture(scope="module", autouse=True)
def phase2b_cluster_guard():
    """Module-scoped, autouse, set up before any per-test database is created: refuses the whole module unless the
    cluster is an approved Phase 2B cluster, or a CI server that proves the CI identity contract. It connects only
    when a test runs, never at collection."""
    admin = os.environ.get("HYPERCHAT_TEST_VERIFY_ADMIN", "")
    if not admin:
        pytest.skip("HYPERCHAT_TEST_VERIFY_ADMIN not set")
    parsed = conninfo_to_dict(admin)
    if parsed.get("host", "") not in LOOPBACK or str(parsed.get("port", "")) in PRE_EXISTING_PORTS:
        pytest.fail("refused before connecting: " + "; ".join(_common_problems(
            {"port": parsed.get("port")}, os.environ, parsed)), pytrace=False)
    ident = _identity(admin)
    problems = cluster_identity_problems(ident, os.environ, parsed, _ci_capture(os.environ))
    if problems:
        pytest.fail("not an approved test cluster: " + "; ".join(problems), pytrace=False)
    return ident


def _ci_capture(env):
    """The provisioning step's identity file (CI mode only); None when absent or unreadable (then refused)."""
    if not _ci_mode(env) or not env.get("HYPERCHAT_TEST_CI_IDENTITY_FILE"):
        return None
    try:
        with open(env["HYPERCHAT_TEST_CI_IDENTITY_FILE"], encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


exclusive_cluster = pytest.mark.exclusive_cluster  # registered nowhere on purpose (no conftest edit): select by -m


@pytest.fixture
def xcluster(phase2b_cluster_guard):
    decision = exclusive_decision(phase2b_cluster_guard, os.environ)
    if decision == "skip":
        pytest.skip("DB-X: runs only with HYPERCHAT_TEST_EXCLUSIVE_CLUSTER=1 on X15 / X17 (A7a / A7b)")
    if decision == "fail":
        pytest.fail("DB-X refused: HYPERCHAT_TEST_EXCLUSIVE_CLUSTER=1 but this is not an approved exclusive cluster",
                    pytrace=False)
    refusal = quarantine_refusal(phase2b_cluster_guard, os.environ)
    if refusal:
        pytest.fail(refusal, pytrace=False)
    return phase2b_cluster_guard


# ── fceadd95 compatibility (A8 runs these tests against the original verify.py in a disposable copy) ──
import inspect  # noqa: E402


def _v(name, default):
    """A verify module attribute that fceadd95 may not have (I10_BASELINE_CONFIRMED, SKIPPED_COMPARISONS, ...)."""
    return getattr(verify, name, default)


def _vcall(conn, **kw):
    """verify.verify with only the keywords this implementation accepts (fceadd95: no max_seconds, no clock)."""
    accepted = inspect.signature(verify.verify).parameters
    return verify.verify(conn, **{k: v for k, v in kw.items() if k in accepted})


def vrun(vdb, role="hyperchat_readonly", **kw):
    """DB.verify for the A1 tests, through _vcall (the pre-existing DB.verify is left unchanged)."""
    kw.setdefault("max_seconds", MAX_SECONDS)
    with vdb.conn(role) as c:
        return _vcall(c, **kw)


# ── shared helpers ──────────────────────────────────────────────────────────────────────────────────────────────────

def tag(kind: str, rid: str, report=None) -> str:
    """The assertion tag the 2B-7 harness reads (plan §5.10), plus a bounded context."""
    ctx = "" if report is None else " " + repr([(f["invariant"], f["class"], f["entity_id"], f["detail"])
                                                for f in report.get("findings", [])])[:3000]
    return f"{kind}:{rid}{ctx}"


def blockers(report) -> set[str]:
    return {b["blocker"] for b in report.get("write_blockers", [])}


def i10_problems_of(report) -> list[str]:
    return [f["detail"].get("problem", "") for f in found(report, "I10")]


def has_i10(report, prefix: str) -> bool:
    return any(p.startswith(prefix) for p in i10_problems_of(report))


def by_reason(report, invariant, reason, **match) -> list[dict]:
    return [f for f in found(report, invariant) if f["detail"].get("reason") == reason
            and all(f["detail"].get(k) == v for k, v in match.items())]


WRITE_GATES = ("reconcile_precondition", "recovery_precondition")


def after_activation(check, *, why: str):
    """Gate and strict-clean assertions are hollow while I10 fails closed (every gate is false then): they run only
    once A5c has set I10_BASELINE_CONFIRMED. The finding assertions before this call always run."""
    if not _v("I10_BASELINE_CONFIRMED", False):
        pytest.skip(f"finding assertions passed; {why} needs the activated I10 baseline (A5c)")
    check()


def _owner_sql(vdb, *statements, params=None):
    with vdb.owner() as c, c.transaction():
        for s in statements:
            c.execute(s, params)


def _admin_db(vdb) -> str:
    return make_conninfo(os.environ["HYPERCHAT_TEST_VERIFY_ADMIN"], dbname=vdb.name)


def _super_sql(vdb, *statements, database=True):
    """As the cluster superuser (catalog changes an owner cannot make), in the throwaway database by default."""
    dsn = _admin_db(vdb) if database else os.environ["HYPERCHAT_TEST_VERIFY_ADMIN"]
    with psycopg.connect(dsn, autocommit=True) as c:
        for s in statements:
            c.execute(s)


@contextmanager
def authtest_role(vdb, *, login=True, inherit=True):
    """DB+R (Rev 7 §5.8, OD-3): a unique throwaway role, dropped in finally (DROP OWNED in the test database first,
    then DROP ROLE); RT-6 checks nothing is left behind."""
    name = f"authtest_{secrets.token_hex(5)}"
    admin = os.environ["HYPERCHAT_TEST_VERIFY_ADMIN"]
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'CREATE ROLE "{name}" {"LOGIN" if login else "NOLOGIN"} {"INHERIT" if inherit else "NOINHERIT"}')
    try:
        yield name
    finally:
        with psycopg.connect(_admin_db(vdb), autocommit=True) as c:
            c.execute(f'DROP OWNED BY "{name}"')
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f'DROP ROLE IF EXISTS "{name}"')


def _ev(vdb, did, pid, kind, ref, status, amount=10000, *, deducted=None, currency="INR", at=0, snapshot=None,
        match=None, phase="chargeback", account="primary", order=None) -> str:
    """One webhook event plus its integrity evidence row (as the app), returning the evidence id. at=None: undated
    (provider_event_at NULL); did=None: unattributed (match 'none'); snapshot: payment_amount_refunded_paise."""
    event_type = {"processed": "refund.processed", "pending": "refund.created", "failed": "refund.failed",
                  "lost": "payment.dispute.lost", "won": "payment.dispute.won",
                  "open": "payment.dispute.created", "under_review": "payment.dispute.under_review"}[status]
    match = match or ("payment" if did else "none")
    with vdb.conn() as c:
        wid = c.execute(
            "INSERT INTO public.payment_webhook_events (event_id, event_type, account, provider_payment_id, "
            "donation_id, outcome, received_at) VALUES (%s, %s, 'primary', %s, %s, 'integrity_recorded', now()) "
            "RETURNING id", ("authtest-vrf-" + secrets.token_hex(6), event_type, pid, did)).fetchone()[0]
        return c.execute(
            "INSERT INTO public.payment_integrity_events (webhook_event_id, donation_id, match_type, account, kind, "
            "event_type, provider_ref, provider_payment_id, provider_order_id, phase, status, amount_paise, currency, "
            "amount_deducted_paise, payment_amount_refunded_paise, provider_event_at, consistent, received_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "CASE WHEN %s::int IS NULL THEN NULL ELSE to_timestamp(1800000000 + %s::int) END, true, now()) "
            "RETURNING id::text",
            (wid, did, match, account, kind, event_type, ref, pid, order, phase if kind == "dispute" else None,
             status, amount, currency, deducted, snapshot, at, at)).fetchone()[0]


def _ref(kind="refund") -> str:
    return ("rfnd_" if kind == "refund" else "disp_") + secrets.token_hex(5)


def _capture(vdb, did, pid, moderation="approved"):
    with vdb.conn() as c:
        c.execute("UPDATE public.donations SET payment_status = 'success', provider_payment_id = %s, "
                  "moderation_status = %s WHERE id = %s", (pid, moderation, did))


def _drop_account_check(vdb):
    """Synthetic corruption (Rev 7 A3 / S18): 0029 pins account IN ('primary'); dropped in this database only."""
    with vdb.owner() as c:
        names = [r[0] for r in c.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'public.payment_integrity_events'::regclass "
            "AND pg_get_constraintdef(oid) LIKE '%account%primary%'").fetchall()]
        for n in names:
            c.execute(f'ALTER TABLE public.payment_integrity_events DROP CONSTRAINT "{n}"')
    assert names, "the 0029 account CHECK was not found"


def _history_row(vdb, did, cause, before, after, refs, *, hold_before=None, hold_after=None, run=None):
    """An owner-written history row (synthetic, constraint-permitted) for the donation's current credit."""
    with vdb.owner() as c:
        return c.execute(
            "INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause, "
            "payment_status, moderation_status, credited_before, credited_after, delta_inr, delta_count, total_after, "
            "actor, reversed_before_paise, reversed_after_paise, hold_before, hold_after, evidence_refs, run_id) "
            "SELECT id, streamer_id, lower(donor_name), %s, payment_status, moderation_status, credited_inr, "
            "credited_inr, 0, 0, NULL, 'x', %s, %s, %s, %s, %s, %s FROM public.donations WHERE id = %s "
            "RETURNING id", (cause, before, after, hold_before, hold_after, refs, run, did)).fetchone()[0]


def _held(vdb, *, extra_refunds=0):
    """Mode hold, a lost dispute (DM hold) plus `extra_refunds` attributed processed refunds (one event each)."""
    vdb.go("active", "hold")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "dispute", _ref("dispute"), "lost", deducted=4000, at=0)
    for i in range(extra_refunds):
        _ev(vdb, did, pid, "refund", _ref(), "processed", 100 + i, at=1 + i)
    assert vdb.settle(did) == "hold"
    return sid, did, pid


# ── I12-H precedence and bounds (Rev 7 §7) ──────────────────────────────────────────────────────────────────────────

def test_rev7_p2_db(vdb):
    """P2 (FF, DB): M12 with refs NULL; the reversal and writer following; a genuine settle -> structural."""
    sid, did, _ = _paid_reversed(vdb, refund=2500)
    _history_row(vdb, did, "evaluation", 2500, 5000, None)
    _owner_sql(vdb, "UPDATE public.donation_reversals SET applied_reversed_paise = 5000 WHERE donation_id = %(d)s",
               "UPDATE public.donations SET updated_at = now() WHERE id = %(d)s", params={"d": did})
    report = vrun(vdb)
    assert found(report, "I12-H", "structural"), tag("FF", "P2", report)
    after_activation(lambda: [None for g in WRITE_GATES if report["gates"][g]] == [] or pytest.fail(
        tag("FF", "P2", report)), why="write gates false")


def test_rev7_p7_db(vdb):
    """P7 (CC, U + DB): a genuine keep, a held activation, a revert and a '{}' evaluation at 0: no I12-H finding."""
    sid, kept, _ = _held(vdb)
    assert vdb.keep(kept, vdb.run("resolve-keep", [kept])) == "kept"
    vdb.set_state("frozen")
    held, hp = vdb.donation(sid, name="held")
    _ev(vdb, held, hp, "dispute", _ref("dispute"), "lost", deducted=4000)
    vdb.set_state("active")
    vdb.reconcile(held, vdb.run("reconcile", [held]))
    rev, rp = vdb.donation(sid, name="reverted")
    _ev(vdb, rev, rp, "refund", _ref(), "processed", 2500)
    vdb.settle(rev)
    vdb.set_state("frozen")
    assert vdb.revert(rev, vdb.run("revert", [rev])) == "reverted"
    vdb.set_state("active")
    zero, zp = vdb.donation(sid, name="zero")
    _ev(vdb, zero, zp, "refund", _ref(), "pending", 2500)  # nothing counted: an evaluation at 0 with refs '{}'
    vdb.settle(zero)
    report = vrun(vdb)
    assert not found(report, "I12-H"), tag("CC", "P7", report)


def test_rev7_p9d_db(vdb):
    """P9d (FF, U + DB): A1 keep reconstruction with one ref of 13 raw events (1 on A, 12 on B) -> V-A2."""
    vdb.go("active", "hold")
    sid = vdb.creator()
    a, pa = vdb.donation(sid, name="A")
    b, pb = vdb.donation(sid, name="B")
    _ev(vdb, a, pa, "dispute", _ref("dispute"), "lost", deducted=4000, at=0)
    ref = _ref()
    _ev(vdb, a, pa, "refund", ref, "processed", 1000, at=1)
    for i in range(12):
        _ev(vdb, b, pb, "refund", ref, "processed", 1000, at=2 + i)
    assert vdb.settle(a) == "hold"
    assert vdb.keep(a, vdb.run("resolve-keep", [a])) == "kept"
    report = vrun(vdb)
    hits = by_reason(report, "I12", "ack_mode_order", enumeration="over_bound", bound="ref_events")
    assert hits and hits[0]["detail"]["events"] == 13 and hits[0]["detail"]["donation"] == a, tag("FF", "P9d", report)
    assert "a1_over_bound" in blockers(report), tag("FF", "P9d", report)
    after_activation(lambda: [g for g in WRITE_GATES if report["gates"][g]] == [] or pytest.fail(
        tag("FF", "P9d", report)), why="reconcile and recovery false")


def test_rev7_p15_db(vdb):
    """P15 (CC, U + DB): a provably-0034 dispute-naming activation row, no apply period, a ref with 13 events
    (product <= 4 096): structural (survives the bound); every gate false."""
    vdb.go("active", "hold")  # never apply
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    disp = _ref("dispute")
    for i in range(12):
        _ev(vdb, did, pid, "dispute", disp, "under_review", at=i)
    _ev(vdb, did, pid, "dispute", disp, "lost", deducted=4000, at=12)
    assert vdb.settle(did) == "hold"
    run = vdb.run("reconcile", [did])
    row = _history_row(vdb, did, "activation", 0, 4000, [disp], hold_before=accounting.DISPUTE_MODE_HOLD, run=run)
    report = vrun(vdb)
    assert [f for f in found(report, "I12-H", "structural") if f["entity_id"] == str(row)], tag("CC", "P15", report)
    after_activation(lambda: not any(report["gates"].values()) or pytest.fail(tag("CC", "P15", report)),
                     why="every gate false")


# ── A1 and gates (report builder) ───────────────────────────────────────────────────────────────────────────────────

def test_rev7_g4_db(vdb):
    """G4 (CC, U + DB): processed-only full active -> clean; three gates true; activation_complete false. The
    existing test_m16_cr1_... asserts the same on PostgreSQL; this is the tagged form."""
    vdb.go("active", "apply")
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500)
    assert vdb.settle(did) == "evaluation"
    report = vrun(vdb)
    assert [f["class"] for f in report["findings"] if f["invariant"] != "I10"] == ["processed"], tag("CC", "G4", report)

    def gates():
        assert report["status"] == "clean" and report["gates"] == {
            "reconcile_precondition": True, "recovery_precondition": True, "activation_verification_ready": True,
            "activation_complete": False}, tag("CC", "G4", report)
    after_activation(gates, why="clean with three gates true")


def test_rev7_g8_db(vdb):
    """G8 (CC, U + DB): scoped; frozen; off; refused -> every gate false (the existing scoped test covers scoped)."""
    off = vrun(vdb)
    vdb.go("frozen", "apply")
    frozen = vrun(vdb)
    refused = vrun(vdb, role="hyperchat_app")
    sid = vdb.creator()
    scoped = vrun(vdb, creator=sid)
    assert refused["status"] == "refused", tag("CC", "G8", refused)

    def gates():
        for name, r in (("off", off), ("frozen", frozen), ("refused", refused), ("scoped", scoped)):
            assert not any(r["gates"].values()), tag("CC", "G8", r) + f" [case {name}]"
    after_activation(gates, why="all four all-false")  # pre-activation every gate is false for another reason


def test_rev7_g10_db(vdb):
    """G10 (FF, U (bound) + DB (keep path)): 1 attributed lost dispute + 12 refund refs (one event each), 1 mode:
    2^13 = 8 192 states -> ack_mode_order over_bound; blocker a1_over_bound; reconcile and recovery false."""
    sid, did, _ = _held(vdb, extra_refunds=12)
    assert vdb.keep(did, vdb.run("resolve-keep", [did])) == "kept"
    report = vrun(vdb)
    hits = by_reason(report, "I12", "ack_mode_order", enumeration="over_bound", bound="combinations")
    assert hits and hits[0]["detail"]["combinations"] > 4096, tag("FF", "G10", report)
    assert "a1_over_bound" in blockers(report), tag("FF", "G10", report)
    after_activation(lambda: [g for g in WRITE_GATES if report["gates"][g]] == [] or pytest.fail(
        tag("FF", "G10", report)), why="reconcile and recovery false")


def test_rev7_g10c_db(vdb):
    """G10c (CC, U + DB): the same with 11 refund refs (2^12 = 4 096, at the bound) -> complete; no blocker."""
    sid, did, _ = _held(vdb, extra_refunds=11)
    assert vdb.keep(did, vdb.run("resolve-keep", [did])) == "kept"
    report = vrun(vdb)
    assert by_reason(report, "I12", "ack_mode_order", enumeration="complete"), tag("CC", "G10c", report)
    assert "a1_over_bound" not in blockers(report), tag("CC", "G10c", report)


def test_rev7_g11_db(vdb):
    """G11 (FF on the additive finding; CC on the violation; U + DB): keep branch, an in-bound conceivable ack
    different from the current fp, a current evidence_fp, and a clear stored reversal disagreeing with the required
    DM hold: an additive ack_mode_order (complete) AND the unchanged evaluation_disagreement."""
    sid, did, pid = _held(vdb)
    assert vdb.keep(did, vdb.run("resolve-keep", [did])) == "kept"
    _ev(vdb, did, pid, "refund", _ref(), "processed", 1000, at=5)  # the current fp now differs from the ack
    _owner_sql(vdb, "UPDATE public.donation_accounting_evaluations SET evidence_fp = "
                    "public.payment_evidence_fp(donation_id) WHERE donation_id = %(d)s",
               "UPDATE public.donation_reversals SET hold = NULL WHERE donation_id = %(d)s", params={"d": did})
    report = vrun(vdb)
    assert by_reason(report, "I12", "ack_mode_order", enumeration="complete"), tag("FF", "G11", report)
    assert found(report, "I12", "structural", "evaluation_disagreement"), tag("CC", "G11", report)
    after_activation(lambda: [g for g in WRITE_GATES if report["gates"][g]] == [] or pytest.fail(
        tag("CC", "G11", report)), why="every write gate false")


# ── I12-H element, eligibility and chain; I18 ───────────────────────────────────────────────────────────────────────

def test_rev7_h1_db(vdb):
    """H1 (CC on severity; FF on the label; U + DB): a ref with 13 versions, all on B, named by A."""
    vdb.go("active", "apply")
    sid = vdb.creator()
    a, _ = vdb.donation(sid, name="A")
    b, pb = vdb.donation(sid, name="B")
    ref = _ref()
    for i in range(13):
        _ev(vdb, b, pb, "refund", ref, "processed", 1000, at=i)
    row = _history_row(vdb, a, "evaluation", 0, 1000, [ref])
    report = vrun(vdb)
    mine = [f for f in found(report, "I12-H") if f["entity_id"] == str(row)]
    assert mine and all(f["class"] == "structural" for f in mine), tag("CC", "H1", report)
    assert [f for f in mine if f["detail"].get("problem") == "ineligible_reference"], tag("FF", "H1", report)


def test_rev7_h5_db(vdb):
    """H5 (CC, U + DB): the legal (frozen, hold) -> (frozen, apply) mode-only transition: no I18 finding."""
    vdb.go("frozen", "hold")
    vdb.set_state(None, "apply")
    assert vdb.state() == ("frozen", "apply")
    report = vrun(vdb)
    assert not found(report, "I18"), tag("CC", "H5", report)


def test_rev7_e6_db(vdb):
    """E6 (CC, U + DB): untouched off / hold: no I18 finding."""
    assert vdb.state() == ("off", "hold")
    report = vrun(vdb)
    assert not found(report, "I18"), tag("CC", "E6", report)


# ── I16 ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_rev7_a1_db(vdb):
    """A1 (FF, DB): an unattributed processed refund before capture, then capture -> unattributed; blocker."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid, paid=False)
    _ev(vdb, None, pid, "refund", _ref(), "processed", 2500)
    _capture(vdb, did, pid)
    report = vrun(vdb)
    assert [f for f in by_reason(report, "I16", "unattributed") if f["entity_id"] == did], tag("FF", "A1", report)
    assert "i16:unattributed" in blockers(report), tag("FF", "A1", report)


def test_rev7_a2_db(vdb):
    """A2 (FF, DB): A's payment and account, attributed to B -> wrong_donation."""
    sid = vdb.creator()
    a, pa = vdb.donation(sid, name="A")
    b, _ = vdb.donation(sid, name="B")
    _ev(vdb, b, pa, "refund", _ref(), "processed", 2500)
    report = vrun(vdb)
    assert [f for f in by_reason(report, "I16", "wrong_donation") if f["entity_id"] == a], tag("FF", "A2", report)


def test_rev7_a3_db(vdb):
    """A3 (FF, DB / U; synthetic-corruption): A's payment id under another account -> account_mismatch."""
    sid = vdb.creator()
    a, pa = vdb.donation(sid)
    _drop_account_check(vdb)
    _ev(vdb, a, pa, "refund", _ref(), "processed", 2500, account="secondary")
    report = vrun(vdb)
    assert [f for f in by_reason(report, "I16", "account_mismatch") if f["entity_id"] == a], tag("FF", "A3", report)


def test_rev7_a4_db(vdb):
    """A4 (FF, DB): two versions of one ref with different payment ids -> ref_identity_conflict."""
    sid = vdb.creator()
    a, pa = vdb.donation(sid, name="A")
    b, pb = vdb.donation(sid, name="B")
    ref = _ref()
    _ev(vdb, a, pa, "refund", ref, "processed", 2500, at=1)
    _ev(vdb, b, pb, "refund", ref, "processed", 2500, at=2)
    report = vrun(vdb)
    assert {f["entity_id"] for f in by_reason(report, "I16", "ref_identity_conflict")} >= {a}, tag("FF", "A4", report)


def test_rev7_a5_db(vdb):
    """A5 (CC, DB): an unattributed pending refund and an open dispute -> no unattributed."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, None, pid, "refund", _ref(), "pending", 2500)
    _ev(vdb, None, pid, "dispute", _ref("dispute"), "open")
    report = vrun(vdb)
    assert not by_reason(report, "I16", "unattributed"), tag("CC", "A5", report)


def test_rev7_a6_db(vdb):
    """A6 (CC, DB): the order id matches A's order but the payment is another -> no ownership, no I16 for A."""
    sid = vdb.creator()
    a, pa = vdb.donation(sid)
    with vdb.owner() as c:
        order = c.execute("SELECT provider_order_id FROM public.donations WHERE id = %s", (a,)).fetchone()[0]
    other = "pay_TESTvrf" + secrets.token_hex(5)
    _ev(vdb, a, other, "refund", _ref(), "processed", 2500, match="order", order=order)
    report = vrun(vdb)
    assert not [f for f in found(report, "I16") if f["entity_id"] == a], tag("CC", "A6", report)


def test_rev7_a7_db(vdb):
    """A7 (NC, DB): A1 under a donation-A scope (present) and under a creator scope excluding A (absent)."""
    sid, other = vdb.creator(), vdb.creator()
    did, pid = vdb.donation(sid, paid=False)
    _ev(vdb, None, pid, "refund", _ref(), "processed", 2500)
    _capture(vdb, did, pid)
    inside = vrun(vdb, donation=did)
    outside = vrun(vdb, creator=other)
    assert by_reason(inside, "I16", "unattributed"), tag("NC", "A7", inside)
    assert not found(outside, "I16"), tag("NC", "A7", outside)


def test_rev7_a8_db(vdb):
    """A8 (CC, DB): a legacy / imported donation is never an owner."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid, provenance="imported")
    _ev(vdb, None, pid, "refund", _ref(), "processed", 2500)
    report = vrun(vdb)
    assert not [f for f in found(report, "I16") if f["entity_id"] == did], tag("CC", "A8", report)


# ── I17 ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _usd(vdb, sid):
    return vdb.donation(sid, 5, currency="USD", amount_inr=418)  # 500 cents


def test_rev7_s1_db(vdb):
    """S1 (FF, DB): S 5 000, P 2 500 -> missing_refund_evidence."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, snapshot=5000)
    assert by_reason(vrun(vdb), "I17", "missing_refund_evidence"), tag("FF", "S1")


def test_rev7_s2_db(vdb):
    """S2 (CC, DB): S = P (INR) -> none."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, snapshot=2500)
    report = vrun(vdb)
    assert not found(report, "I17"), tag("CC", "S2", report)


def test_rev7_s3_db(vdb):
    """S3 (FF, DB): P < S <= P + Q -> pending_refunds."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, at=1)
    _ev(vdb, did, pid, "refund", _ref(), "pending", 2500, at=2, snapshot=4000)
    report = vrun(vdb)
    assert by_reason(report, "I17", "pending_refunds"), tag("FF", "S3", report)


def test_rev7_s4_db(vdb):
    """S4 (FF, DB): S < P -> snapshot_behind."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, at=1, snapshot=2500)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, at=2)
    report = vrun(vdb)
    assert by_reason(report, "I17", "snapshot_behind"), tag("FF", "S4", report)


def test_rev7_s5_db(vdb):
    """S5 (CC, DB): USD: S 500 from a USD refund; P 500 USD -> none."""
    sid = vdb.creator()
    did, pid = _usd(vdb, sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 500, currency="USD", snapshot=500)
    report = vrun(vdb)
    assert not found(report, "I17"), tag("CC", "S5", report)


def test_rev7_s6_db(vdb):
    """S6 (FF, DB): USD donation, P 500 USD; the only snapshot (500) carried by an INR dispute ->
    snapshot_unit_conflict with members[0].unit_status = currency_mismatch."""
    sid = vdb.creator()
    did, pid = _usd(vdb, sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 500, currency="USD", at=1)
    _ev(vdb, did, pid, "dispute", _ref("dispute"), "open", 500, currency="INR", at=2, snapshot=500)
    hits = by_reason(vrun(vdb), "I17", "snapshot_unit_conflict")
    assert hits and hits[0]["detail"]["members"][0]["unit_status"] == "currency_mismatch", tag("FF", "S6")


def test_rev7_s7_db(vdb):
    """S7 (FF, DB): the only snapshot event has currency NULL -> snapshot_unit_conflict, currency_unknown."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, at=1)
    _ev(vdb, did, pid, "dispute", _ref("dispute"), "open", None, currency=None, at=2, snapshot=2500)
    hits = by_reason(vrun(vdb), "I17", "snapshot_unit_conflict")
    assert hits and hits[0]["detail"]["members"][0]["unit_status"] == "currency_unknown", tag("FF", "S7")


def test_rev7_s8_db(vdb):
    """S8 (FF, DB): a refund row whose currency differs from the payment's -> currency_mismatch subject refund;
    the snapshot comparison is conditional."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, currency="USD", at=1)
    _ev(vdb, did, pid, "dispute", _ref("dispute"), "open", 2500, at=2, snapshot=2500)
    report = vrun(vdb)
    assert by_reason(report, "I17", "currency_mismatch", subject="refund"), tag("FF", "S8", report)
    assert [f for f in found(report, "I17") if f["detail"].get("conditional") is True], tag("FF", "S8", report)


def test_rev7_s9_db(vdb):
    """S9 (FF, DB): a processed refund with amount_paise NULL -> amount_unknown subject refund."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", None)
    assert by_reason(vrun(vdb), "I17", "amount_unknown", subject="refund"), tag("FF", "S9")


def test_rev7_s10_db(vdb):
    """S10 (NC, DB): a snapshot from a dispute event, S != P -> missing_refund_evidence, source_ids = the dispute."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, at=1)
    disp = _ev(vdb, did, pid, "dispute", _ref("dispute"), "open", 5000, at=2, snapshot=5000)
    hits = by_reason(vrun(vdb), "I17", "missing_refund_evidence")
    assert hits and hits[0]["detail"]["source_ids"] == [disp], tag("NC", "S10")


def test_rev7_s11_db(vdb):
    """S11 (FF, DB): an older dated discrepancy plus a newer event without a snapshot -> the discrepancy persists
    and newer_unsnapshotted_events is reported."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, at=1, snapshot=5000)
    _ev(vdb, did, pid, "dispute", _ref("dispute"), "open", 1000, at=5)
    hits = by_reason(vrun(vdb), "I17", "missing_refund_evidence")
    assert hits and hits[0]["detail"].get("newer_unsnapshotted_events", 0) >= 1, tag("FF", "S11")


def test_rev7_s12_db(vdb):
    """S12 (FF, DB): tied provider times, comparable, different values -> snapshot_conflict."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, at=3, snapshot=2500)
    _ev(vdb, did, pid, "dispute", _ref("dispute"), "open", 5000, at=3, snapshot=5000)
    assert by_reason(vrun(vdb), "I17", "snapshot_conflict"), tag("FF", "S12")


def test_rev7_s14_db(vdb):
    """S14 (NC, DB): two snapshot events, one refund -> P counts the refund once."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, at=1, snapshot=5000)
    _ev(vdb, did, pid, "dispute", _ref("dispute"), "open", 5000, at=2, snapshot=5000)
    hits = by_reason(vrun(vdb), "I17", "missing_refund_evidence")
    assert hits and hits[0]["detail"]["processed_paise"] == 2500, tag("NC", "S14")


def test_rev7_s15_db(vdb):
    """S15 (NC, DB): one ref, two refund versions (amount changed) -> P = the current version's amount."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    ref = _ref()
    _ev(vdb, did, pid, "refund", ref, "processed", 2000, at=1)
    _ev(vdb, did, pid, "refund", ref, "processed", 2500, at=2, snapshot=5000)
    hits = by_reason(vrun(vdb), "I17", "missing_refund_evidence")
    assert hits and hits[0]["detail"]["processed_paise"] == 2500, tag("NC", "S15")


def test_rev7_s16_db(vdb):
    """S16 (FF on (a); NC on the transition): (a) dated S 5 000, P 2 500 -> missing_refund_evidence; (b) a valid
    processed refund raises P to 5 000 while S stays 5 000 -> no finding (genuine resolution)."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, at=1, snapshot=5000)
    before = vrun(vdb)
    assert by_reason(before, "I17", "missing_refund_evidence"), tag("FF", "S16", before)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, at=2)
    after = vrun(vdb)
    assert not found(after, "I17"), tag("NC", "S16", after)


def test_rev7_s16b_db(vdb):
    """S16b (FF, DB): dated S 5 000 then a later dated S 2 500; P 2 500 -> snapshot_regression (never cleared)."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "dispute", _ref("dispute"), "open", 5000, at=1, snapshot=5000)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, at=2, snapshot=2500)
    report = vrun(vdb)
    assert by_reason(report, "I17", "snapshot_regression"), tag("FF", "S16b", report)
    assert "i17:snapshot_regression" in blockers(report), tag("FF", "S16b", report)


def test_rev7_s17_db(vdb):
    """S17 (FF, DB): a currency outside the eight -> unsupported_currency (refund subject)."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, currency="XYZ")
    assert by_reason(vrun(vdb), "I17", "unsupported_currency", subject="refund"), tag("FF", "S17")


def test_rev7_s18_db(vdb):
    """S18 (NC, DB / U; synthetic-corruption): a snapshot from another account is not a candidate, and I16 reports
    account_mismatch."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, at=1, snapshot=2500)
    _drop_account_check(vdb)
    _ev(vdb, did, pid, "dispute", _ref("dispute"), "open", 9000, at=2, snapshot=9000, account="secondary")
    report = vrun(vdb)
    assert not found(report, "I17"), tag("NC", "S18", report)  # S = 2 500 = P: the other account never counts
    assert by_reason(report, "I16", "account_mismatch"), tag("NC", "S18", report)


def test_rev7_s19_db(vdb):
    """S19 (FF, DB): a mismatched-currency refund and no usable S -> the refund-side finding remains; never clean."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, currency="USD")
    report = vrun(vdb)
    assert by_reason(report, "I17", "currency_mismatch", subject="refund"), tag("FF", "S19", report)
    after_activation(lambda: report["status"] != "clean" or pytest.fail(tag("FF", "S19", report)),
                     why="'never clean'")


# ── I17 chronology ──────────────────────────────────────────────────────────────────────────────────────────────────

def _t1(vdb, reverse=False):
    sid = vdb.creator()
    did, pid = _usd(vdb, sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 500, currency="USD", at=1)
    rows = [("USD", _ref("dispute")), ("INR", _ref("dispute"))]
    for cur, ref in (reversed(rows) if reverse else rows):
        _ev(vdb, did, pid, "dispute", ref, "open", 500, currency=cur, at=5, snapshot=500)
    return did


def test_rev7_t1_db(vdb):
    """T1 (FF, DB): same provider time, 500 USD and 500 INR; USD donation, P 500 -> unit conflict, both ids."""
    _t1(vdb)
    hits = by_reason(vrun(vdb), "I17", "snapshot_unit_conflict")
    assert hits and len(hits[0]["detail"]["source_ids"]) == 2, tag("FF", "T1")


def test_rev7_t2_db(vdb):
    """T2 (FF, DB): T1 with the receipt / UUID order reversed -> the identical finding (shape, statuses, values)."""
    a, b = _t1(vdb), _t1(vdb, reverse=True)
    report = vrun(vdb)

    def shape(did):
        (f,) = [x for x in by_reason(report, "I17", "snapshot_unit_conflict") if x["entity_id"] == did]
        return sorted((m["unit_status"], m["currency"], m["value"]) for m in f["detail"]["members"])
    assert shape(a) == shape(b) == [("comparable", "USD", 500), ("currency_mismatch", "INR", 500)], tag("FF", "T2")


def test_rev7_t3_db(vdb):
    """T3 (FF, DB / U): dated 500 plus undated 800; P 500 -> snapshot_conflict."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 500, at=1, snapshot=500)
    _ev(vdb, did, pid, "dispute", _ref("dispute"), "open", 800, at=None, snapshot=800)
    assert by_reason(vrun(vdb), "I17", "snapshot_conflict"), tag("FF", "T3")


def test_rev7_t4_db(vdb):
    """T4 (NC, negative control; DB / U): earlier dated INR 900, later dated USD 500 = P -> no regression."""
    sid = vdb.creator()
    did, pid = _usd(vdb, sid)
    _ev(vdb, did, pid, "dispute", _ref("dispute"), "open", 900, currency="INR", at=1, snapshot=900)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 500, currency="USD", at=2, snapshot=500)
    report = vrun(vdb)
    assert not by_reason(report, "I17", "snapshot_regression"), tag("NC", "T4", report)


def test_rev7_t5_db(vdb):
    """T5 (FF, DB / U): earlier dated USD 900, later dated USD 500; P 500 -> snapshot_regression."""
    sid = vdb.creator()
    did, pid = _usd(vdb, sid)
    _ev(vdb, did, pid, "dispute", _ref("dispute"), "open", 900, currency="USD", at=1, snapshot=900)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 500, currency="USD", at=2, snapshot=500)
    assert by_reason(vrun(vdb), "I17", "snapshot_regression"), tag("FF", "T5")


def test_rev7_t7_db(vdb):
    """T7 (NC, negative control; DB): a failed refund with NULL currency / amount, plus S = P -> none."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, at=1, snapshot=2500)
    _ev(vdb, did, pid, "refund", _ref(), "failed", None, currency=None, at=2)
    report = vrun(vdb)
    assert not found(report, "I17"), tag("NC", "T7", report)


def test_rev7_t8_db(vdb):
    """T8 (FF, DB): a pending refund with amount NULL -> amount_unknown; conditional."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, at=1, snapshot=2500)
    _ev(vdb, did, pid, "refund", _ref(), "pending", None, at=2)
    hits = by_reason(vrun(vdb), "I17", "amount_unknown", subject="refund")
    assert hits and hits[0]["detail"].get("conditional_match") is True, tag("FF", "T8")


def test_rev7_t11_db(vdb):
    """T11 (NC, U + DB before / after): dated 500 plus undated 800 (before: a conflict), then a later dated 800;
    P 800 -> none (C = {dated 800, undated 800} agree; the dated 500 is superseded, 500 < 800)."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 800, at=1, snapshot=500)
    _ev(vdb, did, pid, "dispute", _ref("dispute"), "open", 800, at=None, snapshot=800)
    before = vrun(vdb)
    assert by_reason(before, "I17", "snapshot_conflict"), tag("NC", "T11/before", before)
    _ev(vdb, did, pid, "dispute", _ref("dispute"), "open", 800, at=5, snapshot=800)
    after = vrun(vdb)
    assert not found(after, "I17"), tag("NC", "T11/after", after)


def test_rev7_t13_db(vdb):
    """T13 (NC, = T6 DB form): a later dated observation equal to P after an earlier dated lower one -> none."""
    sid = vdb.creator()
    did, pid = _usd(vdb, sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 300, currency="USD", at=1, snapshot=300)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 200, currency="USD", at=2, snapshot=500)
    report = vrun(vdb)
    assert not found(report, "I17"), tag("NC", "T13", report)


def test_rev7_t15_db(vdb):
    """T15 (FF (CX6 §2), U + DB before / after): t1 5 000 -> t2 2 500 -> t3 5 000 INR; P 5 000 ->
    snapshot_regression (pair t1 -> t2); blocker; reconcile and recovery false; ready false."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    t1 = _ev(vdb, did, pid, "dispute", _ref("dispute"), "open", 5000, at=1, snapshot=5000)
    t2 = _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, at=2, snapshot=2500)
    before = vrun(vdb)
    assert by_reason(before, "I17", "snapshot_regression"), tag("FF", "T15/before", before)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, at=3, snapshot=5000)
    after = vrun(vdb)
    hits = by_reason(after, "I17", "snapshot_regression")
    assert hits and (hits[0]["detail"]["earlier_id"], hits[0]["detail"]["later_id"]) == (t1, t2), \
        tag("FF", "T15/after", after)
    assert "i17:snapshot_regression" in blockers(after), tag("FF", "T15/after", after)
    after_activation(lambda: not (after["gates"]["reconcile_precondition"] or after["gates"]["recovery_precondition"]
                                  or after["gates"]["activation_verification_ready"]) or pytest.fail(
        tag("FF", "T15", after)), why="reconcile, recovery and ready false")


def test_rev7_t16_db(vdb):
    """T16 (NC, control; U + DB): monotonic 2 500 -> 5 000 -> 6 000; P 6 000 -> no finding."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, at=1, snapshot=2500)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, at=2, snapshot=5000)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 1000, at=3, snapshot=6000)
    report = vrun(vdb)
    assert not found(report, "I17"), tag("NC", "T16", report)


# ── I10: catalog security C0-C33 (DB / DB-X); C34-C36 are unit-only (captured catalogs) ─────────────────────────────

def test_rev7_c0_db(vdb, phase2b_cluster_guard):
    """C0 (CC, DB): pristine 0034 on both versions. Before A5c: exactly one I10 finding, baseline_unverified,
    naming exactly the remaining unverified components (plan §5.1 C0-b). After A5c: clean (C0-c)."""
    report = vrun(vdb)
    if not _v("I10_BASELINE_CONFIRMED", False) or _v("SKIPPED_COMPARISONS", ()):
        assert len(report["findings"]) == 1, tag("CC", "C0-b", report)
        f = report["findings"][0]
        assert (f["invariant"], f["detail"]["problem"]) == ("I10", "baseline_unverified"), tag("CC", "C0-b", report)
        expected = [*([] if _v("I10_BASELINE_CONFIRMED", False) else _v("I10_UNVERIFIED", ())), *_v("SKIPPED_COMPARISONS", ())]
        assert f["detail"]["components"] == expected, tag("CC", "C0-b", report)
    else:
        assert report["status"] == "clean" and report["findings"] == [], tag("CC", "C0-c", report)
    assert report["database"]["head"] == "0034", tag("CC", "C0", report)


GUARD = "payment_accounting_runs_guard"


def test_rev7_c1_db(vdb):
    """C1 (FF, DB): a guard rebound to a no-op function -> trigger_binding:function."""
    _owner_sql(vdb, "CREATE FUNCTION public.authtest_noop() RETURNS trigger LANGUAGE plpgsql AS "
                    "$f$ BEGIN RETURN NULL; END $f$",
               f"DROP TRIGGER {GUARD} ON public.payment_accounting_runs",
               f"CREATE TRIGGER {GUARD} BEFORE UPDATE OR DELETE ON public.payment_accounting_runs FOR EACH ROW "
               "EXECUTE FUNCTION public.authtest_noop()")
    report = vrun(vdb)
    assert has_i10(report, "trigger_binding:function"), tag("FF", "C1", report)


def test_rev7_c2_db(vdb):
    """C2 (FF, DB): a guard recreated AFTER INSERT -> trigger_binding:type."""
    _owner_sql(vdb, f"DROP TRIGGER {GUARD} ON public.payment_accounting_runs",
               f"CREATE TRIGGER {GUARD} AFTER INSERT ON public.payment_accounting_runs FOR EACH ROW "
               "EXECUTE FUNCTION public.payment_accounting_append_only()")
    report = vrun(vdb)
    assert has_i10(report, "trigger_binding:type"), tag("FF", "C2", report)


def test_rev7_c3_db(vdb):
    """C3 (FF, DB): a guard with an OLD / NEW predicate -> trigger_binding:when."""
    _owner_sql(vdb, f"DROP TRIGGER {GUARD} ON public.payment_accounting_runs",
               f"CREATE TRIGGER {GUARD} BEFORE UPDATE ON public.payment_accounting_runs FOR EACH ROW "
               "WHEN (OLD.id IS DISTINCT FROM NEW.id) EXECUTE FUNCTION public.payment_accounting_append_only()")
    report = vrun(vdb)
    assert has_i10(report, "trigger_binding:when"), tag("FF", "C3", report)


def _comparable(report) -> dict:
    return {k: v for k, v in report.items() if k not in ("runtime", "snapshot", "digest")}


def test_rev7_c4_db(vdb):
    """C4 (CC, DB): an alternate search_path during verify gives an identical result."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500, snapshot=5000)
    plain = vrun(vdb)
    _owner_sql(vdb, "CREATE SCHEMA authtest_shadow",
               "CREATE TABLE authtest_shadow.donations (id uuid)")  # a shadowing relation in the alternate path
    _super_sql(vdb, "GRANT USAGE ON SCHEMA authtest_shadow TO hyperchat_readonly",
               "GRANT SELECT ON authtest_shadow.donations TO hyperchat_readonly")
    with vdb.conn("hyperchat_readonly", options="-c search_path=authtest_shadow,public") as c:
        shadowed = _vcall(c, max_seconds=MAX_SECONDS)
    assert _comparable(shadowed) == _comparable(plain), tag("CC", "C4", shadowed)


def test_rev7_c5_db(vdb):
    """C5 (FF, DB): donation_accounting_core set STRICT -> function_attr:proisstrict."""
    _owner_sql(vdb, "ALTER FUNCTION public.donation_accounting_core(uuid, bigint, text, text[], text[], text, bigint) "
                    "STRICT")
    report = vrun(vdb)
    assert has_i10(report, "function_attr:proisstrict"), tag("FF", "C5", report)


def test_rev7_c6_db(vdb):
    """C6 (FF, DB): an extra trigger on streamer_donator_totals -> unexpected_trigger."""
    _owner_sql(vdb, "CREATE TRIGGER authtest_extra BEFORE UPDATE ON public.streamer_donator_totals FOR EACH ROW "
                    "EXECUTE FUNCTION public.set_updated_at()")
    report = vrun(vdb)
    assert has_i10(report, "unexpected_trigger"), tag("FF", "C6", report)


def test_rev7_c7_db(vdb):
    """C7 (FF on the specific diagnosis, DB): an extra overload payment_accounting_keep(text) -> overloaded."""
    _owner_sql(vdb, "CREATE FUNCTION public.payment_accounting_keep(p text) RETURNS text LANGUAGE sql "
                    "AS $f$ SELECT p $f$")
    report = vrun(vdb)
    assert has_i10(report, "overloaded"), tag("FF", "C7", report)


def test_rev7_c8_db(vdb):
    """C8 (FF, DB): GRANT UPDATE (applied_reversed_paise) ON donation_reversals TO hyperchat_app."""
    _owner_sql(vdb, "GRANT UPDATE (applied_reversed_paise) ON public.donation_reversals TO hyperchat_app")
    report = vrun(vdb)
    assert has_i10(report, "column_update_granted:applied_reversed_paise:hyperchat_app"), tag("FF", "C8", report)


def test_rev7_c9_db(vdb):
    """C9 (FF, DB+R): a new login with table INSERT on payment_accounting_events -> table_insert_granted."""
    with authtest_role(vdb) as login:
        _owner_sql(vdb, f'GRANT INSERT ON public.payment_accounting_events TO "{login}"')
        report = vrun(vdb)
        assert has_i10(report, f"table_insert_granted:{login}"), tag("FF", "C9", report)


def test_rev7_c10_db(vdb):
    """C10 (FF on the specific diagnosis, DB): GRANT INSERT ON payment_accounting_runs TO PUBLIC ->
    public_table_insert, plus the effective findings."""
    _owner_sql(vdb, "GRANT INSERT ON public.payment_accounting_runs TO PUBLIC")
    report = vrun(vdb)
    assert has_i10(report, "public_table_insert"), tag("FF", "C10", report)
    assert has_i10(report, "table_insert_granted:"), tag("FF", "C10", report)


def test_rev7_c11_db(vdb):
    """C11 (FF, DB+R): a new login with EXECUTE on payment_accounting_reconcile -> function_acl:...:<login>:EXECUTE."""
    with authtest_role(vdb) as login:
        _owner_sql(vdb, "GRANT EXECUTE ON FUNCTION public.payment_accounting_reconcile(uuid, bigint, text, text[], "
                        f'text[], bigint) TO "{login}"')
        report = vrun(vdb)
        assert [p for p in i10_problems_of(report) if p.startswith("function_acl:payment_accounting_reconcile(")
                and p.endswith(f":{login}:EXECUTE")], tag("FF", "C11", report)


def test_rev7_c12_db(vdb):
    """C12 (CC on the detection; FF on the label; DB): revoke the app's EXECUTE on donation_accounting_apply."""
    _owner_sql(vdb, "REVOKE EXECUTE ON FUNCTION public.donation_accounting_apply(uuid, bigint, text, text[], text[]) "
                    "FROM hyperchat_app")
    report = vrun(vdb)
    assert [p for p in i10_problems_of(report) if "donation_accounting_apply" in p], tag("CC", "C12", report)
    assert has_i10(report, "function_acl_missing:donation_accounting_apply("), tag("FF", "C12", report)


def test_rev7_c13_db(vdb):
    """C13 (FF, DB+R): GRANT CREATE ON SCHEMA public TO <login> -> schema_create."""
    with authtest_role(vdb) as login:
        _owner_sql(vdb, f'GRANT CREATE ON SCHEMA public TO "{login}"')
        report = vrun(vdb)
        assert has_i10(report, f"schema_create:{login}"), tag("FF", "C13", report)


def test_rev7_c19_db(vdb):
    """C19 (FF, DB): donation_reversals owner changed -> ownership."""
    _super_sql(vdb, "ALTER TABLE public.donation_reversals OWNER TO hyperchat_migrator")
    report = vrun(vdb)
    assert has_i10(report, "ownership:donation_reversals"), tag("FF", "C19", report)


def test_rev7_c20_db(vdb):
    """C20 (FF, DB): an RLS policy hiding an evidence row -> refused row_security: payment_integrity_events."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500)
    _owner_sql(vdb, "ALTER TABLE public.payment_integrity_events ENABLE ROW LEVEL SECURITY",
               "CREATE POLICY authtest_hide ON public.payment_integrity_events USING (kind <> 'refund')")
    report = vrun(vdb)
    assert report["status"] == "refused" and "row_security: payment_integrity_events" in report["reason"], \
        tag("FF", "C20", report)
    assert not any(report["gates"].values()), tag("FF", "C20", report)


def test_rev7_c21_db(vdb):
    """C21 (FF, DB): GRANT ... WITH GRANT OPTION to a runtime role on a protected table -> grant_option."""
    _owner_sql(vdb, "GRANT SELECT ON public.donation_credit_events TO hyperchat_app WITH GRANT OPTION")
    report = vrun(vdb)
    assert has_i10(report, "grant_option:donation_credit_events:hyperchat_app"), tag("FF", "C21", report)


def _i10_beyond_baseline(report) -> list[str]:
    return [p for p in i10_problems_of(report) if p != "baseline_unverified"]


def test_rev7_c22_db(vdb):
    """C22 (CC, DB): the owner itself and the expected migrator edge are not findings."""
    report = vrun(vdb)
    assert not [p for p in _i10_beyond_baseline(report) if p.startswith(("role_edge", "owner_path", "admin_option"))], \
        tag("CC", "C22", report)


def test_rev7_c26_db(vdb):
    """C26 (CC, DB): the pristine owner and migrator paths: no I10 finding beyond the documented baseline one."""
    report = vrun(vdb)
    assert _i10_beyond_baseline(report) == [], tag("CC", "C26", report)


def test_rev7_c27_db(vdb):
    """C27 (FF on the specific diagnosis, DB): PUBLIC EXECUTE on payment_accounting_reconcile ->
    public_function_execute, plus the effective function_acl findings."""
    _owner_sql(vdb, "GRANT EXECUTE ON FUNCTION public.payment_accounting_reconcile(uuid, bigint, text, text[], text[], "
                    "bigint) TO PUBLIC")
    report = vrun(vdb)
    assert has_i10(report, "public_function_execute:payment_accounting_reconcile("), tag("FF", "C27", report)
    assert has_i10(report, "function_acl:payment_accounting_reconcile("), tag("FF", "C27", report)


def test_rev7_c28_db(vdb):
    """C28 (FF (CX5 B4), DB+R): a new login with only UPDATE (payment_status) on donations ->
    donations_column_write:UPDATE:payment_status:<login>; every write gate false before any donation changes."""
    with authtest_role(vdb) as login:
        _owner_sql(vdb, f'GRANT UPDATE (payment_status) ON public.donations TO "{login}"')
        report = vrun(vdb)
        assert has_i10(report, f"donations_column_write:UPDATE:payment_status:{login}"), tag("FF", "C28", report)
        after_activation(lambda: [g for g in WRITE_GATES if report["gates"][g]] == [] or pytest.fail(
            tag("FF", "C28", report)), why="every write gate false")


def test_rev7_c29_db(vdb):
    """C29 (FF, DB): GRANT INSERT ON donations TO hyperchat_readonly -> donations_write:INSERT:hyperchat_readonly."""
    _owner_sql(vdb, "GRANT INSERT ON public.donations TO hyperchat_readonly")
    report = vrun(vdb)
    assert has_i10(report, "donations_write:INSERT:hyperchat_readonly"), tag("FF", "C29", report)


def test_rev7_c30_db(vdb):
    """C30 (FF, DB): GRANT UPDATE (payment_status) ON donations TO PUBLIC -> public_column_update:payment_status,
    plus donations_column_write for every non-writer principal."""
    _owner_sql(vdb, "GRANT UPDATE (payment_status) ON public.donations TO PUBLIC")
    report = vrun(vdb)
    assert has_i10(report, "public_column_update:payment_status"), tag("FF", "C30", report)
    assert has_i10(report, "donations_column_write:UPDATE:payment_status:hyperchat_readonly"), tag("FF", "C30", report)


def test_rev7_c32_db(vdb):
    """C32 (FF, DB): EXECUTE on donation_accounting_apply to hyperchat_app WITH GRANT OPTION -> grant_option."""
    _owner_sql(vdb, "GRANT EXECUTE ON FUNCTION public.donation_accounting_apply(uuid, bigint, text, text[], text[]) "
                    "TO hyperchat_app WITH GRANT OPTION")
    report = vrun(vdb)
    assert has_i10(report, "grant_option:donation_accounting_apply(") and \
        [p for p in i10_problems_of(report) if p.startswith("grant_option:donation_accounting_apply(")
         and p.endswith(":hyperchat_app")], tag("FF", "C32", report)


def test_rev7_c33_db(vdb):
    """C33 (FF, DB): REVOKE EXECUTE ON FUNCTION donations_require_eligible_creator() FROM PUBLIC ->
    function_acl_missing:...:PUBLIC (a drift from the pinned baseline)."""
    _owner_sql(vdb, "REVOKE EXECUTE ON FUNCTION public.donations_require_eligible_creator() FROM PUBLIC")
    report = vrun(vdb)
    assert [p for p in i10_problems_of(report) if p.startswith("function_acl_missing:donations_require_eligible_creator(")
            and p.endswith(":PUBLIC")], tag("FF", "C33", report)


# ── DB-X: existing-role changes, X15 / X17 only (A7b), each with RT-5 restoration ───────────────────────────────────

ROLE_STATE = ("SELECT 'role' AS k, r.rolname::text, concat_ws(',', r.rolsuper, r.rolinherit, r.rolcreaterole, "
              "r.rolcreatedb, r.rolcanlogin, r.rolreplication, r.rolbypassrls, r.rolconnlimit) "
              "FROM pg_catalog.pg_roles r WHERE r.rolname LIKE 'hyperchat\\_%' OR r.rolname LIKE 'pg\\_%' "
              "UNION ALL SELECT 'edge', m.member::regrole::text || '->' || m.roleid::regrole::text, "
              "concat_ws(',', m.admin_option{opts}) FROM pg_catalog.pg_auth_members m "
              "UNION ALL SELECT 'datdba', d.datname::text, pg_catalog.pg_get_userbyid(d.datdba)::text "
              "FROM pg_catalog.pg_database d WHERE d.datname IN ('hyperchat_ci', 'hyperchat_verify_tmpl') "
              "ORDER BY 1, 2, 3")


def role_state() -> list[tuple]:
    """RT-5: the cluster's role attributes, memberships (with PG16+ options) and the template owners, read from a
    NEW independent superuser session every time."""
    with psycopg.connect(os.environ["HYPERCHAT_TEST_VERIFY_ADMIN"], autocommit=True) as c:
        v = c.execute("SELECT current_setting('server_version_num')::int").fetchone()[0]
        opts = ", m.inherit_option, m.set_option" if v >= 160000 else ""
        return c.execute(ROLE_STATE.format(opts=opts)).fetchall()


def _migrator_edge_restore() -> str:
    """The exact pre-change PG16+ options of the migrator -> owner edge, as a GRANT that restores them."""
    with _admin_conn() as m:
        inh, st = m.execute("SELECT inherit_option, set_option FROM pg_auth_members WHERE roleid = "
                            "'hyperchat_owner'::regrole AND member = 'hyperchat_migrator'::regrole").fetchone()
    return (f"GRANT hyperchat_owner TO hyperchat_migrator WITH INHERIT {'TRUE' if inh else 'FALSE'}, "
            f"SET {'TRUE' if st else 'FALSE'}")



@pytest.fixture
def xroles(xcluster):
    """DB-X (Rev 7 §5.8): records the pre-state from an independent session, runs the test, then restores and
    verifies from a NEW session through restore_and_verify (the single S8 path)."""
    before = role_state()  # read-only; a failure here precedes any mutation
    restore: list[str] = []
    try:
        yield restore
    finally:
        restore_and_verify(before, restore, connect=_admin_conn, read_state=role_state, ident=xcluster,
                           quarantine_dir=os.environ[QUARANTINE_ENV])


def _version(vdb) -> int:
    with vdb.owner() as c:
        return c.execute("SELECT current_setting('server_version_num')::int").fetchone()[0]


@exclusive_cluster
def test_rev7_c14_db(vdb, xroles):
    """C14 (FF, DB-X / U): GRANT hyperchat_owner TO <login> -> role_edge, plus owner_path."""
    with authtest_role(vdb) as login:
        _super_sql(vdb, f'GRANT hyperchat_owner TO "{login}"', database=False)
        report = vrun(vdb)
        assert has_i10(report, f"role_edge:{login}->hyperchat_owner") and has_i10(report, "owner_path"), \
            tag("FF", "C14", report)


@exclusive_cluster
def test_rev7_c15_db(vdb, xroles):
    """C15 (FF, DB-X / U; PG16+): a set-only path into the owner -> role_edge, plus owner_path."""
    if _version(vdb) < 160000:
        pytest.skip("C15 needs PG16+ membership options (PG15: unit-covered)")
    with authtest_role(vdb) as login:
        _super_sql(vdb, f'GRANT hyperchat_owner TO "{login}" WITH INHERIT FALSE, SET TRUE', database=False)
        report = vrun(vdb)
        assert has_i10(report, f"role_edge:{login}->hyperchat_owner") and has_i10(report, "owner_path"), \
            tag("FF", "C15", report)


@exclusive_cluster
def test_rev7_c16_db(vdb, xroles):
    """C16 (FF, DB-X / U): x -> migrator -> owner -> role_edge, plus owner_path."""
    with authtest_role(vdb) as login:
        _super_sql(vdb, f'GRANT hyperchat_migrator TO "{login}"', database=False)
        report = vrun(vdb)
        assert has_i10(report, f"role_edge:{login}->hyperchat_migrator") and has_i10(report, "owner_path"), \
            tag("FF", "C16", report)


@exclusive_cluster
@pytest.mark.parametrize("attr, problem", [("SUPERUSER", "role_attribute:rolsuper"),
                                           ("BYPASSRLS", "role_attribute:rolbypassrls")])
def test_rev7_c17_db(vdb, xroles, attr, problem):
    """C17 (FF on the specific diagnosis, DB-X / U): ALTER ROLE hyperchat_app SUPERUSER / BYPASSRLS."""
    xroles.append(f"ALTER ROLE hyperchat_app NO{attr}")
    _super_sql(vdb, f"ALTER ROLE hyperchat_app {attr}", database=False)
    report = vrun(vdb)
    assert [f for f in found(report, "I10") if f["detail"].get("problem") == problem
            and f["entity_id"] == "hyperchat_app"], tag("FF", "C17", report)


@exclusive_cluster
def test_rev7_c18_db(vdb, xroles):
    """C18 (FF, DB-X / U): GRANT pg_write_all_data TO <login> -> predefined_role_path, plus role_edge."""
    with authtest_role(vdb) as login:
        _super_sql(vdb, f'GRANT pg_write_all_data TO "{login}"', database=False)
        report = vrun(vdb)
        assert has_i10(report, "predefined_role_path:pg_write_all_data") and \
            has_i10(report, f"role_edge:{login}->pg_write_all_data"), tag("FF", "C18", report)


@exclusive_cluster
def test_rev7_c23_db(vdb, xroles):
    """C23 (FF, DB-X / U; PG17 set-only): GRANT hyperchat_app TO hyperchat_readonly ->
    role_edge:hyperchat_readonly->hyperchat_app."""
    if _version(vdb) < 160000:
        pytest.skip("C23 needs PG16+ membership options (PG15: unit-covered)")
    xroles.append("REVOKE hyperchat_app FROM hyperchat_readonly")
    _super_sql(vdb, "GRANT hyperchat_app TO hyperchat_readonly WITH INHERIT FALSE, SET TRUE", database=False)
    report = vrun(vdb, role="hyperchat_readonly")
    assert has_i10(report, "role_edge:hyperchat_readonly->hyperchat_app"), tag("FF", "C23", report)


@exclusive_cluster
def test_rev7_c24_db(vdb, xroles):
    """C24 (FF, DB-X / U; a PG15 case, PG17 informational): readonly -> NOINHERIT intermediary -> app ->
    role_edge on both edges."""
    with authtest_role(vdb, login=False, inherit=False) as mid:
        _super_sql(vdb, f'GRANT hyperchat_app TO "{mid}"', f'GRANT "{mid}" TO hyperchat_readonly', database=False)
        report = vrun(vdb)
        assert has_i10(report, f"role_edge:hyperchat_readonly->{mid}") and \
            has_i10(report, f"role_edge:{mid}->hyperchat_app"), tag("FF", "C24", report)


@exclusive_cluster
def test_rev7_c25_db(vdb, xroles):
    """C25 (FF, DB-X / U): the database owner changed to a new role -> database_owner (structural)."""
    with authtest_role(vdb) as login:
        xroles.append(f'ALTER DATABASE "{vdb.name}" OWNER TO hyperchat_owner')
        _super_sql(vdb, f'ALTER DATABASE "{vdb.name}" OWNER TO "{login}"', database=False)
        report = vrun(vdb)
        assert has_i10(report, "database_owner"), tag("FF", "C25", report)
        _super_sql(vdb, f'ALTER DATABASE "{vdb.name}" OWNER TO hyperchat_owner', database=False)  # before DROP OWNED


@exclusive_cluster
def test_rev7_c31_db(vdb, xroles):
    """C31 (FF, DB-X / U): a new role that is a member of hyperchat_app (inherited donation rights) -> role_edge,
    plus donations_write for the new role."""
    with authtest_role(vdb) as login:
        _super_sql(vdb, f'GRANT hyperchat_app TO "{login}"', database=False)
        report = vrun(vdb)
        assert has_i10(report, f"role_edge:{login}->hyperchat_app") and \
            has_i10(report, "donations_write:") and any(p.endswith(f":{login}") for p in i10_problems_of(report)
                                                        if p.startswith("donations_write:")), tag("FF", "C31", report)


# ── runtime: R6 / R6b (Rev 7), R15 (MOD; the existing test is the modified form) ───────────────────────────────────

def _admin_conn():
    return psycopg.connect(os.environ["HYPERCHAT_TEST_VERIFY_ADMIN"], autocommit=True)


def _verifier_backends(vdb) -> list:
    with _admin_conn() as m:
        return m.execute("SELECT pid, state FROM pg_stat_activity WHERE datname = %s AND usename = "
                         "'hyperchat_readonly'", (vdb.name,)).fetchall()


def test_rev7_r6_db(vdb):
    """R6 (NC, DB): blocked SQL (a lock held elsewhere) with a short budget -> refused within the remaining time plus
    cancellation; the connection closed; no verifier backend or lock afterwards."""
    holder = vdb.owner()
    holder.autocommit = False
    holder.execute("LOCK TABLE public.payment_integrity_events IN ACCESS EXCLUSIVE MODE")
    try:
        t0 = time.monotonic()
        report = vrun(vdb, max_seconds=5)
        elapsed = time.monotonic() - t0
    finally:
        holder.rollback()
        holder.close()
    assert report["status"] == "refused" and report["exit_code"] == 2, tag("NC", "R6", report)
    assert elapsed < 5 + 10, tag("NC", "R6", {"elapsed": elapsed})  # remaining time plus a cancellation margin
    assert _verifier_backends(vdb) == [], tag("NC", "R6")
    with _admin_conn() as m:
        assert m.execute("SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                         "WHERE a.usename = 'hyperchat_readonly'").fetchone()[0] == 0, tag("NC", "R6")


FETCH_GATE_KEY = 6_206_202  # an advisory-lock key used only by this test, in its own throwaway database


def test_rev7_r6b_db(vdb):
    """R6b (NC, DB): a FETCH that blocks INSIDE PostgreSQL. Test-only setup in the throwaway database: the view read
    by the approved CURRENT constant is wrapped so that evaluating its first row calls a volatile function that waits
    on an advisory lock another session holds. The verifier's statements are unchanged (approved constants under
    DECLARE / FETCH): DECLARE completes (planning never runs the volatile function), the first FETCH waits in the
    server, statement_timeout / lock_timeout cancels it server-side, and the verifier refuses, rolls back and closes.
    Proven: DECLARE completed; that cursor's FETCH was observed waiting on the advisory lock (pg_stat_activity);
    refused with a server-side cancellation reason; ROLLBACK issued; afterwards no verifier backend, no lock and no
    snapshot (backend_xmin) remain."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500)  # the view has a row to evaluate
    with vdb.owner() as c:
        (definition,) = c.execute("SELECT pg_get_viewdef('public.payment_integrity_current'::regclass, true)"
                                  ).fetchone()
    _owner_sql(vdb, "CREATE FUNCTION public.authtest_fetch_gate() RETURNS boolean LANGUAGE plpgsql VOLATILE AS "
                    f"$f$ BEGIN PERFORM pg_catalog.pg_advisory_lock_shared({FETCH_GATE_KEY}); RETURN true; END $f$",
               "CREATE OR REPLACE VIEW public.payment_integrity_current AS SELECT v.* FROM ("
               + definition.strip().rstrip(";") + ") v WHERE public.authtest_fetch_gate()")
    _super_sql(vdb, "GRANT EXECUTE ON FUNCTION public.authtest_fetch_gate() TO PUBLIC")
    holder = vdb.owner()
    holder.execute(f"SELECT pg_catalog.pg_advisory_lock({FETCH_GATE_KEY})")  # session-level, held until released
    done, observed = threading.Event(), []

    def observe():
        with _admin_conn() as m:
            while not done.is_set():
                rows = m.execute("SELECT query FROM pg_stat_activity WHERE datname = %s AND usename = "
                                 "'hyperchat_readonly' AND state = 'active' AND wait_event_type = 'Lock' AND "
                                 "wait_event = 'advisory'", (vdb.name,)).fetchall()
                observed.extend(r[0] for r in rows)
                time.sleep(0.05)
    watcher = threading.Thread(target=observe)
    completed = []

    class Completed(psycopg.Cursor):
        def execute(self, query, params=None, **kw):
            out = super().execute(query, params, **kw)
            completed.append(str(query))  # recorded only once the server finished the statement
            return out
    watcher.start()
    try:
        with vdb.conn("hyperchat_readonly", cursor_factory=Completed) as c:
            report = _vcall(c, max_seconds=6)
            usable = not c.closed and c.info.transaction_status.name != "IDLE"
    finally:
        done.set()
        watcher.join(10)
        holder.execute(f"SELECT pg_catalog.pg_advisory_unlock({FETCH_GATE_KEY})")
        holder.close()
    declared = [q for q in completed if q.startswith("DECLARE vfy_") and q.endswith(verify.CURRENT)]
    assert declared, tag("NC", "R6b", report)  # 1. the DECLARE completed
    cursor = declared[0].split()[1]
    waited = [q for q in observed if q.startswith("FETCH FORWARD ") and q.endswith(f" FROM {cursor}")]
    assert waited, tag("NC", "R6b", report)  # 2. that cursor's FETCH waited on the advisory lock inside the server
    assert not [q for q in completed if q.startswith("FETCH") and q.endswith(f"FROM {cursor}")], \
        tag("NC", "R6b", report)  # that FETCH never completed: the server cancelled it
    assert report["status"] == "refused" and report["exit_code"] == 2 and report["reason"] in (
        "statement timeout or cancellation (deadline)", "lock timeout"), tag("NC", "R6b", report)  # 3. and 4.
    assert not any(report["gates"].values()), tag("NC", "R6b", report)
    assert completed and completed[-1] == "ROLLBACK", tag("NC", "R6b", report)  # 5.
    assert not usable and _verifier_backends(vdb) == [], tag("NC", "R6b")  # 6. and 7.
    with _admin_conn() as m:
        assert m.execute("SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid WHERE a.usename = "
                         "'hyperchat_readonly' AND a.datname = %s", (vdb.name,)).fetchone()[0] == 0, tag("NC", "R6b")
        assert m.execute("SELECT count(*) FROM pg_stat_activity WHERE usename = 'hyperchat_readonly' AND datname = %s "
                         "AND backend_xmin IS NOT NULL", (vdb.name,)).fetchone()[0] == 0, tag("NC", "R6b")


# ── Appendix T2: plan-defined DB checks ─────────────────────────────────────────────────────────────────────────────

class StatementLog(psycopg.Cursor):
    log: list = []

    def execute(self, query, params=None, **kw):
        StatementLog.log.append((str(query), params))
        return super().execute(query, params, **kw)


def _logged_verify(vdb, **kw):
    StatementLog.log = []
    with vdb.conn("hyperchat_readonly", cursor_factory=StatementLog) as c:
        report = _vcall(c, max_seconds=MAX_SECONDS, **kw)
    return report, list(StatementLog.log)


def test_2b_d1a_parameterised_declare_binds_db(vdb):
    """T2-1 (D1-a): every parameterised approved constant runs as DECLARE ... FOR <constant> with bound
    parameters (psycopg sends $n placeholders); never interpolated text."""
    vdb.go("active", "hold")
    sid, did, _ = _held(vdb)
    assert vdb.keep(did, vdb.run("resolve-keep", [did])) == "kept"  # reaches the A1 path (FP_SETS, REF_EVENTS)
    report, log = _logged_verify(vdb)
    assert report["status"] != "refused", report
    declares = [(q, p) for q, p in log if q.startswith("DECLARE vfy_")]
    with_params = [(q, p) for q, p in declares if p]
    assert with_params, "no parameterised DECLARE observed"
    for q, p in with_params:
        assert any(q.endswith(c) for c in verify._APPROVED_SQL), q
        assert did not in q, q  # the value is bound, never interpolated into the statement text


def test_2b_d1b_declare_parameter_types_db(vdb):
    """T2-2 (D1-b): CUT_ROWS with a uuid and 'infinity' through the Session equals an owner reference query."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500)
    with vdb.conn("hyperchat_readonly") as c, c.cursor(row_factory=dict_row) as cur:
        mine = verify._select(cur, verify.CUT_ROWS, {"d": did, "cut": "infinity"})
    with vdb.owner() as c, c.cursor(row_factory=dict_row) as cur:
        ref = verify._select(cur, verify.CURRENT + " AND donation_id = %(d)s::uuid", {"d": did})
    assert [dict(x) for x in mine] == [dict(x) for x in ref]


def test_2b_d1c_fetch_batch_boundary_db(vdb, monkeypatch):
    """T2-3 (D1-c): with FETCH_BATCH = 2 and 2 / 3 streamers: exact counts, one extra FETCH, CLOSE issued."""
    monkeypatch.setattr(verify, "FETCH_BATCH", 2)
    for n in (2, 3):
        while True:
            with vdb.owner() as c:
                if c.execute("SELECT count(*) FROM public.streamers").fetchone()[0] >= n:
                    break
            vdb.creator()
        report, log = _logged_verify(vdb)
        assert report["status"] != "refused", report
        (cursor,) = [q.split()[1] for q, _ in log if q.startswith("DECLARE vfy_") and q.endswith(verify.STREAMERS)]
        fetches = [q for q, _ in log if q == f"FETCH FORWARD 2 FROM {cursor}"]
        assert len(fetches) == n // 2 + 1, (n, fetches)  # full batches, then exactly one short (or empty) batch
        assert f"CLOSE {cursor}" in [q for q, _ in log]


def test_2b_ro1_read_only_repeatable_read_db(vdb):
    """T2-4 (RO-1): the verify transaction is REPEATABLE READ READ ONLY as hyperchat_readonly."""
    report, log = _logged_verify(vdb)
    assert log[0][0] == "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert report["status"] != "refused" and report.get("snapshot"), report


def test_2b_ro5_policy_after_catalog_check_db(vdb):
    """T2-5 (RO-5): a policy committed after the catalog check (second session) -> refused row_security:
    read_filtered (SET LOCAL row_security = off turns a filtered read into an error)."""
    sid = vdb.creator()
    did, pid = vdb.donation(sid)
    _ev(vdb, did, pid, "refund", _ref(), "processed", 2500)
    done = []

    class PolicyAfterCatalog(psycopg.Cursor):
        def execute(self, query, params=None, **kw):
            q = str(query)
            # the first financial read (donations): after the §5.6 catalog check (CAT_RELATIONS), before any
            # user-table lock; later the CURRENT view locks payment_integrity_events, so DDL there would wait forever
            if not done and q.startswith("DECLARE") and q.endswith(verify.DONATIONS):
                done.append(q)
                _owner_sql(vdb, "SET LOCAL lock_timeout = '10s'",  # a design error fails fast, never hangs
                           "ALTER TABLE public.payment_integrity_events ENABLE ROW LEVEL SECURITY",
                           "CREATE POLICY authtest_late ON public.payment_integrity_events USING (true)")
            return super().execute(query, params, **kw)
    with vdb.conn("hyperchat_readonly", cursor_factory=PolicyAfterCatalog) as c:
        report = _vcall(c, max_seconds=MAX_SECONDS)
    assert done and report["status"] == "refused" and "row_security" in report["reason"], report


def test_2b_rt1_deadline_in_cpu_phase_db(vdb, monkeypatch):
    """T2-6 (RT-1): the deadline passes in the CPU phase on a real hot donation -> refused, ROLLBACK, exit 2."""
    sid, did, pid = _held(vdb, extra_refunds=11)
    assert vdb.keep(did, vdb.run("resolve-keep", [did])) == "kept"
    real = verify.Verifier.classify

    def late_classify(self, *a, **k):
        self.budget.deadline = self.budget.clock() - 1  # the deadline is now in the past
        return real(self, *a, **k)
    monkeypatch.setattr(verify.Verifier, "classify", late_classify)
    report, log = _logged_verify(vdb)
    assert report["status"] == "refused" and report["exit_code"] == 2 and "deadline" in report["reason"], report
    assert log[-1][0] == "ROLLBACK" and not any(report["gates"].values())


def test_2b_rt2_cleanup_completes_after_deadline_db(vdb, monkeypatch):
    """T2-7 (RT-2): R8b / R8c on a real connection: the clock crosses the deadline during cleanup; ROLLBACK and
    close still complete, then the report is refused."""
    seen = {}
    real = psycopg.Cursor.execute

    def expire_on_rollback(self, query, params=None, **kw):
        if str(query) == "ROLLBACK":
            seen["rollback"] = True  # the deadline passes while the cleanup runs
        return real(self, query, params, **kw)
    monkeypatch.setattr(psycopg.Cursor, "execute", expire_on_rollback)
    with vdb.conn("hyperchat_readonly") as c:
        report = _vcall(c, max_seconds=MAX_SECONDS, clock=lambda: 1e9 if seen else 0.0)
        idle = c.info.transaction_status.name == "IDLE"
    assert seen and idle, report  # ROLLBACK completed and the session released its snapshot
    assert report["status"] == "refused" and report["exit_code"] == 2 and not any(report["gates"].values()), report


def test_2b_rt3_client_kill_releases_snapshot_db(vdb):
    """T2-8 (RT-3): the verifier's client process is terminated mid-run; the server ends its backend and releases
    the snapshot (observed from an independent session)."""
    import subprocess
    import sys
    dsn = f"host={PG_HOST} port={vdb.port} dbname={vdb.name} user=hyperchat_readonly"
    code = ("import psycopg,time;c=psycopg.connect(%r,autocommit=True);c.execute('BEGIN ISOLATION LEVEL REPEATABLE '"
            "'READ READ ONLY');c.execute('SELECT 1');print('ready',flush=True);time.sleep(600)") % dsn
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "ready"
        assert _verifier_backends(vdb), "the child's backend was not observed"
    finally:
        p.kill()
        p.wait(10)
    for _ in range(100):
        if not _verifier_backends(vdb):
            break
        time.sleep(0.1)
    assert _verifier_backends(vdb) == []


def test_2b_rt4_lock_timeout_precedes_statement_timeout_db(vdb):
    """T2-9 (RT-4): a pure lock wait is ended by lock_timeout = 5 s, before statement_timeout."""
    holder = vdb.owner()
    holder.autocommit = False
    holder.execute("LOCK TABLE public.donations IN ACCESS EXCLUSIVE MODE")
    try:
        t0 = time.monotonic()
        report = vrun(vdb, max_seconds=60)
        elapsed = time.monotonic() - t0
    finally:
        holder.rollback()
        holder.close()
    assert report["status"] == "refused" and 4 <= elapsed < 30, (elapsed, report)
    assert report["reason"].endswith("lock timeout"), report


@exclusive_cluster
def test_2b_rt5_restoration_checker_detects_drift_xdb(vdb, xroles):
    """T2-10 (RT-5 self-test; A7a): on the I9 object the checker sees an unrestored change and an exact restore."""
    v = _version(vdb)
    before = role_state()
    change, undo = (("GRANT hyperchat_owner TO hyperchat_migrator WITH INHERIT TRUE", _migrator_edge_restore())
                    if v >= 160000 else
                    ("ALTER ROLE hyperchat_migrator INHERIT", "ALTER ROLE hyperchat_migrator NOINHERIT"))
    xroles.append(undo)
    _super_sql(vdb, change, database=False)
    assert role_state() != before, "the checker missed a deliberate change"
    _super_sql(vdb, undo, database=False)
    assert role_state() == before, "the checker reports drift after an exact restore"


def test_2b_rt6_no_leftover_authtest_roles_db(vdb):
    """T2-11 (RT-6): after the DB+R tests no authtest_ role remains (run at the end of the module)."""
    with _admin_conn() as m:
        left = m.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE 'authtest\\_%'").fetchall()
    assert left == []


def test_2b_identity_guard_refuses_mismatch_db(phase2b_cluster_guard):
    """T2-12 (IDN): the approved identity passes; any changed expectation, the §0 ports or a socket host refuse."""
    env = dict(os.environ)
    admin = conninfo_to_dict(os.environ["HYPERCHAT_TEST_VERIFY_ADMIN"])
    assert cluster_identity_problems(phase2b_cluster_guard, env, admin, _ci_capture(env)) == []
    for key, bad in (("HYPERCHAT_TEST_EXPECT_SYSID", "1"), ("HYPERCHAT_TEST_EXPECT_DATADIR", "C:\\elsewhere"),
                     ("HYPERCHAT_TEST_EXPECT_VERSION_NUM", "999999")):
        if env.get(key):
            assert cluster_identity_problems(phase2b_cluster_guard, {**env, key: bad}, admin)
    assert cluster_identity_problems({**phase2b_cluster_guard, "port": "55435"}, env, admin)
    assert cluster_identity_problems(phase2b_cluster_guard, env, {**admin, "host": "/var/run/postgresql"})
    forged = {k: v for k, v in env.items() if not k.startswith(("HYPERCHAT_TEST_EXPECT_", "HYPERCHAT_TEST_CI_"))}
    assert cluster_identity_problems(phase2b_cluster_guard, {**forged, "GITHUB_ACTIONS": "true"}, admin)  # never a bypass


def test_2b_exclusive_gate_db(phase2b_cluster_guard):
    """T2-13 (XGATE): skip on an E port without the flag; fail if forced on an E port; run only on X15 / X17."""
    e = {**phase2b_cluster_guard, "port": "55445"}
    x = {**phase2b_cluster_guard, "port": "55455"}
    assert exclusive_decision(e, {}) == "skip"
    assert exclusive_decision(e, {"HYPERCHAT_TEST_EXCLUSIVE_CLUSTER": "1", "HYPERCHAT_TEST_EXPECT_SYSID": "1"}) == "fail"
    assert exclusive_decision(x, {"HYPERCHAT_TEST_EXCLUSIVE_CLUSTER": "1"}) == "fail"  # no expectations: refused
    assert exclusive_decision(x, {"HYPERCHAT_TEST_EXCLUSIVE_CLUSTER": "1", "HYPERCHAT_TEST_EXPECT_SYSID": "1"}) == "run"


# ── §6A I10 baseline activation, DB forms (T2-14); unit forms and the anti-shortcut guard are in the rev 7 units ──
# Each case: (row, label, setup SQL run as the superuser in the throwaway DB, expected code or None for a VOCABULARY
# GAP (OD-11), active-now). "active-now" is the 2B-0 state: the UV rows are already compared against migration
# values; the SK rows are not compared at all. A5b adds a row to verify.I10_ACTIVE_COMPARISONS (an A5b-defined
# frozenset; absent today) when it implements it. While a row is inactive its negative must NOT produce the code
# (the comparison-off fail-first, §6A rule 3) and its list text must still be reported as unverified.

ACT_DB = [
    ("I1", "schema_owner_other_candidate", ["__OTHER_SCHEMA_OWNER__"], "ownership:schema_public",
     False),
    ("I2", "schema_acl_public_create", ["GRANT CREATE ON SCHEMA public TO PUBLIC"], "public_schema_create", True),
    ("I2", "schema_acl_removal", ["REVOKE USAGE ON SCHEMA public FROM hyperchat_app"],
     "schema_acl_missing:schema_public:hyperchat_app:USAGE", False),  # OD-11
    ("I2", "schema_acl_grant_option", ["GRANT USAGE ON SCHEMA public TO hyperchat_readonly WITH GRANT OPTION"],
     "grant_option:schema_public:hyperchat_readonly", True),
    ("I3", "prosrc_set_updated_at", ["__REPLACE_BODY__ set_updated_at"], "body_drift", False),
    ("I4", "prosrc_eligible_creator", ["__REPLACE_BODY__ donations_require_eligible_creator"], "body_drift", False),
    ("I5", "table_acl_removal", ["REVOKE SELECT ON public.donation_reversals FROM hyperchat_readonly"],
     "table_acl_missing:donation_reversals:hyperchat_readonly:SELECT", False),  # OD-11
    ("I5", "table_acl_alteration", ["REVOKE INSERT ON public.payment_integrity_events FROM hyperchat_app",
                                    "GRANT INSERT ON public.payment_integrity_events TO hyperchat_worker"],
     "table_insert_granted:hyperchat_worker", True),
    ("I5", "table_acl_grant_option", ["GRANT SELECT ON public.donation_reversals TO hyperchat_readonly "
                                      "WITH GRANT OPTION"], "grant_option:donation_reversals:hyperchat_readonly", True),
    ("I6", "column_acl_addition", ["GRANT UPDATE (applied_reversed_paise) ON public.donation_reversals TO "
                                   "hyperchat_app"], "column_update_granted:applied_reversed_paise:hyperchat_app",
     True),
    ("I6", "column_acl_alteration", ["GRANT SELECT (payment_status) ON public.donations TO hyperchat_auth"],
     "column_acl:donations.payment_status:hyperchat_auth:SELECT", False),  # OD-11
    ("I7", "alembic_owner", ["ALTER TABLE public.alembic_version OWNER TO hyperchat_migrator"],
     "ownership:alembic_version", False),
    ("I8", "fn_acl_removal_worker", ["REVOKE EXECUTE ON FUNCTION public.set_updated_at() FROM hyperchat_worker"],
     "function_acl_missing:set_updated_at():hyperchat_worker", True),
    ("I10", "fn_args", ["DROP FUNCTION public.payment_accounting_keep(uuid, bigint)",
                        "CREATE FUNCTION public.payment_accounting_keep(p_donation uuid, p_run integer) RETURNS text "
                        "LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public "
                        "AS $f$ BEGIN RETURN 'x'; END $f$"], "function_attr:args", True),
    ("I11", "fn_returns", ["DROP FUNCTION public.payment_accounting_open_run(text, text, text, text, jsonb)",
                           "CREATE FUNCTION public.payment_accounting_open_run(p_command text, p_operator text, "
                           "p_reason text, p_ref text, p_scope jsonb) RETURNS integer LANGUAGE sql SECURITY DEFINER "
                           "SET search_path = pg_catalog, public AS $f$ SELECT 1 $f$"], "function_attr:returns", True),
    ("I12", "fn_proconfig", ["ALTER FUNCTION public.payment_accounting_keep(uuid, bigint) SET work_mem = '64kB'"],
     "function_attr:proconfig", True),
    ("I13", "trigger_type", [f"DROP TRIGGER {GUARD} ON public.payment_accounting_runs",
                             f"CREATE TRIGGER {GUARD} AFTER INSERT ON public.payment_accounting_runs FOR EACH ROW "
                             "EXECUTE FUNCTION public.payment_accounting_append_only()"], "trigger_binding:type", True),
    ("I14", "trigger_args", [f"DROP TRIGGER {GUARD} ON public.payment_accounting_runs",
                             f"CREATE TRIGGER {GUARD} BEFORE UPDATE OR DELETE ON public.payment_accounting_runs FOR "
                             "EACH ROW EXECUTE FUNCTION public.payment_accounting_append_only('x')"],
     "trigger_binding:args", True),
    ("I15", "trigger_constraint", [f"DROP TRIGGER {GUARD} ON public.payment_accounting_runs",
                                   f"CREATE CONSTRAINT TRIGGER {GUARD} AFTER UPDATE OR DELETE ON "
                                   "public.payment_accounting_runs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                                   "EXECUTE FUNCTION public.payment_accounting_append_only()"],
     "trigger_binding:constraint", True),
    ("I16", "trigger_columns", [f"DROP TRIGGER {GUARD} ON public.payment_accounting_runs",
                                f"CREATE TRIGGER {GUARD} BEFORE UPDATE OF command OR DELETE ON "
                                "public.payment_accounting_runs FOR EACH ROW "
                                "EXECUTE FUNCTION public.payment_accounting_append_only()"],
     "trigger_binding:columns", True),
    ("I16", "trigger_when", [f"DROP TRIGGER {GUARD} ON public.payment_accounting_runs",
                             f"CREATE TRIGGER {GUARD} BEFORE UPDATE ON public.payment_accounting_runs FOR EACH ROW "
                             "WHEN (OLD.id IS DISTINCT FROM NEW.id) "
                             "EXECUTE FUNCTION public.payment_accounting_append_only()"], "trigger_binding:when", True),
]
ROW_LIST_TEXT = {  # §6A row -> the verbatim I10_UNVERIFIED / SKIPPED_COMPARISONS entry it activates
    "I1": 0, "I2": 0, "I3": 1, "I4": 1, "I5": 2, "I6": 2, "I7": 3,          # SKIPPED_COMPARISONS[index]
    "I8": -1, "I9": -2, "I10": -3, "I11": -3, "I12": -3, "I13": -4, "I14": -4, "I15": -4, "I16": -4,  # UV[-n-1]
}


A5B_STARTED = hasattr(verify, "I10_ACTIVE_COMPARISONS")  # A5b defines it; absent at A1 / A7a
needs_a5b = pytest.mark.skipif(not A5B_STARTED, reason="§6A DB tests run from A5b (plan A1: skipped until A5b; the "
                                                        "A7a self-test is the only DB-X test before it)")


def _row_active(row, active_now) -> bool:
    return active_now or row in getattr(verify, "I10_ACTIVE_COMPARISONS", frozenset())


def _list_text(row) -> str:
    i = ROW_LIST_TEXT[row]
    return _v("SKIPPED_COMPARISONS", ())[i] if i >= 0 else _v("I10_UNVERIFIED", ())[-i - 1]


def _replace_body(vdb, name):
    """The function's own definition (every SET clause kept) with one comment line added to its body: textually
    different, semantically equal, so only the prosrc md5 changes."""
    with vdb.owner() as c:
        (definition,) = c.execute("SELECT pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON "
                                  "n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = %s",
                                  (name,)).fetchone()
    assert definition.count("AS $function$") == 1, definition
    return definition.replace("AS $function$", "AS $function$\n-- authtest: textually different", 1)


def _other_schema_owner(vdb) -> str:
    with vdb.owner() as c:
        owner = c.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'").fetchone()[0]
    return "ALTER SCHEMA public OWNER TO " + ("hyperchat_owner" if owner == "pg_database_owner" else "pg_database_owner")


@needs_a5b
@pytest.mark.parametrize("row, label", sorted({(r, lab) for r, lab, *_ in ACT_DB}))
def test_i10_act_pristine_db(vdb, row, label):
    """§6A positive (rule 1): the pristine template has no finding with this row's code."""
    (code,) = [c for r, lab, _, c, _ in ACT_DB if (r, lab) == (row, label)]
    report = vrun(vdb)
    if code is not None:
        assert not has_i10(report, code), (row, label, i10_problems_of(report))
    elif not _row_active(row, False):  # an OD-11 gap row: no code to look for; it must still be listed as unverified
        unverified = [f["detail"]["components"] for f in found(report, "I10")
                      if f["detail"]["problem"] == "baseline_unverified"]
        assert unverified and _list_text(row) in unverified[0], (row, label)


U6_XFAIL = pytest.mark.xfail(strict=True, reason="U-6: §6A I5(a) fixture revokes the verifier's own SELECT; "
                                                "plan/fixture correction pending (Part C report §U-6)")


@needs_a5b
@pytest.mark.parametrize("row, label", [pytest.param(r, lab, marks=U6_XFAIL) if (r, lab) == ("I5", "table_acl_removal")
                                        else (r, lab) for r, lab in sorted({(r, lab) for r, lab, *_ in ACT_DB})])
def test_i10_act_changed_db(vdb, row, label):
    """§6A negative (rule 2) when active; the comparison-off fail-first (rule 3) while inactive."""
    ((_, _, sql, code, active_now),) = [x for x in ACT_DB if (x[0], x[1]) == (row, label)]
    statements = [_replace_body(vdb, s.split()[-1]) if s.startswith("__REPLACE_BODY__")
                  else _other_schema_owner(vdb) if s == "__OTHER_SCHEMA_OWNER__" else s for s in sql]
    _super_sql(vdb, *statements)
    report = vrun(vdb)
    unverified = [f["detail"]["components"] for f in found(report, "I10") if f["detail"]["problem"] ==
                  "baseline_unverified"]
    if _row_active(row, active_now):
        if code is None:
            pytest.fail(f"{row}/{label}: active but OD-11 gave no problem code (S13)")
        assert has_i10(report, code), (row, label, i10_problems_of(report))
    else:
        if code is not None:
            assert not has_i10(report, code), (row, label, "detected although the comparison is off")
        assert unverified and _list_text(row) in unverified[0], (row, label, "not reported as unverified")


@needs_a5b
@exclusive_cluster
def test_i10_act_migrator_edge_pristine_db(vdb, xroles):
    """§6A I9 positive (DB-X cluster; no change): the pristine migrator edge is no finding."""
    report = vrun(vdb)
    assert not has_i10(report, "role_edge:hyperchat_migrator->hyperchat_owner") and \
        not has_i10(report, "role_attribute:rolinherit"), i10_problems_of(report)


@needs_a5b
@exclusive_cluster
@pytest.mark.parametrize("case", ["inherit_true", "set_false"])
def test_i10_act_migrator_edge_changed_db(vdb, xroles, case):
    """§6A I9 negatives (DB-X, run under A5b after A7a): PG17 options / PG15 rolinherit, each restored (RT-5)."""
    v = _version(vdb)
    if v >= 160000:
        change = {"inherit_true": "WITH INHERIT TRUE", "set_false": "WITH SET FALSE"}[case]
        xroles.append(_migrator_edge_restore())
        _super_sql(vdb, f"GRANT hyperchat_owner TO hyperchat_migrator {change}", database=False)
        expected = "role_edge:hyperchat_migrator->hyperchat_owner"
    else:
        if case == "set_false":
            pytest.skip("PG15 has no set_option")
        xroles.append("ALTER ROLE hyperchat_migrator NOINHERIT")
        _super_sql(vdb, "ALTER ROLE hyperchat_migrator INHERIT", database=False)
        expected = "role_attribute:rolinherit"
    report = vrun(vdb)
    assert has_i10(report, expected), (case, i10_problems_of(report))
