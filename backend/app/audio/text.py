"""TTS text, as legacy generate-donation-tts built it (pure functions, no I/O).

announcement = random hype line for the INR amount tier (7 tiers x 20 lines) with the donor name and the
spoken amount, then: the sanitised message (text donations) or "And they sent a GIF/an image/a video."
(media). Messages are stored HTML-escaped by checkout (Phase 7), so entities are decoded before speaking.
Emoji become spoken names: Hindi CLDR names (vendored, pinned: data/cldr_hi_emoji_tts.json) when the
creator's TTS accent is Hindi (the default), else English names from Python's Unicode database. Nothing is
fetched at runtime. Donor-supplied text can never carry ElevenLabs audio tags ([shouting]...).
"""

import json
import random
import re
import unicodedata
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from app.payments.rules import _PICTOGRAPHIC

_DATA = Path(__file__).parent / "data"
HYPE_LINES: dict[str, list[str]] = json.loads((_DATA / "hype_lines.json").read_text(encoding="utf-8"))
TIER_FLOORS = [(5000, "legendary"), (2500, "insane"), (1000, "hyper"), (500, "big"), (250, "high"),
               (100, "noticeable"), (0, "light")]
CURRENCY_SPOKEN_NAMES = {
    "INR": "rupees", "USD": "dollars", "EUR": "euros", "GBP": "pounds", "AED": "dirhams", "AUD": "Australian dollars",
    "CAD": "Canadian dollars", "PHP": "pesos",
}
MESSAGE_MAX = 500
NAME_MAX = 80

_ZERO_WIDTH_AND_BIDI = re.compile("[​-‏‪-‮⁦-⁩﻿]")
_CONTROL = re.compile("[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]")
_TAGS = re.compile(r"<[^>]*>")
_SPACES = re.compile(r"\s+")
_ENTITY_HEX = re.compile(r"&#x([0-9a-fA-F]+);")
_ENTITY_DEC = re.compile(r"&#(\d+);")
_AUDIO_TAG = re.compile(r"\[[^\]]{0,40}\]")
_MODIFIERS = re.compile("[\U0001F3FB-\U0001F3FF‍️︎]")
_SKIN = range(0x1F3FB, 0x1F400)
_REGIONAL = range(0x1F1E6, 0x1F200)


def _pictographic(ch: str) -> bool:
    cp = ord(ch)
    return cp >= 0xA9 and any(lo <= cp <= hi for lo, hi in _PICTOGRAPHIC)


def sanitize_for_tts(value: str | None, max_len: int = MESSAGE_MAX) -> str:
    """Legacy sanitizeForTts: tags -> space, zero-width/bidi and control characters removed, whitespace
    collapsed, cut to max_len."""
    if not value:
        return ""
    out = _CONTROL.sub("", _ZERO_WIDTH_AND_BIDI.sub("", _TAGS.sub(" ", str(value))))
    return _SPACES.sub(" ", out).strip()[:max_len]


def is_speakable(value: str | None) -> bool:
    return bool(value) and any(unicodedata.category(ch)[0] in "LN" or _pictographic(ch) for ch in value)


def decode_html_entities(text: str) -> str:
    """Undo the checkout sanitiser's escaping (legacy decodeHtmlEntities; &amp; last)."""
    if not text:
        return text
    out = re.sub("&lt;", "<", text, flags=re.I)
    out = re.sub("&gt;", ">", out, flags=re.I)
    out = re.sub("&quot;", '"', out, flags=re.I)
    out = re.sub("&apos;", "'", out, flags=re.I)

    def code(m: re.Match, base: int) -> str:
        try:
            cp = int(m.group(1), base)
            return chr(cp) if 0 < cp <= 0x10FFFF and not 0xD800 <= cp <= 0xDFFF else m.group(0)
        except ValueError:
            return m.group(0)
    out = _ENTITY_HEX.sub(lambda m: code(m, 16), out)
    out = _ENTITY_DEC.sub(lambda m: code(m, 10), out)
    return re.sub("&amp;", "&", out, flags=re.I)


def collapse_char_runs(text: str) -> str:
    """A run of 4+ identical letters/digits becomes 2 of them."""
    def keep_two(m: re.Match) -> str:
        ch = m.group(1)
        return ch * 2 if unicodedata.category(ch)[0] in "LN" else m.group(0)
    return re.sub(r"(.)\1{3,}", keep_two, text, flags=re.DOTALL) if text else text


def _normalize_word(word: str) -> str:
    lowered = re.sub(r"(.)\1+", r"\1", word.lower(), flags=re.DOTALL)
    end = len(lowered)
    while end and unicodedata.category(lowered[end - 1])[0] not in "LN":
        end -= 1
    return lowered[:end]


def cap_repeated_words_and_phrases(text: str, max_repeats: int = 5, max_phrase_words: int = 8) -> str:
    """Legacy capRepeatedWordsAndPhrases: a word or phrase (<= 8 words) repeated more than 5 times in a
    row is spoken 5 times."""
    words = text.split()
    norm = [_normalize_word(w) for w in words]
    capped, i = [], 0
    while i < len(words):
        handled = False
        longest = min(max_phrase_words, (len(words) - i) // 2)
        for size in range(longest, 0, -1):
            count = 1
            while i + (count + 1) * size <= len(words) and all(
                    norm[i + k] and norm[i + count * size + k] and norm[i + k] == norm[i + count * size + k]
                    for k in range(size)):
                count += 1
            if count > max_repeats:
                for _ in range(max_repeats):
                    capped.extend(words[i:i + size])
                i += count * size
                handled = True
                break
        if not handled:
            capped.append(words[i])
            i += 1
    return " ".join(capped)


# ── emoji ───────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _hindi_names() -> tuple[dict[str, str], int]:
    names = json.loads((_DATA / "cldr_hi_emoji_tts.json").read_text(encoding="utf-8"))["names"]
    return names, max(len(k) for k in names)


def _english_name(seq: str) -> str:
    if len(seq) >= 2 and all(ord(c) in _REGIONAL for c in seq[:2]):
        return "flag"
    if "⃣" in seq:
        return f"keycap {seq[0]}"
    parts = []
    for ch in seq:
        if ch in "‍️︎" or ord(ch) in _SKIN:
            continue
        name = unicodedata.name(ch, "")
        if name:
            parts.append(name.lower())
    return " ".join(parts)


def _emoji_at(text: str, i: int) -> int:
    """Length of the emoji sequence starting at text[i] (0 when none): a pictograph followed by variation
    selectors / skin tones / ZWJ-joined pictographs, a regional-indicator flag pair, or a keycap."""
    ch = text[i]
    if ord(ch) in _REGIONAL:
        return 2 if i + 1 < len(text) and ord(text[i + 1]) in _REGIONAL else 1
    if ch in "0123456789#*":  # keycap: digit (+ VS16) + COMBINING ENCLOSING KEYCAP
        if text[i + 1:i + 3] == "️⃣":
            return 3
        return 2 if text[i + 1:i + 2] == "⃣" else 0
    if not _pictographic(ch):
        return 0
    j = i + 1
    while j < len(text):
        c = text[j]
        if c in "️︎" or ord(c) in _SKIN:
            j += 1
        elif c == "‍" and j + 1 < len(text) and _pictographic(text[j + 1]):
            j += 2
        else:
            break
    return j - i


def find_emoji(text: str) -> list[str]:
    found, i = [], 0
    while i < len(text):
        size = _emoji_at(text, i)
        if size:
            found.append(text[i:i + size])
            i += size
        else:
            i += 1
    return found


def is_emoji_spam(text: str | None) -> bool:
    """Legacy heuristic: more than 5 emoji and all unique (MiniMax voices loop on long emoji lists)."""
    found = find_emoji(text or "")
    return len(found) > 5 and len(set(found)) == len(found)


def emoji_to_words(text: str, language_boost: str | None) -> str:
    hindi = (language_boost or "Hindi").lower() == "hindi"
    names, longest = _hindi_names()
    out, i = [], 0
    while i < len(text):
        size = _emoji_at(text, i)
        if not size:
            out.append(text[i])
            i += 1
            continue
        seq, spoken = text[i:i + size], None
        if hindi:
            for candidate in (seq, seq.replace("️", "")):
                spoken = spoken or names.get(candidate)
            if spoken is None:  # e.g. skin-tone variants: the base emoji's name
                base = _MODIFIERS.sub("", seq)
                spoken = names.get(base) or names.get(seq[0])
        out.append(f" {spoken or _english_name(seq)} ")
        i += size
    return "".join(out)


def convert_emojis_to_text(text: str, language_boost: str | None) -> str:
    """Legacy convertEmojisToText: decode entities, collapse runs, emoji -> names, drop modifiers,
    collapse spaces, cap repeated words/phrases."""
    if not text:
        return text
    result = emoji_to_words(collapse_char_runs(decode_html_entities(text)), language_boost)
    result = _SPACES.sub(" ", _MODIFIERS.sub("", result)).strip()
    return cap_repeated_words_and_phrases(result)


# ── hype lines and audio tags ───────────────────────────────────────────────
def hype_tier(amount_inr) -> str:
    try:
        amt = float(amount_inr)
    except (TypeError, ValueError):
        amt = 0.0
    for floor, tier in TIER_FLOORS:
        if amt >= floor:
            return tier
    return "light"


def escape_audio_tags(text: str) -> str:
    return re.sub(r"[\[\]]", " ", text)


def strip_audio_tags(text: str) -> str:
    return _SPACES.sub(" ", _AUDIO_TAG.sub(" ", text)).strip()


def spoken_amount(amount, currency: str) -> str:
    """40.00 INR -> "40 rupees", 1.50 USD -> "1.5 dollars" (as legacy's JavaScript number formatting)."""
    text = format(Decimal(str(amount)).normalize(), "f")
    return f"{text} {CURRENCY_SPOKEN_NAMES.get(currency, currency)}"


def pick_hype_line(amount_inr, user_name: str, amount_spoken: str, rng: random.Random | None = None) -> str:
    pool = HYPE_LINES[hype_tier(amount_inr)]
    line = pool[(rng or random).randrange(len(pool))]
    return line.replace("{user_name}", user_name).replace("{amount}", amount_spoken)


def media_description(media_type: str | None) -> str:
    kind = media_type or ""
    if "gif" in kind:
        return "a GIF"
    if kind.startswith("video"):
        return "a video"
    return "an image"


def announcement(*, donor_name: str, amount, currency: str, amount_inr, message: str | None, media_type: str | None,
                 is_media: bool, language_boost: str | None, rng: random.Random | None = None) -> str:
    """The full text sent to the TTS provider (still carrying the hype line's audio tags)."""
    name = sanitize_for_tts(decode_html_entities(donor_name or ""), NAME_MAX) or "Anonymous"
    line = pick_hype_line(amount_inr, name, spoken_amount(amount, currency), rng)
    if is_media:
        return f"{line} And they sent {media_description(media_type)}."
    safe = sanitize_for_tts(message, MESSAGE_MAX) if message else ""
    spoken = convert_emojis_to_text(safe, language_boost) if safe and is_speakable(safe) else ""
    return f"{line} {escape_audio_tags(spoken)}" if spoken else line
