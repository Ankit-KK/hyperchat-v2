"""Security audit 2026-11-26 NV-5: ID3 tags that make mutagen inflate or rewrite data are refused before it runs."""

import time
import zlib

import pytest

from app.soundboard.audio import Mp3Error, parse_mp3
from tests.soundboard import mp3_builder as m

AUDIO = m.mp3(2.0)
TITLE = b"\x03Air horn"  # UTF-8 text frame body


@pytest.mark.parametrize("version", [3, 4])
def test_plain_tags_still_parse(version):
    tag = m.id3_tag(m.id3_frame(b"TIT2", TITLE, version=version) + m.id3_frame(b"TPE1", TITLE, version=version),
                    version=version)
    assert parse_mp3(tag + AUDIO).duration_s == pytest.approx(2.0, abs=0.03)


@pytest.mark.parametrize("version,frame_flags", [
    (3, 0x0080),           # v2.3 compressed
    (4, 0x0008 | 0x0001),  # v2.4 compressed (+ data length indicator)
    (4, 0x0002),           # v2.4 unsynchronised frame
])
def test_compressed_or_unsynchronised_frames_are_refused(version, frame_flags):
    bad = m.id3_frame(b"TIT2", TITLE, flags=frame_flags, version=version)
    tag = m.id3_tag(m.id3_frame(b"TPE1", TITLE, version=version) + bad, version=version)
    with pytest.raises(Mp3Error) as exc:
        parse_mp3(tag + AUDIO)
    assert exc.value.args[0] == "bad_id3"


@pytest.mark.parametrize("version,flags", [(4, 0x80), (3, 0x80), (4, 0x40), (3, 0x40), (2, 0x40), (2, 0x80)])
def test_unsynchronised_extended_or_compressed_tags_are_refused(version, flags):
    with pytest.raises(Mp3Error) as exc:
        parse_mp3(m.id3_tag(b"", version=version, flags=flags) + AUDIO)
    assert exc.value.args[0] == "bad_id3"


def test_frame_declaring_more_than_the_tag_is_refused():
    frame = m.id3_frame(b"TIT2", TITLE)
    lying = frame[:4] + m.synchsafe(4096) + frame[8:]  # declares 4 KiB inside a ~40 byte tag
    with pytest.raises(Mp3Error) as exc:
        parse_mp3(m.id3_tag(lying, padding=0) + AUDIO)
    assert exc.value.args[0] == "bad_id3"


def test_zlib_bomb_frame_is_refused_without_inflating():
    inflated = 64 * 1024 * 1024
    body = inflated.to_bytes(4, "big") + zlib.compress(b"\x00" * inflated, 9)  # ~64 KB on disk, 64 MiB inflated
    tag = m.id3_tag(m.id3_frame(b"PRIV", body, flags=0x0008 | 0x0001))
    t0 = time.monotonic()
    with pytest.raises(Mp3Error):
        parse_mp3(tag + AUDIO)
    assert time.monotonic() - t0 < 1.0


def test_compressed_sub_frame_inside_a_chapter_never_reaches_mutagen(monkeypatch):
    """CHAP nests sub-frames that mutagen parses (and inflates) with their own flags; the top-level walk does
    not look inside, so mutagen must never be handed the tag at all."""
    inflated = 64 * 1024 * 1024
    bomb = m.id3_frame(b"PRIV", inflated.to_bytes(4, "big") + zlib.compress(b"\x00" * inflated, 9),
                       flags=0x0008 | 0x0001)
    chap = m.id3_frame(b"CHAP", b"ch1\x00" + b"\x00" * 16 + bomb)  # element id, 4 x 32-bit times/offsets
    seen = []
    import app.soundboard.audio as audio
    real = audio.MP3
    monkeypatch.setattr(audio, "MP3", lambda f: seen.append(f.getvalue()[:3]) or real(f))
    t0 = time.monotonic()
    info = parse_mp3(m.id3_tag(chap) + AUDIO)
    assert time.monotonic() - t0 < 1.0 and info.duration_s == pytest.approx(2.0, abs=0.03)
    assert seen == [AUDIO[:3]]  # mutagen got the audio only


def test_a_second_chained_tag_is_refused():
    with pytest.raises(Mp3Error) as exc:
        parse_mp3(m.id3_tag(b"") + m.id3_tag(m.id3_frame(b"TIT2", TITLE)) + AUDIO)
    assert exc.value.args[0] == "bad_id3"
