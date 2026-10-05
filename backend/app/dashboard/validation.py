"""Pure input rules for the dashboard API (no I/O), so every rule is unit-testable."""

import re
import uuid
from datetime import datetime


def parse_uuid(value: str | None) -> str | None:
    """Canonical UUID text, or None when `value` is not a UUID (callers answer 404)."""
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


# ── settings (legacy update-streamer-settings allowlist, minus the two read-only amounts) ──────

WRITABLE_SETTINGS = (
    "moderation_mode", "telegram_moderation_enabled", "discord_moderation_enabled", "tts_enabled",
    "hyperemotes_enabled", "alert_box_scale", "goal_is_active", "goal_name", "goal_target_amount",
    "goal_activated_at", "media_upload_enabled", "media_moderation_enabled", "tts_language_boost",
    "alert_overlay_id", "leaderboard_overlay_id", "overlay_adjust_mode", "leaderboard_bg_enabled",
    "goal_overlay_id", "brand_color", "leaderboard_widget_enabled", "el_voice_id",
)
# Shown by GET but not writable until limits are approved (A1): no legacy UI ever wrote them.
READ_ONLY_SETTINGS = ("hyperemotes_min_amount", "media_min_amount")
NULLABLE_SETTINGS = frozenset({"goal_activated_at", "el_voice_id"})

# The 16 values of the legacy dashboard select (SettingsPanel.tsx); stored with this capitalisation (A2).
TTS_LANGUAGES = ("Hindi", "English", "Arabic", "Chinese", "French", "German", "Indonesian", "Italian",
                 "Japanese", "Korean", "Portuguese", "Russian", "Spanish", "Thai", "Turkish", "Vietnamese")
_LANGUAGES = {language.lower(): language for language in TTS_LANGUAGES}

ALERT_BOX_SCALES = (0.75, 1.0, 1.25, 1.5)
ALERT_OVERLAY_MAX = 16
LEADERBOARD_OVERLAY_MAX = 15  # the database trigger validate_overlay_ids (0006) is authoritative (A8)
GOAL_OVERLAY_MAX = 6
GOAL_NAME_MAX = 120
GOAL_TARGET_MAX = 100_000_000
_BRAND_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_EL_VOICE_ID = re.compile(r"[A-Za-z0-9]{10,64}")


def tts_language(value: str) -> str:
    """Case-insensitive match against the 16 languages; returns the canonical spelling."""
    try:
        return _LANGUAGES[value.lower()]
    except KeyError:
        raise ValueError(f"must be one of {', '.join(TTS_LANGUAGES)}") from None


def alert_box_scale(value: float) -> float:
    if value not in ALERT_BOX_SCALES:
        raise ValueError("must be 0.75, 1.0, 1.25 or 1.5")
    return float(value)


def brand_color(value: str) -> str:
    if not _BRAND_COLOR.fullmatch(value):
        raise ValueError("must be a hex colour like #3b82f6")
    return value


def el_voice_id(value: str | None) -> str | None:
    """An ElevenLabs voice id; "" or null clears it (stored as NULL: default voice)."""
    if value is None or value == "":
        return None
    if not _EL_VOICE_ID.fullmatch(value):
        raise ValueError("must be a 10-64 character alphanumeric voice id, or empty to clear")
    return value


def goal_name(value: str) -> str:
    value = value.strip()
    if not 1 <= len(value) <= GOAL_NAME_MAX:
        raise ValueError(f"must be 1-{GOAL_NAME_MAX} characters")
    return value


def aware_timestamp(value: object) -> datetime | None:
    """An ISO 8601 timestamp WITH a timezone offset (or null). Numbers are not timestamps here."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("must be an ISO 8601 timestamp with a timezone offset")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("must be an ISO 8601 timestamp with a timezone offset") from None
    if parsed.tzinfo is None:
        raise ValueError("must include a timezone offset, e.g. Z or +05:30")
    return parsed
