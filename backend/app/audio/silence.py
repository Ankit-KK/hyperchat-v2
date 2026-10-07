"""The silent fallback MP3 (system/silence.mp3 in this environment's bucket).

Legacy pointed every fallback at a file in the legacy bucket; v2 generates its own: ~1 s of MPEG-1 Layer III
frames (32 kbps, 44.1 kHz, mono) whose side information and main data are all zero, which every decoder
plays as silence. The worker uploads it once if it is missing.
"""

from app.storage.r2 import SILENCE_KEY, ObjectStore

_HEADER = bytes([0xFF, 0xFB, 0x10, 0xC0])  # sync, MPEG-1, Layer III, no CRC | 32 kbps, 44.1 kHz | mono
_FRAME_BYTES = 104                           # 144 * 32000 / 44100, no padding
FRAMES = 39                                  # 39 * 1152 / 44100 = 1.02 s


def silent_mp3(frames: int = FRAMES) -> bytes:
    return (_HEADER + bytes(_FRAME_BYTES - len(_HEADER))) * frames


async def ensure_silence(store: ObjectStore) -> str:
    """The public URL of the silence object, uploading it first when it does not exist yet."""
    head = await store.head_generated(SILENCE_KEY)
    if head is None or head.size == 0:
        await store.put_object(SILENCE_KEY, silent_mp3(), content_type="audio/mpeg")
    return store.public_url(SILENCE_KEY)
