"""MPEG audio Layer III byte strings for tests: valid frame headers with silent payloads (enough for
mutagen and the frame walk; only headers matter for duration)."""

MPEG1_KBPS = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
FRAME_SECONDS = 1152 / 44100  # MPEG-1 Layer III at 44.1 kHz


def frame(kbps: int = 128) -> bytes:
    """One MPEG-1 Layer III frame, 44.1 kHz, stereo, no CRC, no padding."""
    index = MPEG1_KBPS.index(kbps) + 1
    header = bytes([0xFF, 0xFB, index << 4, 0x44])
    return header + b"\x00" * (144 * kbps * 1000 // 44100 - 4)


def mp3(seconds: float, kbps: int = 128, *, id3: bool = False) -> bytes:
    frames = round(seconds / FRAME_SECONDS)
    tag = b"ID3\x04\x00\x00\x00\x00\x00\x0a" + b"\x00" * 10 if id3 else b""
    return tag + frame(kbps) * frames


def vbr_trick(seconds: float) -> bytes:
    """One 320 kbps frame, then 32 kbps frames: mutagen alone measures ~10x too short."""
    return frame(320) + frame(32) * round(seconds / FRAME_SECONDS)
