"""TTS moderation port: decision-for-decision parity with the legacy engine, plus fail-closed behaviour.

fixtures/moderation_golden.json was produced by running the LEGACY TypeScript engine (mod-1.1.0+dict-1.2.0)
over every string literal of its own test file plus generated variants of every dictionary entry (leet,
spacing, dots, repeats, embedding, phrases, allowlist words, context rules, invalid inputs). Each case stores
the legacy decision and normalised tokens; the Python port must reproduce all of them.
"""

import json
from pathlib import Path

import pytest

from app.audio import moderation as m

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "moderation_golden.json").read_text(encoding="utf-8"))
CASES = GOLDEN["cases"]


def test_versions_match_the_legacy_engine():
    assert GOLDEN["moderation_version"] == m.MODERATION_VERSION == "mod-1.1.0+dict-1.2.0"
    assert m.NORMALIZATION_VERSION == "norm-1.0.0"
    assert len(CASES) > 2500 and sum(1 for c in CASES if not c[3]) > 1000  # blocked and allowed both covered


def _ids(case):
    return f"{case[0]!r}|{case[1]!r}"[:60]


@pytest.mark.parametrize("case", CASES, ids=[_ids(c) for c in CASES])
def test_decision_parity_with_legacy(case):
    name, content, streamer, allowed, invalid, severity, category, rule_id, _, _ = case
    d = m.moderate_for_tts(name, content, streamer)
    assert (d.allowed, d.invalid, d.severity, d.category, d.rule_id) == (allowed, invalid, severity, category, rule_id)


def test_normalization_parity_with_legacy():
    mismatches = []
    for name, content, streamer, *_, tokens, joined in CASES:
        n = m.normalize_text(" ".join([name or "", content or "", streamer or ""]))
        if (n.tokens, n.joined) != (tokens, joined):
            mismatches.append(content)
    assert not mismatches, mismatches[:5]


def test_legacy_normalization_examples():
    assert m.base_normalize("HELLO") == "hello"
    assert m.base_normalize("Ｈｅｌｌｏ") == "hello"
    assert m.base_normalize("hеllо") == "hello"  # Cyrillic е, о
    assert m.base_normalize("he\u200Bl\u200Dlo") == "hello"
    assert m.base_normalize("\u202Ehello") == "hello"
    assert m.base_normalize("héllö") == "hello"
    assert m.normalize_text("h3ll0 w0rld").tokens == ["hello", "world"]
    assert m.normalize_text("niiiiice").tokens == ["niice"]
    assert m.normalize_text("good-game_bro").tokens == ["good", "game", "bro"]
    assert m.normalize_text("20 rupees").tokens == ["20", "rupees"]


@pytest.mark.parametrize("args", [(None, None, None), ("", "", ""), (" ", "  ", "Ankit"), ("a", "x" * 2001, ""),
                                  (123, "hi", "Ankit"), ("Rahul", ["hi"], "Ankit")])
def test_invalid_input_is_blocked(args):
    d = m.moderate_for_tts(*args)
    assert not d.allowed and d.invalid and d.rule_id == "invalid_input"


def test_length_limit_counts_utf16_units_like_legacy():
    emoji = "\U0001F600"  # 2 UTF-16 units
    assert m.moderate_for_tts("R", "a" * 998 + emoji * 501, "").invalid  # 1 + 998 + 1002 = 2001 units
    assert m.moderate_for_tts("R", "a" * 998 + emoji * 500, "").allowed  # 1999 units (only 1499 code points)
    assert m.moderate_for_tts("R", "a" * 999, "x" * 1000).allowed       # exactly 2000


def test_engine_failure_fails_closed():
    def broken():
        raise RuntimeError("dictionary_unavailable")
    d = m.moderate_for_tts("Rahul", "nice stream", "Ankit", compiled=broken)
    assert (d.allowed, d.category, d.rule_id) == (False, "engine_error", "engine_error")

    def bad_normalize(_):
        raise ValueError("boom")
    assert not m.moderate_for_tts("Rahul", "nice stream", "Ankit", normalize=bad_normalize).allowed


def test_malformed_dictionary_refused():
    with pytest.raises(ValueError):
        m.compile_dictionaries([], [{"id": "p", "phrase": "a b", "category": "hate", "severity": "HIGH_RISK"}], [])
    with pytest.raises(ValueError):
        m.compile_dictionaries([{"id": "x", "term": "y", "category": "hate", "severity": "NOPE"}],
                               [{"id": "p", "phrase": "a b", "category": "hate", "severity": "HIGH_RISK"}], [])


def test_names_are_targets():
    assert not m.moderate_for_tts("Rahul", "i will kill priyanka", "Priyanka").allowed
    assert m.moderate_for_tts("Rahul", "kill the boss in this game", "Priyanka").allowed
