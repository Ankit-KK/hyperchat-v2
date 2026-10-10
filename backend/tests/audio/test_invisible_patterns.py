"""Security audit F8: the invisible-character patterns are written with \\u escapes instead of literal characters.
These pin what they match, so that rewrite (and any later edit) cannot silently change donor-text sanitization.
The fingerprints were taken from the patterns before the rewrite (literal characters, 2026-10-11)."""

import hashlib

import pytest

from app.audio import moderation, text

ALL_CODE_POINTS = [cp for cp in range(0x110000) if not 0xD800 <= cp <= 0xDFFF]


def matched(pattern) -> list[int]:
    return [cp for cp in ALL_CODE_POINTS if pattern.fullmatch(chr(cp))]


@pytest.mark.parametrize("pattern, source, count, sha256", [
    (moderation._INVISIBLE,
     "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e"
     "\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\U000e0000-\U000e007f]",
     185, "7d0c8e945355a2e35cd31fbffc3e47b292fd9ed2a9f0304461dfe5b7d6cfe243"),
    (text._ZERO_WIDTH_AND_BIDI, "[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]",
     15, "976fd2898afa3f15ccf6f091cec3a87d9d58d3d22cf58802cb1387fee2e19c1c"),
])
def test_patterns_match_exactly_what_they_matched_before(pattern, source, count, sha256):
    assert pattern.pattern == source  # the same compiled pattern text as with literal characters
    cps = matched(pattern)  # every Unicode code point, not a sample
    assert len(cps) == count and hashlib.sha256(",".join(map(str, cps)).encode()).hexdigest() == sha256


@pytest.mark.parametrize("donor_text, tts, normalized", [  # outputs recorded with the literal-character patterns
    ("\u202ehi\u2066 the\u200bre\u00ad\ufeff\u2069 you\u200f", "hi there\u00ad you", "hi there you"),
    ("ki\u034fll\u3164", "ki\u034fll\u3164", "kill"),
    ("\u061cnor\u180bmal \U000e0041tag", "\u061cnor\u180bmal \U000e0041tag", "normal tag"),
    ("e\u0301moji \u2764\ufe0f \U0001f469\u200d\U0001f4bb", "e\u0301moji \u2764\ufe0f \U0001f469\U0001f4bb",
     "emoji \u2764 \U0001f469\U0001f4bb"),
    ("plain text", "plain text", "plain text"),
])
def test_donor_text_is_cleaned_exactly_as_before(donor_text, tts, normalized):
    assert text.sanitize_for_tts(donor_text) == tts
    assert moderation.base_normalize(donor_text) == normalized
