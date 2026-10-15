# Realtime (Phase 9)

Live updates for the creator dashboard and the OBS overlays, through Pusher. This replaces the legacy
pattern "commit, then call Pusher inside the request" (no retries: a Pusher failure lost the event) with a
transactional outbox published by the worker.

```
request (router → service → repository)
  BEGIN
    domain change (payment success / moderation / settings / Phase 8 audio job completion)
    INSERT realtime_outbox (ids + small non-PII facts)        ← same cursor, same transaction
  COMMIT                                                      (Pusher is never called inside a request)
worker (hyperchat-worker): app.realtime.publisher, next to the job slots
  claim (per-creator FIFO, SKIP LOCKED, 30 s lease) → render from CURRENT state → POST Pusher → published
Pusher → OBS overlays (OBS token) · dashboard (session) · public goal bar
```

Code: `backend/app/realtime/` (`outbox`, `channels`, `pusher`, `payloads`, `publisher`, `auth`) and
`backend/app/routers/realtime.py`. Table: `public.realtime_outbox` (migration `0012`).

## Decisions (approved D1–D12)

| # | Decision |
|---|---|
| D1 | A dedicated `realtime_outbox` table (not the Phase 8 `jobs` table): realtime events need per-creator ordering, coalescing, a short shelf life and their own retention, and must not compete with paid audio jobs. The Phase 8 patterns (SKIP LOCKED, leases, fencing, `Backoff`, reason codes) are reused. |
| D2 | Rows store ids and small non-PII facts only (`subject_id`, `{action, previous_status}`); payloads are rendered from the current database state at publish time. Goal, leaderboard and settings events coalesce while pending. |
| D3 | Legacy channel names: `private-{slug}-{dashboard,alerts,audio,settings,leaderboard}`, public `{slug}-goal`. No presence or encrypted channels. |
| D4 | Six events (below). Not carried forward: `stats-update` (never emitted in legacy), `donation-approved` (duplicate of `donation-updated`), `new-audio-message` (never subscribed in legacy), alerts `new-donation`, the `pending` duplicate. `vaani-celebration` is **deferred to Phase 10** (alerts overlay), not dropped. |
| D5 | The only Phase 8 change: the `donation_audio` completion (and its fallback) writes `audio-ready` in the same transaction. |
| D6 | Pusher apps per `streamers.pusher_group`, groups 1–4 from the environment; an unconfigured group (and 5–10) uses group 1. One resolver for publishing and client config. |
| D7 | `HYPERCHAT_REALTIME_ENABLED=false` by default: events are still written (the transactional path always runs) and marked `skipped`. DEV is enabled only with a dedicated DEV Pusher app the owner creates, never the legacy credentials. |
| D8 | Channel auth: session + CSRF for the dashboard only; OBS token, exact scope, for overlay channels only (never the dashboard); admin allowed; unclaimed creators admin-only; 60/min per IP, 30/min per credential. OBS-token management endpoints come in Phase 10. |
| D9 | Per-creator FIFO; 6 attempts, 2–30 s backoff; events older than 10 minutes expire unsent; at-least-once delivery, `event_id` for deduplication. |
| D10 | Goal progress counts approved / auto-approved donations only (as `GET /public/streamers/{slug}/goal`). |
| D11 | Finished outbox rows are deleted 7 days after creation by the daily cleanup. |
| D12 | `GET /api/v1/realtime/config?streamer=` returns `{key, cluster, auth_endpoint}` for an **active** creator; unknown, `pending`, `rejected` and malformed creators get the identical 404 (2026-10-15); 503 while disabled. |

## Events

| Event | Channel | Written when (same transaction) | Coalesced |
|---|---|---|---|
| `new-donation` | `private-{slug}-dashboard` | a payment completes (`PaymentRepository.apply`, auto-approved or pending) | no |
| `donation-updated` | `private-{slug}-dashboard` | a dashboard moderation action changes something (approve, reject, hide/unhide, ban) | no |
| `goal-progress` | `{slug}-goal` (public) | approved totals change (approve, auto-approve, rejecting an approved donation); goal settings change | per creator |
| `leaderboard-updated` | `private-{slug}-leaderboard` | approved totals change; `leaderboard_widget_enabled` changes | per creator |
| `settings-updated` | `private-{slug}-settings` | an overlay display setting changes (`brand_color`, `alert_box_scale`, overlay ids, `overlay_adjust_mode`, `leaderboard_widget_enabled`, `leaderboard_bg_enabled`) | per creator |
| `audio-ready` | `private-{slug}-audio` | the Phase 8 `donation_audio` job resolved the donation's audio (URL written, or its own voice message / hypersound) while it is still paid + approved | no |

Nothing is written for unpaid or failed payments, soundboard library changes or settings not shown in
overlays (TTS, moderation mode, ...).

## Payload contracts (version `v: 1`)

Every payload carries `event_id` (a UUID: consumers drop duplicates) and `v`. Never in a payload: session,
CSRF, status or upload tokens; order, payment or provider ids; creator or user ids; emails; secrets; provider
responses; moderator identities.

- `new-donation` / `donation-updated`:
  `{event_id, v, action?, previous_status?, donation: {id, name, amount, currency, message, message_visible,
  moderation_status, is_hyperemote, voice_message_url, tts_audio_url, hypersound_url, media_url, media_type,
  created_at}}`. `message` is `null` while the message is hidden. The donation id is the key of the dashboard
  moderation API.
- `goal-progress` (public): `{event_id, v, active, name, target_amount_inr, progress_inr, brand_color,
  goal_overlay_id}`, exactly the public goal endpoint (every field but `active` is `null` while no goal is
  active). Only for `active` creators: nothing is published on the public channel of a `pending` or `rejected`
  creator.
- `leaderboard-updated`: `{event_id, v, enabled, top_donor: {name, total_amount} | null,
  latest: {name, amount, currency, created_at} | null}` (approved donations only; both null while the widget is
  off).
- `settings-updated`: `{event_id, v}` plus the overlay display settings listed above.
- `audio-ready`: `{event_id, v, donation_id}`.

A payload above 9 KB is replaced by `{event_id, v, donation_id?, truncated: true}`; the client refetches.

## Delivery

- **Claim:** up to 20 rows per cycle that are due (`pending`, `available_at <= now()`, or `publishing` with an
  expired lease) **and have no older open row of the same creator**. So there is at most one in flight per
  creator, in id order. Uses `FOR UPDATE SKIP LOCKED`; the lease is 30 s, fenced on `locked_by`.
- **Publish:** one signed `POST https://api-{cluster}.pusher.com/apps/{id}/events` per event (5 s timeout,
  no redirects, at most `HYPERCHAT_REALTIME_PUBLISH_RATE`/s per process).
- **Outcomes:**
  - success → `published`.
  - timeout, network, 429 (`Retry-After` honoured), 5xx → back to `pending` with backoff 2 s × 2ⁿ ±20 %
    (cap 30 s), up to 6 attempts → `failed`.
  - 400, 401, 403, 404, 413 → `failed` at once.
  - render skip (donation gone, unpaid, no longer approved, creator not public) → `skipped`.
  - no Pusher app for the creator → `skipped` (`pusher_not_configured`).
  - database error → nothing written; the lease expires and the row is reclaimed.
- **Expiry:** open rows older than 10 minutes become `expired` and are never sent.
- **Disabled:** every pending row becomes `skipped` (`realtime_disabled`).
- **Delivery is at least once:** a row published whose `published` write was lost is sent again with the
  same `event_id`.
- **Shutdown:** SIGTERM releases an in-flight row without counting the attempt.
- **The original transaction never depends on Pusher:** `emit` is a plain `INSERT` with the caller's cursor.

Ordering: per creator only (no global order is promised). Head-of-line blocking is bounded by the attempt cap and
the expiry, and rendering from current state means a late event never shows stale data.

## Channel authorization

`POST /api/v1/realtime/pusher/auth` with `socket_id` and `channel_name` (form, as pusher-js sends it, or JSON).
Exactly one credential:

| Credential | Channels | Checks |
|---|---|---|
| OBS token (`x-obs-token`, or `?token=` for the OBS browser source) | `alerts`, `audio`, `settings`, `leaderboard` of the token's own creator | Origin absent or allowlisted; token hash active, unrevoked, unexpired; channel exactly in the token's scope (`obs_tokens.pusher_channel_scope`, else the default scope); never `dashboard`, whatever a stored scope says |
| Session cookie | `dashboard` | Origin allowlisted + CSRF token (`x-csrf-token`); owner, linked account or admin (`app.authz`); unclaimed creators admin-only; inactive users refused |

Requests are refused when they carry both or neither credential, or have a malformed `socket_id` (`\d+.\d+`).
`presence-`, `private-encrypted-`, public and unknown channels are refused too. Channel names are parsed with
the suffix anchored at the end and `fullmatch` (no trailing newline).

Limits: 60/min per IP, 30/min per credential. The answer is `{"auth": "<key>:<HMAC-SHA256(secret,
socket_id:channel)>"}` with `Cache-Control: no-store`. Refusals are security events with a reason code: never the
token, cookie or CSRF value.

`GET /api/v1/realtime/config?streamer={slug}` returns `{key, cluster, auth_endpoint}`, cached publicly for
5 minutes, for active creators only: an unknown, `pending`, `rejected` or malformed creator gets the same 404
(no status oracle). OBS pages can instead use `GET /api/v1/obs/session`, which returns the same Pusher config for
the token's creator. Initial overlay state comes from the OBS-token endpoints `GET /api/v1/obs/overlay-config`
(= `settings-updated`) and `GET /api/v1/obs/leaderboard` (top donor; the live event also carries `latest`). The key and cluster are public by Pusher's design. Both endpoints answer 503 while realtime is
disabled.

## Phase 10 (OBS) contract

Implemented in Phase 10: see [`obs-audio.md`](obs-audio.md).

Phase 10 consumes:
- `private-{slug}-audio` `audio-ready {donation_id}`: an item is ready. The player then calls the Phase 10
  claim endpoint (token-authenticated) instead of OBS reloading a URL every few seconds.
- `private-{slug}-settings` `settings-updated`, `private-{slug}-leaderboard` `leaderboard-updated`,
  `{slug}-goal` `goal-progress`.
- the auth and config endpoints above, with OBS tokens.

Phase 10 adds:
- the OBS claim endpoint: the atomic queue pop, the v2 `claim_next_audio`, which writes `audio_played_at`.
- `audio-now-playing` on `private-{slug}-alerts`, written by the claim transaction through this outbox
  (migration: add the `alerts` channel and the event to the CHECK constraints). Proposed payload:
  `{event_id, v, donation: {id, name, amount, currency, message (only when visible), is_hyperemote, audio_url,
  voice_message_url, hypersound_url, media_url, media_type, created_at}}`.
- `vaani-celebration` (deferred from Phase 9, D4).
- OBS-token management, replay, the overlay pages.

## Configuration

`HYPERCHAT_REALTIME_ENABLED` (default `false`), `HYPERCHAT_PUSHER_APP_ID_{n}`, `HYPERCHAT_PUSHER_KEY_{n}`,
`HYPERCHAT_PUSHER_SECRET_{n}`, `HYPERCHAT_PUSHER_CLUSTER_{n}` (n = 1–4; a partly set or invalid group counts as
unconfigured and is reported by name at startup), and `HYPERCHAT_REALTIME_PUBLISH_RATE` (default 50).

Logs carry event ids, event names, channel suffixes, reason codes, attempts, group numbers and variable names
only. `httpx` request logging is capped at WARNING, because Pusher REST URLs carry auth query parameters.

**DEV:** use a separate Pusher app created for DEV. The legacy Pusher apps must never be configured on DEV:
the DEV database holds real creator slugs, so publishing there would reach real streamers' live overlays.
