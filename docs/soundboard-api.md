# Creator soundboard and hypersound library API (Phase 6)

All soundboard routes live under `/api/v1/dashboard/streamers/{streamer}/soundboard`.

**Access:**
- **Who:** you must be signed in as the owner, a linked account or an admin. An unclaimed creator is admin-only.
- **Writes:** also need the `x-csrf-token` header and a trusted `Origin`.
- **Custom Soundboard gate (D3):** checked only after access, on every route below except `/terms` and
  `/terms/accept`. Both are needed:
  - eligibility, granted by an admin (`streamers.soundboard_enabled`): otherwise
    `403 {"error": "Soundboard not enabled"}`;
  - the owner's acceptance of the current terms version `soundboard-2026-10-13`, not revoked: otherwise
    `403 {"error": "Accept the current Custom Soundboard terms first"}`. Older versions' acceptances never count.

**Responses:**
- Never cached (`Cache-Control: no-store`).
- Errors are `{"error": "..."}`; request validation errors are FastAPI's 422 `{"detail": [...]}`.

| Method | Path | Body | Success |
|---|---|---|---|
| GET | `/terms` | none | `{current_version, eligible, accepted, accepted_at, revoked, usable, can_accept}` (owner, linked account or admin) |
| POST | `/terms/accept` | `{version: "soundboard-2026-10-13"}` | the same status object; owner only (`403 "Only the creator's owner can accept these terms"`); any other version `409 "Terms version is not current"`; a revoked acceptance `409 "This acceptance was revoked by HyperChat"`; repeating it changes nothing |
| GET | `` | none | `{active[], hidden[], revision, budget}` |
| PATCH | `` | `{hide[{id}], update[{id, display_name}], reorder[{id, order_index}], unhide[{id, order_index}]}` | `{visible_count}` |
| POST | `/restore-defaults` | none | `{restored: true}` |
| POST | `/uploads` | `{kind: "audio" \| "image", content_type, content_length}` | 201 `{upload_url, key, required_headers, expires_in: 300}` |
| POST | `/slots` | `{display_name, audio_key, image_key}` | 201 `{slot_id}` (hidden custom sound) |
| PUT | `/swappable/{index}` | `{display_name, audio_key, image_key}` | `{slot_id, slot_cooldown_ends_at, swaps_used, swaps_limit}` |
| PATCH | `/slots/{slot_id}/audio` and `/image` | `{audio_key}` / `{image_key}` | `{result: "replaced"}` |
| DELETE | `/slots/{slot_id}` | none | `{result: "removed"}` |

**Slot fields:**
`id, source (platform|custom), is_visible, order_index, is_locked, swappable_index, last_swapped_at, display_name, audio_url, audio_duration_s, image_url, image_type, default_order_index`.

- A creator's row is merged with its platform sound.
- `budget` is `{swaps_used, swaps_limit: 15, window_days: 7, next_budget_slot_frees_at, slot_cooldowns{position: ends_at}}`.
- Object keys are never returned in the board.

**Terms (D3):**
- `accepted` means the current version was accepted (even if revoked since); `usable` is
  `eligible AND accepted AND NOT revoked`; `can_accept` is true for the owner while the acceptance is not revoked.
- Accepting does not need eligibility (a creator may accept before an admin grants access). It records the owner,
  their email and User-Agent in `soundboard_tnc_acceptances`: one row per creator, version and owner (migration 0019);
  only the current owner's acceptance counts, so after an ownership transfer (D13) the new owner must accept; other
  owners' and older versions' rows are never changed.
- Eligibility is changed only by an admin: `PUT /api/v1/admin/creators/{id}/soundboard` `{enabled}` (CSRF; idempotent;
  `{creator_id, soundboard_enabled, changed}`). Revoking an acceptance has no endpoint (HyperChat sets `revoked_at`).

## Rules
- **The first GET seeds the creator's rows** from the platform set, as legacy `seed_streamer_soundboard` did.
- **PATCH applies in the order hide → update → reorder → unhide, in one transaction.** Any error rolls back everything:

  | Status | Cause |
  |---|---|
  | 404 | Unknown, malformed or another creator's sound id |
  | 403 | Locked platform sound |
  | 409 | Hidden sound reordered, position clash, or more than 45 visible |
  | 422 | Name not 1–60 characters, position not 0–45, or a duplicate id in one list |

- **Restore defaults:**
  - custom sounds are hidden but kept;
  - platform overrides are cleared;
  - locked and swappable platform sounds are shown at their default positions.
- **Swap** a position 1–45 (otherwise 400):
  - at most 15 swaps per creator in 7 days;
  - 24 hours per position;
  - when limited: 429 `{error, code: "budget_exhausted" | "slot_cooldown_active", swaps_used, swaps_limit, next_budget_slot_frees_at | slot_cooldown_ends_at}`.
  
  The platform sound at that position (found through the creator's row or its platform slot) is hidden and its place taken. An existing custom sound at the position is updated in place, and its previous files are removed. Every swap is written to `soundboard_swap_log`.
- **Delete:** custom sounds only; platform sounds can only be hidden (403).

## Uploads (presigned PUT to R2)
1. **Request an upload URL:** `POST /uploads`.
   - The key is generated by the server: `soundboards/<slug>/<uuid>.<ext>`.
   - The PUT URL is valid 300 s, and its signature covers `host`, `content-type` and `content-length`. The client must PUT exactly that type and size.
   - Rate limit: 30 per user per 10 minutes.
   - Allowed: audio `audio/mpeg` ≤ 1,500,000 bytes; images GIF/PNG/JPEG/WebP ≤ 3,000,000 bytes (400 otherwise).
2. **PUT the file** to `upload_url` with `required_headers`.
3. **Create, swap or replace** with the key(s). The server confirms each key, and every failure is a 422:
   - the key is this creator's server key shape for the right kind (other creators' prefixes, `hypersounds/`, donor or legacy keys, traversal: all refused);
   - it is not used by any slot;
   - the object exists in R2, with the expected stored type and size within the limit;
   - the bytes are really an MP3 (Layer III) of at most 7.0 s (+0.1 s) or a GIF/PNG/JPEG/WebP whose signature matches.

   The duration is measured on the server: the larger of mutagen's length and an independent frame-by-frame count, so VBR or Xing header tricks cannot understate it. Rejected uploads are deleted. R2 errors are a generic 502; missing R2 configuration is 503.

**Deletion** of R2 objects only happens after the database change has committed, best-effort, and only when all of these hold:
- the object is a soundboard key under the current creator's prefix;
- its stored URL is this environment's bucket (legacy rows point elsewhere);
- no slot still references it.

Platform hypersounds, donor uploads and legacy objects are never deleted; the storage layer itself refuses to delete anything but soundboard keys.

## Hypersound library
`GET /api/v1/public/hypersounds` (public, `Cache-Control: public, max-age=60`).
- **Response:** `{sounds: [{id, display_name, audio_url, thumb_url}]}`.
- **Sounds:** listed from this environment's bucket, `hypersounds/*.mp3`.
- **Thumbnails:** paired from `hypersound-thumbs/<same stem>`, preferring gif > jpg > jpeg > png, case-insensitive.
- **URLs:** built from `HYPERCHAT_R2_PUBLIC_BASE_URL`.
- **Caching:** the listing is cached for 60 s in-process; failures are not cached (502).
