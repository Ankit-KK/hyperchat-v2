"""Pure input rules for the dashboard API (no I/O), so every rule is unit-testable."""

import base64
import binascii
import re
import uuid
from datetime import date, datetime, time, timedelta

from app.core.errors import ApiError
from app.core.timezones import IST


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
    "media_upload_enabled", "media_moderation_enabled", "tts_language_boost",
    "alert_overlay_id", "leaderboard_overlay_id", "overlay_adjust_mode", "leaderboard_bg_enabled",
    "goal_overlay_id", "brand_color", "leaderboard_widget_enabled", "el_voice_id",
)
# Shown by GET but not writable until limits are approved (A1): no legacy UI ever wrote them.
READ_ONLY_SETTINGS = ("hyperemotes_min_amount", "media_min_amount")
# Shown by GET, never accepted from clients (D-G): the server stamps each goal activation, so progress cannot be
# backdated. Sending it is an unknown field (422).
SERVER_OWNED_SETTINGS = ("goal_activated_at",)
NULLABLE_SETTINGS = frozenset({"el_voice_id"})

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


# ── moderators (A7) ─────────────────────────────────────────────────────────────
# Compatibility-preserving: the legacy dashboard and manage-moderators only trimmed the values and
# required a name plus at least one Telegram or Discord id; no id format was ever enforced (dev data
# holds non-numeric ids). The length caps only bound the input: the longest stored values are a
# 24-character name and 10 / 19-character ids. Existing rows are never re-validated.

MOD_NAME_MAX = 100
MOD_IDENTIFIER_MAX = 64


def mod_name(value: str) -> str:
    value = value.strip()
    if not 1 <= len(value) <= MOD_NAME_MAX:
        raise ValueError(f"must be 1-{MOD_NAME_MAX} characters")
    return value


def mod_identifier(value: str | None) -> str | None:
    """A Telegram or Discord id as typed (trimmed); empty means "none"."""
    value = (value or "").strip()
    if len(value) > MOD_IDENTIFIER_MAX:
        raise ValueError(f"must be at most {MOD_IDENTIFIER_MAX} characters")
    return value or None


# ── terms of service ────────────────────────────────────────────────────────────
# Owned by the server (legacy src/config/terms.ts CURRENT_TERMS_VERSION); clients cannot choose it.
TERMS_VERSION = "2026-07-03"
USER_AGENT_MAX = 500


# ── keyset pagination (newest first: created_at DESC, id DESC) ──────────────────

CURSOR_MAX_LENGTH = 200


def encode_cursor(created_at: datetime, donation_id: str) -> str:
    raw = f"{created_at.isoformat()}|{donation_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """(created_at, id) of the last item already shown. ValueError for anything that is not our cursor."""
    if not cursor or len(cursor) > CURSOR_MAX_LENGTH:
        raise ValueError("bad cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ValueError("bad cursor") from None
    stamp, sep, donation_id = raw.partition("|")
    parsed = datetime.fromisoformat(stamp) if sep else None  # ValueError when malformed
    if parsed is None or parsed.tzinfo is None or parse_uuid(donation_id) != donation_id:
        raise ValueError("bad cursor")
    return parsed, donation_id


def cursor_position(cursor: str | None) -> tuple[datetime, str] | None:
    if cursor is None:
        return None
    try:
        return decode_cursor(cursor)
    except ValueError:
        raise ApiError(400, "Invalid cursor", "bad_cursor") from None


def keyset_page(rows: list[dict], limit: int) -> tuple[list[dict], str | None, bool]:
    """`rows` was fetched with LIMIT limit + 1: the extra row only says whether there is more."""
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = encode_cursor(items[-1]["created_at"], items[-1]["donation_id"]) if has_more else None
    return items, next_cursor, has_more


# ── India Standard Time: fixed +05:30 (Asia/Kolkata has had no DST since 1945), as the legacy
#    dashboard computed it. Boundaries are computed here and passed to SQL as timestamps. ─────────

TIMEZONE_NAME = "Asia/Kolkata"
EXPORT_MAX_DAYS = 366
EXPORT_MAX_ROWS = 10_000
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def ist_midnight(day: date) -> datetime:
    return datetime.combine(day, time(), tzinfo=IST)


def ist_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """[start, end) of the IST calendar day containing `now`."""
    today = now.astimezone(IST).date()
    return ist_midnight(today), ist_midnight(today + timedelta(days=1))


def ist_month_bounds(now: datetime) -> tuple[datetime, datetime]:
    """[start, end) of the IST calendar month containing `now`."""
    first = now.astimezone(IST).date().replace(day=1)
    following = (first.replace(year=first.year + 1, month=1) if first.month == 12
                 else first.replace(month=first.month + 1))
    return ist_midnight(first), ist_midnight(following)


def parse_day(value: str | None, name: str) -> date:
    if not value:
        raise ValueError(f"{name} is required (YYYY-MM-DD)")
    if not _DAY.fullmatch(value):
        raise ValueError(f"{name} must be a date in YYYY-MM-DD format")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be a date in YYYY-MM-DD format") from None


def file_name_part(text: str) -> str:
    """Header-safe piece of a Content-Disposition file name: only [a-z0-9_-]."""
    return re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-") or "creator"


def export_days(start: str | None, end: str | None) -> tuple[date, date]:
    """Whole IST days [from, to], at most EXPORT_MAX_DAYS of them."""
    first, last = parse_day(start, "from"), parse_day(end, "to")
    if last < first:
        raise ValueError("to must not be before from")
    if (last - first).days + 1 > EXPORT_MAX_DAYS:
        raise ValueError(f"The date range is limited to {EXPORT_MAX_DAYS} days")
    return first, last
