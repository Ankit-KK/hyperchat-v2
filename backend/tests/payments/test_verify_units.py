"""PAY-15 stage 6a-2: the read-only accounting verifier (app.payments.verify), without a database.

The class table, statuses, exit codes and gates (§9, §9.1, D-7: IR6-T1 to T5), the 24 h window with the clock margin
(C-IR7: IR7-T1 to T7), the run scope (D-10), the canonical report and digest, the SELECT-only construction (STOP-2), and
the 0034 classification rules on synthetic data: the activation commit is never assumed (C-IR2: IR2-T4), W6 by the
state observed under S (C-IR5: IR5-T1, T2, T5, T6), permanent historical ambiguity (C-IR4: IR4-T1, T2), I12 and R-DM."""

import ast
import random
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.payments import accounting, verify
from app.payments.verify import (
    EXIT_CODES, MARGIN, R_MAX, SEVERITY, WINDOW, Finding, Refused, Verifier, active_class, canonical, gates,
    parse_scope, refused_report, scope_covers, status_of, window_class,
)

SOURCE = Path(verify.__file__).read_text(encoding="utf-8")
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
DID = "8d0c6a2e-1111-4a5b-9c3d-000000000001"
SID = "8d0c6a2e-2222-4a5b-9c3d-000000000001"
PAY = "pay_TESTverify01"


# ── §9.1: the closed class table, statuses, exit codes, gates (IR-6) ───────────────────────────────────────────────

def test_the_exit_codes_are_the_approved_d7_contract():
    assert EXIT_CODES == {"clean": 0, "violation": 1, "refused": 2, "attention": 3}


def test_every_class_has_exactly_one_6a2_severity():
    assert set(SEVERITY.values()) <= {"violation", "attention", "record", "processed"}
    assert {c for c, s in SEVERITY.items() if s == "attention"} >= {
        "deadline_uncertain", "historical_ambiguous", "legacy_uncertain", "pending_reconciliation",
        "awaiting_reconciliation", "in_flight", "boundary_uncertain", "deleted_creator_with_accounting_history"}
    for absent in ("missed", "late_proven", "timeliness_uncertain", "epoch_not_rotated", "on_time_proven"):
        assert absent not in SEVERITY  # 0035 rows: added by the hardening PR, never emitted by 6a-2


CLASS_RETURNING = ("timing", "window_class", "active_class")


def _strings(node) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _strings(node.body) | _strings(node.orelse)
    if isinstance(node, ast.Tuple) and node.elts:
        return _strings(node.elts[0])
    return set()


def _emitted_literals() -> set[str]:
    """IR6-T3: every class literal the classifier can emit, read from the module's code: the class argument of
    every Verifier.add() call, plus the classes the timing functions return."""
    out = set()
    tree = ast.parse(SOURCE)
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "add" and len(node.args) >= 2:
                if fn.name == "finish_timing":  # its class is timing()'s return value, collected below
                    assert isinstance(node.args[1], ast.Name) and node.args[1].id == "cls"
                    continue
                assert isinstance(node.args[1], ast.Constant), f"{fn.name}: every emitted class is a literal"
                out.add(node.args[1].value)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in CLASS_RETURNING:
            for ret in ast.walk(node):
                if isinstance(ret, ast.Return):
                    out |= _strings(ret.value)
    return out


def test_ir6_t3_the_emitted_class_literals_equal_the_table():
    emitted = _emitted_literals()
    assert emitted == set(SEVERITY), (emitted ^ set(SEVERITY))


@pytest.mark.parametrize("cls", sorted(SEVERITY))
def test_ir6_t1_each_class_alone_gives_its_status_exit_and_gates(cls):
    status = status_of({cls: 1})
    severity = SEVERITY[cls]
    assert status == {"processed": "clean", "attention": "attention", "violation": "violation"}[severity]
    assert EXIT_CODES[status] == {"clean": 0, "attention": 3, "violation": 1}[status]
    g = gates(status, True, "active", {cls: 1})
    assert g["activation_verification_ready"] == (status == "clean")
    assert g["activation_complete"] is False  # CR-1: the 6a-3 condition is never attested by verify
    assert g["reconcile_precondition"] == (severity != "violation")
    assert gates(status, False, "active", {cls: 1}) == dict.fromkeys(g, False)  # scoped: every gate false
    assert gates(status, True, "frozen", {cls: 1}) == dict.fromkeys(g, False)   # not active: every gate false


def test_ir6_t2_status_is_the_maximum_severity_and_only_processed_can_complete():
    rng = random.Random(20261115)
    classes = sorted(SEVERITY)
    for _ in range(2000):
        bag = {c: rng.randint(1, 3) for c in rng.sample(classes, rng.randint(0, 6))}
        status = status_of(bag)
        worst = max((verify.RANK[SEVERITY[c]] for c in bag), default=0)
        assert status == verify.STATUS_OF[worst]
        g = gates(status, True, "active", bag)
        ready = g["activation_verification_ready"]
        assert ready == all(SEVERITY[c] in ("processed", "record") for c in bag)
        assert not ready or status == "clean"  # the gate invariant
        assert g["activation_complete"] is False  # CR-1: never certified by a verify report


def test_fc3_the_limitations_disclose_decision_5_and_never_claim_a_false_pass_is_impossible():
    assert [x.split(":")[0] for x in verify.LIMITATIONS] == ["L-1", "L-2", "L-2b", "D5-1", "D5-2", "D5-3", "D5-4",
                                                              "D5-5"]
    text = " ".join(verify.LIMITATIONS)
    for needle in ("commit order cannot be reconstructed", "not that the evidence state existed",
                   "a false historical pass is possible", "remain undetected by I12-H", "do not guarantee",
                   "no 0034 keep acknowledgement can be proven", "0035 cannot retroactively prove",
                   "enumeration is bounded", "uncertain, never exact and never forged"):
        assert needle in text, needle
    for superseded in ("never a pass", "can only produce", "evidence cuts within"):  # the r6 timestamp-cut claim
        assert superseded not in text, superseded


def test_ir6_t4_an_unknown_class_refuses_with_every_gate_false():
    with pytest.raises(Refused):
        status_of({"processed": 3, "brand_new_class": 1})
    report = refused_report("unknown class")
    assert report["status"] == "refused" and report["exit_code"] == 2 and not any(report["gates"].values())
    assert set(report["gates"]) == set(verify.GATE_KEYS)  # a refused report carries every gate, all false
    assert report["findings"] == [] and len(report["digest"]) == 64


def test_ir6_t5_a_scoped_clean_run_is_clean_but_every_gate_is_false():
    status = status_of({"processed": 4})
    assert status == "clean"
    assert gates(status, False, "active", {"processed": 4}) == dict.fromkeys(verify.GATE_KEYS, False)


def test_cr1_a_full_scope_active_clean_report_is_verification_ready_but_never_activation_complete():
    g = gates("clean", True, "active", {"processed": 9})
    assert g == {"reconcile_precondition": True, "recovery_precondition": True,
                 "activation_verification_ready": True, "activation_complete": False}
    for counts in ({"deadline_uncertain": 1}, {"historical_ambiguous": 1}, {"structural": 1}):
        g = gates(status_of(counts), True, "active", counts)
        assert not g["activation_verification_ready"] and not g["activation_complete"]
    assert verify.NOT_ATTESTED == ("the 6a-3 second report-only reconcile plans zero changes (plan §4.3)",)


def test_cr1_no_report_can_activate_accounting():
    """The verifier's executable SQL calls no state or owner function, and nothing in the module writes."""
    sql = "\n".join(v for n, v in vars(verify).items() if n.isupper() and isinstance(v, str) and n != "VERIFIER_VERSION")
    sql = re.sub(r"'[^']*'", "''", sql)
    for fn in ("payment_accounting_set_state", "payment_accounting_reconcile", "payment_accounting_keep",
               "payment_accounting_revert", "payment_accounting_apply_evaluation", "payment_accounting_open_run",
               "donation_accounting_apply", "donation_accounting_core", "payment_accounting_lock_state"):
        assert fn + "(" not in sql, fn


def test_attention_never_exits_zero():
    for cls, sev in SEVERITY.items():
        if sev == "attention":
            assert EXIT_CODES[status_of({cls: 1, "processed": 5})] == 3


# ── C-IR7: the 24 h window with ε = r_max × L (IR7-T1 to T7) ─────────────────────────────────────────────────────

def eps(interval: timedelta) -> timedelta:
    return timedelta(seconds=float(Decimal(str(interval.total_seconds())) * R_MAX))


def test_the_margin_is_500_ppm_about_43_seconds_over_a_day():
    assert R_MAX == Decimal("0.0005") and abs(eps(WINDOW).total_seconds() - 43.2) < 1e-9
    assert MARGIN == timedelta(minutes=5)  # D-15


@pytest.mark.parametrize("commit_proven", [False, True])
def test_ir7_t1_t2_pending_only_when_provably_inside(commit_proven):
    e = eps(WINDOW)
    assert window_class(WINDOW - e / 2, commit_proven) == "deadline_uncertain"   # T1: the error interval crosses
    assert window_class(WINDOW - 2 * e, commit_proven) == "pending_reconciliation"  # T2


def test_ir7_t3_t4_overdue_only_with_a_proven_commit_and_beyond_the_margin():
    e = eps(WINDOW)
    assert window_class(WINDOW + e / 2, True) == "deadline_uncertain"            # T3
    assert window_class(WINDOW + 2 * e, True) == "overdue_reconciliation"        # T4 (0035 only)


@pytest.mark.parametrize("true_seconds, scale", [(86401, Decimal(1) - R_MAX),   # T6: slow clock, really late
                                                 (86399, Decimal(1) + R_MAX)])  # T5: fast clock, really in time
def test_ir7_t5_t6_drift_in_either_direction_never_proves_the_wrong_side(true_seconds, scale):
    measured = timedelta(seconds=float(Decimal(true_seconds) * scale))
    assert window_class(measured, True) == "deadline_uncertain"


@pytest.mark.parametrize("scale", [Decimal(1) - R_MAX, Decimal(1), Decimal(1) + R_MAX])
def test_ir7_drifted_measurements_never_contradict_the_truth(scale):
    """For every true interval and every admissible drift, a proven class agrees with the true interval."""
    for true in range(86400 - 200, 86400 + 200, 3):
        cls = window_class(timedelta(seconds=float(Decimal(true) * scale)), True)
        if cls == "pending_reconciliation":
            assert true <= 86400
        if cls == "overdue_reconciliation":
            assert true > 86400


@pytest.mark.parametrize("elapsed", [WINDOW - 2 * eps(WINDOW), WINDOW - eps(WINDOW) / 2, WINDOW,
                                     WINDOW + 2 * eps(WINDOW), timedelta(days=30)])
def test_ir7_t7_and_ir2_on_0034_overdue_is_never_asserted(elapsed):
    assert window_class(elapsed, commit_proven=False) in ("pending_reconciliation", "deadline_uncertain")


def test_w1_without_a_proven_commit_is_never_a_miss():
    assert active_class(timedelta(minutes=9), False) == "in_flight"
    assert active_class(timedelta(minutes=20), False) == "boundary_uncertain"
    assert active_class(timedelta(minutes=20), True) == "missed_evaluation"


# ── D-10 scope, canonical report, STOP-2 ─────────────────────────────────────────────────────────────────────────

def scope_of(at, kind="donations", **kw):
    """A rev. 3 §6 scope the 6a-3 CLI would write, expiring 23 h after `at`."""
    base = {"kind": kind, "expires_at": (at + timedelta(hours=23)).isoformat()}
    base.update({"donations": [DID]} if kind in ("donations", "recovery") else {})
    base.update({"creator": SID} if kind == "creator" else {})
    base.update({"verify_report_sha256": "a" * 64} if kind == "recovery" else {})
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def run_row(i, command, at, scope=None):
    return {"id": i, "command": command, "scope": scope_of(at) if scope is None else scope, "created_at": at}


@pytest.mark.parametrize("kind", ["donations", "creator", "all_v2", "recovery"])
def test_s_valid_scopes_of_every_kind(kind):
    status, info = parse_scope(scope_of(T0, kind), T0)
    assert status == "valid" and info["kind"] == kind


@pytest.mark.parametrize("scope", [{}, {"donations": [DID]}, {"creator": SID, "expires_at": "x"}])
def test_s1_a_scope_without_a_kind_is_unscoped(scope):
    assert parse_scope(scope, T0) == ("unscoped", None)


@pytest.mark.parametrize("defect", [
    "not_an_object", "unknown_kind", "unknown_key", "missing_list", "wrong_type", "invalid_uuid", "empty_list",
    "duplicates", "donations_with_creator", "expires_missing", "expires_unparsable", "expires_naive",
    "expires_before_created", "expires_after_24h", "creator_invalid", "recovery_digest_invalid"])
def test_s4_every_malformed_scope_defect_is_malformed(defect):
    s = scope_of(T0)
    scope = {
        "not_an_object": [DID], "unknown_kind": {**s, "kind": "everything"}, "unknown_key": {**s, "note": "x"},
        "missing_list": {k: v for k, v in s.items() if k != "donations"}, "wrong_type": {**s, "donations": DID},
        "invalid_uuid": {**s, "donations": ["nope"]}, "empty_list": {**s, "donations": []},
        "duplicates": {**s, "donations": [DID, DID.upper()]}, "donations_with_creator": {**s, "creator": SID},
        "expires_missing": {k: v for k, v in s.items() if k != "expires_at"},
        "expires_unparsable": {**s, "expires_at": "tomorrow"},
        "expires_naive": {**s, "expires_at": "2026-11-01T13:00:00"},
        "expires_before_created": {**s, "expires_at": (T0 - timedelta(seconds=1)).isoformat()},
        "expires_after_24h": {**s, "expires_at": (T0 + timedelta(hours=24, seconds=1)).isoformat()},
        "creator_invalid": {**scope_of(T0, "creator"), "creator": "x"},
        "recovery_digest_invalid": {**scope_of(T0, "recovery"), "verify_report_sha256": "A" * 64},
    }[defect]
    assert parse_scope(scope, T0) == ("malformed", None)


def test_s5_s6_s8_s9_s10_scope_coverage():
    def cov(kind, command="resolve-keep", did=DID, sid=SID, v2=True, at=T0 + timedelta(hours=1), **kw):
        return scope_covers(parse_scope(scope_of(T0, kind, **kw), T0)[1], command, did, sid, v2, at)
    other = "8d0c6a2e-1111-4a5b-9c3d-000000000099"
    assert cov("donations") and not cov("donations", did=other)                              # S5
    assert not cov("donations", at=T0 + timedelta(hours=23, seconds=1))                      # S6: after expiry
    assert cov("creator") and not cov("creator", sid="8d0c6a2e-2222-4a5b-9c3d-000000000099")  # S8
    assert cov("all_v2", "reconcile") and not cov("all_v2", "resolve-keep") \
        and not cov("all_v2", "reconcile", v2=False)                                         # S9
    assert cov("recovery", "reconcile") and not cov("recovery", "reconcile", did=other)     # S10: the extras


def test_the_canonical_json_is_deterministic_and_the_digest_covers_everything():
    a = {"b": [Decimal("1.50"), T0], "a": {"z": 1, "y": None}}
    assert canonical(a) == canonical({"a": {"y": None, "z": 1}, "b": [Decimal("1.50"), T0]})
    assert canonical(a) == '{"a":{"y":null,"z":1},"b":["1.50","2026-11-01T12:00:00+00:00"]}'
    r1, r2 = verify.finish({"status": "clean", "x": 1}), verify.finish({"status": "clean", "x": 2})
    assert r1["digest"] != r2["digest"] and verify.finish(dict(r1))["digest"] == r1["digest"]


def test_every_module_sql_statement_is_a_select():
    for name, value in vars(verify).items():
        if name.isupper() and isinstance(value, str) and re.search(r"\bFROM\b|\bSELECT\b", value):
            assert value.lstrip().split(None, 1)[0].upper() in ("SELECT", "WITH"), name
    banned = re.compile(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|CREATE|ALTER|DROP|GRANT|REVOKE|COPY|LOCK|"
                        r"pg_advisory\w*|TEMP(ORARY)?|set_config|nextval|setval)\b", re.I)
    sql = "\n".join(v for n, v in vars(verify).items() if n.isupper() and isinstance(v, str))
    sql = re.sub(r"'[^']*'", "''", sql)  # privilege names inside string literals are data, not statements
    assert not banned.search(sql), banned.search(sql)


def test_the_select_guard_refuses_anything_else():
    class Cur:
        def execute(self, *a):  # pragma: no cover - never reached
            raise AssertionError("executed")
    for sql in ("UPDATE public.donations SET x = 1", "  insert into t values (1)", "DELETE FROM t", "CALL p()"):
        with pytest.raises(Refused):
            verify._select(Cur(), sql)


def test_no_0035_object_or_definer_function_is_read():
    for name in ("observation", "epoch", "receipt", "obligation", "attestation", "payment_evidence_fp(",
                 "donation_accounting_core(", "payment_accounting_lock_state("):
        assert name not in SOURCE.split("# ── the SQL")[1].split("def _select")[0], name


# ── synthetic 0034 data: the classification rules ─────────────────────────────────────────────────────────────────

def hist(i, cause, before, after, hold_before=None, hold_after=None, refs=None, at=T0, run=None, rev=True,
         credited=(Decimal("0.00"), Decimal("0.00")), txid=None):
    return {"id": i, "donation_id": DID, "streamer_id": SID, "donator_name_lower": "fan", "cause": cause,
            "credited_before": credited[0], "credited_after": credited[1], "delta_inr": credited[1] - credited[0],
            "delta_count": 0, "total_after": None, "created_at": at,
            "reversed_before_paise": before if rev else None, "reversed_after_paise": after if rev else None,
            "hold_before": hold_before, "hold_after": hold_after, "evidence_refs": refs, "txid": txid or 100 + i,
            "run_id": run}


def refund(ref="rfnd_A", amount=2500, status="processed"):
    return {"kind": "refund", "provider_ref": ref, "provider_payment_id": PAY, "donation_id": DID,
            "match_type": "payment", "phase": None, "status": status, "amount_paise": amount, "currency": "INR",
            "amount_deducted_paise": None, "conflicting": False}


def lost(ref="disp_A", deducted=4000):
    return {"kind": "dispute", "provider_ref": ref, "provider_payment_id": PAY, "donation_id": DID,
            "match_type": "payment", "phase": "chargeback", "status": "lost", "amount_paise": 10000,
            "currency": "INR", "amount_deducted_paise": deducted, "conflicting": False}


def event(i, f, t, at, fm="hold", tm="hold"):
    return {"id": i, "from_state": f, "to_state": t, "from_mode": fm, "to_mode": tm, "created_at": at}


def make(state="active", mode="apply", *, current=(), reversal=None, ev=None, events=(), history=(),
         evidence=(), runs=None, fp_now="FP", fp_other="FP-OTHER", c_v=None, cut_rows=None, fp_at=None):
    v = Verifier(None, {"c_v": c_v or T0 + timedelta(hours=1), "t_v": T0, "snapshot": "1:1:", "database": "x",
                        "head": "0034", "server_version_num": 170000}, "full", None)
    v.donations = {DID: {"id": DID, "streamer_id": SID, "name_lower": "fan", "amount": Decimal("100.00"),
                         "amount_inr": Decimal("100.00"), "creditable_inr": None, "credited_inr": Decimal("100.00"),
                         "currency": "INR", "payment_status": "success", "moderation_status": "approved",
                         "provider_payment_id": PAY, "legacy_source_table": None, "payment_account": "primary",
                         "provider_order_id": "order_TEST", "v2": True}}
    v.streamers, v.totals = {SID}, {}
    v.history = list(history)
    v.by_donation = {DID: v.history} if history else {}
    v.reversals = {DID: {"donation_id": DID, "captured_paise": 10000, **reversal}} if reversal else {}
    v.current = {DID: list(current)} if current else {}
    v.evidence = {DID: [{"donation_id": DID, "provider_ref": r, "recorded_at": t,
                         "kind": "dispute" if r.startswith("disp") else "refund"} for r, t in evidence]} \
        if evidence else {}
    v.evals = {DID: {"donation_id": DID, "outcome": "x", "ack_fp": None, "ack_run": None, **ev}} if ev else {}
    v.state, v.mode, v.events = state, mode, list(events)
    v.runs = runs or {}
    other = "hold" if mode == "apply" else "apply"
    v.fp = {mode: {DID: fp_now}, other: {DID: fp_other}}
    v.valid_runs = {rid: parse_scope(r["scope"], r["created_at"])[1] for rid, r in v.runs.items()
                    if parse_scope(r["scope"], r["created_at"])[0] == "valid"}
    v.cut_rows = cut_rows or (lambda did, cut: list(current))
    v.fp_at = fp_at or (lambda did, cut, m: fp_now)
    v.evidence_cuts = lambda did: ["-infinity", *sorted({t for _, t in evidence}), "infinity"]
    # Decision 5 seams, synthetic: the versions are the rows any synthetic state shows; the conceivable
    # fingerprints are fp_at over the synthetic states (ref_states itself is tested directly below)
    states = [*sorted({t for _, t in evidence}), T0 - timedelta(days=30), T0 + timedelta(days=30)]

    def versions(did):
        seen = {}
        for c in states:
            for r in v.cut_rows(did, c):
                seen.setdefault(tuple(sorted(r.items())), dict(r))
        return list(seen.values())
    v.versions = versions
    v.conceivable_fps = lambda did, modes: {v.fp_at(did, c, m) for c in states for m in modes}
    return v


def classes(v) -> list[tuple]:
    v.classify()
    return sorted((f.invariant, f.cls, f.detail.get("problem")) for f in v.findings)


ACTIVATED = [event(1, "off", "active", T0 - timedelta(days=2))]


def test_a_consistent_clear_reversal_observed_while_active_is_processed():
    v = make(current=[refund()], reversal={"applied": 2500, "hold": None, "flags": []},
             ev={"evidence_fp": "FP", "evaluated_at": T0, "state": "active", "dispute_mode": "apply"},
             events=ACTIVATED, history=[hist(1, "evaluation", 0, 2500, refs=["rfnd_A"])])
    assert classes(v) == [("I13", "processed", None)]


def test_ir5_t1_an_evaluation_that_started_before_the_activation_and_waited_is_reconciled():
    """CE-2 on W6: evaluated_at (its transaction start) precedes A.created_at, but it observed 'active' under S."""
    a = T0 - timedelta(hours=3)
    v = make(current=[refund()], reversal={"applied": 2500, "hold": None, "flags": []},
             ev={"evidence_fp": "FP", "evaluated_at": a - timedelta(minutes=30), "state": "active",
                 "dispute_mode": "apply"},
             events=[event(1, "off", "frozen", a - timedelta(days=1)), event(2, "frozen", "active", a)],
             history=[hist(1, "activation", 0, 2500, refs=["rfnd_A"], at=a - timedelta(minutes=30), run=9)],
             runs={9: run_row(9, "reconcile", a)})
    assert classes(v) == [("I13", "processed", None)]


def test_ir5_t2_a_keep_while_frozen_during_a_long_activation_is_not_reconciled():
    """A keep's evaluated_at may follow A.created_at although no evaluation ran after A committed: ev.state says
    frozen, so W6 does not hold (the old evaluated_at >= t_W rule called it reconciled)."""
    a = T0 - timedelta(hours=2)
    keep = hist(2, "manual", 0, 0, hold_before="H-DISPUTE-MODE", at=a + timedelta(minutes=30), run=7)
    v = make(mode="hold", current=[lost()], reversal={"applied": 0, "hold": None, "flags": []},
             ev={"evidence_fp": "FP", "evaluated_at": a + timedelta(minutes=30), "state": "frozen",
                 "dispute_mode": "hold", "ack_fp": "FP", "ack_run": 7},
             events=[event(1, "frozen", "active", a)],
             history=[hist(1, "hold", 0, 0, hold_after="H-DISPUTE-MODE", at=a - timedelta(days=1)), keep],
             runs={7: run_row(7, "resolve-keep", a)})
    got = classes(v)
    assert ("I13", "processed", None) not in got
    assert got == [("I12", "historical_ambiguous", None)]  # A1: the keep is unproven, never reconciled by it


def test_ir5_t5_a_revert_while_frozen_needs_reconciliation_after_reactivation():
    a = T0 - timedelta(hours=1)
    rows = [hist(1, "evaluation", 0, 2500, refs=["rfnd_A"], at=a - timedelta(days=2)),
            hist(2, "revert", 2500, 0, at=a - timedelta(days=1), run=5)]
    run = {5: run_row(5, "revert", a)}
    ev = {"evidence_fp": "FP", "evaluated_at": a - timedelta(days=1), "state": "active", "dispute_mode": "apply"}
    frozen = make(state="frozen", current=[refund()], reversal={"applied": 0, "hold": None, "flags": []}, ev=ev,
                  history=rows, runs=run, events=[event(1, "active", "frozen", a - timedelta(days=1, hours=1))])
    assert classes(frozen) == [("I13", "processed", None)]  # D-3: the audited revert agrees while frozen
    reactivated = make(current=[refund()], reversal={"applied": 0, "hold": None, "flags": []}, ev=ev, history=rows,
                       runs=run, events=[event(1, "active", "frozen", a - timedelta(days=1, hours=1)),
                                         event(2, "frozen", "active", a)])
    got = classes(reactivated)
    assert got[0][1] in ("pending_reconciliation", "boundary_uncertain") and ("I13", "processed", None) not in got


@pytest.mark.parametrize("shift", [timedelta(days=-30), timedelta(0), timedelta(days=30)])
def test_ir5_t6_evaluated_at_never_changes_the_reconciliation_result(shift):
    v = make(current=[refund()], reversal={"applied": 2500, "hold": None, "flags": []},
             ev={"evidence_fp": "FP", "evaluated_at": T0 + shift, "state": "active", "dispute_mode": "apply"},
             events=ACTIVATED, history=[hist(1, "evaluation", 0, 2500, refs=["rfnd_A"])])
    assert classes(v) == [("I13", "processed", None)]


def test_ir2_t4_a_long_uncommitted_activation_never_yields_missed_or_overdue():
    """0034 data cannot bound the activation's commit: an uncovered origin after A.created_at is boundary_uncertain
    (never missed) and a backlog item past the nominal window is deadline_uncertain (never overdue)."""
    a = T0 - timedelta(days=3)
    after = make(current=[refund()], ev={"evidence_fp": "OLD", "evaluated_at": a - timedelta(days=1),
                                         "state": "frozen", "dispute_mode": "apply"},
                 events=[event(1, "off", "frozen", a - timedelta(days=5)), event(2, "frozen", "active", a)],
                 evidence=[("rfnd_A", a + timedelta(hours=1))])
    assert classes(after) == [("I13", "boundary_uncertain", None)]
    before = make(current=[refund()], ev={"evidence_fp": "OLD", "evaluated_at": a - timedelta(days=2),
                                          "state": "frozen", "dispute_mode": "apply"},
                  events=[event(1, "off", "frozen", a - timedelta(days=5)), event(2, "frozen", "active", a)],
                  evidence=[("rfnd_A", a - timedelta(days=1))])
    assert classes(before) == [("I13", "deadline_uncertain", None)]
    for v in (after, before):
        assert not {f.cls for f in v.findings} & {"missed_evaluation", "overdue_reconciliation"}


def test_m1_the_oldest_uncovered_evidence_is_the_origin_not_the_newest():
    a = T0 - timedelta(days=1)
    v = make(current=[refund("rfnd_A"), refund("rfnd_B", 100)], c_v=T0,
             ev={"evidence_fp": "OLD", "evaluated_at": T0 - timedelta(hours=2), "state": "active",
                 "dispute_mode": "apply"},
             events=[event(1, "off", "active", a)],
             evidence=[("rfnd_A", T0 - timedelta(minutes=20)), ("rfnd_B", T0 - timedelta(minutes=1))])
    v.classify()
    (f,) = v.findings
    assert f.cls == "boundary_uncertain" and f.detail["origin"] == T0 - timedelta(minutes=20)  # not in_flight


def test_m4_the_first_activation_governs_and_a_later_one_does_not_reset_the_window():
    a1, a2 = T0 - timedelta(hours=23), T0 - timedelta(hours=1)
    v = make(current=[refund()], c_v=T0, ev={"evidence_fp": "OLD", "evaluated_at": a1 - timedelta(days=2),
                                             "state": "frozen", "dispute_mode": "apply"},
             events=[event(1, "off", "frozen", a1 - timedelta(days=3)), event(2, "frozen", "active", a1),
                     event(3, "active", "frozen", a1 + timedelta(hours=2)), event(4, "frozen", "active", a2)],
             evidence=[("rfnd_A", a1 - timedelta(days=1))])
    v.classify()
    (f,) = v.findings
    assert f.cls == "pending_reconciliation" and f.detail["activation_event"] == 2  # D-9 / D-12
    late = make(current=[refund()], c_v=a1 + WINDOW + timedelta(hours=1),
                ev={"evidence_fp": "OLD", "evaluated_at": a1 - timedelta(days=2), "state": "frozen",
                    "dispute_mode": "apply"},
                events=v.events, evidence=[("rfnd_A", a1 - timedelta(days=1))])
    late.classify()
    assert [f.cls for f in late.findings] == ["deadline_uncertain"]  # never overdue on 0034


def test_frozen_or_off_backlog_is_awaiting():
    v = make(state="frozen", current=[refund()], ev=None, events=[event(1, "off", "frozen", T0 - timedelta(days=4))],
             evidence=[("rfnd_A", T0 - timedelta(days=1))])
    assert classes(v) == [("I13", "awaiting_reconciliation", None)]


def test_an_origin_within_the_margin_of_a_transition_is_boundary_uncertain():
    a = T0 - timedelta(hours=5)
    v = make(current=[refund()], ev=None, events=[event(1, "off", "active", a)],
             evidence=[("rfnd_A", a - timedelta(minutes=3))])
    assert classes(v) == [("I13", "boundary_uncertain", None)]


def test_no_computable_origin_is_indeterminate():
    v = make(current=[refund()], reversal={"applied": 0, "hold": None, "flags": []}, events=ACTIVATED,
             ev={"evidence_fp": "OLD", "evaluated_at": T0, "state": "active", "dispute_mode": "apply"}, evidence=[])
    assert classes(v) == [("I13", "indeterminate", None)]


def test_m6_mode_only_change_is_an_o_mode_backlog():
    a = T0 - timedelta(hours=2)
    v = make(current=[refund()], reversal={"applied": 2500, "hold": None, "flags": []}, fp_now="FP-NEW-MODE",
             fp_other="FP", ev={"evidence_fp": "FP", "evaluated_at": a - timedelta(days=2), "state": "active",
                                "dispute_mode": "hold"},
             events=[event(1, "active", "frozen", a - timedelta(days=1), "hold", "hold"),
                     event(2, "frozen", "frozen", a - timedelta(hours=20), "hold", "apply"),
                     event(3, "frozen", "active", a, "apply", "apply")],
             history=[hist(1, "evaluation", 0, 2500, refs=["rfnd_A"], at=a - timedelta(days=2))],
             evidence=[("rfnd_A", a - timedelta(days=3))])
    v.classify()
    (f,) = [f for f in v.findings if f.invariant == "I13"]
    assert f.cls == "pending_reconciliation" and f.detail["origin_kind"] == "o_mode"


def test_m7_altered_evaluation_metadata_is_audit_integrity():
    v = make(current=[refund()], reversal={"applied": 2500, "hold": None, "flags": []}, events=ACTIVATED,
             ev={"evidence_fp": "FP", "evaluated_at": T0, "state": "active", "dispute_mode": "hold"},
             history=[hist(1, "evaluation", 0, 2500, refs=["rfnd_A"])])
    assert ("I12", "audit_integrity", "evaluation_metadata_inconsistent") in classes(v)


def test_m11_a_refunds_only_hold_with_a_valid_frozen_value_is_processed():
    rows = [hist(1, "evaluation", 0, 2500, refs=["rfnd_A"], at=T0 - timedelta(hours=3)),
            hist(2, "hold", 2500, 2500, hold_after="H-DISPUTE-MODE", at=T0 - timedelta(hours=2))]
    v = make(mode="hold", current=[refund(), lost()], reversal={"applied": 2500, "hold": "H-DISPUTE-MODE",
                                                                  "flags": ["F-BOTH"]},
             ev={"evidence_fp": "FP", "evaluated_at": T0, "state": "active", "dispute_mode": "hold"},
             events=[event(1, "off", "active", T0 - timedelta(days=1))], history=rows,
             cut_rows=lambda d, c: [refund()] if c < T0 - timedelta(hours=2) else [refund(), lost()])
    assert classes(v) == [("I13", "processed", None)]


def test_m13_a_changed_hold_reason_on_the_same_evidence_is_structural():
    v = make(current=[{**lost(), "conflicting": True}], events=ACTIVATED,
             reversal={"applied": 0, "hold": "H-NO-DEDUCTION", "flags": []},
             ev={"evidence_fp": "FP", "evaluated_at": T0, "state": "active", "dispute_mode": "apply"},
             history=[hist(1, "hold", 0, 0, hold_after="H-NO-DEDUCTION")])
    assert ("I12", "structural", "hold_disagreement") in classes(v)


def test_a_different_clear_amount_on_the_same_evidence_is_evaluation_disagreement():
    v = make(current=[refund()], reversal={"applied": 2000, "hold": None, "flags": []}, events=ACTIVATED,
             ev={"evidence_fp": "FP", "evaluated_at": T0, "state": "active", "dispute_mode": "apply"})
    assert classes(v) == [("I12", "structural", "evaluation_disagreement")]


def test_m12_an_arbitrary_amount_no_evidence_cut_supports_is_unsupported_and_takes_precedence():
    v = make(current=[refund()], reversal={"applied": 2000, "hold": None, "flags": []}, events=ACTIVATED,
             ev={"evidence_fp": "FP", "evaluated_at": T0, "state": "active", "dispute_mode": "apply"},
             history=[hist(1, "evaluation", 0, 2000, refs=["rfnd_A"])], cut_rows=lambda d, c: [refund()])
    assert classes(v) == [("I12-H", "structural", "unsupported_amount")]  # no timing class on a violated donation


def test_rdm_a_dispute_mode_hold_without_a_loss():
    rows = [hist(1, "hold", 0, 0, hold_after="H-DISPUTE-MODE")]
    base = dict(current=[refund(status="pending")], reversal={"applied": 0, "hold": "H-DISPUTE-MODE", "flags": []},
                history=rows, mode="hold")
    assert classes(make(state="frozen", ev={"evidence_fp": "FP", "evaluated_at": T0, "state": "active",
                                             "dispute_mode": "hold"}, **base)) == \
        [("I11", "awaiting_reconciliation", None)]                                       # F-15
    assert classes(make(events=ACTIVATED, ev={"evidence_fp": "FP", "evaluated_at": T0, "state": "active",
                                              "dispute_mode": "hold"}, **base)) == [("I11", "stale_hold", None)]
    attributable = make(events=ACTIVATED, ev={"evidence_fp": "OLD", "evaluated_at": T0 - timedelta(hours=1),
                                              "state": "active", "dispute_mode": "hold"},
                        evidence=[("rfnd_A", T0 - timedelta(minutes=50))], **base)
    assert classes(attributable)[0][1] in ("boundary_uncertain", "in_flight")  # the origin's class


def test_f12_a_held_donation_reports_its_unapplied_refunds():
    v = make(mode="hold", current=[lost(), refund(amount=3000)], events=ACTIVATED,
             reversal={"applied": 0, "hold": "H-DISPUTE-MODE", "flags": []},
             ev={"evidence_fp": "OLD", "evaluated_at": T0, "state": "active", "dispute_mode": "hold"},
             history=[hist(1, "hold", 0, 0, hold_after="H-DISPUTE-MODE")], evidence=[("rfnd_A", T0)])
    v.classify()
    (f,) = [f for f in v.findings if f.invariant == "I13"]
    assert f.detail["processed_refunds_paise"] == 3000 and f.detail["unapplied_refunds_paise"] == 3000


def test_ir4_t1_t2_historical_ambiguity_is_permanent_and_reports_candidates_and_impact():
    """The I12-H counterexample: 1000 then a corrected 2000 within M; the row stored 2000. A later correct
    reconcile row does not supersede it: still historical_ambiguous, activation blocked, reconcile not blocked."""
    t = T0 - timedelta(hours=1)
    states = {t - MARGIN: [refund(amount=1000)]}

    def cut_rows(did, cut):
        return [refund(amount=1000)] if cut < t else [refund(amount=2000)]
    rows = [hist(1, "evaluation", 0, 2000, refs=["rfnd_A"], at=t),
            hist(2, "activation", 2000, 2000, refs=["rfnd_A"], at=T0 - timedelta(minutes=1), run=4)]
    del states
    v = make(current=[refund(amount=2000)], reversal={"applied": 2000, "hold": None, "flags": []},
             ev={"evidence_fp": "FP", "evaluated_at": T0, "state": "active", "dispute_mode": "apply"},
             events=ACTIVATED, history=rows, evidence=[("rfnd_A", t - timedelta(minutes=1)),
                                                       ("rfnd_A", t + timedelta(minutes=2))],
             runs={4: run_row(4, "reconcile", T0)},
             cut_rows=cut_rows)
    v.classify()
    amb = [f for f in v.findings if f.cls == "historical_ambiguous"]
    # Decision 5: the corrected version's commit order is unknowable, so the later 2000 row may have seen either
    # version too; both stay ambiguous (permanent), neither is superseded
    assert [f.entity_id for f in amb] == ["1", "2"] and all(f.detail["basis"] == "evidence" for f in amb)
    assert amb[0].detail["candidates"] == [1000, 2000] and amb[0].detail["max_impact_paise"] == 1000
    counts = {f.cls: 1 for f in v.findings}
    status = status_of(counts)
    assert status == "attention"
    g = gates(status, True, "active", counts)
    assert not g["activation_complete"] and g["reconcile_precondition"]


def test_violations_take_precedence_over_timing_classes():
    v = make(current=[refund()], reversal={"applied": 2500, "hold": None, "flags": []}, events=ACTIVATED,
             ev=None, evidence=[("rfnd_A", T0)])
    v.findings.append(Finding("I4", "structural", "history", "1", {"donation": DID}))
    v.classify()
    assert [f.cls for f in v.findings] == ["structural"]


def test_keep_exception_needs_a_valid_scoped_run_and_a_reconstructable_ack():
    rows = [hist(1, "hold", 0, 0, hold_after="H-DISPUTE-MODE", at=T0 - timedelta(hours=2)),
            hist(2, "manual", 0, 0, hold_before="H-DISPUTE-MODE", at=T0 - timedelta(hours=1), run=3)]
    ev = {"evidence_fp": "FP", "evaluated_at": T0 - timedelta(hours=1), "state": "active", "dispute_mode": "hold",
          "ack_fp": "FP", "ack_run": 3}
    good = make(mode="hold", current=[lost()], reversal={"applied": 0, "hold": None, "flags": []}, ev=ev,
                events=ACTIVATED, history=rows,
                runs={3: run_row(3, "resolve-keep", T0)})
    assert classes(good) == [("I12", "historical_ambiguous", None)]  # A1: never a proven exception on 0034
    forged = make(mode="hold", current=[lost()], reversal={"applied": 0, "hold": None, "flags": []}, ev=ev,
                  events=ACTIVATED, history=rows, fp_at=lambda d, c, m: "SOMETHING-ELSE",
                  runs={3: run_row(3, "resolve-keep", T0)})
    assert ("I12", "audit_integrity", "forged_ack") in classes(forged)                      # M14
    unscoped = make(mode="hold", current=[lost()], reversal={"applied": 0, "hold": None, "flags": []}, ev=ev,
                    events=ACTIVATED, history=rows,
                    runs={3: run_row(3, "resolve-keep", T0, scope={})})
    assert ("I13", "processed", None) not in classes(unscoped)  # never an exception without a valid run (D-10)


def test_legacy_rows_outside_every_active_period_are_legacy_uncertain():
    v = make(state="frozen", events=[event(1, "off", "active", T0 - timedelta(days=3)),
                                     event(2, "active", "frozen", T0 - timedelta(days=2))],
             history=[hist(1, "evaluation", 0, 0, refs=[], at=T0 - timedelta(days=1))])
    v.legacy_timeline()
    assert [(f.invariant, f.cls) for f in v.findings] == [("L-2", "legacy_uncertain")]


# ── plan rev. 3 N- and S-series, adapted to revision 10 (C-IR2 / C-IR5 / C-IR7, D-7, CE-1) ───────────────────────

def runs_findings(v) -> list[tuple]:
    v.runs_check()
    return sorted((f.cls, f.detail.get("problem"), f.entity_kind) for f in v.findings)


def test_s2_s3_a_used_unscoped_run_by_history_or_by_an_ack_is_a_violation():
    by_history = make(history=[hist(1, "revert", 0, 0, run=8)], runs={8: run_row(8, "revert", T0, scope={})})
    assert runs_findings(by_history) == [("audit_integrity", "unscoped_used_run", "run")]
    by_ack = make(ev={"evidence_fp": "FP", "evaluated_at": T0, "state": "active", "dispute_mode": "hold",
                      "ack_fp": "FP", "ack_run": 8}, runs={8: run_row(8, "resolve-keep", T0, scope={})})
    assert runs_findings(by_ack) == [("audit_integrity", "unscoped_used_run", "run")]
    unused = make(runs={8: run_row(8, "revert", T0, scope={})})
    assert runs_findings(unused) == [("unscoped_unused_run", None, "run")]                      # S1


def test_s4_a_malformed_scope_is_a_violation_referenced_or_not():
    assert runs_findings(make(runs={8: run_row(8, "revert", T0, scope={"kind": "donations"})})) == \
        [("audit_integrity", "malformed_scope", "run")]


def test_s7_n3_a_command_cause_mismatch_or_a_missing_run_is_audit_integrity():
    wrong = make(history=[hist(1, "revert", 0, 0, run=8)], runs={8: run_row(8, "resolve-keep", T0)})
    assert runs_findings(wrong) == [("audit_integrity", "scope_mismatch", "history")]
    missing = make(history=[hist(1, "revert", 0, 0, run=99)])
    assert runs_findings(missing) == [("audit_integrity", "missing_run", "run")]


def test_s6_a_use_after_expiry_is_a_violation_and_never_an_exception():
    rows = [hist(1, "evaluation", 0, 2500, refs=["rfnd_A"], at=T0 - timedelta(days=2)),
            hist(2, "revert", 2500, 0, at=T0, run=5)]
    run = {5: run_row(5, "revert", T0 - timedelta(days=1))}  # expired 23 h after it was opened, before the revert
    v = make(state="frozen", current=[refund()], reversal={"applied": 0, "hold": None, "flags": []}, history=rows,
             runs=run, ev={"evidence_fp": "FP", "evaluated_at": T0, "state": "active", "dispute_mode": "apply"},
             events=[event(1, "active", "frozen", T0 - timedelta(days=1, hours=1))])
    assert ("audit_integrity", "scope_mismatch", "history") in runs_findings(v)
    v.findings.clear()
    v.classify()
    assert ("I13", "processed", None) not in [(f.invariant, f.cls, f.detail.get("problem")) for f in v.findings]


def test_n4_a_miss_inside_an_earlier_active_period_is_not_erased_by_a_freeze_and_reactivation():
    a1 = T0 - timedelta(days=2)
    v = make(current=[refund()], ev=None, evidence=[("rfnd_A", a1 + timedelta(hours=1))],
             events=[event(1, "off", "active", a1), event(2, "active", "frozen", a1 + timedelta(hours=5)),
                     event(3, "frozen", "active", T0 - timedelta(hours=1))])
    assert classes(v) == [("I13", "boundary_uncertain", None)]  # C-IR2: never a proven miss, never backlog


@pytest.mark.parametrize("structural, recovery", [(False, True), (True, False)])
def test_n5_n6_a_backlog_past_the_window_is_deadline_uncertain_and_recovery_blocks_on_structure(structural,
                                                                                                recovery):
    counts = {"deadline_uncertain": 1, **({"structural": 1} if structural else {})}
    g = gates(status_of(counts), True, "active", counts)
    assert g["recovery_precondition"] == recovery and not g["activation_complete"]


def test_n8_new_evidence_after_reconciliation_is_w1_never_pending():
    v = make(current=[refund(), refund("rfnd_B", 100)], c_v=T0,
             reversal={"applied": 2500, "hold": None, "flags": []},
             ev={"evidence_fp": "OLD", "evaluated_at": T0 - timedelta(hours=3), "state": "active",
                 "dispute_mode": "apply"},
             events=[event(1, "off", "active", T0 - timedelta(days=1))],
             history=[hist(1, "evaluation", 0, 2500, refs=["rfnd_A"], at=T0 - timedelta(hours=3))],
             evidence=[("rfnd_A", T0 - timedelta(hours=4)), ("rfnd_B", T0 - timedelta(minutes=2))],  # A before M
             cut_rows=lambda d, c: [refund()])
    got = [f.cls for f in (v.classify() or v.findings) if f.invariant == "I13"]
    assert got == ["in_flight"] and "pending_reconciliation" not in got


def test_n10_a_mode_change_plus_evidence_inside_an_active_period_follows_w1():
    a = T0 - timedelta(hours=3)
    v = make(current=[refund()], fp_now="FP-NEW", fp_other="FP-OTHER", c_v=T0,
             ev={"evidence_fp": "OLD", "evaluated_at": a - timedelta(days=2), "state": "active",
                 "dispute_mode": "hold"},
             events=[event(1, "active", "frozen", a - timedelta(days=1), "hold", "hold"),
                     event(2, "frozen", "frozen", a - timedelta(hours=20), "hold", "apply"),
                     event(3, "frozen", "active", a, "apply", "apply")],
             evidence=[("rfnd_A", T0 - timedelta(minutes=30))])
    v.classify()
    (f,) = [f for f in v.findings if f.invariant == "I13"]
    # the earliest origin governs: the mode change (backlog) before the active-period evidence
    assert f.cls == "pending_reconciliation" and f.detail["origin_kind"] == "o_mode"
    only_evidence = make(current=[refund()], c_v=T0, ev={"evidence_fp": "OLD", "evaluated_at": a,
                                                         "state": "active", "dispute_mode": "apply"},
                         events=v.events, evidence=[("rfnd_A", T0 - timedelta(minutes=30))])
    only_evidence.classify()
    assert [f.cls for f in only_evidence.findings] == ["boundary_uncertain"]  # W1, never pending


def test_n11_replaced_by_ce1_an_evaluation_row_after_the_freeze_event_is_no_violation():
    f = T0 - timedelta(hours=2)
    events = [event(1, "off", "active", T0 - timedelta(days=1)), event(2, "active", "frozen", f)]
    near = make(state="frozen", events=events, history=[hist(1, "evaluation", 0, 0, refs=[], at=f + timedelta(minutes=2))])
    near.legacy_timeline()
    assert near.findings == []                                     # CE-1: granted S while the freeze waited
    far = make(state="frozen", events=events, history=[hist(1, "evaluation", 0, 0, refs=[], at=f + timedelta(hours=1))])
    far.legacy_timeline()
    assert [x.cls for x in far.findings] == ["legacy_uncertain"]   # attention, never a violation (rev. 3 N11 removed)


@pytest.mark.parametrize("field, value", [("isolation", "read committed"), ("isolation", "serializable"),
                                          ("read_only", "off"), ("session_user", "hyperchat_app"),
                                          ("current_user", "hyperchat_owner"), ("head", "0035"),
                                          ("server_version_num", 140012)])
def test_n15_the_session_guard_refuses_every_unsafe_session(field, value):
    first = {"session_user": "hyperchat_readonly", "current_user": "hyperchat_readonly",
             "isolation": "repeatable read", "read_only": "on", "server_version_num": 170000, "head": "0034"}
    verify._guard(first)  # the safe session passes
    with pytest.raises(Refused):
        verify._guard({**first, field: value})


# ── CR-2: historical dispute mode without commit order (synthetic timings beyond the margin) ──────────────────────

LOSS = [refund(), lost()]  # under apply: clear max(2500, 4000) = 4000; under hold: H-DISPUTE-MODE (no clear amount)


def _apply_then_hold(row_at, hold_activation_at):
    """An apply-mode active period A1, a freeze, a mode change to hold, then a hold-mode activation A2."""
    return [event(1, "off", "active", T0 - timedelta(days=3), "apply", "apply"),
            event(2, "active", "frozen", hold_activation_at - timedelta(hours=1), "apply", "apply"),
            event(3, "frozen", "frozen", hold_activation_at - timedelta(minutes=30), "apply", "hold"),
            event(4, "frozen", "active", hold_activation_at, "hold", "hold")]


def _i12h(events, row_at, stored=4000):
    v = make(mode="hold", current=LOSS, events=events, cut_rows=lambda d, c: LOSS,
             evidence=[("rfnd_A", T0 - timedelta(days=4)), ("disp_A", T0 - timedelta(days=4))],
             history=[hist(1, "evaluation", 0, stored, refs=["disp_A", "rfnd_A"], at=row_at)])
    v.i12h(v.donations[DID], v.history)
    return [(f.cls, f.detail.get("problem") or f.detail.get("reason"), f.detail.get("supporting_modes"))
            for f in v.findings]


def test_cr2_a_valid_evaluation_under_the_previous_mode_far_from_any_change_is_exact():
    row_at = T0 - timedelta(days=2)  # inside A1 (apply), a day away from every transition
    assert _i12h(_apply_then_hold(row_at, T0 - timedelta(hours=2)), row_at) == []


def test_d2_a_lost_dispute_ref_excludes_hold_so_a_row_near_the_change_is_exact():
    """Decision 2 (rule-proven exclusion): 0034's core refuses a clear result in mode hold while a lost dispute is
    current, and the refs must be exactly the counted refunds and lost disputes. A clear row naming a lost dispute
    was therefore written under apply, whatever the timestamps say."""
    row_at = T0 - timedelta(hours=2, minutes=-2)  # 2 min after A2 (hold) by stamps, still within A1's margin
    events = _apply_then_hold(row_at, T0 - timedelta(hours=2))
    events[1]["created_at"] = row_at - timedelta(minutes=1)  # the freeze stamped 1 min before the row
    assert _i12h(events, row_at) == []


def test_d2_a2_lost_dispute_ref_excludes_hold_inside_a_hold_period_too():
    row_at = T0 - timedelta(hours=1)  # inside A2 (hold) by stamps, far from A1 (apply): only apply is possible
    assert _i12h(_apply_then_hold(row_at, T0 - timedelta(hours=2)), row_at) == []


def test_d2_a_dispute_naming_clear_row_with_only_hold_periods_has_no_possible_mode_and_is_structural():
    hold_only = [event(1, "off", "active", T0 - timedelta(days=3), "hold", "hold")]
    assert _i12h(hold_only, T0 - timedelta(days=2)) == [("structural", "unsupported_amount", [])]


def _modal(monkeypatch, delta=500):
    """A mode-sensitive evaluator (the 0034 one never is for a non-excluded row): hold adds `delta` to a clear
    amount. It exercises the mode_order path that core-consistent data cannot reach."""
    real = accounting.evaluate_in_mode

    def fake(d, rows, mode):
        e = real(d, rows, mode)
        return accounting.Evaluation(e.reversed_paise + delta, None, e.flags) if mode == "hold" and e.hold is None \
            else e
    monkeypatch.setattr(verify.accounting, "evaluate_in_mode", fake)


def _i12h_refund(events, row_at, stored=2500):
    v = make(mode="hold", current=[refund()], events=events, cut_rows=lambda d, c: [refund()],
             history=[hist(1, "evaluation", 0, stored, refs=["rfnd_A"], at=row_at)])
    v.i12h(v.donations[DID], v.history)
    return [(f.cls, f.detail.get("problem") or f.detail.get("reason"), f.detail.get("supporting_modes"))
            for f in v.findings]


def test_d2_1_supported_by_one_nearby_mode_not_another_is_mode_order(monkeypatch):
    _modal(monkeypatch)
    row_at = T0 - timedelta(hours=2, minutes=-2)
    events = _apply_then_hold(row_at, T0 - timedelta(hours=2))
    events[1]["created_at"] = row_at - timedelta(minutes=1)
    assert _i12h_refund(events, row_at) == [("historical_ambiguous", "mode_order", ["apply"])]


def test_d2_1b_a_nearby_supporting_mode_is_not_proof_while_another_mode_is_possible(monkeypatch):
    """r4 flaw: only the margin modes had to support. Unbounded commit times leave hold possible a day away."""
    _modal(monkeypatch)
    row_at = T0 - timedelta(days=2)  # inside A1 (apply), a day from every transition
    assert _i12h_refund(_apply_then_hold(row_at, T0 - timedelta(hours=2)), row_at) == \
        [("historical_ambiguous", "mode_order", ["apply"])]


def test_d2_2_supported_by_every_possible_mode_is_exact():
    row_at = T0 - timedelta(hours=2, minutes=-2)
    events = _apply_then_hold(row_at, T0 - timedelta(hours=2))
    assert _i12h_refund(events, row_at) == []  # refund-only: both modes compute 2500


def test_d2_3_supported_only_by_a_distant_mode_is_mode_order_never_exact_or_structural(monkeypatch):
    _modal(monkeypatch)
    row_at = T0 - timedelta(hours=1)  # inside A2 (hold) by stamps; only the distant apply supports 2500
    assert _i12h_refund(_apply_then_hold(row_at, T0 - timedelta(hours=2)), row_at) == \
        [("historical_ambiguous", "mode_order", ["apply"])]


# ── Decision 4: evidence states outside the margin (amended I12-H residual rule) ────────────────────────────────────

def _i12h_states(stored, refs=("rfnd_A", "rfnd_B")):
    """rfnd_A long before the row, rfnd_B recorded 10 min after it (outside ±M): only a distant state has both."""
    row_at = T0 - timedelta(hours=1)
    b_at = row_at + timedelta(minutes=10)
    v = make(current=[refund(), refund("rfnd_B", 1000)], events=ACTIVATED,
             evidence=[("rfnd_A", T0 - timedelta(days=1)), ("rfnd_B", b_at)],
             cut_rows=lambda d, c: [refund(), refund("rfnd_B", 1000)] if c == "infinity" or
             (not isinstance(c, str) and c >= b_at) else ([] if isinstance(c, str) else [refund()]),
             history=[hist(1, "activation", 0, stored, refs=list(refs), at=row_at, run=9)])
    v.i12h(v.donations[DID], v.history)
    return [(f.cls, f.detail.get("problem") or f.detail.get("reason")) for f in v.findings]


def test_d4_an_amount_only_a_state_outside_the_margin_supports_is_evidence_order():
    assert _i12h_states(3500) == [("historical_ambiguous", "evidence_order")]  # r4: structural


def test_d4_an_amount_no_evidence_state_supports_is_structural():
    assert _i12h_states(9999) == [("structural", "unsupported_amount")]
    assert _i12h_states(3500, refs=("rfnd_A", "rfnd_X")) == [("structural", "unsupported_amount")]  # stored refs kept


def test_cr2_c_an_amount_no_mode_supports_is_still_structural():
    row_at = T0 - timedelta(days=2)
    got = _i12h(_apply_then_hold(row_at, T0 - timedelta(hours=2)), row_at, stored=3000)
    assert got == [("structural", "unsupported_amount", [])]


def test_cr2_d_without_a_mode_change_the_old_and_new_rules_agree():
    row_at = T0 - timedelta(hours=1)
    events = [event(1, "off", "active", T0 - timedelta(days=1), "apply", "apply")]
    assert _i12h(events, row_at) == [] and _i12h(events, row_at, stored=3000)[0][0] == "structural"


KEEP = hist(2, "manual", 0, 0, hold_before="H-DISPUTE-MODE", at=T0, run=3)
TWO_MODES = [event(1, "off", "frozen", T0 - timedelta(days=2), "hold", "hold"),
             event(2, "frozen", "frozen", T0 - timedelta(days=1), "hold", "apply"),
             event(3, "frozen", "frozen", T0 + timedelta(days=1), "apply", "hold")]


def _ack(events, fp_at, evidence=()):
    return make(events=events, fp_at=fp_at, evidence=evidence).ack_reconstructs(DID, KEEP, "ACK")


def test_d5_a1_an_ack_is_never_proven_on_0034_even_with_a_single_mode():
    """r5 returned True here (single mode, matched within the margin); the keep's snapshot is never observable."""
    one_mode = [event(1, "off", "frozen", T0 - timedelta(days=2), "hold", "hold")]
    assert _ack(one_mode, lambda d, c, m: "ACK" if m == "hold" else "OTHER") is None


def test_d5_an_incomplete_enumeration_is_uncertain_never_forged():
    v = make(events=TWO_MODES)
    v.conceivable_fps = lambda did, modes: None  # over the bound, or the fingerprint replica failed
    assert v.ack_reconstructs(DID, KEEP, "ACK") is None


def _event_row(i, status="processed", amount=2500, ref="rfnd_A", donation=DID, at=None, phase=None, kind="refund"):
    at = at or T0 + timedelta(minutes=i)
    return {"id": f"00000000-0000-0000-0000-{i:012d}", "kind": kind, "provider_ref": ref, "provider_payment_id": PAY,
            "donation_id": donation, "match_type": "payment", "phase": phase, "status": status,
            "amount_paise": amount, "currency": "INR", "amount_deducted_paise": None, "provider_event_at": at,
            "received_at": at, "recorded_at": at}


def _summary(states):
    return sorted((None if s is None else (s["status"], s["amount_paise"], s["conflicting"])) for s in states
                  if s is not None) + ([None] if None in states else [])


def test_d5_ref_states_covers_every_commit_order_of_one_ref():
    pending, processed = _event_row(1, "pending", 2500), _event_row(2, "processed", 2500)
    corrected = _event_row(3, "processed", 2000)
    got = verify.ref_states([pending, processed, corrected], DID)
    # absent; pending alone; either processed version (conflict-free: same status); never pending over processed
    assert _summary(got) == [("pending", 2500, False), ("processed", 2000, False), ("processed", 2500, False), None]


def test_d5_ref_states_includes_conflicts_and_unattributed_winners():
    won, lost_ = _event_row(1, "won", None, ref="disp_A", kind="dispute"), \
        _event_row(2, "lost", None, ref="disp_A", kind="dispute")
    got = verify.ref_states([won, lost_], DID)
    # same rank: both present is a conflict won by the later event; either alone is clean; or no row
    assert _summary(got) == [("lost", None, False), ("lost", None, True), ("won", None, False), None]
    elsewhere = _event_row(3, "processed", 2500, donation="8d0c6a2e-1111-4a5b-9c3d-000000000999")
    assert _summary(verify.ref_states([elsewhere], DID)) == [None]  # the view hides another donation's winner


def test_d5_ref_states_is_bounded():
    assert verify.ref_states([_event_row(i) for i in range(verify.MAX_REF_EVENTS + 1)], DID) is None


def test_d5_an_over_bound_row_is_uncertain_never_exact_or_structural(monkeypatch):
    monkeypatch.setattr(verify, "ENUMERATION_BOUND", 0)
    v = make(current=[refund()], events=ACTIVATED, history=[hist(1, "evaluation", 0, 9999, refs=["rfnd_A"])])
    v.i12h(v.donations[DID], v.history)
    assert [(f.cls, f.detail["reason"], f.detail["enumeration"]) for f in v.findings] == \
        [("historical_ambiguous", "evidence_cut", "over_bound")]


def test_d3_4_an_ack_matching_one_of_several_possible_modes_is_uncertain():
    """r4 flaw: a match under a margin mode was taken as proof. The keep's commit is unbounded, so every mode the
    state ever had is possible; the fingerprint names its mode, but not that the state was in it."""
    assert _ack(TWO_MODES, lambda d, c, m: "ACK" if m == "apply" else "OTHER") is None  # r4: True
    assert _ack(TWO_MODES, lambda d, c, m: "ACK" if m == "hold" else "OTHER") is None


def test_d3_an_ack_matching_only_a_distant_evidence_state_is_uncertain_never_proven_or_forged():
    far = T0 - timedelta(days=2)
    one_mode = [event(1, "off", "frozen", T0 - timedelta(days=3), "hold", "hold")]
    assert _ack(one_mode, lambda d, c, m: "ACK" if c == far else "OTHER",
                evidence=[("disp_A", far), ("disp_A", T0 + timedelta(hours=1))]) is None  # r4: False (forged)


def test_d3_5_an_ack_impossible_under_every_mode_and_evidence_state_is_forged():
    assert _ack(TWO_MODES, lambda d, c, m: "OTHER", evidence=[("disp_A", T0 - timedelta(days=2))]) is False


def test_cr2_e_an_uncertain_ack_grants_no_exception_and_raises_no_violation():
    rows = [hist(1, "hold", 0, 0, hold_after="H-DISPUTE-MODE", at=T0 - timedelta(days=3)),
            hist(2, "manual", 0, 0, hold_before="H-DISPUTE-MODE", at=T0, run=3)]
    events = [event(1, "off", "active", T0 - timedelta(days=4), "hold", "hold"),
              event(2, "active", "frozen", T0 - timedelta(days=2), "hold", "hold"),
              event(3, "frozen", "frozen", T0 - timedelta(days=1), "hold", "apply"),
              event(4, "frozen", "frozen", T0 + timedelta(hours=2), "apply", "hold")]
    v = make(state="frozen", mode="hold", current=[lost()], reversal={"applied": 0, "hold": None, "flags": []},
             ev={"evidence_fp": "FP", "evaluated_at": T0, "state": "frozen", "dispute_mode": "hold",
                 "ack_fp": "FP", "ack_run": 3},
             events=events, history=rows, runs={3: run_row(3, "resolve-keep", T0 - timedelta(hours=1))},
             fp_at=lambda d, c, m: "FP" if m == "hold" else "OTHER")  # matches only the far hold mode
    v.classify()
    got = [(f.cls, f.detail.get("reason"), f.entity_kind) for f in v.findings]
    assert got == [("historical_ambiguous", "ack_mode_order", "history")], got  # attention: neither pass nor forged


def test_cr2_f_a_proven_mode_change_stamped_before_the_evaluation_is_never_indeterminate():
    a = T0 - timedelta(hours=1)
    v = make(state="frozen", mode="hold", current=[refund()], reversal={"applied": 2500, "hold": None, "flags": []},
             fp_now="FP-HOLD", fp_other="FP",  # the stored fingerprint matches under the stored mode (apply)
             ev={"evidence_fp": "FP", "evaluated_at": a, "state": "active", "dispute_mode": "apply"},
             events=[event(1, "off", "active", T0 - timedelta(days=1), "apply", "apply"),
                     event(2, "active", "frozen", a + timedelta(minutes=1), "apply", "apply"),
                     event(3, "frozen", "frozen", a - timedelta(minutes=2), "apply", "hold")],  # stamped earlier
             history=[hist(1, "evaluation", 0, 2500, refs=["rfnd_A"], at=a)], cut_rows=lambda d, c: [refund()])
    v.classify()
    (f,) = [f for f in v.findings if f.invariant == "I13"]
    assert f.cls == "boundary_uncertain" and f.detail["origin_kind"] == "o_mode_uncertain"
