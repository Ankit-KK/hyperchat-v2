"""MP3 validation for soundboard sounds, with the duration measured server-side.

mutagen validates the stream (MPEG audio Layer III) and reports a length, but that length comes from the
first frame's bitrate or a Xing/VBRI header, both of which the uploader controls: a file of one 320 kbps
frame followed by thousands of 32 kbps frames measures ~10x too short. So the frames are also walked one by
one (header -> frame length -> next header, per ISO/IEC 11172-3 and 13818-3) and the duration enforced
is the LARGER of the two. Bounded by the 1.5 MB size cap; run it in a worker thread.
"""

import io
from dataclasses import dataclass

from mutagen import MutagenError
from mutagen.mp3 import MP3

_BITRATES_KBPS = {
    "mpeg1": (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    "mpeg2": (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}  # version bits
MAX_SKIPPED_BYTES = 64 * 1024  # non-frame bytes tolerated in total (padding, junk between frames)


class Mp3Error(ValueError):
    """Not an acceptable MP3. args[0] is a short reason code."""


@dataclass(frozen=True)
class Mp3Info:
    duration_s: float  # the value to enforce limits on
    frames: int
    walked_duration_s: float
    mutagen_duration_s: float


def _frame(data: bytes, pos: int) -> tuple[int, float] | None:
    """(frame length in bytes, seconds of audio) of an MPEG Layer III frame header at pos, or None."""
    if pos + 4 > len(data) or data[pos] != 0xFF:
        return None
    b1, b2 = data[pos + 1], data[pos + 2]
    if b1 & 0xE0 != 0xE0:
        return None
    version, layer = (b1 >> 3) & 0x03, (b1 >> 1) & 0x03
    if version == 1 or layer != 0x01:  # reserved version; only Layer III
        return None
    bitrate_index, rate_index, padding = b2 >> 4, (b2 >> 2) & 0x03, (b2 >> 1) & 0x01
    if bitrate_index in (0, 15) or rate_index == 3:  # free format / bad / reserved
        return None
    rate = _SAMPLE_RATES[version][rate_index]
    if version == 3:
        kbps, coefficient, samples = _BITRATES_KBPS["mpeg1"][bitrate_index - 1], 144, 1152
    else:
        kbps, coefficient, samples = _BITRATES_KBPS["mpeg2"][bitrate_index - 1], 72, 576
    return coefficient * kbps * 1000 // rate + padding, samples / rate


def _trailer(data: bytes, pos: int) -> bool:
    rest = len(data) - pos
    return ((rest == 128 and data[pos:pos + 3] == b"TAG") or data[pos:pos + 8] == b"APETAGEX"
            or data[pos:pos + 11] == b"LYRICSBEGIN")


def _audio_start(data: bytes) -> int:
    if data[:3] != b"ID3":
        return 0
    if len(data) < 10 or any(b & 0x80 for b in data[6:10]):
        raise Mp3Error("bad_id3")
    size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
    start = 10 + size + (10 if data[5] & 0x10 else 0)
    if start >= len(data):
        raise Mp3Error("no_audio")
    return start


def _walk(data: bytes, start: int) -> tuple[int, float]:
    """Every valid frame header the walk lands on counts, like a decoder that resyncs after junk; a frame
    is never skipped because of what follows it, so inserted junk cannot hide audio. Errs towards counting
    more (and rejecting), never less."""
    pos, frames, seconds, skipped, n = start, 0, 0.0, 0, len(data)
    while pos + 4 <= n:
        frame = _frame(data, pos)
        if frame is not None:
            length, duration = frame
            frames += 1  # a truncated last frame still plays: count it
            seconds += duration
            pos += length
            continue
        if _trailer(data, pos):
            break
        pos += 1
        skipped += 1
        if skipped > MAX_SKIPPED_BYTES:
            raise Mp3Error("malformed")
    return frames, seconds


def parse_mp3(data: bytes) -> Mp3Info:
    if not isinstance(data, (bytes, bytearray)) or len(data) < 4:
        raise Mp3Error("not_mp3")
    data = bytes(data)
    start = _audio_start(data)
    frames, walked = _walk(data, start)
    if frames == 0:
        raise Mp3Error("no_audio")
    try:
        info = MP3(io.BytesIO(data)).info
    except (MutagenError, ValueError, IndexError) as exc:
        raise Mp3Error("not_mp3") from None
    if getattr(info, "layer", 3) != 3 or not info.length or info.length <= 0:
        raise Mp3Error("not_mp3")
    return Mp3Info(duration_s=max(walked, float(info.length)), frames=frames, walked_duration_s=walked,
                   mutagen_duration_s=float(info.length))
