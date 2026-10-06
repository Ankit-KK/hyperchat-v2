"""WebM/Opus voice parsing: duration measured from the file's own content, strict structure.

fixtures/chromium152_*.webm are real MediaRecorder outputs (Chromium 152, a synthetic 440 Hz tone and
coloured canvas frames; no microphone, no personal data)."""

import struct
from pathlib import Path

import pytest

from app.uploads import webm
from app.uploads.webm import WebmError, parse_voice
from tests.uploads import webm_builder as b

FIXTURES = Path(__file__).parent / "fixtures"


def test_real_chromium_mediarecorder_recording():
    data = (FIXTURES / "chromium152_mediarecorder_opus_1440ms.webm").read_bytes()
    assert b"\x18\x53\x80\x67\x01\xff\xff\xff\xff\xff\xff\xff" in data  # unknown-size Segment, as browsers write
    info = parse_voice(data)
    assert info.header_duration_s is None
    assert info.duration_s == pytest.approx(1.44, abs=0.01)
    assert info.packet_duration_s == pytest.approx(info.timecode_duration_s, abs=0.005)


@pytest.mark.parametrize("name", ["chromium152_mediarecorder_vp8_video.webm",
                                  "chromium152_mediarecorder_vp8_opus_av.webm"])
def test_real_chromium_video_recordings_rejected(name):
    assert reason((FIXTURES / name).read_bytes()) == "video_track"


def reason(data: bytes) -> str:
    with pytest.raises(WebmError) as exc:
        parse_voice(data)
    return exc.value.args[0]


# ── valid recordings ────────────────────────────────────────────────────────────

def test_browser_style_recording_unknown_sizes_no_duration_header():
    info = parse_voice(b.recording(8, browser=True))
    assert info.header_duration_s is None
    assert info.packet_duration_s == pytest.approx(8.0)
    assert info.timecode_duration_s == pytest.approx(8.0)
    assert info.duration_s == pytest.approx(8.0)


def test_muxer_style_recording_known_sizes_with_duration_header():
    info = parse_voice(b.recording(12.5, browser=False))
    assert info.header_duration_s == pytest.approx(12.5)
    assert info.duration_s == pytest.approx(12.5)


@pytest.mark.parametrize("packet_ms", [2.5, 5, 10, 20, 40, 60])
def test_every_opus_frame_size(packet_ms):
    assert parse_voice(b.recording(3, packet_ms=packet_ms)).packet_duration_s == pytest.approx(3.0)


def test_custom_timecode_scale():
    # 1 timecode unit = 10 ms: block offsets of 2 units are 20 ms apart
    blocks = [b.block_payload([b.opus(20)], rel=2 * i) for i in range(50)]
    data = b.document(b.info(timescale=10_000_000, duration=100.0) + b.tracks() + b.cluster(0, blocks))
    info = parse_voice(data)
    assert info.timecode_duration_s == pytest.approx(1.0) and info.header_duration_s == pytest.approx(1.0)


@pytest.mark.parametrize("lacing", ["xiph", "fixed", "ebml"])
def test_laced_blocks_count_every_frame(lacing):
    packets = [b.opus(20, filler=f) for f in ((40, 40, 40, 40) if lacing == "fixed" else (300, 3, 60, 7))]
    data = b.document(b.info() + b.tracks() + b.cluster(0, [b.block_payload(packets, lacing=lacing)] * 10))
    assert parse_voice(data).packet_duration_s == pytest.approx(10 * 4 * 0.020)


def test_block_groups_and_multi_frame_packets():
    code1 = b.opus(20, code=1)                   # two 20 ms frames
    code3 = b.opus(10, code=3, count=6)          # six 10 ms frames
    blocks = [b.block_payload([code1], rel=0), b.block_payload([code3], rel=40)]
    data = b.document(b.info() + b.tracks() + b.cluster(0, blocks, group=True))
    assert parse_voice(data).packet_duration_s == pytest.approx(0.040 + 0.060)


def test_void_elements_and_cues_are_skipped():
    data = b.document(b.element(b.VOID, b"\x00" * 8) + b.info() + b.tracks()
                      + b.cluster(0, [b.block_payload([b.opus()])]) + b.element(b.CUES, b"\x00" * 4))
    assert parse_voice(data).packet_duration_s == pytest.approx(0.020)


# ── the duration cannot be understated ──────────────────────────────────────────

def test_lying_timecodes_cannot_hide_audio():
    # 30 s of packets, but every block claims timecode 0 and the header says 1 s
    blocks = [b.block_payload([b.opus(20)], rel=0) for _ in range(1500)]
    data = b.document(b.info(duration=1000.0) + b.tracks() + b.cluster(0, blocks))
    info = parse_voice(data)
    assert info.timecode_duration_s < 0.1 and info.header_duration_s == pytest.approx(1.0)
    assert info.duration_s == pytest.approx(30.0)


def test_gaps_in_timecodes_count_towards_duration():
    data = b.document(b.info() + b.tracks() + b.cluster(0, [b.block_payload([b.opus()])])
                      + b.cluster(45_000, [b.block_payload([b.opus()])]))
    assert parse_voice(data).duration_s == pytest.approx(45.02)


def test_larger_duration_header_is_respected():
    data = b.recording(2, browser=False, header_duration=90_000.0)
    assert parse_voice(data).duration_s == pytest.approx(90.0)


def test_recording_starting_at_an_offset_is_measured_as_a_span():
    blocks = [b.block_payload([b.opus()], rel=i * 20) for i in range(100)]
    data = b.document(b.info() + b.tracks() + b.cluster(5_000, blocks))
    assert parse_voice(data).duration_s == pytest.approx(2.0)


# ── rejected content ────────────────────────────────────────────────────────────

def test_video_track_rejected():
    assert reason(b.recording(1, track=(1, 1, "V_VP8"))) == "video_track"
    both = b.document(b.info() + b.tracks(((1, 2, "A_OPUS"), (2, 1, "V_VP9"))) + b.cluster(0, []))
    assert reason(both) == "video_track"


@pytest.mark.parametrize("entries, why", [
    (((1, 2, "A_VORBIS"),), "not_opus"),
    (((1, 2, "A_OPUS"), (2, 2, "A_OPUS")), "multiple_tracks"),
    (((1, 17, "S_TEXT/UTF8"),), "not_audio"),
    ((), "no_tracks"),
])
def test_track_rules(entries, why):
    assert reason(b.document(b.info() + b.tracks(entries) + b.cluster(0, [b.block_payload([b.opus()])]))) == why


def test_matroska_doctype_and_non_webm_files_rejected():
    assert reason(b.recording(1).replace(b"webm", b"mkvx", 1)) == "not_webm"
    assert reason(b.document(b.info() + b.tracks(), doctype="matroska")) == "not_webm"
    for junk in (b"\xff\xd8\xff\xe0" + b"\x00" * 64, b"OggS" + b"\x00" * 64, b"\x00" * 64, b"RIFF....WAVEfmt " * 4):
        assert reason(junk) in ("not_webm", "bad_vint", "truncated", "malformed")
    assert reason(b"") == "not_webm"


def test_encoded_tracks_and_attachments_rejected():
    assert reason(b.document(b.info() + b.tracks(encoded=True) + b.cluster(0, []))) == "encoded_track"
    data = b.document(b.info() + b.tracks() + b.element(b.ATTACHMENTS, b"\x00" * 16)
                      + b.cluster(0, [b.block_payload([b.opus()])]))
    assert reason(data) == "unexpected_element"


def test_structure_rules():
    block = b.block_payload([b.opus()])
    assert reason(b.document(b.cluster(0, [block]) + b.tracks())) == "no_tracks"            # cluster before tracks
    assert reason(b.document(b.info() + b.tracks() + b.cluster(0, [block]) + b.info())) == "header_after_clusters"
    assert reason(b.document(b.info() + b.tracks() + b.cluster(0, [b.block_payload([b.opus()], track=2)]))) \
        == "unknown_track"
    no_tc = b.element(b.CLUSTER, b.element(b.SIMPLE_BLOCK, block))
    assert reason(b.document(b.info() + b.tracks() + no_tc)) == "block_before_timecode"
    assert reason(b.document(b.info() + b.tracks())) == "no_audio"
    assert reason(b.ebml_header() + b.element(b.SEGMENT, b.element(b.INFO, b"", unknown=True))) == "unknown_size"


def test_trailing_bytes_and_truncation_rejected():
    good = b.recording(2, browser=False)
    assert parse_voice(good).duration_s == pytest.approx(2.0)
    assert reason(good + b"\x00" * 10) == "trailing_data"          # nothing may follow a known-size Segment
    assert reason(good[:-7]) == "truncated"
    assert reason(good[:40]) == "truncated"


@pytest.mark.parametrize("packet", [
    b"",                                    # empty frame
    bytes([(31 << 3) | 3]),                 # code 3 without a frame-count byte
    bytes([(31 << 3) | 3, 0]),              # code 3 with zero frames
    bytes([(3 << 3) | 3, 3]) + b"\x55",     # 3 x 60 ms = 180 ms > 120 ms per packet
])
def test_invalid_opus_packets_rejected(packet):
    data = b.document(b.info() + b.tracks() + b.cluster(0, [b.block_payload([b.opus()]), b.block_payload([packet])]))
    assert reason(data) == "bad_opus_packet"


def test_bad_lacing_rejected():
    payload = b.size(1) + struct.pack(">h", 0) + bytes([0x82, 2]) + b"\xff\xff\xff\xff"  # Xiph sizes overrun
    assert reason(b.document(b.info() + b.tracks() + b.cluster(0, [payload]))) in ("truncated", "bad_lacing")
    fixed = b.size(1) + struct.pack(">h", 0) + bytes([0x84, 2]) + b"\xf8" * 4                # 4 bytes / 3 frames
    assert reason(b.document(b.info() + b.tracks() + b.cluster(0, [fixed]))) == "bad_lacing"


def test_bad_numbers_rejected():
    assert reason(b.document(b.info(timescale=0) + b.tracks())) == "bad_timescale"
    nan = b.element(b.INFO, b.element(b.DURATION, struct.pack(">d", float("nan"))))
    assert reason(b.document(nan + b.tracks())) == "bad_float"
    assert reason(b.ebml_header() + b"\x18\x53\x80\x67\x00") == "bad_vint"  # size byte 0x00 is not a vint


def test_element_budget_bounds_the_work(monkeypatch):
    monkeypatch.setattr(webm, "MAX_ELEMENTS", 50)
    assert reason(b.recording(5)) == "too_many_elements"
