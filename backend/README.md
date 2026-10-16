# HyperChat v2 API (FastAPI)

The only backend of HyperChat v2: every client (web frontend, OBS overlays, bots, partners)
talks to this API, and only this API talks to PostgreSQL and the external services.

## Layout
```
app/main.py            app factory, lifespan, request logging, CORS
app/core/config.py     HYPERCHAT_* environment settings
app/core/origins.py    trusted browser origins (frontend + HYPERCHAT_ALLOWED_ORIGINS)
app/core/logging.py    JSON-lines logging (no query strings, headers or bodies)
app/db/pool.py         psycopg 3 async pool + ping
app/auth/              FastAPI-managed authentication (sessions, CSRF, passwords, invites, Google)
app/authz/             authorization: creator (streamer) ownership, require_streamer_access
app/admin/             user / invite / creator administration (onboarding, owner assignment), admin bootstrap
app/public/            public read API (Phase 4): creator profile, pricing, overlay config, soundboard,
                       leaderboard, goal, platform/showcase metrics, signup interest form
                       (see ../docs/public-api.md)
app/dashboard/         creator dashboard API (Phase 5): settings, donations + stats + CSV export,
                       moderation state machine, moderators, terms (see ../docs/dashboard-api.md)
app/core/errors.py     ApiError -> {"error": ...} for non-auth errors
app/core/timezones.py  IST (+05:30), the one definition for day boundaries and human-readable times
app/security/          non-browser callers: HMAC-signed requests, nonces, upload grants, OBS tokens,
                       shared secrets, partner keys (+ PostgreSQL stores and FastAPI dependencies),
                       CORS, security logging
app/maintenance.py     cleanup of expired sessions, tokens, nonces, rate-limit windows, invites; abandoned donor
                       uploads older than 24 hours (D9)
app/cli.py             operator commands: bootstrap-admin, cleanup, enqueue-donation-audio, bot webhook
                       setup, seed-platform-soundboard, payment-health
app/routers/           /health, /api/v1/auth/*, /api/v1/me/streamers, /api/v1/streamers/*, /api/v1/admin/*,
                       /api/v1/public/streamers/*, /api/v1/public/signups, /api/v1/public/config (D5),
                       /api/v1/metrics/*,
                       /api/v1/dashboard/streamers/*,
                       /api/v1/me/terms
tests/                 unit tests + end-to-end tests against a scratch database
```
The database schema and migrations live in `../database`, deployment files in `../deploy`.

## Configuration
All settings are `HYPERCHAT_*` environment variables; template: `../deploy/api.env.example`.
On the VPS they live in `/etc/hyperchat/api.env` (root:hyperchat, 0640), never in Git.
`HYPERCHAT_DB_LABEL` is reported in logs and `/health`; the app refuses to start with
`HYPERCHAT_APP_ENV=production` if the label contains `TEST` or `DEV`.

## Authentication (`/api/v1/auth`)
- Passwords: Argon2id. bcrypt hashes imported from the legacy system at cutover verify and are
  upgraded to Argon2id on the next login.
- Browser sessions: opaque token in the `__Host-hc_session` cookie (Secure, HttpOnly, SameSite=Lax);
  only its SHA-256 is stored. 7-day sliding idle expiry, 30-day absolute expiry.
- CSRF: every cookie-authenticated POST/PUT/PATCH/DELETE needs an allowlisted `Origin` and the
  `x-csrf-token` header (returned by `/login` and `GET /session`). Login itself checks `Origin`.
- 5 failed logins lock the account for 15 minutes; IP rate limits in `rate_limits`.
- Password reset: single-use 1-hour token, all sessions revoked. Change password revokes other sessions.
- Onboarding is invite-only (`POST /invites`, admin; `POST /invites/accept`).
- Google: authorization code + PKCE + state + nonce; links an existing account by Google subject,
  then by verified email. Unknown Google accounts are rejected.

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/auth/login` | email + password -> session cookie + CSRF token |
| `GET /api/v1/auth/session` | current user + CSRF token |
| `POST /api/v1/auth/logout`, `/logout-all` | revoke this / every session |
| `POST /api/v1/auth/password/forgot`, `/reset`, `/change` | password reset and change |
| `POST /api/v1/auth/invites`, `/invites/accept` | admin invite, accept invite (an invite issued for an approved application also claims its unclaimed creator; a plain admin invite with `streamer_id` only links the account) |
| `GET /api/v1/auth/google/start`, `/google/callback` | Google sign-in |

## Authorization
- Admins (`users.role = 'admin'`) may act on every creator. A creator nobody has claimed
  (`streamers.user_id IS NULL`) is admin-only. Otherwise the user must own it (`streamers.user_id`)
  or be linked to it (`users.streamer_id`). Creator-scoped routes use
  `Depends(require_streamer_access)` with a `{streamer}` path parameter (UUID or slug).
- Non-browser callers use `app.security.dependencies`: `require_obs_token` (hashed OBS tokens,
  frontend Origin or none) and `require_partner_key` (ClipWave partner API, x-api-key; see
  `docs/partner-api.md`).

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/me/streamers` | creators the signed-in user owns or is linked to (+ `is_admin`) |
| `GET /api/v1/streamers/{streamer}/access` | may the user open this creator's dashboard (`admin` / `owner` / `linked`) |
| `GET /api/v1/admin/users` | search users (`q`, `role`, `active`, paging); never returns hashes |
| `PATCH /api/v1/admin/users/{id}` | change `role`, `is_active`, `streamer_id`; revokes that user's sessions |
| `PUT /api/v1/admin/users/{id}/email` | D11 admin recovery / correction of a user's email (not self-service): stored lowercase and **unverified**; revokes every session, burns open reset links and removes linked Google identities, atomically; `{user, changed, sessions_revoked, reset_tokens_burned, identities_removed}`; same address (any case) = `changed: false`, nothing touched; 403 own email, 409 address in use / pending invite / last sign-in admin; sends no email; audited (`user_email_changed`, never an address) |
| `POST /api/v1/admin/users/{id}/sessions/revoke` | sign a user out everywhere |
| `GET /api/v1/admin/invites`, `POST /api/v1/admin/invites/{id}/revoke` | list / revoke invites |
| `POST /api/v1/admin/creators` | onboard a creator: validated unique slug (reserved site paths refused), name, an explicit **configured** Razorpay account (no default, D12), optional `brand_color` / `order_prefix`; legacy launch defaults but **`pending`** (not public; migration 0020); `standard` KYC track; least-loaded configured Pusher group; unclaimed |
| `POST /api/v1/admin/creators/{id}/owner` | make an active user the owner of an **unclaimed** creator (an unclaimed creator is admin-only); a claimed creator is 409 (transfers are the separate route below). Answers `donation_block`: a new (unverified) owner stops new donations (`kyc_required`) |
| `POST /api/v1/admin/creators/{id}/activate`, `…/suspend`, `…/reactivate` | `{expected_status, reason?}` (compare-and-set; a reason is required to suspend). pending → active needs an owner, a configured payment account and the owner's approved KYC (none exists yet, so activation is refused with 409 `kyc_required`); active → suspended (hidden like an unknown creator, no new donations; dashboard, OBS tokens and history kept); suspended → active only if the creator could accept donations again. Audited in `creator_status_events` (append-only) and `creator_status_changed` (codes only); answers `{creator, donation_block}` |
| `GET /api/v1/admin/applications?status=submitted\|legacy_unreviewed\|approved\|rejected\|all&limit&offset`, `GET …/applications/{id}` | creator applications (the public signup form, KYC PR2): newest first; one application with its review history (`application_events`) and latest invitation |
| `POST /api/v1/admin/applications/{id}/approve` `{slug, name, razorpay_account, brand_color?, order_prefix?}`, `…/reject` `{reason}`, `…/reopen` `{note?}`, `…/notes` `{note}`, `…/invite` | public signup form -> application (`submitted`; the rows that existed before are `legacy_unreviewed`) -> admin approve (creates the creator `pending`, `standard` track, in the same transaction; never active) or reject (reason required; reopen -> `submitted`) -> admin invite (bound to the application, its creator and the applicant's own email; a resend revokes the previous open link; a failed email answers 502 and is retried without a second creator) -> acceptance makes the applicant the creator's OWNER only while it is unclaimed (an owner already there: 409, nothing consumed, never a transfer); an applicant who already has an account is not invited (409) and is linked explicitly with `POST /api/v1/admin/creators/{id}/owner` -> owner's Didit KYC (PR3) -> admin activation (refused until KYC exists). Rows are locked per application, so concurrent decisions serialize (a second approval is 409, never a second creator); audited as `application_reviewed` (codes and ids only) |
| `GET /api/v1/me/kyc`, `POST /api/v1/me/kyc/session` | owner identity verification with Didit (KYC PR3): the signed-in user's state; start / resume (owners of a creator only; Origin + CSRF; 5/h) -> `{session_id, status, url}`. 503 until Didit is configured. See `docs/kyc.md` |
| `POST /api/v1/webhooks/didit` | Didit status webhooks, verified in the database (`kyc_receive_webhook`; the API holds no webhook secret): `X-Signature-V2` over the canonical JSON body, fresh signed timestamps, `event_id` dedupe, owner-bound session match, provider ordering (`docs/kyc.md`) |
| `GET /api/v1/admin/users/{id}/kyc`, `POST …/kyc/refresh` | admin: a user's verification sessions and audit (no identity data); reconcile with Didit's decision endpoint (unsigned, so it never approves: `refresh: approval_needs_webhook`). No endpoint marks anyone approved |
| `GET /api/v1/admin/kyc/enforcement`, `…/preview`, `…/events`; `GET /api/v1/admin/creators/{id}/kyc`; `POST …/kyc/restrictions`, `…/kyc/restrictions/{rid}/lift`, `…/kyc/grace/revoke` | KYC enforcement (PR4, `docs/kyc-rollout.md`): state, restore-witness check and readiness; read-only impact preview; audit; one creator's eligibility; add / lift (note required) a restriction and revoke grace (Origin + CSRF; refused without the restore witness). Activation, closure and grace grants are operator-only (`python -m app.kyc.operator`, migration role), never an API call |
| `POST /api/v1/admin/creators/{id}/owner/transfer` | D13 admin correction / recovery (not self-service): move a **claimed** creator to any active user, naming the expected current owner (409 when it differs; 409 for an unclaimed creator; 400 unknown / inactive target; same owner = `changed: false`). The previous owner is unlinked from this creator and signed out; nothing else on either account or the creator changes. `{creator, changed, previous_owner_id, sessions_revoked, old_owner_unlinked, active_obs_tokens, donation_block}` (an unverified new owner: `kyc_required`, no new donations); OBS tokens are kept (rotate them when the previous owner must lose the overlays); audited (`creator_owner_transferred`, ids only) |
| `PUT /api/v1/admin/creators/{id}/soundboard` | `{enabled: true\|false}`: grant / revoke Custom Soundboard **eligibility** (D3); idempotent (`changed`), security event `soundboard_eligibility_changed`; never accepts the terms for the owner and never deletes acceptances |
| `GET /api/v1/admin/payments/health` | D7 manual check of the Razorpay account through the same cached probe checkout uses (no order, no payment): `{account, configured, mode: test\|live\|unknown, status: ok\|invalid_key\|missing_key\|network_error, checked_at}`; never a key, secret or Razorpay response; `no-store`; audited (`payment_health_checked`) |

An admin cannot change their own role or status, and at least one active admin who can sign in
(password or Google identity) always remains.

## Creator dashboard (`/api/v1/dashboard/streamers/{streamer}`, Phase 5)
Owner, linked account or admin (`require_streamer_access`); CSRF + Origin on every change;
`Cache-Control: no-store` on every response. Full contracts: [`../docs/dashboard-api.md`](../docs/dashboard-api.md).

| Endpoint | Purpose |
|---|---|
| `GET`, `PATCH .../settings` | creator settings (21 writable fields, strict validation, only changed values written) |
| `GET .../donations`, `.../donations/stats`, `.../donations/export.csv` | approved donations (keyset pages), IST revenue stats, CSV export (≤ 366 days, ≤ 10,000 rows) |
| `GET .../moderation/queue`, `POST .../donations/{id}/moderation` | queue + approve / reject / hide / unhide / ban in one transaction (database state only) |
| `GET`, `POST .../moderators`, `PATCH`, `DELETE .../moderators/{id}` | Telegram/Discord moderators; PATCH edits role / `can_*` (D-M7) |
| `GET /api/v1/me/terms`, `POST /api/v1/me/terms/accept` | terms-of-service status and acceptance (server-owned version) |

## Trusted browser origins
`Settings.browser_origins` = the origin of `HYPERCHAT_FRONTEND_URL` + `HYPERCHAT_ALLOWED_ORIGINS`
(comma-separated exact origins). Only these get credentialed CORS and pass the login/CSRF and OBS
Origin checks. No wildcards; plain http only for localhost; production refuses development origins
and `SameSite=None`.

## Operator commands (on the server)
```
cd /opt/hyperchat/current/backend
# First admin (prompts for the password; refuses once any admin can sign in):
../venv/bin/python -m app.cli --env-file /etc/hyperchat/api.env bootstrap-admin --email you@example.com
# Housekeeping (also runs daily from hyperchat-cleanup.timer):
../venv/bin/python -m app.cli --env-file /etc/hyperchat/api.env cleanup --dry-run
# Fill an EMPTY platform soundboard from the R2 hypersound library (no-op once any slot exists):
../venv/bin/python -m app.cli --env-file /etc/hyperchat/api.env seed-platform-soundboard --dry-run
# Razorpay credentials usable? One probe (GET /payments?count=1: no order, no payment); prints account, configured,
# mode (test|live), status (ok|invalid_key|missing_key|network_error), checked_at; exit 0 only when status=ok (D7):
../venv/bin/python -m app.cli --env-file /etc/hyperchat/api.env payment-health
```

## Tests
```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```
Database-backed tests run only with `HYPERCHAT_TEST_AUTH_DB=<scratch_db>:<port>` (an Alembic-migrated
scratch database) and `HYPERCHAT_TEST_PG_HOST` (Unix-socket directory, default `/var/run/postgresql`,
or a TCP host). They never run against `hyperchat` (production) or `hyperchat_restore_test`;
`hyperchat_dev` needs `HYPERCHAT_TEST_AUTH_ALLOW_DEV=1`, and tests that delete rows they did not
create (cleanup, bootstrap) refuse it entirely. CI (`.github/workflows/ci.yml`) runs everything
against a PostgreSQL 17 service container.
