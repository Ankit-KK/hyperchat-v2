"""Donor upload validation: the declared Content-Type must be allowed AND the bytes must really be that
format. Video is never accepted, whatever it is labelled.

  voice   audio/webm only, at most 5 MB; one Opus audio track; duration measured server-side (webm.py)
  media   JPEG, PNG, WebP or GIF, at most the creator's media_max_file_size_mb (platform cap 10 MB)
"""

import math
from dataclasses import dataclass
from decimal import Decimal

from app.core.errors import ApiError
from app.uploads.webm import VoiceInfo, WebmError, parse_voice

MB = 1024 * 1024
VOICE_MAX_BYTES = 5 * MB
MEDIA_MAX_BYTES = 10 * MB  # platform cap (legacy limit); a creator's setting can only lower it
VOICE_DURATION_TOLERANCE_S = 2.0  # recorders stop on a 1 s timer; allow that overshoot, nothing more

VIDEO_NOT_SUPPORTED = "Video uploads are not supported"


@dataclass(frozen=True)
class ImageKind:
    mime: str
    ext: str
    media_type: str  # "image" or "gif" (legacy response values)


IMAGE_KINDS = {
    "image/jpeg": ImageKind("image/jpeg", "jpg", "image"),
    "image/png": ImageKind("image/png", "png", "image"),
    "image/webp": ImageKind("image/webp", "webp", "image"),
    "image/gif": ImageKind("image/gif", "gif", "gif"),
}
_IMAGE_ALIASES = {"image/jpg": "image/jpeg"}


def unsupported(error: str, reason: str) -> ApiError:
    return ApiError(415, error, reason)


def base_type(content_type: str | None) -> str:
    """'audio/webm;codecs=opus' -> 'audio/webm' (lower case, parameters dropped)."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def _looks_like_video(data: bytes) -> bool:
    return (data[4:8] == b"ftyp"                                   # MP4 / MOV / 3GP
            or data[:4] == b"\x1a\x45\xdf\xa3"                      # Matroska / WebM
            or (data[:4] == b"RIFF" and data[8:11] == b"AVI")       # AVI
            or data[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"))  # MPEG-PS / MPEG-1 video


# ── donor media (images) ────────────────────────────────────────────────────────

def media_limit_bytes(creator_limit_mb) -> int:
    """Effective byte limit for a creator: their media_max_file_size_mb, never above the platform cap.
    NULL means the column default (10 MB); zero, negative or garbage means no media at all."""
    if creator_limit_mb is None:
        return MEDIA_MAX_BYTES
    try:
        mb = float(Decimal(str(creator_limit_mb)))
    except (ArithmeticError, ValueError):
        return 0
    if not math.isfinite(mb) or mb <= 0:
        return 0
    return min(MEDIA_MAX_BYTES, int(mb * MB))


def declared_image_kind(content_type: str | None) -> ImageKind:
    mime = base_type(content_type)
    mime = _IMAGE_ALIASES.get(mime, mime)
    if mime.startswith("video/"):
        raise unsupported(VIDEO_NOT_SUPPORTED, "video_rejected")
    kind = IMAGE_KINDS.get(mime)
    if kind is None:
        raise unsupported("Unsupported file type. Allowed: JPG, PNG, WebP, GIF", "media_type_rejected")
    return kind


def sniff_image(data: bytes) -> str | None:
    """MIME type from the file signature plus a minimal structure check, or None."""
    if len(data) >= 4 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 33 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return "image/png"
    if len(data) >= 14 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if (len(data) >= 20 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
            and data[12:16] in (b"VP8 ", b"VP8L", b"VP8X")
            and int.from_bytes(data[4:8], "little") + 8 == len(data)):  # RIFF size covers the whole file
        return "image/webp"
    return None


def validate_image(data: bytes, declared: ImageKind) -> ImageKind:
    if _looks_like_video(data):
        raise unsupported(VIDEO_NOT_SUPPORTED, "video_rejected")
    sniffed = sniff_image(data)
    if sniffed is None or sniffed != declared.mime:
        raise unsupported("File content does not match its type", "media_signature_mismatch")
    return declared


# ── donor voice (WebM / Opus) ───────────────────────────────────────────────────

def check_voice_content_type(content_type: str | None) -> None:
    mime = base_type(content_type)
    if mime.startswith("video/"):
        raise unsupported(VIDEO_NOT_SUPPORTED, "video_rejected")
    if mime != "audio/webm":
        raise unsupported("Only WebM voice messages are supported", "voice_type_rejected")


def validate_voice(data: bytes, max_seconds: float) -> VoiceInfo:
    """Parse and measure a recording (CPU-bound: call it from a worker thread)."""
    try:
        info = parse_voice(data)
    except WebmError as exc:
        if exc.args and exc.args[0] == "video_track":
            raise unsupported(VIDEO_NOT_SUPPORTED, "video_rejected") from None
        raise unsupported("Voice message must be a WebM recording with Opus audio",
                          f"voice_invalid_{exc.args[0] if exc.args else 'malformed'}") from None
    if info.duration_s > max_seconds + VOICE_DURATION_TOLERANCE_S:
        raise ApiError(422, f"Voice message is too long. Maximum is {int(max_seconds)} seconds.", "voice_too_long")
    return info
