# Creator dashboard API (Phase 5)

Cookie-authenticated endpoints for the creator dashboard: settings, donations (list, stats, CSV
export), moderation (queue + actions), Telegram/Discord moderators, and the terms-of-service gate.
Code: `backend/app/dashboard/` (models, validation, moderation state machine, CSV encoding,
repository, service) and `backend/app/routers/dashboard.py`.

- **Authentication:** the `__Host-hc_session` cookie (Phase 2). POST/PATCH/DELETE also need an
  allowlisted `Origin` and the session's `x-csrf-token` header.
- **Authorization:** every creator route uses `require_streamer_access` (Phase 3): the owner
  (`streamers.user_id`), a linked account (`users.streamer_id`) or an admin. A creator nobody has
  claimed is admin-only. `pending` and `rejected` creators keep their dashboard. `{streamer}` is the
  creator's UUID or slug.
- **Scoping:** every query filters by the creator's id, so a donation or moderator id that belongs
  to another creator (or is not a UUID) is a 404 and nothing is changed.
- **Caching:** every response under `/api/v1/dashboard/` and `/api/v1/me/terms` (success *and*
  error) has `Cache-Control: no-store`. Other APIs are unaffected.
- **Format:** snake_case JSON with fixed field sets; request bodies reject unknown fields (422) and
  use strict JSON types (`"true"`, `1` or `"3"` are not booleans/integers).
- **No side effects outside the database:** no Pusher, TTS/music/SFX, Telegram, Discord, OBS or
  replay calls. Overlays pick up setting changes through the public overlay config (30 s cache).
  Phase 9: moderation and overlay-setting changes also write realtime outbox rows in the same transaction;
  the worker publishes them to Pusher ([`realtime.md`](realtime.md)).

## Endpoints
Creator paths are `/api/v1/dashboard/streamers/{streamer}/…`.

| # | Endpoint | Success | Errors |
|---|---|---|---|
| D-R1 | `GET …/settings` | 200 `DashboardSettings` | 401, 403, 404 |
| D-M1 | `PATCH …/settings` | 200 `{settings, changed}` | 400 empty, 403 CSRF/access, 404, 422 |
| D-R2 | `GET …/donations?limit=&cursor=` | 200 `{donations, next_cursor, has_more}` | 400 cursor, 422 limit |
| D-R3 | `GET …/donations/stats` | 200 `DonationStats` | 401, 403, 404 |
| D-R4 | `GET …/donations/export.csv?from=&to=` | 200 `text/csv` (attachment, streamed) | 400 range / too many rows |
| D-R5 | `GET …/moderation/queue?status=&limit=&cursor=` | 200 `{donations, pending_count, next_cursor, has_more}` | 400 cursor, 422 status/limit |
| D-M2 | `POST …/donations/{donation_id}/moderation` | 200 `{result, action, previous_status, new_status, donation}` | 404, 409 unpaid, 422 |
| D-R8 | `GET …/moderation/banned-donors` | 200 `{banned_donors, total, truncated}` | – |
| D-M6 | `POST …/moderation/banned-donors/{ban_id}/unban` | 200 `{result, bans_lifted, ban}` | 403 CSRF, 404 |
| D-R6 | `GET …/moderators` | 200 `{moderators}` | 401, 403, 404 |
| D-M3 | `POST …/moderators` | 201 `Moderator` | 409 duplicate, 422 |
| D-M4 | `DELETE …/moderators/{moderator_id}` | 200 `{result, moderator}` | 404 |
| D-R7 | `GET /api/v1/me/terms` | 200 `{current_version, accepted, is_admin}` | 401 |
| D-M5 | `POST /api/v1/me/terms/accept` | 200 `{accepted, current_version}` | 403 CSRF, 409 version, 422 |

Errors use `{"error": "..."}` (FastAPI's `{"detail": [...]}` for 422): 401 `Not signed in`;
403 `Forbidden`, `This creator account is not claimed yet`, `Request origin not allowed`,
`Invalid CSRF token`; 404 `Streamer not found`, `Donation not found`, `Moderator not found`.

## G1 settings
**D-R1 fields:** `slug, name, status, moderation_mode, telegram_moderation_enabled,
discord_moderation_enabled, media_moderation_enabled, tts_enabled, tts_language_boost, el_voice_id,
hyperemotes_enabled, hyperemotes_min_amount, media_upload_enabled, media_min_amount, brand_color,
alert_box_scale, alert_overlay_id, leaderboard_overlay_id, goal_overlay_id, overlay_adjust_mode,
leaderboard_bg_enabled, leaderboard_widget_enabled, goal_name, goal_target_amount, goal_is_active,
goal_activated_at`. Nullable booleans and `moderation_mode` are served with their column default.
`status` is shown, `rejection_reason` never is.

**D-M1** accepts any subset of the 21 writable fields (the legacy `update-streamer-settings`
allowlist minus the two read-only amounts):

| Field | Rule |
|---|---|
| `moderation_mode` | `auto_approve` or `manual` |
| `telegram_moderation_enabled`, `discord_moderation_enabled`, `tts_enabled`, `hyperemotes_enabled`, `goal_is_active`, `media_upload_enabled`, `media_moderation_enabled`, `overlay_adjust_mode`, `leaderboard_bg_enabled`, `leaderboard_widget_enabled` | JSON boolean, not null |
| `alert_box_scale` | one of 0.75, 1.0, 1.25, 1.5 |
| `goal_name` | trimmed, 1–120 characters |
| `goal_target_amount` | number, > 0 and ≤ 100,000,000 |
| `goal_activated_at` | ISO 8601 timestamp **with** a timezone offset, or null |
| `tts_language_boost` | one of Hindi, English, Arabic, Chinese, French, German, Indonesian, Italian, Japanese, Korean, Portuguese, Russian, Spanish, Thai, Turkish, Vietnamese; case-insensitive, stored with this capitalisation (A2) |
| `alert_overlay_id` | integer 0–16 |
| `leaderboard_overlay_id` | integer 0–15 (the database trigger `validate_overlay_ids` is authoritative, A8) |
| `goal_overlay_id` | integer 0–6 |
| `brand_color` | `#RRGGBB` |
| `el_voice_id` | 10–64 letters/digits; `""` or null clears it (stored as NULL) |
| `hyperemotes_min_amount`, `media_min_amount` | **read-only** in Phase 5 (A1): sending them is a 422 |

An empty body is `400 {"error": "No settings to update"}`; one invalid or unknown field rejects the
whole patch. PostgreSQL compares the new values with the stored ones under a row lock
(`IS DISTINCT FROM`): only differing columns are written, `changed` lists them, and an unchanged
patch writes nothing (`updated_at` is untouched). Changes are logged as
`streamer_settings_updated` (user id, creator id, changed field names).

## G2 donations
- **D-R2 list:** paid (`payment_status = 'success'`) and approved or auto-approved donations, newest
  first. Fields: `donation_id, donor_name, amount, currency, amount_inr, message, message_visible,
  is_hyperemote, voice_message_url, tts_audio_url, hypersound_url, media_url, media_type,
  moderation_status, created_at`. Hidden messages are included (the creator moderates them).
  Keyset pagination on `(created_at DESC, id DESC)`: `limit` 1–100 (default 20); `next_cursor` is
  opaque and only valid as returned (a tampered cursor is `400 {"error": "Invalid cursor"}`).
- **D-R3 stats:** `total_revenue_inr, total_donations, today_revenue_inr, month_revenue_inr,
  timezone ("Asia/Kolkata"), as_of` over **every paid donation, any moderation status** (legacy
  semantics), summed from the stored `amount_inr`. Today/month are IST calendar buckets. The
  boundaries are computed in Python with the fixed +05:30 offset (Asia/Kolkata has had no DST since
  1945; the legacy dashboard did the same) and passed to SQL: an IST day starts at 18:30:00 UTC on
  the previous day.
- **D-R4 export:** `from` and `to` are `YYYY-MM-DD` IST dates, both included
  (`[from 00:00 IST, to + 1 day 00:00 IST)`), at most 366 days. Paid donations, any moderation
  status, newest first. Columns: `created_at` (ISO 8601 with `+05:30`), `donor_name`, `amount`,
  `currency`, `amount_inr` — no order ids (D6). More than 10,000 rows is
  `400 {"error": "More than 10000 donations in this range: choose a shorter range"}` — the file is
  never truncated (A4). Text cells starting with `=`, `+`, `-`, `@`, TAB or CR get a leading `'`
  (formula injection). Rows are fetched first (the database connection is released) and then
  streamed. File name: `<slug>-donations-<from>-<to>.csv`. Exports are logged as
  `donations_exported` with the row count.

  | Bad input | Response (400) |
  |---|---|
  | missing `from` / `to` | `from is required (YYYY-MM-DD)` / `to is required (YYYY-MM-DD)` |
  | malformed date | `from must be a date in YYYY-MM-DD format` |
  | `to` before `from` | `to must not be before from` |
  | more than 366 days | `The date range is limited to 366 days` |

## G3 moderation
**D-R5 queue:** paid donations by `status` = `pending` (default), `approved` (approved +
auto_approved), `rejected` or `all`; newest first, keyset cursor, `limit` 1–100 (default 50).
Rows are D-R2's fields plus `approved_at`, `approved_by_label` and `is_donor_banned` (an active ban
of the same name, compared with `lower()` — exact, no wildcards). `pending_count` counts paid
pending donations.

**D-M2** body `{"action": "approve" | "reject" | "hide_message" | "unhide_message" | "ban_donor",
"reason"?: ≤ 500 characters}`; `reason` is only used by `ban_donor` (ban reason and audit notes).
`unban_donor` (A3) and `replay` (Phase 10) are not actions here (422).

| Current \ action | approve | reject | ban_donor |
|---|---|---|---|
| pending | → approved; credit | → rejected; hide message | ban + → rejected; hide |
| approved | unchanged | → rejected; hide; **debit** | ban + → rejected; hide; debit |
| auto_approved | unchanged (A5) | → rejected; hide; **debit** | ban + → rejected; hide; debit |
| rejected | → approved; credit | unchanged | ban (status stays); unchanged if already banned |

`hide_message` / `unhide_message` only set `message_visible` (unchanged when already so). A ban is
only added when the donor has no active ban. Approve sets `approved_at` and `audio_scheduled_at` to
now, `approved_by_label` to the user's **username, or "Dashboard"** (never the email, A6), and
clears `approved_by_moderator_id`.

Each action is **one transaction**:
1. `SELECT … FOR UPDATE` the donation by id **and** creator id (otherwise 404);
2. it must be paid, otherwise `409 {"error": "Donation is not paid"}` and nothing is written;
3. the pure `transition()` decides (`backend/app/dashboard/moderation.py`); for `ban_donor` the
   donor's existing ban is checked under a per-creator, per-name advisory lock;
4. update the donation; 5. credit (upsert) or debit (floored at 0) `streamer_donator_totals` by
   `amount_inr`, keyed by `(streamer_id, lower(donor_name))`; 6. insert the ban;
7. insert `moderation_actions` (`action_source = 'dashboard'`, `moderator_name` = the label,
   `notes` = ban reason, `actor_user_id` = the signed-in user, migration 0009); 8. commit.

An action already in effect returns `"result": "unchanged"` and writes nothing — no audit row, no
totals change. Concurrent actions on one donation serialise on the row lock (two simultaneous
approvals credit once); any error rolls everything back.

**D-R8 ban list:** the creator's **active** bans, newest first, at most 500 (`truncated` says more exist; `total`
counts them all). Each ban: `ban_id`, `donor_name`, `banned_by_name`, `reason`, `is_active`, `created_at`,
`unbanned_at`, `unbanned_by_label`.

**D-M6 unban by id** (replaces legacy's per-donation `unban_donor`, which A3 keeps out of D-M2): in one
transaction, every **active** ban of that donor name (exact `lower()`, never `ILIKE`) for this creator becomes
inactive and records `unbanned_at`, `unbanned_by_label` (the actor label) and `unbanned_by_user_id` (migration
0016); the rows are kept as history. Legacy data can hold several active bans of one name, and leaving one active
would keep the donor banned, so all are lifted: `bans_lifted` says how many. Already lifted → `"result":
"unchanged"`, `bans_lifted: 0`. A ban of another creator (or a malformed id) is 404. It takes the same
per-creator, per-donor lock as `ban_donor`, so a ban racing an unban of one donor serialises. A security event
`donor_unbanned` records the user and creator (no donor name). The donation's moderation status is not changed
(legacy behaviour).

## G4 moderators
Moderators are Telegram/Discord identities used by the bots (Phase 11, [`bots.md`](bots.md)); they have no web access.
- **D-R6:** all moderators of the creator, active and removed, newest first: `moderator_id,
  mod_name, telegram_user_id, discord_user_id, role, can_approve, can_reject, can_hide_message,
  can_ban, can_replay, is_active, created_at, last_action_at`.
- **D-M3** `{mod_name, telegram_user_id?, discord_user_id?}` → 201. Compatibility-preserving rules
  (A7): the legacy dashboard and `manage-moderators` only trimmed the values and required a name and
  at least one id, so **no Telegram/Discord format is enforced** (dev data contains non-numeric
  ids). `mod_name` is trimmed, 1–100 characters; each id is an optional string, trimmed, at most 64
  characters, `""` meaning none; at least one id is required. The caps only bound input (the longest
  stored values are a 24-character name and 10/19-character ids). Existing rows are never
  re-validated or rewritten. Permissions keep their column defaults (editing them is D-M7, deferred).
- **Duplicates (409):** an active moderator of the same creator with the same Telegram id
  (`An active moderator with this Telegram ID already exists`, enforced by the existing
  `streamers_moderators_unique_active` index) or Discord id (`… Discord ID …`, checked under a
  per-creator advisory lock; there is no Discord index because existing data already holds a
  duplicate, which stays untouched). Removed moderators do not count.
- **D-M4:** soft removal (`is_active = false`): `"removed"`, then `"unchanged"` on repeat.
- Additions and removals are logged (`moderator_added`, `moderator_removed`).

## G5 terms
The current version is a server constant, `2026-07-03` (legacy `src/config/terms.ts`).
- **D-R7:** `{current_version, accepted, is_admin}`; admins are always `accepted` (legacy bypass).
  Only an acceptance of the current version counts.
- **D-M5** `{"version": "2026-07-03"}`: any other version is
  `409 {"error": "Terms version is not current"}`; repeat calls keep one row
  (`ON CONFLICT DO NOTHING`); the `User-Agent` header (≤ 500 characters) is stored, as legacy did.
- The gate stays client-side (A9): no other endpoint checks acceptance.

## Never returned
Owner/user ids and emails, password or session data, CSRF tokens, `rejection_reason`, Razorpay key
refs, `order_prefix`, `pusher_group`, `api_enabled`, `custom_page_path`, the Discord channel id,
OBS tokens, order ids (`order_id`, `provider_order_id`), `status_token_hash`, `upload_token_hash`,
`legacy_source_table`, `mod_notified`. The only internal ids served are `donation_id` and
`moderator_id` (needed to act on them, D7).

## Intentional differences from the legacy Edge Functions
| Topic | Legacy | v2 |
|---|---|---|
| Moderation implementation | `moderate-donation` (974 lines; dashboard + bot callers) | not ported: a pure state machine + one transaction (D3) |
| Bot moderation path | identified by a moderator id only (S1) | no bot access in Phase 5 (Phase 11) |
| Unpaid donations | could be approved/replayed and credited (S2) | 409 for every action |
| Cross-creator guard | skipped when the donation had no `streamer_id` (S3) | every query scoped by creator id |
| Totals on reject of `auto_approved` | not debited (S5) | debited |
| Approve on `auto_approved` | set `approved`, re-alerted | unchanged (A5) |
| Writes | several statements, errors ignored (S8) | one transaction |
| Audit identity | client-supplied moderator fields (S7) | session user (`actor_user_id`), label = username or "Dashboard" |
| Unban | per-donation action, `ILIKE` on the donor-chosen name (S6) | by ban id (D-M6), exact `lower()`; recorded on the ban row (0016) |
| Side effects | Pusher, TTS/music/SFX, Telegram notifications in the request | none (Phases 8, 9, 11) |
| Settings validation | some fields unchecked (S11); leaderboard overlay 0–16 | strict types for all 21 fields; leaderboard 0–15 (A8); minimum amounts read-only (A1) |
| Stats | all paid rows loaded and re-converted with fixed rates (S12) | one SQL aggregate over `amount_inr`; no average/top-donation fields |
| Export | no date limit, `order_id` included, silently capped at 10,000 | ≤ 366 days, no order ids (D6), 400 above 10,000 rows (A4), formula-safe |
| Pagination | page/offset with exact counts | keyset cursor, `has_more` |
| Terms version | chosen by the client (S14) | server constant; other versions 409 |
| Duplicate Telegram moderator | database error (500) | 409 |
| Responses | included creator `id`, `user_id`, `source_table` | fixed fields, no internal ids beyond D7 |
| Request format | POST with `streamerId`/`action` in the body | REST paths with the creator in the URL |

## Deferred (not in Phase 5)
Moderator permission editing (D-M7), (ban list and unban by id, D-R8/D-M6, were added in the backend completion
phase), OBS tokens and replay
(Phase 10), soundboard (Phase 6), Pusher broadcasts and the live feed (Phase 9), TTS/music/SFX on
approval (Phase 8), Telegram/Discord moderation and notifications (Phase 11), payouts (Phase 12),
Sarvam transcription (D14).

## Schema
Migration `0009_moderation_actor` adds `moderation_actions.actor_user_id uuid NULL REFERENCES
users(id) ON DELETE SET NULL` (no table rewrite; existing rows stay NULL; the foreign key is checked
once against the existing all-NULL rows and briefly locks `users` against writes; table grants from
0007 cover the column). No new index: every query uses an existing one (see `backend/app/dashboard/repository.py`).
