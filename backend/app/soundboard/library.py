"""Public hypersound library (legacy list-hypersounds): the platform sounds stored in this environment's
bucket under hypersounds/*.mp3, each paired with a thumbnail under hypersound-thumbs/<same stem>.<ext>
(case-insensitive stem; preference gif > jpg > jpeg > png, as legacy). Read-only: listed from R2 and
cached in-process for CACHE_SECONDS; URLs use the configured public base. Nothing here writes or deletes.
"""

import asyncio
import re
import time

from app.core.errors import ApiError
from app.soundboard.models import Hypersound, Hypersounds
from app.storage.r2 import ObjectStore, StorageError

SOUNDS, THUMBS = "hypersounds/", "hypersound-thumbs/"
THUMB_PRIORITY = {"gif": 0, "jpg": 1, "jpeg": 2, "png": 3}
CACHE_SECONDS = 60
UNAVAILABLE = "Sound library unavailable"


def display_name(stem: str) -> str:
    """'air-horn_2' -> 'Air Horn 2' (legacy seed-platform-soundboard naming)."""
    return re.sub(r"\b\w", lambda m: m.group(0).upper(), re.sub(r"[-_]+", " ", stem.lower())).strip()


def build(sound_keys: list[str], thumb_keys: list[str], public_url) -> list[Hypersound]:
    thumbs: dict[str, tuple[int, str]] = {}
    for key in thumb_keys:
        name = key[len(THUMBS):]
        stem, dot, ext = name.rpartition(".")
        if not dot or ext.lower() not in THUMB_PRIORITY:
            continue
        rank = THUMB_PRIORITY[ext.lower()]
        current = thumbs.get(stem.lower())
        if current is None or rank < current[0]:
            thumbs[stem.lower()] = (rank, key)
    sounds = []
    for key in sorted(sound_keys):
        name = key[len(SOUNDS):]
        if not name.lower().endswith(".mp3") or len(name) <= 4:
            continue
        stem = name[:-4]
        thumb = thumbs.get(stem.lower())
        sounds.append(Hypersound(id=stem, display_name=display_name(stem.rsplit("/", 1)[-1]) or stem,
                                 audio_url=public_url(key), thumb_url=public_url(thumb[1]) if thumb else None))
    return sounds


class HypersoundLibrary:
    def __init__(self, store: ObjectStore | None, clock=time.monotonic) -> None:
        self.store, self._clock = store, clock
        self._cached: tuple[float, Hypersounds] | None = None
        self._lock = asyncio.Lock()

    async def sounds(self) -> Hypersounds:
        if self.store is None:
            raise ApiError(503, UNAVAILABLE, "storage_not_configured")
        if self._cached and self._cached[0] > self._clock():
            return self._cached[1]
        async with self._lock:
            if self._cached and self._cached[0] > self._clock():
                return self._cached[1]
            try:
                sound_keys = await self.store.list_keys(SOUNDS)
                thumb_keys = await self.store.list_keys(THUMBS)
            except StorageError:
                raise ApiError(502, UNAVAILABLE, "library_list_failed") from None  # failures are not cached
            result = Hypersounds(sounds=build(sound_keys, thumb_keys, self.store.public_url))
            self._cached = (self._clock() + CACHE_SECONDS, result)
            return result
