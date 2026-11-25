"""Creator images (onboarding Stage 4, Phase A): validate, decode, crop and re-encode a profile picture or a 16:9
background, then store content-addressed WebP variants in R2 (docs/creator-images.md).

Validation order (each step fails closed; nothing reaches R2 before step 6):
  1. declared Content-Type JPEG / PNG / WebP, Content-Length within the limit (before the body is read)   415 / 413
  2. body read with a hard cap (chunked uploads too)                                                       413
  3. signature sniff (app.uploads.media.sniff_image): JPEG, PNG or WebP, matching the declared type         415
  4. Pillow decode (those three formats only) under a pixel cap, in a bounded thread pool                  422 / 503
  5. EXIF orientation applied; background: crop to 16:9 (optional client rectangle), at least 1920x1080
     source pixels, never upscaled; profile: no minimum, quality_warning below 512 px on the longest side    400 / 422
  6. variants re-encoded to WebP with no metadata (no EXIF, ICC or XMP) and written to
     creator-assets/<streamer uuid>/<logo|background>/<sha256 of the variant's own bytes>.webp            502
  7. the pointer column (brand_logo_url / background_url) switched in one transaction under the creator row lock
The original upload is never stored. A failure after step 6 deletes the objects THIS call created (a key that
already existed, e.g. the live image uploaded again, is never deleted) and writes nothing to the database.

One upload per creator and kind is in progress at a time: a transaction-scoped advisory lock keyed by the creator
and the kind, held from the first R2 write to the commit (409 when taken). Decoding runs before it, without a
database connection; the creator row itself is locked only for the short pointer update, so a slow R2 write never
blocks the creator's donations (their trigger takes FOR SHARE on that row) or settings saves.
"""

import asyncio
import hashlib
import io
import logging
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps
from psycopg.rows import dict_row

from app.core.errors import ApiError
from app.security.audit import security_event
from app.storage.r2 import ObjectStore, StorageError
from app.uploads.media import MB, base_type, sniff_image

MAX_PIXELS = 40_000_000  # decode cap (decompression-bomb guard), checked from the header before any pixel is decoded
DECODE_SLOTS = 2         # per process: decodes + R2 write transactions at once (the pool has 4 connections)
DAILY_UPLOADS = 20       # per creator, both kinds together, failed attempts included
RATE_BUCKET = "upload:creator-image"
RATE_WINDOW_SECONDS = 24 * 3600
PROFILE_WARNING_PX = 512
BACKGROUND_MIN = (1920, 1080)
WEBP_QUALITY = 85
ALLOWED_TYPES = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
_ALIASES = {"image/jpg": "image/jpeg"}


@dataclass(frozen=True)
class Spec:
    key_dir: str            # R2 key segment (S6 scheme)
    column: str             # streamers column the primary variant's URL goes to
    max_bytes: int
    sizes: tuple            # profile: longest sides; background: (width, height) boxes, largest last


SPECS = {
    "profile": Spec("logo", "brand_logo_url", 5 * MB, (1024, 256)),          # owner decision Q9: brand_logo_url
    "background": Spec("background", "background_url", 10 * MB, ((1920, 1080), (2560, 1440))),
}


@dataclass(frozen=True)
class Crop:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class Variant:
    data: bytes
    width: int
    height: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class Processed:
    variants: tuple[Variant, ...]  # primary (pointer) variant first, duplicates removed
    source_width: int              # after EXIF orientation
    source_height: int
    quality_warning: bool


def too_large(kind: str) -> ApiError:
    return ApiError(413, f"File too large. Maximum size is {SPECS[kind].max_bytes // MB}MB.", "image_too_large")


def check_declared(kind: str, content_type: str | None, content_length: str | None) -> int:
    """Steps 1: the declared type and size, before the body is read. Returns the byte cap."""
    mime = base_type(content_type)
    if _ALIASES.get(mime, mime) not in ALLOWED_TYPES:
        raise ApiError(415, "Unsupported file type. Allowed: JPG, PNG, WebP", "image_type_rejected")
    if content_length is not None:
        if not content_length.isascii() or not content_length.isdigit():
            raise ApiError(400, "Invalid Content-Length", "bad_content_length")
        if int(content_length) > SPECS[kind].max_bytes:
            raise too_large(kind)
    return SPECS[kind].max_bytes


def parse_crop(x: int | None, y: int | None, width: int | None, height: int | None) -> Crop | None:
    values = (x, y, width, height)
    if all(v is None for v in values):
        return None
    if any(v is None for v in values) or x < 0 or y < 0 or width <= 0 or height <= 0:
        raise ApiError(400, "crop_x, crop_y, crop_width and crop_height go together and must be positive",
                       "bad_crop")
    return Crop(x, y, width, height)


def _refuse(error: str, reason: str, status: int = 422) -> ApiError:
    return ApiError(status, error, reason)


def _decode(data: bytes, declared: str) -> Image.Image:
    sniffed = sniff_image(data)
    if sniffed is None or sniffed not in ALLOWED_TYPES:
        raise _refuse("Unsupported file type. Allowed: JPG, PNG, WebP", "image_type_rejected", 415)
    if sniffed != declared:
        raise _refuse("File content does not match its type", "image_signature_mismatch", 415)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            im = Image.open(io.BytesIO(data), formats=[ALLOWED_TYPES[sniffed]])
            if im.width * im.height > MAX_PIXELS:  # from the header: nothing is decoded yet
                raise _refuse(f"Image has too many pixels (maximum {MAX_PIXELS // 1_000_000} megapixels)",
                              "image_too_many_pixels")
            im.load()  # first frame only for animated input
            return ImageOps.exif_transpose(im)
    except ApiError:
        raise
    except (OSError, SyntaxError, ValueError, EOFError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise _refuse("The image could not be read", "image_corrupt") from None


def _webp(im: Image.Image) -> Variant:
    im.info = {}  # belt and braces: the encoder takes metadata only from save() arguments, and we pass none
    out = io.BytesIO()
    im.save(out, "WEBP", quality=WEBP_QUALITY, method=4)
    return Variant(out.getvalue(), im.width, im.height)


def _fit(w: int, h: int, longest: int) -> tuple[int, int]:
    """Scale (w, h) so the longest side is at most `longest` (never up)."""
    scale = min(1.0, longest / max(w, h))
    return max(1, round(w * scale)), max(1, round(h * scale))


def _background_box(width: int, height: int, crop: Crop | None) -> tuple[int, int, int, int]:
    """The largest 16:9 box centred in the client rectangle (or the whole image), in oriented source pixels."""
    if crop is None:
        crop = Crop(0, 0, width, height)
    elif crop.x + crop.width > width or crop.y + crop.height > height:
        raise ApiError(400, "Crop rectangle lies outside the image", "bad_crop")
    w = min(crop.width, crop.height * 16 // 9)
    h = w * 9 // 16
    w = h * 16 // 9  # exact 16:9 after integer rounding
    left = crop.x + (crop.width - w) // 2
    top = crop.y + (crop.height - h) // 2
    return left, top, left + w, top + h


def process(kind: str, data: bytes, content_type: str | None, crop: Crop | None = None) -> Processed:
    """Steps 3-6 without I/O (CPU-bound: call it from a worker thread)."""
    if not data:
        raise ApiError(400, "Upload body is empty", "empty_body")
    if len(data) > SPECS[kind].max_bytes:
        raise too_large(kind)
    mime = base_type(content_type)
    im = _decode(data, _ALIASES.get(mime, mime))
    width, height = im.size
    spec = SPECS[kind]
    if kind == "profile":
        if crop is not None:
            raise ApiError(400, "Only the background takes a crop rectangle", "bad_crop")
        has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
        im = im.convert("RGBA" if has_alpha else "RGB")
        variants = [_webp(im.resize(_fit(width, height, side), Image.Resampling.LANCZOS)) for side in spec.sizes]
        warning = max(width, height) < PROFILE_WARNING_PX
    else:
        box = _background_box(width, height, crop)
        bw, bh = box[2] - box[0], box[3] - box[1]
        if bw < BACKGROUND_MIN[0] or bh < BACKGROUND_MIN[1]:
            raise ApiError(422, "Background must be at least 1920x1080 pixels after cropping to 16:9",
                           "background_too_small")
        im = im.convert("RGB")
        variants = [_webp(im.resize(size, Image.Resampling.LANCZOS, box=box))
                    for size in spec.sizes if size[0] <= bw and size[1] <= bh]  # never upscale
        warning = False
    unique = list({v.sha256: v for v in variants}.values())  # a tiny profile gives two identical variants
    return Processed(tuple(unique), width, height, warning)


def asset_key(streamer_id: str, kind: str, variant: Variant) -> str:
    return f"creator-assets/{streamer_id}/{SPECS[kind].key_dir}/{variant.sha256}.webp"


class CreatorImageService:
    """Routes -> process -> R2 -> pointer. `conn` is the API's pooled connection factory (hyperchat_app)."""

    def __init__(self, conn, store: ObjectStore | None, limiter, slots: asyncio.Semaphore) -> None:
        self._conn, self.store, self.limiter, self.slots = conn, store, limiter, slots

    def ensure_available(self) -> ObjectStore:
        if self.store is None:
            raise ApiError(503, "Image uploads are not available", "uploads_not_configured")
        return self.store

    async def check_rate(self, streamer_id: str) -> None:
        if not await self.limiter.hit_rate_limit(f"creator:{streamer_id}", RATE_BUCKET, DAILY_UPLOADS,
                                                 RATE_WINDOW_SECONDS):
            security_event("creator_image_rejected", reason="rate_limited", streamer_id=streamer_id)
            raise ApiError(429, "Too many image uploads today. Please try again tomorrow.", "image_rate_limited")

    async def current(self, streamer_id: str) -> dict:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT brand_logo_url, background_url FROM public.streamers WHERE id = %s",
                              (streamer_id,))
            row = await cur.fetchone()
        if row is None:
            raise ApiError(404, "Streamer not found", "streamer_not_found")
        return {"profile": {"url": row["brand_logo_url"]}, "background": {"url": row["background_url"]}}

    async def upload(self, streamer_id: str, kind: str, data: bytes, content_type: str | None,
                     crop: Crop | None) -> dict:
        store = self.ensure_available()
        if self.slots.locked():  # shed instead of queueing: each slot may hold a pooled connection
            raise ApiError(503, "Image processing is busy. Please try again shortly.", "image_busy")
        async with self.slots:
            processed = await asyncio.to_thread(process, kind, data, content_type, crop)
            keys = [asset_key(streamer_id, kind, v) for v in processed.variants]
            await self._store(store, streamer_id, kind, processed, keys)
        security_event("creator_image_stored", level=logging.INFO, reason=kind, streamer_id=streamer_id,
                       count=len(keys))
        return {
            "kind": kind, "url": store.public_url(keys[0]), "quality_warning": processed.quality_warning,
            "source": {"width": processed.source_width, "height": processed.source_height},
            "variants": [{"url": store.public_url(k), "width": v.width, "height": v.height, "bytes": len(v.data)}
                         for k, v in zip(keys, processed.variants)],
        }

    async def _store(self, store: ObjectStore, streamer_id: str, kind: str, processed: Processed,
                     keys: list[str]) -> None:
        created: list[str] = []
        try:
            async with self._conn() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS got",
                                  (f"hyperchat:creator-image:{streamer_id}:{kind}",))
                if not (await cur.fetchone())["got"]:
                    raise ApiError(409, "Another upload of this image is in progress", "image_upload_in_progress")
                for key, variant in zip(keys, processed.variants):
                    if await store.head_creator_asset(key) is None:  # content-addressed: an existing key is reused
                        await store.put_object(key, variant.data, content_type="image/webp")
                        created.append(key)
                await cur.execute("SELECT id FROM public.streamers WHERE id = %s FOR UPDATE", (streamer_id,))
                if await cur.fetchone() is None:
                    raise ApiError(404, "Streamer not found", "streamer_not_found")
                await cur.execute(f"UPDATE public.streamers SET {SPECS[kind].column} = %s WHERE id = %s",
                                  (store.public_url(keys[0]), streamer_id))
                # No realtime event: neither column is an overlay setting (app.realtime.outbox.OVERLAY_SETTINGS).
        except BaseException as exc:
            await self._discard(store, streamer_id, created)
            if isinstance(exc, StorageError):
                logging.getLogger("hyperchat.storage").warning(
                    "storage write failed", extra={"reason": str(exc), "streamer_id": streamer_id})
                raise ApiError(502, "Upload storage failed. Please try again.", "storage_failed") from None
            raise

    @staticmethod
    async def _discard(store: ObjectStore, streamer_id: str, created: list[str]) -> None:
        for key in created:
            try:
                await store.delete_creator_asset(key)
            except (StorageError, ValueError) as exc:  # left behind; Phase B's tracked cleanup can find it
                security_event("creator_image_orphaned", reason=str(exc)[:64], streamer_id=streamer_id)
