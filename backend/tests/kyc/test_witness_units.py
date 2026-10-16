"""The KYC restore witness (app.kyc.witness) without a database: monotonic, append-only, fail-closed."""

import json

from app.kyc.witness import KycWitness


def test_no_path_accepts_only_the_untouched_database():
    w = KycWitness(None)
    assert w.problem("draft", 0) is None
    assert w.problem("draft", 1) == w.problem("active", 1) == "witness_not_configured"
    w.record("active", 3)  # nowhere to write: a no-op


def test_a_missing_file_accepts_only_the_untouched_database(tmp_path):
    w = KycWitness(str(tmp_path / "w"))
    assert w.seen() is None and w.problem("draft", 0) is None
    assert w.problem("draft", 2) == "witness_missing"


def test_it_only_moves_forward_and_detects_a_database_behind_it(tmp_path):
    w = KycWitness(str(tmp_path / "w"))
    w.record("draft", 2, action="restriction_added")
    w.record("active", 3, action="enforcement_activated")
    w.record("active", 1)  # lower: not written
    with open(w.path, encoding="utf-8") as f:
        assert [(e["state"], e["generation"]) for e in map(json.loads, f)] == [("draft", 2), ("active", 3)]
    assert w.problem("active", 3) is None and w.problem("closed", 4) is None
    assert w.problem("active", 2) == "restore_suspected"  # an older generation
    assert w.problem("draft", 9) == "restore_suspected"   # an earlier state, whatever the generation


def test_a_torn_or_foreign_line_is_never_trusted(tmp_path):
    path = tmp_path / "w"
    path.write_text('{"generation": 5, "state": "active"}\n{"generation": 9, "sta\nnot json\n{"generation": 2}\n')
    w = KycWitness(str(path))
    assert w.seen() == (5, 1)
    assert w.problem("active", 4) == "restore_suspected" and w.problem("active", 5) is None


def test_an_unreadable_witness_fails_closed(tmp_path):
    w = KycWitness(str(tmp_path))  # a directory: reading it fails
    assert w.problem("draft", 0) == "witness_unreadable"
