# OBS audio playback (Phase 10)

Replaces the legacy `get-current-audio` Media Source polling with a token-authenticated claim endpoint, a small
browser-source audio player driven by realtime events, OBS-token management, replay and the `!vaani`
celebration. Code: `backend/app/obs/`, `backend/app/routers/obs.py`; migration `0013`.

```
approval / payment ──(tx)── donation_audio job ── worker ── audio URL + audio-ready ──► Pusher private-{slug}-audio
OBS browser source  …/obs/player#token=obs_…   (the token stays in the #fragment: never sent, never logged)
  GET  /api/v1/obs/session                     creator, Pusher config, poll interval
  Pusher private-{slug}-audio  (auth: /api/v1/realtime/pusher/auth with x-obs-token)
  POST /api/v1/obs/audio/claim                 on connect, audio-ready, item end/error, Retry-After, every 30 s idle
        tx: SKIP LOCKED pop + audio_played_at + outbox audio-now-playing  → 200 item | 204 (Retry-After)
  one reused <audio> element plays the item
Alerts overlay (frontend, Phase 13) ◄── audio-now-playing / vaani-celebration on private-{slug}-alerts
OBS Media Source (compatibility):  GET /api/v1/obs/audio/next?token=obs_…  → 302 to the audio file | 204
```

## Decisions (approved P10-D1..D15)

| # | Decision |
|---|---|
| D1 | Dashboard token management: create (shown once, inside the OBS URLs), list (never the token), revoke. |
| D2 | Rotation grace (legacy): creating a token revokes the creator's other tokens 60 s later; the validator accepts `revoked_at > now()`. |
| D3 | Default channel scope only (no custom scopes at creation). Migrated legacy tokens keep working (the ETL stored their hashes) but cannot be displayed. |
| D4 | An item counts as played when claimed (legacy). Replay is the recovery. |
| D5 | Only allow-listed objects of this environment's bucket are served (see below); anything else is marked played and skipped. No open redirect. |
| D6 | A hidden message is never shown (`message: null`) and never spoken (generated TTS → the silence object). |
| D7 | 24-hour queue window: an item scheduled more than 24 h ago is never claimed. |
| D8 | Media Source compatibility endpoint kept (302 / 204, exactly as legacy). |
| D9 | The minimal browser-source player ships with the API (`/obs/player`). |
| D10 | nginx logs `/api/v1/obs/` and `/api/v1/realtime/` without the query string (DEV site). |
| D11 | `audio-now-playing` expires after 30 s in the publisher (other events: 10 min). |
| D12 | The visual alert designs stay with the frontend (Phase 13); this phase defines the event contract. |
| D13 | `!vaani` celebration on every payment-completion path, for `HYPERCHAT_VAANI_CREATORS` (default `ankit,captain_vaani`); credited like any approved donation (goal, leaderboard); no audio. |
| D14 | Several OBS clients split the queue (legacy): use one player per creator. |
| D15 | Dashboard replay for paid + approved donations with audio, 30 per hour per creator; Telegram/Discord replay in Phase 11. |

## The queue

`POST /api/v1/obs/audio/claim` (and `GET /api/v1/obs/audio/next`), one transaction:

```sql
UPDATE donations SET audio_played_at = now() WHERE id = (
  SELECT id FROM donations
  WHERE streamer_id = <the token's creator> AND audio_played_at IS NULL AND payment_status = 'success'
    AND moderation_status IN ('approved', 'auto_approved')
    AND audio_scheduled_at <= now() AND audio_scheduled_at >= now() - interval '24 hours'
    AND (hypersound_url IS NOT NULL OR voice_message_url IS NOT NULL OR tts_audio_url IS NOT NULL)
  ORDER BY audio_scheduled_at, id LIMIT 1 FOR UPDATE SKIP LOCKED)
RETURNING …;                                -- index donations_audio_queue_idx
+ realtime_outbox 'audio-now-playing' {kind}  (same transaction)
```

- **Audio choice (legacy order):** the hypersound, else the voice message, else the TTS file.
- **Servable URLs:** only exactly one of these:
  - `<public base>/hypersounds/<one segment>.mp3`
  - `<public base>/voice-messages/<this creator's slug>_<ms>_<hex>.webm`
  - `<public base>/tts-audio/<this creator's id>/<this donation's id>.mp3`
  - `<public base>/system/silence.mp3`
- **Anything else is skipped:** other hosts (legacy R2, Supabase storage), other creators' objects, query strings,
  fragments, `..` and encoded separators. Up to 10 such items are skipped per claim.
- **Answer:**
  - `200 {donation_id, kind: hypersound|voice|tts|silence, audio_url, display}`. `display` is the `audio-now-playing` donation object.
  - `204` with `Retry-After` when the next item is due within 60 s (for example, the 10 s auto-approve delay).
- **Replay:** `POST /api/v1/dashboard/streamers/{streamer}/donations/{id}/replay`. It sets `audio_played_at = NULL` and
  `audio_scheduled_at = now()`, writes a `moderation_actions` row (`replay`, `dashboard`), and writes `donation-updated {action: replay}` and `audio-ready`, all in one transaction.

## Realtime events (added to the Phase 9 contract)

| Event | Channel | Written | Payload |
|---|---|---|---|
| `audio-now-playing` | `private-{slug}-alerts` | in the claim transaction | `{event_id, v, donation: {id, name, amount, currency, message (null when hidden), is_hyperemote, kind, audio_url, voice_message_url, hypersound_url, media_url, media_type, created_at}}` |
| `vaani-celebration` | `private-{slug}-alerts` | in the payment-completion transaction | `{event_id, v, donation_id, name, amount, currency, created_at}` |
| `audio-ready` | `private-{slug}-audio` | audio job completion (Phase 9) and replay | `{event_id, v, donation_id}` |

`audio-now-playing` is expired after 30 s (a late "now playing" is worse than none). Generated TTS URLs contain the
creator id (`tts-audio/<creator id>/…`, Phase 8 key layout); they are sent on the creator's private channels only.

## Overlay contract (for the frontend)

- **Alerts overlay:**
  - shows an alert on `audio-now-playing` and drops duplicates by `event_id` and `donation.id`
  - legacy durations by type: hyperemote 8 s, hypersound 10 s, voice/TTS 15 s, text 10 s
  - shows no message when it is null
  - shows the `!sfx` / `!testmusic` labels
  - plays the celebration on `vaani-celebration`
  - does not replay missed alerts after a reconnect
- **Settings, leaderboard and goal:** as in Phase 9 (`settings-updated`, `leaderboard-updated`, public `goal-progress`), with initial
  state from the OBS-token endpoints `GET /api/v1/obs/overlay-config` and `GET /api/v1/obs/leaderboard` (the token's
  creator) and, for the goal, the public `GET /api/v1/public/streamers/{slug}/goal`.

## OBS tokens

`/api/v1/dashboard/streamers/{streamer}/obs-tokens`: owner, linked account or admin; changes need Origin + CSRF.

- **`POST`:**
  - mints `obs_` + 64 hex characters and stores only the SHA-256
  - schedules the creator's other tokens for revocation in 60 s
  - returns the token once, inside `player_url` (`…/obs/player#token=…`) and `media_source_url` (`…/api/v1/obs/audio/next?token=…`)
  - limited to 10 per hour per creator
- **`GET`:** id, state (`active | revoking | revoked | expired | inactive`), times, usage count.
- **`DELETE /{id}`:** revokes immediately.

**OBS requests** (session, claim, next, channel auth) accept the token in the `x-obs-token` header, or in `?token=` (Media Source).
The `Origin` must be absent, allow-listed, or the API's own origin (the player is served by the API).

- **Limits:**
  - claim / next: 120 per minute per token
  - session: 30 per minute per token
  - replay: 30 per hour per creator

## Player

`/obs/player` is static HTML with `/obs/player.js` and `/obs/player.css`. It has no third-party code, a strict CSP
(`script-src 'self'`, `connect-src 'self' wss://*.pusher.com`, `media-src <public base>/`), and is `noindex`.

1. It reads the token from the fragment and immediately removes it from the address bar.
2. It loads the session.
3. It checks that audio can play (OBS allows autoplay; a normal browser tab shows one "Click to enable audio" button first).
4. It subscribes to `private-{slug}-audio`, then claims items one at a time (single-flight, never while playing) on:
   - connecting
   - `audio-ready`
   - an item ending or failing
   - `Retry-After`
   - every 30 s while idle

One reused `<audio>` element plays every item, with no decoding into memory. Without realtime the 30 s claim alone
keeps it working.

Compared with legacy:
- **Idle traffic:** legacy had OBS reload a Media Source every few seconds per creator (an HTTP request, an edge
  function and an RPC each time, plus scene-switcher setup). Now there is one indexed claim every 30 s, plus pushes.
- **Downloads:** each item is fetched once, and replays are usually served from the browser cache (objects are immutable).

## Failure and recovery

| Situation | Behaviour |
|---|---|
| Worker crash | Job and outbox leases are reclaimed. A claimed item is already played; replay brings it back. |
| API restart | Claims are atomic; the player retries. |
| OBS restart | The player claims on load and continues with the next item. |
| Pusher outage or missed `audio-ready` | The 30 s idle claim keeps audio playing. `audio-now-playing` expires after 30 s. |
| R2 failure or missing object | The `<audio>` error moves on after 3 s; the item counts as played. |
| Concurrent claims or OBS clients | `SKIP LOCKED` gives distinct items; never the same item twice. |
| Stale entries | Not claimed after 24 h. |

Moderator notifications at play time (legacy `notify-new-donations`) belong to Phase 11.
