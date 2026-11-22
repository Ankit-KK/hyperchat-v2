"""PAY-15 stage 6a-2, audit-correction revision 7: the financial verifier without a database (Phase 1).

Pure-Python (U) rows of the revision 7 §7 matrix: the closed finding variants and the `reason` discriminator (G9*),
the blocker-aware gates (G1-G8, truth table rows 2-10), the I12-H precedence ladder and row shapes (P*, H*), the A1
outcomes (G10-G12, P9d/P9e), I16 attribution and I17 snapshot classification (unit analogs of the A*/S* DB rows, and
the T* chronology rows), the I18 event chain (E1-E6), and F7 / F8 (R1-R5). DB rows are deferred to Phase 2."""

import itertools
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.payments import accounting, operator, verify
from app.payments.verify import (
    ENUMERATION_BOUND, MAX_REF_EVENTS, VARIANTS, Finding, Refused, gates, parse_scope, refused_report, write_blockers,
)
from tests.payments.test_verify_units import (
    ACTIVATED, DID, LOSS, PAY, SID, T0, event, hist, lost, make, refund, run_row,
)

B = "8d0c6a2e-1111-4a5b-9c3d-000000000002"   # another donation
U1, U2, U3 = (f"00000000-0000-0000-0000-{i:012d}" for i in (1, 2, 3))
DM = accounting.DISPUTE_MODE_HOLD
FALSE_GATES = dict.fromkeys(verify.GATE_KEYS, False)


# ── helpers ───────────────────────────────────────────────────────────────────────────────────────────────────────

def rep(v) -> dict:
    """report() as verify() would return it: a Refused anywhere is a refused report (exit 2, every gate false)."""
    for attr, val in (("excluded", []), ("baselines", []), ("orphan_buckets", 0), ("orphan_rows", 0)):
        if not hasattr(v, attr):
            setattr(v, attr, val)
    try:
        return v.report()
    except Refused as exc:
        return refused_report(str(exc))


def ok(v) -> dict:
    r = rep(v)
    assert r["status"] != "refused", r.get("reason")
    return r


def blockers_of(r) -> set:
    return {b["blocker"] for b in r["write_blockers"]}


def variant_of(f, findings=()) -> str:
    """The one §3.2 variant a real finding matches (refuses otherwise)."""
    write_blockers(f, [f, *findings])
    payload = {k: x for k, x in f.detail.items() if k != "reason"}
    (name,) = [v.name for v in VARIANTS[(f.invariant, f.cls, f.detail["reason"])] if verify._matches(v, payload)]
    return name


def ev_row(i, *, ref="rfnd_A", kind=None, status="processed", amount=2500, donation=DID, payment=PAY,
           account="primary", currency="INR", deducted=None, phase=None, snapshot=None, at=None, undated=False):
    kind = kind or ("dispute" if ref.startswith("disp") else "refund")
    t = at or T0 + timedelta(minutes=i)
    return {"id": f"00000000-0000-0000-0000-{i:012d}", "kind": kind, "provider_ref": ref, "provider_payment_id": payment,
            "donation_id": donation, "account": account, "match_type": "payment", "phase": phase, "status": status,
            "amount_paise": amount, "currency": currency, "amount_deducted_paise": deducted, "snapshot": snapshot,
            "provider_event_at": None if undated else t, "received_at": t, "recorded_at": t}


def lost_row(i, ref="disp_A", donation=DID, deducted=4000, **kw):
    return ev_row(i, ref=ref, status="lost", amount=10000, deducted=deducted, phase="chargeback", donation=donation,
                  **kw)


def with_raw(v, by_ref: dict):
    """Revision 7 seam: the raw events of each stored ref (any donation)."""
    v.raw_events = lambda did, ref: by_ref.get(ref, [])
    return v


def i12h_findings(v) -> list[tuple]:
    v.i12h(v.donations[DID], v.history)
    return [(f.invariant, f.cls, f.detail.get("problem") or f.detail.get("reason")) for f in v.findings]


# ── G9: closed variants and the reason discriminator (§3.2, §4.2) ─────────────────────────────────────────────────

H1 = {"enumeration": "complete", "chain": "valid", "basis": "evidence"}
H2 = {**H1, "chain": "invalid"}
H3 = {"enumeration": "over_bound", "bound": "ref_events", "chain": "valid", "basis": "evidence", "ref": "rfnd_A",
      "events": 13}
H4 = {"enumeration": "over_bound", "bound": "combinations", "chain": "valid", "basis": "evidence",
      "combinations": 4608}
A1 = {"enumeration": "complete", "run": 3, "basis": "mode"}
A2 = {"enumeration": "over_bound", "bound": "ref_events", "run": 3, "ref": "rfnd_A", "events": 13}
A3 = {"enumeration": "over_bound", "bound": "combinations", "run": 3, "combinations": 8192}
A4 = {"enumeration": "replica_failed", "run": 3, "donation": DID}
I1 = {"ref": "rfnd_A", "payment": PAY, "account": "primary", "source_ids": [U1]}
SNAP = {"subject": "snapshot", "source_ids": [U1]}
REPLICA = Finding("I10", "structural", "donation", DID, {"problem": "fp_replica_mismatch"})

VALID = [  # (id, invariant, class, reason, payload, blockers)
    ("V-H1", "I12-H", "historical_ambiguous", "evidence_cut", H1, set()),
    ("V-H1", "I12-H", "historical_ambiguous", "mode_order", {**H1, "basis": "mode"}, set()),
    ("V-H1", "I12-H", "historical_ambiguous", "evidence_order", H1, set()),
    ("V-H2", "I12-H", "historical_ambiguous", "evidence_cut", H2, {"chain_invalid"}),
    ("V-H2", "I12-H", "historical_ambiguous", "mode_order", {**H2, "basis": "mode"}, {"chain_invalid"}),
    ("V-H2", "I12-H", "historical_ambiguous", "evidence_order", H2, {"chain_invalid"}),
    ("V-H3", "I12-H", "historical_ambiguous", "evidence_cut", H3, {"s2_over_bound"}),
    ("V-H3", "I12-H", "historical_ambiguous", "evidence_cut", {**H3, "chain": "invalid"},
     {"s2_over_bound", "chain_invalid"}),
    ("V-H4", "I12-H", "historical_ambiguous", "evidence_cut", H4, {"s2_over_bound"}),
    ("V-H4", "I12-H", "historical_ambiguous", "evidence_cut", {**H4, "chain": "invalid"},
     {"s2_over_bound", "chain_invalid"}),
    ("V-A1", "I12", "historical_ambiguous", "ack_mode_order", A1, set()),
    ("V-A2", "I12", "historical_ambiguous", "ack_mode_order", A2, {"a1_over_bound"}),
    ("V-A2", "I12", "historical_ambiguous", "ack_mode_order", {**A2, "basis": "mode"}, {"a1_over_bound"}),
    ("V-A3", "I12", "historical_ambiguous", "ack_mode_order", A3, {"a1_over_bound"}),
    ("V-A4", "I12", "historical_ambiguous", "ack_mode_order", A4, {"a1_replica_failed"}),
    ("V-I1", "I16", "evidence_attribution", "unattributed", I1, {"i16:unattributed"}),
    ("V-I1", "I16", "evidence_attribution", "account_mismatch", I1, {"i16:account_mismatch"}),
    ("V-I2", "I16", "evidence_attribution", "wrong_donation", {**I1, "stored_donation": B}, {"i16:wrong_donation"}),
    ("V-I3", "I16", "evidence_attribution", "ref_identity_conflict", {**I1, "versions": 2},
     {"i16:ref_identity_conflict"}),
    ("V-S1", "I17", "refund_snapshot_mismatch", "snapshot_conflict", {**SNAP, "source_ids": [U1, U2]},
     {"i17:snapshot_conflict"}),
    ("V-S2", "I17", "refund_snapshot_mismatch", "snapshot_unit_conflict",
     {**SNAP, "members": [{"id": U1, "unit_status": "currency_mismatch", "currency": "INR", "value": 500}]},
     {"i17:snapshot_unit_conflict"}),
    ("V-S3", "I17", "refund_snapshot_mismatch", "snapshot_regression",
     {**SNAP, "source_ids": [U1, U2], "earlier_id": U1, "earlier_at": T0, "earlier_paise": 5000, "later_id": U2,
      "later_at": T0 + timedelta(hours=1), "later_paise": 2500, "regressions": 1}, {"i17:snapshot_regression"}),
    ("V-S4", "I17", "refund_snapshot_mismatch", "pending_refunds",
     {**SNAP, "snapshot_paise": 4000, "processed_paise": 2500, "pending_paise": 2000}, {"i17:pending_refunds"}),
    ("V-S4", "I17", "refund_snapshot_mismatch", "pending_refunds",
     {**SNAP, "snapshot_paise": 4500, "processed_paise": 2500, "pending_paise": 2000, "conditional": True},
     {"i17:pending_refunds"}),
    ("V-S5", "I17", "refund_snapshot_mismatch", "missing_refund_evidence",
     {**SNAP, "snapshot_paise": 5000, "processed_paise": 2500, "pending_paise": 0}, {"i17:missing_refund_evidence"}),
    ("V-S6", "I17", "refund_snapshot_mismatch", "snapshot_behind",
     {**SNAP, "snapshot_paise": 2000, "processed_paise": 2500, "pending_paise": 0}, {"i17:snapshot_behind"}),
    ("V-R1", "I17", "refund_snapshot_mismatch", "amount_unknown", {"subject": "refund", "ref": "rfnd_A",
                                                                    "source_ids": [U1]}, {"i17:amount_unknown"}),
    ("V-R2", "I17", "refund_snapshot_mismatch", "currency_unknown",
     {"subject": "refund", "ref": "rfnd_A", "source_ids": [U1], "conditional_match": True}, {"i17:currency_unknown"}),
    ("V-R3", "I17", "refund_snapshot_mismatch", "currency_mismatch",
     {"subject": "refund", "ref": "rfnd_A", "source_ids": [U1], "refund_currency": "USD", "donation_currency": "INR"},
     {"i17:currency_mismatch"}),
    ("V-R4", "I17", "refund_snapshot_mismatch", "unsupported_currency",
     {"subject": "refund", "ref": "rfnd_A", "source_ids": [U1], "refund_currency": "JPY"},
     {"i17:unsupported_currency"}),
    ("V-D1", "I17", "refund_snapshot_mismatch", "unsupported_currency",
     {"subject": "donation", "donation_currency": "JPY"}, {"i17:unsupported_currency"}),
]


def fnd(inv, cls, reason, payload, **extra) -> Finding:
    detail = {**payload, **extra}
    if reason is not None:
        detail["reason"] = reason
    return Finding(inv, cls, "history" if inv.startswith("I12") else "donation", DID, detail)


@pytest.mark.parametrize("vid,inv,cls,reason,payload,expected", VALID, ids=[f"{v[0]}-{v[3]}" for v in VALID])
def test_g9_v1_g9_r2_every_valid_variant_with_reason_in_detail_gives_exactly_its_blockers(vid, inv, cls, reason,
                                                                                         payload, expected):
    f = fnd(inv, cls, reason, payload)
    assert set(write_blockers(f, [f, REPLICA])) == expected
    assert variant_of(f, [REPLICA]) == vid


def _refuses(f, findings=()):
    with pytest.raises(Refused):
        write_blockers(f, [f, *findings])


@pytest.mark.parametrize("vid,inv,cls,reason,payload,expected", VALID, ids=[f"{v[0]}-{v[3]}" for v in VALID])
def test_g9_v2_every_missing_required_field_refuses(vid, inv, cls, reason, payload, expected):
    (variant,) = [v for v in VARIANTS[(inv, cls, reason)] if v.name == vid]
    for k in variant.required:
        _refuses(fnd(inv, cls, reason, {x: y for x, y in payload.items() if x != k}), [REPLICA])


@pytest.mark.parametrize("case", [
    ("G9a not_run", "I12-H", "evidence_cut", {**H1, "enumeration": "not_run"}),
    ("G9b bogus reason", "I12-H", "bogus", H1),
    ("G9c no enumeration", "I12-H", "evidence_cut", {k: x for k, x in H1.items() if k != "enumeration"}),
    ("G9f over_bound without bound", "I12-H", "evidence_cut", {k: x for k, x in H3.items() if k != "bound"}),
    ("G9g contradictory bound", "I12-H", "evidence_cut", {**H1, "bound": "combinations", "combinations": 999999}),
    ("V3 H1+events", "I12-H", "evidence_cut", {**H1, "events": 13}),
    ("V3 H3+combinations", "I12-H", "evidence_cut", {**H3, "combinations": 8192}),
    ("V3 H4+ref", "I12-H", "evidence_cut", {**H4, "ref": "rfnd_A"}),
    ("V3 A4+bound", "I12", "ack_mode_order", {**A4, "bound": "combinations"}),
    ("V3 A1+events", "I12", "ack_mode_order", {**A1, "events": 13}),
    ("V4 A2 replica_failed", "I12", "ack_mode_order", {**A2, "enumeration": "replica_failed"}),
    ("V5 enumeration partial", "I12-H", "evidence_cut", {**H1, "enumeration": "partial"}),
    ("V5 bound rows", "I12-H", "evidence_cut", {**H3, "bound": "rows"}),
    ("V5 chain unknown", "I12-H", "evidence_cut", {**H1, "chain": "unknown"}),
    ("V6 H3 events 12", "I12-H", "evidence_cut", {**H3, "events": 12}),
    ("V6 H4 combinations 4096", "I12-H", "evidence_cut", {**H4, "combinations": 4096}),
    ("V8 unknown field", "I12-H", "evidence_cut", {**H1, "foo": 1}),
], ids=lambda c: c[0])
def test_g9_invalid_i12_combinations_refuse(case):
    _, inv, reason, payload = case
    _refuses(fnd(inv, "historical_ambiguous", reason, payload), [REPLICA])


@pytest.mark.parametrize("case", [
    ("G9d unknown I16 reason", "I16", "evidence_attribution", "bogus", I1),
    ("G9e snapshot reason with refund subject", "I17", "refund_snapshot_mismatch", "snapshot_conflict",
     {"subject": "refund", "source_ids": [U1, U2]}),
    ("V3 R4+members", "I17", "refund_snapshot_mismatch", "unsupported_currency",
     {"subject": "refund", "ref": "rfnd_A", "source_ids": [U1], "refund_currency": "JPY", "members": []}),
    ("V3 D1+ref", "I17", "refund_snapshot_mismatch", "unsupported_currency",
     {"subject": "donation", "donation_currency": "JPY", "ref": "rfnd_A"}),
    ("V3 S4+earlier_id", "I17", "refund_snapshot_mismatch", "pending_refunds",
     {**SNAP, "snapshot_paise": 4000, "processed_paise": 2500, "pending_paise": 2000, "earlier_id": U1}),
    ("V5 subject payment", "I17", "refund_snapshot_mismatch", "amount_unknown",
     {"subject": "payment", "ref": "rfnd_A", "source_ids": [U1]}),
    ("V5 unit_status bogus", "I17", "refund_snapshot_mismatch", "snapshot_unit_conflict",
     {**SNAP, "members": [{"id": U1, "unit_status": "bogus", "currency": "INR", "value": 1}]}),
    ("V6 S5 with S <= P+Q", "I17", "refund_snapshot_mismatch", "missing_refund_evidence",
     {**SNAP, "snapshot_paise": 4000, "processed_paise": 2500, "pending_paise": 2000}),
    ("V6 S3 not decreasing", "I17", "refund_snapshot_mismatch", "snapshot_regression",
     {**SNAP, "source_ids": [U1, U2], "earlier_id": U1, "earlier_at": T0, "earlier_paise": 2500, "later_id": U2,
      "later_at": T0 + timedelta(hours=1), "later_paise": 2500, "regressions": 1}),
    ("V6 S2 all comparable", "I17", "refund_snapshot_mismatch", "snapshot_unit_conflict",
     {**SNAP, "members": [{"id": U1, "unit_status": "comparable", "currency": "INR", "value": 1}]}),
    ("V2 R1 two source ids", "I17", "refund_snapshot_mismatch", "amount_unknown",
     {"subject": "refund", "ref": "rfnd_A", "source_ids": [U1, U2]}),
], ids=lambda c: c[0])
def test_g9_invalid_i16_i17_combinations_refuse(case):
    _, inv, cls, reason, payload = case
    _refuses(fnd(inv, cls, reason, payload))


def test_g9_v7_a4_needs_the_matching_i10_replica_finding():
    f = fnd("I12", "historical_ambiguous", "ack_mode_order", A4)
    _refuses(f)
    other = Finding("I10", "structural", "donation", B, {"problem": "fp_replica_mismatch"})
    _refuses(f, [other])
    assert write_blockers(f, [f, REPLICA]) == {"a1_replica_failed"}


def test_g9_v8_a_diag_field_is_accepted_and_never_read():
    assert write_blockers(fnd("I12-H", "historical_ambiguous", "evidence_cut", {**H1, "diag_note": "x"}), []) == set()


@pytest.mark.parametrize("bad", ["missing", 7, None])
def test_g9_r3_a_missing_or_non_string_reason_refuses(bad):
    f = Finding("I12-H", "historical_ambiguous", "history", "1", {**H1, **({} if bad == "missing" else {"reason": bad})})
    with pytest.raises(Refused, match="missing or invalid reason"):
        write_blockers(f, [f])


def test_g9_r3_r4_a_refused_report_has_exit_2_and_every_gate_false():
    for reason in (None, "bogus"):
        v = make(history=())
        v.findings.append(fnd("I12-H", "historical_ambiguous", reason, H1))
        r = rep(v)
        assert (r["status"], r["exit_code"], r["gates"]) == ("refused", 2, FALSE_GATES)


def test_g9_r5_synthetic_the_f12_hold_diagnostics_are_accepted_on_an_a1_finding():
    """SYNTHETIC (Codex note on revision 7): at fceadd95 the A1 keep branch requires x_hold is None, so the real
    emitter never merges the F-12 hold keys. They stay permitted as harmless diagnostics; this test does not claim
    the A1 emitter produces that combination."""
    f12 = {"hold": DM, "applied_paise": 0, "processed_refunds_paise": 2500, "unapplied_refunds_paise": 2500}
    f = fnd("I12", "historical_ambiguous", "ack_mode_order", {**A1, **f12, "donation": DID})
    assert write_blockers(f, [f]) == set() and variant_of(f) == "V-A1"


# ── §4.3 gates and the §4.4 truth table (G1-G8, G9-C, G9-R1) ──────────────────────────────────────────────────

def _report_with(*findings, state="active", scope="full"):
    v = make(state=state)
    v.scope_kind = scope
    v.findings.extend(findings)
    return rep(v)


ORDINARY = fnd("I12-H", "historical_ambiguous", "evidence_cut", H1)
OVER = Finding("I12-H", "historical_ambiguous", "history", "77", {**H4, "reason": "evidence_cut"})
OVERDUE = Finding("I13", "overdue_reconciliation", "donation", DID, {})
STRUCT = Finding("I12-H", "structural", "history", "5", {"problem": "row_shape:run"})


def test_g1_g9_c_g9_r1_an_ordinary_evidence_cut_leaves_truth_table_row_3():
    r = _report_with(ORDINARY)
    assert r["status"] == "attention" and r["write_blockers"] == []
    assert r["gates"] == {"reconcile_precondition": True, "recovery_precondition": True,
                          "activation_verification_ready": False, "activation_complete": False}


def test_g2_g3_p18_an_over_bound_row_blocks_both_write_gates_and_is_listed():
    r = _report_with(OVER)
    assert blockers_of(r) == {"s2_over_bound"} and not r["gates"]["reconcile_precondition"]
    assert not r["gates"]["recovery_precondition"]
    both = _report_with(ORDINARY, OVER)
    assert [(b["blocker"], b["entity_id"]) for b in both["write_blockers"]] == [("s2_over_bound", "77")]


def test_g4_processed_only_full_active_is_clean_and_ready_but_never_complete():
    r = _report_with(Finding("I13", "processed", "donation", DID, {}))
    assert r["status"] == "clean"
    assert r["gates"] == {"reconcile_precondition": True, "recovery_precondition": True,
                          "activation_verification_ready": True, "activation_complete": False}


def test_g5_g6_g7_overdue_recovery_and_the_blocker_exception():
    g5 = _report_with(OVERDUE)["gates"]
    assert (g5["reconcile_precondition"], g5["recovery_precondition"]) == (False, True)
    g6 = _report_with(OVERDUE, OVER)["gates"]
    assert (g6["reconcile_precondition"], g6["recovery_precondition"]) == (False, False)  # truth table row 6
    g7 = _report_with(OVERDUE, STRUCT)["gates"]
    assert g7 == FALSE_GATES


@pytest.mark.parametrize("state,scope", [("active", "donation"), ("active", "creator"), ("frozen", "full"),
                                         ("off", "full")])
def test_g8_scoped_frozen_or_off_reports_have_every_gate_false(state, scope):
    assert _report_with(Finding("I13", "processed", "donation", DID, {}), state=state, scope=scope)["gates"] == \
        FALSE_GATES


def test_gate_invariants_refuse_an_inconsistent_build():
    with pytest.raises(Refused):
        gates("clean", True, "active", {}, frozenset({"s2_over_bound"}))
    assert gates("attention", True, "active", {"historical_ambiguous": 1}, frozenset({"x"}))[
        "reconcile_precondition"] is False


# ── I12-H ladder (§2.1) and row shapes (§2.2) ────────────────────────────────────────────────────────────────

def test_p1_a_clear_evaluation_without_refs_is_structural():
    v = make(history=[hist(1, "evaluation", 0, 9999, refs=None)])
    assert i12h_findings(v) == [("I12-H", "structural", "row_shape:missing_evidence_refs")]


def test_p3_p4_p5_keep_rows_must_satisfy_the_whole_keep_contract():
    p3 = make(history=[hist(1, "manual", 0, 0, hold_before="H-CURRENCY", run=3)],
              runs={3: run_row(3, "resolve-keep", T0)})
    assert i12h_findings(p3) == [("I12-H", "structural", "row_shape:keep_hold")]
    p4 = make(history=[hist(1, "manual", 0, 0, hold_before=DM, run=8)], runs={8: run_row(8, "revert", T0, scope={})})
    assert i12h_findings(p4) == [("I12-H", "structural", "row_shape:keep_run")]
    p5 = make(history=[hist(1, "manual", 0, 0, hold_before=DM, run=4)], runs={4: run_row(4, "resolve-apply", T0)})
    assert i12h_findings(p5) == [("I12-H", "structural", "row_shape:apply_refs")]


def test_p4_p5_the_d10_run_findings_are_unchanged():
    for command in ("revert", "resolve-apply"):
        v = make(history=[hist(1, "manual", 0, 0, hold_before=DM, run=8)],
                 runs={8: run_row(8, command, T0, scope={} if command == "revert" else None)})
        v.runs_check()
        assert any(f.cls == "audit_integrity" for f in v.findings), command


def test_p6_a_history_only_donation_is_traversed():
    v = make(history=[hist(1, "evaluation", 0, 9999, refs=None)])
    assert (v.current, v.evals, v.reversals, v.evidence) == ({}, {}, {}, {})
    v.classify()
    assert [(f.cls, f.detail.get("problem")) for f in v.findings] == \
        [("structural", "row_shape:missing_evidence_refs")]


def test_row_shape_activation_and_revert_runs_and_the_missing_run():
    assert make().row_shape(hist(1, "activation", 0, 2500, refs=["rfnd_A"], run=None)) == ["run"]
    assert make().row_shape(hist(1, "manual", 0, 0, hold_before=DM, run=None)) == ["run"]   # a keep without a run
    assert make().row_shape(hist(1, "manual", 0, 0, hold_before=DM, run=99)) == ["run"]     # a run that is gone
    v = make(runs={9: run_row(9, "reconcile", T0), 8: run_row(8, "revert", T0, scope={})})
    assert v.row_shape(hist(1, "activation", 0, 2500, refs=["rfnd_A"], run=9)) == []
    assert v.row_shape(hist(1, "activation", 0, 0, hold_after=DM, run=9)) == []
    assert v.row_shape(hist(1, "revert", 2500, 0, run=8)) == []
    assert v.row_shape(hist(1, "revert", 2500, 100, run=8)) == ["revert_shape"]
    assert v.row_shape(hist(1, "hold", 0, 0, hold_after=DM)) == []
    assert v.row_shape(hist(1, "hold", 0, 100, hold_after=DM)) == ["amount_changed"]
    assert v.row_shape(hist(1, "resolved", 0, 0, hold_before=DM, refs=[])) == []
    assert v.row_shape(hist(1, "evaluation", 0, 0, refs=[])) == []  # '{}' stays distinct from NULL (P7)


@pytest.mark.parametrize("refs,rule", [(["rfnd_A", None], "null_ref"), (["rfnd_A", "rfnd_A"], "duplicate_ref"),
                                       (["refund_A"], "ref_format")])
def test_h2_h3_element_rules_are_structural_in_a_normal_report(refs, rule):
    v = make(history=[hist(1, "evaluation", 0, 2500, refs=refs)])
    v.i12h(v.donations[DID], v.history)  # H3: a NULL element no longer raises TypeError (β4)
    assert [(f.cls, f.detail["problem"]) for f in v.findings] == [("structural", "row_shape:" + rule)]
    assert ok(v)["status"] == "violation"


def test_h2_a_duplicate_ref_wins_over_an_over_bound_ref():
    pools = {f"rfnd_{c}": [ev_row(10 * n + i, ref=f"rfnd_{c}", amount=100 + i) for i in range(k)]
             for n, (c, k) in enumerate(zip("ABCD", (8, 8, 8, 9)))}
    v = with_raw(make(history=[hist(1, "evaluation", 0, 2500, refs=["rfnd_A", "rfnd_A", "rfnd_B", "rfnd_C",
                                                                    "rfnd_D"])]), pools)
    assert i12h_findings(v) == [("I12-H", "structural", "row_shape:duplicate_ref")]


def test_p8_a_ref_with_no_event_anywhere_is_absent():
    v = with_raw(make(history=[hist(1, "evaluation", 0, 2500, refs=["rfnd_Z"])]), {})
    assert i12h_findings(v) == [("I12-H", "structural", "absent_reference")]
    assert v.findings[0].detail["ref"] == "rfnd_Z"


def test_p9_h1_h4_refs_no_version_could_have_been_counted_for_this_donation_are_ineligible():
    on_b = [ev_row(i, donation=B) for i in range(13)]  # H1: 13 versions, all on B (step 3 precedes the bound)
    other_payment = [ev_row(20, payment="pay_OTHER")]
    pending_only = [ev_row(30, status="pending")]  # H4
    for raw in (on_b, other_payment, pending_only, [ev_row(40, currency="USD")]):
        v = with_raw(make(history=[hist(1, "evaluation", 0, 2500, refs=["rfnd_A"])]), {"rfnd_A": raw})
        assert i12h_findings(v) == [("I12-H", "structural", "ineligible_reference")]
        assert v.findings[0].detail["versions"] == len(raw)


def test_eligibility_of_a_lost_dispute():
    v = make()
    d = v.donations[DID]
    assert v.eligible(lost_row(1), d)
    assert not v.eligible(lost_row(1, deducted=0), d)
    assert not v.eligible({**lost_row(1), "phase": None}, d)
    assert not v.eligible(lost_row(1, currency="USD"), d)


def _mixed(n_on_b):
    """P9b / P9c: one eligible version on A plus n processed versions of the same ref on B."""
    return {"rfnd_A": [ev_row(0), *[ev_row(i + 1, donation=B, amount=3000 + i) for i in range(n_on_b)]]}


def test_p9b_mixed_attribution_with_13_raw_events_is_over_bound_and_blocks():
    v = with_raw(make(events=ACTIVATED, history=[hist(1, "evaluation", 0, 2500, refs=["rfnd_A"])]), _mixed(12))
    v.i12h(v.donations[DID], v.history)
    (f,) = v.findings
    assert (f.cls, f.detail["reason"], f.detail["bound"], f.detail["events"]) == \
        ("historical_ambiguous", "evidence_cut", "ref_events", 13)
    assert variant_of(f) == "V-H3"
    r = ok(v)
    assert blockers_of(r) == {"s2_over_bound"} and not r["gates"]["reconcile_precondition"]
    assert not r["gates"]["recovery_precondition"]


def test_p9c_p10_twelve_raw_events_are_in_bound():
    v = with_raw(make(events=ACTIVATED, history=[hist(1, "evaluation", 0, 2500, refs=["rfnd_A"])]), _mixed(11))
    v.i12h(v.donations[DID], v.history)
    (f,) = v.findings  # P9c: enumerated in bound; the one eligible version gives exactly the stored amount
    assert (f.detail["enumeration"], f.detail["candidates"]) == ("complete", [2500])  # (no nearby state: evidence_order)
    all_a = {"rfnd_A": [ev_row(i, amount=2500 if i == 0 else 2000 + i) for i in range(MAX_REF_EVENTS)]}
    v = with_raw(make(events=ACTIVATED, history=[hist(1, "evaluation", 0, 2500, refs=["rfnd_A"])]), all_a)
    v.i12h(v.donations[DID], v.history)
    (f,) = v.findings  # P10: twelve versions, enumerated completely
    assert (f.detail["reason"], f.detail["enumeration"], len(f.detail["candidates"])) == ("evidence_cut", "complete",
                                                                                        12)


def test_p11_thirteen_raw_events_with_one_counted_are_over_bound():
    raw = {"rfnd_A": [ev_row(0), *[ev_row(i, status="pending") for i in range(1, 13)]]}
    v = with_raw(make(events=ACTIVATED, history=[hist(1, "evaluation", 0, 2500, refs=["rfnd_A"])]), raw)
    v.i12h(v.donations[DID], v.history)
    assert [(f.detail["bound"], f.detail["events"]) for f in v.findings] == [("ref_events", 13)]


def _pools(sizes):
    return {f"rfnd_{c}": [ev_row(100 * n + i, ref=f"rfnd_{c}", amount=100 * (n + 1) + i) for i in range(k)]
            for n, (c, k) in enumerate(zip("ABCD", sizes))}


def test_p12_p13_the_combination_bound_is_inclusive():
    refs = ["rfnd_A", "rfnd_B", "rfnd_C", "rfnd_D"]
    stored = 100 + 200 + 300 + 400  # the first version of each ref
    p12 = with_raw(make(events=ACTIVATED, history=[hist(1, "evaluation", 0, stored, refs=refs)]), _pools((8,) * 4))
    p12.i12h(p12.donations[DID], p12.history)
    assert 8 ** 4 == ENUMERATION_BOUND
    (f,) = p12.findings  # enumerated: 4 096 combinations, inclusive
    assert (f.cls, f.detail["reason"], f.detail["enumeration"]) == ("historical_ambiguous", "evidence_cut", "complete")
    assert variant_of(f) == "V-H1"
    p13 = with_raw(make(events=ACTIVATED, history=[hist(1, "evaluation", 0, stored, refs=refs)]),
                   _pools((8, 8, 8, 9)))
    p13.i12h(p13.donations[DID], p13.history)
    (f,) = p13.findings
    assert (f.detail["bound"], f.detail["combinations"], variant_of(f)) == ("combinations", 4608, "V-H4")
    assert blockers_of(ok(p13)) == {"s2_over_bound"}


def test_p15_a_mode_impossible_proven_activation_row_stays_structural_whatever_the_bound():
    raw = {"disp_A": [lost_row(0), *[lost_row(i, donation=B) for i in range(1, 13)]]}  # 13 raw events
    v = with_raw(make(mode="hold", events=ACTIVATED, runs={9: run_row(9, "reconcile", T0)},
                      history=[hist(1, "activation", 0, 4000, refs=["disp_A"], run=9)]), raw)
    assert i12h_findings(v) == [("I12-H", "structural", "unsupported_amount")]
    assert v.findings[0].detail["provenance"] == "0034"
    assert ok(v)["gates"] == FALSE_GATES


def test_p16_an_unproven_mode_impossible_row_over_the_product_bound_is_structural():
    raw = {"disp_A": [lost_row(0)], **_pools((8, 8, 8, 9))}
    refs = ["disp_A", "rfnd_A", "rfnd_B", "rfnd_C", "rfnd_D"]
    v = with_raw(make(mode="hold", events=ACTIVATED, history=[hist(1, "evaluation", 0, 4000, refs=refs)]), raw)
    assert i12h_findings(v) == [("I12-H", "structural", "unsupported_amount")]  # β2 gave attention over_bound
    assert v.findings[0].detail["provenance"] == "unproven_0034"
    assert ok(v)["gates"] == FALSE_GATES


def test_p17_an_over_bound_row_plus_a_structural_row_is_a_violation():
    refs = ["rfnd_A", "rfnd_B", "rfnd_C", "rfnd_D"]
    v = with_raw(make(events=ACTIVATED, history=[hist(1, "evaluation", 0, 1000, refs=refs),
                                                 hist(2, "evaluation", 0, 9999, refs=None)]), _pools((8, 8, 8, 9)))
    v.i12h(v.donations[DID], v.history)
    r = ok(v)
    assert r["status"] == "violation" and r["gates"] == FALSE_GATES


def _damaged(mode_event_missing=True):
    """H6: an apply activation whose freeze / mode-change events are missing (a link break)."""
    return [event(1, "off", "active", T0 - timedelta(days=3), "hold", "hold"),
            event(3, "frozen", "active", T0 - timedelta(days=1), "apply", "apply")]


def test_p19_h6_an_invalid_chain_suspends_mode_dependent_exactness():
    v = make(mode="apply", current=LOSS, events=_damaged(), cut_rows=lambda d, c: LOSS,
             evidence=[("rfnd_A", T0 - timedelta(days=4)), ("disp_A", T0 - timedelta(days=4))],
             history=[hist(1, "evaluation", 0, 4000, refs=["disp_A", "rfnd_A"], at=T0 - timedelta(hours=12))])
    v.state_event_chain()
    assert not v.chain_valid
    assert [(f.invariant, f.detail["problem"]) for f in v.findings] == [("I18", "state_event_chain:link")]
    v.i12h(v.donations[DID], v.history)
    f = v.findings[-1]
    assert (f.cls, f.detail["reason"], f.detail["chain"], f.detail["basis"], variant_of(f)) == \
        ("historical_ambiguous", "mode_order", "invalid", "mode", "V-H2")
    r = ok(v)
    assert "chain_invalid" in blockers_of(r) and r["gates"] == FALSE_GATES


def test_h7_on_an_invalid_chain_a_row_no_mode_gives_stays_structural():
    v = make(mode="apply", current=LOSS, events=_damaged(), cut_rows=lambda d, c: LOSS,
             evidence=[("rfnd_A", T0 - timedelta(days=4)), ("disp_A", T0 - timedelta(days=4))],
             history=[hist(1, "evaluation", 0, 9999, refs=["disp_A", "rfnd_A"], at=T0 - timedelta(hours=12))])
    v.state_event_chain()
    v.i12h(v.donations[DID], v.history)
    assert [(f.cls, f.detail.get("problem")) for f in v.findings[1:]] == [("structural", "unsupported_amount")]


def test_a_valid_chain_keeps_the_fceadd95_exact_result():
    """Control: the same dispute-naming row on a valid apply chain is exact (no finding)."""
    events = [event(1, "off", "active", T0 - timedelta(days=3), "hold", "hold"),
              event(2, "active", "frozen", T0 - timedelta(days=2), "hold", "hold"),
              event(3, "frozen", "frozen", T0 - timedelta(days=2) + timedelta(minutes=5), "hold", "apply"),
              event(4, "frozen", "active", T0 - timedelta(days=1), "apply", "apply")]
    v = make(mode="apply", current=LOSS, events=events, cut_rows=lambda d, c: LOSS,
             evidence=[("rfnd_A", T0 - timedelta(days=4)), ("disp_A", T0 - timedelta(days=4))],
             history=[hist(1, "evaluation", 0, 4000, refs=["disp_A", "rfnd_A"], at=T0 - timedelta(hours=12))])
    v.state_event_chain()
    assert v.chain_valid and v.findings == []
    v.i12h(v.donations[DID], v.history)
    assert v.findings == []


# ── I18 (§2.5): E1-E6, H5 ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,state,mode,events,rules", [
    ("E1 active without events", "active", "hold", [], ["initial"]),
    ("E2 broken link", "active", "apply", None, ["link"]),
    ("E3 final mismatch", "frozen", "hold", [event(1, "off", "active", T0, "hold", "hold")], ["final"]),
    ("E4 simultaneous", "active", "apply", [event(1, "off", "active", T0, "hold", "apply")], ["simultaneous"]),
    ("E5 vocabulary", "active", "hold", [event(1, "off", "active", T0, "hold", "bogus"),
                                        event(2, "active", "active", T0, "bogus", "hold")],
     ["vocabulary", "vocabulary"]),
    ("E5 mode change while active", "active", "apply", [event(1, "off", "active", T0, "hold", "hold"),
                                                      event(2, "active", "active", T0, "hold", "apply")],
     ["mode_change"]),
    ("transition off->frozen", "frozen", "hold", [event(1, "off", "frozen", T0, "hold", "hold")], ["transition"]),
    ("noop", "off", "hold", [event(1, "off", "off", T0, "hold", "hold")], ["noop"]),
    ("E6 untouched off/hold", "off", "hold", [], []),
    ("H5 legal frozen mode change", "active", "apply",
     [event(1, "off", "active", T0, "hold", "hold"), event(2, "active", "frozen", T0, "hold", "hold"),
      event(3, "frozen", "frozen", T0, "hold", "apply"), event(4, "frozen", "active", T0, "apply", "apply")], []),
    ("legal mode change while off", "active", "apply",
     [event(1, "off", "off", T0, "hold", "apply"), event(2, "off", "active", T0, "apply", "apply")], []),
], ids=lambda x: x if isinstance(x, str) else "")
def test_i18_the_state_event_chain(name, state, mode, events, rules):
    if events is None:
        events = _damaged()
    v = make(state=state, mode=mode, events=events)
    v.state_event_chain()
    got = [f.detail["problem"].split(":")[1] for f in v.findings]
    assert got == rules
    assert all((f.invariant, f.cls, f.entity_kind) == ("I18", "structural", "accounting_state") for f in v.findings)
    assert v.chain_valid is (not rules)


# ── A1 (§2.4a): four outcomes, additive (G10-G12, P9d, P9e) ──────────────────────────────────────────────────

KEEP_ROWS = [hist(1, "hold", 0, 0, hold_after=DM, at=T0 - timedelta(hours=2)),
             hist(2, "manual", 0, 0, hold_before=DM, at=T0 - timedelta(hours=1), run=3)]
KEEP_EV = {"evidence_fp": "FP", "evaluated_at": T0 - timedelta(hours=1), "state": "active", "dispute_mode": "hold",
           "ack_fp": "FP", "ack_run": 3}


def _keep(by_ref=None, monkeypatch=None, fps=None, ev=None, reversal=None, **kw):
    """The keep branch with the real conceivable_fps over `by_ref`; FP_SETS answered by `fps(sets)`."""
    v = make(mode="hold", current=[lost()], reversal=reversal or {"applied": 0, "hold": None, "flags": []},
             ev=ev or KEEP_EV, events=ACTIVATED, history=KEEP_ROWS, runs={3: run_row(3, "resolve-keep", T0)}, **kw)
    if by_ref is not None:
        del v.conceivable_fps  # the real method, not make()'s seam
        v.ref_events = lambda did: by_ref
    if fps is not None:
        monkeypatch.setattr(verify, "_select",
                            lambda cur, sql, params=None: [{"fp": x} for x in fps(json.loads(params["sets"]))])
    return v


def _one_event_refs(n_refunds):
    return {"disp_A": [lost_row(0)], **{f"rfnd_{i}": [ev_row(i + 1, ref=f"rfnd_{i}")] for i in range(n_refunds)}}


def test_g10_a1_over_the_combination_bound_blocks_both_write_gates():
    v = _keep(_one_event_refs(12))
    v.classify()
    (f,) = [f for f in v.findings if f.cls == "historical_ambiguous"]
    assert (f.detail["enumeration"], f.detail["bound"], f.detail["combinations"]) == ("over_bound", "combinations",
                                                                                     8192)
    assert variant_of(f) == "V-A3"
    r = ok(v)
    assert blockers_of(r) == {"a1_over_bound"}
    assert not r["gates"]["reconcile_precondition"] and not r["gates"]["recovery_precondition"]


def test_g10c_a1_at_exactly_4096_is_enumerated_and_does_not_block(monkeypatch):
    v = _keep(_one_event_refs(11), monkeypatch, fps=lambda sets: ["FP"] * len(sets))
    v.classify()
    (f,) = [f for f in v.findings if f.cls == "historical_ambiguous"]
    assert (f.detail["enumeration"], variant_of(f)) == ("complete", "V-A1")
    r = ok(v)
    assert r["write_blockers"] == [] and r["gates"]["reconcile_precondition"] is True  # as G1


def test_p9d_a1_ref_with_13_raw_events_is_over_bound_ref_events():
    by_ref = {"disp_A": [lost_row(0), *[lost_row(i, donation=B) for i in range(1, 13)]]}
    v = _keep(by_ref)
    v.classify()
    (f,) = [f for f in v.findings if f.cls == "historical_ambiguous"]
    assert (f.detail["bound"], f.detail["events"], variant_of(f)) == ("ref_events", 13, "V-A2")
    assert blockers_of(ok(v)) == {"a1_over_bound"}


def test_p9e_a1_ref_with_12_raw_events_is_enumerated(monkeypatch):
    by_ref = {"disp_A": [lost_row(0), *[lost_row(i, donation=B) for i in range(1, 12)]]}
    v = _keep(by_ref, monkeypatch, fps=lambda sets: ["FP"] * len(sets))
    v.classify()
    assert all(f.detail.get("enumeration") in (None, "complete") for f in v.findings)
    assert "a1_over_bound" not in blockers_of(ok(v))


def test_g11_an_additive_ack_finding_never_hides_the_evaluation_disagreement():
    v = _keep(ev={**KEEP_EV, "ack_fp": "ACK"}, fp_at=lambda d, c, m: "ACK")  # conceivable, but not the current fp
    v.classify()
    got = sorted((f.invariant, f.cls, f.detail.get("problem") or f.detail.get("reason")) for f in v.findings)
    assert got == [("I12", "historical_ambiguous", "ack_mode_order"),
                   ("I12", "structural", "evaluation_disagreement")]
    assert [f.detail["enumeration"] for f in v.findings if f.cls == "historical_ambiguous"] == ["complete"]
    assert ok(v)["gates"] == FALSE_GATES


def test_g12_a_failed_fingerprint_replica_is_i10_structural_plus_a_replica_failed_ack(monkeypatch):
    v = _keep(_one_event_refs(1), monkeypatch, fps=lambda sets: ["FP"] * (len(sets) - 1) + ["NOT-THE-CURRENT-FP"])
    v.classify()
    got = sorted((f.invariant, f.cls, f.detail.get("problem") or f.detail.get("enumeration")) for f in v.findings)
    assert got == [("I10", "structural", "fp_replica_mismatch"), ("I12", "historical_ambiguous", "replica_failed")]
    r = ok(v)  # the V-A4 cross-check finds the I10 finding
    assert "a1_replica_failed" in blockers_of(r) and r["gates"] == FALSE_GATES


def test_a1_forged_is_unchanged():
    v = _keep(fp_at=lambda d, c, m: "SOMETHING-ELSE")
    v.classify()
    assert [(f.cls, f.detail.get("problem")) for f in v.findings] == [("audit_integrity", "forged_ack")]


# ── I16 (§2.3): unit analogs of A1-A8 ─────────────────────────────────────────────────────────────────────────

def _evidence(v, rows):
    v.all_events = rows
    v.events_by_ref = {}
    for e in rows:
        v.events_by_ref.setdefault(e["provider_ref"], []).append(e)
    return v


def _i16(rows, **donation):
    v = make()
    v.donations[DID].update(donation)
    _evidence(v, rows)
    v.evidence_attribution()
    return v, [f.detail["reason"] for f in v.findings]


def test_i16_unattributed_counted_evidence_is_attention_and_blocks():
    v, got = _i16([ev_row(1, donation=None)])
    assert got == ["unattributed"] and variant_of(v.findings[0]) == "V-I1"
    assert blockers_of(ok(v)) == {"i16:unattributed"}
    assert _i16([lost_row(1, donation=None)])[1] == ["unattributed"]


def test_i16_a5_unattributed_pending_refund_or_open_dispute_is_not_reported():
    assert _i16([ev_row(1, donation=None, status="pending")])[1] == []
    assert _i16([ev_row(1, ref="disp_A", donation=None, status="open")])[1] == []


def test_i16_wrong_donation_and_account_mismatch_and_identity_conflict():
    v, got = _i16([ev_row(1, donation=B)])
    assert got == ["wrong_donation"] and v.findings[0].detail["stored_donation"] == B
    assert variant_of(v.findings[0]) == "V-I2"
    v, got = _i16([ev_row(1, account="secondary")])
    assert got == ["account_mismatch"] and variant_of(v.findings[0]) == "V-I1"
    v, got = _i16([ev_row(1), ev_row(2, payment="pay_OTHER", status="pending")])  # A4: versions disagree on payment
    assert "ref_identity_conflict" in got
    (f,) = [f for f in v.findings if f.detail["reason"] == "ref_identity_conflict"]
    assert f.detail["versions"] == 2 and variant_of(f) == "V-I3"


def test_i16_a6_a8_order_or_legacy_never_owns():
    assert _i16([ev_row(1, donation=None, payment="pay_OTHER")])[1] == []  # same order, another payment
    assert _i16([ev_row(1, donation=None)], v2=False)[1] == []            # legacy / imported donation
    assert _i16([ev_row(1, donation=None)], payment_status="pending")[1] == []


def test_i16_the_winner_is_the_0029_ranking():
    """A processed version outranks a later pending one: the current row is processed and counted."""
    rows = [ev_row(1, donation=None), ev_row(2, donation=None, status="pending")]
    assert _i16(rows)[1] == ["unattributed"]


# ── I17 (§2.4): classification (T*), unit analogs of S* ────────────────────────────────────────────────────────

def _i17(observations, refunds=(), currency="INR"):
    """Observations are snapshot-carrying won disputes (they never count in P / Q); refunds give P and Q."""
    v = make()
    v.donations[DID]["currency"] = currency
    _evidence(v, [*observations, *refunds])
    v.refund_snapshots()
    return v, sorted(f.detail["reason"] for f in v.findings)


def obs(i, value, currency="INR", hours=0, undated=False):
    return ev_row(i, ref=f"disp_O{i}", status="won", amount=None, currency=currency, snapshot=value,
                  at=T0 + timedelta(hours=hours), undated=undated)


def processed(i, amount, currency="INR", status="processed"):
    return ev_row(100 + i, ref=f"rfnd_P{i}", amount=amount, currency=currency, status=status)


def test_s1_s3_s4_the_snapshot_against_processed_and_pending_refunds():
    assert _i17([obs(1, 5000)], [processed(1, 2500)])[1] == ["missing_refund_evidence"]
    assert _i17([obs(1, 4000)], [processed(1, 2500), processed(2, 2000, status="pending")])[1] == ["pending_refunds"]
    assert _i17([obs(1, 2000)], [processed(1, 2500)])[1] == ["snapshot_behind"]
    assert _i17([obs(1, 2500)], [processed(1, 2500)])[1] == []  # S2
    v, _ = _i17([obs(1, 5000)], [processed(1, 2500)])
    assert variant_of(v.findings[0]) == "V-S5"
    assert blockers_of(ok(v)) == {"i17:missing_refund_evidence"}


def test_s5_s6_s7_currency_of_the_candidates():
    assert _i17([obs(1, 500, "USD")], [processed(1, 500, "USD")], currency="USD")[1] == []  # S5
    v, got = _i17([obs(1, 500, "INR")], [processed(1, 500, "USD")], currency="USD")        # S6
    assert got == ["snapshot_unit_conflict"]
    assert v.findings[0].detail["members"][0]["unit_status"] == "currency_mismatch"
    v, got = _i17([obs(1, 500, None)], [processed(1, 500)])                                # S7
    assert got == ["snapshot_unit_conflict"] and variant_of(v.findings[0]) == "V-S2"
    assert v.findings[0].detail["members"][0]["unit_status"] == "currency_unknown"


def test_s8_s9_s17_s19_refund_side_exclusions():
    v, got = _i17([obs(1, 5000)], [processed(1, 2500), processed(2, 2500, "USD")])   # S8 / S19
    assert got == ["currency_mismatch", "missing_refund_evidence"]
    assert all(f.detail.get("conditional") for f in v.findings if f.detail["reason"] == "missing_refund_evidence")
    assert {variant_of(f) for f in v.findings} == {"V-R3", "V-S5"}
    v, got = _i17([obs(1, 2500)], [processed(1, 2500), processed(2, None)])        # S9; S = P with an exclusion
    assert got == ["amount_unknown"] and v.findings[0].detail["conditional_match"] is True
    v, got = _i17([obs(1, 2500)], [processed(1, 2500), processed(2, 2500, "JPY")])  # S17 refund subject
    assert got == ["unsupported_currency"] and variant_of(v.findings[0]) == "V-R4"
    v, got = _i17([obs(1, 2500, "JPY")], [], currency="JPY")                        # S17 donation subject
    assert got == ["unsupported_currency"] and variant_of(v.findings[0]) == "V-D1"
    assert _i17([obs(1, 2500)], [processed(1, 2500), processed(2, None, None, status="failed")])[1] == []  # T7
    assert _i17([obs(1, 2500)], [processed(1, 2500), processed(2, None, status="pending")])[1] == \
        ["amount_unknown"]                                                                                  # T8


@pytest.mark.parametrize("name,observations,p,currency,expected", [
    ("T3 dated 500 + undated 800", [obs(1, 500), obs(2, 800, undated=True)], 500, "INR", ["snapshot_conflict"]),
    ("T4 INR 900 then USD 500", [obs(1, 900, "INR", 0), obs(2, 500, "USD", 1)], 500, "USD", []),
    ("T5 USD 900 then USD 500", [obs(1, 900, "USD", 0), obs(2, 500, "USD", 1)], 500, "USD",
     ["snapshot_regression"]),
    ("T6 300 then 500", [obs(1, 300, hours=0), obs(2, 500, hours=1)], 500, "INR", []),
    ("T9 all undated, equal", [obs(1, 500, undated=True), obs(2, 500, undated=True)], 500, "INR", []),
    ("T10 dated 500 + undated 500", [obs(1, 500), obs(2, 500, undated=True)], 500, "INR", []),
    ("T11 superseded dated 500", [obs(1, 500, hours=0), obs(2, 800, undated=True), obs(3, 800, hours=1)], 800,
     "INR", []),
    ("T12 undated disagreement survives", [obs(1, 500, undated=True), obs(2, 800, undated=True),
                                           obs(3, 800, hours=1)], 800, "INR", ["snapshot_conflict"]),
    ("T15 5000 -> 2500 -> 5000", [obs(1, 5000, hours=0), obs(2, 2500, hours=1), obs(3, 5000, hours=2)], 5000,
     "INR", ["snapshot_regression"]),
    ("T15b 5000 -> 2500 -> 6000", [obs(1, 5000, hours=0), obs(2, 2500, hours=1), obs(3, 6000, hours=2)], 6000,
     "INR", ["snapshot_regression"]),
    ("T16 monotonic", [obs(1, 2500, hours=0), obs(2, 5000, hours=1), obs(3, 6000, hours=2)], 6000, "INR", []),
    ("T17 equal", [obs(1, 5000, hours=0), obs(2, 5000, hours=1), obs(3, 5000, hours=2)], 5000, "INR", []),
    ("T18 equal-time group then decrease", [obs(1, 5000, hours=0), obs(2, 2500, hours=0), obs(3, 4000, hours=1)],
     4000, "INR", ["snapshot_regression"]),
    ("T18b group only at the top", [obs(1, 2500, hours=0), obs(2, 5000, hours=1), obs(3, 4000, hours=1)], 4000,
     "INR", ["snapshot_conflict"]),
    ("T18c cross-currency chronology", [obs(1, 9000, "INR", 0), obs(2, 500, "USD", 1), obs(3, 400, "USD", 2)], 400,
     "USD", ["snapshot_regression"]),
    ("S16a dated 5000, P 2500", [obs(1, 5000)], 2500, "INR", ["missing_refund_evidence"]),
    ("S16b later dated 2500", [obs(1, 5000, hours=0), obs(2, 2500, hours=1)], 2500, "INR",
     ["snapshot_regression"]),
], ids=lambda x: x if isinstance(x, str) else "")
def test_i17_chronology(name, observations, p, currency, expected):
    v, got = _i17(observations, [processed(1, p, currency)], currency=currency)
    assert got == expected
    r = ok(v)  # every real emitter shape matches exactly one variant
    assert bool(r["write_blockers"]) is bool(expected)
    if expected:
        assert not r["gates"]["reconcile_precondition"] and not r["gates"]["recovery_precondition"]


def test_t15_t15b_t18c_report_the_first_decreasing_pair():
    v, _ = _i17([obs(1, 5000, hours=0), obs(2, 2500, hours=1), obs(3, 5000, hours=2), obs(4, 1000, hours=3)],
                [processed(1, 1000)])
    (f,) = [f for f in v.findings if f.detail["reason"] == "snapshot_regression"]
    assert (f.detail["earlier_paise"], f.detail["later_paise"], f.detail["regressions"]) == (5000, 2500, 2)
    assert f.detail["source_ids"] == [f.detail["earlier_id"], f.detail["later_id"]] and variant_of(f) == "V-S3"
    v, _ = _i17([obs(1, 9000, "INR", 0), obs(2, 500, "USD", 1), obs(3, 400, "USD", 2)], [processed(1, 400, "USD")],
                currency="USD")
    (f,) = v.findings
    assert (f.detail["earlier_paise"], f.detail["later_paise"]) == (500, 400)  # INR 9 000 never compared


def test_t14_the_unit_conflict_is_identical_under_every_id_and_receipt_order():
    results = set()
    for ids, flip in itertools.product(((1, 2), (2, 1)), (False, True)):
        usd, inr = obs(ids[0], 500, "USD"), obs(ids[1], 500, "INR")
        usd["provider_event_at"] = inr["provider_event_at"] = T0
        if flip:
            usd["received_at"], inr["received_at"] = inr["received_at"], usd["received_at"]
        v, got = _i17([usd, inr], [processed(1, 500, "USD")], currency="USD")
        assert got == ["snapshot_unit_conflict"]
        f = v.findings[0]
        results.add(json.dumps(sorted((m["unit_status"], m["currency"]) for m in f.detail["members"])))
        assert len(f.detail["source_ids"]) == 2
    assert len(results) == 1


def test_i17_without_a_snapshot_a_refund_exclusion_is_still_reported_and_blocks():
    """Implementation choice (conservative, listed for the owner): §2.4.5 reports a relevant refund row that cannot
    contribute; it is reported even when no snapshot exists, so the write gates close (attention + blocker)."""
    v, got = _i17([], [processed(1, 2500, "USD")])
    assert got == ["currency_mismatch"] and "conditional_match" not in v.findings[0].detail
    assert blockers_of(ok(v)) == {"i17:currency_mismatch"}


def test_i17_v_d1_is_reported_for_an_unsupported_donation_currency_even_without_evidence():
    v, got = _i17([], [], currency="JPY")
    assert got == ["unsupported_currency"] and variant_of(v.findings[0]) == "V-D1"


def test_i17_no_snapshot_is_no_finding_and_another_account_is_not_an_observation():
    assert _i17([], [processed(1, 2500)])[1] == []
    other = obs(1, 9999)
    other["account"] = "secondary"
    assert _i17([other], [processed(1, 2500)])[1] == []  # S18 (I16 reports the account mismatch)


# ── G9-R5 / G9-V9: every real emitter's shape matches exactly one variant ─────────────────────────────────────

def test_g9_r5_the_real_emitters_cover_every_variant(monkeypatch):
    names = set()

    def collect(v):
        r = ok(v)
        names.update(variant_of(f, v.findings) for f in v.findings if f.cls in verify.VARIANT_CLASSES)
        return r

    for over in (_mixed(12), _pools((8, 8, 8, 9))):  # V-H3, V-H4
        refs = sorted(over)
        v = with_raw(make(events=ACTIVATED, history=[hist(1, "evaluation", 0, 2500, refs=refs)]), over)
        v.i12h(v.donations[DID], v.history)
        collect(v)
    v = make(mode="apply", current=LOSS, events=_damaged(), cut_rows=lambda d, c: LOSS,
             evidence=[("rfnd_A", T0 - timedelta(days=4)), ("disp_A", T0 - timedelta(days=4))],
             history=[hist(1, "evaluation", 0, 4000, refs=["disp_A", "rfnd_A"], at=T0 - timedelta(hours=12))])
    v.state_event_chain()
    v.i12h(v.donations[DID], v.history)
    collect(v)                                                                         # V-H2
    v = make(current=[refund(), refund("rfnd_B", 1000)], events=ACTIVATED,
             cut_rows=lambda d, c: [refund(), refund("rfnd_B", 1000)],
             history=[hist(1, "evaluation", 0, 3500, refs=["rfnd_A", "rfnd_B"])])
    v.raw_events = lambda did, ref: [ev_row(1, ref=ref), ev_row(2, ref=ref, amount=1000)]  # 2000 / 3500 / 5000
    v.i12h(v.donations[DID], v.history)
    collect(v)                                                                         # V-H1 (evidence_cut)
    collect((lambda k: (k.classify(), k)[1])(_keep(_one_event_refs(12))))              # V-A3
    collect((lambda k: (k.classify(), k)[1])(_keep({"disp_A": [lost_row(0), *[lost_row(i, donation=B)
                                                                              for i in range(1, 13)]]})))  # V-A2
    k = _keep(_one_event_refs(1), monkeypatch, fps=lambda sets: ["FP"] * (len(sets) - 1) + ["X"])
    k.classify()
    collect(k)                                                                         # V-A4
    k = _keep(_one_event_refs(1), monkeypatch, fps=lambda sets: ["FP"] * len(sets))
    k.classify()
    collect(k)                                                                         # V-A1
    for rows in ([ev_row(1, donation=None)], [ev_row(1, donation=B)],
                 [ev_row(1), ev_row(2, payment="pay_OTHER", status="pending")]):
        collect(_i16(rows)[0])                                                         # V-I1, V-I2, V-I3
    for args in (([obs(1, 500), obs(2, 800, hours=0)], [processed(1, 500)]),
                 ([obs(1, 500, None)], [processed(1, 500)]),
                 ([obs(1, 5000, hours=0), obs(2, 2500, hours=1)], [processed(1, 2500)]),
                 ([obs(1, 4000)], [processed(1, 2500), processed(2, 2000, status="pending")]),
                 ([obs(1, 2000)], [processed(1, 2500)]),
                 ([obs(1, 5000)], [processed(1, 2500), processed(2, None), processed(3, 1, None),
                                   processed(4, 1, "USD"), processed(5, 1, "JPY")])):
        collect(_i17(*args)[0])                                                        # V-S1-S6, V-R1-R4
    collect(_i17([obs(1, 1, "JPY")], [], currency="JPY")[0])                           # V-D1
    assert names == {v.name for vs in VARIANTS.values() for v in vs}, sorted(
        {v.name for vs in VARIANTS.values() for v in vs} - names)


# ── F7 / F8 (§2.6): R1-R5 ─────────────────────────────────────────────────────────────────────────────────────

class RecordingConn:
    autocommit = True

    def __init__(self):
        self.statements = []

    def cursor(self, *a, **kw):  # pragma: no cover - must never be reached for an invalid scope
        self.statements.append("cursor")
        raise AssertionError("no SQL may be issued")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("kw", [{"donation": ""}, {"creator": ""}, {"donation": "   "}, {"creator": "not-a-uuid"},
                                {"donation": "8d0c6a2e"}])
def test_r1_r3_an_explicit_empty_or_invalid_id_refuses_before_any_sql(kw):
    conn = RecordingConn()
    r = verify.verify(conn, **kw)
    assert (r["status"], r["exit_code"], r["gates"], conn.statements) == ("refused", 2, FALSE_GATES, [])
    assert r["scope"]["kind"] == next(iter(kw))  # never widened to full


def test_r1_both_scopes_refuse():
    r = verify.verify(RecordingConn(), creator=SID, donation=DID)
    assert r["status"] == "refused"


@pytest.mark.parametrize("flag", ["--donation", "--creator"])
def test_r2_the_cli_refuses_an_empty_id_with_exit_2_and_no_sql(monkeypatch, capsys, flag):
    conn = RecordingConn()
    monkeypatch.setenv("HYPERCHAT_READONLY_DSN", "host=unused")
    monkeypatch.setattr(operator.psycopg, "connect", lambda *a, **kw: conn)
    assert operator.main(["verify", flag, ""]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["scope"]["kind"] == flag[2:] and conn.statements == []
    args = SimpleNamespace(creator="" if flag == "--creator" else None,
                           donation="" if flag == "--donation" else None, output=None)
    monkeypatch.delenv("HYPERCHAT_READONLY_DSN")
    assert operator.run_verify(args) == 2
    assert json.loads(capsys.readouterr().out)["scope"]["kind"] == flag[2:]  # no DSN: the scope is still explicit


def test_r4_r5_finding_identity_includes_the_detail_and_is_order_independent():
    a = Finding("I11", "stale_hold", "donation", DID, {"problem": "x", "hold": DM})
    b = Finding("I11", "stale_hold", "donation", DID, {"problem": "y", "hold": DM})
    reports = []
    for order in ([a, b], [b, a], [a, b, a, Finding("I11", "stale_hold", "donation", DID, dict(a.detail))]):
        v = make()
        v.findings.extend(order)
        reports.append(ok(v)["findings"])
    assert len(reports[0]) == 2 and reports[0] == reports[1] == reports[2]  # R4: 2, ordered; R5: exact dups -> 1


# ══ Phase 2A (offline): I10 over captured catalogs, the §2.7 runtime, §2.7.4 publication, CLI scope ══════════════
# Captured-catalog rows are synthetic and migration-derived: they test the classifier, never the real PG15 / PG17
# catalog (that is Phase 2B, which also pins the baseline that is still I10_UNVERIFIED here).

import errno  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402

import psycopg  # noqa: E402

from app.payments.verify import (  # noqa: E402
    FN_ACL_BASELINE, FN_EXPECT, FUNCTION_PINS, MIGRATOR, OWNER, TRIGGER_PINS, Budget, Session, decode_tgtype,
    i10_problems, statement_allowed,
)

APP_ROLE_OIDS = {OWNER: 16400, MIGRATOR: 16401, "hyperchat_app": 16402, "hyperchat_worker": 16403,
                 "hyperchat_readonly": 16404, "hyperchat_lifecycle": 16405, "hyperchat_auth": 16406,
                 "hyperchat_stepup": 16407}
PREDEFINED = {"pg_write_all_data": 4200, "pg_read_all_data": 6181, "pg_database_owner": 6171, "pg_monitor": 3373,
              "pg_read_all_settings": 3374}


def role(name, oid, *, super_=False, inherit=True, login=True, **attrs):
    return {"oid": oid, "name": name, "super": super_, "inherit": inherit, "login": login,
            **{k: attrs.get(k, False) for k in ("createrole", "createdb", "replication", "bypassrls")}}


def edge(member, granted, admin=False, inherit=None, set_=None):
    return {"member": member, "role": granted, "admin_option": admin, "inherit_option": inherit, "set_option": set_}


def opts(version, inherit=True):
    """PG16+ edges carry options; PG15 edges have none (NULL in CAT_MEMBERS_15)."""
    return {"inherit": inherit, "set_": True} if version >= 160000 else {}


def tgtype(timing, level, events):
    bits = {"INSERT": 4, "DELETE": 8, "UPDATE": 16, "TRUNCATE": 32}
    return (1 if level == "ROW" else 0) | (2 if timing == "BEFORE" else 0) | sum(bits[e] for e in events)


def pristine(version=170000):
    """The migration-derived expectation of an unmodified 0034 catalog, as captured rows."""
    roles = [role("postgres", 10, super_=True)]
    roles += [role(n, o, inherit=n != MIGRATOR, login=n != OWNER) for n, o in APP_ROLE_OIDS.items()]
    roles += [role(n, o, login=False) for n, o in PREDEFINED.items()]
    members = [edge(APP_ROLE_OIDS[MIGRATOR], APP_ROLE_OIDS[OWNER], **opts(version, inherit=False)),
               edge(PREDEFINED["pg_monitor"], PREDEFINED["pg_read_all_settings"], **opts(version))]
    rights = [{"role": w, "rel": "donations", "priv": p} for w in ("hyperchat_app", "hyperchat_worker")
              for p in ("INSERT", "UPDATE", "DELETE")]
    rights += [{"role": "hyperchat_app", "rel": t, "priv": "INSERT"} for t in verify.APPEND_ONLY_EVIDENCE]
    rights += [{"role": x, "rel": "donations", "priv": "INSERT"} for x in (OWNER, "postgres", "pg_write_all_data")]
    fn_rights = [{"role": g, "name": n, "args": FN_EXPECT[n]["args"]} for n, gs in FN_ACL_BASELINE.items()
                 for g in gs if g != "PUBLIC"]
    everyone = ["public", "postgres", *APP_ROLE_OIDS, *PREDEFINED]
    fn_rights += [{"role": g, "name": "donations_require_eligible_creator", "args": ""} for g in everyone]
    fn_rights += [{"role": OWNER, "name": n, "args": FN_EXPECT[n]["args"]} for n in FN_EXPECT]
    acl = [{"name": n, "args": FN_EXPECT[n]["args"], "grantee": OWNER, "privilege": "EXECUTE", "grantable": False}
           for n in FN_EXPECT]
    acl += [{"name": n, "args": FN_EXPECT[n]["args"], "grantee": g, "privilege": "EXECUTE", "grantable": False}
            for n, gs in FN_ACL_BASELINE.items() for g in gs]
    return {
        "roles": roles, "members": members, "database_owner": OWNER,
        "relations": [{"rel": r, "kind": "r", "owner": OWNER, "rls": False, "force_rls": False, "policy": False}
                      for r in verify.READ_RELATIONS],
        "schema_owner": "pg_database_owner",
        "table_rights": rights, "column_rights": [], "function_rights": fn_rights,
        "schema_create": [{"role": OWNER}, {"role": "postgres"}, {"role": "pg_database_owner"}],
        "function_acl": acl,
        # A5b: the complete explicit ACLs of the pristine 0034 catalog (OD-5 values, A5a-R2 reconciled; version-keyed)
        "schema_acl": [{"grantee": g, "privilege": p, "grantable": False}
                       for g, p in sorted(verify.SCHEMA_ACL_BASELINE)],
        "table_acl": [{"rel": t, "grantee": g, "privilege": p, "grantable": False}
                      for t, g, p in sorted(verify.TABLE_ACL_BASELINE[version // 10000])],
        "column_acl": [],
        "triggers": [{"rel": rel, "name": name, "fn_schema": "public", "fn_name": fn, "fn_args": "",
                      "tgtype": tgtype(timing, level, events), "enabled": "O", "tgnargs": 0, "tgargs": "",
                      "deferrable": False, "initdeferred": False, "is_constraint": False, "no_qual": True,
                      "update_cols": []} for (rel, name), (fn, timing, level, events) in TRIGGER_PINS.items()],
        "functions": [{"schema": "public", "name": n, "args": e["args"], "returns": e["returns"],
                       "lanname": e["lanname"], "prokind": "f", "provolatile": e["provolatile"], "proparallel": "u",
                       "proisstrict": False, "proleakproof": False, "prosecdef": n in verify.DEFINER_FUNCTIONS,
                       "proconfig": list(e["proconfig"]) if e["proconfig"] else None, "owner": OWNER,
                       "src": FUNCTION_PINS.get(n, "unpinned"), "no_bin": True} for n, e in FN_EXPECT.items()],
    }


def problems(cat, version=170000, admins=frozenset()):
    return sorted(p for _, _, p, _ in i10_problems(cat, version, admins)[0])


def new_role(cat, name, oid, **kw):
    cat["roles"].append(role(name, oid, **kw))
    return oid


SIG_APPLY = "donation_accounting_apply(" + FN_EXPECT["donation_accounting_apply"]["args"] + ")"
SIG_RECONCILE = "payment_accounting_reconcile(" + FN_EXPECT["payment_accounting_reconcile"]["args"] + ")"
RECONCILE_ARGS = FN_EXPECT["payment_accounting_reconcile"]["args"]


@pytest.mark.parametrize("version", [150000, 170000])
def test_c0_unit_the_migration_derived_pristine_catalog_has_no_problem_but_the_baseline_stays_unverified(version):
    found, unverified = i10_problems(pristine(version), version)
    assert found == []
    assert unverified == [*verify.I10_UNVERIFIED, *verify.SKIPPED_COMPARISONS]
    assert verify.I10_BASELINE_CONFIRMED is False


def pristine_0035(version=170000):
    """The migration-derived pristine catalog at head 0035 (OD-35-V): 0034's, plus 0035's functions, pins and trigger."""
    cat = pristine(version)
    fn_expect, fn_pins, trigger_pins = verify.head_expectations("0035")
    for row in cat["functions"]:
        row["src"] = fn_pins.get(row["name"], row["src"])
    for n, e in verify.HEAD_DELTAS["0035"]["functions"].items():
        cat["functions"].append({"schema": "public", "name": n, "args": e["args"], "returns": e["returns"],
                                 "lanname": e["lanname"], "prokind": "f", "provolatile": e["provolatile"],
                                 "proparallel": "u", "proisstrict": False, "proleakproof": False, "prosecdef": False,
                                 "proconfig": list(e["proconfig"]), "owner": OWNER, "src": fn_pins[n], "no_bin": True})
        cat["function_rights"].append({"role": OWNER, "name": n, "args": e["args"]})
        cat["function_acl"].append({"name": n, "args": e["args"], "grantee": OWNER, "privilege": "EXECUTE",
                                    "grantable": False})
    for (rel, name), (fn, timing, level, events) in verify.HEAD_DELTAS["0035"]["triggers"].items():
        cat["triggers"].append({"rel": rel, "name": name, "fn_schema": "public", "fn_name": fn, "fn_args": "",
                                "tgtype": tgtype(timing, level, events), "enabled": "O", "tgnargs": 0, "tgargs": "",
                                "deferrable": False, "initdeferred": False, "is_constraint": False, "no_qual": True,
                                "update_cols": []})
    return cat


@pytest.mark.parametrize("version", [150000, 170000])
def test_od35v_the_pristine_0035_catalog_is_clean_at_head_0035_and_each_head_rejects_the_other(version):
    """OD-35-V: at head 0035 the 0035 objects are expected (no problem, the baseline still unverified, A5c blocked);
    the same catalog judged as 0034 shows the unexpected trigger and the changed bodies, and a 0034 catalog judged as
    0035 shows the missing objects: the head decides, never a guess."""
    found, unverified = i10_problems(pristine_0035(version), version, head="0035")
    assert found == [] and unverified == [*verify.I10_UNVERIFIED, *verify.SKIPPED_COMPARISONS]
    assert verify.I10_BASELINE_CONFIRMED is False
    as_0034 = sorted(p for _, _, p, _ in i10_problems(pristine_0035(version), version)[0])
    assert "unexpected_trigger" in as_0034 and as_0034.count("body_drift") == 4, as_0034
    as_0035 = sorted(p for _, _, p, _ in i10_problems(pristine(version), version, head="0035")[0])
    assert as_0035.count("missing") == 2 and "trigger_binding:missing" in as_0035 and as_0035.count("body_drift") == 4


def test_od35v_heads_and_pins_follow_the_migration_text():
    """The 0035 pins are md5 of the rendered prosrc of migration 0035 (as the 0034 ones are of 0034)."""
    import hashlib
    import importlib.util
    import pathlib
    import re
    import types
    versions = pathlib.Path(__file__).resolve().parents[3] / "database" / "migrations" / "versions"
    spec = importlib.util.spec_from_file_location("_m0035", versions / "0035_payment_accounting_hardening.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []
    module.op = types.SimpleNamespace(execute=statements.append)
    module.upgrade()
    bodies = {}
    for s in statements:
        m, b = re.search(r"CREATE (?:OR REPLACE )?FUNCTION public\.(\w+)\(", s), re.search(r"\$\$(.*)\$\$", s, re.S)
        if m and b:
            bodies[m.group(1)] = hashlib.md5(b.group(1).encode()).hexdigest()
    assert bodies == verify.HEAD_DELTAS["0035"]["pins"]
    assert verify.EXPECTED_HEADS == ("0034", "0035")


def test_i10_fails_closed_while_the_baseline_is_unverified(monkeypatch):
    v = make()
    v.catalog = pristine()
    v.i10()
    (f,) = v.findings
    assert (f.invariant, f.cls, f.detail["problem"]) == ("I10", "structural", "baseline_unverified")
    r = ok(v)
    assert r["status"] == "violation" and r["gates"] == FALSE_GATES  # never clean before Phase 2B pins it
    monkeypatch.setattr(verify, "I10_BASELINE_CONFIRMED", True)  # the flag alone never makes I10 clean:
    v = make()
    v.catalog = pristine()
    v.i10()
    (f,) = v.findings  # the comparisons not yet made at all are still reported
    assert f.detail["components"] == list(verify.SKIPPED_COMPARISONS)
    r = run(FakeConn())
    assert [x["detail"]["components"] for x in r["findings"] if x["invariant"] == "I10"] == [
        list(verify.SKIPPED_COMPARISONS)] and r["gates"] == FALSE_GATES


@pytest.mark.parametrize("version", [150000, 170000])
def test_c14_c16_paths_into_the_owner(version):
    cat = pristine(version)
    new_role(cat, "login_x", 17000)
    cat["members"].append(edge(17000, APP_ROLE_OIDS[OWNER], **opts(version)))
    assert problems(cat, version) == ["owner_path", "role_edge:login_x->hyperchat_owner"]          # C14
    cat = pristine(version)
    new_role(cat, "login_y", 17001)
    cat["members"].append(edge(17001, APP_ROLE_OIDS[MIGRATOR], **opts(version)))
    assert problems(cat, version) == ["owner_path", "role_edge:login_y->hyperchat_migrator"]      # C16


def test_c15_a_set_only_path_into_the_owner_pg16():
    cat = pristine(170000)
    new_role(cat, "login_s", 17002)
    cat["members"].append(edge(17002, APP_ROLE_OIDS[OWNER], inherit=False, set_=True))
    assert problems(cat) == ["owner_path", "role_edge:login_s->hyperchat_owner"]
    cat["members"][-1].update(set_option=False, inherit_option=False)  # a membership that grants nothing
    assert problems(cat) == ["role_edge:login_s->hyperchat_owner"]       # still default-deny


@pytest.mark.parametrize("attr,col", [("super", "rolsuper"), ("bypassrls", "rolbypassrls"),
                                      ("createrole", "rolcreaterole")])
def test_c17_application_role_attributes_are_checked_first(attr, col):
    cat = pristine()
    next(r for r in cat["roles"] if r["name"] == "hyperchat_app")[attr] = True
    assert problems(cat) == [f"role_attribute:{col}"]  # never also unexpected_superuser


def test_c18_a_predefined_role_path():
    cat = pristine()
    new_role(cat, "login_p", 17003)
    cat["members"].append(edge(17003, PREDEFINED["pg_write_all_data"], inherit=True, set_=True))
    assert problems(cat) == ["predefined_role_path:pg_write_all_data", "role_edge:login_p->pg_write_all_data"]


def test_c23_c24_edges_between_application_roles():
    cat = pristine(170000)  # C23: PG17 set-only readonly -> app
    cat["members"].append(edge(APP_ROLE_OIDS["hyperchat_readonly"], APP_ROLE_OIDS["hyperchat_app"], inherit=False,
                               set_=True))
    assert problems(cat) == ["role_edge:hyperchat_readonly->hyperchat_app"]
    cat = pristine(150000)  # C24: PG15 readonly -> NOINHERIT intermediary -> app
    new_role(cat, "relay", 17004, inherit=False, login=False)
    cat["members"] += [edge(APP_ROLE_OIDS["hyperchat_readonly"], 17004), edge(17004, APP_ROLE_OIDS["hyperchat_app"])]
    assert problems(cat, 150000) == ["role_edge:hyperchat_readonly->relay", "role_edge:relay->hyperchat_app"]


def test_c25_database_owner_and_c19_ownership():
    cat = pristine()
    cat["database_owner"] = "someone"
    next(r for r in cat["relations"] if r["rel"] == "donation_reversals")["owner"] = "hyperchat_app"
    cat["schema_owner"] = "hyperchat_app"
    name = "payment_accounting_keep"  # no grantee beyond the owner in the baseline
    next(f for f in cat["functions"] if f["name"] == name)["owner"] = "hyperchat_app"
    for a in cat["function_acl"]:  # ALTER FUNCTION ... OWNER TO rewrites the owner's own ACL entry
        if (a["name"], a["grantee"]) == (name, OWNER):
            a["grantee"] = "hyperchat_app"
    assert problems(cat) == sorted(["database_owner", "ownership:donation_reversals", "ownership:schema_public",
                                    f"ownership:{name}"])


def test_c31_a_member_of_the_app_role_inherits_its_donation_rights():
    cat = pristine()
    new_role(cat, "shadow", 17005)
    cat["members"].append(edge(17005, APP_ROLE_OIDS["hyperchat_app"], inherit=True, set_=True))
    cat["table_rights"] += [{"role": "shadow", "rel": "donations", "priv": p} for p in ("INSERT", "UPDATE")]
    assert problems(cat) == ["donations_write:INSERT:shadow", "donations_write:UPDATE:shadow",
                             "role_edge:shadow->hyperchat_app"]


@pytest.mark.parametrize("version", [150000, 170000])
def test_c34_c35_paths_to_an_approved_administrator(version):
    o = opts(version, inherit=False)
    cat = pristine(version)
    new_role(cat, "admin_x", 17010, super_=True)
    new_role(cat, "login_l", 17011)
    cat["members"].append(edge(17011, 17010, **o))
    assert problems(cat, version, frozenset({"admin_x"})) == ["admin_path:login_l->admin_x",
                                                              "role_edge:login_l->admin_x"]     # C34
    assert "unexpected_superuser" in problems(cat, version)                                     # not approved
    new_role(cat, "middle", 17012)
    cat["members"][-1] = edge(17011, 17012, **o)
    cat["members"].append(edge(17012, 17010, **o))
    assert problems(cat, version, frozenset({"admin_x"})) == [
        "admin_path:login_l->admin_x", "admin_path:middle->admin_x", "role_edge:login_l->middle",
        "role_edge:middle->admin_x"]                                                             # C35


def test_c36_the_allow_list_never_authorizes_an_application_role():
    cat = pristine()
    new_role(cat, "admin_x", 17020, super_=True)
    cat["members"].append(edge(APP_ROLE_OIDS["hyperchat_app"], 17020, inherit=True, set_=True))
    got = problems(cat, admins=frozenset({"admin_x", "hyperchat_app"}))
    assert got == ["admin_path:hyperchat_app->admin_x", "role_edge:hyperchat_app->admin_x"]


def test_the_bootstrap_superuser_and_approved_administrators_may_hold_memberships():
    cat = pristine()
    new_role(cat, "admin_x", 17030, super_=True)
    cat["members"] += [edge(10, APP_ROLE_OIDS[OWNER], inherit=True, set_=True),
                       edge(17030, APP_ROLE_OIDS["hyperchat_app"], inherit=True, set_=True)]
    assert problems(cat, admins=frozenset({"admin_x"})) == []
    assert problems(cat) == ["role_edge:admin_x->hyperchat_app", "unexpected_superuser"]


def test_the_migrator_edge_options_are_pinned():
    cat = pristine(170000)
    cat["members"][0]["admin_option"] = True
    assert problems(cat) == ["admin_option"]
    cat = pristine(170000)
    cat["members"][0]["inherit_option"] = True
    assert problems(cat) == ["role_edge:hyperchat_migrator->hyperchat_owner"]
    cat = pristine(150000)
    next(r for r in cat["roles"] if r["name"] == MIGRATOR)["inherit"] = True
    assert problems(cat, 150000) == ["role_attribute:rolinherit"]


def test_c28_c29_c30_the_minimum_donations_write_check():
    cat = pristine()
    new_role(cat, "login_c", 17040)
    cat["column_rights"].append({"role": "login_c", "rel": "donations", "col": "payment_status", "priv": "UPDATE"})
    assert problems(cat) == ["donations_column_write:UPDATE:payment_status:login_c"]                    # C28
    cat = pristine()
    cat["table_rights"].append({"role": "hyperchat_readonly", "rel": "donations", "priv": "INSERT"})
    assert problems(cat) == ["donations_write:INSERT:hyperchat_readonly"]                               # C29
    cat = pristine()
    cat["column_acl"].append({"rel": "donations", "col": "payment_status", "grantee": "PUBLIC", "privilege": "UPDATE",
                              "grantable": False})
    cat["column_rights"] += [{"role": r, "rel": "donations", "col": "payment_status", "priv": "UPDATE"}
                             for r in ("public", "hyperchat_readonly", "hyperchat_auth")]
    assert problems(cat) == ["donations_column_write:UPDATE:payment_status:PUBLIC",
                             "donations_column_write:UPDATE:payment_status:hyperchat_auth",
                             "donations_column_write:UPDATE:payment_status:hyperchat_readonly",
                             "public_column_update:payment_status"]                                     # C30
    cat = pristine()
    cat["table_rights"].append({"role": "hyperchat_app", "rel": "donations", "priv": "TRUNCATE"})
    assert problems(cat) == ["donations_write:TRUNCATE:hyperchat_app"]  # nobody truncates donations


def test_c8_c9_c10_protected_and_evidence_table_rights():
    cat = pristine()
    cat["column_rights"].append({"role": "hyperchat_app", "rel": "donation_reversals", "col": "applied_reversed_paise",
                                 "priv": "UPDATE"})
    assert problems(cat) == ["column_update_granted:applied_reversed_paise:hyperchat_app"]              # C8
    cat = pristine()
    new_role(cat, "login_n", 17050)
    cat["table_rights"].append({"role": "login_n", "rel": "payment_accounting_events", "priv": "INSERT"})
    assert problems(cat) == ["table_insert_granted:login_n"]                                            # C9
    cat = pristine()
    cat["table_acl"].append({"rel": "payment_accounting_runs", "grantee": "PUBLIC", "privilege": "INSERT",
                             "grantable": False})
    cat["table_rights"] += [{"role": r, "rel": "payment_accounting_runs", "priv": "INSERT"}
                            for r in ("public", "hyperchat_app")]
    assert problems(cat) == ["public_table_insert", "table_insert_granted:PUBLIC",
                             "table_insert_granted:hyperchat_app"]                                      # C10
    cat = pristine()
    cat["table_rights"].append({"role": "hyperchat_app", "rel": "payment_integrity_events", "priv": "UPDATE"})
    assert problems(cat) == ["table_update_granted:hyperchat_app"]  # evidence: the app inserts only


def test_c11_c12_c27_c32_c33_the_signature_exact_function_acl():
    cat = pristine()
    new_role(cat, "login_f", 17060)
    cat["function_rights"].append({"role": "login_f", "name": "payment_accounting_reconcile", "args": RECONCILE_ARGS})
    cat["function_acl"].append({"name": "payment_accounting_reconcile", "args": RECONCILE_ARGS, "grantee": "login_f",
                                "privilege": "EXECUTE", "grantable": False})
    assert problems(cat) == [f"function_acl:{SIG_RECONCILE}:login_f:EXECUTE"]                          # C11
    cat = pristine()
    cat["function_rights"] = [r for r in cat["function_rights"]
                              if (r["role"], r["name"]) != ("hyperchat_app", "donation_accounting_apply")]
    cat["function_acl"] = [r for r in cat["function_acl"]
                           if (r["grantee"], r["name"]) != ("hyperchat_app", "donation_accounting_apply")]
    assert problems(cat) == [f"function_acl_missing:{SIG_APPLY}:hyperchat_app"]                        # C12
    cat = pristine()
    cat["function_acl"].append({"name": "payment_accounting_reconcile", "args": RECONCILE_ARGS, "grantee": "PUBLIC",
                                "privilege": "EXECUTE", "grantable": False})
    cat["function_rights"] += [{"role": r, "name": "payment_accounting_reconcile", "args": RECONCILE_ARGS}
                               for r in ("public", "hyperchat_readonly")]
    assert problems(cat) == [f"function_acl:{SIG_RECONCILE}:PUBLIC:EXECUTE",
                             f"function_acl:{SIG_RECONCILE}:hyperchat_readonly:EXECUTE", "public_execute",
                             f"public_function_execute:{SIG_RECONCILE}"]                                # C27
    cat = pristine()
    next(r for r in cat["function_acl"]
         if (r["grantee"], r["name"]) == ("hyperchat_app", "donation_accounting_apply"))["grantable"] = True
    assert problems(cat) == [f"grant_option:{SIG_APPLY}:hyperchat_app"]                                # C32
    cat = pristine()
    cat["function_rights"] = [r for r in cat["function_rights"]
                              if not (r["name"] == "donations_require_eligible_creator" and r["role"] == "public")]
    cat["function_acl"] = [r for r in cat["function_acl"]
                           if not (r["name"] == "donations_require_eligible_creator" and r["grantee"] == "PUBLIC")]
    assert problems(cat) == ["function_acl_missing:donations_require_eligible_creator():PUBLIC"]       # C33


def test_c13_c21_schema_create_and_grant_options():
    cat = pristine()
    new_role(cat, "login_k", 17070)
    cat["schema_create"].append({"role": "login_k"})
    assert problems(cat) == ["schema_create:login_k"]                                                   # C13
    cat = pristine()
    for x in cat["table_acl"]:  # A5b: the fixture now carries the baseline entry; C21 re-grants it WITH GRANT OPTION
        if (x["rel"], x["grantee"], x["privilege"]) == ("donation_reversals", "hyperchat_app", "SELECT"):
            x["grantable"] = True
    cat["schema_acl"].append({"grantee": "PUBLIC", "privilege": "CREATE", "grantable": False})
    assert problems(cat) == ["grant_option:donation_reversals:hyperchat_app", "public_schema_create"]   # C21


@pytest.mark.parametrize("change,problem", [
    ({"fn_name": "set_updated_at"}, "trigger_binding:function"),                                         # C1
    ({"tgtype": tgtype("AFTER", "ROW", {"INSERT"})}, "trigger_binding:type"),                           # C2
    ({"no_qual": False}, "trigger_binding:when"),                                                       # C3
    ({"enabled": "D"}, "trigger_binding:enabled"),
    ({"tgnargs": 1, "tgargs": "78"}, "trigger_binding:args"),
    ({"deferrable": True}, "trigger_binding:constraint"),
    ({"update_cols": ["payment_status"]}, "trigger_binding:columns"),
])
def test_c1_c3_trigger_binding(change, problem):
    cat = pristine()
    next(t for t in cat["triggers"] if t["name"] == "payment_accounting_runs_guard").update(change)
    assert problems(cat) == [problem]


def test_c6_an_extra_trigger_and_a_missing_one():
    cat = pristine()
    cat["triggers"].append({**cat["triggers"][0], "rel": "streamer_donator_totals", "name": "sneaky"})
    cat["triggers"] = [t for t in cat["triggers"] if t["name"] != "donations_credit_writer"]
    assert problems(cat) == ["trigger_binding:missing", "unexpected_trigger"]


@pytest.mark.parametrize("change,problem", [
    ({"proisstrict": True}, "function_attr:proisstrict"),                                                # C5
    ({"src": "0" * 32}, "body_drift"),
    ({"prosecdef": False}, "security"),
    ({"proconfig": ["search_path=public"]}, "search_path"),
    ({"returns": "void"}, "function_attr:returns"),
    ({"provolatile": "i"}, "function_attr:provolatile"),
    ({"no_bin": False}, "function_attr:probin"),
])
def test_c5_function_identity(change, problem):
    cat = pristine()
    next(f for f in cat["functions"] if f["name"] == "payment_accounting_keep").update(change)
    assert problems(cat) == [problem]


def test_c7_an_overload_lock_timeout_and_a_missing_function():
    cat = pristine()
    keep = next(f for f in cat["functions"] if f["name"] == "payment_accounting_keep")
    cat["functions"].append({**keep, "args": "p_donation text"})
    assert "overloaded" in problems(cat)
    cat = pristine()
    next(f for f in cat["functions"] if f["name"] == "payment_accounting_set_state")["proconfig"] = list(verify._SP)
    assert problems(cat) == ["lock_timeout"]
    cat = pristine()
    cat["functions"] = [f for f in cat["functions"] if f["name"] != "payment_evidence_fp"]
    assert "missing" in problems(cat)


def test_decode_tgtype():
    assert decode_tgtype(tgtype("BEFORE", "ROW", {"UPDATE", "DELETE"})) == ("BEFORE", "ROW",
                                                                            frozenset({"UPDATE", "DELETE"}))
    assert decode_tgtype(32 | 2) == ("BEFORE", "STATEMENT", frozenset({"TRUNCATE"}))
    assert decode_tgtype(4 | 1) == ("AFTER", "ROW", frozenset({"INSERT"}))


# ── §2.7 runtime: a scripted in-process connection and a controlled clock (no database) ──

class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def first_row(version=170000):
    return {"t_v": T0, "c_v": T0, "snapshot": "1:1:", "isolation": "repeatable read", "read_only": "on",
            "session_user": "hyperchat_readonly", "current_user": "hyperchat_readonly", "database": "x",
            "server_version_num": version, "head": "0034"}


class FakeCursor:
    """Answers the approved constants from the connection's table; DECLARE / FETCH page through them."""

    def __init__(self, conn):
        self.conn, self.result, self.open_cursors = conn, [], {}

    def execute(self, stmt, params=None):
        c = self.conn
        c.log.append(stmt)
        c.clock.t += c.step
        if c.fail_on and c.fail_on(stmt):
            raise c.fail_exc
        self.result = []
        if stmt.startswith("BEGIN"):
            c.status = "INTRANS"
        elif stmt == "ROLLBACK":
            c.clock.t += c.rollback_cost
            if c.rollback_raises:
                raise RuntimeError("rollback failed")
            c.status = "INTRANS" if c.stuck else "IDLE"
        elif stmt.startswith("DECLARE"):
            name, sql = stmt.split(" ", 2)[1], stmt.split(" FOR ", 1)[1]
            self.open_cursors[name] = list(c.answers.get(sql, []))
        elif stmt.startswith("FETCH"):
            n, name = int(stmt.split()[2]), stmt.split()[-1]
            self.result, self.open_cursors[name] = self.open_cursors[name][:n], self.open_cursors[name][n:]
            c.served.append(self.result)
        elif not stmt.startswith(("SET", "CLOSE")):
            self.result = list(c.answers.get(stmt, []))

    def fetchall(self):
        return self.result

    def close(self):
        pass


class FakeConn:
    autocommit = True

    def __init__(self, *, answers=None, version=170000):
        self.clock, self.step, self.log, self.closed, self.status = Clock(), 0.001, [], False, "IDLE"
        self.served = []
        self.rollback_cost, self.rollback_raises, self.stuck, self.fail_on, self.fail_exc = 0.0, False, False, None, None
        cat = pristine(version)
        self.answers = {
            verify.FIRST: [first_row(version)], verify.STATE: [{"state": "off", "dispute_mode": "hold"}],
            verify.CAT_ROLES: cat["roles"], verify.CAT_MEMBERS_16: cat["members"], verify.CAT_MEMBERS_15: cat["members"],
            verify.CAT_DATABASE: [{"owner": OWNER}], verify.CAT_RELATIONS: cat["relations"],
            verify.CAT_SCHEMA: [{"owner": cat["schema_owner"]}], verify.CAT_TABLE_RIGHTS: cat["table_rights"],
            verify.CAT_FUNCTION_RIGHTS: cat["function_rights"], verify.CAT_SCHEMA_CREATE: cat["schema_create"],
            verify.CAT_FUNCTION_ACL: cat["function_acl"], verify.CAT_SCHEMA_ACL: cat["schema_acl"],
            verify.CAT_TABLE_ACL: cat["table_acl"], verify.CAT_TRIGGERS: cat["triggers"],
            verify.CAT_FUNCTIONS: cat["functions"], **(answers or {})}

    def cursor(self, **kw):
        return FakeCursor(self)

    @property
    def info(self):
        return SimpleNamespace(transaction_status=getattr(verify.TransactionStatus, self.status))

    def close(self):
        self.closed = True


def run(conn, max_seconds=60.0, **kw):
    return verify.verify(conn, max_seconds=max_seconds, clock=conn.clock, **kw)


def test_r15_unit_a_full_run_issues_only_the_approved_protocol_and_fails_closed_on_the_baseline():
    conn = FakeConn(answers={verify.ALL_EVENTS: [ev_row(i) for i in range(3)]})
    r = run(conn)
    assert r["status"] == "violation" and r["gates"] == FALSE_GATES  # baseline_unverified: I10 fails closed
    assert [f["detail"]["problem"] for f in r["findings"] if f["invariant"] == "I10"] == ["baseline_unverified"]
    log = conn.log
    assert log[:3] == ["BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY", "SET LOCAL lock_timeout = '5s'",
                       "SET LOCAL row_security = off"] and log[-1] == "ROLLBACK"
    assert all(statement_allowed(x) for x in log)
    assert not [x for x in log if x in verify._APPROVED_SQL]  # no data statement is ever issued bare
    data = [i for i, x in enumerate(log) if x.startswith(("DECLARE", "FETCH"))]
    assert data and all(log[i - 1].startswith("SET LOCAL statement_timeout") for i in data)  # each one capped
    names = [x.split()[1] for x in log if x.startswith("DECLARE")]
    assert names == [f"vfy_{i}" for i in range(1, len(names) + 1)]
    assert sorted(x.split()[1] for x in log if x.startswith("CLOSE")) == sorted(names)
    assert declared(conn)[:2] == [verify.FIRST, verify.CAT_RELATIONS]  # §5.6: row security before any read
    assert conn.status == "IDLE" and not conn.closed


def test_r15_unit_an_unapproved_statement_is_refused_before_it_is_issued():
    assert not statement_allowed("SELECT 1")
    assert not statement_allowed("DECLARE vfy_1 NO SCROLL CURSOR WITHOUT HOLD FOR SELECT 1")
    assert statement_allowed(f"DECLARE vfy_1 NO SCROLL CURSOR WITHOUT HOLD FOR {verify.FP_SETS}")  # MOD: batched too
    assert not statement_allowed(f"DECLARE vfy_1 NO SCROLL CURSOR WITHOUT HOLD FOR {verify.FP_SETS} ")
    assert not statement_allowed("SET LOCAL statement_timeout = '0ms'")
    assert not statement_allowed("SET LOCAL statement_timeout = 0")
    assert not statement_allowed("FETCH FORWARD 10 FROM other")
    assert not statement_allowed("ROLLBACK; DROP TABLE x")
    conn = FakeConn()
    s = Session(conn.cursor(), Budget(60, conn.clock))
    with pytest.raises(Refused):
        verify._select(s, "SELECT 1")
    with pytest.raises(Refused):
        s.protocol("COMMIT")
    assert conn.log == []


@pytest.mark.parametrize("version,issued,never", [(150000, "CAT_MEMBERS_15", "CAT_MEMBERS_16"),
                                                    (170000, "CAT_MEMBERS_16", "CAT_MEMBERS_15")])
def test_the_membership_query_follows_the_server_version(version, issued, never):
    conn = FakeConn(version=version)
    run(conn)
    assert getattr(verify, issued) in declared(conn) and getattr(verify, never) not in declared(conn)


def test_every_executable_module_query_is_approved():
    """An approved-set omission would refuse every real run; the scripted run never reaches CUT_ROWS, FP_CUT,
    FP_SETS, REF_EVENTS or EVIDENCE_CUTS, so the set is checked against the module itself."""
    queries = {n: v for n, v in vars(verify).items() if n.isupper() and not n.startswith("_") and isinstance(v, str)
               and v.lstrip().split(None, 1)[0].upper() in ("SELECT", "WITH")}
    assert {"CUT_ROWS", "FP_CUT", "FP_SETS", "REF_EVENTS", "EVIDENCE_CUTS", "CAT_TRIGGERS"} <= set(queries)
    assert [n for n, v in queries.items() if v not in verify._APPROVED_SQL] == []
    assert not hasattr(verify, "_CURSORED")  # correction 1: every approved constant is read through a cursor


def test_all_events_is_fetched_in_bounded_batches(monkeypatch):
    monkeypatch.setattr(verify, "FETCH_BATCH", 5)
    conn = FakeConn(answers={verify.ALL_EVENTS: [ev_row(i) for i in range(12)]})
    s = Session(conn.cursor(), Budget(60, conn.clock))
    assert len(verify._select(s, verify.ALL_EVENTS)) == 12
    assert [x for x in conn.log if x.startswith(("FETCH", "CLOSE"))] == ["FETCH FORWARD 5 FROM vfy_1"] * 3 + [
        "CLOSE vfy_1"]


@pytest.mark.parametrize("ceiling,value", [("MAX_ROWS", 10), ("MAX_BYTES", 2_000)])
def test_r9_a_row_or_byte_ceiling_refuses_and_still_rolls_back(monkeypatch, ceiling, value):
    monkeypatch.setattr(verify, ceiling, value)
    conn = FakeConn(answers={verify.ALL_EVENTS: [ev_row(i) for i in range(50)]})
    r = run(conn)
    assert (r["status"], r["exit_code"], r["gates"], r["findings"]) == ("refused", 2, FALSE_GATES, [])
    assert "ceiling" in r["reason"] and conn.log[-1] == "ROLLBACK" and conn.status == "IDLE"


def test_r7_the_work_ceiling_and_cpu_checkpoints(monkeypatch):
    clock = Clock()
    b = Budget(10, clock)
    b.work(5)
    clock.t += 11
    b.work(10)  # past the deadline, but no checkpoint boundary crossed yet
    with pytest.raises(Refused, match="deadline"):
        b.work(verify.CHECKPOINT_EVERY)  # refused at the next checkpoint
    monkeypatch.setattr(verify, "MAX_WORK", 100)
    with pytest.raises(Refused, match="work ceiling"):
        Budget(10, Clock()).work(101)
    v = with_raw(make(events=ACTIVATED, history=[hist(1, "evaluation", 0, 1000,
                                                      refs=["rfnd_A", "rfnd_B", "rfnd_C", "rfnd_D"])]),
                 {f"rfnd_{c}": [ev_row(10 * n + i, ref=f"rfnd_{c}", amount=100 + i) for i in range(8)]
                  for n, c in enumerate("ABCD")})
    v.budget = Budget(10, Clock())
    with pytest.raises(Refused, match="work ceiling"):  # the I12-H enumeration counts its work
        v.i12h(v.donations[DID], v.history)


def test_r1b_a_statement_with_less_than_50_ms_left_is_refused_before_issuing():
    conn = FakeConn()
    s = Session(conn.cursor(), Budget(0.08, conn.clock))
    conn.clock.t += 0.035  # 45 ms left
    with pytest.raises(Refused, match="50 ms"):
        verify._select(s, verify.STATE)
    assert conn.log == []
    conn = FakeConn()
    s = Session(conn.cursor(), Budget(300, conn.clock))
    verify._select(s, verify.STATE)
    assert conn.log[0] == "SET LOCAL statement_timeout = '120000ms'"  # min(120 000, remaining)


def test_r8b_a_deadline_crossed_during_rollback_refuses_after_the_release():
    conn = FakeConn()
    conn.rollback_cost = 120.0
    r = run(conn, max_seconds=60)
    assert (r["status"], r["gates"], r["findings"]) == ("refused", FALSE_GATES, [])
    assert "after the snapshot release" in r["reason"] and conn.log[-1] == "ROLLBACK" and conn.status == "IDLE"


def test_r8c_cleanup_runs_whatever_time_remains():
    conn = FakeConn()
    conn.step = 0.0
    s = Session(conn.cursor(), Budget(1.0, conn.clock))
    verify._select(s, verify.DONATIONS)
    conn.clock.t += 0.99  # under 50 ms left: the next data statement refuses ...
    with pytest.raises(Refused):
        verify._select(s, verify.STREAMERS)
    assert conn.log[-1] == "CLOSE vfy_1"  # the earlier cursor closed although time was short
    assert verify._release(conn, s) and conn.log[-1] == "ROLLBACK"  # ... and ROLLBACK still runs


def test_a_failed_rollback_closes_the_connection_and_an_unconfirmed_release_refuses():
    conn = FakeConn()
    conn.rollback_raises = True
    r = run(conn)
    assert conn.closed and r["status"] != "refused"  # released by closing (§2.7.1 step 3): the report stands
    conn = FakeConn()
    conn.stuck = True
    r = run(conn)
    assert (r["status"], r["findings"]) == ("refused", []) and "could not be confirmed" in r["reason"] and conn.closed


@pytest.mark.parametrize("bad", [0, -1, math.inf, math.nan, True, "60", None])
def test_r14_max_seconds_must_be_finite_and_positive(bad):
    conn = FakeConn()
    r = verify.verify(conn, max_seconds=bad, clock=conn.clock)
    assert (r["status"], r["exit_code"], conn.log) == ("refused", 2, [])


def test_c20_row_security_refuses_the_whole_report_before_any_financial_read():
    conn = FakeConn()
    next(x for x in conn.answers[verify.CAT_RELATIONS] if x["rel"] == "payment_integrity_events")["policy"] = True
    r = run(conn)
    assert (r["status"], r["reason"], r["findings"]) == ("refused", "row_security: payment_integrity_events", [])
    assert declared(conn) == [verify.FIRST, verify.CAT_RELATIONS] and conn.log[-1] == "ROLLBACK"  # no data read


@pytest.mark.parametrize("exc,reason", [
    (psycopg.errors.QueryCanceled("x"), "statement timeout"),
    (psycopg.errors.LockNotAvailable("x"), "lock timeout"),
    (psycopg.errors.InsufficientPrivilege("query would be affected by row-level security policy"),
     "row_security: read_filtered"),
])
def test_database_errors_refuse_and_still_roll_back(exc, reason):
    conn = FakeConn()
    conn.fail_on, conn.fail_exc = (lambda x: x.startswith("DECLARE")), exc
    r = run(conn)
    assert r["status"] == "refused" and r["reason"].startswith(reason) and conn.log[-1] == "ROLLBACK"


def test_the_report_names_the_administrators_and_the_provisional_ceilings():
    r = run(FakeConn(), admins=frozenset({"dba_x"}), admin_list_sha256="ab" * 32)
    assert r["administrators"] == {"allow_list_sha256": "ab" * 32, "approved": ["dba_x"]}
    assert r["runtime"]["provisional_ceilings"]["max_rows"] == verify.MAX_ROWS


# ── Phase 2A correction 1, blocker 1: nested byte accounting, bounded batches, checkpoints, report size, counters ──

def declared(conn) -> list:
    """The SQL constants a run declared cursors over, in order."""
    return [x.split(" FOR ", 1)[1] for x in conn.log if x.startswith("DECLARE")]


class TickClock(Clock):
    """Every reading advances time: a deadline is crossed during CPU work, not only between statements."""

    def __init__(self, step=0.01):
        super().__init__()
        self.tick = step

    def __call__(self):
        self.t += self.tick
        return self.t


def test_c1_nested_content_counts_against_the_byte_ceiling(monkeypatch):
    row = {"refs": ["x" * 1_000_000]}
    shallow = sum(__import__("sys").getsizeof(v) for v in row.values())
    assert shallow < 200 < verify._value_bytes(row) and verify._value_bytes(row) > 1_000_000  # the Codex reproducer
    nested_json = {"j": {"a": {"b": ["y" * 1_000_000]}}}
    assert verify._value_bytes(nested_json) > 1_000_000
    cycle = []
    cycle.append(cycle)
    assert verify._value_bytes(cycle) < 1_000  # each object once: a cycle terminates
    many = ["z" * 100] * 1 + [f"s{i}" for i in range(100_000)]
    assert 1_000 < verify._value_bytes(many, limit=1_000) < verify._value_bytes(many)  # stops at the limit
    monkeypatch.setattr(verify, "MAX_BYTES", 200)
    for value in (row, nested_json):
        conn = FakeConn(answers={verify.STATE: [value]})
        s = Session(conn.cursor(), Budget(60, conn.clock))
        with pytest.raises(Refused, match="byte ceiling"):
            verify._select(s, verify.STATE)
        assert s.budget.rows == 1 and s.budget.bytes > 200


def test_c1_a_parameterised_catalog_query_is_fetched_in_bounded_batches(monkeypatch):
    monkeypatch.setattr(verify, "FETCH_BATCH", 4)
    monkeypatch.setattr(verify, "MAX_ROWS", 10)
    rows = [{"role": f"r{i}", "rel": "donations", "priv": "INSERT"} for i in range(50)]
    conn = FakeConn(answers={verify.CAT_TABLE_RIGHTS: rows})
    s = Session(conn.cursor(), Budget(60, conn.clock))
    with pytest.raises(Refused, match="row ceiling"):
        verify._select(s, verify.CAT_TABLE_RIGHTS, {"rels": list(verify.RIGHTS_RELS)})
    assert declared(conn) == [verify.CAT_TABLE_RIGHTS]  # DECLAREd with its parameters, never a bare fetchall
    fetches = [int(x.split()[2]) for x in conn.log if x.startswith("FETCH")]
    assert fetches == [4, 4, 3] and sum(len(b) for b in conn.served) == 11  # never more than one row past
    assert s.budget.rows == 11


def _budgeted(v, max_seconds=5.0):
    v.budget = Budget(max_seconds, TickClock(1.0))
    return v


def test_c1_a_deadline_crossed_inside_i16_refuses_at_the_next_checkpoint(monkeypatch):
    monkeypatch.setattr(verify, "CHECKPOINT_EVERY", 1)
    v = _evidence(make(), [ev_row(i, ref=f"rfnd_X{i}", donation=None) for i in range(50)])
    _budgeted(v)
    with pytest.raises(Refused, match="deadline"):
        v.evidence_attribution()
    assert len(v.findings) < 50  # stopped inside the loop, not after it


def test_c1_a_deadline_crossed_inside_i17_refuses_at_the_next_checkpoint(monkeypatch):
    monkeypatch.setattr(verify, "CHECKPOINT_EVERY", 1)
    v = _evidence(make(), [obs(i, 1000 + i, hours=i) for i in range(50)] + [processed(1, 2500)])
    _budgeted(v)
    with pytest.raises(Refused, match="deadline"):
        v.refund_snapshots()


def test_c1_report_expansion_beyond_the_byte_ceiling_refuses(monkeypatch):
    v = make()
    for i in range(200):
        v.findings.append(Finding("I11", "stale_hold", "donation", f"d{i}", {"problem": "x" * 500, "hold": DM}))
    assert ok(v)["status"] == "violation"  # within the ceiling: reported
    monkeypatch.setattr(verify, "MAX_BYTES", 50_000)
    r = rep(v)
    assert (r["status"], r["exit_code"], r["gates"], r["findings"]) == ("refused", 2, FALSE_GATES, [])
    assert r["reason"] == "report byte ceiling exceeded"
    with pytest.raises(Refused):  # the exact canonical size is streamed and refused before serialization
        verify.canonical_size({"x": ["y" * 1000] * 100}, 10_000)
    assert verify.canonical_size({"a": [1, "b"]}, 100) == len(verify.canonical({"a": [1, "b"]}))


def test_c1_dedup_sorting_and_blockers_count_against_the_work_ceiling(monkeypatch):
    v = make()
    for i in range(500):
        v.findings.append(Finding("I11", "stale_hold", "donation", f"d{i}", {"problem": "p"}))
    monkeypatch.setattr(verify, "MAX_WORK", 600)  # the dedup fits, the sort's estimate does not
    v.budget = Budget(60, Clock())
    with pytest.raises(Refused, match="work ceiling"):
        v.report()
    monkeypatch.setattr(verify, "MAX_WORK", 200_000_000)
    v.excluded, v.baselines, v.orphan_buckets, v.orphan_rows = [], [], 0, 0
    v.budget = Budget(60, TickClock(30.0))  # a deadline crossed during report construction (third checkpoint)
    with pytest.raises(Refused, match="deadline"):
        v.report()


def test_c1_the_report_carries_the_actual_counters():
    conn = FakeConn(answers={verify.ALL_EVENTS: [ev_row(i) for i in range(7)]})
    r = run(conn)
    served = [row for batch in conn.served for row in batch]
    assert r["runtime"]["rows_loaded"] == len(served) > 7
    assert r["runtime"]["bytes_loaded"] == sum(verify._value_bytes(x) for x in served)
    small = r["runtime"]["work_units"]
    bigger = run(FakeConn(answers={verify.ALL_EVENTS: [ev_row(i) for i in range(200)]}))["runtime"]
    assert 0 < small and bigger["work_units"] >= small + 193 and bigger["rows_loaded"] == len(served) + 193
    assert rep(make())["runtime"]["rows_loaded"] == 0  # a unit-test verifier loads nothing


def test_c1_a_refusal_during_processing_still_rolls_back_and_releases(monkeypatch):
    monkeypatch.setattr(verify, "CHECKPOINT_EVERY", 1)
    conn = FakeConn(answers={verify.ALL_EVENTS: [ev_row(i, ref=f"rfnd_Y{i}") for i in range(400)]})
    conn.step = 0.0
    clock = TickClock(0.002)
    conn.clock = clock
    r = verify.verify(conn, max_seconds=2.0, clock=clock)
    assert (r["status"], r["gates"], r["findings"]) == ("refused", FALSE_GATES, [])
    assert "deadline" in r["reason"] and conn.log[-1] == "ROLLBACK" and conn.status == "IDLE"
    assert any(x.startswith("FETCH") for x in conn.log)  # it was refused during the run, not before it


# ── Phase 2A correction 2: checkpoints INSIDE one hot collection (bulk precharging would not catch these) ──

HOT = 3  # hot collections hold HOT x CHECKPOINT_EVERY items


class TickOnKey(dict):
    """A row that advances `clock` the `after`-th time `key` is read: the deadline passes mid-scan."""

    def __init__(self, data, key, clock, counter, after):
        super().__init__(data)
        self._hook = (key, clock, counter, after)

    def __getitem__(self, k):
        key, clock, counter, after = self._hook
        if k == key:
            counter.append(1)
            if len(counter) == after:
                clock.t += 1_000
        return super().__getitem__(k)


def test_c2_1_i16_one_hot_reference_is_checkpointed_inside_its_winner_scan(monkeypatch):
    n = HOT * verify.CHECKPOINT_EVERY
    v = _evidence(make(), [ev_row(i, ref="rfnd_HOT", status="pending", donation=None) for i in range(n)])
    clock = Clock()
    v.budget = Budget(10, clock)
    calls, real = [], verify._rank_key

    def rank(e):  # the deadline passes during this ONE reference's _rank_key scan
        calls.append(1)
        if len(calls) == 10:
            clock.t += 1_000
        return real(e)
    monkeypatch.setattr(verify, "_rank_key", rank)
    with pytest.raises(Refused, match="deadline"):
        v.evidence_attribution()
    assert 10 <= len(calls) <= 10 + verify.CHECKPOINT_EVERY < n  # refused within one checkpoint interval


def test_c2_2_i17_one_hot_snapshot_group_is_checkpointed_inside_snapshot_one():
    n = HOT * verify.CHECKPOINT_EVERY
    clock, reads = Clock(), []
    observations = [TickOnKey(obs(i, 5000), "snapshot", clock, reads, after=10) for i in range(n)]  # ONE time group
    v = make()
    v.budget = Budget(10, clock)
    with pytest.raises(Refused, match="deadline"):
        v.snapshot_one(v.donations[DID], observations, [processed(1, 5000)], [])
    assert 10 <= len(reads) <= 10 + verify.CHECKPOINT_EVERY < n  # stopped inside the group's own scan


def test_c2_3_i10_one_large_edge_scan_is_checkpointed_inside_reach():
    """Codex's case: ONE member with a huge edge list (a single scan inside reach()), not many small scans."""
    n = HOT * verify.CHECKPOINT_EVERY
    cat = pristine(170000)
    clock, reads = Clock(), []
    new_role(cat, "hub", 17_100)
    for i in range(n):
        new_role(cat, f"leaf_{i}", 17_101 + i)
    cat["members"] += [TickOnKey(edge(17_100, 17_101 + i, inherit=True, set_=True), "set_option", clock, reads,
                                 after=10) for i in range(n)]
    budget = Budget(10, clock)
    admins = frozenset(f"leaf_{i}" for i in range(n))  # only the hub walks its edges
    with pytest.raises(Refused, match="deadline"):
        i10_problems(cat, 170000, admins, work=budget.work)
    assert 10 <= len(reads) <= 10 + verify.CHECKPOINT_EVERY < n  # refused inside the one edge scan
    found, _ = i10_problems(cat, 170000, admins)  # without a deadline the same graph classifies normally
    assert sum(1 for _, e, p, _ in found if p.startswith("admin_path:hub->")) == n


def _a1_replica_findings(n, drop=None, mismatch=None):
    out = []
    for i in range(n):
        did = f"don-{i:05d}"
        out.append(Finding("I12", "historical_ambiguous", "history", str(i),
                           {**A4, "donation": did, "reason": "ack_mode_order"}))
        if i != drop:
            out.append(Finding("I10", "structural", "donation", "elsewhere" if i == mismatch else did,
                               {"problem": "fp_replica_mismatch"}))
    return out


def test_c2_4_a1_cross_check_is_indexed_once_bounded_and_fail_closed(monkeypatch):
    n = 2_000
    v = make()
    v.findings.extend(_a1_replica_findings(n))
    v.excluded, v.baselines, v.orphan_buckets, v.orphan_rows = [], [], 0, 0
    built, real_index = [], verify.replica_index
    monkeypatch.setattr(verify, "replica_index", lambda fs, each=iter: built.append(1) or real_index(fs, each))
    v.budget = Budget(60, Clock())
    r = v.report()
    assert built == [1]  # one indexed pass, not a scan of every finding per A1 finding
    assert r["status"] == "violation" and r["gates"] == FALSE_GATES
    assert sum(1 for b in r["write_blockers"] if b["blocker"] == "a1_replica_failed") == n
    def units(k):
        w = make()
        w.findings.extend(_a1_replica_findings(k))
        w.excluded, w.baselines, w.orphan_buckets, w.orphan_rows = [], [], 0, 0
        w.budget = Budget(60, Clock())
        w.report()
        return w.budget.units
    assert units(2 * n) < 2.2 * units(n)  # linear: the per-finding scan of every finding grew as n squared
    for kw in ({"drop": 12}, {"mismatch": 7}):  # a missing or mismatched replica finding still refuses
        bad = make()
        bad.findings.extend(_a1_replica_findings(50, **kw))
        assert len(bad.findings) == 100 - ("drop" in kw)
        assert rep(bad)["reason"] == "cross-check failed for V-A4"
    late = make()  # and the cross-check pass itself is checkpointed
    late.findings.extend(_a1_replica_findings(n))
    late.excluded, late.baselines, late.orphan_buckets, late.orphan_rows = [], [], 0, 0
    late.budget = Budget(10, TickClock(1.0))
    with pytest.raises(Refused, match="deadline"):
        late.report()


def test_c2_5_a_deadline_inside_one_hot_reference_refuses_the_full_run_and_releases(monkeypatch):
    n = HOT * verify.CHECKPOINT_EVERY
    conn = FakeConn(answers={verify.ALL_EVENTS: [ev_row(i, ref="rfnd_HOT", donation=None) for i in range(n)]})
    calls, real = [], verify._rank_key

    def rank(e):
        calls.append(1)
        if len(calls) == 10:
            conn.clock.t += 1_000  # the deadline passes inside I16's scan of the one hot reference
        return real(e)
    monkeypatch.setattr(verify, "_rank_key", rank)
    r = run(conn, max_seconds=60)
    assert (r["status"], r["exit_code"], r["gates"], r["findings"]) == ("refused", 2, FALSE_GATES, [])
    assert r["reason"] == "deadline exceeded" and len(calls) <= 10 + verify.CHECKPOINT_EVERY < n
    assert conn.log[-1] == "ROLLBACK" and conn.status == "IDLE" and not conn.closed  # released, confirmed


def test_c2_every_scan_charges_as_it_iterates_never_in_bulk():
    """Budget.each charges one unit per item produced: stopping early charges only what was consumed."""
    b = Budget(60, Clock())
    it = b.each(range(10_000))
    for _ in zip(range(5), it):
        pass
    assert b.units == 5
    assert verify._NoBudget().each([1, 2]) == [1, 2]



# ── Phase 2A correction 3: the four remaining checkpoint gaps ──

class CountStr(str):
    """A role name that counts how often it is formatted into a finding AFTER the clock jumped (emission)."""

    clock = None
    emitted = None

    def __format__(self, spec):
        if CountStr.clock is not None and CountStr.clock.t >= 1_500:
            CountStr.emitted.append(1)
        return str.__format__(self, spec)


def _hub_to_admins(n, clock, reads):
    cat = pristine(170000)
    new_role(cat, "hub", 17_100)
    names = [CountStr(f"adm_{i:05d}") for i in range(n)]
    for i, name in enumerate(names):
        new_role(cat, name, 17_101 + i)
    # the clock jumps on the LAST inherit_option read: the traversal is then complete, only emission remains
    cat["members"] += [TickOnKey(edge(17_100, 17_101 + i, inherit=True, set_=True), "inherit_option", clock, reads,
                                 after=n) for i in range(n)]
    return cat, frozenset(names)


def test_c3_a_i10_path_findings_are_checkpointed_while_they_are_emitted(monkeypatch):
    n = HOT * verify.CHECKPOINT_EVERY
    clock, reads, emitted = Clock(), [], []
    CountStr.clock, CountStr.emitted = clock, emitted
    try:
        cat, admins = _hub_to_admins(n, clock, reads)
        with pytest.raises(Refused, match="deadline"):
            i10_problems(cat, 170000, admins, work=Budget(10, clock).work)
        assert len(reads) == n  # the traversal itself had finished
        # refused during the result preparation or emission, before all n (correction 3B: the result list is
        # built under checkpoints and its sort is checked first, so refusal may now come before any emission)
        assert len(emitted) <= verify.CHECKPOINT_EVERY < n
        CountStr.clock = None
        quiet = Budget(1e9, Clock())  # no deadline: identical findings with and without the budget
        with_budget = i10_problems(cat, 170000, admins, work=quiet.work)
        assert with_budget == i10_problems(cat, 170000, admins)
        assert sum(1 for _, _, p, _ in with_budget[0] if p.startswith("admin_path:hub->")) == n
        monkeypatch.setattr(verify, "MAX_WORK", quiet.units - 1)  # and the work ceiling is enforced on it
        with pytest.raises(Refused, match="work ceiling"):
            i10_problems(cat, 170000, admins, work=Budget(1e9, Clock()).work)
    finally:
        CountStr.clock = CountStr.emitted = None


def test_c3_a_the_verifiers_own_i10_emission_is_checkpointed():
    """Verifier.i10() adds one finding per classified problem: that loop is checkpointed as well."""
    n = HOT * verify.CHECKPOINT_EVERY
    v = make()
    v.catalog = pristine()
    clock, added, real = Clock(), [], verify.i10_problems
    v.budget = Budget(10, clock)

    def classify(cat, version, admins, work=None, check=None, head="0034"):
        problems = [("role", f"r{i}", f"role_edge:r{i}->x", {}) for i in range(n)]
        clock.t += 1_000  # the classifier finished in time; the deadline passes as its findings are added
        return problems, []
    real_add = v.add

    def add(*a, **kw):
        added.append(1)
        return real_add(*a, **kw)
    v.add = add
    import app.payments.verify as mod
    mod.i10_problems, saved = classify, real
    try:
        with pytest.raises(Refused, match="deadline"):
            v.i10()
    finally:
        mod.i10_problems = saved
    assert len(added) <= verify.CHECKPOINT_EVERY < n


def _twelve_event_refs(count):
    return {f"rfnd_R{j}": [ev_row(100 * j + i, ref=f"rfnd_R{j}", amount=1000 + i) for i in range(MAX_REF_EVENTS)]
            for j in range(count)}


def _a1_verifier(by_ref, clock=None):
    v = make(mode="hold", events=ACTIVATED)
    del v.conceivable_fps  # the real enumeration, not make()'s seam
    v.ref_events = lambda did: by_ref
    v.budget = Budget(10, clock or Clock())
    return v


def test_c3_b_a1_a_deadline_inside_the_first_ref_states_expansion_refuses(monkeypatch):
    clock, calls, real = Clock(), [], verify._rank_key

    def rank(e):
        calls.append(1)
        if len(calls) == 10:
            clock.t += 1_000  # inside the first 12-event ref's subset enumeration
        return real(e)
    monkeypatch.setattr(verify, "_rank_key", rank)
    v = _a1_verifier(_twelve_event_refs(3), clock)
    with pytest.raises(Refused, match="deadline"):  # a deadline refusal, never an over_bound result
        v.conceivable_fps(DID, ["hold"])
    one_ref = 2 * MAX_REF_EVENTS * 2 ** (MAX_REF_EVENTS - 1)  # the rank calls of one full 12-event expansion
    assert len(calls) <= 10 + 2 * verify.CHECKPOINT_EVERY < one_ref


def test_c3_b_a1_an_over_bound_product_stops_expanding_later_refs(monkeypatch):
    calls, real = [], verify._rank_key
    monkeypatch.setattr(verify, "_rank_key", lambda e: calls.append(1) or real(e))
    refs = _twelve_event_refs(6)
    verify.ref_states(next(iter(refs.values())), DID)
    one_ref, calls[:] = len(calls), []
    status, info = _a1_verifier(refs).conceivable_fps(DID, ["hold"])
    assert (status, info["bound"]) == ("over_bound", "combinations") and info["combinations"] > ENUMERATION_BOUND
    assert len(calls) < 4 * one_ref  # 13 states per ref: 13**4 > 4096 is proven inside the 4th ref; refs 5-6 skipped
    assert variant_of(Finding("I12", "historical_ambiguous", "history", "1",
                              {"reason": "ack_mode_order", "enumeration": "over_bound", "bound": "combinations",
                               "combinations": info["combinations"], "run": 3})) == "V-A3"  # still a blocker


def test_c3_b_a1_the_inclusive_bounds_are_unchanged(monkeypatch):
    status, info = _a1_verifier({"rfnd_R0": [ev_row(i) for i in range(MAX_REF_EVENTS + 1)]}).conceivable_fps(
        DID, ["hold"])
    assert (status, info["bound"], info["events"]) == ("over_bound", "ref_events", 13)  # 13 raw events
    monkeypatch.setattr(verify, "_select", lambda cur, sql, params=None: [{"fp": "FP"}] * len(json.loads(
        params["sets"])))
    twelve = {f"rfnd_S{i}": [ev_row(i, ref=f"rfnd_S{i}")] for i in range(12)}  # 2**12 = 4096 exactly, in bound
    assert _a1_verifier(twelve).conceivable_fps(DID, ["hold"])[0] == "ok"
    thirteen = {f"rfnd_S{i}": [ev_row(i, ref=f"rfnd_S{i}")] for i in range(13)}  # 8192: over
    assert _a1_verifier(thirteen).conceivable_fps(DID, ["hold"]) == ("over_bound", {"bound": "combinations",
                                                                                    "combinations": 8192})
    full = _a1_verifier({"rfnd_R0": [ev_row(i, amount=1000 + i) for i in range(MAX_REF_EVENTS)]})
    assert full.conceivable_fps(DID, ["hold"])[0] == "ok"  # 12 raw events: enumerated (13 states)


def test_c3_c_i12h_a_deadline_inside_one_refs_eligibility_scan_refuses():
    n = HOT * verify.CHECKPOINT_EVERY
    raw = {"rfnd_A": [ev_row(0), *[ev_row(i, donation=B, amount=3000 + i) for i in range(1, n)]]}
    v = with_raw(make(events=ACTIVATED, history=[hist(1, "evaluation", 0, 2500, refs=["rfnd_A"])]), raw)
    clock, calls, real = Clock(), [], v.eligible

    def eligible(ver, d):
        calls.append(1)
        if len(calls) == 10:
            clock.t += 1_000  # during the step-3 eligibility scan of the one heavily versioned ref
        return real(ver, d)
    v.eligible = eligible
    v.budget = Budget(10, clock)
    with pytest.raises(Refused, match="deadline"):
        v.i12h(v.donations[DID], v.history)
    assert 10 <= len(calls) <= 10 + verify.CHECKPOINT_EVERY < n and v.findings == []
    v2 = with_raw(make(events=ACTIVATED, history=[hist(1, "evaluation", 0, 2500, refs=["rfnd_A"])]), raw)
    v2.i12h(v2.donations[DID], v2.history)  # without a deadline: the accepted step-5 result, unchanged
    (f,) = v2.findings
    assert (f.detail["reason"], f.detail["bound"], f.detail["events"]) == ("evidence_cut", "ref_events", n)


def _violated(n):
    v = make()
    base = v.donations[DID]
    for i in range(n):
        did = f"8d0c6a2e-1111-4a5b-9c3d-{i:012d}"
        v.donations[did] = {**base, "id": did, "provider_payment_id": f"pay_v{i}"}
        v.reversals[did] = {"donation_id": did, "captured_paise": 10000, "applied": 0, "hold": None, "flags": []}
        v.findings.append(Finding("I12", "structural", "donation", did, {"problem": "frozen_value"}))
    return v


class CountingSeverity(dict):
    def __init__(self, data, clock=None, after=None):
        super().__init__(data)
        self.lookups, self.clock, self.after = 0, clock, after

    def __getitem__(self, k):
        self.lookups += 1
        if self.after is not None and self.lookups == self.after:
            self.clock.t += 1_000
        return super().__getitem__(k)


def test_c3_d_the_violation_index_is_linear_complete_and_never_stale(monkeypatch):
    def lookups(n):
        v = _violated(n)
        sev = CountingSeverity(verify.SEVERITY)
        monkeypatch.setattr(verify, "SEVERITY", sev)
        v.classify()
        monkeypatch.undo()
        assert not [f for f in v.findings if f.invariant == "I13"]  # every violated donation is still skipped
        return sev.lookups
    assert lookups(600) < 2.5 * lookups(300)  # linear (the per-donation rescan grew as n squared)
    v = _violated(50)
    v.classify()
    brute = {f.entity_id if f.entity_kind == "donation" else f.detail.get("donation")
             for f in v.findings if verify.SEVERITY[f.cls] == "violation"}
    assert v.violated_donations() == brute  # the original definition, exactly
    v.findings.append(Finding("I12-H", "structural", "history", "9", {"problem": "x", "donation": "late"}))
    assert "late" in v.violated_donations()  # a later finding is never missed
    v.findings = [Finding("I12", "structural", "donation", "only", {"problem": "y"})]
    assert v.violated_donations() == {"only"}  # a replaced list is re-indexed
    assert rep(_violated(5))["gates"] == FALSE_GATES  # structural violations still close every gate


def test_c3_d_a_deadline_during_index_construction_refuses(monkeypatch):
    n = HOT * verify.CHECKPOINT_EVERY
    v = _violated(n)
    clock = Clock()
    sev = CountingSeverity(verify.SEVERITY, clock, after=10)
    monkeypatch.setattr(verify, "SEVERITY", sev)
    v.budget = Budget(10, clock)
    with pytest.raises(Refused, match="deadline"):
        v.violated_donations()
    assert 10 <= sev.lookups <= 10 + verify.CHECKPOINT_EVERY < n



# ── Phase 2A correction 3A: one donation's own history / evidence scans are checkpointed ──

class TickList(list):
    """A list whose ITERATION (forward or reversed) advances `clock` once `after` items have been produced: a
    deadline that passes in the middle of one donation's scan (each pass over the list counts on)."""

    def __init__(self, items, clock, after):
        super().__init__(items)
        self.clock, self.after, self.produced = clock, after, 0

    def _count(self, items):
        for x in items:
            self.produced += 1
            if self.produced == self.after:
                self.clock.t += 1_000
            yield x

    def __iter__(self):
        return self._count(list.__iter__(self))

    def __reversed__(self):
        return self._count(list.__reversed__(self))


def _deadline_verifier(**kw):
    clock = Clock()
    v = make(**kw)
    v.budget = Budget(10, clock)
    return v, clock


def _history(n, **kw):
    """One donation's consistent credit / reversal history of n writer rows (with the bucket's running total)."""
    rows = [hist(i, "writer", 0, 0, credited=(Decimal(i), Decimal(i + 1)), **kw) for i in range(n)]
    for i, h in enumerate(rows):
        h["total_after"] = Decimal(i + 1)
    return rows


N3A = HOT * verify.CHECKPOINT_EVERY


def _ack_eval(v):
    v.evals = {DID: {"donation_id": DID, "outcome": "x", "evidence_fp": "FP", "evaluated_at": T0, "state": "active",
                     "dispute_mode": v.mode, "ack_fp": None, "ack_run": 99}}


PATHS = {  # each path is driven on its own: a checkpoint in one never hides a missing one in another
    "credit_chain": lambda v, rows: v.credit_chain("I3", DID, rows, None),
    "reversal_chain": lambda v, rows: v.reversal_chain(DID, rows),
    "frozen_value": lambda v, rows: v.frozen_value(rows, DM),
    "classify_one_unmatched_ack": lambda v, rows: (_ack_eval(v), v.classify_one(DID, v.donations[DID], rows)),
    "classify_one_last_accounting_row": lambda v, rows: v.classify_one(DID, v.donations[DID], rows),
}


@pytest.mark.parametrize("path", sorted(PATHS))
def test_c3a_one_donations_history_scan_is_checkpointed(path):
    v, clock = _deadline_verifier()
    rows = TickList(_history(N3A), clock, after=10)
    with pytest.raises(Refused, match="deadline"):
        PATHS[path](v, rows)
    per_unit = 2 if path == "credit_chain" else 1  # credit_chain's zip walks the rows with two iterators at once
    assert 10 <= rows.produced <= 10 + per_unit * verify.CHECKPOINT_EVERY < per_unit * N3A  # inside the one scan


def test_c3a_one_rows_evidence_refs_scan_is_checkpointed():
    v, clock = _deadline_verifier()
    refs = TickList([f"rfnd_{i}" for i in range(N3A)], clock, after=10)
    with pytest.raises(Refused, match="deadline"):
        v.row_shape(hist(1, "evaluation", 0, 0, refs=refs))
    assert refs.produced <= 10 + verify.CHECKPOINT_EVERY < N3A


def test_c3a_frozen_value_steps_back_through_the_checkpoint():
    v, clock = _deadline_verifier()
    reads = []  # hold_after is read only by the while loop stepping back over one long held run
    rows = [TickOnKey(hist(i, "hold", 0, 0, hold_after=DM), "hold_after", clock, reads, after=10) for i in range(N3A)]
    with pytest.raises(Refused, match="deadline"):
        v.frozen_value(rows, DM)
    assert 10 <= len(reads) <= 10 + verify.CHECKPOINT_EVERY < N3A


def test_c3a_one_buckets_writer_rows_are_checkpointed():
    clock, reads = Clock(), []
    rows = []
    for i in range(N3A):
        h = hist(i, "writer", 0, 0, credited=(Decimal(0), Decimal(1)))
        h["total_after"] = Decimal(i + 1)
        rows.append(TickOnKey(h, "total_after", clock, reads, after=10))  # read only by the per-bucket scan
    v = make(history=rows)
    v.totals = {(SID, "fan"): Decimal(N3A)}
    v.budget = Budget(10, clock)
    with pytest.raises(Refused, match="deadline"):
        v.buckets()
    assert 10 <= len(reads) <= 10 + 2 * verify.CHECKPOINT_EVERY < 2 * N3A


def test_c3a_origins_scans_are_checkpointed():
    v, clock = _deadline_verifier()
    v.fp = {"hold": {DID: "NOW"}, "apply": {DID: "NOW"}}
    ev = {"evidence_fp": "OLD", "evaluated_at": T0, "state": "active", "dispute_mode": v.mode}
    rows = TickList(_history(N3A), clock, after=10)  # no evidence: the capture scan over the history runs
    with pytest.raises(Refused, match="deadline"):
        v.origins(DID, v.donations[DID], ev, "NOW", rows)
    assert rows.produced <= 10 + verify.CHECKPOINT_EVERY < N3A


def test_c3a_timing_scans_are_checkpointed():
    v, clock = _deadline_verifier()
    events = [event(i, "frozen" if i % 2 else "off", "active" if i % 2 else "frozen", T0 + timedelta(days=i))
              for i in range(1, N3A + 1)]
    v.events = TickList(events, clock, after=N3A + 10)  # periods() consumes N3A; the deadline passes in timing's scan
    with pytest.raises(Refused, match="deadline"):
        v.timing(DID, T0 - timedelta(days=30), "o_ev")
    assert v.events.produced <= N3A + 10 + 2 * verify.CHECKPOINT_EVERY < 2 * N3A


def test_c3a_a_deadline_inside_one_donations_history_refuses_the_full_run_and_releases():
    conn, reads = FakeConn(), []
    rows = [TickOnKey(h, "credited_before", conn.clock, reads, after=10) for h in _history(N3A)]
    d = dict(make().donations[DID])
    d.update({"credited_inr": Decimal(N3A), "writer_target": Decimal(N3A), "reversed_inr_sql": "0.00"})
    conn.answers.update({verify.DONATIONS: [d], verify.HISTORY: rows})
    r = run(conn, max_seconds=60)
    assert (r["status"], r["exit_code"], r["gates"], r["findings"]) == ("refused", 2, FALSE_GATES, [])
    assert r["reason"] == "deadline exceeded" and 10 <= len(reads) <= 10 + verify.CHECKPOINT_EVERY < N3A
    assert conn.log[-1] == "ROLLBACK" and conn.status == "IDLE" and not conn.closed


def test_c3a_load_second_passes_over_retained_rows_are_checkpointed():
    """load() re-walks retained rows to build its maps; a deadline inside one such pass refuses within one interval
    (the donations map: its 'id' key is read only by that comprehension)."""
    conn, reads = FakeConn(), []
    base = dict(make().donations[DID])
    rows = [TickOnKey({**base, "id": f"8d0c6a2e-1111-4a5b-9c3d-{i:012d}", "writer_target": base["credited_inr"],
                       "reversed_inr_sql": "0.00"}, "id", conn.clock, reads, after=10) for i in range(N3A)]
    conn.answers[verify.DONATIONS] = rows
    r = run(conn, max_seconds=60)
    assert (r["status"], r["exit_code"], r["gates"]) == ("refused", 2, FALSE_GATES)
    assert r["reason"] == "deadline exceeded" and 10 <= len(reads) <= 10 + verify.CHECKPOINT_EVERY < N3A
    assert conn.log[-1] == "ROLLBACK" and conn.status == "IDLE"


def _c3_module():
    """Correction 3's verify.py (the preserved backup), loaded beside the current module for equivalence."""
    import importlib.util
    path = os.environ.get("PAY15_C3_VERIFY", "D:/tmp/pay15-6a2-tools/verify_c3_2d3af632.py")
    if not os.path.exists(path):
        pytest.skip("the Correction 3 backup is not available on this host")
    spec = importlib.util.spec_from_file_location("verify_c3_reference", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scenarios():
    """In-budget scenarios touching every corrected path; each call builds fresh, independent verifiers."""
    def broken():
        rows = _history(5)
        rows[3] = hist(3, "writer", 0, 0, credited=(Decimal(9), Decimal(4)))  # an I3 credit-chain break
        rows[3]["total_after"] = Decimal(4)
        return make(history=rows)

    def writer():
        rows = _history(4)
        for i, h in enumerate(rows):
            h["total_after"] = Decimal(i + 1)
        v = make(history=rows)
        v.totals = {(SID, "fan"): Decimal(3)}  # an I6 last-total mismatch
        return v
    held = [hist(1, "evaluation", 0, 2500, refs=["rfnd_A"], at=T0 - timedelta(days=2)),
            hist(2, "hold", 2500, 2500, hold_after=DM, at=T0 - timedelta(days=1))]
    return [
        broken, writer, lambda: _keep(), lambda: _keep(ev={**KEEP_EV, "ack_fp": "ACK"}, fp_at=lambda d, c, m: "ACK"),
        lambda: make(mode="hold", current=[refund(), lost()], reversal={"applied": 2500, "hold": DM, "flags": []},
                     events=ACTIVATED, history=held),
        lambda: make(current=[refund()], reversal={"applied": 0, "hold": None, "flags": []}, events=ACTIVATED,
                     ev={"evidence_fp": "OLD", "evaluated_at": T0 - timedelta(hours=3), "state": "active",
                         "dispute_mode": "apply"}, evidence=[("rfnd_A", T0 - timedelta(hours=2))]),
        lambda: make(history=[hist(1, "evaluation", 0, 2500, refs=["rfnd_A"], at=T0 - timedelta(days=60))],
                     events=ACTIVATED),
    ]


def _pipeline(v):
    for d in v.donations.values():  # the DONATIONS columns make() leaves out, consistent with its data
        applied = (v.reversals.get(d["id"]) or {}).get("applied", 0)
        d.setdefault("writer_target", d["credited_inr"])
        d.setdefault("reversed_inr_sql", str(accounting.reversed_inr(d, applied)))
    for phase in (v.per_donation, v.chains, v.runs_check, v.legacy_timeline, v.state_event_chain, v.classify):
        phase()
    r = rep(v)
    return {k: x for k, x in r.items() if k not in ("runtime", "verifier", "digest")}


def test_c3a_financial_outputs_are_identical_to_correction_3():
    c3 = _c3_module()
    for build in _scenarios():
        current, reference = build(), build()
        reference.__class__ = c3.Verifier  # the same data and seams, Correction 3's code
        reference.budget = c3._NoBudget()
        got, want = _pipeline(current), _pipeline(reference)
        assert got == want  # identical findings, amounts, classification, blockers and gates
        assert got["status"] != "refused" and got["findings"]



# ── Phase 2A correction 3B: self-contained financial equivalence (pinned digests, no external files) ──
# Each scenario is built deterministically in memory; the SHA-256 of its normalized report (runtime counters,
# version and digest removed) was recorded from the Correction 3A implementation (e8170f47) and is pinned here.
# The pipeline scenarios cover the history / classification paths; the full-run scenarios go through the real
# verify() path: load(), I10, I16, I17, view_replica, classify and report construction.

def _report_digest(r: dict) -> str:
    body = {k: x for k, x in r.items() if k not in ("runtime", "verifier", "digest")}
    return __import__("hashlib").sha256(verify.canonical(body).encode("ascii")).hexdigest()


def _donation_row(**kw):
    d = dict(make().donations[DID])
    d.update({"writer_target": d["credited_inr"], "reversed_inr_sql": "0.00"}, **kw)
    return d


def _full_run_answers(name):
    if name == "full_i16_i17":
        return {verify.DONATIONS: [_donation_row()],
                verify.ALL_EVENTS: [ev_row(1, donation=None),
                                    ev_row(2, ref="rfnd_W", donation=B),
                                    ev_row(3, ref="disp_S", status="won", amount=None, snapshot=5000, at=T0),
                                    ev_row(4, ref="disp_T", status="won", amount=None, snapshot=2500,
                                           at=T0 + timedelta(hours=1)),
                                    ev_row(5, ref="rfnd_X", status="processed", amount=1000,
                                           at=T0 + timedelta(hours=2))]}
    if name == "full_history_classify":
        rows = _history(3)
        rows.append(hist(9, "evaluation", 0, 2500, refs=["rfnd_A"], at=T0 - timedelta(hours=5)))
        return {verify.DONATIONS: [_donation_row(credited_inr=Decimal(3), writer_target=Decimal(3))],
                verify.HISTORY: rows,
                verify.TOTALS: [{"streamer_id": SID, "donator_name_lower": "fan", "total_amount": Decimal(3)}],
                verify.REVERSALS: [{"donation_id": DID, "captured_paise": 10000, "applied": 2500, "hold": None,
                                    "flags": []}],
                verify.CURRENT: [refund()],
                verify.EVIDENCE: [{"donation_id": DID, "provider_ref": "rfnd_A", "kind": "refund",
                                   "recorded_at": T0 - timedelta(hours=6)}],
                verify.EVALUATIONS: [{"donation_id": DID, "evidence_fp": "OLD", "evaluated_at": T0 - timedelta(hours=4),
                                      "state": "active", "dispute_mode": "apply", "outcome": "x", "ack_fp": None,
                                      "ack_run": None}],
                verify.ALL_EVENTS: [ev_row(1, at=T0 - timedelta(hours=6))],
                verify.STATE: [{"state": "active", "dispute_mode": "apply"}],
                verify.EVENTS: [event(1, "off", "active", T0 - timedelta(days=2), "apply", "apply")],
                verify.FP_NOW: [{"id": DID, "fp": "FP"}],
                verify.CUT_ROWS: [refund()],
                verify.EVIDENCE_CUTS: [{"recorded_at": T0 - timedelta(hours=6)}]}
    if name == "full_buckets_runs":
        writer = _history(2)
        return {verify.DONATIONS: [_donation_row(credited_inr=Decimal(2), writer_target=Decimal(2))],
                verify.HISTORY: writer + [hist(5, "activation", 0, 0, hold_after=DM, run=7)],
                verify.TOTALS: [{"streamer_id": SID, "donator_name_lower": "fan", "total_amount": Decimal(1)}],
                verify.RUNS: [run_row(7, "reconcile", T0), run_row(8, "revert", T0, scope={})],
                verify.STREAMERS: [{"id": SID}],  # a live creator: the I6 / I7 bucket path
                verify.STATE: [{"state": "frozen", "dispute_mode": "hold"}]}
    raise KeyError(name)


FULL_RUNS = ("full_i16_i17", "full_history_classify", "full_buckets_runs")


def _full_run_digest(name, verify_fn=None):
    conn = FakeConn(answers=_full_run_answers(name))
    r = (verify_fn or verify.verify)(conn, max_seconds=60, clock=conn.clock)
    assert r["status"] != "refused", r.get("reason")
    return _report_digest(r)


def _classify_only(v):
    """Without the chain phases, whose structural findings would skip classify_one (A1, R-DM, I13 timing)."""
    for d in v.donations.values():
        d.setdefault("writer_target", d["credited_inr"])
        d.setdefault("reversed_inr_sql", "0.00")
    for phase in (v.legacy_timeline, v.state_event_chain, v.classify):
        phase()
    return {k: x for k, x in rep(v).items() if k not in ("runtime", "verifier", "digest")}


def _pipeline_digests(cls=None, nobudget=None, pipeline=None):
    out = []
    for build in _scenarios():
        v = build()
        if cls is not None:
            v.__class__, v.budget = cls, nobudget
        out.append(_report_digest({**(pipeline or _pipeline)(v), "runtime": None}))
    return out


GOLDEN_PIPELINES = [
    "434b35a8c5e1e6759ac2453840b95e8ef89d5616acc90610cb4f5b00967f951e",
    "7668dd613c22769ab914ca2c6cd6b8a14854a18011cbc75f96520f34d1ad611f",
    "b8281e187239f5b992605132dcbea6e3abdbaad15df9c807e4a356ca827a49b6",
    "b8281e187239f5b992605132dcbea6e3abdbaad15df9c807e4a356ca827a49b6",
    "96cae3a668f8e1013811a9ec867058fe1db46ee6966067e59d1dec5ccc4e9de2",
    "5c2dbd144e1dc569a85dfa72acb027c1545a4feeb7aff42790ff7899c419f0d3",
    "b20c0fd8f17ebb630c8d6d5bb91fede1f9a1623a55a158eff77aebad7b9a8449",
]
GOLDEN_CLASSIFY = [
    "0c3de060bbc6e23fff9a9fac80388a9e9b6b6e8e95bb23daea25f0ff5863b3d6",
    "0c3de060bbc6e23fff9a9fac80388a9e9b6b6e8e95bb23daea25f0ff5863b3d6",
    "cbd2c0a7c2a5d46dbbd91acc103692c5c29773a1289f8f869b1462271741be44",
    "546af26295279fb78554ab8286f44ab1d966dcf338b8fc20f1e41d48a9496ef7",
    "9fa4cc1dce29505851ec84c72b07dcf371b41328e079d3d4997188cc6a984208",
    "92909db8a58e6362d059d3c87dd2a6c3e879a6b26d4b4bfd60f2b5713b3d622f",
    "d443ceeac8640237b54b4513e9d5f1489ee3a799548d7ac705882b37d6b29530",
]
GOLDEN_FULL_RUNS = {
    "full_i16_i17": "9f81fea970e7c6cce30dc929674f1247635ca2f9b0c3869bb1a7a1e2e97f6d37",
    "full_history_classify": "e74a28feb3ecdf908a879bd1bac2fb52d582df5b2fa4b48b38559323e440189c",
    "full_buckets_runs": "e3e1c6507699b8ee170fc2606eda06d1bfecd0b5e8f70a26d66807d945948396",
}


def test_c3b_financial_outputs_match_the_pinned_correction_3a_digests():
    """Self-contained: no reference module or file is read. The pinned digests are Correction 3A's outputs for
    exactly these deterministic inputs; any change to a finding, amount, class, blocker or gate changes them."""
    assert _pipeline_digests() == GOLDEN_PIPELINES
    assert _pipeline_digests(pipeline=_classify_only) == GOLDEN_CLASSIFY
    assert {name: _full_run_digest(name) for name in FULL_RUNS} == GOLDEN_FULL_RUNS



# ── Phase 2A correction 3B: evaluation boundaries (no eager work before a checkpoint) ──
# The keys below count their REAL hash and ordering calls (set insertion / lookup, sort comparisons); the clock is
# advanced from those counts, so a set, union or sort that runs before any checkpoint is observed directly.

class Ops:
    """Counts hash / order operations per tag; advances `clock` at the `after`-th counted operation."""
    active = None

    def __init__(self, clock, after, tags=("main",)):
        self.clock, self.after, self.n = clock, after, 0
        self.by = {t: {"hash": 0, "lt": 0} for t in tags}

    def tick(self, tag, kind):
        self.by[tag][kind] += 1
        if tag == "main":
            self.n += 1
            if self.n == self.after:
                self.clock.t += 1_000


class K(str):
    """A string key whose hash and < are counted (tag 'main' unless set)."""
    tag = "main"

    def __hash__(self):
        if Ops.active is not None:
            Ops.active.tick(self.tag, "hash")
        return str.__hash__(self)

    def __lt__(self, other):
        if Ops.active is not None:
            Ops.active.tick(self.tag, "lt")
        return str.__lt__(self, other)


def key(i, tag="main", prefix="k"):
    k = K(f"{prefix}{i:07d}")
    k.tag = tag
    return k


class armed:
    def __init__(self, ops):
        self.ops = ops

    def __enter__(self):
        Ops.active = self.ops
        return self.ops

    def __exit__(self, *a):
        Ops.active = None


N3B = HOT * verify.CHECKPOINT_EVERY + 7  # strictly more than 3 checkpoint intervals


def test_c3b_b1_load_relevant_aggregation_is_incremental(monkeypatch):
    conn = FakeConn()
    evals = [{"donation_id": key(i), "evidence_fp": "F", "evaluated_at": T0, "state": "off", "dispute_mode": "hold",
              "outcome": "x", "ack_fp": None, "ack_run": None} for i in range(N3B)]
    conn.answers[verify.EVALUATIONS] = evals
    # load() hashes each id once building the evals map; the deadline then passes in the 'relevant' aggregation
    with armed(Ops(conn.clock, after=N3B + 10)) as ops:
        r = run(conn, max_seconds=60)
    assert (r["status"], r["reason"], r["gates"]) == ("refused", "deadline exceeded", FALSE_GATES)
    assert ops.by["main"]["lt"] == 0  # refused while the union was built: its sort never began
    assert ops.n <= N3B + 10 + verify.CHECKPOINT_EVERY and conn.log[-1] == "ROLLBACK" and conn.status == "IDLE"


def test_c3b_b2_i10_principal_preparation_is_incremental():
    cat = pristine()
    for i in range(N3B):
        cat["roles"].append(role(key(i, prefix="role_"), 18_000 + i))
    clock = Clock()
    # building the roles map hashes each name once; the deadline then passes during the principal preparation
    with armed(Ops(clock, after=N3B + 10)) as ops:
        with pytest.raises(Refused, match="deadline"):
            i10_problems(cat, 170000, frozenset(), work=Budget(10, clock).work)
    assert ops.by["main"]["lt"] == 0  # the principal sort never began
    # refused inside the principal filter (two set lookups per role), within one checkpoint interval of roles
    assert ops.n <= N3B + 10 + 2 * verify.CHECKPOINT_EVERY


class KT(datetime):
    """A timestamp whose hash and comparisons are counted (near_cuts' evidence times)."""

    def __hash__(self):
        if Ops.active is not None:
            Ops.active.tick("main", "hash")
        return datetime.__hash__(self)

    def __lt__(self, other):
        if Ops.active is not None:
            Ops.active.tick("main", "lt")
        return datetime.__lt__(self, other)

    def __gt__(self, other):
        if Ops.active is not None:
            Ops.active.tick("main", "lt")
        return datetime.__gt__(self, other)

    __le__ = datetime.__le__
    __ge__ = datetime.__ge__


def test_c3b_b3_near_cuts_refuses_right_after_its_one_sort():
    v, clock = _deadline_verifier()
    base = T0 + timedelta(minutes=1)
    times = [base + timedelta(milliseconds=50 * i) for i in range(N3B)]  # every one inside the D-15 margin
    times = [KT(x.year, x.month, x.day, x.hour, x.minute, x.second, x.microsecond, tzinfo=x.tzinfo) for x in times]
    v.evidence = {DID: [{"recorded_at": t} for t in times]}
    with armed(Ops(clock, after=3 * N3B + 20)):  # after the filter (2 compares + 1 hash per time): during the sort
        with pytest.raises(Refused, match="deadline"):  # checked right after the sort, before near_cuts returns
            v.near_cuts(DID, T0 + timedelta(minutes=1))


class TickDict(dict):
    """A dict whose key ITERATION advances `clock` at the `after`-th key (CPython caches tuple hashes, so a bucket
    key's hash cannot be counted; its production by iteration is, under either a set() copy or an incremental add)."""

    def __init__(self, items, clock, after):
        super().__init__(items)
        self.clock, self.after, self.produced = clock, after, 0

    def __iter__(self):
        for k in super().__iter__():
            self.produced += 1
            if self.produced == self.after:
                self.clock.t += 1_000
            yield k


def test_c3b_b4_bucket_key_preparation_is_incremental():
    v, clock = _deadline_verifier()
    totals = TickDict({(key(i, prefix="s"), "fan"): Decimal(1) for i in range(N3B)}, clock, after=10)
    v.totals = totals
    with armed(Ops(clock, after=10 ** 9)) as ops:  # counts only: the clock is advanced by the key iteration
        with pytest.raises(Refused, match="deadline"):
            v.buckets()
    assert ops.by["main"]["lt"] == 0  # refused while the key union was built: its sort never began
    assert 10 <= totals.produced <= 10 + verify.CHECKPOINT_EVERY < N3B


def test_c3b_b5_classification_aggregation_is_incremental():
    v, clock = _deadline_verifier()
    v.evals = {key(i): {} for i in range(N3B)}
    with armed(Ops(clock, after=10)) as ops:
        with pytest.raises(Refused, match="deadline"):
            v.classify()
    assert ops.by["main"]["lt"] == 0 and ops.n <= 10 + verify.CHECKPOINT_EVERY


def test_c3b_b6_replica_preparation_is_incremental():
    v, clock = _deadline_verifier()
    v.current = {key(i): [] for i in range(N3B)}
    with armed(Ops(clock, after=10)) as ops:
        with pytest.raises(Refused, match="deadline"):
            v.view_replica()
    assert ops.by["main"]["lt"] == 0 and ops.n <= 10 + verify.CHECKPOINT_EVERY


def test_c3b_b7_two_report_sorts_never_run_back_to_back():
    v, clock = _deadline_verifier()
    v.excluded = [{"bucket": key(N3B - i, "main", "x")} for i in range(N3B)]
    v.baselines = [{"bucket": key(N3B - i, "second", "y")} for i in range(N3B)]
    v.orphan_buckets = v.orphan_rows = 0
    with armed(Ops(clock, after=10, tags=("main", "second"))) as ops:
        with pytest.raises(Refused, match="deadline"):
            v.report()
    assert ops.by["main"]["lt"] > 10 and ops.by["second"]["lt"] == 0  # checked between the two sorts


# ── A. closed-variant validation inside one large finding ──

def _uuids(n):
    return [f"00000000-0000-4000-8000-{i:012d}" for i in range(n)]


def test_c3b_a1_one_large_v_i3_finding_is_validated_under_checkpoints(monkeypatch):
    ids = _uuids(N3B)
    f = Finding("I16", "evidence_attribution", "donation", DID,
                {"reason": "ref_identity_conflict", "ref": "rfnd_A", "payment": PAY, "account": "primary",
                 "source_ids": ids, "versions": N3B})
    assert variant_of(f) == "V-I3" and write_blockers(f, [f]) == {"i16:ref_identity_conflict"}  # in budget: unchanged
    v, clock = _deadline_verifier()
    v.findings.append(f)
    v.excluded, v.baselines, v.orphan_buckets, v.orphan_rows = [], [], 0, 0
    calls, real = [], verify._uuid

    def counted(x):
        calls.append(1)
        if len(calls) == 10:
            clock.t += 1_000  # the deadline passes during the 10th source-id validation
        return real(x)
    monkeypatch.setattr(verify, "_uuid", counted)
    with pytest.raises(Refused, match="deadline"):
        v.report()
    assert 10 <= len(calls) <= 10 + verify.CHECKPOINT_EVERY < N3B  # refused before the whole list was parsed


def _s2(n, members=None, ids=None):
    ids = ids if ids is not None else _uuids(n)
    members = members if members is not None else [
        {"id": i, "unit_status": "currency_mismatch" if k == 0 else "comparable", "currency": "USD", "value": 1}
        for k, i in enumerate(ids)]
    return Finding("I17", "refund_snapshot_mismatch", "donation", DID,
                   {"reason": "snapshot_unit_conflict", "subject": "snapshot", "source_ids": ids, "members": members})


def test_c3b_a2_one_large_v_s2_finding_is_validated_under_checkpoints():
    f = _s2(N3B)
    assert variant_of(f) == "V-S2" and write_blockers(f, [f]) == {"i17:snapshot_unit_conflict"}  # unchanged
    clock, reads = Clock(), []
    members = [TickOnKey(m, "unit_status", clock, reads, after=10) for m in f.detail["members"]]
    v = make()
    v.budget = Budget(10, clock)
    v.findings.append(_s2(N3B, members=members, ids=f.detail["source_ids"]))
    v.excluded, v.baselines, v.orphan_buckets, v.orphan_rows = [], [], 0, 0
    with pytest.raises(Refused, match="deadline"):
        v.report()
    assert 10 <= len(reads) <= 10 + verify.CHECKPOINT_EVERY < N3B  # refused inside the member scan


def test_c3b_a3_malformed_ids_and_mismatched_members_still_refuse():
    ids = _uuids(5)
    bad = Finding("I16", "evidence_attribution", "donation", DID,
                  {"reason": "ref_identity_conflict", "ref": "rfnd_A", "payment": PAY, "account": "primary",
                   "source_ids": ids[:4] + ["not-a-uuid"], "versions": 5})
    for f in (bad,
              _s2(5, ids=ids, members=[{"id": i, "unit_status": "currency_mismatch", "currency": "USD", "value": 1}
                                       for i in [ids[0]] * 5]),               # duplicate members != source ids
              _s2(5, ids=ids, members=[{"id": i, "unit_status": "comparable", "currency": "USD", "value": 1}
                                       for i in ids]),                         # every member comparable
              _s2(5, ids=ids, members=[{"id": i, "unit_status": "comparable", "currency": "USD"} for i in ids])):
        with pytest.raises(Refused):
            write_blockers(f, [f])
    same = _s2(4, ids=[ids[0], ids[0], ids[1], ids[2]])  # duplicates on both sides: an equal multiset, accepted
    assert variant_of(same) == "V-S2"


def test_c3b_a4_the_v_s2_multiset_rule_is_checkpointed():
    """The cross-field rule (source_ids == member ids as multisets) runs under the budget: the ids' hashes are
    counted, and the first hash of an id happens in that rule (validation and identity never hash them)."""
    ids = [K(u) for u in _uuids(N3B)]
    f = _s2(N3B, ids=ids)
    assert variant_of(f) == "V-S2" and write_blockers(f, [f]) == {"i17:snapshot_unit_conflict"}  # in budget
    v, clock = _deadline_verifier()
    v.findings.append(f)
    v.excluded, v.baselines, v.orphan_buckets, v.orphan_rows = [], [], 0, 0
    with armed(Ops(clock, after=10)) as ops:  # the deadline passes at the 10th id hash / comparison
        with pytest.raises(Refused, match="deadline"):
            v.report()
    assert ops.n <= 10 + 2 * verify.CHECKPOINT_EVERY + 2 < N3B  # <= 2 hashed operations per id before a checkpoint


def test_c3b_a5_report_identity_is_checked_after_every_finding(monkeypatch):
    v, clock = _deadline_verifier()
    for i in range(50):
        v.findings.append(Finding("I16", "evidence_attribution", "donation", DID,
                                  {"reason": "ref_identity_conflict", "ref": f"rfnd_A{i}", "payment": PAY,
                                   "account": "primary", "source_ids": _uuids(2), "versions": 2}))
    v.excluded, v.baselines, v.orphan_buckets, v.orphan_rows = [], [], 0, 0
    calls, real = [], verify.canonical

    def counted(x):
        calls.append(1)
        if len(calls) == 1:
            clock.t += 1_000  # the deadline passes during the first finding's canonical identity
        return real(x)
    monkeypatch.setattr(verify, "canonical", counted)
    with pytest.raises(Refused, match="deadline"):
        v.report()
    assert len(calls) == 1  # checked before the second canonical-identity operation


# ── C. the scope set ──

def test_c3b_c_a_large_scope_set_is_built_once_under_checkpoints(monkeypatch):
    clock, hashes = Clock(), []

    class H(str):
        def __hash__(self):
            hashes.append(1)
            if len(hashes) == 10:
                clock.t += 1_000  # the deadline passes during the 10th insertion
            return str.__hash__(self)
    real = verify._uuid
    monkeypatch.setattr(verify, "_uuid", lambda x: None if real(x) is None else H(real(x)))
    ids = _uuids(N3B)
    scope = {"kind": "recovery", "donations": ids, "verify_report_sha256": "a" * 64,
             "expires_at": (T0 + timedelta(hours=1)).isoformat()}
    with pytest.raises(Refused, match="deadline"):
        parse_scope(scope, T0, Budget(10, clock).each)
    assert 10 <= len(hashes) <= 10 + verify.CHECKPOINT_EVERY < N3B
    hashes.clear()
    status, info = parse_scope(scope, T0, Budget(1e9, Clock()).each)  # no deadline: valid, each id hashed ONCE
    assert status == "valid" and info["donations"] == set(ids) and len(hashes) == N3B
    for broken in (ids[:5] + [ids[0]], ids[:5] + ["nope"]):  # a duplicate or an invalid uuid: still malformed
        assert parse_scope({**scope, "donations": broken}, T0) == ("malformed", None)
    assert parse_scope({**scope, "extra": 1}, T0) == ("malformed", None)  # the key-set rule, constant work



def test_c3b_streamed_report_digest_equals_the_finish_digest():
    for name in FULL_RUNS:
        conn = FakeConn(answers=_full_run_answers(name))
        r = verify.verify(conn, max_seconds=60, clock=conn.clock)
        assert r["status"] != "refused" and r["digest"] == verify.finish(dict(r))["digest"]


# ── §2.7.4 publication inside a trust boundary (correction 1, blocker 3; local temporary directories only) ──

import threading  # noqa: E402

POSIX = pytest.mark.skipif(not operator._POSIX_SECURE,
                           reason="POSIX-only: needs O_NOFOLLOW, O_DIRECTORY and dir_fd operations")
WINDOWS = pytest.mark.skipif(operator._POSIX_SECURE, reason="only where the POSIX guarantees are unavailable")


def _text(extra=""):
    return verify.canonical(refused_report("example" + extra))


def _tmps(d):
    return sorted(p.name for p in d.iterdir() if p.name.endswith(".tmp"))


def _raise(exc):
    def f(*a, **kw):
        raise exc
    return f


@WINDOWS
def test_publication_refuses_where_it_cannot_be_secured(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "report.json"
    assert operator.publish(_text(), str(dest)) == "unsupported_platform" and not dest.exists()
    monkeypatch.delenv("HYPERCHAT_READONLY_DSN", raising=False)
    assert operator.main(["verify", "--max-seconds", "60", "--output", str(dest)]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "output_not_published: unsupported_platform"


@POSIX
def test_r13_success_writes_exactly_the_canonical_bytes_0600_and_keeps_the_exit_code(tmp_path, capsys):
    dest = tmp_path / "report.json"
    report = verify.finish({"status": "clean", "exit_code": 0, "x": 1})
    assert operator._emit(report, str(dest)) == 0
    printed = capsys.readouterr().out
    assert dest.read_bytes() == (verify.canonical(report) + "\n").encode("ascii") == printed.encode("ascii")
    assert dest.stat().st_mode & 0o777 == 0o600 and _tmps(tmp_path) == []


@POSIX
def test_r10_an_existing_destination_and_an_existing_symlink_are_never_touched(tmp_path):
    dest = tmp_path / "report.json"
    dest.write_bytes(b"original")
    assert operator.publish(_text(), str(dest)) == "link"
    assert dest.read_bytes() == b"original" and _tmps(tmp_path) == []
    target_ = tmp_path / "target.json"
    target_.write_bytes(b"keep")
    link = tmp_path / "link.json"
    link.symlink_to(target_)
    assert operator.publish(_text(), str(link)) == "link"
    assert target_.read_bytes() == b"keep" and link.is_symlink() and _tmps(tmp_path) == []


@POSIX
def test_r11d_a_temp_name_collision_preserves_the_foreign_file(tmp_path, monkeypatch):
    dest = tmp_path / "report.json"
    monkeypatch.setattr(operator.secrets, "token_hex", lambda n: "f" * 16)
    squatter = tmp_path / f"report.json.{'f' * 16}.tmp"
    squatter.write_bytes(b"someone else's")
    assert operator.publish(_text(), str(dest)) == "create"
    assert squatter.read_bytes() == b"someone else's" and not dest.exists()


@POSIX
def test_a_substituted_temp_pathname_is_never_published_and_never_deleted(tmp_path, monkeypatch):
    """The Codex counterexample, made deterministic: a barrier after fsync and before the commit point replaces the
    temporary pathname with attacker content."""
    dest, attacker = tmp_path / "report.json", tmp_path / "attacker"
    attacker.write_bytes(b'{"status":"clean","exit_code":0}\n')
    real_fsync = os.fsync

    def fsync_then_swap(fd):
        real_fsync(fd)
        if os.path.exists(attacker):  # the barrier: only the temporary file's fsync (once)
            (tmp,) = _tmps(tmp_path)
            os.replace(attacker, tmp_path / tmp)
    monkeypatch.setattr(operator.os, "fsync", fsync_then_swap)
    assert operator.publish(_text(), str(dest)) == "substituted"
    assert not dest.exists()
    (tmp,) = _tmps(tmp_path)
    assert (tmp_path / tmp).read_bytes() == b'{"status":"clean","exit_code":0}\n'  # foreign content kept


@POSIX
def test_a_swap_after_the_check_is_never_published_either(tmp_path, monkeypatch):
    """A swap between the check and the commit point: the open inode lost its last name, so linking it fails and
    the call refuses; the attacker's file is neither published nor deleted (no fallback to linking by name)."""
    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("no /proc/self/fd on this host")
    dest, attacker = tmp_path / "report.json", tmp_path / "attacker"
    attacker.write_bytes(b"attacker")
    real_stat, swapped = os.stat, []

    def stat_then_swap(path, *a, **kw):
        out = real_stat(path, *a, **kw)
        if isinstance(path, str) and path.endswith(".tmp") and not swapped:
            swapped.append(path)
            os.replace(attacker, tmp_path / path)
        return out
    monkeypatch.setattr(operator.os, "stat", stat_then_swap)
    assert operator.publish(_text(), str(dest)) == "substituted"
    assert not dest.exists()
    assert (tmp_path / swapped[0]).read_bytes() == b"attacker"  # never unlinked: not this call's inode


@POSIX
def test_an_untrusted_or_symlinked_output_directory_refuses(tmp_path):
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    open_dir.chmod(0o777)
    assert operator.publish(_text(), str(open_dir / "r.json")) == "directory"
    assert operator.publish(_text(), str(tmp_path / "r.json"), trusted_uids={0} - {os.geteuid()}) == "directory"
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "alias").symlink_to(real)
    assert operator.publish(_text(), str(tmp_path / "alias" / "r.json")) == "directory"
    assert list(real.iterdir()) == [] and not (open_dir / "r.json").exists()
    inner = open_dir / "inner"
    inner.mkdir(mode=0o700)  # trusted itself, but under a writable, non-sticky ancestor that could swap it
    assert operator.publish(_text(), str(inner / "r.json")) == "directory" and list(inner.iterdir()) == []


@POSIX
def test_without_proc_self_fd_publication_refuses_rather_than_linking_by_name(tmp_path, monkeypatch):
    real_isdir = os.path.isdir
    monkeypatch.setattr(operator.os.path, "isdir", lambda p: False if p == "/proc/self/fd" else real_isdir(p))
    assert operator.publish(_text(), str(tmp_path / "r.json")) == "unsupported_platform"
    assert list(tmp_path.iterdir()) == []


@POSIX
def test_r12_genuinely_concurrent_publishers_exactly_one_wins(tmp_path, monkeypatch):
    dest = tmp_path / "report.json"
    gate, real_fsync, mine = threading.Barrier(2, timeout=10), os.fsync, threading.local()

    def fsync_then_meet(fd):
        real_fsync(fd)
        if not getattr(mine, "met", False):  # the report file's fsync only (not the directory's)
            mine.met = True
            gate.wait()  # both publishers have written and synced; both now race for the commit point
    monkeypatch.setattr(operator.os, "fsync", fsync_then_meet)
    results, texts = {}, {0: _text("-0"), 1: _text("-1")}
    threads = [threading.Thread(target=lambda i=i: results.__setitem__(i, operator.publish(texts[i], str(dest))))
               for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert sorted(results.values(), key=str) == [None, "link"]
    winner = next(i for i, r in results.items() if r is None)
    assert dest.read_bytes() == (texts[winner] + "\n").encode() and _tmps(tmp_path) == []


@POSIX
@pytest.mark.parametrize("step", ["write", "short", "fsync", "link"])
def test_r11_a_failure_before_the_commit_point_publishes_nothing_and_removes_only_its_temp(tmp_path, monkeypatch,
                                                                                           step):
    dest = tmp_path / "report.json"
    boom = OSError(errno.ENOSPC, "no space")
    if step == "write":
        monkeypatch.setattr(operator.os, "write", _raise(boom))
    elif step == "short":
        monkeypatch.setattr(operator.os, "write", lambda fd, b: 0)
    elif step == "fsync":
        monkeypatch.setattr(operator.os, "fsync", _raise(boom))
    else:
        monkeypatch.setattr(operator.os, "link", _raise(boom))
    assert operator.publish(_text(), str(dest)) == ("write" if step == "short" else step)
    assert list(tmp_path.iterdir()) == []


@POSIX
def test_r11b_a_cleanup_failure_after_the_commit_point_is_still_published(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "report.json"
    monkeypatch.setattr(operator.os, "unlink", _raise(OSError("busy")))
    assert operator.publish(_text(), str(dest)) is None
    assert dest.read_bytes() == (_text() + "\n").encode() and "temporary file remains" in capsys.readouterr().err


@POSIX
def test_the_cli_refuses_with_exit_2_when_the_report_cannot_be_published(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "report.json"
    dest.write_bytes(b"original")
    monkeypatch.delenv("HYPERCHAT_READONLY_DSN", raising=False)
    monkeypatch.setattr(operator.psycopg, "connect", _raise(AssertionError("no connection expected")))
    assert operator.main(["verify", "--max-seconds", "60", "--output", str(dest)]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["reason"] == "output_not_published: link" and dest.read_bytes() == b"original"


# ── Codex Phase 1 note A: the CLI validates before connecting ──

@pytest.mark.parametrize("argv,reason", [
    (["--donation", "not-a-uuid", "--max-seconds", "60"], "scope id is not a uuid"),
    (["--creator", " ", "--max-seconds", "60"], "scope id is not a uuid"),
    (["--max-seconds", "abc"], "--max-seconds"),
    (["--max-seconds", "0"], "--max-seconds"),
    (["--max-seconds", "nan"], "--max-seconds"),
    ([], "--max-seconds"),
])
def test_the_cli_never_connects_for_an_invalid_invocation(monkeypatch, capsys, argv, reason):
    monkeypatch.setenv("HYPERCHAT_READONLY_DSN", "host=unused")
    monkeypatch.setattr(operator.psycopg, "connect", _raise(AssertionError("no connection expected")))
    assert operator.main(["verify", *argv]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "refused" and reason in out["reason"]


# ── the administrator allow-list (correction 1, blocker 2) ──

def _allowlist(tmp_path, body=b"# approved\ndba_one\n\ndba_two  # on call\n", mode=0o600):
    f = tmp_path / "admins"
    f.write_bytes(body)
    f.chmod(mode)
    return f


def test_a_missing_allow_list_approves_nobody(tmp_path):
    assert operator.load_admin_allowlist(str(tmp_path / "absent")) == (frozenset(), None)


@WINDOWS
def test_an_allow_list_is_refused_where_ownership_and_symlinks_cannot_be_checked(tmp_path):
    f = _allowlist(tmp_path)
    with pytest.raises(ValueError, match="not supported on this platform"):
        operator.load_admin_allowlist(str(f))


@POSIX
def test_a_secure_allow_list_owned_by_the_approved_uid(tmp_path):
    f = _allowlist(tmp_path)
    names, sha = operator.load_admin_allowlist(str(f), trusted_uid=os.getuid())
    assert names == {"dba_one", "dba_two"} and sha == __import__("hashlib").sha256(f.read_bytes()).hexdigest()


@POSIX
def test_an_allow_list_with_an_untrusted_owner_is_refused_even_at_0600(tmp_path):
    f = _allowlist(tmp_path)
    if os.getuid() == 0:
        pytest.skip("running as root: every file is owned by the default trusted uid")
    with pytest.raises(ValueError, match="not owned by the trusted owner"):
        operator.load_admin_allowlist(str(f))  # the default policy: root (uid 0) only


@POSIX
def test_a_symlinked_allow_list_is_refused(tmp_path):
    f = _allowlist(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(f)
    with pytest.raises(ValueError, match="symlink"):
        operator.load_admin_allowlist(str(link), trusted_uid=os.getuid())


@POSIX
@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_a_group_or_other_writable_allow_list_is_refused(tmp_path, mode):
    f = _allowlist(tmp_path, mode=mode)
    with pytest.raises(ValueError, match="writable"):
        operator.load_admin_allowlist(str(f), trusted_uid=os.getuid())


@POSIX
def test_an_allow_list_in_an_untrusted_directory_or_not_a_regular_file_is_refused(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)  # writable by others, not sticky: the file could be swapped
    f = _allowlist(shared)
    with pytest.raises(ValueError, match="directory"):
        operator.load_admin_allowlist(str(f), trusted_uid=os.getuid())
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ValueError, match="regular file"):
        operator.load_admin_allowlist(str(fifo), trusted_uid=os.getuid())


@POSIX
@pytest.mark.parametrize("body", [b"Robert'); DROP\n", b"dba one\n", "dbaä\n".encode()])
def test_a_malformed_allow_list_is_refused(tmp_path, body):
    f = _allowlist(tmp_path, body=body)
    with pytest.raises(ValueError):
        operator.load_admin_allowlist(str(f), trusted_uid=os.getuid())


@POSIX
def test_a_substitution_during_validation_reads_the_verified_inode(tmp_path, monkeypatch):
    f = _allowlist(tmp_path, body=b"dba_one\n")
    evil = tmp_path / "evil"
    evil.write_bytes(b"evil_dba\n")
    evil.chmod(0o600)
    real_fstat = os.fstat

    def fstat_then_swap(fd):
        out = real_fstat(fd)
        if evil.exists():
            os.replace(evil, f)  # the pathname now names another file
        return out
    monkeypatch.setattr(operator.os, "fstat", fstat_then_swap)
    names, _ = operator.load_admin_allowlist(str(f), trusted_uid=os.getuid())
    assert names == {"dba_one"}  # bound to the descriptor that was validated, never the replacement


def test_an_insecure_allow_list_refuses_before_connecting_and_trusts_nobody(tmp_path, monkeypatch, capsys):
    f = _allowlist(tmp_path, body=b"evil_dba\n")  # untrusted owner on POSIX (not root); unsupported on Windows
    if operator._POSIX_SECURE and os.getuid() == 0:
        pytest.skip("running as root")
    real = operator.load_admin_allowlist
    monkeypatch.setattr(operator, "load_admin_allowlist", lambda path=str(f): real(path))
    monkeypatch.setenv("HYPERCHAT_READONLY_DSN", "host=unused")
    monkeypatch.setattr(operator.psycopg, "connect", _raise(AssertionError("no connection expected")))
    monkeypatch.setattr(operator.verifier, "verify", _raise(AssertionError("never verified with these admins")))
    assert operator.main(["verify", "--max-seconds", "60"]) == 2
    assert "allow-list" in json.loads(capsys.readouterr().out)["reason"]
    cat = pristine()
    new_role(cat, "evil_dba", 17090, super_=True)
    assert problems(cat, admins=frozenset()) == ["unexpected_superuser"]  # nothing ever trusted it


# ── Codex Phase 1 note C: the remaining diagnostics, never classification ──

def test_i17_diagnostics_are_informational_only():
    v, got = _i17([obs(1, 900, "INR", 0), obs(2, 500, "USD", 1)], [processed(1, 600, "USD")], currency="USD")
    (f,) = v.findings
    assert got == ["snapshot_behind"] and f.detail["superseded_incomparable"] == [obs(1, 0)["id"]]
    assert "diag_latest_processed_refund_at" in f.detail
    assert f.detail["snapshot_at"] == obs(2, 0, hours=1)["provider_event_at"]
    assert variant_of(f) == "V-S6"  # the diagnostics never change the variant or the blockers
    base, _ = _i17([obs(1, 5000)], [processed(1, 2500)])  # the refund event itself is newer and unsnapshotted
    newer = ev_row(50, ref="disp_N", status="won", amount=None, at=T0 + timedelta(hours=5))
    v, _ = _i17([obs(1, 5000)], [processed(1, 2500), newer])
    assert v.findings[0].detail["newer_unsnapshotted_events"] == base.findings[0].detail[
        "newer_unsnapshotted_events"] + 1
    assert [f.detail["reason"] for f in v.findings] == [f.detail["reason"] for f in base.findings]


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# Phase 2B, stage A1 (accepted validation plan df1da1e1): pure-Python additions, runnable offline.
#   - the Phase 2B cluster safeguards (pure parts of test_verify_db.py's guard), tested without a database;
#   - NB-1 to NB-3 (Codex's non-blocking deadline recommendations);
#   - §6A I10 activation: the captured-catalog unit forms (16 rows) and the anti-shortcut guard (rule 4).
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════

import itertools  # noqa: E402

from tests.payments.verify_helpers import cluster_identity_problems, exclusive_decision  # noqa: E402

_E15 = {"sysid": "7000000000000000001", "port": "55445", "datadir": "D:\\tmp\\pay15-6a2b-pg\\pg15",
        "version_num": "150020"}
_ENV15 = {"HYPERCHAT_TEST_EXPECT_SYSID": _E15["sysid"], "HYPERCHAT_TEST_EXPECT_PORT": "55445",
          "HYPERCHAT_TEST_EXPECT_DATADIR": _E15["datadir"], "HYPERCHAT_TEST_EXPECT_VERSION_NUM": "150020",
          "HYPERCHAT_TEST_AUTH_DB": "hyperchat_ci:55445", "HYPERCHAT_TEST_PG_HOST": "127.0.0.1"}
_ADMIN15 = {"host": "127.0.0.1", "port": "55445", "user": "postgres", "dbname": "postgres"}


def test_2b_guard_accepts_only_the_exact_recorded_identity():
    assert cluster_identity_problems(_E15, _ENV15, _ADMIN15) == []
    for key, bad in (("sysid", "1"), ("datadir", "D:\\tmp\\elsewhere"), ("version_num", "170012")):
        assert cluster_identity_problems({**_E15, key: bad}, _ENV15, _ADMIN15), key


def test_2b_guard_refuses_pre_existing_ports_sockets_remote_hosts_and_missing_expectations():
    for port in ("55415", "55417", "55435", "55437"):  # plan §0: never, whatever the expectations say
        env = {**_ENV15, "HYPERCHAT_TEST_EXPECT_PORT": port, "HYPERCHAT_TEST_AUTH_DB": f"hyperchat_ci:{port}"}
        assert cluster_identity_problems({**_E15, "port": port}, env, {**_ADMIN15, "port": port}), port
    assert cluster_identity_problems(_E15, _ENV15, {**_ADMIN15, "host": "/var/run/postgresql"})  # a VPS socket
    assert cluster_identity_problems(_E15, _ENV15, {**_ADMIN15, "host": "db.example.org"})
    no_expect = {k: v for k, v in _ENV15.items() if not k.startswith("HYPERCHAT_TEST_EXPECT_")}
    assert cluster_identity_problems(_E15, no_expect, _ADMIN15)  # no silent fallback
    partial = {**no_expect, "HYPERCHAT_TEST_EXPECT_SYSID": _E15["sysid"]}
    assert cluster_identity_problems(_E15, partial, _ADMIN15)
    assert cluster_identity_problems({**_E15, "port": "5432"}, {**_ENV15, "HYPERCHAT_TEST_EXPECT_PORT": "5432",
                                                               "HYPERCHAT_TEST_AUTH_DB": "x:5432"},
                                     {**_ADMIN15, "port": "5432"})  # an unapproved port, even if expected
    assert cluster_identity_problems(_E15, {**_ENV15, "HYPERCHAT_TEST_AUTH_DB": "hyperchat_ci:55447"}, _ADMIN15)


_NONCE = "0123456789abcdef" * 4
_CI_ENV = {"HYPERCHAT_TEST_CI_IDENTITY": "1", "HYPERCHAT_TEST_CI_MARKER": _NONCE, "HYPERCHAT_TEST_CI_PG_MAJOR": "17",
           "HYPERCHAT_TEST_AUTH_DB": "hyperchat_ci:5432", "HYPERCHAT_TEST_PG_HOST": "localhost",
           "HYPERCHAT_TEST_CI_IDENTITY_FILE": "/runner/temp/hyperchat-ci-identity.json"}
_CI_ADMIN = {"host": "localhost", "port": "5432", "user": "postgres", "dbname": "postgres"}
_CI_SERVER = {"sysid": "7438211907213456789", "port": "5432", "datadir": "/var/lib/postgresql/data",
              "version_num": "170004",
              "databases": ["postgres", "template0", "template1", "hyperchat_ci", "hyperchat_verify_tmpl"],
              "ci_marker": "hyperchat-ci-disposable:" + _NONCE, "admin_user": "postgres", "admin_super": True,
              "login_roles": ["postgres", "hyperchat_migrator", "hyperchat_app", "hyperchat_worker",
                              "hyperchat_readonly", "hyperchat_lifecycle", "hyperchat_auth", "hyperchat_stepup"],
              "readonly": {"super": False, "bypassrls": False, "read_only_default": True}}
_CI_CAPTURE = {"source": "hyperchat-ci-provisioning-v1", "sysid": "7438211907213456789",
               "datadir": "/var/lib/postgresql/data", "version_num": "170004", "port": "5432", "nonce": _NONCE}


def _ci_logic(fn):
    """Run `fn` with CI execution forced ON (comparison logic only), so a refusal must come from the logic itself,
    never from the disabled switch (which would make every negative test hollow)."""
    from tests.payments import verify_helpers as m
    saved, m.CI_DB_EXECUTION_AUTHORIZED = m.CI_DB_EXECUTION_AUTHORIZED, True
    try:
        return fn()
    finally:
        m.CI_DB_EXECUTION_AUTHORIZED = saved


def _ci(server=None, env=None, capture=_CI_CAPTURE, admin=_CI_ADMIN):
    return _ci_logic(lambda: cluster_identity_problems({**_CI_SERVER, **(server or {})}, {**_CI_ENV, **(env or {})},
                                                       admin, capture))


def test_2b_ci_identity_authorized_by_od14_accepts_only_complete_matching_evidence():
    """OD-14 (2026-11-22): CI database execution is authorized. A complete, matching capture passes with the real
    switch; a missing capture is still refused, and the switch off still refuses for that reason alone."""
    from tests.payments import verify_helpers as m
    assert m.CI_DB_EXECUTION_AUTHORIZED is True
    assert cluster_identity_problems(_CI_SERVER, _CI_ENV, _CI_ADMIN, _CI_CAPTURE) == []
    assert cluster_identity_problems(_CI_SERVER, _CI_ENV, _CI_ADMIN, None)
    saved, m.CI_DB_EXECUTION_AUTHORIZED = m.CI_DB_EXECUTION_AUTHORIZED, False
    try:
        assert cluster_identity_problems(_CI_SERVER, _CI_ENV, _CI_ADMIN, _CI_CAPTURE) == [m.CI_DISABLED]
    finally:
        m.CI_DB_EXECUTION_AUTHORIZED = saved


def test_2b_ci_identity_comparison_logic_accepts_only_the_matching_shape(monkeypatch):
    """The comparison logic kept for OD-14, exercised with the switch forced on IN THIS TEST ONLY: the one shape
    whose server facts equal the capture passes; the refusal tests below force the switch on too."""
    assert _ci() == []
    assert _ci(server={"sysid": "1"}) and _ci(capture=None)


@pytest.mark.parametrize("server, why", [
    ({"sysid": "7438211907213456780"}, "another cluster's system identifier"),
    ({"datadir": "/var/lib/postgresql/data2"}, "another data directory"),
    ({"datadir": "/VAR/lib/postgresql/data"}, "data_directory is compared exactly"),
    ({"version_num": "170005"}, "another minor version"),
    ({"version_num": "150010"}, "another major version"),
    ({"port": "5433"}, "another port"),
])
def test_2b_ci_identity_wrong_server_fact_refused(server, why):
    assert _ci(server=server), why


@pytest.mark.parametrize("change, why", [
    ({"server": {"ci_marker": "hyperchat-ci-disposable:" + "f" * 64}}, "a replayed marker from another run"),
    ({"server": {"ci_marker": None}}, "no marker on the server"),
    ({"env": {"HYPERCHAT_TEST_CI_MARKER": "f" * 64}}, "the environment nonce differs from the capture"),
    ({"capture": {**_CI_CAPTURE, "nonce": "f" * 64}}, "the capture's nonce differs from the server marker"),
    ({"capture": {**_CI_CAPTURE, "nonce": "abc"}}, "a short nonce"),
    ({"capture": {**_CI_CAPTURE, "nonce": "z" * 64}}, "a non-hex nonce"),
])
def test_2b_ci_identity_replayed_or_incorrect_nonce_refused(change, why):
    assert _ci(server=change.get("server"), env=change.get("env"), capture=change.get("capture", _CI_CAPTURE)), why


@pytest.mark.parametrize("capture, why", [
    (None, "no identity capture at all"),
    ({k: v for k, v in _CI_CAPTURE.items() if k != "sysid"}, "a capture without the system identifier"),
    ({**_CI_CAPTURE, "datadir": ""}, "an empty captured data directory"),
    ({**_CI_CAPTURE, "source": "test-step"}, "a capture not written by the provisioning step"),
    ({**_CI_CAPTURE, "port": "55445"}, "a capture of another port"),
    ("not a dict", "an unreadable capture"),
])
def test_2b_ci_identity_incomplete_or_inconsistent_capture_refused(capture, why):
    assert _ci(capture=capture), why


@pytest.mark.parametrize("extra", [
    {"GITHUB_ACTIONS": "true"},
    {"GITHUB_ACTIONS": "true", "CI": "true", "RUNNER_OS": "Linux", "GITHUB_RUN_ID": "1"},
])
def test_2b_ci_identity_forged_ci_mode_refused(extra):
    """GITHUB_ACTIONS (and companions) never bypass anything; CI mode declared without a provisioning capture, or
    with only environment declarations, is refused even against a server that looks like CI."""
    base = {"HYPERCHAT_TEST_AUTH_DB": "hyperchat_ci:5432", "HYPERCHAT_TEST_PG_HOST": "localhost", **extra}
    for server in (_CI_SERVER, {"port": "5432"}):
        assert cluster_identity_problems(server, base, _CI_ADMIN), server            # not even CI mode
        assert _ci_logic(lambda: cluster_identity_problems(server, {**base, **_CI_ENV}, _CI_ADMIN)), server  # no capture
    # a capture copied from the environment's own claims but naming another server's identity: refused by the logic
    forged = {**_CI_CAPTURE, "sysid": "1", "datadir": "/tmp/fake"}
    assert _ci_logic(lambda: cluster_identity_problems(_CI_SERVER, {**base, **_CI_ENV}, _CI_ADMIN, forged))


@pytest.mark.parametrize("server_change, why", [
    ({"databases": [*_CI_SERVER["databases"], "hyperchat_dev"]}, "a DEV database is present"),
    ({"databases": [*_CI_SERVER["databases"], "hyperchat"]}, "a production database is present"),
    ({"databases": [*_CI_SERVER["databases"], "hyperchat_restore_test"]}, "the restore source is present"),
    ({"login_roles": [*_CI_SERVER["login_roles"], "deploy_bot"]}, "an unexpected login role"),
    ({"admin_user": "hyperchat_migrator", "admin_super": False}, "the admin is not the superuser postgres"),
    ({"readonly": {"super": False, "bypassrls": True, "read_only_default": True}}, "readonly can bypass RLS"),
    ({"readonly": {"super": False, "bypassrls": False, "read_only_default": False}}, "readonly is not read-only"),
    ({"readonly": {}}, "no readonly role"),
])
def test_2b_ci_identity_database_and_role_restrictions_kept(server_change, why):
    assert _ci(server=server_change), why


def test_2b_ci_identity_bad_declarations_refused():
    for env in ({"HYPERCHAT_TEST_CI_PG_MAJOR": "15"}, {"HYPERCHAT_TEST_CI_PG_MAJOR": ""},
                {"HYPERCHAT_TEST_AUTH_DB": "hyperchat_dev:5432"}, {"HYPERCHAT_TEST_PG_HOST": "/var/run/postgresql"},
                {"HYPERCHAT_TEST_EXPECT_SYSID": "7438211907213456789"}):  # CI and Phase 2B never mix
        assert _ci(env=env), env
    assert _ci(admin={**_CI_ADMIN, "host": "db.example.org"})
    assert _ci(server={"port": "55435"}, env={"HYPERCHAT_TEST_AUTH_DB": "hyperchat_ci:55435"},
               capture={**_CI_CAPTURE, "port": "55435"})  # a pre-existing cluster port, whatever the capture says


def test_2b_ci_identity_db_x_always_refused():
    for port in ("5432", "55455", "55457"):
        env = {**_CI_ENV, "HYPERCHAT_TEST_EXCLUSIVE_CLUSTER": "1", "HYPERCHAT_TEST_EXPECT_SYSID": "1"}
        assert exclusive_decision({**_CI_SERVER, "port": port}, env) == "fail", port


def test_2b_guard_phase2b_clusters_are_unchanged_and_never_satisfied_by_ci_markers():
    assert cluster_identity_problems(_E15, _ENV15, _ADMIN15) == []
    for port, version in (("55447", "170012"), ("55455", "150020"), ("55457", "170012")):
        ident = {**_E15, "port": port, "version_num": version}
        env = {**_ENV15, "HYPERCHAT_TEST_EXPECT_PORT": port, "HYPERCHAT_TEST_EXPECT_VERSION_NUM": version,
               "HYPERCHAT_TEST_AUTH_DB": f"hyperchat_ci:{port}"}
        assert cluster_identity_problems(ident, env, {**_ADMIN15, "port": port}) == [], port
    assert _ci_logic(lambda: cluster_identity_problems({**_CI_SERVER, "port": "55445"}, {
        **_CI_ENV, "HYPERCHAT_TEST_AUTH_DB": "hyperchat_ci:55445"}, _ADMIN15, {**_CI_CAPTURE, "port": "55445"}))


# ── S8: a restoration failure aborts the session and quarantines the cluster (fake sessions, a real pytest run) ──

_S8_PROBE = """
import pytest
from tests.payments.verify_helpers import restore_and_verify

BEFORE = [("role", "hyperchat_app", "f,t,f,f,t,f,f,20")]


class Conn:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on

    def __enter__(self):
        if self.fail_on == "connect":
            raise ConnectionError("the restore connection could not be opened")
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt):
        if self.fail_on == "statement":
            raise RuntimeError("restore statement failed: " + stmt)


def read_state():
    if MODE == "verify_connect":
        raise ConnectionError("the independent verification session could not be opened")
    if MODE == "state_query":
        raise RuntimeError("the post-restore state query failed")
    if MODE == "mismatch":
        return [("role", "hyperchat_app", "t,t,f,f,t,f,f,20")]
    return BEFORE


MODE = {mode!r}


@pytest.fixture
def fake_xroles():
    restore = []
    yield restore
    restore_and_verify(BEFORE, restore, connect=lambda: Conn(MODE if MODE in ("connect", "statement") else None),
                       read_state=read_state, ident={{"port": "55455", "sysid": "999"}}, quarantine_dir={qdir!r})


def test_a_db_x_mutation(fake_xroles):
    fake_xroles.append("ALTER ROLE hyperchat_app NOSUPERUSER")


def test_b_sentinel():
    open({sentinel!r}, "w").write("ran")
"""


@pytest.mark.parametrize("mode, original", [
    ("connect", "the restore connection could not be opened"),
    ("statement", "restore statement failed: ALTER ROLE hyperchat_app NOSUPERUSER"),
    ("verify_connect", "the independent verification session could not be opened"),
    ("state_query", "the post-restore state query failed"),
    ("mismatch", "post-restore role state differs from the pre-state"),
])
def test_2b_s8_any_restoration_failure_aborts_the_session_and_quarantines(tmp_path, mode, original):
    """Blocker 2: every failure point of the restoration (fake sessions; no PostgreSQL) ends the WHOLE pytest session
    with exit code 3 before a following sentinel test runs, writes the quarantine marker for that X cluster, and
    carries the original exception text."""
    import subprocess
    import sys
    from pathlib import Path
    backend = Path(__file__).resolve().parents[2]
    qdir, sentinel = tmp_path / "quarantine", tmp_path / "sentinel_ran"
    probe = tmp_path / "test_s8_probe.py"
    probe.write_text(_S8_PROBE.format(mode=mode, qdir=str(qdir), sentinel=str(sentinel)), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("HYPERCHAT_TEST_")}
    env["PYTHONPATH"] = os.pathsep.join([str(backend), env.get("PYTHONPATH", "")])
    r = subprocess.run([sys.executable, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-q", "-p", "no:randomly",
                        str(probe)], cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    assert r.returncode == 3, out
    assert not sentinel.exists(), "a later test ran after the S8 abort"
    marker = qdir / "X-55455-999.quarantined"
    assert marker.exists() and original in marker.read_text(encoding="utf-8"), out
    assert "QUARANTINED" in out and original in out


def test_2b_s8_a_clean_restoration_passes_without_quarantine(tmp_path):
    from tests.payments.verify_helpers import restore_and_verify
    calls, exits = [], []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, stmt):
            calls.append(stmt)
    before = [("role", "hyperchat_app", "x"), ("edge", "authtest_1->hyperchat_app", "f")]
    restore_and_verify(before, ["A", "B"], connect=Conn, read_state=lambda: [("role", "hyperchat_app", "x")],
                       ident={"port": "55457", "sysid": "1"}, quarantine_dir=str(tmp_path / "q"),
                       exit_=lambda *a, **k: exits.append((a, k)))
    assert calls == ["B", "A"] and exits == [] and not (tmp_path / "q").exists()


def test_2b_s8_a_quarantined_cluster_refuses_db_x(tmp_path):
    """After an S8 abort the marker blocks every later DB-X run on that cluster (also in a new session)."""
    from tests.payments.verify_helpers import quarantine_marker, quarantine_refusal
    ident, env = {"port": "55455", "sysid": "999"}, {"HYPERCHAT_TEST_QUARANTINE_DIR": str(tmp_path)}
    assert quarantine_refusal(ident, {}) and "QUARANTINE_DIR" in quarantine_refusal(ident, {})
    assert quarantine_refusal(ident, env) is None
    open(quarantine_marker(str(tmp_path), ident), "w").write("QUARANTINED")
    assert "QUARANTINED" in quarantine_refusal(ident, env)
    assert quarantine_refusal({"port": "55457", "sysid": "1"}, env) is None  # only that cluster


def test_2b_exclusive_gate_decisions():
    x = {**_E15, "port": "55455"}
    assert exclusive_decision(_E15, {}) == "skip"
    assert exclusive_decision(x, {}) == "skip"
    assert exclusive_decision(_E15, {"HYPERCHAT_TEST_EXCLUSIVE_CLUSTER": "1", "HYPERCHAT_TEST_EXPECT_SYSID": "1"}) == \
        "fail"
    assert exclusive_decision(None, {"HYPERCHAT_TEST_EXCLUSIVE_CLUSTER": "1"}) == "fail"
    assert exclusive_decision(x, {"HYPERCHAT_TEST_EXCLUSIVE_CLUSTER": "1"}) == "fail"
    assert exclusive_decision(x, {"HYPERCHAT_TEST_EXCLUSIVE_CLUSTER": "1", "HYPERCHAT_TEST_EXPECT_SYSID": "1"}) == "run"


# ── NB-1: A1 serialization (Codex non-blocking note) ───────────────────────────────────────────────────────────────

def _nb1_verifier(monkeypatch, clock):
    v = _keep(_one_event_refs(11))  # 12 refs, 2^12 = 4 096 states: the in-bound maximum (4 097 sets)
    v.budget = Budget(10, clock)
    issued = []

    def select(cur, sql, params=None):
        issued.append(sql)
        return [{"fp": "x"}]
    monkeypatch.setattr(verify, "_select", select)
    return v, issued


def test_nb1a_a1_deadline_while_the_sets_are_built_refuses_before_serialization(monkeypatch):
    """NB-1 (a), a control: the product is iterated through each(), so a deadline passing at the 10th combination
    refuses before json.dumps and before FP_SETS is issued (also true of Correction 3A)."""
    clock, real, seen = Clock(), itertools.product, []

    def product(*pools, **kw):
        for i, combo in enumerate(real(*pools, **kw)):
            if i == 10:
                clock.t += 1_000
            seen.append(1)
            yield combo
    monkeypatch.setattr(itertools, "product", product)
    v, issued = _nb1_verifier(monkeypatch, clock)
    dumps = []
    monkeypatch.setattr(verify.json, "dumps", lambda *a, **k: dumps.append(1) or "[]")
    with pytest.raises(Refused, match="deadline"):
        v.conceivable_fps(DID, ["hold"])
    assert verify.FP_SETS not in issued and dumps == [] and len(seen) <= 10 + verify.CHECKPOINT_EVERY


def test_nb1b_a1_deadline_inside_the_serialization_refuses_before_fp_sets(monkeypatch):
    """NB-1 (b), fail-first against Correction 3A: the deadline passes INSIDE json.dumps of the 4 097 sets; the
    check after the accepted atomic serialization refuses before the FP_SETS statement is issued (3A issued it)."""
    clock, real = Clock(), json.dumps
    v, issued = _nb1_verifier(monkeypatch, clock)

    def dumps(obj, **kw):
        out = real(obj, **kw)
        clock.t += 1_000  # the deadline passes during the serialization itself
        return out
    monkeypatch.setattr(verify.json, "dumps", dumps)
    with pytest.raises(Refused, match="deadline"):
        v.conceivable_fps(DID, ["hold"])
    assert verify.FP_SETS not in issued, issued


# ── NB-2: I10 proconfig (Codex non-blocking note) ──────────────────────────────────────────────────────────────────

def test_nb2_i10_a_huge_proconfig_is_parsed_under_checkpoints():
    """NB-2, fail-first against Correction 3A: one pinned function's captured proconfig holds more than three
    checkpoint intervals of entries; the deadline passes at entry 10; the refusal comes within one interval
    (3A parsed the whole array with dict() before any checkpoint)."""
    clock = Clock()
    cat = pristine()
    n = 3 * verify.CHECKPOINT_EVERY + 7
    entries = TickList([f"authtest_{i}=x" for i in range(n)], clock, after=10)
    (fn,) = [f for f in cat["functions"] if f["name"] == "payment_accounting_keep"]
    fn["proconfig"] = entries
    with pytest.raises(Refused, match="deadline"):
        i10_problems(cat, 170000, frozenset(), work=Budget(10, clock).work)
    assert 10 <= entries.produced <= 10 + verify.CHECKPOINT_EVERY < n


# ── NB-3: the replica comparison (Codex non-blocking note) ─────────────────────────────────────────────────────────

class EqTick:
    """A value whose == is counted; the clock advances at the `after`-th comparison."""

    def __init__(self, v, clock, counter, after):
        self.v, self._hook = v, (clock, counter, after)

    def __eq__(self, other):
        clock, counter, after = self._hook
        counter.append(1)
        if len(counter) == after:
            clock.t += 1_000
        return isinstance(other, EqTick) and self.v == other.v

    __hash__ = None


def test_nb3_the_view_replica_comparison_is_checkpointed_pairwise():
    """NB-3, fail-first against Correction 3A: one donation's replica and view, each more than three checkpoint
    intervals long and equal; the deadline passes at the 10th value comparison; refused within one interval of
    pairs (3A compared the two whole lists with one != before any checkpoint)."""
    clock, counter = Clock(), []
    n = 3 * verify.CHECKPOINT_EVERY + 7
    rows = lambda: [{"provider_ref": f"rfnd_{i:06d}", "amount_paise": EqTick(i, clock, counter, 10)}  # noqa: E731
                    for i in range(n)]
    v = make()
    v.budget = Budget(10, clock)
    v.current, v.evidence = {DID: rows()}, {}
    replica = rows()
    v.cut_rows = lambda did, cut: replica
    with pytest.raises(Refused, match="deadline"):
        v.view_replica()
    assert 10 <= len(counter) <= 10 + verify.CHECKPOINT_EVERY + 2 < n


def test_nb3_control_an_equal_and_an_unequal_replica_keep_their_result():
    v = make()
    view = [{"provider_ref": "rfnd_A", "amount_paise": 1}, {"provider_ref": "rfnd_B", "amount_paise": 2}]
    v.current, v.evidence = {DID: view}, {}
    v.cut_rows = lambda did, cut: [dict(x) for x in view]
    v.view_replica()
    assert not [f for f in v.findings if f.detail.get("problem") == "view_replica_mismatch"]
    for replica in ([dict(view[0])], [dict(view[0]), {**view[1], "amount_paise": 3}]):
        w = make()
        w.current, w.evidence = {DID: view}, {}
        w.cut_rows = lambda did, cut, r=replica: r
        w.view_replica()
        assert [f for f in w.findings if f.detail.get("problem") == "view_replica_mismatch"], replica


# ── §6A I10 baseline activation: captured-catalog unit forms and the anti-shortcut guard (plan §6A rule 4) ──────────

ORIGINAL_I10_UNVERIFIED = (
    "function ACL baseline (signature-exact, derived on pristine PG15 and PG17 at 0034)",
    "migrator -> owner edge inherit_option and set_option (PG16+)",
    "function identity-argument, return-type and proconfig texts",
    "trigger pins (tgtype, args, deferrable, constraint) on pristine catalogs",
)
ORIGINAL_SKIPPED_COMPARISONS = (
    "schema public exact owner (only pg_database_owner / hyperchat_owner accepted) and ACL",
    "prosrc md5 of set_updated_at and donations_require_eligible_creator",
    "table and column ACL exact pins (only PUBLIC entries and grant options are judged)",
    "alembic_version ownership",
)
# §6A row -> the original list entry it activates
ACTIVATION_ROWS = {
    "I1": ORIGINAL_SKIPPED_COMPARISONS[0], "I2": ORIGINAL_SKIPPED_COMPARISONS[0],
    "I3": ORIGINAL_SKIPPED_COMPARISONS[1], "I4": ORIGINAL_SKIPPED_COMPARISONS[1],
    "I5": ORIGINAL_SKIPPED_COMPARISONS[2], "I6": ORIGINAL_SKIPPED_COMPARISONS[2],
    "I7": ORIGINAL_SKIPPED_COMPARISONS[3],
    "I8": ORIGINAL_I10_UNVERIFIED[0], "I9": ORIGINAL_I10_UNVERIFIED[1],
    "I10": ORIGINAL_I10_UNVERIFIED[2], "I11": ORIGINAL_I10_UNVERIFIED[2], "I12": ORIGINAL_I10_UNVERIFIED[2],
    "I13": ORIGINAL_I10_UNVERIFIED[3], "I14": ORIGINAL_I10_UNVERIFIED[3], "I15": ORIGINAL_I10_UNVERIFIED[3],
    "I16": ORIGINAL_I10_UNVERIFIED[3],
}


def _fn_set(cat, name, **kw):
    (f,) = [x for x in cat["functions"] if x["name"] == name]
    f.update(kw)


def _guard_trigger(cat, **kw):
    (t,) = [x for x in cat["triggers"] if (x["rel"], x["name"]) == ("payment_accounting_runs",
                                                                     "payment_accounting_runs_guard")]
    t.update(kw)


def _schema_owner_other(cat):
    cat["schema_owner"] = "hyperchat_owner" if cat["schema_owner"] == "pg_database_owner" else "pg_database_owner"


# (row, case, mutation, code or None for a VOCABULARY GAP (OD-11), active in the 2B-0 state, versions)
ACTIVATION_UNIT = [
    ("I1", "schema_owner_other_candidate", _schema_owner_other, "ownership:schema_public", False, (150000, 170000)),
    ("I2", "schema_acl_public_create",
     lambda c: c["schema_acl"].append({"grantee": "PUBLIC", "privilege": "CREATE", "grantable": False}),
     "public_schema_create", True, (150000, 170000)),
    ("I2", "schema_acl_removal",
     lambda c: c.update(schema_acl=[x for x in c["schema_acl"] if x["grantee"] != "hyperchat_app"]),
     "schema_acl_missing:schema_public:hyperchat_app:USAGE", False, (150000, 170000)),  # OD-11
    ("I2", "schema_acl_grant_option",
     lambda c: [x.update(grantable=True) for x in c["schema_acl"] if x["grantee"] == "hyperchat_readonly"],
     "grant_option:schema_public:hyperchat_readonly", True, (150000, 170000)),
    ("I3", "prosrc_set_updated_at", lambda c: _fn_set(c, "set_updated_at", src="authtest-drift"), "body_drift",
     False, (150000, 170000)),
    ("I4", "prosrc_eligible_creator", lambda c: _fn_set(c, "donations_require_eligible_creator",
                                                         src="authtest-drift"), "body_drift", False, (150000, 170000)),
    ("I5", "table_acl_removal",
     lambda c: c.update(table_acl=[x for x in c["table_acl"] if (x["rel"], x["grantee"]) !=
                                   ("donation_reversals", "hyperchat_readonly")]),
     "table_acl_missing:donation_reversals:hyperchat_readonly:SELECT", False, (150000, 170000)),  # OD-11
    ("I5", "table_acl_alteration",
     lambda c: c.update(table_rights=[x for x in c["table_rights"] if (x["role"], x["rel"]) !=
                                      ("hyperchat_app", "payment_integrity_events")]
                        + [{"role": "hyperchat_worker", "rel": "payment_integrity_events", "priv": "INSERT"}]),
     "table_insert_granted:hyperchat_worker", True, (150000, 170000)),
    ("I5", "table_acl_grant_option",
     lambda c: [x.update(grantable=True) for x in c["table_acl"] if (x["rel"], x["grantee"]) ==
                ("donation_reversals", "hyperchat_readonly")],
     "grant_option:donation_reversals:hyperchat_readonly", True, (150000, 170000)),
    ("I6", "column_acl_addition",
     lambda c: c["column_rights"].append({"role": "hyperchat_app", "rel": "donation_reversals",
                                          "col": "applied_reversed_paise", "priv": "UPDATE"}),
     "column_update_granted:applied_reversed_paise:hyperchat_app", True, (150000, 170000)),
    ("I6", "column_acl_alteration",
     lambda c: c["column_acl"].append({"rel": "donations", "col": "payment_status", "grantee": "hyperchat_auth",
                                       "privilege": "SELECT", "grantable": False}),
     "column_acl:donations.payment_status:hyperchat_auth:SELECT", False, (150000, 170000)),  # OD-11
    ("I7", "alembic_owner",
     lambda c: [r.update(owner="hyperchat_migrator") for r in c["relations"] if r["rel"] == "alembic_version"],
     "ownership:alembic_version", False, (150000, 170000)),
    ("I8", "fn_acl_removal_worker",
     lambda c: c.update(function_acl=[x for x in c["function_acl"] if (x["name"], x["grantee"]) !=
                                      ("set_updated_at", "hyperchat_worker")]),
     "function_acl_missing:set_updated_at():hyperchat_worker", True, (150000, 170000)),
    ("I9", "migrator_inherit_true", lambda c: c["members"][0].update(inherit_option=True),
     "role_edge:hyperchat_migrator->hyperchat_owner", True, (170000,)),
    ("I9", "migrator_set_false", lambda c: c["members"][0].update(set_option=False),
     "role_edge:hyperchat_migrator->hyperchat_owner", True, (170000,)),
    ("I9", "migrator_rolinherit", lambda c: [r.update(inherit=True) for r in c["roles"] if r["name"] == MIGRATOR],
     "role_attribute:rolinherit", True, (150000,)),
    ("I10", "fn_args", lambda c: _fn_set(c, "payment_accounting_keep", args="p_donation uuid, p_run integer"),
     "function_attr:args", True, (150000, 170000)),
    ("I11", "fn_returns", lambda c: _fn_set(c, "payment_accounting_open_run", returns="integer"),
     "function_attr:returns", True, (150000, 170000)),
    ("I12", "fn_proconfig", lambda c: _fn_set(c, "payment_accounting_keep", proconfig=[
        *FN_EXPECT["payment_accounting_keep"]["proconfig"], "work_mem=64kB"]), "function_attr:proconfig", True,
     (150000, 170000)),
    ("I13", "trigger_type", lambda c: _guard_trigger(c, tgtype=tgtype("AFTER", "ROW", frozenset({"INSERT"}))),
     "trigger_binding:type", True, (150000, 170000)),
    ("I14", "trigger_args", lambda c: _guard_trigger(c, tgnargs=1, tgargs="x"), "trigger_binding:args", True,
     (150000, 170000)),
    ("I15", "trigger_constraint", lambda c: _guard_trigger(c, deferrable=True, initdeferred=True, is_constraint=True),
     "trigger_binding:constraint", True, (150000, 170000)),
    ("I16", "trigger_columns", lambda c: _guard_trigger(c, update_cols=[3]), "trigger_binding:columns", True,
     (150000, 170000)),
    ("I16", "trigger_when", lambda c: _guard_trigger(c, no_qual=False), "trigger_binding:when", True,
     (150000, 170000)),
]
_ACT_PARAMS = [pytest.param(row, case, version, id=f"{row}-{case}-pg{version // 10000}")
               for row, case, _, _, _, versions in ACTIVATION_UNIT for version in versions]


def _act(row, case):
    (x,) = [x for x in ACTIVATION_UNIT if (x[0], x[1]) == (row, case)]
    return x


def _active(row, active_now) -> bool:
    return active_now or row in getattr(verify, "I10_ACTIVE_COMPARISONS", frozenset())


@pytest.mark.parametrize("row, case, version", _ACT_PARAMS)
def test_6a_unit_positive_pristine_has_no_finding_with_the_rows_code(row, case, version):
    code = _act(row, case)[3]
    assert not [p for p in problems(pristine(version), version) if code and p.startswith(code)]


@pytest.mark.parametrize("row, case, version", _ACT_PARAMS)
def test_6a_unit_negative_detects_when_active_and_stays_fail_closed_when_not(row, case, version):
    """§6A rules 2 / 3: an active row detects its mutation with its code; an inactive row (the comparison off)
    must NOT detect it, and its list entry must still be reported as unverified (fail closed)."""
    _, _, mutate, code, active_now, _ = _act(row, case)
    cat = pristine(version)
    mutate(cat)
    found_, unverified = i10_problems(cat, version)
    got = [p for _, _, p, _ in found_]
    if _active(row, active_now):
        assert code is not None, f"{row}/{case}: active without an OD-11 code (S13)"
        assert [p for p in got if p.startswith(code)], (row, case, got)
    else:
        if code is not None:
            assert not [p for p in got if p.startswith(code)], (row, case, "detected while off", got)
        assert ACTIVATION_ROWS[row] in unverified, (row, case, "no longer reported as unverified", unverified)


def test_6a_guard_every_original_list_entry_maps_to_activation_rows():
    assert set(ACTIVATION_ROWS) == {f"I{i}" for i in range(1, 17)}
    assert set(ACTIVATION_ROWS.values()) == set(ORIGINAL_I10_UNVERIFIED) | set(ORIGINAL_SKIPPED_COMPARISONS)
    assert {r for r, *_ in ACTIVATION_UNIT} == set(ACTIVATION_ROWS)  # every row has unit mutations
    current = set(verify.I10_UNVERIFIED) | set(verify.SKIPPED_COMPARISONS)
    assert current <= set(ORIGINAL_I10_UNVERIFIED) | set(ORIGINAL_SKIPPED_COMPARISONS), \
        "a new unverified entry needs its own activation rows"


def test_6a_guard_an_entry_leaves_its_list_only_when_every_row_is_active():
    """Rule 4 (b): no item can become clean merely by deleting its name: every entry absent from the current lists
    must have all of its rows declared active by A5b (verify.I10_ACTIVE_COMPARISONS) - and then their negatives
    above require detection."""
    active = getattr(verify, "I10_ACTIVE_COMPARISONS", frozenset())
    current = set(verify.I10_UNVERIFIED) | set(verify.SKIPPED_COMPARISONS)
    for text in set(ORIGINAL_I10_UNVERIFIED) | set(ORIGINAL_SKIPPED_COMPARISONS):
        rows = {r for r, t in ACTIVATION_ROWS.items() if t == text}
        if text not in current:
            assert rows <= active, (text, "removed without its rows active", rows - active)


def test_6a_guard_the_confirmed_flag_and_an_empty_skip_list_need_every_row_active():
    """Rule 4 (c): I10_BASELINE_CONFIRMED and SKIPPED_COMPARISONS == () imply all 16 rows are active, and the
    reported unverified list is exactly what remains."""
    active = getattr(verify, "I10_ACTIVE_COMPARISONS", frozenset())
    if verify.I10_BASELINE_CONFIRMED:
        assert {r for r, t in ACTIVATION_ROWS.items() if t in ORIGINAL_I10_UNVERIFIED} <= active
    if not verify.SKIPPED_COMPARISONS:
        assert {r for r, t in ACTIVATION_ROWS.items() if t in ORIGINAL_SKIPPED_COMPARISONS} <= active
    for version in (150000, 170000):
        _, unverified = i10_problems(pristine(version), version)
        assert unverified == [*([] if verify.I10_BASELINE_CONFIRMED else verify.I10_UNVERIFIED),
                              *verify.SKIPPED_COMPARISONS]


def test_6a_guard_the_pre_activation_state_is_fail_closed():
    """Before A5c: the flag is off and every original entry is still listed, so I10 fails closed on a pristine
    catalog on both versions, HOWEVER MANY comparisons A5b has declared active (A1 asserted the empty set; A5b
    declares all 16 rows: population is not activation)."""
    assert verify.I10_UNVERIFIED == ORIGINAL_I10_UNVERIFIED and verify.SKIPPED_COMPARISONS == ORIGINAL_SKIPPED_COMPARISONS
    assert verify.I10_BASELINE_CONFIRMED is False
    assert verify.I10_ACTIVE_COMPARISONS == {f"I{i}" for i in range(1, 17)}  # A5b: every row implemented
    for version in (150000, 170000):
        found, unverified = i10_problems(pristine(version), version)
        assert found == [] and unverified == [*ORIGINAL_I10_UNVERIFIED, *ORIGINAL_SKIPPED_COMPARISONS]


# ══ A5b Part B (offline): OD-5 values, the OD-11 ACL families, U-3, U-5, fail-closed paths ═══════════════════════════

SOURCE_A = r"D:\tmp\pay15-6a2b-evidence\A5a"  # the frozen Source A (c49c2cd5); outside the repository


def _source_a(version):
    path = os.path.join(SOURCE_A, f"2B-expected-from-migrations-pg{version // 10000}.json")
    if not os.path.exists(path):
        pytest.skip("the frozen Source A evidence is not present on this machine")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize("version", [150000, 170000])
def test_a5b_the_implemented_values_equal_the_frozen_source_a(version):
    """OD-5: every constant A5b populated equals the reconciled, frozen Source A (A = B = C at A5a-R2)."""
    a = _source_a(version)
    assert [[verify.SCHEMA_OWNER_PIN]] == a["schema_owner"]
    assert {(g, p) for g, p, _ in map(tuple, a["schema_acl"])} == set(verify.SCHEMA_ACL_BASELINE)
    assert all(gr == "f" for _, _, gr in map(tuple, a["schema_acl"]))
    assert {(r, g, p) for r, g, p, _ in map(tuple, a["table_acl"])} == set(verify.TABLE_ACL_BASELINE[version // 10000])
    assert a["column_acl"] == [] and verify.COLUMN_ACL_BASELINE == frozenset()
    assert {r[0] for r in a["relation_owner"]} == set(verify.READ_RELATIONS) and all(r[1] == OWNER for r in a["relation_owner"])
    fa = {r[0]: r for r in a["functions"]}
    for name, e in FN_EXPECT.items():
        assert fa[name][1] == e["args"] and fa[name][2] == e["returns"], name
        assert fa[name][3] == ("{" + ",".join(e["proconfig"]) + "}" if e["proconfig"] else ""), name
        assert fa[name][4] == FUNCTION_PINS[name], name  # all 19 bodies pinned
    acl = {}
    for r in a["function_acl"]:
        if r[2] != OWNER:
            acl.setdefault(r[0], set()).add(r[2])
    assert {n: set(g) for n, g in FN_ACL_BASELINE.items()} == acl
    tg = {(r[0], r[1]): r for r in a["triggers"]}
    assert set(tg) == set(TRIGGER_PINS)
    for key, (fn, timing, level, events) in TRIGGER_PINS.items():
        assert decode_tgtype(int(tg[key][2])) == (timing, level, events) and tg[key][3:] == ["0", "f", "f", "f", "t", "O"]
    assert a["migrator"] == ([["pg15", "f"]] if version < 160000 else [["pg16+", "f", "t"]])


def test_a5b_version_specific_values_are_exactly_v1_and_v2():
    extra = verify.TABLE_ACL_BASELINE[17] - verify.TABLE_ACL_BASELINE[15]
    assert extra == {(r, OWNER, "MAINTAIN") for r in verify.RIGHTS_RELS} and len(extra) == 10
    assert not (verify.TABLE_ACL_BASELINE[15] - verify.TABLE_ACL_BASELINE[17])
    assert verify.TABLE_PRIVILEGES[17] == (*verify.TABLE_PRIVILEGES[15], "MAINTAIN")


def test_a5b_population_is_not_activation():
    assert verify.I10_BASELINE_CONFIRMED is False
    assert verify.I10_UNVERIFIED == ORIGINAL_I10_UNVERIFIED and verify.SKIPPED_COMPARISONS == ORIGINAL_SKIPPED_COMPARISONS


def _acl_row(**kw):
    return {"grantee": kw.get("grantee", "hyperchat_readonly"), "privilege": kw.get("privilege", "SELECT"),
            "grantable": kw.get("grantable", False), **{k: v for k, v in kw.items() if k in ("rel", "col")}}


@pytest.mark.parametrize("version", [150000, 170000])
def test_a5b_od11_six_families_added_removed_altered(version):
    cat = pristine(version)
    cat["schema_acl"].append(_acl_row(grantee="hyperchat_lifecycle", privilege="CREATE"))              # added
    cat["schema_acl"] = [x for x in cat["schema_acl"] if (x["grantee"], x["privilege"]) != ("hyperchat_stepup", "USAGE")]
    cat["table_acl"] = [x for x in cat["table_acl"] if not (x["rel"] == "payment_integrity_events"
                                                             and x["grantee"] == "hyperchat_app" and x["privilege"] == "INSERT")]
    cat["table_acl"].append(_acl_row(rel="payment_integrity_events", grantee="hyperchat_worker", privilege="INSERT"))  # altered
    cat["table_acl"] = [x for x in cat["table_acl"] if not (x["rel"] == "donations" and x["grantee"] == OWNER
                                                             and x["privilege"] == "TRUNCATE")]                        # owner entry removed
    cat["column_acl"].append(_acl_row(rel="donation_reversals", col="applied_reversed_paise", grantee="hyperchat_app",
                                      privilege="UPDATE"))
    got = problems(cat, version)
    assert "schema_acl:schema_public:hyperchat_lifecycle:CREATE" in got
    assert "schema_acl_missing:schema_public:hyperchat_stepup:USAGE" in got
    assert "table_acl_missing:payment_integrity_events:hyperchat_app:INSERT" in got
    assert "table_acl:payment_integrity_events:hyperchat_worker:INSERT" in got
    assert "table_acl_missing:donations:hyperchat_owner:TRUNCATE" in got
    assert "column_acl:donation_reversals.applied_reversed_paise:hyperchat_app:UPDATE" in got
    assert not [p for p in got if p.startswith("column_acl_missing")]  # the column baseline is empty


@pytest.mark.parametrize("version", [150000, 170000])
def test_a5b_od11_public_and_grant_option_codes_are_unchanged(version):
    cat = pristine(version)
    cat["table_acl"].append(_acl_row(rel="payment_accounting_runs", grantee="PUBLIC", privilege="INSERT"))
    for x in cat["table_acl"]:
        if (x["rel"], x["grantee"], x["privilege"]) == ("donation_reversals", "hyperchat_readonly", "SELECT"):
            x["grantable"] = True
    got = problems(cat, version)
    assert "public_table_insert" in got and "grant_option:donation_reversals:hyperchat_readonly" in got
    assert not [p for p in got if p.startswith("table_acl:payment_accounting_runs:PUBLIC")]  # PUBLIC keeps its own code
    assert not [p for p in got if p.startswith("table_acl") and "donation_reversals:hyperchat_readonly" in p]


def test_a5b_od11_every_new_code_is_an_i10_structural_violation(monkeypatch):
    cat = pristine(170000)
    cat["schema_acl"].append(_acl_row(grantee="hyperchat_lifecycle", privilege="CREATE"))
    v = SimpleNamespace(catalog=cat, first={"server_version_num": 170000}, admins=frozenset(), findings=[],
                        budget=verify._NoBudget())
    v.add = lambda inv, cls, kind, entity, **d: v.findings.append(verify.Finding(inv, cls, kind, str(entity), d))
    verify.Verifier.i10(v)
    f = [x for x in v.findings if x.detail.get("problem", "").startswith("schema_acl")]
    assert f and all((x.invariant, x.cls) == ("I10", "structural") for x in f)
    assert verify.SEVERITY["structural"] == "violation"


@pytest.mark.parametrize("bad, label", [
    (dict(grantee="", privilege="SELECT", grantable=False), "empty grantee"),
    (dict(grantee=None, privilege="SELECT", grantable=False), "no grantee"),
    (dict(grantee="hyperchat_readonly", privilege="FLY", grantable=False), "unknown privilege"),
    (dict(grantee="hyperchat_readonly", privilege="MAINTAIN", grantable=False), "MAINTAIN on PG15"),
    (dict(grantee="hyperchat_readonly", privilege="SELECT", grantable="f"), "non-boolean grantable"),
])
def test_a5b_od11_unreadable_table_acl_rows_refuse(bad, label):
    cat = pristine(150000)
    cat["table_acl"].append({"rel": "donation_reversals", **bad})
    with pytest.raises(verify.Refused, match="acl_unreadable: table donation_reversals"):
        i10_problems(cat, 150000)


def test_a5b_od11_duplicate_rows_refuse_schema_table_and_column():
    cat = pristine(170000)
    cat["schema_acl"].append(dict(cat["schema_acl"][0]))
    with pytest.raises(verify.Refused, match="acl_unreadable: schema public"):
        i10_problems(cat, 170000)
    cat = pristine(170000)
    cat["table_acl"].append(dict(cat["table_acl"][0]))
    with pytest.raises(verify.Refused, match="acl_unreadable: table "):
        i10_problems(cat, 170000)
    cat = pristine(170000)
    cat["column_acl"] += [_acl_row(rel="donations", col="payment_status", grantee="hyperchat_auth")] * 2
    with pytest.raises(verify.Refused, match="acl_unreadable: column donations.payment_status"):
        i10_problems(cat, 170000)


def test_a5b_od11_maintain_is_a_valid_privilege_name_only_on_pg17():
    cat = pristine(170000)
    cat["table_acl"].append(_acl_row(rel="donation_reversals", grantee="hyperchat_app", privilege="MAINTAIN"))
    assert "table_acl:donation_reversals:hyperchat_app:MAINTAIN" in problems(cat, 170000)


def test_a5b_unknown_major_fails_closed_through_baseline_unverified():
    cat = pristine(170000)
    found, unverified = i10_problems(cat, 160000)
    assert unverified[-1] == "table ACL baseline for server_version_num 160000 (unpinned major)"
    assert not [p for _, _, p, _ in found if p.startswith("table_acl")]  # nothing guessed, nothing compared


def test_a5b_inactive_comparisons_emit_nothing(monkeypatch):
    """Rule 3: with every A5b comparison switched off, the mutated catalog yields no baseline code at all."""
    monkeypatch.setattr(verify, "I10_ACTIVE_COMPARISONS", frozenset())
    for version in (150000, 170000):
        cat = pristine(version)
        cat["schema_owner"] = OWNER
        cat["schema_acl"] = cat["schema_acl"][:1]
        cat["table_acl"] = []
        cat["column_acl"].append(_acl_row(rel="donations", col="payment_status", grantee="hyperchat_auth"))
        [r.update(owner=MIGRATOR) for r in cat["relations"] if r["rel"] == "alembic_version"]
        _fn_set(cat, "set_updated_at", src="drift")
        got = problems(cat, version)
        assert not [p for p in got if p.split(":")[0] in ("schema_acl", "schema_acl_missing", "table_acl",
                                                           "table_acl_missing", "column_acl", "column_acl_missing")]
        assert "ownership:alembic_version" not in got and "ownership:schema_public" not in got
        assert "body_drift" not in got  # I3 pin inactive -> not compared
        _, unverified = i10_problems(cat, version)
        assert ORIGINAL_SKIPPED_COMPARISONS[0] in unverified


def test_a5b_u3_lock_state_identity_is_the_out_signature():
    assert FN_EXPECT["payment_accounting_lock_state"]["args"] == "OUT state text, OUT dispute_mode text"
    assert FN_EXPECT["payment_accounting_lock_state"]["returns"] == "record"
    cat = pristine(170000)
    assert not [p for p in problems(cat, 170000) if "payment_accounting_lock_state" in p]
    _fn_set(cat, "payment_accounting_lock_state", args="")  # the pre-A5b (wrong) text now drifts
    assert "function_attr:args" in problems(cat, 170000)
    assert [p for p in problems(cat, 170000) if p.startswith("function_acl") and
            "payment_accounting_lock_state(OUT state text, OUT dispute_mode text)" in p] == []  # sig() uses the catalog text


@pytest.mark.parametrize("rel, code", [
    ("donation_reversals", "table_maintain_granted:hyperchat_app"),
    ("payment_integrity_events", "table_maintain_granted:hyperchat_app"),
    ("donations", "donations_write:MAINTAIN:hyperchat_app"),
])
def test_a5b_u5_an_unexpected_maintain_right_cannot_be_clean(rel, code):
    cat = pristine(170000)
    cat["table_rights"].append({"role": "hyperchat_app", "rel": rel, "priv": "MAINTAIN"})
    assert code in problems(cat, 170000)
    cat["table_rights"].append({"role": OWNER, "rel": rel, "priv": "MAINTAIN"})  # the owner's own right: exempt
    assert not [p for p in problems(cat, 170000) if "MAINTAIN" in p and OWNER in p]


def test_a5b_u5_pristine_pg17_owner_maintain_is_clean_and_pg15_query_has_no_maintain():
    assert "MAINTAIN" in verify.CAT_TABLE_RIGHTS_17 and "MAINTAIN" not in verify.CAT_TABLE_RIGHTS
    assert verify.CAT_TABLE_RIGHTS_17 in verify._APPROVED_SQL and verify.statement_allowed(verify.CAT_TABLE_RIGHTS_17)
    assert problems(pristine(170000), 170000) == [] and problems(pristine(150000), 150000) == []


@pytest.mark.parametrize("version", [160000, 140012, 180000])
def test_a5b_u5_unsupported_major_is_refused_before_any_statement(version):
    """load() refuses an unpinned major before issuing a single statement (the fake cursor raises on any execute)."""
    class Cur:
        def execute(self, *a, **k):
            raise AssertionError("no statement may be issued")
    v = verify.Verifier(Cur(), {"server_version_num": version, "c_v": datetime.now(UTC)}, "all", None)
    with pytest.raises(verify.Refused, match=f"unsupported PostgreSQL major {version // 10000}"):
        v.load()


def test_a5b_u5_supported_majors_select_the_version_specific_rights_query():
    issued = []

    class Cur:
        def execute(self, sql, params=None):
            issued.append(sql)
            if sql is not verify.CAT_RELATIONS:
                raise RuntimeError("stop after the first statement")

        def fetchall(self):
            return []
    for version in (150020, 170012):
        issued.clear()
        v = verify.Verifier(Cur(), {"server_version_num": version, "c_v": datetime.now(UTC)}, "all", None)
        with pytest.raises(RuntimeError):
            v.load()
        assert issued[0] is verify.CAT_RELATIONS  # the major check passed; the §5.6 read came first


def test_a5b_findings_are_deduplicated_and_order_independent():
    cat = pristine(170000)
    cat["schema_acl"].append(_acl_row(grantee="hyperchat_lifecycle", privilege="CREATE"))
    cat["table_acl"].append(_acl_row(rel="donation_reversals", grantee="hyperchat_auth", privilege="SELECT"))
    a = i10_problems(cat, 170000)[0]
    cat["table_acl"].reverse()
    cat["schema_acl"].reverse()
    b = i10_problems(cat, 170000)[0]
    assert sorted(map(verify.canonical, a)) == sorted(map(verify.canonical, b))
    assert len({verify.canonical(x) for x in a}) == len(a)


# ══ A5b Part B correction 1: a scoped table absent from the versioned table-ACL baseline is MISSING configuration ═══

def _baseline_without(version, rel):
    base = dict(verify.TABLE_ACL_BASELINE)
    base[version // 10000] = frozenset(t for t in base[version // 10000] if t[0] != rel)
    return base


@pytest.mark.parametrize("version", [150000, 170000])
def test_a5b_c1_missing_table_baseline_is_reported_not_compared(monkeypatch, version):
    monkeypatch.setattr(verify, "TABLE_ACL_BASELINE", _baseline_without(version, "donations"))
    cat = pristine(version)
    found, unverified = i10_problems(cat, version)
    assert "table ACL baseline missing for donations" in unverified
    assert not [p for _, _, p, _ in found if p.startswith(("table_acl:donations", "table_acl_missing:donations"))]
    # the other scoped tables are still compared against their (present) baselines
    assert [p for p in problems(cat, version)] == []
    assert len(unverified) == len(verify.I10_UNVERIFIED) + len(verify.SKIPPED_COMPARISONS) + 1


@pytest.mark.parametrize("version", [150000, 170000])
def test_a5b_c1_missing_baseline_cannot_be_bypassed_by_an_empty_catalog(monkeypatch, version):
    """No explicit donations ACL in the catalog either: the comparison would be {} == {} and silently clean; the
    missing-baseline component must still be reported."""
    monkeypatch.setattr(verify, "TABLE_ACL_BASELINE", _baseline_without(version, "donations"))
    cat = pristine(version)
    cat["table_acl"] = [x for x in cat["table_acl"] if x["rel"] != "donations"]
    found, unverified = i10_problems(cat, version)
    assert "table ACL baseline missing for donations" in unverified
    assert not [p for _, _, p, _ in found if "donations" in p and p.startswith("table_acl")]


def test_a5b_c1_missing_baseline_makes_the_report_a_violation(monkeypatch):
    monkeypatch.setattr(verify, "TABLE_ACL_BASELINE", _baseline_without(170000, "donations"))
    monkeypatch.setattr(verify, "I10_BASELINE_CONFIRMED", True)   # even with the flag on and ...
    monkeypatch.setattr(verify, "SKIPPED_COMPARISONS", ())         # ... the skip list empty (the A5c end state)
    cat = pristine(170000)
    v = SimpleNamespace(catalog=cat, first={"server_version_num": 170000}, admins=frozenset(), findings=[],
                        budget=verify._NoBudget())
    v.add = lambda inv, cls, kind, entity, **d: v.findings.append(verify.Finding(inv, cls, kind, str(entity), d))
    verify.Verifier.i10(v)
    b = [f for f in v.findings if f.detail.get("problem") == "baseline_unverified"]
    assert len(b) == 1 and b[0].cls == "structural" and verify.SEVERITY["structural"] == "violation"
    assert b[0].detail["components"] == ["table ACL baseline missing for donations"]


@pytest.mark.parametrize("version", [150000, 170000])
def test_a5b_c1_a_valid_baseline_emits_no_missing_component(version):
    _, unverified = i10_problems(pristine(version), version)
    assert not [u for u in unverified if u.startswith("table ACL baseline missing")]
    assert all(rel in {t[0] for t in verify.TABLE_ACL_BASELINE[version // 10000]} for rel in verify.RIGHTS_RELS)
