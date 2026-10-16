"""The KYC restore witness (app.kyc.witness) without a database: anchored on the history chain, append-only,
fail-closed."""

import json

from app.kyc.witness import KycWitness


def test_no_path_accepts_only_the_untouched_database():
    w = KycWitness(None)
    assert w.problem("draft", 0, None) is None
    assert w.problem("draft", 1, "c1") == w.problem("active", 1, "c1") == "witness_not_configured"
    w.record("active", 3, "c3")  # nowhere to write: a no-op


def test_a_missing_file_fails_closed_and_an_empty_one_is_a_fresh_witness(tmp_path):
    w = KycWitness(str(tmp_path / "w"))
    assert w.seen() is None and w.problem("draft", 0, None) is None
    assert w.problem("draft", 2, None) == "witness_missing"  # deleted, or never provisioned
    (tmp_path / "w").touch()
    assert w.problem("draft", 2, None) is None and w.anchor_generation() == 0


def test_the_anchor_is_the_witnessed_history_not_just_a_number(tmp_path):
    (tmp_path / "w").touch()
    w = KycWitness(str(tmp_path / "w"))
    w.record("draft", 2, "c2", action="restriction_added")
    w.record("active", 3, "c3", action="enforcement_activated")
    w.record("active", 1, "c1")  # lower: not written
    with open(w.path, encoding="utf-8") as f:
        assert [(e["state"], e["generation"], e["chain"]) for e in map(json.loads, f)] == [
            ("draft", 2, "c2"), ("active", 3, "c3")]
    assert w.anchor_generation() == 3
    assert w.problem("active", 3, "c3") is None and w.problem("closed", 9, "c3") is None  # extensions
    assert w.problem("active", 2, None) == "restore_suspected"     # an older generation
    assert w.problem("draft", 9, "c3") == "restore_suspected"      # an earlier state
    assert w.problem("active", 9, "other") == "restore_suspected"  # caught up in number, not in history


def test_a_torn_or_foreign_line_is_never_trusted(tmp_path):
    path = tmp_path / "w"
    path.write_text('{"generation": 5, "chain": "c5", "state": "active"}\n{"generation": 9, "sta\nnot json\n'
                    '{"generation": 7, "state": "active"}\n')
    w = KycWitness(str(path))
    assert w.seen() == {"generation": 5, "chain": "c5", "rank": 1}
    assert w.problem("active", 6, "c5") is None and w.problem("active", 6, "x") == "restore_suspected"


def test_an_unreadable_witness_fails_closed(tmp_path):
    w = KycWitness(str(tmp_path))  # a directory: reading it fails
    assert w.problem("draft", 0, None) == "witness_unreadable"
