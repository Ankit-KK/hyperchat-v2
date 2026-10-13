# Partner API (ClipWave, D4)

Server-to-server API for partner platforms. The only partner is **ClipWave**. It replaces the legacy Supabase Edge
Functions `partner-signup`, `partner-check-streamer` and `generate-partner-key` with the same contract, so ClipWave only
changes the URL (owner decision D4, 2026-10-13: keep the ClipWave integration). Code: `backend/app/partners/`,
`backend/app/routers/partners.py`.

| Legacy (Supabase) | V2 |
|---|---|
| `POST https://<project>.supabase.co/functions/v1/partner-signup` | `POST https://<api host>/api/v1/partner/signups` |
| `POST https://<project>.supabase.co/functions/v1/partner-check-streamer` | `POST https://<api host>/api/v1/partner/streamers/check` |
| `generate-partner-key` (admin session, `{action}` body) | `GET / POST /api/v1/admin/partner-keys`, `POST /api/v1/admin/partner-keys/{id}/revoke` |

API hosts: DEV `https://api.quicktestspace.site`, production `https://api.hyperchat.space`.

## Authentication
- Header **`x-api-key: <key>`** on every partner request. No cookie, CSRF token, Origin, HMAC signature or
  Supabase header is needed or used.
- Keys look like `hc_<partner_slug>_<64 hex>` (20–200 characters accepted). HyperChat stores only the key's SHA-256 and
  a 12-character display prefix, and records each key's last use. **The existing ClipWave keys keep working unchanged**:
  the lookup is the same SHA-256 the legacy functions used.
- The partner is **always the key's `partner_slug`**. A `partner_slug` in the body is refused (400); one in the query
  string is ignored.
- Missing, malformed, unknown or revoked key: `401 {"error": "Invalid or missing API key"}` (the same for every case).
- Keys are server-side only: never in a browser, an app bundle or a repository. The API sends no CORS headers for
  partners.

## `POST /api/v1/partner/signups`
Records a creator signup for the calling partner in `partner_signups`. It never creates a HyperChat user, creator or
organic signup; HyperChat staff follow up from the table.

Body (JSON, at most 10 KB):

| Field | Required | Rule |
|---|---|---|
| `channel_name` | yes | 2–100 characters after trimming, no emoji (`name` is accepted as the legacy deprecated alias) |
| `mobile_number` | yes | 10–15 digits once separators (`+`, spaces, `-` …) are ignored; stored as sent (trimmed) |
| `youtube_channel` | yes | `youtube.com`, `m.youtube.com` or `youtu.be` (with or without `www.`), http(s), at most 300 characters; `https://` is added when the scheme is left out |
| `email` | yes | a valid address, at most 254 characters; stored lowercased |
| `instagram_handle` | no | at most 100 characters |

```bash
curl -X POST https://api.quicktestspace.site/api/v1/partner/signups \
  -H "x-api-key: $HYPERCHAT_PARTNER_KEY" -H "content-type: application/json" \
  -d '{"channel_name":"Clip Wave Tester","mobile_number":"9876543210",
       "youtube_channel":"https://youtube.com/@clipwavetest","email":"creator@example.com",
       "instagram_handle":"@clipwave"}'
```

| Status | Body | Meaning |
|---|---|---|
| 201 | `{"success": true, "id": "<uuid>", "already_on_hyperchat": false}` | Recorded |
| 200 | `{"success": true, "duplicate": true, "already_on_hyperchat": false}` | This partner already sent this email (any letter case): success, do not retry |
| 400 | `{"error": "Validation failed", "fields": {"<field>": "<message>"}}` | Field errors (unknown fields included) |
| 400 | `{"error": "Invalid JSON body"}` | Not JSON |
| 401 | `{"error": "Invalid or missing API key"}` | Bad key |
| 413 | `{"error": "Request too large"}` | Body over 10 KB |
| 429 | `{"error": "Too many requests. Please try again later."}` | More than 60 requests per minute for this partner |
| 503 | `{"error": "Partner API unavailable. Please try again later."}` | Database unavailable: retry with backoff |

`already_on_hyperchat` uses the same match as the creator check below and is only a boolean.

## `POST /api/v1/partner/streamers/check`
Answers one question: is this channel name already a HyperChat creator? Body (at most 2 KB):
`{"channel_name": "clumsy_god"}` (1–100 characters).

```json
{"success": true, "exists": true,  "message": "This creator is already registered on HyperChat."}
{"success": true, "exists": false, "message": "This creator is not registered on HyperChat yet."}
```

- **Matching:** the name is normalized as legacy did (trimmed, lowercased, spaces and `-` become `_`, anything other
  than `a-z 0-9 _` removed, repeated `_` collapsed) and compared **exactly** with creator slugs. V2 slugs may also
  contain `-`, so the same name with `-` kept is tried too (`Clip-Wave` matches `clip_wave` or `clip-wave`). No partial,
  fuzzy or wildcard matching. Every creator row counts, whatever its status (as legacy).
- **Nothing else is returned:** never a name, id, status, owner or any other creator or user data.
- Errors: `400` validation (`{"error": "Validation failed", "fields": {"channel_name": …}}`), `401` bad key, `413`
  body over 2 KB, `429` more than 30 requests per minute for this partner (no bulk scanning).

## Admin key management (`/api/v1/admin/partner-keys`)
Admins only, signed in, with the CSRF token and a trusted Origin on writes (as every admin route).

| Method | Path | Body | Result |
|---|---|---|---|
| GET | `` | none | `{"keys": [{id, partner_slug, key_prefix, revoked, last_used_at, created_at}]}`: never a key or a hash |
| POST | `` | `{"partner_slug": "clipwave"}` (2–50 of `a-z 0-9 _ -`) | 201 `{"api_key": "hc_clipwave_<64 hex>", "key": {...}}` with `Cache-Control: no-store`. **The only time the key is shown.** |
| POST | `/{id}/revoke` | none | `{"key": {...}, "changed": true\|false}`; revocation is permanent and immediate |

Creating a key never touches the partner's other keys. Rotation: create a new key, give it to the partner, switch
them over, then revoke the old one. Security events: `partner_key_created`, `partner_key_revoked`,
`partner_signup_recorded`, `partner_rejected` (with `partner_slug`, never a key or hash).

## Deliberate differences from legacy
- Unknown body fields, non-string values and control characters are refused (legacy ignored them or read them as empty).
- A YouTube link must be http(s) without a user part (`https://youtube.com@evil.example` is refused). It is stored as
  sent, with `https://` added when the scheme was left out (legacy re-serialised it through `URL()`).
- The creator check also tries the `-`-preserving form (above).
- Duplicates are found with `ON CONFLICT` on the legacy unique index `(partner_slug, lower(email))`.
- No CORS for partners.
- Server errors are `503 Partner API unavailable` (legacy: `500` with a message).

## Migration for ClipWave (production cutover, separate approval)
1. Before the cutover: ClipWave tests against DEV with a DEV-only key that an admin creates for them (the production
   key must not be used on DEV).
2. At the cutover: ClipWave changes the two URLs above to `https://api.hyperchat.space/...`. Same header, same key,
   same bodies and responses. The legacy partner rows and keys are migrated with the rest of the data, so the existing
   production key keeps working.
3. After the switch: confirm `last_used_at` advances on V2 and no calls reach the legacy functions, then retire them.

## Security rules
- The key is a credential: header only, never a query parameter; HyperChat logs request paths and security events
  only, never headers, keys or key hashes.
- Every request counts against the per-partner rate limit before the body is read.
- Responses are `Cache-Control: no-store`.
