"""Upload grants bound to an upload type; verify (no side effects) and consume (single use) separately.
The legacy untyped 4-part form is no longer issued or accepted."""

import pytest

from app.security.crypto import hmac_sha256_hex
from app.security.nonce_store import InMemoryNonceStore
from app.security.upload_grant import GRANT_TTL_MS, consume_upload_grant, issue_upload_grant, verify_upload_grant

SECRET = "TEST-ONLY-grant-secret"
NOW = 1_800_000_000_000


def legacy_untyped_grant(secret: str, slug: str, now_ms: int) -> str:
    """The legacy 4-part form ("<slug>.<expiryMs>.<nonce>.<hmac>"), correctly signed: no longer accepted."""
    payload = f"{slug}.{now_ms + GRANT_TTL_MS}.{'a' * 32}"
    return f"{payload}.{hmac_sha256_hex(secret, payload)}"


def test_typed_grant_format_and_binding():
    grant, expires_at = issue_upload_grant(SECRET, "creator-a", kind="voice", now_ms=NOW)
    slug, kind, expiry, nonce, sig = grant.split(".")
    assert (slug, kind, int(expiry)) == ("creator-a", "voice", NOW + GRANT_TTL_MS)
    assert len(nonce) == 32 and len(sig) == 64 and expires_at == "2027-01-15T08:10:00.000Z"
    ok = verify_upload_grant(SECRET, grant, "creator-a", kind="voice", now_ms=NOW)
    assert ok.ok and ok.nonce == nonce
    assert verify_upload_grant(SECRET, grant, "creator-a", kind="media", now_ms=NOW).error \
        == "Upload grant is not valid for this upload type"
    assert verify_upload_grant(SECRET, grant, "creator-b", kind="voice", now_ms=NOW).error \
        == "Upload grant does not match this creator"


def test_the_legacy_untyped_form_is_refused_and_a_type_is_always_required():
    legacy = legacy_untyped_grant(SECRET, "creator-a", NOW)  # correctly signed, but no upload type
    for kind in ("voice", "media"):
        assert verify_upload_grant(SECRET, legacy, "creator-a", kind=kind, now_ms=NOW).error == "Malformed upload grant"
    typed, _ = issue_upload_grant(SECRET, "creator-a", kind="media", now_ms=NOW)
    with pytest.raises(TypeError):  # no untyped verification or issuing any more
        verify_upload_grant(SECRET, typed, "creator-a", now_ms=NOW)
    with pytest.raises(TypeError):
        issue_upload_grant(SECRET, "creator-a", now_ms=NOW)
    for kind in (None, "", "video"):
        with pytest.raises(ValueError):
            verify_upload_grant(SECRET, typed, "creator-a", kind=kind, now_ms=NOW)


def test_tampering_expiry_and_size():
    grant, _ = issue_upload_grant(SECRET, "creator-a", kind="voice", now_ms=NOW)
    slug, kind, expiry, nonce, sig = grant.split(".")
    for forged in (f"{slug}.media.{expiry}.{nonce}.{sig}", f"{slug}.{kind}.{int(expiry) + 1}.{nonce}.{sig}",
                   f"{slug}.{kind}.{expiry}.{'b' * 32}.{sig}"):
        check = verify_upload_grant(SECRET, forged, "creator-a", kind=forged.split(".")[1], now_ms=NOW)
        assert check.error == "Invalid upload grant"
    assert verify_upload_grant("TEST-ONLY-other", grant, "creator-a", kind="voice", now_ms=NOW).error \
        == "Invalid upload grant"
    assert verify_upload_grant(SECRET, grant, "creator-a", kind="voice", now_ms=NOW + GRANT_TTL_MS).ok  # last ms
    assert verify_upload_grant(SECRET, grant, "creator-a", kind="voice", now_ms=NOW + GRANT_TTL_MS + 1).error \
        == "Upload grant expired"
    for bad in (f"{slug}.{kind}.{expiry}.NOTHEX{'0' * 26}.{sig}", grant + "x" * 600, None, "", 42):
        assert not verify_upload_grant(SECRET, bad, "creator-a", kind="voice", now_ms=NOW).ok


def test_unknown_kind_cannot_be_issued():
    with pytest.raises(ValueError):
        issue_upload_grant(SECRET, "creator-a", kind="video", now_ms=NOW)


@pytest.mark.anyio
async def test_verify_has_no_side_effects_consume_is_single_use():
    grant, _ = issue_upload_grant(SECRET, "creator-a", kind="voice", now_ms=NOW)
    store = InMemoryNonceStore()
    check = verify_upload_grant(SECRET, grant, "creator-a", kind="voice", now_ms=NOW)
    assert verify_upload_grant(SECRET, grant, "creator-a", kind="voice", now_ms=NOW).ok  # still unused
    assert (await consume_upload_grant(store, check)).ok
    assert f"upload:{check.nonce}" in store
    again = await consume_upload_grant(store, check)
    assert (again.ok, again.error, again.unavailable) == (False, "Upload grant already used", False)
    third = await consume_upload_grant(store, verify_upload_grant(SECRET, grant, "creator-a", kind="voice", now_ms=NOW))
    assert third.error == "Upload grant already used"


@pytest.mark.anyio
async def test_nonce_store_failure_fails_closed_and_is_flagged():
    grant, _ = issue_upload_grant(SECRET, "creator-a", kind="media", now_ms=NOW)
    store = InMemoryNonceStore()
    store.fail_with = RuntimeError("db down")
    result = await consume_upload_grant(store, verify_upload_grant(SECRET, grant, "creator-a", kind="media", now_ms=NOW))
    assert (result.ok, result.unavailable) == (False, True)
    store.fail_with = None  # the store is back: the grant was never burned, so it is still usable once
    assert (await consume_upload_grant(store, verify_upload_grant(SECRET, grant, "creator-a", kind="media",
                                                                  now_ms=NOW))).ok
