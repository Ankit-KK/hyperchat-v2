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


def synchsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def id3_frame(name: bytes, body: bytes, *, flags: int = 0, version: int = 4) -> bytes:
    """One ID3v2.3/2.4 frame (v2.4 sizes are synchsafe, v2.3 sizes plain)."""
    size = synchsafe(len(body)) if version == 4 else len(body).to_bytes(4, "big")
    return name + size + flags.to_bytes(2, "big") + body


def id3_tag(frames: bytes, *, version: int = 4, flags: int = 0, padding: int = 16) -> bytes:
    body = frames + b"\x00" * padding
    return b"ID3" + bytes([version, 0, flags]) + synchsafe(len(body)) + body
