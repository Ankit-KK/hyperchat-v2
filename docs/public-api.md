# Public read API (Phase 4)

Unauthenticated `GET` endpoints for the donation page, OBS overlays (initial state) and the landing
page. No cookies are required or set; CSRF does not apply (read-only). CORS follows the configured
browser-origin allowlist; requests without an `Origin` header (OBS browser sources) work.

| # | Endpoint | Cache-Control | Response fields |
|---|---|---|---|
| E1 | `GET /api/v1/public/streamers/{slug}` | 60 s | `slug, name, brand_color, brand_logo_url, background_url, tts_enabled, hyperemotes_enabled, media_upload_enabled, media_max_file_size_mb, soundboard_enabled, leaderboard_widget_enabled` |
| E2 | `GET …/{slug}/pricing?currency=INR` | 60 s | `currency, min_text, min_tts, min_voice, min_hypersound, min_media, tts_enabled, message_char_tiers[{min_amount, max_chars}] \| null, voice_duration_tiers[{min_amount, max_duration_seconds}]` |
| E3 | `GET …/{slug}/overlay-config` | 30 s | `slug, name, brand_color, brand_logo_url, alert_box_scale, alert_overlay_id, leaderboard_overlay_id, goal_overlay_id, overlay_adjust_mode, leaderboard_widget_enabled, leaderboard_bg_enabled, hyperemotes_enabled, media_upload_enabled, tts_enabled, goal_name, goal_target_amount, goal_activated_at, goal_is_active` |
| E4 | `GET …/{slug}/soundboard` | 60 s | `sounds[{id, display_name, audio_url, thumb_url}]` |
| E5 | `GET …/{slug}/leaderboard` | 10 s | `top_donor {name, total_amount_inr} \| null` |
| E6 | `GET …/{slug}/goal` | 10 s | `active, name, target_amount_inr, progress_inr` |
| E7 | `GET /api/v1/metrics/platform` | 300 s (+ in-process cache) | `total_interactions, total_alerts, active_streams` |
| E8 | `GET /api/v1/metrics/showcase` | 60 s (+ in-process cache) | `streamers_live, alerts_last_24h, last_sync` |
| E9 | `GET /api/v1/public/config` (D5) | 300 s | `contact {name, email, phones[], discord_url}` |

Errors: `404 {"error": "Streamer not found"}` for unknown, malformed, `pending` and `rejected` slugs
(identical, so the endpoints cannot be used to discover creators); `404 {"error": "Soundboard not
enabled"}` (the Custom Soundboard is served only while the creator is eligible **and** its owner accepted the
current terms, D3; the profile's `soundboard_enabled` means exactly that); `400 {"error": "Unsupported currency"}`; `429 {"error": "Too many requests"}` from Nginx.

Rate limiting: Nginx, per client IP, 10 requests/s with bursts of 20 on `/api/v1/public/` and
`/api/v1/metrics/` (`deploy/nginx/snippets/`); verified by `tests/e2e/nginx_ratelimit_check.sh`.
The database `rate_limits` table is not used for these reads.

Never returned: emails, user/creator ids, password or session data, CSRF/OBS tokens, API keys,
order/provider ids, donation messages, individual donation amounts, payment configuration,
moderation/TTS internals, `custom_page_path`, `pusher_group`, Discord/Telegram ids.
The only donor information is the leaderboard's top display name + total (E5) and goal totals (E6).
`sounds[].id` is the opaque soundboard slot id (needed later to choose a sound when donating).

## Intentional differences from the legacy Edge Functions (decisions D1-D10)
| Topic | Legacy | v2 |
|---|---|---|
| Hypersound library (`list-hypersounds`) | live R2 listing | not in Phase 4; moves to Phase 6 with R2 (D1) |
| Top donor | always public | only when `leaderboard_widget_enabled`; creators with only zero totals return `null`; ties broken by name (D2) |
| Latest-donations feed | with OBS token | not in Phase 4 (Phase 10, OBS endpoints) (D2) |
| Inactive creators | pricing/config/goal served for any existing slug | `pending`/`rejected` give the same 404 as unknown slugs (D3) |
| Currency | unknown currency priced as INR | only the 8 legacy currencies; others 400 (D4) |
| Goal progress | re-converted each donation with the fixed rate table | sums the stored `donations.amount_inr` (D4) |
| Field names | camelCase (`minText`, `topDonator`, `currentProgress`) | snake_case (D5) |
| Overlay config | included the creator `id` | no ids (privacy) |
| Request format | `POST` with JSON body (`streamer_slug`) | `GET` with the slug in the path |
| Showcase metrics | counted `payment_status = 'completed'` (never true → always 0); `streamersLive` = all creators | successful, approved donations in the last 24 h; `streamers_live` = active creators (D7) |
| Profile images / tagline | static frontend assets + hard-coded tagline per page | `brand_logo_url` / `background_url` from the database; no tagline (D8) |
| `custom_page_path` | used by hand-written pages | never exposed (D9) |
| Reviews, signups, Discord link | public table reads / browser inserts / config function | not part of Phase 4 (D10); signups added in backend completion G3, the Discord link in D5 (below) |

## Public contact config (owner decision D5, 2026-10-15)

`GET /api/v1/public/config` (E9) gives the frontend the platform's static contact details and Discord invite:

```json
{"contact": {"name": "…", "email": "…", "phones": ["…"], "discord_url": "https://discord.gg/…"}}
```

The values come only from `HYPERCHAT_PUBLIC_CONTACT_NAME`, `HYPERCHAT_PUBLIC_CONTACT_EMAIL`,
`HYPERCHAT_PUBLIC_CONTACT_PHONES` (comma-separated) and `HYPERCHAT_PUBLIC_DISCORD_URL`; an unset value is
`null` (`phones`: `[]`). They are intentionally public and validated at startup (a malformed value stops the API
from starting): the name is trimmed, at most 80 characters, without control characters; the email is trimmed and
lowercased; each phone (at most 5) has 6–15 digits and only spaces, a leading `+`, parentheses and hyphens; the
Discord URL is `https://discord.gg/<code>` or `https://discord.com/invite/<code>` without query, fragment,
credentials or port. No database, no session; the response models forbid any other field, so no setting can leak.

**There is no contact form.** The legacy `submit-contact` function was broken (its table never existed in the legacy
database) and is retired: there is no contact-submission API, and the frontend shows these details, the Discord invite
and the signup interest form below. V2's `contact_submissions` table (migration 0005) remains, unused.

## Signup interest form (backend completion G3)

`POST /api/v1/public/signups` replaces the legacy signup dialog's direct browser insert into `user_signups`.
No cookies; the request must come from a trusted frontend `Origin` (403 otherwise, checked before the body),
and each IP may submit 5 times per hour (`rate_limits`, bucket `signup:create`; 429 after that).

Body (JSON, no other fields): `name` (1–100), `email` (5–255, legacy pattern), `mobile_number` (8–20: digits,
spaces, `+ - ( )`), optional `youtube_channel` (≤ 500) and `instagram_handle` (≤ 100). Values are trimmed, the
email is lowercased, control characters are refused, and an empty optional field is stored as NULL. Invalid
bodies answer 422.

Response: `201 {"status": "received"}` with `Cache-Control: no-store`, **also for an email that already signed
up** (in any letter case): nothing is stored or overwritten, and the answer does not reveal whether the address
was known (legacy said "This email is already registered"; decision D14 in
[`backend-completion-checklist.md`](backend-completion-checklist.md)). A database failure answers
`503 {"error": "Signups are unavailable right now. Please try again later."}`. No email is sent; the security
log records rejections and failures by reason and IP, never the submitted values.
