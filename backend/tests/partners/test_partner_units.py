"""Partner (ClipWave, D4) field rules and slug normalization: the legacy contract, without a database."""

import pytest

from app.partners import validation as v

GOOD = {"channel_name": "Clip Wave Tester", "mobile_number": "+91 98765-43210",
        "youtube_channel": "https://youtube.com/@clipwavetest", "email": "Creator@Example.COM",
        "instagram_handle": "@clipwave"}


@pytest.mark.parametrize("text, legacy, candidates", [
    ("clumsy_god", "clumsy_god", ["clumsy_god"]),
    ("  Clumsy God ", "clumsy_god", ["clumsy_god"]),
    ("Clumsy-God", "clumsy_god", ["clumsy_god", "clumsy-god"]),
    ("Clip--Wave__TV!!", "clip_wave_tv", ["clip_wave_tv", "clip--wave_tv"]),
    ("_x_", "x", ["x"]),
    ("😀", "", []),
    ("---", "", []),
])
def test_slug_normalization_matches_legacy_plus_dashed_form(text, legacy, candidates):
    assert v.legacy_slug(text) == legacy
    assert v.slug_candidates(text) == candidates


def test_valid_signup_is_trimmed_and_the_email_lowercased():
    errors, value = v.signup({**GOOD, "channel_name": "  Clip Wave Tester "})
    assert errors == {}
    assert value == {"channel_name": "Clip Wave Tester", "mobile_number": "+91 98765-43210",
                     "youtube_channel": "https://youtube.com/@clipwavetest", "email": "creator@example.com",
                     "instagram_handle": "@clipwave"}


def test_optional_instagram_and_the_legacy_name_alias():
    body = {k: val for k, val in GOOD.items() if k not in ("channel_name", "instagram_handle")}
    errors, value = v.signup({**body, "name": "Legacy Alias"})
    assert errors == {} and value["channel_name"] == "Legacy Alias" and value["instagram_handle"] is None


@pytest.mark.parametrize("field, bad", [
    ("channel_name", "x"), ("channel_name", "y" * 101), ("channel_name", "Nice 😀 Name"), ("channel_name", None),
    ("mobile_number", "12345"), ("mobile_number", "1" * 16), ("mobile_number", ""),
    ("email", "not-an-email"), ("email", "a@b.c"), ("email", "a" * 250 + "@x.com"), ("email", ""),
    ("youtube_channel", "https://evil.example/@x"), ("youtube_channel", "https://youtube.com@evil.example/x"),
    ("youtube_channel", "ftp://youtube.com/x"), ("youtube_channel", "https://youtube.com/" + "a" * 300),
    ("youtube_channel", ""), ("instagram_handle", "i" * 101),
])
def test_each_field_rule(field, bad):
    body = {**GOOD, field: bad} if bad is not None else {k: x for k, x in GOOD.items() if k != field}
    errors, _ = v.signup(body)
    assert set(errors) == {field}, errors


def test_youtube_scheme_is_added_and_hosts_allowed():
    for link, stored in (("youtube.com/@x", "https://youtube.com/@x"), ("www.youtube.com/@x", "https://www.youtube.com/@x"),
                         ("http://m.youtube.com/c/x", "http://m.youtube.com/c/x"), ("youtu.be/abc", "https://youtu.be/abc")):
        errors, value = v.signup({**GOOD, "youtube_channel": link})
        assert errors == {} and value["youtube_channel"] == stored, link


@pytest.mark.parametrize("body", [
    {**GOOD, "partner_slug": "someone-else"}, {**GOOD, "status": "approved"}, {**GOOD, "id": "x"},
])
def test_unknown_fields_are_refused(body):
    errors, _ = v.signup(body)
    assert [k for k in errors] == [k for k in body if k not in v.SIGNUP_FIELDS]


@pytest.mark.parametrize("body", [[], "x", 5, None])
def test_body_must_be_an_object(body):
    assert v.signup(body)[0] == {"body": "The body must be a JSON object."}
    assert v.check(body)[0] == {"body": "The body must be a JSON object."}


def test_non_strings_and_control_characters_are_refused():
    assert set(v.signup({**GOOD, "email": ["a@b.co"]})[0]) == {"email"}
    assert set(v.signup({**GOOD, "channel_name": "Clip\nWave"})[0]) == {"channel_name"}


@pytest.mark.parametrize("body, ok", [
    ({"channel_name": "clumsy_god"}, True), ({"channel_name": "  Clumsy God "}, True),
    ({"channel_name": ""}, False), ({"channel_name": "x" * 101}, False), ({"channel_name": 5}, False),
    ({}, False), ({"channel_name": "!!!"}, False), ({"channel_name": "a", "email": "x"}, False),
])
def test_check_body(body, ok):
    errors, candidates = v.check(body)
    assert (not errors) == ok
    if ok:
        assert candidates
