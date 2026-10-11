# Backend completion checklist

Tracks every backend capability from the full backend audit (58 capabilities, 2026-10-10) until the V2 backend is
complete. The frontend (plan phase 13) and the production cutover (phases 14–15) are out of scope here, except
where a backend item exists only to make them possible.

**Baseline (verified 2026-10-11):** `main` = `c53a8c9` (security fixes #24-#32, D1 tooling #33/#34; migration head
`0016`); DEV still runs `9042fb7fff40` at migration `0015` (bots disabled), so nothing merged since then is deployed;
production runs `940137070657` at migration `0009`, untouched.

## Statuses

| Status | Meaning |
|---|---|
| `MISSING` | No V2 implementation. |
| `PARTIAL` | Some of the required behaviour exists. |
| `IMPLEMENTED_UNVERIFIED` | Implemented and unit/integration tested, but never run against the real external service it depends on. |
| `TESTED` | Implemented, with automated tests (incl. PostgreSQL) proving the behaviour; no runtime evidence needed or none yet. |
| `VERIFIED` | Automated tests **and** DEV runtime / real external-service evidence (recorded in the Evidence column). |
| `BLOCKED` | Waiting on a decision or credential from the owner (see "Decisions" below). |
| `DEFERRED_BY_APPROVAL` | Deliberately not built / changed, with recorded owner approval. |

Evidence abbreviations: **A** automated tests (PostgreSQL 17 suite), **D** DEV runtime, **X** real external
service (Razorpay test mode, the DEV Pusher app, the DEV Telegram/Discord bots, the DEV R2 bucket).

## Decisions requiring owner approval

| ID | Decision | Blocks | Recommendation |
|---|---|---|---|
| **D1** | **Migrated users' credentials.** Legacy passwords live in Supabase `auth.users` (not in the dump); 0/137 DEV users have a hash. Options: import Supabase bcrypt hashes at cutover; forced reset by email; Google-only; a mix. | AUTH-6, launch | Import hashes at cutover where available **plus** reset-by-email / Google as the fallback; needs D8 (Resend). **Tooling done:** read-only report (#33, run by the owner) and importer (#34, not run against real Supabase data; no hash imported). **Still the owner's:** approve the cutover import, how the excluded accounts regain access, whether `email_confirmed_at` may ever set `email_verified_at`, and the timing (after legacy password changes stop). Real hashes stay out of DEV without separate approval. |
| **D2** | **Payout data.** Key custody and migration of the AES-GCM encrypted bank/UPI/PAN records (32 on DEV): the same key in V2, or decrypt-and-re-encrypt at cutover; who may read decrypted values; who marks an account "verified". | DASH-9 | Port the legacy format unchanged with the same key held only in the API env; no endpoint ever returns decrypted values; verification stays a manual admin action. |
| **D3** | **Soundboard terms acceptance.** Confirm legacy behaviour (custom uploads require acceptance of the soundboard T&C) is still required, and its version string. | DASH-11 | Keep legacy behaviour. |
| **D4** | **Partner API** (key management, partner signup/check). 2 keys exist on DEV. Keep or drop? | PLAT-2, PLAT-3, SEC-L1 | Drop unless partners are still active; never invalidate the existing keys without approval. |
| **D5** | **Discord invite link** (deferred in Phase 11, D12) and **contact form** (broken in legacy: its table never existed). Build or drop? | BOT-7, PLAT-8 | Discord link: a public config value (cheap). Contact form: drop. |
| **D6** | **Second Razorpay account** (`secondary`): configure and verify on DEV, or deprecate. | PAY-8 | Depends on whether any creator uses it in legacy production. |
| **D7** | **Scheduled payment-health check** (legacy: every 6 h). V2 checks keys at checkout only. Needed? | PAY-9 | Add a daily CLI check under the existing cleanup timer only if creators/admins look at the status. |
| **D8** | **Credentials / budgets for live verification on DEV:** MiniMax, ElevenLabs, Sarvam, Resend (DEV sender domain), Google OAuth (DEV client). | AUTH-3/4/5, AUD-1/2, DASH-8 | Small capped test budgets; keys entered by the owner on the server. |
| **D9** | **Media access model.** Donor voice messages and media are readable by anyone holding the URL (unguessable key). Accept, or move to signed URLs? | SEC review | Accept for launch (same as legacy), documented as a residual risk. |
| **D10** | **Leaderboard latest-donations feed.** Approved as dropped in Phase 4 (D2: "top donor only, no latest feed"). Revisit? | OBS-6 | Keep the approved decision unless the new overlay design needs it. |
| **D11** | **Admin email change** (legacy `admin-update-email`). Needed? | AUTH-9 | Yes, small: admin-only endpoint with session revocation. |
| **D12** | **Razorpay account for new creators.** Onboarding currently requires the admin to name a *configured* account per creator (no default). Keep that, or default to one account? | CRE-2 | Keep it explicit until D6 settles the second account. |
| **D13** | **Creator ownership transfer.** Owner assignment only claims an *unclaimed* creator; a claimed creator answers 409. Allow admin transfers (and what happens to the old owner's sessions)? | CRE-2 | Not for launch; transfers stay a manual, reviewed operation. |
| **D14** | **Duplicate signup answer.** Legacy told the visitor "This email is already registered", which lets anyone test whether an address signed up. V2 answers a repeated email exactly like a new one (201, nothing stored or overwritten). Keep, or restore the legacy message? | PLAT-5 | Keep the identical answer (privacy); the frontend can say "Thanks, we'll be in touch" in both cases. |
| **D15** | **Legacy OBS Media Source URLs on `*.supabase.co`.** V2 cannot answer on that host, so they stop working at Supabase shutdown. (1) Creators update the URL before shutdown; (2) bridge: during cutover, replace the legacy `get-current-audio` Edge Function with a 302 to `https://api.hyperchat.space/api/v1/obs/audio/next?token=<same token>` (OBS must follow two redirects; needs a live OBS test). | OBS-4, cutover | (2) for the switch, (1) communicated well before Supabase shutdown. See `docs/obs-cutover.md`. |

## Capabilities

### Authentication and accounts

| ID | Capability (audit finding) | Status | Required change → acceptance | Tests | DEV / external | Blockers | Evidence |
|---|---|---|---|---|---|---|---|
| AUTH-1 | Password login, session, logout, CSRF | VERIFIED | – | auth tests | `smoke_auth.py` 20/20 on DEV | – | A+D (2026-10-09) |
| AUTH-2 | Logout-all, password change | TESTED | – | auth tests | – | – | A |
| AUTH-3 | Password reset (email) | IMPLEMENTED_UNVERIFIED | Live Resend delivery on DEV → mail received, token works once | existing | Resend on DEV | D8 | A; DEV email `disabled` |
| AUTH-4 | Invites (create/accept, email) | IMPLEMENTED_UNVERIFIED | Live delivery on DEV | existing | Resend on DEV | D8 | A |
| AUTH-5 | Google sign-in | IMPLEMENTED_UNVERIFIED | DEV OAuth client → login with a test Google account links an existing user only | existing | Google | D8 | A |
| AUTH-6 | **Migrated users can log in** | IMPLEMENTED_UNVERIFIED | Per D1 → the approved import runs at cutover and a migrated user signs in (**not verified**: no real hash has been imported anywhere) | `database/tests/test_auth_readonly_report.py`, `test_auth_hash_import.py`, `backend/tests/auth/test_hash_import_login.py` (synthetic hashes only) | cutover | **D1** (run approval), D8 (reset fallback) | 0/137 hashes on DEV. Phase 1 read-only report (#33, `3e9c687`) run by the owner: 153 Supabase / 137 V2 users, 134 exact id+email matches with a source hash and no target hash, 1 email mismatch, 2 V2 legacy ids missing in Supabase, 18 Supabase ids not in V2, 1 V2 email used by another Supabase id. Phase 2 importer (#34, `c53a8c9`): dry run by default, exact id+email only, bcrypt cost 04-12 via the shared `app/auth/bcrypt_format.py`; not run against real Supabase data; no hash imported |
| AUTH-7 | Terms gate | TESTED | – | dashboard tests | – | – | A |
| AUTH-8 | Admin user / invite management | TESTED | – | admin tests | – | – | A |
| AUTH-9 | Admin email change | BLOCKED | Admin-only endpoint; unique email; revokes the user's sessions; audit event | unit + PostgreSQL | – | D11 | – |
| AUTH-10 | Sign-in throttling, rate limits, security events | TESTED | Security audit F2: the account-wide lockout is replaced by layered throttling (IP 20/15 min + 100/24 h, account at IP 5/15 min + 15/24 h, one generic 401) | `tests/auth/test_login_throttling.py` (12) + security tests | DEV smoke after deploy approval (the earlier DEV evidence covered the old lockout) | – | A |

### Creators

| ID | Capability | Status | Required change → acceptance | Tests | DEV | Blockers | Evidence |
|---|---|---|---|---|---|---|---|
| CRE-1 | Creators a user may manage; access check | VERIFIED | – | authz tests | DEV | – | A+D |
| CRE-2 | **Create / onboard a creator** | TESTED | `POST /api/v1/admin/creators` (admin + CSRF): slug 3–64 `[a-z0-9_-]`, lowercased, reserved site paths refused; name; **explicit configured Razorpay account (no default, D12)**; optional `#RRGGBB` colour and legacy-shape `order_prefix`; legacy launch defaults (`docs/ADD_NEW_STREAMER.md` step 3); least-loaded configured Pusher group (legacy rule); unclaimed; `creator_created` event. **Gap found while testing:** an unclaimed creator is admin-only and nothing set `streamers.user_id`, so a new creator could never be handed over → `POST /api/v1/admin/creators/{id}/owner` claims an **unclaimed** creator for an active user (409 when claimed: no transfers, D13), `creator_owner_assigned` event | 7 PostgreSQL tests (+15 validation cases): defaults written exactly, public page works, admin-only until owned then the owner gets access; validation; slug / prefix conflicts; 401/403/CSRF; extra fields (`user_id`, `status`) refused; concurrent same-slug creations → one; owner assignment rules; concurrent assignments → one winner | DEV after deploy approval | D12, D13 (current behaviour is the conservative default) | A (G2, PostgreSQL 17: backend 4,464 / database 46; 5 clean repeats) |

### Donations and payments

| ID | Capability | Status | Required change → acceptance | Tests | DEV / external | Blockers | Evidence |
|---|---|---|---|---|---|---|---|
| PAY-1 | Creator profile, pricing, minimums | VERIFIED | – | public tests | Phase 4 smoke 53/53 | – | A+D |
| PAY-2 | Hypersounds, public soundboard | VERIFIED | – | soundboard tests | DEV R2 | – | A+X |
| PAY-3 | Voice upload (grant + upload) | VERIFIED | Fix the stale smoke assertion (PAY-11) | uploads tests | DEV R2 | – | A+X |
| PAY-4 | Image upload | VERIFIED | – | uploads tests | DEV R2 | – | A+X |
| PAY-5 | Checkout (Razorpay order) | VERIFIED | Live-mode keys only at cutover | payments tests | 2 real test-mode orders, 2026-10-07 | – | A+X |
| PAY-6 | Payment webhook | VERIFIED | – | payments tests (incl. 3 concurrent deliveries) | 2 real signed `payment.captured`, 2026-10-07 | – | A+X |
| PAY-7 | Status polling / confirm | IMPLEMENTED_UNVERIFIED | One real test-mode poll + confirm on DEV (synthetic creator, test key) → donation completes via each path | existing | Razorpay test mode | owner runs the test-mode checkout | A |
| PAY-8 | Second Razorpay account | BLOCKED | Per D6 | existing | – | D6 | A; not configured on DEV |
| PAY-9 | Scheduled payment-health check | BLOCKED | Per D7 | – | – | D7 | checkout-time check only |
| PAY-10 | Auto-approve, donor totals, `!vaani` | VERIFIED | – | payments tests | DEV smoke | – | A+D |
| PAY-11 | Stale Phase 6 / Phase 7 smoke assertions | BLOCKED | Correct the two known stale assertions (they were never in the repo; the scripts live in the operator scratchpad) → re-run green. G4 review: which checks are stale can only be confirmed by re-running the scripts against live DEV, and they write synthetic rows and R2 objects and create Razorpay **test-mode** orders | – | DEV | **owner approval** for a live DEV smoke run (R2 writes, Razorpay test mode) | 28/29, 29/30 (last runs, 2026-10-07) |

### Creator dashboard

| ID | Capability | Status | Required change → acceptance | Tests | DEV | Blockers | Evidence |
|---|---|---|---|---|---|---|---|
| DASH-1 | Settings (every legacy-writable field) | VERIFIED | – | dashboard tests | Phase 5 smoke | – | A+D |
| DASH-2 | History, stats, CSV export | VERIFIED | – | dashboard tests | Phase 5 smoke | – | A+D |
| DASH-3 | Moderation queue + approve/reject/hide/unhide/ban | VERIFIED | – | dashboard tests | DEV | – | A+D |
| DASH-4 | **Banned-donor list + unban** | TESTED | Per the approved Phase 5 design (A3: no per-donation `unban_donor`; deferred D-R8 ban list, D-M6 unban by id): `GET …/moderation/banned-donors`, `POST …/banned-donors/{ban_id}/unban` (lifts every active ban of that name, exact `lower()`, same per-donor lock as ban, recorded on the ban row via migration 0016, security event) | 4 PostgreSQL tests: list scope/order/fields; unban lifts duplicates, records who/when, idempotent, re-ban works; cross-creator 404 / 403 / CSRF / 401; concurrent unbans lift once, ban racing unban serialises | DEV after deploy approval | – | A (G1, PostgreSQL 17: backend 4,436 / database 47; 5 clean repeats). Review found a deadlock (unbans of two ids of one name); fixed, and the race test fails with `DeadlockDetected` on the old lock order. Final review: a stale (already lifted) ban id could lift a NEWER ban of the same donor; fixed (only a still-active ban acts), regression test added |
| DASH-5 | Moderators | VERIFIED | – | dashboard tests | DEV | – | A+D |
| DASH-6 | OBS tokens | VERIFIED | – | obs tests | Phase 10 smoke | – | A+D |
| DASH-7 | Replay | VERIFIED | – | obs tests | Phase 10 smoke | – | A+D |
| DASH-8 | Voice transcription (Sarvam) | IMPLEMENTED_UNVERIFIED | Live Sarvam call on a synthetic voice message | existing | Sarvam | D8 | A |
| DASH-9 | **Payout account submit / status** | BLOCKED | Port legacy (submit once, immutable, AES-GCM, status pending/verified) per D2 → tests incl. no decrypted value ever returned | new | DEV with synthetic data | **D2** | – |
| DASH-10 | Soundboard management | VERIFIED | – | soundboard tests | DEV R2 | – | A+X |
| DASH-12 | **Moderator permission editing** (D-M7, deferred in Phase 5; missed by the audit; **new in v2**: legacy never edited them) | TESTED | `PATCH …/moderators/{id}` (owner / linked / admin + CSRF): `role` and the five `can_*` flags the bots check (not `can_manage_mods`, unused); compared with effective values (NULL = default) under the row lock; only changed columns written; removed → 409, other creator → 404; `moderator_permissions_changed` event | 4 PostgreSQL tests (+9 invalid-body cases): NULL legacy rows, only diffs written, effect on `app.bots.permissions.can`, unchanged repeat, scoping/403/CSRF/401, concurrent edits no lost update | DEV after deploy approval | – | A (G2) |
| DASH-11 | **Soundboard terms acceptance** | BLOCKED | Status + accept endpoints; uploads refused until accepted (legacy) | new | – | D3 | table exists, no endpoint |

### Audio, OBS, realtime

| ID | Capability | Status | Required change → acceptance | Tests | DEV / external | Blockers | Evidence |
|---|---|---|---|---|---|---|---|
| AUD-1 | TTS (MiniMax / ElevenLabs) | IMPLEMENTED_UNVERIFIED | Live generation on DEV for a synthetic donation | existing | providers | D8 | A; providers disabled on DEV |
| AUD-2 | `!sfx`, `!testmusic` | IMPLEMENTED_UNVERIFIED | Live generation on DEV | existing | ElevenLabs | D8 | A |
| AUD-3 | TTS text moderation | TESTED | – | audio tests | – | – | A |
| AUD-4 | Silence fallback, retries | VERIFIED | – | jobs tests | DEV | – | A+D |
| OBS-1 | OBS audio queue (302), player | VERIFIED | – | obs tests | Phase 10 live smoke 40/40 | – | A+X |
| OBS-2 | Realtime events + Pusher auth | VERIFIED | – | realtime tests | DEV Pusher | – | A+X |
| OBS-3 | Goal, overlay config | VERIFIED | – | public tests | DEV | – | A+D |
| OBS-4 | **Existing OBS media-source URLs** | PARTIAL | **Documented (G4): `docs/obs-cutover.md`.** The 5 legacy URL shapes (alerts, Media Source, goal, helper page, older player) and their V2 equivalents; route slugs vs creator slugs (`chiaa` → `chiaa_gaming`; `altrui`, `gigi_is_live` not in the DEV dump); cutover checklist. Backend side already covered: legacy-format tokens (`obs_` + 64 hex, ETL SHA-256) work on the Media Source endpoint and Pusher auth, header and `?token=`. **Remaining:** frontend routes (phase 13) and D15 for the `*.supabase.co` Media Source URLs | existing `tests/obs/test_obs_db.py`, `tests/realtime/test_realtime_db.py` (legacy-format tokens, `?token=`) | read-only DEV counts: 218 tokens, 143 active, 126 creators | D15, frontend | A (backend); mapping from the legacy source |
| OBS-6 | Leaderboard latest-donations feed | DEFERRED_BY_APPROVAL | Phase 4 D2 approved "top donor only" | – | – | D10 (revisit?) | PR #3 |

### Moderation bots

| ID | Capability | Status | Evidence |
|---|---|---|---|
| BOT-1 | Telegram buttons | VERIFIED | A+X (DEV bot, Phase 11) |
| BOT-2 | Discord buttons | VERIFIED | A+X |
| BOT-3 | New-donation notifications | VERIFIED | A+X |
| BOT-4 | Commands | VERIFIED | A+X; `/pending`/`/recent` tests only since the incident |
| BOT-5 | Allow-lists, audit history, dedupe, concurrency | VERIFIED | A+D (PR #16, #17) |
| BOT-6 | Webhook setup CLI | VERIFIED | X |
| BOT-7 | Discord invite link | BLOCKED | D5 |

### Platform, admin, operations

| ID | Capability | Status | Required change → acceptance | Blockers | Evidence |
|---|---|---|---|---|---|
| PLAT-1 | Platform / showcase metrics | VERIFIED | – | – | A+D |
| PLAT-2 | Partner API keys | BLOCKED | Per D4 | D4 | – |
| PLAT-3 | Partner signup / check | BLOCKED | Per D4 | D4 | – |
| PLAT-4 | Platform soundboard seeding | TESTED | `python -m app.cli seed-platform-soundboard [--dry-run]`: legacy pairing (`hypersounds/<stem>.mp3` + `hypersound-thumbs/<stem>.<ext>`, case-insensitive stem, gif > png > jpg > jpeg > webp, `jpeg` stored as `jpg`, unpaired sounds skipped and listed), title-cased names, legacy 3.0 s duration placeholder (the server may not read library objects); only fills an **empty** table, all rows in one transaction under an advisory lock; refuses 0 or more than 44 pairs; `--dry-run` writes nothing; `platform_soundboard_seeded` event. Not run on DEV (DEV already has its 44 slots, so it would answer "Nothing to do") | 5 unit tests with the in-memory R2 store (pairing, dry run, refusals incl. R2 list failure and 45 pairs, existing rows → R2 never listed, exactly 44) + 3 PostgreSQL tests (seeds once then nothing; 4 concurrent seeders → one set; a failing row inserts nothing) | – | A (G3) |
| PLAT-5 | Signup interest form | TESTED | `POST /api/v1/public/signups` (public, no cookies): trusted frontend Origin (checked before the body), 5 per IP per hour (`rate_limits`, bucket `signup:create`), legacy field rules (name 1–100, email 5–255 + legacy pattern, mobile 8–20 of digits / `+ - ( )` / spaces, YouTube ≤ 500, Instagram ≤ 100), trimmed, email lowercased, control characters refused, extra fields refused; a repeated email (any letter case, incl. legacy mixed-case rows) answers the same 201 and stores / overwrites nothing (D14); database errors → 503 without details; nothing is emailed; `signup_rejected` / `signup_failed` events without the email | 27 unit tests (origin, 19 invalid bodies, missing fields, normalisation, rate limit, DB failure in insert and in the limiter, not configured) + 3 PostgreSQL tests (stored row, duplicates incl. legacy mixed case, real limiter + no email in logs) | D14 (current behaviour is the privacy-preserving default) | A (G3) |
| PLAT-6 | Security logs, nonces, rate limits | TESTED | – | – | A |
| PLAT-7 | **Order-lookup cleanup** | TESTED | Legacy daily rule `created_at < now() - 30 days` in `maintenance.RULES` (the existing daily cleanup timer) → cleanup test counts/keeps; a webhook for an order whose lookup row was cleaned still completes via `donations.provider_order_id` | – | A (G1) |
| PLAT-8 | Contact form | BLOCKED | Per D5 | D5 | legacy broken |
| PLAT-9 | Production backups | VERIFIED | Restore drill documented in release readiness | – | nightly dumps |
| PLAT-10 | Admin email change | see AUTH-9 | | | |

### Cleanup and documentation (audit findings 3–5, README)

| ID | Item | Status | Required change → acceptance | Blockers |
|---|---|---|---|---|
| SEC-L1 | Unused HMAC / partner-key / cron-secret layer | BLOCKED | Remove after tracing; the partner verifier depends on D4 | D4 |
| SEC-L2 | Dead upload-grant helpers (legacy 4-part grant, `verify_and_consume_upload_grant`, `fetch_order`) | PARTIAL | **Done (G3):** the legacy untyped 4-part grant is no longer issued or accepted (`kind` is required for issue and verify; a correctly signed 4-part token is refused as malformed) and `verify_and_consume_upload_grant` is removed; the upload API already used only typed grants + `consume_upload_grant`, so no client changes. Tests now cover the refusal of the legacy form, missing / unknown kinds, and that a refused grant is never burned. **Left in place:** `RazorpayClient.fetch_order` (unused by app code, but part of the payments client, outside G3's upload-grant scope; remove or use with the PAY-7 poll work) | – |
| SEC-L3 | Duplicated IST constant | TESTED | `app.core.timezones.IST` is the one definition (dashboard validation / CSV export and bot message formatting import it; the bots' copy had no name, so `tzname` is now "IST" there too, which no output uses) → 4 tests: offset, no DST, identity, the 18:30 UTC day boundary, month / year-end bounds, unchanged bot and CSV text | – |
| DOC-1 | Stale README status table | TESTED | README status table, `docs/architecture.md` delivery plan, `database/README.md` (revisions 0001–0016, order) and `docs/target-schema.md` now match `main`: phases 1–11 merged, backend completion G1-G4 merged, security fixes and D1 tooling merged (updated 2026-10-11 at `c53a8c9`), DEV and production state, external verification status pointing at this checklist | – |

## Out of scope for this phase (tracked elsewhere)
- Frontend (plan phase 13): every page type; nothing built.
- Cutover (phases 14–15): ETL of a fresh dump, credential import (D1), OBS URL comms (OBS-4), webhook re-pointing,
  Supabase shutdown and secret rotation.

## Work groups
| Group | Scope | Status |
|---|---|---|
| G1 | DASH-4 ban list + unban by id (migration 0016); PLAT-7 order-lookup cleanup | merged into `main` (#18, `7b21cad`); TESTED; not deployed (DEV deploy needs approval) |
| G2 | CRE-2 creator onboarding + owner assignment (admin); DASH-12 moderator permission editing | merged into `main` (#19, `c526d2e`); TESTED; not deployed (DEV deploy needs approval) |
| G3 | PLAT-5 signup form, PLAT-4 soundboard seed CLI, SEC-L2 (upload grants; `fetch_order` left), SEC-L3, DOC-1 | merged into `main` (#20, `1b4e2a8`); TESTED; no migration; not deployed (DEV deploy needs approval) |
| G4 | OBS-4 URL mapping + cutover compatibility checklist; PAY-11 | OBS-4 documented, merged into `main` (#22, `bfb9a1e`; remaining: D15, frontend); PAY-11 blocked on approval for a live DEV smoke run |
| D1 tooling | Phase 1 read-only Supabase Auth report; phase 2 password-hash importer (dry run by default) | merged (#33 `3e9c687`, #34 `c53a8c9`); report run by the owner; importer not run against real Supabase data; no hash imported; migrated sign-in not verified (AUTH-6) |
| G5+ | Items unblocked by decisions D1–D15 | waiting |
| SEC | Full backend security audit (`docs/backend-security-audit.md`) after G1–G4 | audit done on `bfb9a1e`: 0 P0, 1 P1 (F1 OBS tokens in production Nginx logs), 3 P2 (F2 lockout abuse, F3 unlimited current-password checks, F4 tokens in frontend URLs), 3 P3. F1 merged in #24 (`aa76d78`: production Nginx logs no query strings / referrers, CI-checked; not deployed); F1-R merged in #24 (token paths log errors only at crit, DEV port 80 without query; not deployed); F2 fixed in the repository (layered sign-in throttling: IP 20/15 min + 100/24 h, account at IP 5/15 min + 15/24 h, no account-wide lockout, one generic 401; merged in #29, `1c018a7`; not deployed); F9 (P1, introduced by F2: unbounded Argon2 for IP-throttled sign-ins) fixed in the repository (no hashing over the IP limit, hashing via asyncio.to_thread; merged in #30, `4a30786`, not deployed; must ship with F2); F10 (reset links burned whenever a password is set or the account is deactivated, refused for inactive accounts; atomic reset; compare-and-set change; race-safe password sign-in sessions) merged in #31 (`3b834d8`; not deployed); F11 (forgot-password timing) merged in #32 (`9a33fe8`: reset email sent after the response via BackgroundTasks; not deployed); F12 (P3, found reviewing #34: bcrypt cost unbounded at sign-in) merged in #34 (`c53a8c9`: one shared bcrypt format check, cost 04-12; not deployed); F10 follow-up (Google sign-in: account check, link, record and session in one transaction under the user-row lock; closes the deactivation race and partial writes) merged in #36 (`c5b8751`; not deployed); F10 follow-up (invite acceptance: session inserted in the acceptance transaction; a reset requested after acceptance now revokes it, and a failure leaves no consumed invite, password or session) in the PR from branch `security-invite-session-race` (not merged / deployed); F3 merged in #25 (`dc4c7d8`: per user, across sessions and under concurrency; not deployed), policy approved 2026-10-11 (5 current-password checks per user per hour, session kept, 429 until reset, weak new passwords not counted); F5 (public /health: status only; details for 127.0.0.1) and F6 (invite links refused in production) merged in #28 (`ca76614`; not deployed); Dependency scan 2026-10-10 (pip-audit, PyPI + OSV): 50/50 packages, 0 known vulnerabilities; Bandit: 65 results, 1 real (F8, P3, literal bidi characters; merged in #27, `0c6f03b`: `\u` escapes + CI check), rest false positive / informational |
