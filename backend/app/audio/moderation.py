"""Deterministic TTS moderation (port of legacy supabase/functions/_shared/moderation, mod-1.1.0+dict-1.2.0).

No AI, no external service. Runs before EVERY paid provider call (TTS, music, sound effects) and fails
closed: invalid input or any internal error means "blocked". The dictionaries are vendored unchanged in
data/moderation_dictionary.json (backend-only: never returned by any API). Parity with the legacy engine is
asserted case by case against a golden fixture produced by the legacy code (tests/audio/fixtures).

Decision order (as legacy): normalize -> exact term -> phrase/context -> squeezed/substring/fuzzy (the only
checks the allowlist can cancel) -> worst severity; MEDIUM_RISK or above blocks.
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_DATA = json.loads((Path(__file__).parent / "data" / "moderation_dictionary.json").read_text(encoding="utf-8"))
DICTIONARY_VERSION = _DATA["DICTIONARY_VERSION"]
MODERATION_VERSION = _DATA["MODERATION_VERSION"]
NORMALIZATION_VERSION = _DATA["NORMALIZATION_VERSION"]
MAX_INPUT_CHARS = 2000  # UTF-16 code units, as legacy (JavaScript string length)
BLOCK_CODE = "TTS_CONTENT_BLOCKED"
RANK = {"SAFE": 0, "LOW_RISK": 1, "MEDIUM_RISK": 2, "HIGH_RISK": 3, "CRITICAL": 4}
BLOCK_AT = RANK["MEDIUM_RISK"]

# ── normalization (legacy engine/normalize.ts) ──────────────────────────────
_INVISIBLE = re.compile("[\u00AD\u034F\u061C\u115F\u1160\u17B4\u17B5\u180B-\u180F\u200B-\u200F\u202A-\u202E"
                        "\u2060-\u206F\u3164\uFE00-\uFE0F\uFEFF\uFFA0\U000E0000-\U000E007F]")
_HOMOGLYPHS = {
    "а": "a", "в": "b", "с": "c", "е": "e", "ё": "e", "һ": "h", "н": "h", "і": "i", "ї": "i",
    "ј": "j", "к": "k", "м": "m", "о": "o", "р": "p", "г": "r", "ѕ": "s", "т": "t", "у": "y",
    "х": "x", "ԁ": "d", "ԛ": "q", "ԝ": "w", "п": "n", "и": "u", "ь": "b", "з": "3",
    "α": "a", "β": "b", "ε": "e", "η": "n", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p",
    "τ": "t", "υ": "u", "χ": "x", "γ": "y", "ω": "w", "μ": "u", "ς": "s", "σ": "o",
    "ı": "i", "ł": "l", "ø": "o", "đ": "d", "ħ": "h", "ŧ": "t", "ß": "ss", "æ": "ae", "œ": "oe",
}
_LEET = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "6": "g", "7": "t", "8": "b", "9": "g",
         "@": "a", "$": "s", "!": "i", "|": "i", "+": "t", "€": "e", "£": "l", "¡": "i"}
_TOKEN_SYMBOLS = set("@$!|+€£¡")
_LATIN_MARKS = re.compile("([a-z])[̀-ͯ]+")
_RUN3 = re.compile(r"(.)\1{2,}", re.DOTALL)
_RUN2 = re.compile(r"(.)\1+", re.DOTALL)


def base_normalize(text: str) -> str:
    """Case, Unicode, invisible chars, Latin diacritics, homoglyphs. Keeps spaces/punctuation."""
    if not isinstance(text, str):
        raise TypeError("normalize: input must be a string")
    s = _INVISIBLE.sub("", unicodedata.normalize("NFKC", text)).lower()
    s = unicodedata.normalize("NFC", _LATIN_MARKS.sub(r"\1", unicodedata.normalize("NFD", s)))
    return "".join(_HOMOGLYPHS.get(ch, ch) for ch in s)


def _is_letter(ch: str) -> bool:
    return unicodedata.category(ch).startswith("L")


def leet(token: str) -> str:
    if not any(_is_letter(ch) for ch in token):
        return token
    return "".join(_LEET.get(ch, ch) for ch in token)


def collapse2(token: str) -> str:
    """Runs of 3+ identical chars -> 2 ("niiiice" -> "niice")."""
    return _RUN3.sub(r"\1\1", token)


def collapse1(token: str) -> str:
    """Every run -> 1 (used only with allowlist protection)."""
    return _RUN2.sub(r"\1", token)


def _raw_tokens(text: str) -> list[str]:
    """Runs of letters, marks, digits and the leet symbols (legacy TOKEN_RE)."""
    out, cur = [], []
    for ch in text:
        if unicodedata.category(ch)[0] in "LMN" or ch in _TOKEN_SYMBOLS:
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


@dataclass(frozen=True)
class Normalized:
    base: str
    tokens: list[str]
    squeezed: list[str]
    joined: list[str]  # runs of 3+ fragments of <= 2 chars glued together (high-risk checks only)


def normalize_text(text: str) -> Normalized:
    base = base_normalize(text)
    raw = _raw_tokens(base)
    tokens = [t for t in (collapse2(leet(r)) for r in raw) if t]
    joined, run = [], []
    for r in raw:
        if len(r) <= 2:
            run.append(r)
        else:
            if len(run) >= 3:
                joined.append(collapse2(leet("".join(run))))
            run = []
    if len(run) >= 3:
        joined.append(collapse2(leet("".join(run))))
    return Normalized(base, tokens, [collapse1(t) for t in tokens], joined)


# ── compiled dictionaries ───────────────────────────────────────────────────
@dataclass(frozen=True)
class Entry:
    id: str
    category: str
    severity: str
    high_risk: bool = False


@dataclass(frozen=True)
class Compiled:
    exact: dict
    squeezed: dict
    high_risk_singles: list
    substr: list
    phrases: list
    allow: frozenset


def _toks(s: str) -> list[str]:
    return normalize_text(s).tokens


def compile_dictionaries(terms: list, phrases: list, allowlist: list) -> Compiled:
    if not terms:
        raise ValueError("dictionary_unavailable")
    if not phrases:
        raise ValueError("phrases_unavailable")
    exact, squeezed, high, substr, compiled_phrases = {}, {}, [], [], []

    def keep(m: dict, k: str, e: Entry) -> None:
        cur = m.get(k)
        if cur is None or RANK[e.severity] > RANK[cur.severity]:
            m[k] = e

    for t in terms:
        if not t.get("id") or not t.get("term") or t.get("severity") not in RANK:
            raise ValueError("malformed_dictionary_entry")
        e = Entry(t["id"], t["category"], t["severity"], bool(t.get("highRisk")))
        words = _toks(t["term"])
        if not words:
            continue
        if len(words) > 1:
            compiled_phrases.append((e, words))
            continue
        c2 = words[0]
        keep(exact, c2, e)
        keep(squeezed, collapse1(c2), e)
        if e.high_risk and len(c2) >= 5:
            high.append((e, c2))
        if t.get("substring") and len(collapse1(c2)) >= 4:
            substr.append((e, c2, collapse1(c2)))
    for p in phrases:
        words = _toks(p["phrase"])
        if words:
            compiled_phrases.append((Entry(p["id"], p["category"], p["severity"]), words))
    allow = frozenset(t for a in allowlist for t in _toks(a))
    return Compiled(exact, squeezed, high, substr, compiled_phrases, allow)


@lru_cache(maxsize=1)
def default_compiled() -> Compiled:
    return compile_dictionaries(_DATA["TERMS"], _DATA["PHRASES"], _DATA["ALLOWLIST"])


def _set_of(words: list[str]) -> frozenset:
    out = set()
    for w in words:
        for t in _toks(w):
            out.add(t)
            out.add(collapse1(t))
    return frozenset(out)


_VERBS = _set_of(_DATA["THREAT_VERBS"])
_DIE = _set_of(_DATA["DIE_WORDS"])
_MARKERS = _set_of(_DATA["DIE_MARKERS"])
_TARGETS = _set_of(_DATA["TARGET_WORDS"])
_MINORS = _set_of(_DATA["MINOR_WORDS"])
_CHILD_SEX = _set_of(_DATA["CHILD_SEXUAL_WORDS"])
_TSEX = _set_of(_DATA["TARGETED_SEXUAL_WORDS"])
_AGE = re.compile(r"\b([1-9]|1[0-7])\s?(yo|y/o|yrs?|years?|saal)\b", re.ASCII)


def _in(s: frozenset, t: str) -> bool:
    return t in s or collapse1(t) in s


def _levenshtein(a: str, b: str, limit: int) -> int:
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i]
        row_min = i
        for j in range(1, len(b) + 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if a[i - 1] == b[j - 1] else 1)))
            row_min = min(row_min, cur[j])
        if row_min > limit:
            return limit + 1
        prev = cur
    return prev[len(b)]


def _analyze(n: Normalized, targets: set, c: Compiled) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []  # (rule id, category, severity) in legacy order
    add = hits.append
    tokens = n.tokens
    matched = set()
    # 1. exact term (never cancelled by the allowlist)
    for i, t in enumerate(tokens):
        e = c.exact.get(t)
        if e:
            add((e.id, e.category, e.severity))
            matched.add(i)
    for j in n.joined:  # joined split-letter view: high-risk entries only
        e = c.exact.get(j)
        if e is None and j not in c.allow:
            e = c.squeezed.get(collapse1(j))
        if e is not None and e.high_risk:
            add((e.id, e.category, e.severity))
        elif j not in c.allow:
            for entry, c2, _ in c.substr:
                if c2 in j:
                    add((entry.id, entry.category, entry.severity))
                    break
    # 2. phrases (consecutive tokens, tolerant to repeated letters)
    for entry, words in c.phrases:
        size = len(words)
        for i in range(0, len(tokens) - size + 1):
            if all(tokens[i + k] == w or collapse1(tokens[i + k]) == collapse1(w) for k, w in enumerate(words)):
                add((entry.id, entry.category, entry.severity))
                break

    # 2b. context / combination rules
    def is_target(t: str) -> bool:
        return _in(_TARGETS, t) or _in(targets, t)

    for i, t in enumerate(tokens):
        if _in(_VERBS, t):
            for j in range(i + 1, min(i + 4, len(tokens))):
                if is_target(tokens[j]):
                    add(("ctx.threat.verb_target", "threats", "CRITICAL"))
                    break
        if is_target(t):
            for j in range(max(0, i - 3), min(i + 4, len(tokens))):
                e = c.exact.get(tokens[j])
                if j != i and e is not None and e.category == "sexual_violence":
                    add(("ctx.sv.target", "sexual_violence", "CRITICAL"))
                    break
        if _in(_DIE, t):
            win = tokens[max(0, i - 5):i + 4]
            if any(_in(_MARKERS, w) for w in win) and any(is_target(w) for w in win):
                add(("ctx.threat.die_target", "threats", "HIGH_RISK"))
        if _in(_TSEX, t):
            for j in range(i + 1, min(i + 3, len(tokens))):
                if is_target(tokens[j]):
                    add(("ctx.harass.sexual_target", "harassment", "LOW_RISK"))
                    break
    has_minor = any(_in(_MINORS, t) for t in tokens) or _AGE.search(n.base) is not None
    has_sexual = any(_in(_CHILD_SEX, t) or (c.exact.get(t) is not None and c.exact[t].category == "sexual")
                     for t in tokens)
    if has_minor and has_sexual:
        add(("ctx.child.minor_sexual", "child_safety", "CRITICAL"))

    # 3. squeezed / substring / fuzzy: the only checks the allowlist can cancel
    for i, t in enumerate(tokens):
        if i in matched or t in c.allow:
            continue
        sq = c.squeezed.get(collapse1(t))
        if sq is not None:
            add((sq.id, sq.category, sq.severity))
            continue
        hit = next(((e.id, e.category, e.severity) for e, c2, c1 in c.substr if c2 in t or c1 in collapse1(t)), None)
        if hit:
            add(hit)
            continue
        if len(t) < 5:
            continue
        for e, c2 in c.high_risk_singles:
            if c2[0] != t[0]:
                continue
            limit = 2 if len(c2) >= 7 else 1
            if _levenshtein(t, c2, limit) <= limit:
                add((e.id, e.category, e.severity))
                break
    return hits


@dataclass(frozen=True)
class Decision:
    allowed: bool
    severity: str = "SAFE"
    category: str | None = None
    rule_id: str | None = None
    invalid: bool = False


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


_INVALID = Decision(False, "HIGH_RISK", "invalid", "invalid_input", invalid=True)


def moderate_for_tts(supporter_name: str | None, content: str | None, streamer_name: str | None,
                     *, compiled=default_compiled, normalize=normalize_text) -> Decision:
    """allowed=False on a block, invalid input or ANY internal failure (fail closed)."""
    try:
        parts = [supporter_name, content, streamer_name]
        if any(p is not None and not isinstance(p, str) for p in parts):
            return _INVALID
        name, text, streamer = (p or "" for p in parts)
        if _utf16_len(name) + _utf16_len(text) + _utf16_len(streamer) in (0,) \
                or _utf16_len(name) + _utf16_len(text) + _utf16_len(streamer) > MAX_INPUT_CHARS \
                or not (name + text).strip().strip("﻿").strip():
            return _INVALID
        c = compiled()
        n = normalize(" ".join([name, text, streamer]))
        if n is None or not isinstance(n.tokens, list):
            raise ValueError("normalization_failed")
        targets = set()
        for who in (streamer_name, supporter_name):
            for t in normalize(who or "").tokens:
                if len(t) >= 3:
                    targets.add(t)
                    targets.add(collapse2(t))
        worst = None
        for h in _analyze(n, targets, c):
            if worst is None or RANK[h[2]] > RANK[worst[2]]:
                worst = h
        if worst is not None and RANK[worst[2]] >= BLOCK_AT:
            return Decision(False, worst[2], worst[1], worst[0])
        return Decision(True, *(worst[2], worst[1], worst[0]) if worst else ("SAFE", None, None))
    except Exception:  # noqa: BLE001 - fail closed; never logs the input
        return Decision(False, "CRITICAL", "engine_error", "engine_error")
