"""TTS text (legacy generate-donation-tts behaviour): sanitising, entity decoding, emoji names (vendored CLDR
Hindi / Python Unicode English, never fetched at runtime), repeat caps, hype lines, audio tags."""

import random
from decimal import Decimal

import pytest

from app.audio import text as t


def test_sanitize_for_tts_strips_tags_invisibles_controls_and_cuts():
    assert t.sanitize_for_tts("<b>hi</b>\u200B there\x07\n\n  you") == "hi there you"
    assert t.sanitize_for_tts("\u202Eevil\u2066") == "evil"
    assert len(t.sanitize_for_tts("x" * 900)) == 500 and len(t.sanitize_for_tts("x" * 900, 80)) == 80
    assert t.sanitize_for_tts(None) == "" and t.sanitize_for_tts("") == ""


@pytest.mark.parametrize("raw, expected", [
    ("Tom &amp; Jerry", "Tom & Jerry"), ("&lt;3 &gt; &quot;x&quot; &#x27;y&#x27; &apos;z&apos;", "<3 > \"x\" 'y' 'z'"),
    ("&#128512;", "\U0001F600"), ("&#x1F600;", "\U0001F600"), ("&amp;lt;", "&lt;"),  # &amp; decoded last, once
    ("&#xD800;", "&#xD800;"), ("&#99999999;", "&#99999999;"),                        # invalid code points kept
])
def test_decode_html_entities(raw, expected):
    assert t.decode_html_entities(raw) == expected


def test_runs_and_repeats_are_capped():
    assert t.collapse_char_runs("noooooo!!!!! 1111111") == "noo!!!!! 11"  # letters/digits only
    assert t.cap_repeated_words_and_phrases(" ".join(["go"] * 9)) == " ".join(["go"] * 5)
    assert t.cap_repeated_words_and_phrases(" ".join(["gg wp"] * 7)) == " ".join(["gg wp"] * 5)
    # 6 equivalent words (case, repeats, trailing punctuation ignored): legacy speaks the FIRST one 5 times
    assert t.cap_repeated_words_and_phrases("GO go Goo go! go go") == "GO GO GO GO GO"
    assert t.cap_repeated_words_and_phrases("a b c d") == "a b c d"


def test_emoji_hindi_names_come_from_the_vendored_cldr_file():
    assert t.convert_emojis_to_text("hi \U0001F600", "Hindi") == "hi दाँत दिखाता चेहरा"
    assert t.convert_emojis_to_text("❤️", None) == "लाल दिल"  # default accent is Hindi; VS16 stripped
    assert t.convert_emojis_to_text("\U0001F44D\U0001F3FD", "hindi") == "थंब्स अप"  # skin tone -> base emoji


def test_emoji_english_names_come_from_python_unicode_data():
    assert t.convert_emojis_to_text("hi \U0001F600", "English") == "hi grinning face"
    assert t.convert_emojis_to_text("\U0001F1EE\U0001F1F3 1️⃣", "Tamil") == "flag keycap 1"
    assert t.convert_emojis_to_text("\U0001F468‍\U0001F4BB", "English") == "man personal computer"


def test_no_runtime_fetch(monkeypatch):
    import httpx

    def refuse(*a, **k):
        raise AssertionError("no network access is allowed while building TTS text")
    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    assert t.convert_emojis_to_text("\U0001F600 \U0001F525", "Hindi")


def test_emoji_spam_detection():
    assert t.is_emoji_spam("\U0001F600\U0001F601\U0001F602\U0001F923\U0001F603\U0001F604")  # 6 unique
    assert not t.is_emoji_spam("\U0001F600" * 6)                                            # repeats
    assert not t.is_emoji_spam("\U0001F600\U0001F601\U0001F602\U0001F923\U0001F603")       # only 5
    assert not t.is_emoji_spam(None)


@pytest.mark.parametrize("inr, tier", [(0, "light"), (39, "light"), (99.99, "light"), (100, "noticeable"),
                                       (250, "high"), (500, "big"), (1000, "hyper"), (2500, "insane"),
                                       (5000, "legendary"), (99999, "legendary"), ("bad", "light"), (None, "light")])
def test_hype_tiers(inr, tier):
    assert t.hype_tier(inr) == tier


def test_hype_pool_is_seven_tiers_of_twenty_lines_with_placeholders():
    assert set(t.HYPE_LINES) == {"light", "noticeable", "high", "big", "hyper", "insane", "legendary"}
    assert all(len(lines) == 20 and all("{user_name}" in x and "{amount}" in x for x in lines)
               for lines in t.HYPE_LINES.values())


def test_spoken_amount():
    assert t.spoken_amount(Decimal("40.00"), "INR") == "40 rupees"
    assert t.spoken_amount("1.50", "USD") == "1.5 dollars"
    assert t.spoken_amount(Decimal("100"), "AUD") == "100 Australian dollars"


def test_audio_tags_cannot_be_injected_and_are_stripped_for_plain_providers():
    assert t.escape_audio_tags("[shouting] buy [now]") == " shouting  buy  now "
    assert t.strip_audio_tags("[excited] Ravi just sent 40 rupees! [gasps] wow") == "Ravi just sent 40 rupees! wow"


def test_announcement_text_donation():
    rng = random.Random(7)
    text = t.announcement(donor_name="Ravi &amp; Co", amount=Decimal("40.00"), currency="INR",
                          amount_inr=Decimal("40.00"), message="nice &lt;3 [shouting] stream \U0001F525",
                          media_type=None, is_media=False, language_boost="English", rng=rng)
    assert "Ravi & Co" in text and "40 rupees" in text
    assert text.endswith("nice <3  shouting  stream fire")  # donor brackets neutralised, emoji named
    line = text.split(" nice")[0]
    assert line.replace("Ravi & Co", "{user_name}").replace("40 rupees", "{amount}") in t.HYPE_LINES["light"]


def test_announcement_media_and_empty_message():
    media = t.announcement(donor_name="", amount=500, currency="INR", amount_inr=500, message="ignored",
                           media_type="gif", is_media=True, language_boost=None, rng=random.Random(1))
    assert "Anonymous" in media and media.endswith("And they sent a GIF.") and "ignored" not in media
    assert t.announcement(donor_name="A", amount=1, currency="USD", amount_inr=89, message=None, media_type="video",
                          is_media=True, language_boost=None).endswith("And they sent a video.")
    plain = t.announcement(donor_name="A", amount=40, currency="INR", amount_inr=40, message="​ ...",
                           media_type=None, is_media=False, language_boost=None, rng=random.Random(2))
    assert plain.replace("A", "{user_name}", 1).replace("40 rupees", "{amount}") in t.HYPE_LINES["light"]
