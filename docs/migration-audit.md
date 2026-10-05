> **Snapshot (2026-10-05).** Read-only audit of the legacy application (`Ankit-KK/hyperchat`) and of the
> restored production database, written before the v2 repository existed. Paths such as
> `D:\claude-code\...` and branch names refer to that time and to the legacy repository; the layout of
> this repository is described in [`../README.md`](../README.md) and the relationship in
> [`history.md`](history.md). Since then: Phase 1 (schema + ETL) and Phase 2 (FastAPI-managed auth)
> were completed, and the Supabase-specific transitional security code listed below was dropped from v2.

# HyperChat — full Supabase-exit migration audit & target architecture (read-only)

## Context
Direction changed: the goal is no longer a FastAPI layer in front of the Supabase-powered app, but a
**complete replacement**: New frontend → FastAPI → self-hosted PostgreSQL, with **no Supabase at runtime**
(no Supabase JS, Auth, Edge Functions, Realtime, Storage, PostgREST, pg_net/pg_cron dependency).
The existing frontend + Edge Functions are the **reference** for features, business rules and security
behaviour. The restored PostgreSQL copy (`restoretest`, 2026-10-04 dump) is the development source of truth.

Evidence: repo `Ankit-KK/hyperchat` (local clone `D:\claude-code\hyperchat`, commit `0afda4a` + backend branch),
three repository audits (67 Edge Functions, `src/`, migrations/config), production DB audit
(`hyperchat-migration\database\audit-raw.txt`) and new read-only checks on `restoretest` (this session).
Tags: **[C]** confirmed in repo/DB · **[P]** proposed · **[A]** assumption to verify.

This document is saved as `D:\claude-code\hyperchat-migration\docs\full-migration-audit.md`
(the only file written). No database, frontend, endpoint, deployment or credential changes.

Impact on work already done:
- Phase 1 skeleton (pushed) stays valid.
- Phase 2 (uncommitted, branch `backend/phase-1-skeleton`): **reusable** — HMAC verification, nonce store,
  upload grants, OBS tokens, partner/internal secrets, CORS, security logging, crypto, stores pattern.
  **Transitional only** — Supabase access-token verifier and the `supabase_user_id` session bridge
  (replaced by FastAPI-managed auth; keep only if a dual-run period is ever needed).

---

## A. Supabase dependency inventory  [C]
| Supabase component | Where used | Scale |
|---|---|---|
| **supabase-js client** (anon key hardcoded) | `src/integrations/supabase/client.ts`; anon key also in `authenticatedFetch.ts`, `pusherClient.ts`, `usePusherConfig.ts`, `public/obs/hyperchat-v4.html` | ~87 files call it |
| **Supabase Auth** | `src/contexts/AuthContext.tsx` (signInWithPassword, getSession, onAuthStateChange, signOut), `ForgotPassword.tsx` (resetPasswordForEmail → `/reset-password`), `ResetPassword.tsx` (updateUser); Edge `_shared/session.ts` (auth.getUser); `admin-update-email` (auth.admin.updateUserById); trigger `on_auth_user_created → handle_new_user` (inserts `public.profiles`); email templates `email-templates/*` | 146 users (140 email, 6 Google — Google sign-in not in UI, last used 2025-09-07) |
| **Edge Functions** | 67 functions (~15.7k lines) — §K | ~41 called by frontend |
| **PostgREST from browser** | `obs_tokens` update (`OBSTokenManager.tsx:356`), `user_signups` insert (`SignupDialog.tsx:153`), `<slug>_donations` select/update (`MediaSourcePlayer.tsx:71`, `useAudioPlayer.ts`), RPC `validate_obs_token_secure` | 4 call sites |
| **RLS** | 206 policies on 168 tables; 31 on storage.objects | §E |
| **Supabase roles** | anon / authenticated / service_role in grants, policies, 8 functions | — |
| **auth.* SQL helpers** | auth.uid()/auth.email() in 37 functions, auth.role() in 2+ policies, 10 public policies | — |
| **Storage** | 19 buckets, 473 objects; live code uses R2 instead; 16 DB rows still point to Supabase Storage | — |
| **Realtime** | Publication on 3 tables, **0 frontend subscriptions** (Pusher used instead) | unused |
| **pg_cron + pg_net** | 3 jobs: notify-new-donations (every min, HTTP→Edge), payment-health-check (6 h, HTTP→Edge), cleanup-order-lookup (daily SQL); pg_net also in unattached `notify_telegram_new_donation` | live, defined outside migrations |
| **Vault / pgsodium / pgjwt / graphql** | installed, **unused** by the app | — |
| **Supavisor pooler / project URL** | `*.supabase.co` in CSP (`vercel.json`), OBS media-source URLs (`/functions/v1/get-current-audio?token=`), partner docs, cron commands | external URLs embedded in OBS setups |
| **Supabase secrets store** | 30+ env names: Razorpay (global + per-creator names via `streamers."razorpay-keyid/keysecret"` — only **2 distinct key sets** across 133 creators), Pusher ×4 groups, R2, ElevenLabs, MiniMax, Sarvam, Telegram, Discord, Resend, REQUEST_SIGNING_SECRET, UPLOAD_GRANT_SECRET, INTERNAL_FUNCTIONS_SECRET, PAYOUT_ENCRYPTION_KEY, PAYMENT_HEALTH_CRON_SECRET | must move to the VPS secret store |

## B. Backend / API functionality inventory  [C]
- **Auth**: password login, logout, session restore/refresh, password-reset email + set new password, invite-only accounts (created manually in the Supabase dashboard + `auth_users` row, `docs/ADD_NEW_STREAMER.md` step 16), admin email change, terms gate, per-session request-signing key.
- **Creator dashboard**: creator list (owned/admin), settings (pricing tiers, TTS/voice, moderation mode, overlays, goal, leaderboard, branding), donation list/stats/CSV export, moderation queue + approve/reject/hide/ban/replay, moderators (Telegram/Discord IDs), OBS token create/list/revoke, payout bank/UPI details (AES-GCM encrypted), soundboard (platform + custom slots, R2 presigned upload, T&C), voice transcription (Sarvam).
- **Public donation flow**: pricing, HyperSound/soundboard lists, upload grant → voice/media upload to R2, Razorpay order (per-creator keys, pending row, status/upload tokens), status page polling, payment health gate.
- **Payment completion**: Razorpay webhook (HMAC + server-side payment fetch + amount check + idempotency), status-check path; donor totals; auto-approve rules (triggers); TTS/music/SFX generation (ElevenLabs, MiniMax) → R2; Pusher events; moderator notifications (Telegram/Discord).
- **OBS overlays**: alerts, goal, leaderboard, media-audio player; OBS token auth; `get-current-audio` = atomic queue pop (`claim_next_audio`, SKIP LOCKED) + 302 to R2 + `audio-now-playing` event; overlay config; Pusher private-channel auth.
- **Moderation bots**: Telegram webhook (commands + buttons), Discord interactions (Ed25519), callback mapping tables, notify job.
- **Platform**: metrics (platform/showcase), partner API (signup/check, hashed keys, rate limit), signups (`user_signups`), contact form (broken), rate limiting (`check_rate_limit_v2`, `check_server_rate_limit`), request nonces, TTS moderation engine (`_shared/moderation`), security audit logs.
- **Jobs**: notify new donations (1 min), payment health (6 h), order_lookup cleanup (daily).

## C. Proposed FastAPI architecture  [P]
- **Modular monolith** in `backend/app/`: `api/v1/<area>` routers (thin) → `services/` (business rules, ported from Edge code) → `repositories/` (SQL via psycopg 3, `psycopg.sql.Identifier` for the per-creator donation tables, resolved through `streamers.donation_table_id` / `resolve_streamer`) → PostgreSQL.
- `integrations/`: razorpay, pusher (REST + auth signing), r2 (S3 API), elevenlabs, minimax, sarvam, telegram, discord, email (Resend). Each with timeouts, retries, circuit-breaker logging, no secrets in logs.
- `security/`: Phase 2 modules + new auth (sessions, passwords, CSRF), authorization policies.
- `jobs/`: PostgreSQL-backed job queue (table + `FOR UPDATE SKIP LOCKED`) for TTS/music/SFX, notifications, Pusher outbox; scheduler (systemd timers or worker loop) for health check + cleanup. Separate `hyperchat-worker.service`.
- **Transactional outbox** for realtime and notifications: business transaction writes `outbox` rows; worker publishes to Pusher/Telegram/Discord with retries (today they are fire-and-forget `fetch` calls).
- Schema management: **Alembic** with a baseline generated from the restored schema; SQL-first migrations.
- Config via `HYPERCHAT_*` env (Phase 1), secrets in `/etc/hyperchat/*.env` (0640) — later optionally a secrets manager.
- OpenAPI is the contract for the new frontend (typed client generation).

## D. Proposed new frontend architecture  [P]
- React + TypeScript + Vite SPA (team already uses it; existing components are the UI reference), TanStack Query, React Router with **generic routes** (`/:slug`, `/dashboard/:slug`, `/:slug/obs-alerts`, `/:slug/goal-overlay`, `/:slug/media-audio-player`) replacing 697 hand-written routes and ~68 copy-paste donation pages; creator theming from API data instead of `src/config/streamers.ts`.
- Typed API client generated from FastAPI OpenAPI (`openapi-typescript`); no Supabase JS; cookie session + CSRF header.
- OBS overlays as lightweight routes; **must keep existing OBS URL paths and `?token=` parameter** so streamers' browser sources keep working.
- Realtime via `pusher-js` with FastAPI `authEndpoint`.
- Razorpay Checkout script (unchanged); R2 direct upload via presigned URLs.
- Hosting decision open: Vercel (as today) or Nginx on the VPS; **same registrable domain as the API** is required for first-party cookies (see F).

## E. Database access / grant requirements  [P unless tagged]
- **New production cluster** on the VPS (not `restoretest`), PostgreSQL 15, localhost/Unix socket only; nightly `pg_dump` + WAL archiving (PITR) to off-machine storage.
- Roles: `hyperchat_owner` (owns schema, used by Alembic only), `hyperchat_app` (API: DML on app tables, EXECUTE on kept functions, no DDL, no BYPASSRLS), `hyperchat_worker` (jobs: narrower DML), `hyperchat_readonly` (reporting/backup checks). Supabase roles `anon/authenticated/service_role` and their grants removed at the end.
- Extensions needed **[C]**: `pgcrypto`, `uuid-ossp` only (pg_cron/pg_net/vault/pgsodium/pgjwt/http not needed).
- Keep (pure SQL business logic, used by Edge code) **[C]**: `resolve_streamer`, `claim_next_audio`, `increment/decrement_donator_total`, soundboard RPCs (`apply_soundboard_changes`, `restore_soundboard_defaults`, `seed_streamer_soundboard`, `swap_soundboard_slot`, `get_soundboard_budget`), terms RPCs, rate-limit RPCs, `get_platform_metrics`, `compute_showcase_metrics`, `validate_obs_token_secure`, `purge_expired_request_nonces`, `log_security_violation`; the **18 table triggers**.
- Rewrite or drop: 41 functions using `auth.uid()/auth.email()`/Supabase role names; `validate_session_token` & other legacy custom-auth functions; 116 functions not referenced by app code/policies/triggers/views **[C]** (some are called by other functions, e.g. `is_admin_email` by 17 **[C]**) → drop list verified function-by-function.
- RLS: today it protects browser access only. Target: authorization in FastAPI; Supabase-role policies dropped; optional defense-in-depth RLS keyed on a per-request `app.user_id` GUC **[decision]**.
- Data-model decisions: consolidate 133 `<slug>_donations` tables (only **3 distinct column shapes**, 28–34 cols **[C]**) into one `donations` table with `streamer_id` — recommended before the new frontend relies on it **[decision]**; `profiles` (156 rows, only written by the Supabase trigger, unused by code **[C]**) → drop or merge into users.

## F. Authentication architecture  [P]
- Users = existing `public.auth_users` (id, email, password_hash, role, streamer_id, is_active, failed_login_attempts, locked_until **[C]**), extended with email_verified, password_changed_at, last_login_at.
- Passwords: Argon2id for new hashes; bcrypt verified and re-hashed on login.
- **Credential migration [decision]**: current passwords live in Supabase `auth.users` (not in the dump). `auth_users` holds 116 bcrypt hashes from the old custom login (likely stale — users reset when moving to Supabase Auth **[A]**) and 21 placeholders. Options: (1) one-time read-only export of `auth.users.encrypted_password` (bcrypt) at cutover → users keep passwords; (2) force password reset for all 137 accounts.
- Sessions: opaque 256-bit tokens, SHA-256 stored server-side (reuse/replace `auth_sessions`: token, expires_at, absolute_expires_at, csrf_token_hash, ua_hash **[C]**), httpOnly + Secure + SameSite=Lax cookie, idle + absolute expiry, rotation on login/privilege change, revoke-all on password change.
- CSRF: per-session token in header for state-changing requests + strict Origin check. HMAC request signing (Phase 2) becomes optional **[decision]**.
- **Domain requirement**: API must be on the same site as the frontend (e.g. `api.hyperchat.space` with `hyperchat.space`); `api.quicktestspace.site` is fine for development only.
- Password reset: `password_reset_tokens` (hash, expiry, used_at **[C]**) + email via Resend (existing provider **[C]**), templates from `email-templates/`.
- Invite-only onboarding: admin creates account + invite link (replaces manual Supabase dashboard step). Login rate limit + lockout; optional TOTP MFA for admins. Google OAuth: drop unless required **[decision]**.

## G. Authorization architecture  [P]
- Roles: `admin` (admin_emails today **[C]** → explicit role/permission table), `streamer` (owner via `streamers.user_id` or `auth_users.streamer_id` **[C]**), moderators (bot identities, not web users **[C]**).
- FastAPI dependencies: `require_user`, `require_admin`, `require_streamer_access(slug)` (Phase 2 port), `require_obs_token(scope)`, `require_partner_key`, webhook verifiers.
- OBS tokens: keep scopes/expiry/revocation **[C]**; **hash tokens at rest** (plaintext today **[C]**).
- Every rule from the 206 policies is re-expressed in services/tests (policy → endpoint mapping test suite).

## H. Webhook / HMAC architecture  [P on top of C]
| Mechanism | Today | Target |
|---|---|---|
| Razorpay webhook | HMAC-SHA256 raw body, `!==` compare; payment fetch; amount check; idempotent update **[C]** | constant-time; event-id idempotency table; same verification chain |
| Razorpay checkout signature | **missing** (status check polls instead) **[C]** | verify `order_id|payment_id` HMAC on return |
| Telegram webhook | **no secret check** **[C]** | `X-Telegram-Bot-Api-Secret-Token`; signed callback data |
| Discord interactions | Ed25519 **[C]** | port as-is |
| Moderator actions from bots | trusts `moderatorTelegramId` only **[C]** | only via verified webhook context |
| Pusher | REST signing + private-channel auth HMAC **[C]** | port; auth endpoint in FastAPI |
| Upload grants / request HMAC / partner keys / internal secret | **[C]** (ported in Phase 2) | keep; internal secret disappears (in-process calls) |
| Payout data | AES-GCM with PAYOUT_ENCRYPTION_KEY **[C]** | same algorithm + key migration; key rotation plan |

## I. Realtime replacement  [P]
Supabase Realtime is not used **[C]**; Pusher stays (4 apps = `pusher_group` 1–4 **[C]**). FastAPI publishes via the outbox worker with the same channels/events initially (`{slug}-alerts|audio|settings|leaderboard|dashboard` private, `{slug}-goal` public **[C]**), then a cleaned event contract for the new frontend. Fix known mismatches (`stats-update` vs `stats-updated` **[C]**; goal channel public vs private auth **[C]**). Drop the `supabase_realtime` publication.

## J. Storage replacement  [P]
R2 stays (all live media **[C]**). FastAPI: presigned PUT (size/type-bound) for donor voice/media and soundboard, HEAD-confirm, key layout unchanged (`voice-messages/`, `donation-media/`, `tts-audio/`, `soundboards/`, `hypersounds/` **[C]**). Serve via custom domain (e.g. `media.hyperchat.space`) instead of `r2.dev` **[decision]**. Supabase Storage: copy 473 objects to R2, rewrite the 16 DB URLs **[C]**, then delete buckets.

## K. Edge Function migration plan (67)  [C disposition basis]
- **Port as API endpoints (≈44)**: dashboard (get-user-streamers, get-streamer-for-owner, update-streamer-settings, broadcast-settings-update, get-dashboard-donations, get-goal-progress, generate-obs-token, list-obs-tokens, get-moderation-queue, moderate-donation, manage-moderators, terms-acceptance, get-payout-status, submit-payout-account, transcribe-voice-sarvam, soundboard ×8), public (get-streamer-pricing, list-hypersounds, list-streamer-soundboard, get-overlay-config, get-leaderboard-data, get-goal-progress, get-platform-metrics, get-showcase-metrics, get-discord-link, issue-upload-grant, upload-voice-message-direct, upload-donation-media, create-razorpay-order-unified, check-payment-status-unified), OBS (get-current-audio, get-pusher-config, pusher-auth), partner (partner-signup, partner-check-streamer), admin (generate-partner-key, admin-update-email, seed-platform-soundboard).
- **Webhooks (3)**: razorpay-webhook, telegram-webhook, discord-webhook.
- **Jobs/internal services (5)**: notify-new-donations, payment-health-check, generate-donation-tts, -music, -sfx.
- **Replaced by FastAPI auth (2)**: get-signing-key (only if HMAC signing kept), get-user-streamers' session part.
- **Admin CLI scripts (2)**: setup-telegram-webhook, setup-discord-webhook.
- **Drop (≈11)**: authenticate-user, auth-migration-link/-verify (410 stubs), request-password-reset/validate-reset-token/reset-password (legacy; rebuilt in F), create-razorpay-order-looteriya-gaming, get-dashboard-stats-looteriya-gaming, get-telegram-settings/manage-telegram-user (dead UI, insecure), get-telegram-user, get-video-url, submit-contact (table missing).

## L. New frontend → FastAPI API structure  [P]
`/api/v1/auth/{login,logout,session,password/forgot,password/reset,invite/accept}` ·
`/api/v1/me` · `/api/v1/public/streamers/{slug}` (profile, pricing, soundboard, hypersounds, leaderboard, goal) ·
`/api/v1/uploads/{grant,voice,media,presign}` · `/api/v1/donations` (create order) · `/api/v1/donations/{order_id}/status` ·
`/api/v1/overlays/{slug}/{config,leaderboard,goal}` (OBS token) · `/api/v1/obs/audio/next` (media source) ·
`/api/v1/realtime/pusher/auth` · `/api/v1/dashboard/streamers/{slug}/{settings,donations,donations/export,moderation,moderation/actions,moderators,obs-tokens,goal,soundboard/*,payout,terms,transcribe}` ·
`/api/v1/admin/{users,streamers,partners,soundboard/seed}` · `/api/v1/partner/{signup,streamers/check}` ·
`/api/v1/webhooks/{razorpay,telegram,discord}` · `/api/v1/metrics/{platform,showcase}` · `/health`.

## Migration matrix (summary; full table in Appendix 3)
| Existing functionality | Current Supabase mechanism | New FastAPI implementation | New frontend requirement | PostgreSQL dependency |
|---|---|---|---|---|
| Login / logout / session | Supabase Auth signInWithPassword, onAuthStateChange | `/auth/login|logout|session`, server sessions, Argon2id/bcrypt | login page, cookie session, CSRF header | auth_users, sessions (auth_sessions) |
| Password reset | resetPasswordForEmail + updateUser, Supabase email | reset tokens + Resend email | forgot/reset pages | password_reset_tokens |
| Account creation | manual Supabase dashboard + SQL | admin invite flow | invite accept page | auth_users, streamers.user_id |
| Creator access / admin | Edge `assertStreamerAccess`, RLS owns_streamer | `require_streamer_access`, roles | role-aware UI | streamers, auth_users, admin_emails→roles |
| Dashboard settings | update-streamer-settings + broadcast | `/dashboard/.../settings` + outbox → Pusher | settings forms | streamers (50 cols) |
| Donations list / export | get-dashboard-donations | repository over donation table(s) | table, filters, CSV | `<slug>_donations` (or consolidated) |
| Moderation | moderate-donation (+ missing RPC) | moderation service + audit | moderation panel | donation tables, moderation_actions, banned_donors |
| Moderators / bots | manage-moderators, telegram/discord webhooks | moderators API + verified webhooks | moderator manager | streamers_moderators, *_callback_mapping |
| OBS tokens | generate/list-obs-tokens, browser update via PostgREST | `/dashboard/.../obs-tokens` (hashed) | token manager | obs_tokens |
| Donation checkout | create-razorpay-order-unified | `/donations` + Razorpay integration | donation page per slug | order_lookup, donation tables, streamer_payment_health |
| Payment confirmation | razorpay-webhook, check-payment-status | webhook + status endpoint + signature verify | status page | same + streamer_donator_totals |
| Voice / media uploads | issue-upload-grant + upload-* (R2) | presigned upload + grant | recorder/uploader | request_nonces |
| TTS / music / SFX | generate-donation-* (internal HTTP) | worker jobs | — | donation tables, tts_moderation_audit |
| OBS audio queue | get-current-audio + claim_next_audio | `/obs/audio/next` | media-source URL | claim_next_audio, donation tables |
| Overlays realtime | Pusher (Edge publish), pusher-auth | outbox publisher + `/realtime/pusher/auth` | pusher-js overlays | outbox (new) |
| Leaderboard / goal | get-leaderboard-data, get-goal-progress | public overlay endpoints | widgets | streamer_donator_totals, streamers |
| Soundboard | 8 functions + RPCs + R2 presign | soundboard service | soundboard UI | soundboard tables + RPCs |
| Payouts | submit-payout-account (AES-GCM) | payout service (same crypto) | payout form | payout_accounts |
| Partner API | partner-* with hashed keys | `/partner/*` | — | partner_api_keys, partner_signups |
| Signup interest form | browser insert into user_signups (RLS) | `/public/signups` | signup dialog | user_signups |
| Metrics | get-platform/showcase-metrics RPCs | `/metrics/*` | landing widgets | RPCs |
| Scheduled jobs | pg_cron + pg_net → Edge | worker + systemd timers | — | order_lookup, streamer_payment_health |
| Media storage | R2 (+ 473 legacy Supabase objects) | R2 integration | media URLs | URL columns |

## Functionality missing or broken today (must be (re)built)  [C unless tagged]
FastAPI login/sessions/reset/invites (Supabase Auth provides them now) · RPC `increment_total_actions` called by moderate-donation **does not exist** · `contact_submissions` table missing (submit-contact) · Razorpay checkout signature verification · Telegram webhook auth, bot moderator auth, notify-new-donations auth · retries/queue for TTS & notifications · realtime outbox · OBS token hashing · browser-side PostgREST calls on legacy audio-player routes and OBS token deactivation are likely blocked by RLS (non-admins) **[A]** · `handle_new_user`/`profiles` and two cron jobs exist only in prod, not in the repo · 10 tables not referenced by app code (may be used by DB functions) **[A]**.

## M. Migration phases & dependencies  [P]
0 Decisions (credentials, donation-table consolidation, domain, HMAC vs CSRF, hosting) →
1 Platform: production PG cluster + roles + Alembic baseline + backups (needs 0) →
2 Auth (needs 1) → 3 Authorization/security (reuse Phase 2 code; needs 2) →
4 Public read APIs → 5 Dashboard APIs → 6 Uploads/R2 → 7 Payments + webhooks → 8 Jobs (TTS/notify/health/cleanup) → 9 Realtime outbox + Pusher auth → 10 OBS endpoints → 11 Bots → 12 Admin/payouts/partners (4–12 each depend on 3) ·
13 New frontend (starts with 4, feature-by-feature against OpenAPI) ·
14 Cutover rehearsal: fresh `pg_dump` of public (+ credential export if chosen), Storage copy, data validation, freeze window, re-point Razorpay/Telegram/Discord webhooks, streamer comms for OBS media-source URL change, DNS switch ·
15 Decommission Supabase + rotate every secret.

## N. Security risks  [C → P mitigation]
Known Edge weaknesses (bot moderation spoofing, Telegram webhook, unauthenticated notify/telegram-settings/setup endpoints, `!==` compares) · service_role JWT hardcoded in 3 migrations (valid to 2035) · plaintext OBS tokens · payout encryption key handling during migration · password-hash export handling (if chosen) · cross-site cookie pitfalls (domain) · OBS media-source URLs on `*.supabase.co` will break at decommission · dual systems during cutover (double notifications/webhooks) · production data copies on the VPS (restoretest) · secret sprawl (30+ secrets) needing rotation after exit.

## O. Files eventually created / modified  [P]
- `backend/app/{api/v1/*, services/*, repositories/*, integrations/*, security/* (+ auth, sessions, passwords, csrf, policies), jobs/*, schemas/*}`, `backend/migrations/` (Alembic), `backend/tests/{unit,integration,contract}/`, `backend/deploy/{systemd/hyperchat-worker.service, timers, nginx}`.
- New `frontend/` app (generic routes, generated API client) — existing `src/` and `supabase/` untouched until decommission, then removed.
- `vercel.json`/hosting config for the new frontend; DB roles/backups scripts under `backend/deploy/`.

## Verification of this audit
Each [C] item traced to repo file paths or `restoretest`/prod-audit queries (counts and schema only; no row
data, no secrets). [A] items listed for verification before implementation.

---

# Appendix 1 — Every Edge Function (67)  [C]
Auth legend: SESSION = Supabase JWT → auth_users; OWN = assertStreamerAccess; HMAC = signed request + nonce;
CSRF = legacy CSRF; ADMIN = admin_emails; nearly all use service-role DB access. Target: E = endpoint,
W = webhook, J = job/internal, CLI = admin script, X = drop.

| Function | What it does | Called by | Auth today | Target |
|---|---|---|---|---|
| admin-update-email | Change a user's email in Supabase Auth + auth_users | admin (manual) | SESSION+ADMIN | E admin |
| auth-migration-link | Retired (410) | — | none | X |
| auth-migration-verify | Retired (410) | — | none | X |
| authenticate-user | Retired custom login (410) | frontend refs | none | X |
| broadcast-settings-update | Push overlay settings via Pusher | dashboard | Origin+HMAC+SESSION+OWN | E (+outbox) |
| check-payment-status-unified | Poll Razorpay order, mark success, events, start TTS/music/SFX | status page | status token (skipped for old rows) | E |
| create-razorpay-order-looteriya-gaming | Legacy single-creator order | none | rate limit | X |
| create-razorpay-order-unified | Razorpay order + pending donation, status/upload tokens | 71 donation pages | public | E |
| delete-soundboard-slot | Delete custom slot | dashboard | SESSION+OWN | E |
| discord-webhook | Discord buttons → moderation | Discord | Ed25519 | W |
| generate-donation-music | `!testmusic` ElevenLabs music → R2 | internal | internal secret | J |
| generate-donation-sfx | `!sfx` ElevenLabs SFX → R2 | internal | internal secret | J |
| generate-donation-tts | MiniMax/ElevenLabs TTS → R2 + row update | internal | internal secret | J |
| generate-obs-token | Mint OBS token (CSPRNG) | dashboard | HMAC+SESSION+OWN | E |
| generate-partner-key | Create/list/revoke partner keys | admin | SESSION+ADMIN | E admin |
| get-current-audio | Pop next audio (claim_next_audio), Pusher event, 302 to R2 | OBS media source | OBS token | E (streamers must update URL) |
| get-dashboard-donations | Donation list/stats/export | dashboard | SESSION+OWN | E |
| get-dashboard-stats-looteriya-gaming | Legacy stats (likely broken) | none | raw auth_sessions token | X |
| get-discord-link | Discord invite URL | signup dialog | none | E (config) |
| get-goal-progress | Goal progress | overlay + dashboard | optional SESSION+OWN | E |
| get-leaderboard-data | Top donors / latest feed | overlay | optional OBS token | E |
| get-moderation-queue | Banned donors + queue | dashboard | SESSION+OWN | E |
| get-overlay-config | Public overlay display settings | overlays | none | E |
| get-payout-status | Payout details submitted? | dashboard | HMAC+SESSION | E |
| get-platform-metrics | Public metrics (RPC) | landing | none | E |
| get-pusher-config | Pusher key/cluster/auth endpoint | overlays, dashboard | optional OBS/SESSION | E |
| get-showcase-metrics | Public metrics (RPC) | landing | none | E |
| get-signing-key | Per-session HMAC key | dashboard | SESSION | E only if HMAC kept |
| get-soundboard | Creator soundboard state | dashboard | SESSION+owner/ADMIN | E |
| get-streamer-for-owner | One streamers row for owner | dashboard | SESSION+OWN | E |
| get-streamer-pricing | Minimums + voice tiers | donation pages | none | E |
| get-telegram-settings | Lists moderator Telegram IDs (no auth) | dead UI | none | X |
| get-telegram-user | Telegram getChatMember | none | none | X |
| get-user-streamers | Creators the user may manage | dashboard | SESSION(+ADMIN) | E |
| get-video-url | Returns a configured URL | none | none | X |
| issue-upload-grant | Single-use upload grant | donation pages | IP rate limit | E |
| list-hypersounds | List R2 hypersounds | donation pages | none | E |
| list-obs-tokens | List OBS tokens | dashboard | SESSION+OWN | E |
| list-streamer-soundboard | Public active soundboard | donation pages | none | E |
| manage-moderators | Moderator CRUD | dashboard | SESSION+CSRF+HMAC+OWN | E |
| manage-telegram-user | Add/remove Telegram moderators | dead UI | SESSION+owner/ADMIN | X (merged into moderators) |
| moderate-donation | approve/reject/hide/unhide/ban/unban/replay | dashboard, bots | dashboard: full; bots: moderator ID only | E + W (fix bot auth, missing RPC) |
| notify-new-donations | Telegram/Discord notifications (scan or single) | pg_cron, webhook, get-current-audio | none | J |
| partner-check-streamer | Partner: creator onboarded? | partners | x-api-key | E partner |
| partner-signup | Partner creator signup | partners | x-api-key | E partner |
| payment-health-check | Probe each creator's Razorpay keys | pg_cron | optional cron secret | J |
| pusher-auth | Pusher private-channel auth | pusher-js | OBS token+scope or SESSION+owner/admin | E |
| razorpay-webhook | payment.captured / payment.failed | Razorpay | HMAC-SHA256 | W |
| request-password-reset | Legacy reset email (Resend) | none | rate limits | X (rebuilt in auth) |
| reset-password | Legacy reset into auth_users | frontend refs | reset token | X (rebuilt) |
| restore-soundboard-defaults | Restore defaults (RPC) | dashboard | SESSION+OWN | E |
| save-soundboard-changes | Apply changes (RPC) | dashboard | SESSION+OWN | E |
| seed-platform-soundboard | Seed platform slots from R2 | admin | SESSION+ADMIN | E admin / CLI |
| setup-discord-webhook | Set Discord interactions URL (no auth) | manual | none | CLI |
| setup-telegram-webhook | Telegram setWebhook (no secret, no auth) | manual | none | CLI |
| soundboard-confirm-upload | HEAD R2 object, validate, write slot | dashboard | SESSION+OWN | E |
| soundboard-tnc | Soundboard T&C status/accept | dashboard | SESSION+OWN | E |
| soundboard-upload-url | Presigned R2 PUT (300 s) | dashboard | SESSION+OWN | E |
| submit-contact | Contact form (table missing) | none | rate limit | X (or rebuild with a table) |
| submit-payout-account | Bank/UPI/PAN, AES-GCM encrypted | dashboard | Origin+CSRF+HMAC+SESSION+OWN | E |
| telegram-webhook | Bot commands + buttons → moderation | Telegram | none | W (add secret token) |
| terms-acceptance | Terms gate | dashboard | SESSION(+ADMIN) | E |
| transcribe-voice-sarvam | Voice speech-to-text/translate (Sarvam) | dashboard | Origin+SESSION+owner/ADMIN | E |
| update-streamer-settings | Settings update | dashboard | Origin+CSRF+HMAC+SESSION+OWN | E |
| upload-donation-media | Donor image/video → R2 | donation pages | grant or order + upload token | E (presigned) |
| upload-voice-message-direct | Donor voice → R2 | donation pages | grant or order + upload token | E (presigned) |
| validate-reset-token | Legacy reset token check | none | token hash | X |

Shared code to port [C]: cors, session, streamerAccess, hmac, signingKey, csrf (legacy), uploadGrant, razorpay,
payoutCrypto, moderation engine (deterministic TTS text filter + probe limiting, audit in tts_moderation_audit),
sanitize, hypeLines, musicCommand, sfxCommand.

# Appendix 2 — RLS policies and the behaviour they provide  [C]
| Class | Count | Behaviour today | Target |
|---|---|---|---|
| service_role USING/WITH CHECK true | 162 | Lets Edge Functions read/write; browser roles blocked by omission | Drop; FastAPI role gets explicit table grants |
| Always-false denies for anon/authenticated | 15 (api_keys, auth_sessions, auth_users, password_reset_tokens [restrictive], rate_limits, system_alerts, user_signups ×3) | Explicitly blocks browser access to sensitive tables | Drop (no browser DB access) |
| Pure SQL public reads / validated inserts | 10 (platform_voice_tiers active, streamer_voice_tiers, reviews ×3, terms_acceptances ×2, moderation_actions for moderators, user_signups insert validation, visits insert validation) | Public read of tiers/reviews; validated anonymous inserts | Endpoint validation (Pydantic); fix reviews (approval not enforced) and terms_acceptances (read-all/insert-any) |
| Direct auth.* | 10 (admin_emails ×4, profiles ×2, user_queries ×2, godspree/niklaus service_role_all via auth.role()) | Self-service row access by Supabase identity | FastAPI authorization |
| Via app functions | 9 (banned_donors ×2, moderation_actions, soundboard_tnc_acceptances, streamer_payment_health, streamers_moderators → owns_streamer; obs_tokens → is_admin_user; streamers ×2 → current_account_id / current_user_is_admin) | Owner/admin access to creator data | require_streamer_access / require_admin |
| storage.objects | 31 | Public read of 18 buckets; any authenticated user may upload/delete on 3 buckets | Removed with Supabase Storage |

# Appendix 3 — Full migration matrix  [C current / P new]
| Existing functionality | Current Supabase mechanism | New FastAPI implementation | New frontend requirement | PostgreSQL dependency |
|---|---|---|---|---|
| Password login | Supabase Auth signInWithPassword | POST /auth/login, Argon2id/bcrypt, lockout | login form | auth_users |
| Session persistence / refresh | supabase-js session + onAuthStateChange | server session cookie, sliding expiry | bootstrap via /auth/session | sessions (auth_sessions) |
| Logout | auth.signOut | POST /auth/logout (+ logout-all) | logout action | sessions |
| Password reset | resetPasswordForEmail, updateUser | /auth/password/forgot + /reset, Resend | forgot + reset pages | password_reset_tokens |
| Invite-only accounts | manual Supabase dashboard + SQL | admin invite + accept flow | admin users page, accept page | auth_users, streamers.user_id |
| Admin email change | admin-update-email | PATCH /admin/users/{id} | admin UI | auth_users |
| Terms gate | terms-acceptance + RPCs | /dashboard/.../terms | gate component | terms_acceptances + RPCs |
| Creator picker | get-user-streamers | GET /me/streamers | dashboard home | streamers, auth_users |
| Creator settings | get-streamer-for-owner, update-streamer-settings | GET/PATCH /dashboard/streamers/{slug}/settings | settings forms | streamers |
| Settings live push | broadcast-settings-update → Pusher | outbox → Pusher settings-updated | overlays listen | outbox (new) |
| Goal management | settings + get-goal-progress | /dashboard/.../goal, /overlays/{slug}/goal | goal manager + overlay | streamers, donation tables |
| Donation list / stats / CSV | get-dashboard-donations | GET /dashboard/.../donations (+export) | table + export | donation tables |
| Moderation queue / actions | get-moderation-queue, moderate-donation | /dashboard/.../moderation (+actions) | moderation panel | donation tables, moderation_actions, banned_donors, streamer_donator_totals |
| Moderators | manage-moderators | /dashboard/.../moderators | moderator manager | streamers_moderators |
| Telegram moderation | telegram-webhook, notify-new-donations | /webhooks/telegram (secret token) + notify job | — | telegram_callback_mapping |
| Discord moderation | discord-webhook, notify-new-donations | /webhooks/discord (Ed25519) + notify job | — | discord_callback_mapping |
| OBS tokens | generate/list-obs-tokens, PostgREST update | /dashboard/.../obs-tokens (hashed at rest) | token manager | obs_tokens |
| Payout details | get-payout-status, submit-payout-account | /dashboard/.../payout (AES-GCM) | payout form | payout_accounts |
| Soundboard | get/save/restore/delete, tnc, upload-url, confirm | /dashboard/.../soundboard/* | soundboard editor | soundboard tables + RPCs |
| Voice transcription | transcribe-voice-sarvam | /dashboard/.../transcribe | transcriber | — |
| Creator public page | 134 routes + config file + get-streamer-pricing | GET /public/streamers/{slug} | generic /:slug page | streamers, voice tiers |
| HyperSounds / soundboard list | list-hypersounds, list-streamer-soundboard | /public/streamers/{slug}/sounds | sound picker | soundboard tables, R2 listing |
| Donor uploads | issue-upload-grant, upload-* | /uploads/grant + presign | recorder/uploader | request_nonces |
| Checkout | create-razorpay-order-unified | POST /donations | Razorpay Checkout | order_lookup, donation tables, payment health |
| Payment status | check-payment-status-unified | GET /donations/{id}/status (+ signature verify) | status page | donation tables |
| Payment webhook | razorpay-webhook | /webhooks/razorpay | — | donation tables, order_lookup, totals |
| Auto-approve / HyperEmotes | 4 DB triggers | keep triggers (later service rules) | — | trigger functions |
| TTS / music / SFX | generate-donation-* | jobs (ElevenLabs/MiniMax → R2) | — | donation tables, tts_moderation_audit |
| OBS audio player | get-current-audio | GET /obs/audio/next | new media-source URL | claim_next_audio |
| OBS alerts / goal / leaderboard | get-overlay-config, get-leaderboard-data, Pusher | /overlays/{slug}/*, Pusher | overlay routes (same paths) | streamers, totals |
| Pusher private auth | pusher-auth, get-pusher-config | /realtime/pusher/auth + config | pusher-js authEndpoint | obs_tokens, streamers |
| Platform / showcase metrics | get-*-metrics RPCs | /metrics/* | landing page | metric RPCs |
| Signup interest | browser insert into user_signups | POST /public/signups | signup dialog | user_signups |
| Discord invite | get-discord-link | config endpoint | signup dialog | — |
| Partner API | partner-signup, partner-check-streamer | /partner/* | — | partner_api_keys, partner_signups |
| Partner key admin | generate-partner-key | /admin/partners | admin UI | partner_api_keys |
| Rate limiting | check_rate_limit_v2 / server RPCs | same RPCs or middleware | — | rate_limits |
| Replay protection | request_nonces | same (Phase 2 code) | — | request_nonces |
| Payment health | payment-health-check (pg_cron) | timer job | dashboard badge | streamer_payment_health |
| Order cleanup | pg_cron SQL | timer job | — | order_lookup |
| Security audit | log_security_violation etc. | security log + table | — | security tables |
| Legacy Supabase Storage media | 19 buckets | copy to R2 + URL rewrite | — | 16 URL rows |

# Appendix 4 — External integrations  [C]
Razorpay (orders, payments, webhook; 2 key sets) · Pusher (REST publish + private auth; 4 apps by pusher_group) ·
Cloudflare R2 (S3 API: PutObject, presign, Head, Delete, ListObjectsV2) · ElevenLabs (TTS, music, SFX) ·
MiniMax (TTS) · Sarvam AI (speech-to-text/translation) · Telegram Bot API (messages, webhook, getChatMember) ·
Discord API v10 (messages/DMs, interactions, application PATCH) · Resend (legacy reset email) ·
Supabase Auth email (current reset emails; provider configured in the dashboard [A]) · Google OAuth (6 dormant identities) ·
Cashfree (npm dependency; only referenced in FeatureShowcase.tsx, no live payment code) · Lovable (cdn.gpteng.co script).

# Appendix 5 — Assumptions to verify before implementation  [A]
1. Whether auth_users bcrypt hashes are stale versus current Supabase passwords (decides the credential strategy).
2. Supabase Auth email/SMTP provider and templates actually in use.
3. Exact production definitions of handle_new_user, the two cron jobs and any dashboard-only objects (prod vs repo).
4. Whether legacy routes (/ankit/audio-player, /chiaa/audio-player, OBS token deactivation via PostgREST) work today under RLS.
5. Which of the 116 unreferenced DB functions are called by other functions or ops scripts (drop list).
6. Which of the 10 unreferenced tables are still written by DB functions (rate_limits, system_alerts, soundboard_swap_log, terms_acceptances via RPCs).
7. Whether any partner or third party calls Edge URLs other than the documented partner API.
8. How many streamers' OBS media sources point at *.supabase.co/functions/v1/get-current-audio (communication plan).
9. Whether Pusher group 4 credentials exist (dynamic env name).
10. Whether the service_role JWT in the migrations is still valid (legacy keys enabled).
