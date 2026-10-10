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
| Tracked owner decisions / approvals | 6 | D9, D14, D15, D4 / SEC-L1, SEC-L2, PAY-11 |
| Dependency scan / static analysis (2026-10-10) | 0 vulnerabilities; 1 P3 | F8 (literal bidi characters; fixed in the repository, not merged); all other Bandit results are false positives or informational |

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

#### F1-R (P3): tokens in the Nginx error log and the DEV redirect log
- **Assessment:**
  - **Upstream failures** (connect refused, timeouts, early closes): Nginx writes an `error`-level line whose context includes `request: "GET /api/v1/obs/audio/next?token=… HTTP/x"`, plus the `referrer`. Nginx can't drop the query from that line, because the error-log format isn't configurable. So whenever the API is down or slow, every OBS request would put its token in `error.log`.
  - **Rate-limit rejections** are logged the same way, but `limit_req` only applies to `/api/v1/public/` and `/api/v1/metrics/`, which carry no credentials.
  - **The bootstrap config** (`.acme.conf`) only redirects and serves ACME files. Tokens aren't sent there, but a client that did send one would get it logged as a query string on a failed file lookup. Not mitigated; negligible.
  - **DEV's port-80 redirect** logged with the default format, query string included.
- **Mitigation (`9cd6093`, `f15c5a8`), in the repository and not deployed:**
  - **Token-path location:** production and DEV proxy `/api/v1/obs/` and `/api/v1/realtime/` in their own location with `error_log … crit`. Upstream failures there are not written to the error log. Other paths keep full error logging.
  - **Diagnostics kept:** the production access-log format gains `us=$upstream_status rt=$request_time`, so a failed token-path request still shows as `502 … us=502 rt=…`, without the query. The API's own logs are unaffected.
  - **DEV port 80:** DEV's redirect block now uses `hyperchat_dev_noquery`.
  - **Parity:** DEV gets the identical location, so `tests/test_deploy_production.py` (production and DEV keep the same proxy and timeout directives) still holds.
- **Cost:** for those two paths, `error`-level Nginx diagnostics (for example the upstream address and errno) are no longer written. The status, upstream status and timing in the access log, plus the API logs, identify such failures.
- **Test:** `tests/e2e/nginx_ratelimit_check.sh` (real site files, sandboxed):
  - it requests the DEV port-80 redirect with a marker;
  - it then runs both sites with the upstream **down**, requesting production `/obs/` and `/realtime/` and DEV `/obs/`, each with a marker in the query and the `Referer`.
  - **Assertions:** all four requests get 502; the marker appears in **no** log file; the access log records the production failures with `us=502`.
  - **Control:** a failing request on another production path *is* written to the error log with its query string, which shows the check would see a leak.
- **Evidence:** CI run on `f15c5a8`, green in both Nginx environments: `upstream-down requests: 502 502 502 502`, `error log: no OBS token from the token paths, production and DEV`, access log 9 of 9 requests and no marker. The first attempt (`9cd6093`) failed the backend parity test, because the location was only in production; fixed by `f15c5a8`, not by changing the test.
- **Limits:**
  - These only prove the configuration under the tested failure (connection refused). Other `error`-level messages for those paths are suppressed by the same directive, but weren't each exercised.
  - `crit`-level messages (rare: for example, worker or system failures) can still include the request line.
  - Non-token query strings on other paths still reach the error log on failures, by design, for diagnostics.

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
| Email delivery, Google sign-in, TTS / transcription providers | No DEV credentials (D8) | D8 |
| Live payment status poll / confirm | Needs a Razorpay test-mode run (PAY-7) | Owner-run test-mode checkout |
| Production as deployed | Production runs release `940137070657` (Phase 5) at migration `0009`; this audit covers `main` | Redeploy from `main` before cutover, then re-check F1 |
| Frontend | Not built | Frontend phase; F4 is its first requirement |
| Media URLs | Public to anyone holding the URL (unguessable key), as legacy | Owner decision D9 |

## Dependency scan and static analysis (2026-10-10)

**Environment:**
- **Where:** a separate virtual environment in the operator's scratch directory, outside the repository, the project test environment, the VPS, DEV and production.
- **Tools installed:** `pip-audit` 2.10.1 and `bandit` 1.9.4 (and their dependencies), from PyPI.
- **Untouched:** no dependency pin was changed and nothing was upgraded.
- **Full outputs:** `docs/security-evidence/2026-10-10-*` (pip-audit JSON per source, Bandit JSON and text).

**TLS note:** the first scan attempt failed for both sources with `requests.exceptions.SSLError … CERTIFICATE_VERIFY_FAILED … unable to get local issuer certificate` (hosts `pypi.org`, `api.osv.dev`).
- **Cause:** Norton Antivirus intercepts TLS on the operator's machine (certificate issuer "Norton Web/Mail Shield Root"). That root is in the Windows certificate store but not in `certifi`.
- **How the scan was run:** with certificate verification **on**, against the Windows store, via pip's vendored `truststore` (the same trust `pip install` used).
- **Caveat:** the vulnerability data reached the scanner through Norton's TLS inspection, so its integrity also depends on that proxy. A re-run from CI or another machine would remove the caveat.

### Dependencies: no known vulnerabilities
| | Source `pypi` (PyPI JSON API, `pypi.org`) | Source `osv` (`api.osv.dev`) |
|---|---|---|
| Command | `pip-audit --disable-pip --no-deps -s pypi -r backend/requirements.txt -r backend/requirements-dev.txt --desc on -f json` | the same with `-s osv` |
| Packages checked | 50 of 50 pinned (`==`) packages, none skipped | 50 of 50, none skipped |
| Vulnerabilities | **0** | **0** |

- **What was sent:** package names and versions only. Because of `--disable-pip --no-deps`, no project package was downloaded or installed. pip-audit recommends hashed pins; the requirement files have none.
- **Coverage:** `backend/requirements.txt` pins the full environment (45 packages); `requirements-dev.txt` adds 5 test packages.
- **Not covered:** the Python interpreter (3.14.4), operating-system packages, Nginx, PostgreSQL, and the frontend.
- **A clean result is not a guarantee:** it means no *published* advisory matched these exact versions on 2026-10-10.

### Bandit (`bandit -r backend/app`): 65 results, 1 real (P3)
Bandit ran on 11,050 lines with no errors and no `# nosec` suppressions. Each result was checked against the code.

| Result | Count | Classification | Reason |
|---|---|---|---|
| **B613** trojansource (High / Medium) | 2 (`audio/moderation.py:29`, `audio/text.py:32`) | **F8, P3, verified** | See F8 below. |
| B608 hardcoded SQL expressions (Medium) | 52 | **False positive** | Every interpolated piece is one of: a module constant (column lists, the `CALLBACK_TABLE` map, `maintenance.RULES`), a fixed fragment chosen in code (`AND m.is_active = true`, `id = %s`), or a column name from an allowlist (`WRITABLE_SETTINGS`, `MODERATOR_PERMISSIONS`, Pydantic models with `extra="forbid"`). All values are bound parameters. No request data becomes SQL text. |
| B324 `hashlib.md5` (High) | 1 (`realtime/pusher.py:78`) | False positive | `body_md5` is required by the Pusher REST protocol. The request is authenticated with HMAC-SHA256. |
| B311 `random` (Low) | 4 | False positive | Retry jitter and backoff (`audio/handlers.py`, `jobs/runner.py`, `realtime/publisher.py`) and the probabilistic purge trigger (`security/nonce_store.py`). No secret is drawn from `random`; tokens use `secrets`. |
| B105 hard-coded password (Low) | 2 | False positive | Google's token endpoint URL (`auth/google.py:17`) and an error message text (`cli.py:48`). |
| B101 `assert` (Low) | 3 | Informational | Internal invariants (`jobs/queue.py:101`, `realtime/outbox.py:178`) and a test helper (`storage/memory.py:130`). Python `-O` would remove them; the service runs without `-O` (`deploy/systemd`). Could become explicit checks; no security impact found. |
| B110 `try/except/pass` (Low) | 1 (`security/nonce_store.py:55`) | Informational | Best-effort purge of expired nonces; a failure only delays cleanup. Nonce checks themselves are not affected. |

### F8 (P3): literal bidirectional control characters in source
- **Evidence:** `backend/app/audio/moderation.py:29` (4 characters) and `backend/app/audio/text.py:32` (5 characters) contain *literal* Unicode bidi controls, among them U+202E RIGHT-TO-LEFT OVERRIDE, inside regex character classes. The same appears in two tests (`tests/audio/test_moderation.py:51`, `tests/audio/test_text.py:14`).
- **Runtime:** intended. These are the characters the code strips from donor text.
- **Impact:** "Trojan Source" class (CVE-2021-42574). Editors and GitHub may display those lines differently from what Python executes, so a reviewer could be misled by a future change on those lines.
- **Exploitability:** not exploitable at runtime; it's a code-review risk.
- **Remediation (not applied; findings first):** write the characters as `\u` escapes, which compile to the same regex. Add a CI check (Bandit B613 or a grep) that rejects literal bidi characters in `backend/`.
- **Status: fixed in the repository (branch `security-f8-bidi-escapes`; not merged, nothing to deploy).**
  - **The rewrite:** every literal invisible character on the two regex lines (`moderation.py:29-30`, `text.py:32`), on the two test lines, and the one U+202E in the legacy golden fixture (`tests/audio/fixtures/moderation_golden.json`, as a JSON escape, so `json.loads` gives the same value) is now a `\u` escape.
  - **Untouched:** other invisible characters that belong to visible text (emoji variation selectors and joiners in bot messages, Hindi combining marks in tests).
  - **Same behaviour, proven:**
    - `tests/audio/test_invisible_patterns.py` pins both patterns' text and the exact set of code points they match, over **all 1,114,112** code points (185 and 15, SHA-256 of the list taken *before* the rewrite).
    - Five hostile donor texts give outputs recorded with the old patterns.
    - A side-by-side run of `main`'s modules and the new ones gave identical outputs.
  - **Regression check:** `tests/test_source_hygiene.py` scans every source and config file in the repository (`.py`, `.sh`, `.sql`, `.conf`, `.yml`, `.md`, `.json`, …) and fails on any literal bidi control: U+061C, U+200E/F, U+202A–U+202E, U+2066–U+2069.
    - **Exception:** `docs/security-evidence/` is skipped, because it holds verbatim scanner output quoting the old lines.
    - **Proof:** with `main`'s `text.py` restored, the check fails and names all five characters on line 32.
- **Observation (not changed; outside F8):** `sanitize_for_tts` strips most bidi controls but **not** U+061C ARABIC LETTER MARK, nor the soft hyphen or Hangul fillers. The moderation normaliser does strip them. Text-to-speech output is audio, so there is no display effect; recorded only so a future change to the TTS filter can include it.

## Tracked decisions and approvals (unchanged by this audit)

| ID | Item | State |
|---|---|---|
| D14 | Repeated signup email: identical answer (current) or legacy message | recommendation: keep; awaiting owner |
| D15 | Legacy Media Source URLs on `*.supabase.co` | recommendation in `docs/obs-cutover.md`; awaiting owner |
| SEC-L2 | `RazorpayClient.fetch_order` (unused) | delete or keep; awaiting owner |
| PAY-11 | Stale smoke assertions | needs approval for a live DEV smoke run |
| D4 / SEC-L1 | Partner API and the unused HMAC / partner-key layer | awaiting owner |
| D9 | Media access model | awaiting owner |

## Remediation plan

1. **F1** (P1): **done in the repository** (`cc961df`; not deployed). **F1-R** (P3, residual): **mitigated in the repository** (`9cd6093`, `f15c5a8`; not deployed):
   - **Token paths:** they log errors only at `crit`, and the access log carries the upstream status and timing.
   - **DEV port 80:** logs without the query.
   - **Remaining:** only `crit`-level lines and the negligible bootstrap case; see F1-R above.

   Original plan: production log format without query strings + CI assertion. Small, configuration only; deploying it
   to production needs approval.
2. **F3** (P2): limit current-password checks. Small code change with tests.
3. **F2** (P2): owner decision on the lockout model, then code and tests.
4. **F6, F5** (P3): production guard for invite links; trim the public health response.
5. **Dependency scan:** done 2026-10-10, no known vulnerabilities. **F8** (P3): write the literal bidi characters as `\u` escapes and add a CI check that rejects literal bidi characters in source (no behaviour change). Re-run the scan whenever pins change and before cutover. **F8: fixed in the repository** (`\u` escapes, behaviour pinned over all code points, repository-wide CI check).
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
