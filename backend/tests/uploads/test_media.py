"""Donor media rules: declared type allowed AND bytes really that format; size limits; video rejected."""

from decimal import Decimal

import pytest

from app.core.errors import ApiError
from app.uploads import media
from app.uploads.media import MB
from tests.uploads import webm_builder as b

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"\x00" * 13 + b"\x00" * 16
GIF = b"GIF89a" + b"\x01\x00\x01\x00\x80\x00\x00" + b"\x00" * 16 + b";"
_WEBP_BODY = b"WEBPVP8 " + b"\x00" * 24
WEBP = b"RIFF" + len(_WEBP_BODY).to_bytes(4, "little") + _WEBP_BODY
MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 64
SAMPLES = {"image/jpeg": JPEG, "image/png": PNG, "image/gif": GIF, "image/webp": WEBP}


def rejected(fn, *args) -> ApiError:
    with pytest.raises(ApiError) as exc:
        fn(*args)
    return exc.value


@pytest.mark.parametrize("mime, ext, media_type", [("image/jpeg", "jpg", "image"), ("image/png", "png", "image"),
                                                   ("image/webp", "webp", "image"), ("image/gif", "gif", "gif")])
def test_each_allowed_image_type(mime, ext, media_type):
    kind = media.declared_image_kind(mime.upper() + "; charset=binary")
    assert (kind.mime, kind.ext, kind.media_type) == (mime, ext, media_type)
    assert media.sniff_image(SAMPLES[mime]) == mime
    assert media.validate_image(SAMPLES[mime], kind) is kind


def test_jpg_alias_accepted():
    assert media.declared_image_kind("image/jpg").mime == "image/jpeg"


@pytest.mark.parametrize("declared", ["image/svg+xml", "image/bmp", "image/heic", "text/html", "application/octet-stream",
                                      "", None, "audio/webm"])
def test_other_declared_types_rejected(declared):
    err = rejected(media.declared_image_kind, declared)
    assert err.status == 415 and err.error == "Unsupported file type. Allowed: JPG, PNG, WebP, GIF"


@pytest.mark.parametrize("declared", ["video/mp4", "video/webm", "VIDEO/QuickTime"])
def test_declared_video_rejected(declared):
    err = rejected(media.declared_image_kind, declared)
    assert (err.status, err.error) == (415, "Video uploads are not supported")


@pytest.mark.parametrize("declared, data", [
    ("image/png", JPEG), ("image/jpeg", PNG), ("image/gif", WEBP), ("image/webp", GIF),
    ("image/jpeg", b"<svg xmlns='http://www.w3.org/2000/svg'/>"), ("image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40),
    ("image/webp", WEBP + b"trailing"), ("image/gif", b"GIF89a"), ("image/jpeg", b""),
])
def test_signature_must_match_declared_type(declared, data):
    err = rejected(media.validate_image, data, media.declared_image_kind(declared))
    assert (err.status, err.error) == (415, "File content does not match its type")


@pytest.mark.parametrize("data", [MP4, b.recording(1), b"RIFF\x00\x00\x00\x00AVI LIST" + b"\x00" * 32,
                                  b"\x00\x00\x01\xba" + b"\x00" * 32])
def test_video_bytes_rejected_whatever_the_label(data):
    err = rejected(media.validate_image, data, media.declared_image_kind("image/jpeg"))
    assert (err.status, err.error) == (415, "Video uploads are not supported")


@pytest.mark.parametrize("value, expected", [
    (None, 10 * MB), (10, 10 * MB), (Decimal("10"), 10 * MB), (5, 5 * MB), (Decimal("2.5"), int(2.5 * MB)),
    (25, 10 * MB), (Decimal("1000"), 10 * MB),  # never above the platform cap
    (0, 0), (-1, 0), ("abc", 0), (float("nan"), 0), (float("inf"), 0),
])
def test_media_limit_honours_creator_setting_under_the_cap(value, expected):
    assert media.media_limit_bytes(value) == expected


@pytest.mark.parametrize("ct", ["audio/webm", "audio/webm;codecs=opus", "AUDIO/WEBM; codecs=\"opus\""])
def test_voice_content_type_webm_only(ct):
    media.check_voice_content_type(ct)


@pytest.mark.parametrize("ct, error", [
    ("video/webm", "Video uploads are not supported"), ("audio/ogg;codecs=opus", "Only WebM voice messages are supported"),
    ("audio/mp4", "Only WebM voice messages are supported"), ("audio/mpeg", "Only WebM voice messages are supported"),
    (None, "Only WebM voice messages are supported"), ("", "Only WebM voice messages are supported"),
])
def test_other_voice_types_rejected(ct, error):
    err = rejected(media.check_voice_content_type, ct)
    assert (err.status, err.error) == (415, error)


def test_voice_duration_enforced_server_side_with_tolerance():
    assert media.validate_voice(b.recording(15), 15).duration_s == pytest.approx(15)
    assert media.validate_voice(b.recording(16.9), 15).duration_s == pytest.approx(16.9)  # within 2 s tolerance
    err = rejected(media.validate_voice, b.recording(17.2), 15)
    assert (err.status, err.error) == (422, "Voice message is too long. Maximum is 15 seconds.")


def test_voice_validation_maps_parser_errors():
    assert rejected(media.validate_voice, b.recording(1, track=(1, 1, "V_VP8")), 15).error \
        == "Video uploads are not supported"
    for bad in (JPEG, b"\x1a\x45\xdf\xa3" + b"\x00" * 30, b.recording(2)[:-9]):
        err = rejected(media.validate_voice, bad, 15)
        assert (err.status, err.error) == (415, "Voice message must be a WebM recording with Opus audio")
