"""Donation minimums (E2). Pure functions; no I/O.

Same rules as the legacy get-streamer-pricing (decision D4): effective minimum = max(platform floor,
creator minimum), converted with the fixed 8-currency table and rounded "nicely" for display.
Voice tiers: active creator tiers, else active platform tiers, else the built-in fallback.
Unlike legacy, unsupported currencies are rejected instead of silently priced as INR.
"""

import math
from decimal import Decimal

PLATFORM_FLOORS_INR = {"text": 20, "tts": 40, "voice": 0, "hypersound": 30, "media": 0}
EXCHANGE_RATES_TO_INR: dict[str, float] = {
    "INR": 1, "USD": 89, "EUR": 94, "GBP": 113, "AED": 24, "AUD": 57, "CAD": 64, "PHP": 1.5,
}
FALLBACK_VOICE_TIERS_INR = [(0, 8), (150, 12), (300, 15)]


class UnsupportedCurrency(ValueError):
    pass


def normalize_currency(value: str | None) -> str:
    currency = (value or "INR").strip().upper()
    if currency not in EXCHANGE_RATES_TO_INR:
        raise UnsupportedCurrency(currency)
    return currency


def round_nice(value: float, currency: str) -> float:
    if currency == "INR":
        return float(math.ceil(value / 10) * 10)
    if currency == "AED":
        return float(math.ceil(value))
    return math.ceil(value * 2) / 2


def convert(amount_inr: float, currency: str) -> float:
    return round_nice(amount_inr / EXCHANGE_RATES_TO_INR[currency], currency)


def _num(value) -> float:
    if value is None:
        return 0.0
    return float(value) if isinstance(value, (int, float, Decimal)) else 0.0


def minimums(streamer: dict, currency: str) -> dict[str, float]:
    effective = {
        "text": max(PLATFORM_FLOORS_INR["text"], _num(streamer["min_text_amount_inr"])),
        "tts": max(PLATFORM_FLOORS_INR["tts"], _num(streamer["min_tts_amount_inr"])),
        "voice": max(PLATFORM_FLOORS_INR["voice"], _num(streamer["min_voice_amount_inr"])),
        "hypersound": max(PLATFORM_FLOORS_INR["hypersound"], _num(streamer["min_hypersound_amount_inr"])),
        "media": max(PLATFORM_FLOORS_INR["media"], _num(streamer["media_min_amount"])),
    }
    return {f"min_{k}": convert(v, currency) for k, v in effective.items()}


def message_char_tiers(raw, currency: str) -> list[dict] | None:
    """Convert the creator's {min_amount (INR), max_chars} tiers; malformed entries are skipped."""
    if not isinstance(raw, list):
        return None
    tiers = []
    for tier in raw:
        if not isinstance(tier, dict):
            continue
        amount, chars = tier.get("min_amount"), tier.get("max_chars")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or isinstance(chars, bool) \
                or not isinstance(chars, int):
            continue
        tiers.append({"min_amount": convert(float(amount), currency), "max_chars": chars})
    return tiers


def voice_tiers(streamer_tiers: list[tuple], platform_tiers: list[tuple], currency: str) -> list[dict]:
    source = streamer_tiers or platform_tiers or FALLBACK_VOICE_TIERS_INR
    return [{"min_amount": convert(float(amount), currency), "max_duration_seconds": int(seconds)}
            for amount, seconds in source]
