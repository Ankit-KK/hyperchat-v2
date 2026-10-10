"""Seed public.platform_soundboard_slots from this environment's R2 library (legacy seed-platform-soundboard).

Same pairing as legacy: every hypersounds/<stem>.mp3 (in key order) with the hypersound-thumbs/<stem>.<ext> of
the same case-insensitive stem, preferring gif > png > jpg > jpeg > webp; sounds without a thumbnail are skipped.
Display names are title-cased stems and audio_duration_s is legacy's 3.0 placeholder (the server may only read
creator soundboard objects, never the library, so the length is not measured). Only ever fills an empty table,
all rows in one transaction under an advisory lock: running it twice, or twice at once, inserts nothing more.
"""

from app.soundboard.library import SOUNDS, THUMBS, display_name

IMAGE_PRIORITY = {"gif": 0, "png": 1, "jpg": 2, "jpeg": 3, "webp": 4}
MAX_SLOTS = 44  # default_order_index 0..43 (platform_default_order_range)
PLACEHOLDER_DURATION_S = 3.0
_LOCK = "hyperchat:seed-platform-soundboard"


def plan(sound_keys: list[str], thumb_keys: list[str], public_url) -> tuple[list[dict], list[str]]:
    """The rows to insert, and the sound keys skipped for having no thumbnail."""
    thumbs: dict[str, tuple[str, str]] = {}
    for key in thumb_keys:
        stem, dot, ext = key[len(THUMBS):].rpartition(".")
        ext = ext.lower()
        if not dot or not stem or ext not in IMAGE_PRIORITY:
            continue
        current = thumbs.get(stem.lower())
        if current is None or IMAGE_PRIORITY[ext] < IMAGE_PRIORITY[current[1]]:
            thumbs[stem.lower()] = (key, ext)
    rows, unpaired = [], []
    for key in sorted(sound_keys):
        stem = key[len(SOUNDS):-len(".mp3")].lower()
        if not key.endswith(".mp3") or not stem:
            continue
        thumb = thumbs.get(stem)
        if thumb is None:
            unpaired.append(key)
            continue
        rows.append({"display_name": display_name(stem), "audio_key": key, "audio_url": public_url(key),
                     "audio_duration_s": PLACEHOLDER_DURATION_S, "image_key": thumb[0],
                     "image_url": public_url(thumb[0]), "image_type": "jpg" if thumb[1] == "jpeg" else thumb[1],
                     "default_order_index": len(rows)})
    return rows, unpaired


async def existing_slots(connection_factory) -> int:
    async with connection_factory() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM public.platform_soundboard_slots")
        return (await cur.fetchone())[0]


async def insert_if_empty(connection_factory, rows: list[dict]) -> int:
    """Insert every row in one transaction, only into an empty table. Returns the rows already there (0 = seeded)."""
    async with connection_factory() as conn, conn.transaction(), conn.cursor() as cur:
        await cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_LOCK,))
        await cur.execute("SELECT count(*) FROM public.platform_soundboard_slots")
        existing = (await cur.fetchone())[0]
        if existing == 0:
            await cur.executemany("""
                INSERT INTO public.platform_soundboard_slots
                    (display_name, audio_key, audio_url, audio_duration_s, image_key, image_url, image_type,
                     default_order_index)
                VALUES (%(display_name)s, %(audio_key)s, %(audio_url)s, %(audio_duration_s)s, %(image_key)s,
                        %(image_url)s, %(image_type)s, %(default_order_index)s)
            """, rows)
        return existing
