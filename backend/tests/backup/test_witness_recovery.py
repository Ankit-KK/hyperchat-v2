"""Recovery of the KYC restore witness (app.backup.witness) and what a restored database looks like to the real check
(app.kyc.witness). The rule under test: an older backup must never come back together with its own older witness."""

import json
from pathlib import Path

import pytest

from app.backup.witness import WitnessConflict, main, select
from app.kyc.witness import KycWitness


def line(generation: int, state: str, chain: str) -> str:
    return json.dumps({"chain": chain, "generation": generation, "state": state, "at": "t"}, sort_keys=True) + "\n"


HISTORY = [line(1, "active", "c1"), line(2, "active", "c2"), line(3, "active", "c3")]


def write(path: Path, *lines: str) -> Path:
    path.write_text("".join(lines))
    return path


def test_the_newest_copy_of_one_history_is_chosen_wherever_it_lives(tmp_path):
    old_backup = write(tmp_path / "set1.witness", *HISTORY[:1])
    newer_backup = write(tmp_path / "set2.witness", *HISTORY[:2])
    live = write(tmp_path / "live.witness", *HISTORY)
    result = select([old_backup, live, newer_backup])
    assert result["chosen"] == str(live) and (result["generation"], result["chain"], result["state"]) == (3, "c3",
                                                                                                         "active")
    assert [c["generation"] for c in result["candidates"]] == [1, 2, 3]


def test_a_copy_taken_mid_write_is_still_part_of_the_history(tmp_path):
    torn = write(tmp_path / "torn.witness", HISTORY[0], HISTORY[1][:10])  # a crash while appending line 2
    full = write(tmp_path / "full.witness", *HISTORY[:2])
    assert select([torn, full])["chosen"] == str(full)
    assert KycWitness(str(torn)).seen()["generation"] == 1  # the torn line is never trusted


@pytest.mark.parametrize("other", [
    [line(1, "active", "c1"), line(2, "active", "OTHER")],  # rewritten after a copy was taken
    [line(1, "active", "x1")],                                # another environment's witness, or a re-initialised one
])
def test_diverged_histories_are_refused_and_left_to_the_operator(tmp_path, other):
    a = write(tmp_path / "a.witness", *HISTORY)
    b = write(tmp_path / "b.witness", *other)
    with pytest.raises(WitnessConflict, match="diverged_histories"):
        select([a, b])


def test_symlinks_and_non_witness_files_are_refused(tmp_path):
    real = write(tmp_path / "real.witness", *HISTORY)
    (tmp_path / "link.witness").symlink_to(real)
    with pytest.raises(WitnessConflict, match="not_a_plain_file"):
        select([real, tmp_path / "link.witness"])
    (tmp_path / "dump").write_bytes(b"PGDMP binary")
    with pytest.raises(WitnessConflict, match="not_a_witness"):
        select([tmp_path / "dump"])
    with pytest.raises(WitnessConflict, match="no_candidates"):
        select([])


def test_the_cli_reports_json_and_never_writes(tmp_path, capsys):
    a, b = write(tmp_path / "a.witness", *HISTORY[:1]), write(tmp_path / "b.witness", *HISTORY)
    before = {p: p.read_bytes() for p in (a, b)}
    assert main(["select", str(a), str(b)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["chosen"] == str(b)
    assert {p: p.read_bytes() for p in (a, b)} == before and sorted(tmp_path.iterdir()) == sorted([a, b])
    write(b, line(1, "active", "zz"))
    assert main(["select", str(a), str(b)]) == 2 and json.loads(capsys.readouterr().out)["reason"] == "diverged_histories"


# ── what the API concludes after a restore (app.kyc.witness, unchanged) ─────────

def test_an_older_database_with_the_newest_witness_is_detected(tmp_path):
    newest = KycWitness(str(write(tmp_path / "w", *HISTORY)))
    # a backup taken at generation 2 restored: its chain at the anchor (3) does not exist
    assert newest.problem("active", 2, None) == "restore_suspected"
    # and a database rolled back to draft (enforcement lost) is caught by the state rank too
    assert newest.problem("draft", 0, None) == "restore_suspected"
    assert newest.problem("active", 3, "c3") is None  # the database that matches the newest witness


def test_the_bypass_this_tooling_prevents(tmp_path):
    """An older database paired with ITS OWN older witness copy looks consistent: exactly what recovery must avoid,
    which is why select() always takes the newest copy and nothing installs a witness automatically."""
    own_old = KycWitness(str(write(tmp_path / "old", *HISTORY[:2])))
    assert own_old.problem("active", 2, "c2") is None  # silently accepted: later restrictions would be lost
    newest = select([tmp_path / "old", write(tmp_path / "live", *HISTORY)])
    assert KycWitness(newest["chosen"]).problem("active", 2, None) == "restore_suspected"


def test_a_missing_witness_fails_closed_once_the_database_has_history(tmp_path):
    missing = KycWitness(str(tmp_path / "gone.witness"))
    assert missing.problem("active", 3, "c3") == "witness_missing"
    assert missing.problem("draft", 0, None) is None  # only the untouched pre-rollout database
