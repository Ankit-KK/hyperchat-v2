"""Phase 7 pure rules: signatures, amounts, names, minimums and tiers, attachment URLs, tokens, the
auto-approve rule, mismatches; plus configuration (live-key guard), accounts, health cache and the
storage rule for head_upload. No network, no database; every secret here is a TEST-ONLY literal."""

import asyncio
import hashlib
import hmac
from decimal import Decimal

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.core.errors import ApiError
from app.payments import rules
from app.payments.accounts import HEALTH_TTL_SECONDS, AccountHealth, PaymentAccounts
from app.payments.razorpay import Payment, RazorpayAccount, checkout_signature_matches, signature_matches
from app.storage.memory import InMemoryObjectStore
from app.storage.r2 import check_upload_key

SECRET = "TEST-ONLY-webhook-secret"
BODY = b'{"event":"payment.captured","payload":{}}'
HEX = "a" * 32
BASE = "https://media.example.test"


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ── signatures ──────────────────────────────────────────────────────────────

def test_signature_valid_and_case_insensitive_hex():
    assert signature_matches(SECRET, BODY, sign(SECRET, BODY))
    assert signature_matches(SECRET, BODY, sign(SECRET, BODY).upper())


@pytest.mark.parametrize("body", [BODY + b" ", BODY[:-1], BODY.replace(b"captured", b"failed"), b""])
def test_tampered_body_never_matches(body):
    assert not signature_matches(SECRET, body, sign(SECRET, BODY))


def test_wrong_or_other_account_secret_never_matches():
    assert not signature_matches("TEST-ONLY-other-account", BODY, sign(SECRET, BODY))
    assert not signature_matches("", BODY, sign("", BODY))  # an unset secret matches nothing


@pytest.mark.parametrize("signature", [None, "", "zz" * 32, sign(SECRET, BODY)[:-1], sign(SECRET, BODY) + "0",
                                       " " + sign(SECRET, BODY), sign(SECRET, BODY).encode().hex()[:64],
                                       "sha256=" + sign(SECRET, BODY), 12345])
def test_malformed_signatures_never_match(signature):
    assert not signature_matches(SECRET, BODY, signature)


def test_checkout_signature_is_over_order_pipe_payment():
    good = sign("TEST-ONLY-key-secret", b"order_ABC|pay_XYZ")
    assert checkout_signature_matches("TEST-ONLY-key-secret", "order_ABC", "pay_XYZ", good)
    assert not checkout_signature_matches("TEST-ONLY-key-secret", "order_ABC", "pay_OTHER", good)
    assert not checkout_signature_matches("TEST-ONLY-key-secret", "order_OTHER", "pay_XYZ", good)
    assert not checkout_signature_matches("TEST-ONLY-other", "order_ABC", "pay_XYZ", good)


# ── names and messages ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["", "   ", "\n\t"])
def test_name_required(raw):
    with pytest.raises(ApiError) as exc:
        rules.donor_name(raw)
    assert (exc.value.status, exc.value.error) == (400, "Name is required")


@pytest.mark.parametrize("raw", ["Ravi \U0001F600", "❤️ fan", "\U0001F346", "© Corp", "⚽ goal",
                                 "\U0001F9D1‍\U0001F4BB dev", "⭐"])
def test_names_with_emoji_rejected(raw):
    with pytest.raises(ApiError) as exc:
        rules.donor_name(raw)
    assert exc.value.error == "Name cannot contain emojis"


@pytest.mark.parametrize("raw", ["Ravi", "रवि", "José Ñúñez", "李雷", "Ahmed_99", "O'Brien", "\U0001F1EE\U0001F1F3"])
def test_names_without_pictographs_accepted(raw):
    assert rules.donor_name(raw)  # regional-indicator flags are not Extended_Pictographic (as legacy)


def test_name_sanitised_like_legacy_and_cut_to_100():
    assert rules.donor_name("<b>Ravi</b> & 'co'") == "Ravi &amp; &#x27;co&#x27;"
    assert len(rules.donor_name("x" * 300)) == 100
    with pytest.raises(ApiError) as exc:
        rules.donor_name("<script></script>")
    assert exc.value.error == "Invalid name provided"


@pytest.mark.parametrize("raw, expected", [
    (None, None), ("", None), ("  hi  ", "hi"),
    ("<img src=x onerror=alert(1)>hello", "hello"),
    ("JavaScript:alert(1)", "alert(1)"),
    ("a vbscript: b DATA:text/html", "a  b text/html"),
    ('5 > 3 & "x" < \'y\'', "5 &gt; 3 &amp; &quot;x&quot; &lt; &#x27;y&#x27;"),
    ("<<b>>", "&gt;"),
])
def test_sanitize_matches_legacy(raw, expected):
    assert rules.sanitize(raw) == expected


def test_sanitize_cuts_after_encoding():
    assert len(rules.sanitize("&" * 600)) == 500


# ── amounts ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, currency, amount, inr", [
    (Decimal("40"), "INR", Decimal("40.00"), Decimal("40.00")),
    (Decimal("1.5"), "USD", Decimal("1.50"), Decimal("133.50")),
    (Decimal("0.01"), "INR", Decimal("0.01"), Decimal("0.01")),
    (Decimal("10.10"), "PHP", Decimal("10.10"), Decimal("15.15")),
    (Decimal("1E+2"), "INR", Decimal("100.00"), Decimal("100.00")),
    (Decimal("100.000"), "INR", Decimal("100.00"), Decimal("100.00")),  # trailing zeros are still 2 decimals
])
def test_amounts_accepted(value, currency, amount, inr):
    assert rules.checked_amount(value, currency) == (amount, inr)


@pytest.mark.parametrize("value, error", [
    (Decimal("0"), "Valid amount is required"), (Decimal("-5"), "Valid amount is required"),
    (Decimal("NaN"), "Valid amount is required"), (Decimal("Infinity"), "Valid amount is required"),
    (Decimal("10.001"), "Amount can have at most 2 decimal places"),
    (Decimal("0.30000000000000004"), "Amount can have at most 2 decimal places"),  # 0.1 + 0.2 sent as a float
    (Decimal("500000.01"), "Amount is too large"), (Decimal("1E+30"), "Amount is too large"),
])
def test_amounts_rejected(value, error):
    with pytest.raises(ApiError) as exc:
        rules.checked_amount(value, "INR")
    assert (exc.value.status, exc.value.error) == (400, error)


def test_cap_applies_in_inr():
    rules.checked_amount(Decimal("5617.97"), "USD")  # 499,999.33 INR
    with pytest.raises(ApiError):
        rules.checked_amount(Decimal("5618"), "USD")  # 500,002 INR


@pytest.mark.parametrize("amount, expected", [(Decimal("40.00"), 4000), (Decimal("0.01"), 1), (Decimal("1.10"), 110),
                                              (Decimal("19.99"), 1999), (Decimal("500000.00"), 50000000)])
def test_subunits_are_exact(amount, expected):
    assert rules.subunits(amount) == expected and isinstance(rules.subunits(amount), int)


# ── minimums and tiers ──────────────────────────────────────────────────────

CREATOR = {"min_text_amount_inr": None, "min_tts_amount_inr": None, "min_voice_amount_inr": Decimal("150"),
           "min_hypersound_amount_inr": Decimal("10"), "media_min_amount": Decimal("100")}


@pytest.mark.parametrize("kind, ok, too_low, message", [
    ("text", "40", "39.99", "Text messages require minimum INR 40"),        # D3: shared Rs 40 floor
    ("hypersound", "30", "29", "HyperSounds require minimum INR 30"),       # platform floor 30 beats creator 10
    ("voice", "150", "149", "Voice messages require minimum INR 150"),
    ("media", "100", "99.5", "Media messages require minimum INR 100"),
])
def test_minimum_by_kind(kind, ok, too_low, message):
    rules.check_minimum(CREATOR, kind, Decimal(ok), "INR")
    with pytest.raises(ApiError) as exc:
        rules.check_minimum(CREATOR, kind, Decimal(too_low), "INR")
    assert (exc.value.status, exc.value.error) == (400, message)


def test_minimum_in_donor_currency_matches_the_pricing_page():
    rules.check_minimum(CREATOR, "text", Decimal("0.5"), "USD")  # 40/89 rounds up to 0.5 on the page
    with pytest.raises(ApiError) as exc:
        rules.check_minimum(CREATOR, "text", Decimal("0.49"), "USD")
    assert exc.value.error == "Text messages require minimum USD 0.5"


TIERS = [{"min_amount": 0, "max_chars": 50}, {"min_amount": 100, "max_chars": 120}, {"min_amount": 500, "max_chars": 300},
         {"min_amount": "x", "max_chars": 999}, "junk", {"min_amount": True, "max_chars": 5}]


@pytest.mark.parametrize("inr, expected", [("40", 50), ("99.99", 50), ("100", 120), ("499", 120), ("500", 300)])
def test_message_char_limit_highest_tier_reached(inr, expected):
    assert rules.message_char_limit(TIERS, Decimal(inr)) == expected


def test_message_char_limit_none_without_tiers():
    assert rules.message_char_limit(None, Decimal("100")) is None
    assert rules.message_char_limit([{"min_amount": 200, "max_chars": 10}], Decimal("100")) is None


@pytest.mark.parametrize("own, platform, inr, expected", [
    ([], [], "40", 8), ([], [], "150", 12), ([], [], "1000", 15),        # built-in fallback tiers
    ([(150, 15), (300, 30)], [], "100", 15),                             # below every tier: the lowest tier
    ([(150, 15), (300, 30)], [], "300", 30),
    ([], [(0, 10), (200, 20)], "199.99", 10),
])
def test_voice_max_seconds(own, platform, inr, expected):
    assert rules.voice_max_seconds(own, platform, Decimal(inr)) == expected


# ── attachment URLs ─────────────────────────────────────────────────────────

VOICE = f"voice-messages/creator-a_1800000000000_{HEX}.webm"
MEDIA = f"donation-media/creator-a_1800000000000_{HEX}.gif"


def test_own_upload_urls_accepted():
    assert rules.upload_key(f"{BASE}/{VOICE}", BASE + "/", "creator-a", "voice") == VOICE
    assert rules.upload_key(f"{BASE}/{MEDIA}", BASE, "creator-a", "media") == MEDIA
    assert rules.media_type(MEDIA) == "gif" and rules.media_type(MEDIA.replace(".gif", ".png")) == "image"


@pytest.mark.parametrize("url, kind", [
    (f"{BASE}/voice-messages/creator-b_1800000000000_{HEX}.webm", "voice"),          # another creator
    (f"{BASE}/voice-messages/creator-a-x_1800000000000_{HEX}.webm", "voice"),        # longer slug, same prefix
    (f"https://evil.example/{VOICE}", "voice"),                                      # another host
    (f"{BASE}.evil.example/{VOICE}", "voice"),                                       # host suffix trick
    (f"https://media.example.test@evil.example/{VOICE}", "voice"),                   # userinfo trick
    (f"http://media.example.test/{VOICE}", "voice"),                                 # not the public base
    (f"{BASE}/{VOICE}?x=1", "voice"), (f"{BASE}/{VOICE}#x", "voice"),                # query / fragment
    (f"{BASE}/voice-messages/../{VOICE}", "voice"), (f"{BASE}/./{VOICE}", "voice"),  # path traversal
    (f"{BASE}/voice-messages%2Fcreator-a_1800000000000_{HEX}.webm", "voice"),       # encoded slash
    (f"{BASE}/voice-messages/creator-a_1800000000000_{HEX}%2Ewebm", "voice"),       # encoded dot
    (f"{BASE}/voice-messages/creator-a_1800000000000_{HEX.upper()}.webm", "voice"),  # not our hex
    (f"{BASE}/voice-messages/creator-a_180000000000_{HEX}.webm", "voice"),           # 12-digit time
    (f"{BASE}/{MEDIA}", "voice"), (f"{BASE}/{VOICE}", "media"),                      # wrong kind
    (f"{BASE}/donation-media/creator-a_1800000000000_{HEX}.svg", "media"),           # not an upload type
    (f"{BASE}/soundboards/creator-a/{HEX}.mp3", "media"), (f"{BASE}/hypersounds/x.mp3", "voice"),
    (f" {BASE}/{VOICE}", "voice"), (f"{BASE}/{VOICE} ", "voice"), (f"{BASE}//{VOICE}", "voice"),
])
def test_foreign_or_crafted_upload_urls_rejected(url, kind):
    with pytest.raises(ApiError) as exc:
        rules.upload_key(url, BASE + "/", "creator-a", kind)
    assert exc.value.status == 400 and exc.value.error in ("Invalid voice message", "Invalid media")


def test_slug_is_matched_literally():
    with pytest.raises(ApiError):  # a slug is never a pattern
        rules.upload_key(f"{BASE}/voice-messages/aXb_1800000000000_{HEX}.webm", BASE, "a.b", "voice")


# ── order ids and status tokens ─────────────────────────────────────────────

def test_order_ids_follow_legacy_format():
    oid = rules.new_order_id("ankit_rp_", "ankit", 1_800_000_000_000)
    assert oid.startswith("ankit_rp_1800000000000_") and len(oid.rsplit("_", 1)[1]) == 8
    assert rules.ORDER_REF.match(oid)
    assert rules.new_order_id(None, "creator-a", 1).startswith("creator-a_1_")
    assert len({rules.new_order_id("p_", "s", 1) for _ in range(200)}) == 200
    assert len(rules.receipt("x" * 80)) == 40


def test_status_token_hash_and_constant_time_check():
    token, digest = rules.new_status_token()
    assert len(token) == 64 and digest == hashlib.sha256(token.encode()).hexdigest() and digest != token
    assert rules.status_token_matches(digest, token)
    assert not rules.status_token_matches(digest, token.upper())
    assert not rules.status_token_matches(digest, token[:-1] + ("0" if token[-1] != "0" else "1"))
    assert not rules.status_token_matches(digest, None) and not rules.status_token_matches(digest, "")
    assert not rules.status_token_matches(None, token)  # legacy rows without a hash (D4)
    assert not rules.status_token_matches(digest, digest)  # the stored hash is not a token


# ── auto-approve (D2) ───────────────────────────────────────────────────────

def approve(**kw):
    base = dict(is_hyperemote=False, has_hypersound=False, has_voice=False, has_media=False,
                moderation_mode="auto_approve", media_moderation_enabled=True)
    return rules.auto_approves(**{**base, **kw})


@pytest.mark.parametrize("kw, expected", [
    ({}, True),                                                          # text, auto mode
    ({"moderation_mode": None}, True),                                   # NULL = auto_approve
    ({"moderation_mode": "manual"}, False),
    ({"has_media": True}, False), ({"has_voice": True}, False),          # D2: voice held like media
    ({"has_media": True, "media_moderation_enabled": False}, True),
    ({"has_voice": True, "media_moderation_enabled": False}, True),
    ({"has_voice": True, "media_moderation_enabled": None}, False),      # NULL = moderation on
    ({"has_hypersound": True, "moderation_mode": "manual"}, True),       # hypersounds always
    ({"is_hyperemote": True, "moderation_mode": "manual", "has_media": True}, True),
    ({"has_voice": True, "moderation_mode": "manual", "media_moderation_enabled": False}, False),
])
def test_auto_approve_rule(kw, expected):
    assert approve(**kw) is expected


# ── payment vs order ────────────────────────────────────────────────────────

@pytest.mark.parametrize("payment, expected", [
    (Payment("pay_A", "order_X", "captured", 4000, "INR"), None),
    (Payment("pay_A", "order_Y", "captured", 4000, "INR"), "payment_mismatch"),
    (Payment("pay_A", None, "captured", 4000, "INR"), "payment_mismatch"),
    (Payment("pay_A", "order_X", "authorized", 4000, "INR"), "payment_mismatch"),
    (Payment("pay_A", "order_X", "refunded", 4000, "INR"), "payment_mismatch"),
    (Payment("pay_A", "order_X", "captured", 4000, "USD"), "currency_mismatch"),
    (Payment("pay_A", "order_X", "captured", 3999, "INR"), "amount_mismatch"),
    (Payment("pay_A", "order_X", "captured", 4001, "INR"), "amount_mismatch"),  # exact, no legacy +-1 slack
])
def test_payment_mismatch(payment, expected):
    assert rules.payment_mismatch(payment, provider_order_id="order_X", amount=Decimal("40.00"),
                                  currency="INR") == expected


# ── configuration: live keys never outside production ───────────────────────

def settings(**env):
    return Settings(_env_file=None, **env)


@pytest.mark.parametrize("app_env", ["development", "test"])
@pytest.mark.parametrize("field", ["razorpay_primary_key_id", "razorpay_secondary_key_id"])
def test_live_keys_refused_outside_production(app_env, field):
    with pytest.raises(ValidationError) as exc:
        settings(app_env=app_env, **{field: "rzp_live_TESTONLYxxxx"})
    text = str(exc.value)
    assert "live Razorpay keys outside production" in text and "rzp_live_TESTONLY" not in text
    assert f"HYPERCHAT_{field.upper()}" in text


def test_test_keys_accepted_and_live_keys_allowed_in_production():
    assert settings(app_env="development", razorpay_primary_key_id="rzp_test_TESTONLY").razorpay_credentials(
        "primary")[0] == "rzp_test_TESTONLY"
    prod = settings(app_env="production", db_label="PRODUCTION", frontend_url="https://hyperchat.space",
                    razorpay_primary_key_id="rzp_live_TESTONLY")
    assert prod.razorpay_credentials("primary")[0] == "rzp_live_TESTONLY"
    with pytest.raises(ValueError):
        prod.razorpay_credentials("tertiary")


def test_accounts_from_settings_and_redaction():
    s = settings(razorpay_primary_key_id=" rzp_test_P ", razorpay_primary_key_secret="TEST-ONLY-ks",
                 razorpay_primary_webhook_secret="TEST-ONLY-wh", razorpay_secondary_webhook_secret="TEST-ONLY-wh2")
    accounts = PaymentAccounts.from_settings(s)
    primary = accounts.for_payments("primary")
    assert primary.key_id == "rzp_test_P" and primary.key_secret.get_secret_value() == "TEST-ONLY-ks"
    assert accounts.for_payments("secondary") is None  # webhook secret only: cannot create or fetch payments
    assert accounts.for_payments(None) is None and accounts.for_payments("tertiary") is None
    assert accounts.webhook_secrets() == [("primary", "TEST-ONLY-wh"), ("secondary", "TEST-ONLY-wh2")]
    assert accounts.summary() == {"payments": ["primary"], "webhooks": ["primary", "secondary"]}
    assert "TEST-ONLY" not in repr(primary) and "rzp_test" not in repr(primary)
    assert PaymentAccounts.from_settings(settings()).summary() == {"payments": [], "webhooks": []}


# ── health cache ────────────────────────────────────────────────────────────

class ProbeClient:
    def __init__(self, results):
        self.results, self.calls = list(results), 0

    async def probe(self, account):
        self.calls += 1
        return self.results.pop(0)


ACCOUNT = RazorpayAccount("primary", "rzp_test_X", SecretStr("TEST-ONLY"), None)


def test_health_cached_for_two_minutes_per_account():
    clock = [0.0]
    client = ProbeClient(["ok", "invalid_key"])
    health = AccountHealth(client, clock=lambda: clock[0])
    assert asyncio.run(health.status(ACCOUNT)) == "ok"
    clock[0] = HEALTH_TTL_SECONDS - 1
    assert asyncio.run(health.status(ACCOUNT)) == "ok" and client.calls == 1
    clock[0] = HEALTH_TTL_SECONDS + 1
    assert asyncio.run(health.status(ACCOUNT)) == "invalid_key" and client.calls == 2


def test_network_errors_are_not_cached_and_mark_overrides():
    clock = [0.0]
    client = ProbeClient(["network_error", "ok"])
    health = AccountHealth(client, clock=lambda: clock[0])
    assert asyncio.run(health.status(ACCOUNT)) == "network_error"
    assert asyncio.run(health.status(ACCOUNT)) == "ok" and client.calls == 2
    health.mark("primary", "invalid_key")
    assert asyncio.run(health.status(ACCOUNT)) == "invalid_key" and client.calls == 2


def test_concurrent_checkouts_share_one_probe():
    client = ProbeClient(["ok"])
    health = AccountHealth(client)

    async def many():
        return await asyncio.gather(*(health.status(ACCOUNT) for _ in range(5)))
    assert asyncio.run(many()) == ["ok"] * 5 and client.calls == 1


# ── storage: head_upload only inspects donor uploads ────────────────────────

@pytest.mark.parametrize("key", ["hypersounds/x.mp3", f"soundboards/a/{'0' * 8}-0000-0000-0000-{'0' * 12}.mp3",
                                 "../voice-messages/a_1800000000000_" + HEX + ".webm", "tts-audio/x.mp3", ""])
def test_head_upload_refuses_anything_but_donor_uploads(key):
    with pytest.raises(ValueError):
        check_upload_key(key)
    with pytest.raises(ValueError):
        asyncio.run(InMemoryObjectStore().head_upload(key))


def test_head_upload_returns_metadata():
    store = InMemoryObjectStore()
    asyncio.run(store.put_object(VOICE, b"abc", content_type="audio/webm", metadata={"duration-ms": "1440"}))
    head = asyncio.run(store.head_upload(VOICE))
    assert (head.size, head.content_type, head.metadata) == (3, "audio/webm", {"duration-ms": "1440"})
    assert asyncio.run(store.head_upload(MEDIA)) is None
