"""Strict reader for donor voice messages: WebM (the Matroska subset browsers record), one Opus audio
track, nothing else. It measures the duration server-side from the file's own content.

mutagen has no Matroska/WebM support, so this is a small, bounded EBML walker instead of a library call.
Browser recordings (MediaRecorder) usually carry no Duration header and use "unknown size" Segment and
Cluster elements; both are handled. The duration is the LARGEST of:
  - the sum of every Opus packet's length, decoded from its TOC byte (RFC 6716 section 3.1): the amount
    of audio that will actually play, whatever the timestamps or header claim;
  - the span from the first block's start to the last block's end, by cluster/block timecodes;
  - the Duration header, if present.
Anything unexpected (other doctype, video or extra tracks, encrypted/compressed tracks, attachments,
truncation, trailing bytes, malformed lacing or Opus packets) is rejected. Work is bounded by the input
size (the caller caps it at 5 MB) and by MAX_ELEMENTS.
"""

import math
import struct
from dataclasses import dataclass

EBML = 0x1A45DFA3
DOCTYPE = 0x4282
SEGMENT = 0x18538067
SEEK_HEAD, INFO, TRACKS, CLUSTER = 0x114D9B74, 0x1549A966, 0x1654AE6B, 0x1F43B675
CUES, ATTACHMENTS, CHAPTERS, TAGS = 0x1C53BB6B, 0x1941A469, 0x1043A770, 0x1254C367
VOID, CRC32 = 0xEC, 0xBF
TIMECODE_SCALE, DURATION = 0x2AD7B1, 0x4489
TRACK_ENTRY, TRACK_NUMBER, TRACK_TYPE, CODEC_ID, CONTENT_ENCODINGS = 0xAE, 0xD7, 0x83, 0x86, 0x6D80
CLUSTER_TIMECODE, SIMPLE_BLOCK, BLOCK_GROUP, BLOCK, ENCRYPTED_BLOCK = 0xE7, 0xA3, 0xA0, 0xA1, 0xAF
LEVEL1 = frozenset({SEEK_HEAD, INFO, TRACKS, CLUSTER, CUES, ATTACHMENTS, CHAPTERS, TAGS})

TRACK_TYPE_VIDEO, TRACK_TYPE_AUDIO = 1, 2
OPUS = "A_OPUS"
MAX_ELEMENTS = 2_000_000  # element headers + laced frames; a 5 MB file never legitimately needs more
MAX_OPUS_PACKET_US = 120_000  # RFC 6716: at most 120 ms of audio per packet


class WebmError(ValueError):
    """The file is not an acceptable WebM voice recording. args[0] is a short reason code."""


@dataclass(frozen=True)
class VoiceInfo:
    duration_s: float  # the value to enforce limits on (largest of the three measures below)
    packet_duration_s: float
    timecode_duration_s: float
    header_duration_s: float | None


class _Walker:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.elements = 0
        self.packet_us = 0
        self.timecode_start_us: int | None = None
        self.timecode_end_us = 0
        self.frames = 0

    def count(self, n: int = 1) -> None:
        self.elements += n
        if self.elements > MAX_ELEMENTS:
            raise WebmError("too_many_elements")

    # ── EBML primitives ──────────────────────────────────────────────────────
    def vint(self, pos: int, end: int, *, is_id: bool) -> tuple[int, int, bool]:
        """(value, next position, unknown-size flag). IDs keep their length marker; sizes drop it."""
        if pos >= end:
            raise WebmError("truncated")
        first = self.data[pos]
        if first == 0:
            raise WebmError("bad_vint")
        length = 9 - first.bit_length()
        if length > (4 if is_id else 8):
            raise WebmError("bad_vint")
        if pos + length > end:
            raise WebmError("truncated")
        if is_id:
            return int.from_bytes(self.data[pos:pos + length], "big"), pos + length, False
        value = first & (0xFF >> length)
        for b in self.data[pos + 1:pos + length]:
            value = (value << 8) | b
        return value, pos + length, value == (1 << (7 * length)) - 1

    def header(self, pos: int, end: int) -> tuple[int, int, int | None]:
        """(element id, data start, data end or None for unknown size). Known sizes must fit in `end`."""
        self.count()
        eid, pos, _ = self.vint(pos, end, is_id=True)
        size, pos, unknown = self.vint(pos, end, is_id=False)
        if unknown:
            return eid, pos, None
        if pos + size > end:
            raise WebmError("truncated")
        return eid, pos, pos + size

    def children(self, start: int, end: int):
        pos = start
        while pos < end:
            eid, cstart, cend = self.header(pos, end)
            if cend is None:
                raise WebmError("unknown_size")
            yield eid, cstart, cend
            pos = cend

    def uint(self, start: int, end: int) -> int:
        if end - start > 8:
            raise WebmError("bad_uint")
        return int.from_bytes(self.data[start:end], "big")

    def read_float(self, start: int, end: int) -> float:
        size = end - start
        if size == 0:
            return 0.0
        if size not in (4, 8):
            raise WebmError("bad_float")
        value = struct.unpack(">f" if size == 4 else ">d", self.data[start:end])[0]
        if not math.isfinite(value) or value < 0:
            raise WebmError("bad_float")
        return value

    # ── document structure ───────────────────────────────────────────────────
    def parse(self) -> VoiceInfo:
        data, end = self.data, len(self.data)
        eid, start, hend = self.header(0, end)
        if eid != EBML or hend is None:
            raise WebmError("not_webm")
        doctype = None
        for cid, cstart, cend in self.children(start, hend):
            if cid == DOCTYPE:
                doctype = data[cstart:cend].rstrip(b"\x00")
        if doctype != b"webm":
            raise WebmError("not_webm")

        pos = hend
        while True:  # Void padding may precede the Segment
            eid, start, seg_end = self.header(pos, end)
            if eid != VOID:
                break
            if seg_end is None:
                raise WebmError("unknown_size")
            pos = seg_end
        if eid != SEGMENT:
            raise WebmError("no_segment")
        if seg_end is None:
            seg_end = end
        elif seg_end != end:
            raise WebmError("trailing_data")  # nothing may follow the Segment (no polyglots)

        timescale_ns, header_duration, track = 1_000_000, None, None
        pos, clusters = start, 0
        while pos < seg_end:
            eid, cstart, cend = self.header(pos, seg_end)
            if eid == CLUSTER:
                if track is None:
                    raise WebmError("no_tracks")
                pos = self.cluster(cstart, cend, seg_end, track, timescale_ns)
                clusters += 1
                continue
            if cend is None:
                raise WebmError("unknown_size")
            if eid in (EBML, SEGMENT, ATTACHMENTS):
                raise WebmError("unexpected_element")
            if eid in (INFO, TRACKS) and clusters:
                raise WebmError("header_after_clusters")
            if eid == INFO:
                timescale_ns, header_duration = self.info(cstart, cend)
            elif eid == TRACKS:
                if track is not None:
                    raise WebmError("multiple_tracks")
                track = self.tracks(cstart, cend)
            pos = cend  # SeekHead, Cues, Tags, Chapters, Void, CRC-32 and unknown elements are skipped

        if self.frames == 0 or self.packet_us == 0:
            raise WebmError("no_audio")
        packet_s = self.packet_us / 1_000_000
        timecode_s = (self.timecode_end_us - (self.timecode_start_us or 0)) / 1_000_000
        header_s = header_duration * timescale_ns / 1_000_000_000 if header_duration is not None else None
        return VoiceInfo(duration_s=max(packet_s, timecode_s, header_s or 0.0), packet_duration_s=packet_s,
                         timecode_duration_s=timecode_s, header_duration_s=header_s)

    def info(self, start: int, end: int) -> tuple[int, float | None]:
        timescale, duration = 1_000_000, None
        for cid, cstart, cend in self.children(start, end):
            if cid == TIMECODE_SCALE:
                timescale = self.uint(cstart, cend)
                if not 1 <= timescale <= 1_000_000_000:
                    raise WebmError("bad_timescale")
            elif cid == DURATION:
                duration = self.read_float(cstart, cend)
        return timescale, duration

    def tracks(self, start: int, end: int) -> int:
        """Track number of the single Opus audio track; anything else is rejected."""
        entries = []
        for cid, cstart, cend in self.children(start, end):
            if cid != TRACK_ENTRY:
                continue
            number = kind = codec = None
            for tid, tstart, tend in self.children(cstart, cend):
                if tid == TRACK_NUMBER:
                    number = self.uint(tstart, tend)
                elif tid == TRACK_TYPE:
                    kind = self.uint(tstart, tend)
                elif tid == CODEC_ID:
                    codec = self.data[tstart:tend].rstrip(b"\x00").decode("ascii", "replace")
                elif tid == CONTENT_ENCODINGS:
                    raise WebmError("encoded_track")  # compressed/encrypted frames: TOC bytes unreadable
            entries.append((number, kind, codec))
        if any(kind == TRACK_TYPE_VIDEO for _, kind, _ in entries):
            raise WebmError("video_track")
        if len(entries) != 1:
            raise WebmError("multiple_tracks" if entries else "no_tracks")
        number, kind, codec = entries[0]
        if kind != TRACK_TYPE_AUDIO or not number:
            raise WebmError("not_audio")
        if codec != OPUS:
            raise WebmError("not_opus")
        return number

    def cluster(self, start: int, cend: int | None, seg_end: int, track: int, timescale_ns: int) -> int:
        """Walk one Cluster; returns where the next Segment child starts."""
        unknown = cend is None
        end = seg_end if unknown else cend
        pos, cluster_tc = start, None
        while pos < end:
            eid, bstart, bend = self.header(pos, end)
            if unknown and eid in LEVEL1:
                return pos  # an unknown-size Cluster ends where the next top-level element begins
            if bend is None:
                raise WebmError("unknown_size")
            if eid == CLUSTER_TIMECODE:
                cluster_tc = self.uint(bstart, bend)
            elif eid in (SIMPLE_BLOCK, BLOCK_GROUP):
                if cluster_tc is None:
                    raise WebmError("block_before_timecode")
                if eid == SIMPLE_BLOCK:
                    self.block(bstart, bend, track, cluster_tc, timescale_ns)
                else:
                    for gid, gstart, gend in self.children(bstart, bend):
                        if gid == BLOCK:
                            self.block(gstart, gend, track, cluster_tc, timescale_ns)
            elif eid == ENCRYPTED_BLOCK:
                raise WebmError("encoded_track")
            pos = bend
        return end

    def block(self, start: int, end: int, track: int, cluster_tc: int, timescale_ns: int) -> None:
        number, pos, _ = self.vint(start, end, is_id=False)
        if number != track:
            raise WebmError("unknown_track")
        if pos + 3 > end:
            raise WebmError("truncated")
        relative = int.from_bytes(self.data[pos:pos + 2], "big", signed=True)
        lacing = (self.data[pos + 2] >> 1) & 0x03
        block_us = 0
        for fstart, fend in self.frames_of(pos + 3, end, lacing):
            block_us += self.opus_packet_us(fstart, fend)
        self.packet_us += block_us
        start_us = max(0, cluster_tc + relative) * timescale_ns // 1000
        if self.timecode_start_us is None or start_us < self.timecode_start_us:
            self.timecode_start_us = start_us
        self.timecode_end_us = max(self.timecode_end_us, start_us + block_us)

    def frames_of(self, pos: int, end: int, lacing: int) -> list[tuple[int, int]]:
        if lacing == 0:
            self.count()
            self.frames += 1
            return [(pos, end)]
        if pos >= end:
            raise WebmError("truncated")
        count = self.data[pos] + 1
        pos += 1
        self.count(count)
        sizes: list[int] = []
        if lacing == 1:  # Xiph: each size is a run of 255s plus a final byte
            for _ in range(count - 1):
                size = 0
                while True:
                    if pos >= end:
                        raise WebmError("truncated")
                    byte = self.data[pos]
                    pos += 1
                    size += byte
                    if byte != 255:
                        break
                sizes.append(size)
        elif lacing == 3:  # EBML: first size, then signed differences
            if count > 1:
                first, pos, _ = self.vint(pos, end, is_id=False)
                sizes.append(first)
                for _ in range(count - 2):
                    raw, nxt, _ = self.vint(pos, end, is_id=False)
                    sizes.append(sizes[-1] + raw - ((1 << (7 * (nxt - pos) - 1)) - 1))
                    pos = nxt
        else:  # fixed-size lacing
            if (end - pos) % count:
                raise WebmError("bad_lacing")
            sizes = [(end - pos) // count] * (count - 1)
        last = end - pos - sum(sizes)
        if any(s <= 0 for s in sizes) or last <= 0:
            raise WebmError("bad_lacing")
        sizes.append(last)
        frames, cursor = [], pos
        for size in sizes:
            frames.append((cursor, cursor + size))
            cursor += size
        self.frames += count
        return frames

    def opus_packet_us(self, start: int, end: int) -> int:
        """Audio length of one Opus packet from its TOC byte (RFC 6716 section 3.1)."""
        if end <= start:
            raise WebmError("bad_opus_packet")
        toc = self.data[start]
        config, code = toc >> 3, toc & 0x03
        if config < 12:  # SILK-only
            frame_us = (10_000, 20_000, 40_000, 60_000)[config & 3]
        elif config < 16:  # hybrid
            frame_us = (10_000, 20_000)[config & 1]
        else:  # CELT-only
            frame_us = (2_500, 5_000, 10_000, 20_000)[config & 3]
        if code == 0:
            count = 1
        elif code in (1, 2):
            count = 2
        else:
            if end - start < 2:
                raise WebmError("bad_opus_packet")
            count = self.data[start + 1] & 0x3F
            if count == 0:
                raise WebmError("bad_opus_packet")
        total = count * frame_us
        if total > MAX_OPUS_PACKET_US:
            raise WebmError("bad_opus_packet")
        return total


def parse_voice(data: bytes) -> VoiceInfo:
    """Validate a WebM/Opus voice recording and measure it. Raises WebmError with a reason code."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 16:
        raise WebmError("not_webm")
    try:
        return _Walker(bytes(data)).parse()
    except (IndexError, ValueError, struct.error, OverflowError) as exc:
        if isinstance(exc, WebmError):
            raise
        raise WebmError("malformed") from None
