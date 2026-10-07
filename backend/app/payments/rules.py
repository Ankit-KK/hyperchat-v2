"""Pure checkout and completion rules (no I/O; unit-tested).

Legacy sources: create-razorpay-order-unified (validation, sanitising, minimums, order ids) and the two
completion paths (razorpay-webhook, check-payment-status-unified), whose auto-approve rules disagreed; this
is the single approved rule (D2: voice messages are held like media when media moderation is on).
"""

import hashlib
import hmac
import re
import secrets
from decimal import Decimal, InvalidOperation

from app.core.errors import ApiError
from app.public import pricing

NAME_MAX = 100
MESSAGE_MAX = 500
# Sanity cap: Rs 5,00,000 per donation (in INR at the fixed rates), Razorpay's default per-payment ceiling.
AMOUNT_CAP_INR = Decimal("500000")
AUDIO_DELAY_SECONDS = 10  # auto-approved donations play 10 s after payment (legacy)
VAANI_CREATORS = frozenset({"ankit", "captain_vaani"})  # legacy default (HYPERCHAT_VAANI_CREATORS overrides)


def is_vaani_celebration(*, slug: str, message: str | None, has_voice: bool, has_hypersound: bool, has_media: bool,
                         creators: frozenset[str]) -> bool:
    """Legacy razorpay-webhook rule (P10-D13): the message is exactly "!vaani" (trimmed, any case), the donation has
    no voice message, hypersound or media, and the creator is one of the configured creators. Such a donation is
    a purely visual celebration: auto-approved whatever the moderation mode, no audio job, never queued."""
    return (slug in creators and (message or "").strip().lower() == "!vaani"
            and not (has_voice or has_hypersound or has_media))
VOICE_DURATION_TOLERANCE_MS = 100  # same slack as the Phase 6 upload check
STATUS_TOKEN = re.compile(r"^[0-9a-f]{64}$")
ORDER_REF = re.compile(r"^[A-Za-z0-9_-]{1,100}$")  # our order ids, as path parameters
_ORDER_SUFFIX = "abcdefghijklmnopqrstuvwxyz0123456789"
_CENT = Decimal("0.01")

# Unicode Extended_Pictographic (emoji-data), as legacy /\p{Extended_Pictographic}/u: emoji in donor names are
# read out by TTS and used to abuse alerts. Regional indicators, ZWJ and variation selectors are not in it.
_PICTOGRAPHIC = (
    (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x203C), (0x2049, 0x2049), (0x2122, 0x2122), (0x2139, 0x2139),
    (0x2194, 0x2199), (0x21A9, 0x21AA), (0x231A, 0x231B), (0x2328, 0x2328), (0x2388, 0x2388), (0x23CF, 0x23CF),
    (0x23E9, 0x23F3), (0x23F8, 0x23FA), (0x24C2, 0x24C2), (0x25AA, 0x25AB), (0x25B6, 0x25B6), (0x25C0, 0x25C0),
    (0x25FB, 0x25FE), (0x2600, 0x2605), (0x2607, 0x2612), (0x2614, 0x2685), (0x2690, 0x2705), (0x2708, 0x2712),
    (0x2714, 0x2714), (0x2716, 0x2716), (0x271D, 0x271D), (0x2721, 0x2721), (0x2728, 0x2728), (0x2733, 0x2734),
    (0x2744, 0x2744), (0x2747, 0x2747), (0x274C, 0x274C), (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757),
    (0x2763, 0x2767), (0x2795, 0x2797), (0x27A1, 0x27A1), (0x27B0, 0x27B0), (0x27BF, 0x27BF), (0x2934, 0x2935),
    (0x2B05, 0x2B07), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x3030, 0x3030), (0x303D, 0x303D),
    (0x3297, 0x3297), (0x3299, 0x3299), (0x1F000, 0x1F0FF), (0x1F10D, 0x1F10F), (0x1F12F, 0x1F12F),
    (0x1F16C, 0x1F171), (0x1F17E, 0x1F17F), (0x1F18E, 0x1F18E), (0x1F191, 0x1F19A), (0x1F1AD, 0x1F1E5),
    (0x1F201, 0x1F20F), (0x1F21A, 0x1F21A), (0x1F22F, 0x1F22F), (0x1F232, 0x1F23A), (0x1F23C, 0x1F23F),
    (0x1F249, 0x1F3FA), (0x1F400, 0x1F53D), (0x1F546, 0x1F64F), (0x1F680, 0x1F6FF), (0x1F774, 0x1F77F),
    (0x1F7D5, 0x1F7FF), (0x1F80C, 0x1F80F), (0x1F848, 0x1F84F), (0x1F85A, 0x1F85F), (0x1F888, 0x1F88F),
    (0x1F8AE, 0x1F8FF), (0x1F90C, 0x1F93A), (0x1F93C, 0x1F945), (0x1F947, 0x1FAFF), (0x1FC00, 0x1FFFD),
)

_TAGS = re.compile(r"<[^>]*>")
_SCHEMES = re.compile(r"javascript:|vbscript:|data:", re.IGNORECASE)


def bad_request(error: str, reason: str) -> ApiError:
    return ApiError(400, error, reason)


# ── donor name and message ──────────────────────────────────────────────────

def has_pictograph(text: str) -> bool:
    for ch in text:
        cp = ord(ch)
        if cp >= 0xA9 and any(lo <= cp <= hi for lo, hi in _PICTOGRAPHIC):
            return True
    return False


def sanitize(text: str | None, limit: int = MESSAGE_MAX) -> str | None:
    """Legacy sanitizeInput, unchanged so stored text keeps the form every consumer expects: strip HTML tags
    and javascript:/vbscript:/data: schemes, entity-encode & < > " ', trim, cut to `limit` characters."""
    if not text:
        return None
    clean = _SCHEMES.sub("", _TAGS.sub("", text))
    clean = (clean.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
             .replace("'", "&#x27;").strip())
    return clean[:limit] or None


def donor_name(raw: str) -> str:
    if not raw or not raw.strip():
        raise bad_request("Name is required", "name_required")
    if has_pictograph(raw):
        raise bad_request("Name cannot contain emojis", "name_emoji")
    name = sanitize(raw, NAME_MAX)
    if not name:
        raise bad_request("Invalid name provided", "name_invalid")
    return name


# ── amounts ─────────────────────────────────────────────────────────────────

def to_inr(amount: Decimal, currency: str) -> Decimal:
    return (amount * Decimal(str(pricing.EXCHANGE_RATES_TO_INR[currency]))).quantize(_CENT)


def subunits(amount: Decimal) -> int:
    """Razorpay amounts are integers in the smallest unit; all 8 supported currencies have 2 decimals."""
    return int((amount * 100).to_integral_exact())


def checked_amount(value: Decimal, currency: str) -> tuple[Decimal, Decimal]:
    """(amount with 2 decimals, amount in INR). Positive, at most 2 decimal places, below the cap."""
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise bad_request("Valid amount is required", "amount_invalid")
    if value > AMOUNT_CAP_INR * 1000:
        raise bad_request("Amount is too large", "amount_too_large")
    try:
        amount = value.quantize(_CENT)
    except InvalidOperation:
        raise bad_request("Valid amount is required", "amount_invalid") from None
    if amount != value:
        raise bad_request("Amount can have at most 2 decimal places", "amount_precision")
    inr = to_inr(amount, currency)
    if inr > AMOUNT_CAP_INR:
        raise bad_request("Amount is too large", "amount_too_large")
    return amount, inr


def _display(value: float) -> str:
    return f"{value:g}"


MINIMUM_ERRORS = {
    "hypersound": ("min_hypersound", "HyperSounds require minimum {currency} {minimum}"),
    "voice": ("min_voice", "Voice messages require minimum {currency} {minimum}"),
    "media": ("min_media", "Media messages require minimum {currency} {minimum}"),
    "text": ("min_text", "Text messages require minimum {currency} {minimum}"),
}


def check_minimum(creator: dict, kind: str, amount: Decimal, currency: str) -> None:
    """The minimum the donation page shows (Phase 4 pricing, in the donor's currency) for this kind."""
    key, message = MINIMUM_ERRORS[kind]
    minimum = pricing.minimums(creator, currency)[key]
    if amount < Decimal(str(minimum)):
        raise bad_request(message.format(currency=currency, minimum=_display(minimum)), "below_minimum")


def message_char_limit(raw_tiers, amount_inr: Decimal) -> int | None:
    """Legacy: the highest message_char_tiers entry (INR) the amount reaches; None when no tier applies."""
    if not isinstance(raw_tiers, list):
        return None
    tiers = []
    for tier in raw_tiers:
        if not isinstance(tier, dict):
            continue
        minimum, chars = tier.get("min_amount"), tier.get("max_chars")
        if (isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or isinstance(chars, bool)
                or not isinstance(chars, int)):
            continue
        tiers.append((Decimal(str(minimum)), chars))
    for minimum, chars in sorted(tiers, key=lambda t: t[0], reverse=True):
        if amount_inr >= minimum:
            return chars
    return None


def voice_max_seconds(own: list[tuple], platform: list[tuple], amount_inr: Decimal) -> float:
    """Legacy getVoiceMaxDuration: the highest tier the amount reaches, else the lowest tier."""
    source = own or platform or pricing.FALLBACK_VOICE_TIERS_INR
    tiers = sorted(((Decimal(str(a)), float(s)) for a, s in source), key=lambda t: t[0], reverse=True)
    for minimum, seconds in tiers:
        if amount_inr >= minimum:
            return seconds
    return tiers[-1][1]


# ── attachments (Phase 6 uploads, platform hypersounds) ─────────────────────

ATTACHMENT_ERRORS = {"voice": "Invalid voice message", "media": "Invalid media", "hypersound": "Invalid HyperSound"}


def upload_key(url: str, public_base: str, slug: str, kind: str) -> str:
    """The R2 key behind a donor upload URL, only if it is exactly a URL this API issued for THIS creator:
    <public base>/voice-messages/<slug>_<ms>_<hex>.webm or <public base>/donation-media/<slug>_<ms>_<hex>.<ext>.
    Other hosts, other creators, query strings, fragments and encoded or relative paths never match."""
    error = bad_request(ATTACHMENT_ERRORS[kind], f"{kind}_url_invalid")
    prefix = public_base.rstrip("/") + "/"
    if not isinstance(url, str) or not url.startswith(prefix):
        raise error
    key = url[len(prefix):]
    s = re.escape(slug)
    pattern = (rf"voice-messages/{s}_\d{{13}}_[0-9a-f]{{32}}\.webm" if kind == "voice"
               else rf"donation-media/{s}_\d{{13}}_[0-9a-f]{{32}}\.(?:jpg|png|webp|gif)")
    if not re.fullmatch(pattern, key):
        raise error
    return key


def media_type(key: str) -> str:
    """Server-side media type of an uploaded image (Phase 6 values): "gif" or "image"."""
    return "gif" if key.endswith(".gif") else "image"


# ── order ids and status tokens ─────────────────────────────────────────────

def new_order_id(prefix: str | None, slug: str, now_ms: int) -> str:
    """Legacy format <creator order prefix><epoch ms>_<8 random [a-z0-9]>."""
    suffix = "".join(secrets.choice(_ORDER_SUFFIX) for _ in range(8))
    return f"{prefix or slug + '_'}{now_ms}_{suffix}"


def receipt(order_id: str) -> str:
    return order_id[:40]  # Razorpay receipts are at most 40 characters


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_status_token() -> tuple[str, str]:
    """(token for the donor, SHA-256 hex stored in donations.status_token_hash). 32 random bytes as legacy."""
    token = secrets.token_hex(32)
    return token, token_hash(token)


def status_token_matches(stored_hash: str | None, presented: str | None) -> bool:
    """Rows without a hash (legacy) never match (D4). Compared in constant time."""
    if not stored_hash or not isinstance(presented, str) or not STATUS_TOKEN.match(presented):
        return False
    return hmac.compare_digest(token_hash(presented), stored_hash)


# ── completion ──────────────────────────────────────────────────────────────

# Verified mismatches between Razorpay's payment and our order: the donation is treated as failed (legacy).
MISMATCHES = ("amount_mismatch", "currency_mismatch", "payment_mismatch", "payment_not_found")


def payment_mismatch(payment, *, provider_order_id: str | None, amount: Decimal, currency: str) -> str | None:
    """Why a captured payment fetched from Razorpay does not pay exactly this order, or None. Exact match:
    our order was created for exactly subunits(amount) in `currency`."""
    if payment.order_id != provider_order_id or payment.status != "captured":
        return "payment_mismatch"
    if payment.currency != currency:
        return "currency_mismatch"
    if payment.amount != subunits(amount):
        return "amount_mismatch"
    return None


def auto_approves(*, is_hyperemote: bool, has_hypersound: bool, has_voice: bool, has_media: bool,
                  moderation_mode: str | None, media_moderation_enabled: bool | None) -> bool:
    """The single auto-approve rule applied when a payment completes.

    HyperEmotes and HyperSounds are always auto-approved. Otherwise only creators in auto_approve mode
    (the column default) auto-approve, except that with media moderation on (default true) a donation
    carrying media OR a voice message waits for a moderator (D2).
    """
    if is_hyperemote or has_hypersound:
        return True
    if (moderation_mode or "auto_approve") != "auto_approve":
        return False
    media_moderation = True if media_moderation_enabled is None else media_moderation_enabled
    return not (media_moderation and (has_media or has_voice))
