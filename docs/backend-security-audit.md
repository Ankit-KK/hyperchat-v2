# Backend security audit

**Scope:** the V2 backend on `main` at `bfb9a1e` (backend completion groups 1–4 merged): every HTTP route, authentication,
authorization, payments and webhooks, uploads and media, OBS tokens, realtime, bots, admin and moderator permissions,
background jobs, logging, database roles, and the deployment configuration in `deploy/`.
**Date:** 2026-10-10. **Method:** code and configuration review; safe automated tests; read-only checks on the VPS
(file modes, a few non-secret settings, release and migration). No live payment, R2, webhook, bot or production
operation was run. The frontend does not exist yet and is out of scope, except where the backend relies on it.

**How to read it:** a control is listed as *verified* only where code review and a test or experiment both support
it. A passing test alone does not make a control secure: tests only cover the cases someone thought of.

## Summary

| Priority | Count | Findings |
|---|---|---|
| **P0**: exploitable now, stop everything | **0** | – |
| **P1**: fix before production traffic | **1** | F1 (fixed in the repository, `cc961df`; not deployed) |
| **P2**: fix before launch | **3** | F2, F3, F4 |
| **P3**: hardening | **3** | F5, F6, F7 |
| Tracked owner decisions / approvals | 7 | D9, D14, D15, D4 / SEC-L1, SEC-L2, PAY-11, tool approval |

No route lacks authentication where it needs it, no cross-creator access path was found, and payment completion
cannot be forged without a provider secret. The open items are logging hygiene, abuse resistance and hardening.

## Findings

### F1 (P1): production Nginx would log OBS tokens
- **Evidence:** `deploy/nginx/api.hyperchat.space.conf` uses the default access log (`combined`, which includes the
  query string). OBS Media Source URLs carry the token in the query (`/api/v1/obs/audio/next?token=…`), as can
  Pusher auth (`/api/v1/realtime/pusher/auth?token=…`). DEV already strips the query for `/api/v1/obs/` and
  `/api/v1/realtime/` (`api.quicktestspace.site.conf`, `log_format hyperchat_dev_noquery`); production lacks it.
- **Impact:** long-lived OBS tokens (they authorize a creator's alerts, audio queue and overlay channels) end up in
  `/var/log/nginx/access.log` and anything that copies it (rotation, backups, log shipping).
- **Exploitability:** needs read access to the logs. **Not yet exposed:** production takes no traffic.
- **Remediation:** port the DEV `log_format` and both `map`s to the production site (production names), extend the
  Nginx CI check to assert that no `?token=` reaches the production log. Deploying it needs approval.
- **Status: fixed in the repository, not deployed.**
  - **The fix:** the production site files (`api.hyperchat.space.conf` and the bootstrap `.acme.conf`) log every request with `hyperchat_prod_noquery`: the path only (`$uri`), no query string and no referrer. Both server blocks are covered, including the port-80 redirect. This goes further than DEV, which only strips `/api/v1/obs/` and `/api/v1/realtime/`.
  - **The test:** `tests/e2e/nginx_ratelimit_check.sh` now runs the real DEV and production site files in its throwaway Nginx. It sends requests carrying a secret marker in the query string and in the `Referer`, and fails if the marker reaches the access log.
  - **The control:** a DEV query string that should be logged is checked to be visible, so the check is able to detect a leak. The old production config used the default `combined` format, which the control shows records query strings.
  - **Evidence:** CI run 38060888737 on `cc961df` passed in both Nginx environments: 8 of 8 requests reached the sites, 8 log lines, no marker.
  - **Deployment:** production keeps its current config until a deployment is approved.
  - **Residuals:**
    1. Nginx's **error** log has no configurable format, and its entries for upstream failures and rate-limit rejections include the full request line, query string included.
    2. DEV's port-80 redirect block uses the default log.

    Neither is covered by this fix; both are tracked as F1-R below.

### F2 (P2): anyone can lock a creator out of password login
- **Evidence:** `AuthService.login` (`backend/app/auth/service.py`) records a failed attempt for any existing, active
  account with a password; 5 failures lock it for 15 minutes (`login_lockout_threshold`, `login_lockout_minutes`). The
  per-IP limit is 20 attempts per 15 minutes, so one IP can keep 4 accounts locked indefinitely, more with many IPs.
- **Impact:** availability: a creator cannot sign in with a password (e.g. during a stream). Google sign-in is not
  blocked by the lock. No account is compromised.
- **Remediation (needs a decision):** lock per (account, IP) instead of per account, or replace the hard lock with
  growing delays; keep the per-IP limit. Add a test for the abuse case.

### F3 (P2): the current-password check on password change is not rate limited
- **Evidence:** `AuthService.change_password` verifies `current_password` with no attempt limit.
- **Impact:** someone holding a stolen session can guess the current password without limit, then change it (other
  sessions are revoked), turning a temporary session theft into a permanent account takeover.
- **Exploitability:** needs a valid session cookie (HttpOnly, Secure) plus its CSRF token.
- **Remediation:** per-user limit on failed current-password checks (e.g. 5 per hour), counted with the login
  lockout, and a security event.

### F4 (P2): reset and invite tokens travel in frontend URLs
- **Evidence:** reset and invite emails link to `{frontend}/reset-password?token=…` and `{frontend}/invite?token=…`
  (`AuthService.request_reset`, `create_invite`). Tokens are hashed at rest, single use, 60 minutes / 7 days.
- **Impact:** the frontend host's access logs, analytics or a `Referer` header could capture a live token.
- **Remediation (frontend phase, tracked here):** the frontend reads the token, removes it from the address bar
  before loading anything else, sets `Referrer-Policy: no-referrer` on those pages and keeps them out of analytics.
  Alternatively use a `#fragment`, as the OBS player already does.

### F5 (P3): the public health check reveals deployment details
- **Evidence:** `GET /health` returns the release hash, the environment name and the database label to anyone.
- **Impact:** helps an attacker fingerprint the deployment; no data exposure.
- **Remediation:** keep `status` / `database.status` public; return the rest only to localhost (the deploy checks
  run on the server).

### F6 (P3): the invite-link convenience setting is not refused in production
- **Evidence:** `HYPERCHAT_AUTH_RETURN_INVITE_LINKS=true` returns the accept URL to the inviting admin. Accepting an
  invite sets the password of an existing password-less account (`AuthRepository.accept_invite`, intended for the
  137 migrated users, D1). With the setting on, an admin could take over any password-less account without access to
  its mailbox. It is `false` on DEV and production today (checked read-only).
- **Remediation:** refuse to start with it in production (like the other production guards in
  `app/core/config.py`).

### F7 (P3): unused security code remains
- **Evidence:** the HMAC request signing, shared-secret and partner-key verifiers in `backend/app/security/` have no
  route (route inventory below; SEC-L1, owner decision D4). `RazorpayClient.fetch_order` is unused (SEC-L2; reviewed
  in G3: read-only, validated, no security issue).
- **Impact:** none today; unused code tends to be forgotten when the rules around it change.
- **Remediation:** delete after D4 / SEC-L2 are decided.

## Verified controls

| Area | Control | Evidence |
|---|---|---|
| Client IP | Rate limits key on the real client IP; a forged `X-Forwarded-For` cannot change it | uvicorn 0.54 `--proxy-headers --forwarded-allow-ips 127.0.0.1` behind Nginx (`$proxy_add_x_forwarded_for`); experiment: `"1.2.3.4, 203.0.113.9"` resolves to `203.0.113.9` |
| Sessions | Random 32-byte token, stored hashed; cookie `Secure`, `HttpOnly`, `SameSite=Lax` (`None` refused in production); idle 7 d / absolute 30 d; inactive users' sessions revoked on use | `auth/dependencies.py`, `auth/service.py`; auth tests |
| CSRF | Every unsafe method with a session needs an allowlisted `Origin` **and** the session's HMAC CSRF token (constant-time compare) | `enforce_csrf`; tests in every dashboard / admin suite |
| Login | One generic error; the same hashing work for unknown or locked accounts; per-IP limit; Argon2id with rehash | `AuthService.login`, `auth/passwords.py` |
| Password reset | Per-IP and per-email limits; no account enumeration; token hashed, single use, 60 min; all sessions revoked after a reset | `request_reset`, `reset_password` |
| Google sign-in | Authorization code + PKCE + state (HMAC-signed cookie, 10 min) + nonce; RS256 with audience and issuer checks; `email_verified` required; only existing accounts; redirect limited to a path on the frontend origin | `auth/google.py`, `routers/auth.py` |
| Authorization | One rule for creator routes: admin, owner, or linked user; unclaimed creators admin-only; inactive users refused; denials logged | `authz/access.py`; authz tests |
| Creator scoping | Every query by donation / ban / moderator / slot / token id runs after a lookup or lock scoped to the creator in the same transaction | reviewed `dashboard`, `obs`, `soundboard`, `audio` repositories; cross-creator 404 tests |
| Admin | Admin-only routes; the last usable admin cannot be removed; role and status changes revoke sessions | `admin/`; admin tests |
| Checkout | Trusted `Origin`, per-IP limit; amounts checked server-side; live keys refused outside production | `payments/`; payments tests |
| Payment completion | Webhook: signature over the raw, size-capped body before parsing; deduplicated event id; the signing account must be the order's account; the payment is re-fetched from Razorpay and must match order, currency and exact amount. Confirm: Checkout signature `HMAC(key secret, order|payment)` first, then the same re-fetch. A successful donation is never failed again | `payments/service.py`, `payments/rules.py`, `payments/repository.py`; tests incl. concurrent deliveries; real test-mode webhooks on DEV (PAY-6) |
| Payment status | Status token only in a header, stored hashed; unknown order and wrong token give the same 404 | `PaymentService._donation` |
| Uploads | Trusted `Origin`, per-IP limits; single-use HMAC grants bound to creator and upload type (legacy untyped form refused); size caps enforced while streaming; content checked by parsing, not by the declared type; server-chosen object keys | `uploads/`, `security/upload_grant.py`; uploads tests; real DEV R2 (PAY-3/4) |
| Storage | The server may only write server-generated keys, read and delete creator soundboard objects, and list the two library prefixes | `storage/r2.py` key checks |
| OBS tokens | Hashed at rest; shown once; revocable; legacy tokens work via the same hash; scope never includes the dashboard channel; the player keeps the token in the URL fragment and is served with a Content-Security-Policy, without HTML injection sinks | `obs/`, `security/obs_token.py`, `app/obs/static/player.js`; obs and realtime tests |
| Realtime | Pusher auth: session (+CSRF) for the dashboard channel only, OBS token for overlay channels in scope; per-IP and per-credential limits | `realtime/auth.py`; realtime tests |
| Bots | Telegram secret header (constant-time); Discord Ed25519 signature with a timestamp window; each update processed once; outside production only allow-listed creators and recipients | `bots/`; bots tests; DEV bots (Phase 11) |
| Output encoding | Bot messages escaped for HTML / Markdown; CSV export neutralises formula injection | `bots/format.py`, `dashboard/csv_export.py` |
| Signups | Trusted `Origin`, 5 per IP per hour, strict field rules, identical answer for repeated emails (D14 pending) | `routers/signups.py`; signup tests |
| CORS | Credentials only for the configured origins, echoed exactly | `security/cors.py` |
| Configuration | Refuses: live Razorpay keys outside production, a TEST/DEV database label in production, development origins or `SameSite=None` in production. API docs off. Env files `0640 root:<service group>` (checked) | `core/config.py`; VPS check |
| Database | Runtime roles connect over local peer auth only; DML grants, no DDL; migrations run as a separate role | `deploy/postgres/pg_hba.conf`, migration 0007 |
| Processes | API runs as an unprivileged user with `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict` | `deploy/systemd/` |
| Transport | TLS 1.2/1.3, HSTS, `nosniff`, `Referrer-Policy: no-referrer`, 12 MB body cap | `deploy/nginx/` |
| Secrets in Git | No credential in the history (scan for Razorpay live / test keys, private keys, AWS / Resend / Slack keys, JWTs, 64-hex values; the hits were placeholders and published test vectors) | `git log --all -p -G …` |

## Not verified

| Area | Why | Needed |
|---|---|---|
| Known vulnerabilities in dependencies | `pip-audit` is not installed; installing it downloads packages | **Approval** to install `pip-audit` (and optionally `bandit`) in the scratch environment and run them |
| Email delivery, Google sign-in, TTS / transcription providers | No DEV credentials (D8) | D8 |
| Live payment status poll / confirm | Needs a Razorpay test-mode run (PAY-7) | Owner-run test-mode checkout |
| Production as deployed | Production runs release `940137070657` (Phase 5) at migration `0009`; this audit covers `main` | Redeploy from `main` before cutover, then re-check F1 |
| Frontend | Not built | Frontend phase; F4 is its first requirement |
| Media URLs | Public to anyone holding the URL (unguessable key), as legacy | Owner decision D9 |

## Tracked decisions and approvals (unchanged by this audit)

| ID | Item | State |
|---|---|---|
| D14 | Repeated signup email: identical answer (current) or legacy message | recommendation: keep; awaiting owner |
| D15 | Legacy Media Source URLs on `*.supabase.co` | recommendation in `docs/obs-cutover.md`; awaiting owner |
| SEC-L2 | `RazorpayClient.fetch_order` (unused) | delete or keep; awaiting owner |
| PAY-11 | Stale smoke assertions | needs approval for a live DEV smoke run |
| D4 / SEC-L1 | Partner API and the unused HMAC / partner-key layer | awaiting owner |
| D9 | Media access model | awaiting owner |
| Tools | `pip-audit` / `bandit` | awaiting approval |

## Remediation plan

1. **F1** (P1): **done in the repository** (`cc961df`; not deployed). **F1-R** (P3, residual):
   - **Error log:** entries for upstream failures and rate-limit rejections carry the query string. Options are a lower `error_log` level for `/api/v1/obs/` and `/api/v1/realtime/`, or restricting who can read the error log.
   - **DEV port-80 block:** it uses the default log. Give it the query-free format as well.

   Original plan: production log format without query strings + CI assertion. Small, configuration only; deploying it
   to production needs approval.
2. **F3** (P2): limit current-password checks. Small code change with tests.
3. **F2** (P2): owner decision on the lockout model, then code and tests.
4. **F6, F5** (P3): production guard for invite links; trim the public health response.
5. **Dependency scan** once approved; any finding is triaged here.
6. **F4**: carried into the frontend phase as a requirement.
7. **F7**: after D4 and SEC-L2.

## Evidence: route inventory and test runs

All 82 routes of `create_app()` (the dependency chain is what FastAPI resolves before the handler):

- **Session required** (`require_user`, CSRF on unsafe methods): `/api/v1/auth/{logout, logout-all, password/change}`,
  `/api/v1/me/*`.
- **Admin** (`require_admin`): `/api/v1/admin/*`, `POST /api/v1/auth/invites`.
- **Creator access** (`require_streamer_access`): every `/api/v1/dashboard/streamers/{streamer}/…` route and
  `/api/v1/streamers/{streamer}/access`.
- **OBS token** (`require_obs_token`): `/api/v1/obs/{session, audio/claim, audio/next}`.
- **Trusted Origin + per-IP limit** (`_guard`): signups, upload grant and uploads, checkout, payment status and confirm.
- **Checked inside the handler:** login, password forgot / reset, invite accept (Origin), Google start / callback
  (state), the three webhooks (signatures), Pusher auth (session or OBS token).
- **Public by design:** `/health`, public reads under `/api/v1/public/` and `/api/v1/metrics/` (Nginx per-IP limit),
  `/api/v1/realtime/config`, `/obs/player*` (static; the token stays in the fragment).

Test runs on `bfb9a1e`, 2026-10-10:
- Disposable PostgreSQL 17 (created and dropped on the VPS): database **47 passed**; backend **4,511 passed, 0 failed**.
- pyflakes over `backend` and `database`: one warning (unused variable, `soundboard/audio.py:107`), no security
  relevance.
- GitHub CI on `bfb9a1e`: green.
