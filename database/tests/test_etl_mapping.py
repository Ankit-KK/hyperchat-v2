import contextlib
import hashlib
from types import SimpleNamespace

import pytest

from etl.mapping import (
    normalize_currency,
    obs_token_hash,
    razorpay_account,
    slug_from_table,
    split_approved_by,
    user_role,
)

MOD = "6f0e8f2a-1c2b-4d3e-9f40-0a1b2c3d4e5f"


@pytest.mark.parametrize("value, expected", [
    (None, (None, None)),
    ("", (None, None)),
    (MOD, (MOD, None)),
    (MOD.upper(), (MOD, None)),
    ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", (None, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")),  # unknown UUID stays text
    ("system", (None, "system")),
    ("Some Moderator", (None, "Some Moderator")),
])
def test_split_approved_by(value, expected):
    assert split_approved_by(value, {MOD}) == expected


@pytest.mark.parametrize("value, expected", [("INR", "INR"), (" usd ", "USD"), (None, "INR"), ("eur", "EUR")])
def test_normalize_currency(value, expected):
    assert normalize_currency(value) == expected


@pytest.mark.parametrize("value", ["RUPEE", "1NR", "", "US"])
def test_normalize_currency_rejects_invalid(value):
    with pytest.raises(ValueError):
        normalize_currency(value)


def test_obs_token_hash_is_sha256_hex():
    token = "obs_" + "ab" * 32
    assert obs_token_hash(token) == hashlib.sha256(token.encode()).hexdigest()
    assert len(obs_token_hash(token)) == 64


def test_user_role_folds_admin_emails_case_insensitively():
    admins = {"admin@example.invalid"}
    assert user_role("user", "Admin@Example.Invalid", admins) == "admin"
    assert user_role("streamer", "s@example.invalid", admins) == "streamer"
    assert user_role(None, "u@example.invalid", admins) == "user"


@pytest.mark.parametrize("key, secret, expected", [
    ("razorpay-keyid", "razorpay-keysecret", "primary"),
    ("razorpay-keyid(test)", "razorpay-keysecret(test)", "primary"),  # D6: test-mode keys, not a 2nd account
    ("razorpay-keyid", "razorpay-keysecret(test)", None),  # mixed pair: no account
    (None, None, None),
    ("razorpay-keyid", None, None),
    ("RAZORPAY-KEYID", "RAZORPAY-KEYSECRET", None),  # names are exact
])
def test_razorpay_account(key, secret, expected):
    assert razorpay_account(key, secret) == expected


def test_slug_from_table():
    assert slug_from_table("gaming_with_latifa_donations") == "gaming_with_latifa"
    with pytest.raises(ValueError):
        slug_from_table("streamers")


def test_both_legacy_key_pairs_are_the_one_account_and_orders_get_none():
    """D6: the "(test)" pair held test-mode keys, an environment concern, not a second account. Legacy orders never
    recorded an account, so the loader writes none (donations.payment_account stays NULL)."""
    from etl.mapping import RAZORPAY_ACCOUNTS
    from etl.rehearsal import DONATION_COLUMNS

    assert set(RAZORPAY_ACCOUNTS.values()) == {"primary"} and len(RAZORPAY_ACCOUNTS) == 2
    assert "payment_account" not in DONATION_COLUMNS and "provider_payment_id" not in DONATION_COLUMNS


# ── D9: media URL base rewrite ─────────────────────────────────────────────────

LEGACY = ("https://pub-aaa.r2.dev", "https://pub-bbb.r2.dev")
TARGET = "https://media.example.test"
VOICE = "voice-messages/creator_1700000000000_" + "a" * 32 + ".webm"


@pytest.mark.parametrize("url, expected", [
    (f"{LEGACY[0]}/{VOICE}", f"{TARGET}/{VOICE}"),                                    # voice
    (f"{LEGACY[1]}/donation-media/x_1700000000000_{'b' * 32}.png",
     f"{TARGET}/donation-media/x_1700000000000_{'b' * 32}.png"),                       # the second legacy host
    (f"{LEGACY[0]}/soundboards/s/x.mp3", f"{TARGET}/soundboards/s/x.mp3"),            # soundboard
    (f"{LEGACY[0]}/tts-audio/a/b.mp3", f"{TARGET}/tts-audio/a/b.mp3"),
    (f"{TARGET}/{VOICE}", f"{TARGET}/{VOICE}"),                                       # already on the target
    ("https://xyz.supabase.co/storage/v1/object/public/a.png",
     "https://xyz.supabase.co/storage/v1/object/public/a.png"),                        # unrelated: unchanged
    ("https://pub-aaa.r2.dev.evil.example/" + VOICE, "https://pub-aaa.r2.dev.evil.example/" + VOICE),
    (f"{LEGACY[0]}", f"{LEGACY[0]}"), (f"{LEGACY[0]}/", f"{LEGACY[0]}/"),           # no key: unchanged
    (None, None),
])
def test_rewrite_media_url(url, expected):
    from etl.mapping import rewrite_media_url
    assert rewrite_media_url(url, LEGACY, TARGET) == expected
    assert rewrite_media_url(expected, LEGACY, TARGET) == expected  # idempotent
    assert rewrite_media_url(url, LEGACY, None) == url               # no target: copy unchanged


@pytest.mark.parametrize("value, ok", [
    ("https://media.example.test", "https://media.example.test"), ("https://media.example.test/", "https://media.example.test"),
    ("https://x.example/media/", "https://x.example/media"), ("http://media.example.test", None), ("", None),
    ("https://media.example.test/?a=1", None), ("https://u:p@media.example.test", None), ("media.example.test", None),
])
def test_media_base(value, ok):
    from etl.mapping import media_base
    if ok is None:
        with pytest.raises(ValueError):
            media_base(value)
    else:
        assert media_base(value) == ok


def test_loader_rewrites_only_media_url_columns_and_counts():
    from etl.rehearsal import DONATION_COLUMNS, Loader
    loader = Loader(None, None, media_from=LEGACY, media_to=TARGET)
    i = {c: DONATION_COLUMNS.index(c) for c in ("voice_message_url", "media_url", "message", "hypersound_url")}
    row = [None] * len(DONATION_COLUMNS)
    row[i["voice_message_url"]] = f"{LEGACY[0]}/{VOICE}"
    row[i["message"]] = f"{LEGACY[0]}/{VOICE}"            # not a media column: never touched
    row[i["hypersound_url"]] = "https://elsewhere.example/a.mp3"
    out = loader.media("donations", DONATION_COLUMNS, [tuple(row)])[0]
    assert out[i["voice_message_url"]] == f"{TARGET}/{VOICE}" and out[i["message"]] == f"{LEGACY[0]}/{VOICE}"
    assert out[i["hypersound_url"]] == "https://elsewhere.example/a.mp3" and out[i["media_url"]] is None
    assert dict(loader.media_rewritten) == {"donations.voice_message_url": 1}
    assert loader.media("donations", DONATION_COLUMNS, [out])[0] == out  # idempotent
    assert loader.media("visits", ("voice_message_url",), [(f"{LEGACY[0]}/{VOICE}",)]) == [(f"{LEGACY[0]}/{VOICE}",)]
    plain = Loader(None, None)
    assert plain.media("donations", DONATION_COLUMNS, [tuple(row)]) == [tuple(row)]  # no rewrite configured


def test_loader_puts_every_legacy_creator_on_the_legacy_kyc_track():
    """Migration 0020: legacy has no kyc_track / kyc_baseline_owner_id. The import writes 'legacy' and the creator's
    imported owner as the baseline (so an existing creator keeps accepting donations), and keeps its own status."""
    from etl.rehearsal import Loader

    queries: list[str] = []
    loader = Loader(None, None)
    loader.fetch = lambda query, params=None: queries.append(query) or []
    loader.columns = lambda conn, table: ["id", "streamer_slug", "status", "user_id", "kyc_track",
                                          "kyc_baseline_owner_id"]
    loader.copy = lambda table, columns, rows: 0
    loader.tgt = SimpleNamespace(cursor=lambda: contextlib.nullcontext(SimpleNamespace(execute=lambda *a: None)))
    loader.load_users_and_streamers()
    assert queries[-1] == ('SELECT "id", "streamer_slug", "status", "user_id", \'legacy\', "user_id" '
                           'FROM public.streamers')
