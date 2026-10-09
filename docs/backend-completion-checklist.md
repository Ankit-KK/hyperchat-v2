# Backend completion checklist

Tracks every backend capability from the full backend audit (58 capabilities, 2026-10-10) until the V2 backend is
complete. The frontend (plan phase 13) and the production cutover (phases 14–15) are out of scope here, except
where a backend item exists only to make them possible.

**Baseline (verified 2026-10-10):** `main` = `9042fb7`; DEV runs `9042fb7fff40` at migration `0015` (bots
disabled); production runs `940137070657` at migration `0009`, untouched.

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
| **D1** | **Migrated users' credentials.** Legacy passwords live in Supabase `auth.users` (not in the dump); 0/137 DEV users have a hash. Options: import Supabase bcrypt hashes at cutover; forced reset by email; Google-only; a mix. | AUTH-6, launch | Import hashes at cutover where available (bcrypt is already verified by `auth/passwords.py`) **plus** reset-by-email as the fallback; needs D8 (Resend). |
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

## Capabilities

### Authentication and accounts

| ID | Capability (audit finding) | Status | Required change → acceptance | Tests | DEV / external | Blockers | Evidence |
|---|---|---|---|---|---|---|---|
| AUTH-1 | Password login, session, logout, CSRF | VERIFIED | – | auth tests | `smoke_auth.py` 20/20 on DEV | – | A+D (2026-10-09) |
| AUTH-2 | Logout-all, password change | TESTED | – | auth tests | – | – | A |
| AUTH-3 | Password reset (email) | IMPLEMENTED_UNVERIFIED | Live Resend delivery on DEV → mail received, token works once | existing | Resend on DEV | D8 | A; DEV email `disabled` |
| AUTH-4 | Invites (create/accept, email) | IMPLEMENTED_UNVERIFIED | Live delivery on DEV | existing | Resend on DEV | D8 | A |
| AUTH-5 | Google sign-in | IMPLEMENTED_UNVERIFIED | DEV OAuth client → login with a test Google account links an existing user only | existing | Google | D8 | A |
| AUTH-6 | **Migrated users can log in** | BLOCKED | Per D1 (hash import tooling and/or reset flow) → a migrated test user signs in on DEV | import + login tests | DEV | **D1**, D8 | 0/137 hashes on DEV |
| AUTH-7 | Terms gate | TESTED | – | dashboard tests | – | – | A |
| AUTH-8 | Admin user / invite management | TESTED | – | admin tests | – | – | A |
| AUTH-9 | Admin email change | BLOCKED | Admin-only endpoint; unique email; revokes the user's sessions; audit event | unit + PostgreSQL | – | D11 | – |
| AUTH-10 | Lockout, rate limits, security events | VERIFIED | – | security tests | smoke | – | A+D |

### Creators

| ID | Capability | Status | Required change → acceptance | Tests | DEV | Blockers | Evidence |
|---|---|---|---|---|---|---|---|
| CRE-1 | Creators a user may manage; access check | VERIFIED | – | authz tests | DEV | – | A+D |
| CRE-2 | **Create / onboard a creator** | MISSING | Admin-only endpoint: validated unique slug, default settings, Razorpay account assignment, optional link to a user/invite; never grants access to another creator; idempotent on retry → PostgreSQL tests incl. slug races and cross-creator isolation | new | DEV with a synthetic `authtest-` creator | – | – |

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
| PAY-11 | Stale Phase 6 / Phase 7 smoke assertions | PARTIAL | Correct the two known stale assertions (they were never in the repo; the scripts live in the operator scratchpad) → re-run green | – | DEV | – | 28/29, 29/30 |

### Creator dashboard

| ID | Capability | Status | Required change → acceptance | Tests | DEV | Blockers | Evidence |
|---|---|---|---|---|---|---|---|
| DASH-1 | Settings (every legacy-writable field) | VERIFIED | – | dashboard tests | Phase 5 smoke | – | A+D |
| DASH-2 | History, stats, CSV export | VERIFIED | – | dashboard tests | Phase 5 smoke | – | A+D |
| DASH-3 | Moderation queue + approve/reject/hide/unhide/ban | VERIFIED | – | dashboard tests | DEV | – | A+D |
| DASH-4 | **Banned-donor list + unban** | TESTED | Per the approved Phase 5 design (A3: no per-donation `unban_donor`; deferred D-R8 ban list, D-M6 unban by id): `GET …/moderation/banned-donors`, `POST …/banned-donors/{ban_id}/unban` (lifts every active ban of that name, exact `lower()`, same per-donor lock as ban, recorded on the ban row via migration 0016, security event) | 4 PostgreSQL tests: list scope/order/fields; unban lifts duplicates, records who/when, idempotent, re-ban works; cross-creator 404 / 403 / CSRF / 401; concurrent unbans lift once, ban racing unban serialises | DEV after deploy approval | – | A (G1, PostgreSQL 17: backend 4,436 / database 47; 5 clean repeats). Review found a deadlock (unbans of two ids of one name); fixed, and the race test fails with `DeadlockDetected` on the old lock order |
| DASH-5 | Moderators | VERIFIED | – | dashboard tests | DEV | – | A+D |
| DASH-6 | OBS tokens | VERIFIED | – | obs tests | Phase 10 smoke | – | A+D |
| DASH-7 | Replay | VERIFIED | – | obs tests | Phase 10 smoke | – | A+D |
| DASH-8 | Voice transcription (Sarvam) | IMPLEMENTED_UNVERIFIED | Live Sarvam call on a synthetic voice message | existing | Sarvam | D8 | A |
| DASH-9 | **Payout account submit / status** | BLOCKED | Port legacy (submit once, immutable, AES-GCM, status pending/verified) per D2 → tests incl. no decrypted value ever returned | new | DEV with synthetic data | **D2** | – |
| DASH-10 | Soundboard management | VERIFIED | – | soundboard tests | DEV R2 | – | A+X |
| DASH-12 | **Moderator permission editing** (D-M7, deferred in Phase 5; missed by the audit) | MISSING | Edit a moderator's role / `can_*` flags (legacy `manage-moderators`); bots read them at click time | new | DEV | – | – |
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
| OBS-4 | **Existing OBS media-source URLs** | PARTIAL | Document legacy → V2 URL mapping and a cutover compatibility checklist (no creator contact) | – | – | – | – |
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
| PLAT-4 | Platform soundboard seeding | MISSING | Idempotent CLI (`app.cli`) from the R2 hypersound library → tests: re-run changes nothing | – | – |
| PLAT-5 | Signup interest form | MISSING | `POST /api/v1/public/signups` → validation, per-IP rate limit, no data echoed back, tests | – | 232 legacy rows on DEV |
| PLAT-6 | Security logs, nonces, rate limits | TESTED | – | – | A |
| PLAT-7 | **Order-lookup cleanup** | TESTED | Legacy daily rule `created_at < now() - 30 days` in `maintenance.RULES` (the existing daily cleanup timer) → cleanup test counts/keeps; a webhook for an order whose lookup row was cleaned still completes via `donations.provider_order_id` | – | A (G1) |
| PLAT-8 | Contact form | BLOCKED | Per D5 | D5 | legacy broken |
| PLAT-9 | Production backups | VERIFIED | Restore drill documented in release readiness | – | nightly dumps |
| PLAT-10 | Admin email change | see AUTH-9 | | | |

### Cleanup and documentation (audit findings 3–5, README)

| ID | Item | Status | Required change → acceptance | Blockers |
|---|---|---|---|---|
| SEC-L1 | Unused HMAC / partner-key / cron-secret layer | BLOCKED | Remove after tracing; the partner verifier depends on D4 | D4 |
| SEC-L2 | Dead upload-grant helpers (legacy 4-part grant, `verify_and_consume_upload_grant`, `fetch_order`) | MISSING | Remove with their tests; full suite green | – |
| SEC-L3 | Duplicated IST constant | MISSING | One definition, one import | – |
| DOC-1 | Stale README status table | MISSING | Matches the actual phases | – |

## Out of scope for this phase (tracked elsewhere)
- Frontend (plan phase 13): every page type; nothing built.
- Cutover (phases 14–15): ETL of a fresh dump, credential import (D1), OBS URL comms (OBS-4), webhook re-pointing,
  Supabase shutdown and secret rotation.

## Work groups
| Group | Scope | Status |
|---|---|---|
| G1 | DASH-4 ban list + unban by id (migration 0016); PLAT-7 order-lookup cleanup | done (TESTED; DEV deploy needs approval) |
| G2 | CRE-2 creator onboarding (admin); DASH-12 moderator permission editing | next |
| G3 | PLAT-5 signup form, PLAT-4 soundboard seed CLI, SEC-L2, SEC-L3, DOC-1 | planned |
| G4 | OBS-4 URL mapping + cutover compatibility checklist; PAY-11 | planned |
| G5+ | Items unblocked by decisions D1–D11 | waiting |
| SEC | Full backend security audit (`docs/backend-security-audit.md`) after G1–G4 | planned |
