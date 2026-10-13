import hashlib

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
