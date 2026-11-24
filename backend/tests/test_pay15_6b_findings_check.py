"""PAY-15 6b gates R2 / S2: deploy/pay15-6b/findings_check.py (FU-1 A, FU-2): the verifier findings must be exactly
the packet's expected set; smoke-origin orphan buckets are allowed only inside the smoke window."""

import importlib.util
import json
from collections import Counter
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "deploy" / "pay15-6b" / "findings_check.py"
spec = importlib.util.spec_from_file_location("findings_check", SRC)
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)

KNOWN = "be292c92-f948-42db-bbaf-34079bcf39d0"
NEW = "11111111-2222-3333-4444-555555555555"
EXPECTED = Counter(["I10|structural|baseline|i10", "I13|awaiting_reconciliation|donation|25fb78ef"])


def f(inv, cls, kind, eid):
    return {"invariant": inv, "class": cls, "entity_kind": kind, "entity_id": eid, "detail": {}}


BASE = [f("I10", "structural", "baseline", "i10"), f("I13", "awaiting_reconciliation", "donation", "25fb78ef")]


def orphan(sid):
    return f("Erratum-1", "excluded_bucket_net_credit", "bucket", f"{sid}:0123456789abcdef")


def test_exactly_the_expected_set_passes_and_known_orphans_are_allowed():
    assert fc.check(BASE + [orphan(KNOWN), orphan(KNOWN)], EXPECTED, [KNOWN]) == []


def test_an_extra_or_missing_finding_fails():
    assert fc.check(BASE + [f("I6", "structural", "history", "9")], EXPECTED, []) == ["unexpected finding I6|structural|history|9"]
    assert fc.check(BASE[:1], EXPECTED, []) == ["expected finding missing I13|awaiting_reconciliation|donation|25fb78ef"]


def test_an_orphan_bucket_of_an_unknown_creator_fails_without_the_smoke_window():
    assert fc.check(BASE + [orphan(NEW)], EXPECTED, [KNOWN]) == [
        f"unexpected finding Erratum-1|excluded_bucket_net_credit|bucket|{NEW}:0123456789abcdef"]


def test_a_new_orphan_written_inside_the_smoke_window_is_allowed():
    before, after = {KNOWN: (10, 20)}, {KNOWN: (10, 20), NEW: (105, 110)}
    assert fc.check(BASE + [orphan(KNOWN), orphan(NEW)], EXPECTED, [KNOWN], before, after, 100, 120) == []


def test_orphan_history_outside_the_window_or_changed_or_unpinned_fails():
    before = {KNOWN: (10, 20)}
    assert fc.check(BASE, EXPECTED, [KNOWN], before, {KNOWN: (10, 20), NEW: (90, 110)}, 100, 120) == [
        f"new orphan creator {NEW} has history outside the smoke window"]
    assert fc.check(BASE, EXPECTED, [KNOWN], before, {KNOWN: (10, 115)}, 100, 120) == [
        f"orphan creator {KNOWN} got new credit history during S"]
    assert fc.check(BASE, EXPECTED, [], before, before, 100, 120)[0].startswith("orphan creators before the smoke")


def test_accounting_history_on_a_deleted_creator_is_never_allowed():
    bad = f("Erratum-1", "deleted_creator_with_accounting_history", "bucket", f"{KNOWN}:0123456789abcdef")
    assert fc.check(BASE + [bad], EXPECTED, [KNOWN]) != []


def test_the_command_line(tmp_path):
    (tmp_path / "v.json").write_text(json.dumps({"findings": BASE + [orphan(NEW)]}))
    (tmp_path / "e.txt").write_text("# DEV pre-A5c\n" + "\n".join(EXPECTED) + "\n\n")
    (tmp_path / "b.txt").write_text("")
    (tmp_path / "a.txt").write_text(f"{NEW}|105|110\n")
    args = [str(tmp_path / "v.json"), str(tmp_path / "e.txt")]
    assert fc.main(args) == 1
    window = ["--orphans-before", str(tmp_path / "b.txt"), "--orphans-after", str(tmp_path / "a.txt")]
    assert fc.main(args + window + ["--since", "100", "--until", "120"]) == 0
    assert fc.main(args + window) == 2  # files without the window
    assert fc.main([str(tmp_path / "missing.json"), str(tmp_path / "e.txt")]) == 2
