"""Builds WebM (EBML) byte strings for tests: browser-style recordings (unknown sizes, no Duration),
muxer-style files (known sizes, Duration header), and deliberately broken variants.

Opus packets are TOC bytes plus filler; only the TOC matters for duration (RFC 6716 section 3.1).
"""

import struct

EBML, DOCTYPE, SEGMENT, INFO, TRACKS, CLUSTER = 0x1A45DFA3, 0x4282, 0x18538067, 0x1549A966, 0x1654AE6B, 0x1F43B675
TIMECODE_SCALE, DURATION, TRACK_ENTRY, TRACK_NUMBER, TRACK_TYPE, CODEC_ID = 0x2AD7B1, 0x4489, 0xAE, 0xD7, 0x83, 0x86
CONTENT_ENCODINGS, CLUSTER_TIMECODE, SIMPLE_BLOCK, BLOCK_GROUP, BLOCK, VOID = 0x6D80, 0xE7, 0xA3, 0xA0, 0xA1, 0xEC
ATTACHMENTS, CUES = 0x1941A469, 0x1C53BB6B
UNKNOWN = b"\x01\xff\xff\xff\xff\xff\xff\xff"  # 8-byte "unknown size"

# Opus TOC config per frame length (CELT fullband for <= 20 ms, SILK narrowband for 40/60 ms)
_CONFIG = {2.5: 28, 5: 29, 10: 30, 20: 31, 40: 2, 60: 3}


def size(n: int) -> bytes:
    for length in range(1, 9):
        if n < (1 << (7 * length)) - 1:
            return ((1 << (7 * length)) | n).to_bytes(length, "big")
    raise ValueError(n)


def element(eid: int, payload: bytes = b"", *, unknown: bool = False) -> bytes:
    eid_bytes = eid.to_bytes((eid.bit_length() + 7) // 8, "big")
    return eid_bytes + (UNKNOWN if unknown else size(len(payload))) + payload


def uint(v: int) -> bytes:
    return v.to_bytes(max(1, (v.bit_length() + 7) // 8), "big")


def opus(ms: float = 20, *, code: int = 0, count: int | None = None, filler: int = 3) -> bytes:
    toc = (_CONFIG[ms] << 3) | code
    if code == 3:
        return bytes([toc, count if count is not None else 1]) + b"\x55" * filler
    return bytes([toc]) + b"\x55" * filler


def block_payload(packets: list[bytes], *, track: int = 1, rel: int = 0, lacing: str | None = None) -> bytes:
    head = size(track) + struct.pack(">h", rel)
    if lacing is None:
        assert len(packets) == 1
        return head + b"\x80" + packets[0]
    flags = {"xiph": 0x82, "fixed": 0x84, "ebml": 0x86}[lacing]
    out = head + bytes([flags, len(packets) - 1])
    if lacing == "xiph":
        for p in packets[:-1]:
            n = len(p)
            out += b"\xff" * (n // 255) + bytes([n % 255])
    elif lacing == "ebml" and len(packets) > 1:
        out += size(len(packets[0]))
        for prev, cur in zip(packets[:-2], packets[1:-1]):
            raw = len(cur) - len(prev) + (1 << 13) - 1  # 2-byte signed vint: bias 2^13 - 1
            out += ((1 << 14) | raw).to_bytes(2, "big")
    elif lacing == "fixed":
        assert len({len(p) for p in packets}) == 1
    return out + b"".join(packets)


def ebml_header(doctype: str = "webm") -> bytes:
    return element(EBML, element(0x4286, uint(1)) + element(DOCTYPE, doctype.encode()))


def tracks(entries=((1, 2, "A_OPUS"),), *, encoded: bool = False) -> bytes:
    body = b""
    for number, kind, codec in entries:
        entry = element(TRACK_NUMBER, uint(number)) + element(TRACK_TYPE, uint(kind)) + element(CODEC_ID, codec.encode())
        if encoded:
            entry += element(CONTENT_ENCODINGS, element(0x6240, b""))
        body += element(TRACK_ENTRY, entry)
    return element(TRACKS, body)


def info(*, timescale: int | None = None, duration: float | None = None) -> bytes:
    body = b""
    if timescale is not None:
        body += element(TIMECODE_SCALE, uint(timescale))
    if duration is not None:
        body += element(DURATION, struct.pack(">d", duration))
    return element(INFO, body)


def cluster(timecode_ms: int, blocks: list[bytes], *, unknown: bool = False, group: bool = False) -> bytes:
    body = element(CLUSTER_TIMECODE, uint(timecode_ms))
    for b in blocks:
        body += element(BLOCK_GROUP, element(BLOCK, b)) if group else element(SIMPLE_BLOCK, b)
    return element(CLUSTER, body, unknown=unknown)


def document(segment_children: bytes, *, unknown_segment: bool = False, doctype: str = "webm") -> bytes:
    return ebml_header(doctype) + element(SEGMENT, segment_children, unknown=unknown_segment)


def recording(seconds: float, *, packet_ms: float = 20, browser: bool = True, header_duration: float | None = None,
              timescale: int | None = None, track: tuple = (1, 2, "A_OPUS")) -> bytes:
    """`seconds` of audio in 1-second clusters. browser=True: unknown-size Segment and Clusters, no Duration
    (what MediaRecorder writes); browser=False: known sizes and a Duration header (muxer style)."""
    per_cluster = int(1000 / packet_ms)
    total = round(seconds * 1000 / packet_ms)
    clusters, sent, tc = b"", 0, 0
    while sent < total:
        n = min(per_cluster, total - sent)
        blocks = [block_payload([opus(packet_ms)], track=track[0], rel=round(i * packet_ms)) for i in range(n)]
        clusters += cluster(tc, blocks, unknown=browser)
        sent += n
        tc += 1000
    head = info(timescale=timescale, duration=None if browser else (header_duration or seconds * 1000))
    if browser and header_duration is not None:
        head = info(timescale=timescale, duration=header_duration)
    return document(head + tracks((track,)) + clusters, unknown_segment=browser)
