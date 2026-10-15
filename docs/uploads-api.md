# Donor uploads API (Phase 6, steps 1–4)

Anonymous donors upload a voice message or an image **before** paying. The upload returns a public URL
that the donation is later created with (Phase 7). Files are validated server-side and written to
Cloudflare R2 by the API; clients never choose object keys and never talk to R2 directly.

Base: `https://api.quicktestspace.site` (development). All three routes are public (no cookies) but:
- `Origin` must be a trusted frontend origin (`HYPERCHAT_FRONTEND_URL` + `HYPERCHAT_ALLOWED_ORIGINS`), else 403;
- each action is rate limited per client IP (fixed 60 s window, `public.rate_limits`);
- every response is `Cache-Control: no-store`; errors are `{"error": "..."}` (422 validation errors use
  FastAPI's `{"detail": [...]}`).

| Method | Path | Rate limit |
|---|---|---|
| POST | `/api/v1/public/streamers/{slug}/upload-grant` | 10 / min (`upload:grant`) |
| POST | `/api/v1/public/streamers/{slug}/uploads/voice` | 5 / min (`upload:voice`) |
| POST | `/api/v1/public/streamers/{slug}/uploads/media` | 3 / min (`upload:media`) |

## 1. Upload grant
Request body: `{"kind": "voice" | "media"}` (no other fields).
Response 201: `{"grant": "<opaque string>", "expires_at": "2026-10-07T10:20:30.123Z"}`.

The grant is HMAC-signed (`HYPERCHAT_UPLOAD_GRANT_SECRET`), valid 10 minutes, bound to **one creator and
one upload type**, and single-use: it is burned by inserting `upload:<nonce>` into `public.request_nonces`
just before the file is stored. Request a new grant for every upload attempt.

| Status | When |
|---|---|
| 403 | untrusted/missing Origin; `kind: media` for a creator without media uploads |
| 404 | unknown, pending, rejected or malformed creator slug (identical body) |
| 422 | body missing/invalid |
| 429 | rate limit |
| 503 | uploads not configured (grant secret or R2 missing); `Donations are temporarily unavailable` (the creator may not accept new donations, migration 0020) |

## 2. Uploads
The request body is the raw file; `Content-Type` is the file's type; the grant goes in `x-upload-grant`.

| | Voice | Media |
|---|---|---|
| Content-Type | `audio/webm` (parameters such as `;codecs=opus` allowed) | `image/jpeg` (`image/jpg`), `image/png`, `image/webp`, `image/gif` |
| Bytes | WebM document, exactly one Opus audio track, no video | JPEG / PNG / WebP / GIF signature matching the declared type |
| Size | ≤ 5 MB | ≤ creator's `media_max_file_size_mb` (NULL = 10), never above 10 MB |
| Other | duration ≤ creator's longest voice tier + 2 s | creator must have `media_upload_enabled` |
| Response 201 | `{"voice_message_url": "<url>"}` | `{"media_url": "<url>", "media_type": "image" \| "gif"}` |
| Object key | `voice-messages/<slug>_<epoch ms>_<uuid4 hex>.webm` | `donation-media/<slug>_<epoch ms>_<uuid4 hex>.<ext>` |

URLs are `HYPERCHAT_R2_PUBLIC_BASE_URL` + `/` + key. Objects are stored with the sniffed content type and
`Cache-Control: public, max-age=31536000, immutable`; voice objects carry `x-amz-meta-duration-ms` (the
server-measured duration, for the payment step).

Voice duration is measured from the file itself (`backend/app/uploads/webm.py`): the largest of the sum of
the Opus packet lengths (RFC 6716 TOC byte), the timecode span and the Duration header, so neither missing
headers (normal for browser MediaRecorder output) nor rewritten timestamps can understate it. mutagen
cannot read Matroska/WebM, so a small bounded EBML reader is used.

| Status | When |
|---|---|
| 400 | empty body, invalid Content-Length |
| 403 | untrusted Origin; grant missing, malformed, expired, for another creator, for the other upload type, already used, or with a bad signature; media disabled |
| 404 | unknown/inactive creator |
| 413 | declared or actual size above the limit (checked before and while reading) |
| 415 | type not allowed, bytes do not match the type, not WebM/Opus, any video |
| 422 | voice message too long |
| 429 | rate limit (failed attempts count too) |
| 502 | R2 write failed (generic message; the grant is already burned) |
| 503 | not configured, or the nonce store is unavailable (nothing stored) |

Order: Origin → configuration → rate limit → creator → declared type → declared size → grant (signature,
expiry, creator, type) → body (hard cap) → content checks → grant burned → R2 write. A rejected file does
not burn the grant.

## Configuration (names only; values live in `/etc/hyperchat/api.env`)
`HYPERCHAT_UPLOAD_GRANT_SECRET`, `HYPERCHAT_R2_ACCOUNT_ID`, `HYPERCHAT_R2_ACCESS_KEY_ID`,
`HYPERCHAT_R2_SECRET_ACCESS_KEY`, `HYPERCHAT_R2_BUCKET`, `HYPERCHAT_R2_PUBLIC_BASE_URL`, optional
`HYPERCHAT_R2_ENDPOINT`. Missing or invalid values never stop the API: uploads answer 503 and the startup
log lists the offending variable names. The R2 client (boto3) writes only keys matching the patterns above.

## Access and retention (owner decision D9, 2026-10-15)
- **Public, unguessable URLs** (no signed GET, no private bucket, no proxy): the key's uuid4 carries 122 random
  bits and the bucket is never publicly listable. Anyone holding a URL can fetch the object while it exists.
- **Abandoned uploads are deleted after 24 hours.** The daily `python -m app.cli cleanup` lists only
  `voice-messages/` and `donation-media/` and deletes an object only when its key matches the upload pattern,
  both its LastModified and the time in its key are more than 24 hours old, and no donation's
  `voice_message_url` or `media_url` ends in that key (on any host). A failed listing or reference query deletes
  nothing. Use an upload within minutes (the grant lives 10); a checkout naming an upload older than 24 hours
  may find it gone.
- **Donation media is kept** once a donation references it (paid or not), for replay and history.
- **Production** serves objects from a configured custom domain (`HYPERCHAT_R2_PUBLIC_BASE_URL`), never `r2.dev`.
