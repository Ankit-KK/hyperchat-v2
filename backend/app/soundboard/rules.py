"""Soundboard limits and pure checks (legacy soundboard-upload-url / soundboard-confirm-upload /
swap_soundboard_slot values; Phase 6 plan)."""

import re
import uuid
from datetime import timedelta

from app.core.errors import ApiError
from app.storage.r2 import SOUNDBOARD_KEY

AUDIO_MAX_BYTES = 1_500_000
IMAGE_MAX_BYTES = 3_000_000
AUDIO_MAX_SECONDS = 7.0
AUDIO_DURATION_TOLERANCE_S = 0.1  # MP3 frames are ~26 ms; a clip trimmed to exactly 7 s may measure 7.03 s
NAME_MAX = 60
SWAPPABLE_MIN, SWAPPABLE_MAX = 1, 45
ORDER_MIN, ORDER_MAX = 0, 45
VISIBLE_MAX = 45  # enforce_soundboard_active_cap (0006)
SWAP_BUDGET = 15
SWAP_WINDOW = timedelta(days=7)
SLOT_COOLDOWN = timedelta(hours=24)
UPLOAD_RATE = ("soundboard:upload", 30, 600)  # presigned URLs per user per 10 minutes
MAX_CHANGES = 100  # entries per list in one PATCH

AUDIO_TYPES = {"audio/mpeg": "mp3", "audio/mp3": "mp3"}
IMAGE_TYPES = {"image/gif": "gif", "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp"}
STORED_TYPE = {"mp3": "audio/mpeg", "gif": "image/gif", "png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
AUDIO_EXTS = frozenset({"mp3"})
IMAGE_EXTS = frozenset({"gif", "png", "jpg", "webp"})
MAX_BYTES = {"audio": AUDIO_MAX_BYTES, "image": IMAGE_MAX_BYTES}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def display_name(value: str) -> str:
    value = value.strip()
    if not 1 <= len(value) <= NAME_MAX:
        raise ValueError(f"must be 1-{NAME_MAX} characters")
    if _CONTROL.search(value):
        raise ValueError("must not contain control characters")
    return value


def content_type_ext(kind: str, content_type: str) -> tuple[str, str]:
    """(extension, canonical content type) for a declared upload type; 400 when not allowed."""
    base = (content_type or "").split(";", 1)[0].strip().lower()
    table = AUDIO_TYPES if kind == "audio" else IMAGE_TYPES
    ext = table.get(base)
    if ext is None:
        allowed = "MP3" if kind == "audio" else "GIF, PNG, JPEG, WebP"
        raise ApiError(400, f"Unsupported {kind} type. Allowed: {allowed}", "soundboard_type_rejected")
    return ext, STORED_TYPE[ext]


def new_key(slug: str, ext: str) -> str:
    return f"soundboards/{slug}/{uuid.uuid4()}.{ext}"


def owned_key(slug: str, key: object, kind: str) -> str:
    """The key if it is a server-generated soundboard key of THIS creator for this kind; else 422.
    Rejects other creators' prefixes, platform/donor/legacy keys, traversal and any other shape."""
    m = SOUNDBOARD_KEY.match(key) if isinstance(key, str) else None
    exts = AUDIO_EXTS if kind == "audio" else IMAGE_EXTS
    if m is None or m.group("slug") != slug or m.group("ext") not in exts:
        raise ApiError(422, f"Invalid {kind} upload key", "soundboard_bad_key")
    return key


def key_ext(key: str) -> str:
    return key.rsplit(".", 1)[-1]
