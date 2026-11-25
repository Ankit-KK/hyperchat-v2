# Creator images API (onboarding Stage 4, Phase A)

Every creator uploads a **profile picture** and a **16:9 background** (PDP v1.1 §6). The API validates, decodes and
re-encodes both server-side and writes WebP variants to Cloudflare R2. A creator can replace either image at any time
without admin approval. Code: `backend/app/onboarding/images.py`, `backend/app/routers/creator_images.py`.

Owner decisions implemented here:

- **Q8**: upload limits are 5 MB (profile) and 10 MB (background). They replace D-M2c's 1 MB / 3 MB for new uploads.
  D-M2c's figures remain only the reporting threshold for legacy files (`operations/legacy-asset-migration-plan.md`).
- **Q9**: the profile picture is stored in the existing `streamers.brand_logo_url` column; the background in
  `streamers.background_url`. No new column.
- **Q10**: Pillow, pinned (`backend/requirements.txt`), with a decode pixel cap and decoding in a bounded thread pool.

Phase A adds no migration, no table, no job kind, no role and no grant (PAY-15 soak until 2026-12-01).

## Routes

Base path `/api/v1/streamers/{streamer}/images` (`{streamer}` is the creator's UUID or slug).

| Method | Path | Body | Answer |
|---|---|---|---|
| `PUT` | `.../images/profile` | raw image | 200 upload result |
| `PUT` | `.../images/background?crop_x&crop_y&crop_width&crop_height` | raw image | 200 upload result |
| `GET` | `.../images` | | `{"profile": {"url"}, "background": {"url"}}` (the two pointers; `null` when unset) |

- **Access**: the owner, an account linked to the creator (`users.streamer_id`; linked accounts have full owner power,
  owner decision 2026-11-25) or an admin with admin assurance: `require_streamer_access`, as the dashboard.
  An unclaimed creator is admin-only. A **pending** creator may upload: onboarding happens while pending.
- **Origin + CSRF** (`x-csrf-token`) on `PUT`, as every dashboard write. Anonymous: 401; another user: 403.
- Every response is `Cache-Control: no-store`. Errors are `{"error": "..."}`.
- The request body is the file itself; `Content-Type` is its type (`image/jpeg` or `image/jpg`, `image/png`,
  `image/webp`).

Upload result:

```json
{
  "kind": "background",
  "url": "<public base>/creator-assets/<uuid>/background/<sha256>.webp",
  "quality_warning": false,
  "source": {"width": 4000, "height": 2250},
  "variants": [
    {"url": ".../<sha256>.webp", "width": 1920, "height": 1080, "bytes": 182344},
    {"url": ".../<sha256>.webp", "width": 2560, "height": 1440, "bytes": 301920}
  ]
}
```

`url` is the first variant, the one written to the pointer column. The other variant URLs are returned only here:
Phase A stores no list of variants (the `GET` answers only the pointers).

### Background crop

The optional crop rectangle is four integer query parameters, all or none: `crop_x`, `crop_y` (top-left corner,
≥ 0), `crop_width`, `crop_height` (> 0). They are in **source pixels after EXIF orientation** (the image as a browser
displays it). The rectangle must lie inside the image (else 400 `bad_crop`). The server then takes the largest 16:9
box centred in it, so a client cropper's rounding does not matter. Without a rectangle, the largest centred 16:9 box
of the whole image is used. A profile upload with a crop is refused (400).

## Limits

| | Profile | Background |
|---|---|---|
| Size | ≤ 5 MB (Q8) | ≤ 10 MB (Q8) |
| Types | JPEG, PNG, WebP | JPEG, PNG, WebP |
| Pixels | ≤ 40 megapixels | ≤ 40 megapixels |
| Minimum | none; `quality_warning: true` below 512 px on the longest side | 1920x1080 after the 16:9 crop, in source pixels |
| Variants (WebP) | 1024 px and 256 px on the longest side, never upscaled; identical variants are stored once | 1920x1080, plus 2560x1440 when the crop is at least that large; never upscaled |
| Pointer | `brand_logo_url` = the 1024 px variant (Q9) | `background_url` = the 1920x1080 variant |

- **Rate limit**: 20 upload attempts per creator per day (both kinds together, refused ones included), in
  `public.rate_limits` (`creator:<uuid>`, bucket `upload:creator-image`, 24 h window). 429 beyond that.
- **One upload at a time** per creator and kind: 409 `image_upload_in_progress` while another one is writing.
- **Decode slots**: at most 2 images are processed at once per API process; a third gets 503 `image_busy`
  (try again). Each slot may hold one pooled database connection while it writes.

## Validation order

1. Declared `Content-Type` (415) and `Content-Length` (413), **before** the body is read.
2. The rate limit (429).
3. The body, read with a hard cap, also for chunked uploads without `Content-Length` (413).
4. Signature sniff (`app.uploads.media.sniff_image`): JPEG, PNG or WebP only (GIF, SVG, video: 415), matching the
   declared type (415 `image_signature_mismatch`).
5. Pillow decode, restricted to the sniffed format. The pixel count is checked from the header before any pixel is
   decoded (422 `image_too_many_pixels`); unreadable or truncated data is 422 `image_corrupt`. Animated input uses its
   first frame. Decoding runs in a worker thread under the per-process slots.
6. EXIF orientation applied; colour modes (CMYK, palette, greyscale, 16-bit) converted to RGB (or RGBA for a
   transparent profile picture).
7. Background: crop and minimum (400 / 422 `background_too_small`). Profile: the quality warning.
8. Each variant re-encoded to WebP with **no metadata**: no EXIF (GPS included), ICC profile or XMP.

The original upload is never stored: the R2 bucket is served publicly, so nothing unprocessed may reach it.

## Storage

Key scheme: the S6 scheme of `operations/legacy-asset-migration-plan.md` §2, unchanged:

```
creator-assets/<streamer uuid>/<logo|background>/<sha256 of the variant's own bytes>.webp
```

- `WRITABLE_KEY` in `backend/app/storage/r2.py` admits `CREATOR_ASSET_KEY`
  (`^creator-assets/<uuid>/(logo|background)/[0-9a-f]{64}\.(jpg|png|webp|gif)$`), which also covers the later legacy
  copies. `head_creator_asset` reads only such keys.
- Objects are written with `Content-Type: image/webp` and `Cache-Control: public, max-age=31536000, immutable`: a key
  is content-addressed, so it never holds other bytes.
- A variant whose key already exists (the same image uploaded again) is not written again and is reused.
- **Delete**: `delete_creator_asset` accepts only `.webp` keys under `creator-assets/`. The pipeline calls it only for
  objects **its own failed call created**. An object that existed before the call (for example the live image,
  uploaded again) is never deleted, and legacy copies are never deleted (D-M2b).

## Atomicity and failures

- Decoding happens before any database connection or R2 write.
- Then, in one transaction: a transaction-scoped advisory lock on (creator, kind) (409 when taken), the R2 writes,
  `SELECT … FOR UPDATE` on the creator row, the pointer `UPDATE`, commit.
- Any failure from the first R2 write to the commit (R2 error: 502 `storage_failed`; database error; the creator
  gone) deletes the objects the call created and leaves the pointer as it was. A delete that itself fails is logged
  (`creator_image_orphaned`, codes only) and left for Phase B's cleanup.
- **Deviation from the Stage 1 review** (§4, "one upload in progress per creator and kind, enforced with a row lock on
  the creator"): the in-progress guard is an advisory lock keyed by creator and kind, and the creator row is locked
  only for the pointer update. A row lock held across R2 writes would block that creator's donations (their trigger
  takes `FOR SHARE` on the row) and settings saves for as long as R2 is slow, and could not tell the two kinds apart.
- **Replacement**: the new pointer simply replaces the old one. Superseded objects stay in R2; Phase B tracks and
  removes them.
- **Realtime**: no outbox event. Neither column is an overlay setting (`app.realtime.outbox.OVERLAY_SETTINGS`), and no
  realtime payload carries them.

## What Phase B adds (after 2026-12-01)

- The `creator_image_uploads` table (migration 0039): one row per variant with its role (1024 px, 256 px, 1920x1080,
  2560x1440), dimensions and state `ready` / `superseded` / `failed`. The pointer switch and the row states change in
  one transaction.
- **Validity is the row, not the prefix.** Legacy copies live under the same prefix unvalidated, so only a `ready`
  row satisfies activation conditions 6 and 7 (Stage 5). A Phase A upload has no row yet.
- The delete pattern narrows to keys a row marks `failed` or `superseded`. The daily cleanup removes superseded objects
  after 30 days.
- `GET …/images` can then list every variant.

## Configuration

None new. Uploads need R2 configured (the `HYPERCHAT_R2_*` variables of [`uploads-api.md`](uploads-api.md)); without
it the routes answer 503 `uploads_not_configured`.
