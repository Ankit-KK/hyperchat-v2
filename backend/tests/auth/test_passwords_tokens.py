"""Password hashing (Argon2id + legacy bcrypt), opaque tokens, CSRF derivation, signed payloads."""

import time

import bcrypt
import pytest

from app.auth.passwords import hash_password, needs_rehash, validate_new_password, verify_password
from app.auth.tokens import (
    constant_time_equal,
    csrf_token_for,
    new_hex_token,
    new_token,
    sha256_hex,
    sign_payload,
    verify_payload,
)

SECRET = "TEST-ONLY-auth-secret"


def test_new_hashes_are_argon2id_and_verify():
    h = hash_password("correct horse battery")
    assert h.startswith("$argon2id$")
    assert verify_password("correct horse battery", h)
    assert not verify_password("wrong", h)
    assert not needs_rehash(h)


@pytest.mark.parametrize("prefix", [b"2a", b"2b"])
def test_legacy_bcrypt_verifies_and_needs_rehash(prefix):
    h = bcrypt.hashpw(b"legacy-pass", bcrypt.gensalt(rounds=4, prefix=prefix)).decode()
    assert verify_password("legacy-pass", h)
    assert not verify_password("legacy-pasS", h)
    assert needs_rehash(h)


def test_bcrypt_2y_prefix_from_php_style_hashes():
    h = bcrypt.hashpw(b"legacy-pass", bcrypt.gensalt(rounds=4)).decode()
    assert verify_password("legacy-pass", "$2y$" + h[4:])


def test_bcrypt_long_password_uses_first_72_bytes_like_the_old_systems():
    long = "x" * 100
    h = bcrypt.hashpw(long.encode()[:72], bcrypt.gensalt(rounds=4)).decode()
    assert verify_password(long, h)
    assert verify_password("x" * 72 + "different-tail", h)  # bcrypt semantics, unchanged


@pytest.mark.parametrize("stored", [None, "", "plaintext", "$1$md5crypt", "$argon2id$garbage", "$2b$broken"])
def test_missing_or_unknown_hash_never_matches(stored):
    assert not verify_password("anything", stored)


def test_unknown_format_needs_rehash():
    assert needs_rehash("$argon2id$garbage")


def test_new_password_policy():
    assert validate_new_password("short", 8, 128)
    assert validate_new_password("x" * 129, 8, 128)
    assert validate_new_password("long-enough", 8, 128) is None


def test_tokens_shape_and_hashing():
    assert len(new_hex_token(32)) == 64 and int(new_hex_token(32), 16) >= 0
    assert new_token(32) != new_token(32)
    assert sha256_hex("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_csrf_token_is_per_session_and_per_secret():
    a = csrf_token_for(SECRET, "11111111-1111-4111-8111-111111111111")
    assert a == csrf_token_for(SECRET, "11111111-1111-4111-8111-111111111111")
    assert a != csrf_token_for(SECRET, "22222222-2222-4222-8222-222222222222")
    assert a != csrf_token_for("TEST-ONLY-other", "11111111-1111-4111-8111-111111111111")
    assert constant_time_equal(a, a) and not constant_time_equal(a, a[:-1] + "0")


def test_signed_payload_round_trip_tamper_and_expiry():
    value = sign_payload(SECRET, {"s": "state", "r": "/dashboard"}, 600)
    body = verify_payload(SECRET, value)
    assert body["s"] == "state" and body["r"] == "/dashboard"
    raw, sig = value.rsplit(".", 1)
    assert verify_payload(SECRET, raw + "x." + sig) is None
    assert verify_payload(SECRET, raw + "." + ("0" * len(sig))) is None
    assert verify_payload("TEST-ONLY-other", value) is None
    assert verify_payload(SECRET, value, now=time.time() + 601) is None
    assert verify_payload(SECRET, None) is None and verify_payload(SECRET, "no-dot") is None
