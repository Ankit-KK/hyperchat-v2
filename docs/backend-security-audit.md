# Backend security audit

**Scope:** the V2 backend on `main` at `bfb9a1e` (backend completion groups 1–4 merged): every HTTP route, authentication,
authorization, payments and webhooks, uploads and media, OBS tokens, realtime, bots, admin and moderator permissions,
background jobs, logging, database roles, and the deployment configuration in `deploy/`.
**Status updated 2026-10-12** against `main` at `ae6371e` (fixes #24-#40, D1 tooling #33/#34, D1 policy and exclusion/cutoff decisions in #42/#43; delta review below;
F12-R accepted by the owner).
None of the fixes is deployed: DEV still runs `9042fb7fff40`, production `940137070657`.
**Date:** 2026-10-10. **Method:** code and configuration review; safe automated tests; read-only checks on the VPS
(file modes, a few non-secret settings, release and migration). No live payment, R2, webhook, bot or production
operation was run. The frontend does not exist yet and is out of scope, except where the backend relies on it.

**How to read it:** a control is listed as *verified* only where code review and a test or experiment both support
it. A passing test alone does not make a control secure: tests only cover the cases someone thought of.

## Summary

| Priority | Count | Findings |
|---|---|---|
| **P0**: exploitable now, stop everything | **0** | – |
| **P1**: fix before production traffic | **1** | F1 (merged in #24, `aa76d78`; not deployed) |
| **P2**: fix before launch | **3** | F2 (merged in #29, `1c018a7`; not deployed), F3 (merged in #25, `dc4c7d8`; policy approved 2026-10-11; not deployed), F4 |
| **P1** (found after the first round): sign-in hashing DoS | **1** | F9 (introduced by F2; merged in #30, `4a30786`; not deployed) |
| **P3** (found after the first round) | **4** | F10 (reset links outlived a password change; merged in #31, `3b834d8`, not deployed), F11 (forgot-password timing; merged in #32, `9a33fe8`, not deployed), F12 (bcrypt cost unbounded at sign-in; merged in #34, `c53a8c9`, not deployed); F13 (safety guards written as `assert`; found in the delta review; merged in #40, `06b884b`, not deployed); **F12-R** (imported-bcrypt sign-in timing: accepted residual risk, not remediated) |
| **P2** (found in the local GoTrue rehearsal) | **1** | F14 (row-level security silently filtered the D1 source; merged in #47, `2c4b58b`; not deployed) |
| **P2** (found in the review of #48) | **1** | F15 (D1 import recovery could mislead the operator; merged in #49, `2c3efc7`; not deployed) |
| **P3**: hardening | **3** | F5, F6 (merged in #28, `ca76614`; not deployed), F7 |
| Tracked owner decisions / approvals | 8 | D9 (resolved 2026-10-15), D14 (resolved 2026-10-15), D15 (cutover), D4 / SEC-L1 (resolved 2026-10-13), SEC-L2 (open, P3 cleanup), PAY-11 (verified on DEV 2026-10-14); D1 (policy approved 2026-10-12, execution pending); F12-R (accepted residual risk) |
| Dependency scan / static analysis (2026-10-10) | 0 vulnerabilities; 1 P3 | F8 (literal bidi characters; merged in #27, `0c6f03b`; nothing to deploy beyond the next release); all other Bandit results are false positives or informational |

**Status 2026-10-15:** every P0/P1/P2 finding is fixed on `main` and deployed on DEV (`dbaa74c66d88`); "not deployed" below refers to production, which receives them with the cutover deploy. F4 is a frontend requirement. No open P0/P1/P2 backend blocker.

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
- **Status: merged in #24 (`aa76d78`), with F1-R; not deployed.**
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
- **Mitigation (`9cd6093`, `f15c5a8`), merged in #24 (`aa76d78`) and not deployed:**
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

- **Root cause found during the fix:** the per-account counter (`users.failed_login_attempts`) was reset only by a successful sign-in or a password change, never when the lock expired. Once at 5, **each further failure re-locked the account**, so one wrong guess per 15 minutes kept a creator locked out indefinitely, needing only their email. A locked account also answered 429 while an unknown email answered 401, which revealed that the account exists.
- **Status: merged in #29 (`1c018a7`); not deployed. Deploy only together with F9 (merged in #30, `4a30786`).** Policy approved by the owner on 2026-10-11: layered throttling instead of an account-wide lockout.
  - **Limits** (`app/auth/service.py`, `LOGIN_LIMITS`), all in the existing `rate_limits` table:

    | Bucket | Key | Limit | Reset |
    |---|---|---|---|
    | IP, short | `ip:<client>` | 20 attempts / 15 min | window (unchanged existing limit) |
    | IP, long | `ip:<client>` | 100 attempts / 24 h | window (new) |
    | Account at IP, short | `acct:<sha256(lower(email))>:ip:<client>` | 5 attempts / 15 min | window, or a successful sign-in |
    | Account at IP, long | the same key | 15 attempts / 24 h | window, or a successful sign-in |

    - **Rationale for the values:**
      - creators sign in rarely (sessions last 30 days);
      - 5 tries per 15 minutes covers typos;
      - the 24 h buckets make sustained guessing progressively slower: after 15 failures at one IP an account is throttled there for the rest of the day;
      - the IP buckets keep shared NATs usable (20 per 15 min, as before).
  - **How it counts:** every attempt is counted **before** the password is checked, through the atomic upsert `hit_rate_limit` (`INSERT … ON CONFLICT DO UPDATE … RETURNING`). Concurrent requests can't race past a limit, and a throttled request reveals nothing about the password.
  - **No account-wide lockout any more:** the lock path, `AuthRepository.record_failed_login` and the `login_lockout_*` settings are removed. The `users.failed_login_attempts` / `locked_until` columns stay in the schema (no migration) and in the `User` model, but **no longer enforce an account-wide login lockout**: sign-in never increments the counter or sets `locked_until`, and never refuses an attempt because of them. They are still selected with every user row (`AuthRepository`, `_USER_COLS`) and still reset to `0` / `NULL` by a successful sign-in (`record_successful_login`), `set_password` (password reset and change), invite acceptance and `bootstrap-admin`. The admin user list still reports `locked` from `locked_until`; since nothing sets it any more, that is only true for a value written before this change, until it expires. Failures at one IP only throttle that account **at that IP**; every window expires on its own and nothing can renew it beyond its window.
  - **One generic answer:** every failed sign-in (unknown account, wrong password, inactive account, any limit) returns **401** with `GENERIC_LOGIN_ERROR` ("Invalid email or password, or too many attempts. Please try again later."). Throttled requests still run one dummy Argon2 verification, so each failure does the same hashing work.
  - **Account identifier:** the email is trimmed and lowercased, then hashed. The raw email is never stored as a rate-limit key.
  - **Client address:** `client_ip(request)` is the address Uvicorn derives through the trusted proxy chain (see F5). A missing address counts under one shared `ip:None` key, so it can't bypass the limit.
  - **Storage:** `rate_limits` rows are deleted by the existing daily cleanup once their window started more than a day ago, which matches the longest window. A successful sign-in deletes that account-at-IP's rows.
  - **Logging:** `login_rejected` (reason, user id if known, client IP), `login_rate_limited` (the limit hit, client IP) and `login_succeeded`. No password, email or token is logged.
  - **Unchanged:** successful sign-in, session creation, cookies, CSRF, Google sign-in, password reset and change (F3).
  - **Tests** (`backend/tests/auth/test_login_throttling.py`, 12 PostgreSQL tests): requests go through Uvicorn's real `ProxyHeadersMiddleware` with the test client as the local Nginx, so each request's address comes from `X-Forwarded-For` exactly as Nginx appends it. They cover:
    - the documented limits;
    - 20 failures from one IP throttle it across accounts while another IP is unaffected;
    - 5 failures for one account at one IP throttle only there, the owner at another IP always gets in while the attacker keeps going window after window, and nothing is stored on the account;
    - the email is normalized (5 spellings share one budget);
    - spoofed `X-Forwarded-For` values (incl. `127.0.0.1`) don't bypass the IP limit, and the forged addresses never become keys;
    - an untrusted peer can't choose its address;
    - unknown, wrong-password, inactive and throttled failures look identical (status, body, one password verification each, no cookie);
    - sign-in works again after the short window, and the 24 h limit holds until its own window ends;
    - a successful sign-in clears that account-at-IP budget;
    - 12 concurrent wrong guesses reach the password check exactly 5 times, with all 12 counted;
    - expired rows are removed by the daily cleanup;
    - logs contain no password, email or CSRF token.
  - **Regression proof:** against the previous `service.py`/`repository.py`/`config.py`, **10 of the 12** new tests fail. These include the account-lockout, generic-response, normalization, IP-limit, spoofing, concurrency and logging tests. The two that pass on both check the limit values and the success reset, which are non-regression checks.
  - **Remaining limitations:**
    - **A distributed attacker** with many IPs can guess against one account at up to 5 attempts per 15 min (15 per day) per IP. This is the accepted trade-off for not locking accounts. Argon2id and the password policy are the remaining defence; a CAPTCHA or second factor would be a later decision.
    - **Shared IPs:** users behind one IP share the IP buckets.
    - **Not done:** the old `failed_login_attempts` / `locked_until` columns remain in the schema and the `User` model (still selected and reset, no longer enforced); dropping them would need a migration and changes to those queries.

### F3 (P2): the current-password check on password change is not rate limited
- **Evidence:** `AuthService.change_password` verifies `current_password` with no attempt limit.
- **Impact:** someone holding a stolen session can guess the current password without limit, then change it (other
  sessions are revoked), turning a temporary session theft into a permanent account takeover.
- **Exploitability:** needs a valid session cookie (HttpOnly, Secure) plus its CSRF token.
- **Remediation:** per-user limit on failed current-password checks (e.g. 5 per hour), counted with the login
  lockout, and a security event.

- **Status: implemented; policy approved by the owner on 2026-10-11 (5 current-password verifications per user per hour; the session stays active at the limit; 429 until the window resets; a weak new password does not count). Merged in #25 (`dc4c7d8`); not deployed.**
  - **The limit:** `AuthService.change_password` allows **5 current-password verifications per user per hour** (`PASSWORD_CHANGE_LIMIT`, existing `rate_limits` table, bucket `auth:password-change`, key `user:<id>`).
  - **What counts:** every verification counts before the password is checked, so once the limit is reached even the right password gets 429 (no oracle). A rejected weak new password is refused before verification and is not counted.
  - **Logging:** security events `password_change_rejected` and `password_change_rate_limited`, without the password.
  - **Not touched:** login and sessions (sign-in throttling is F2).
  - **Residual bound:** at most 120 guesses per day from one stolen session, about 3,600 over the 30-day session lifetime.
  - **Session revocation at the limit:** considered and **not** adopted. The approved policy keeps the session active.
  - **Tests:** 5 PostgreSQL tests in `tests/auth/test_auth_flow.py`:
    - 5 failures, then 429 even for the right password; the password is unchanged; events are logged and no password appears in the logs;
    - legitimate typos within the limit succeed, a weak new password does not count, and the window resets;
    - the limit is per user;
    - **switching sessions does not reset it:** guesses spread over two sessions of one user, plus a fresh sign-in after signing out, share one budget, and the session stays active at the limit;
    - **concurrent guesses cannot exceed it:** 12 simultaneous wrong guesses through the real service, each on its own database connection, give exactly 5 password checks and 7 × 429, with all 12 attempts counted. The counter is a single atomic PostgreSQL upsert keyed `user:<id>`.
  - **Regression proof:** against the unprotected `change_password`, the limit, per-user, session-switching and concurrency tests all **fail**.
  - **Verified** on PostgreSQL 17 and in CI (see #25); not deployed.

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

- **Status: merged in #28 (`ca76614`); not deployed.**
  - **The change:** `GET /health` (`backend/app/routers/health.py`) answers public callers with exactly `{"status": "ok", "database": {"status": …}}`. Release, environment, database label and service name go only to loopback callers (`127.0.0.1`, `::1`), i.e. the deploy checks on the server (`curl 127.0.0.1:<port>/health`, `deploy/README.md`).
  - **Why loopback can't be faked:**
    - uvicorn listens on 127.0.0.1 only (`deploy/systemd`).
    - It trusts `X-Forwarded-For` only from Nginx, and it takes the client address Nginx appended (verified earlier in this audit). So every proxied request carries the real, non-loopback client address.
    - The app itself never reads the header.
  - **Evidence, automated (CI):**
    - **The route** (`tests/test_health.py`):
      - the public response is exactly status + database status;
      - loopback callers (IPv4, IPv6) get the details;
      - the route never reads `X-Forwarded-For` itself. This runs through Starlette's `TestClient`, so it says nothing about Uvicorn or Nginx.
      - **Regression proof:** fails against the previous implementation.
    - **Uvicorn's real proxy middleware** (`tests/test_health_proxy_chain.py`): the real app wrapped in `uvicorn.middleware.proxy_headers.ProxyHeadersMiddleware`, the class `--proxy-headers` installs, with `trusted_hosts="127.0.0.1"` as in the units.
      - Through the trusted proxy, `X-Forwarded-For` values `203.0.113.9`, `127.0.0.1, 203.0.113.9`, `::1, 127.0.0.1, 203.0.113.9` and `127.0.0.1,203.0.113.9` all get the public response.
      - From an untrusted peer, `X-Forwarded-For: 127.0.0.1` is ignored.
      - A genuine local request without the header keeps the details.
      - **Sensitivity proof:** these tests fail against the previous route, and also fail when the middleware is configured to trust every proxy (`*`). So they test the real boundary, not just the route.
    - **Real end to end** (`tests/test_health_proxy_chain.py::test_real_nginx_and_uvicorn_never_treat_a_forged_header_as_local`):
      - **Setup:** a real Nginx, using the production site's own `location /` forwarding directives (read from `api.hyperchat.space.conf`), in front of a real Uvicorn process started with the production unit's flags (read from `hyperchat-prod-api.service`: `--host 127.0.0.1 --proxy-headers --forwarded-allow-ips 127.0.0.1`, …).
      - **Requests:** from a non-loopback address of the CI machine, without and with forged `X-Forwarded-For: 127.0.0.1`, `::1, 127.0.0.1` and `X-Real-IP: 127.0.0.1`. All get the public response. A direct local request to Uvicorn keeps the details.
      - **When it runs:** only where an `nginx` binary exists; it skips otherwise (e.g. on Windows).
    - **Configuration consistency** (`tests/test_health_proxy_chain.py`):
      - both systemd units listen on 127.0.0.1 and trust forwarded headers only from 127.0.0.1 (no wildcard);
      - both Nginx sites set `X-Forwarded-For $proxy_add_x_forwarded_for` in every proxied location and never rewrite the client address (`real_ip`).

      If either changes, CI fails.
  - **Evidence, configuration audit only (not exercised by a test):**
    - the TLS front of the real site (the end-to-end test uses plain HTTP and only the `location /` directives);
    - the installed files on the server matching the repository (they're deployed from it);
    - a request from the server itself through Nginx counts as local, by design: its client address is loopback.
  - **Deploy docs:** `deploy/README.md` (public health step) and `deploy/api.env.example` (label comment) are updated. **Deployment prerequisite:** the operator's deploy scripts outside the repository (`p8d_deploy.sh`, `p9d_deploy.sh`, `p10d_deploy.sh`, `p11d_deploy.sh`, `p11e_deploy.sh`, `p17_verify.sh`, `p17_build.sh` in the operator scratch directory) read `"release"` from the **public** URL `https://api.quicktestspace.site/health`. Before the next deploy they must read it on the server instead: `curl -fsS http://127.0.0.1:8000/health` (DEV) or `curl -fsS http://127.0.0.1:8001/health` (production). The public URL keeps answering `status`.

### F6 (P3): the invite-link convenience setting is not refused in production
- **Evidence:** `HYPERCHAT_AUTH_RETURN_INVITE_LINKS=true` returns the accept URL to the inviting admin. Accepting an
  invite sets the password of an existing password-less account (`AuthRepository.accept_invite`, intended for the
  137 migrated users, D1). With the setting on, an admin could take over any password-less account without access to
  its mailbox. It is `false` on DEV and production today (checked read-only).
- **Remediation:** refuse to start with it in production (like the other production guards in
  `app/core/config.py`).

- **Status: merged in #28 (`ca76614`), with F5; not deployed.**
  - **The guard:** `Settings` now refuses to start with `HYPERCHAT_APP_ENV=production` and `HYPERCHAT_AUTH_RETURN_INVITE_LINKS=true` ("Refusing to start: HYPERCHAT_AUTH_RETURN_INVITE_LINKS is not allowed in production."). It's a model validator next to the other production guards, so an administrator can't enable it by mistake.
  - **Unchanged elsewhere:** development and test may still use the setting.
  - **Production today:** `false` (checked read-only earlier), so the guard changes nothing about how production currently starts.
  - **Test:** production with the setting is refused; production without it, and development with it, still start.
  - **Regression proof:** the test **fails** against the previous configuration code.

### F7 (P3): unused security code remains
- **Evidence:** the HMAC request signing, shared-secret and partner-key verifiers in `backend/app/security/` have no
  route (route inventory below; SEC-L1, owner decision D4). `RazorpayClient.fetch_order` is unused (SEC-L2; reviewed
  in G3: read-only, validated, no security issue).
- **Impact:** none today; unused code tends to be forgotten when the rules around it change.
- **Remediation:** delete after D4 / SEC-L2 are decided.

### F9 (P1): sign-in could be used to overload the API from one IP (introduced by F2, fixed before deployment)
- **Evidence:**
  - The F2 change (#29) made every throttled sign-in run a dummy Argon2 verification, to keep timing equal.
  - One Argon2 verification costs about **50 ms** (measured: 50.7 ms dummy, 52.7 ms real).
  - It ran synchronously on the event loop, freezing the worker for that time.
  - Nginx does not rate-limit `/api/v1/auth/`.
  - So a client over its IP limit could still make the server hash on every request, without bound. About 40 requests per second from one IP would keep both API workers busy and stall every endpoint.
  - Before F2, an IP over its limit was refused before any hashing.
- **Impact:** availability of the whole API, unauthenticated, from a single IP. No data exposure.
- **Exploitability:** trivial once #29 is deployed. **Not exposed:** DEV and production still run older releases.
- **Status: merged in #30 (`4a30786`); not deployed.** Owner-approved 2026-10-11.
  - **No hashing over the IP limit:** `AuthService.login` checks both IP buckets first. Once either is over its limit, it returns the generic 401 **without any password hashing**, and without touching the account buckets.
    - Within the IP limits, account-at-IP throttling still runs one dummy verification, so account-specific state can't be told apart by timing.
    - Total hashing per IP is therefore capped by the IP limits (20 per 15 min, 100 per 24 h).
    - Every attempt is still counted atomically against the IP before any check, so no request can pass the IP limit or reach verification after it.
  - **Hashing off the event loop:** every Argon2 / bcrypt call in the auth service (sign-in, rehash, password reset and change, invite acceptance) runs via `asyncio.to_thread`. That's the mechanism the project already uses for blocking work; the default thread pool is bounded at `min(32, CPUs + 4)` threads. A slow hash no longer freezes the worker's other requests. Successful sign-in behaviour is unchanged.
- **Accepted timing side channel** (owner decision, 2026-10-11):
  - **What it reveals:** an IP-throttled attempt gets the identical status and body but answers faster, because no hash runs. That shows only that **the IP's shared bucket is exhausted**.
  - **What it doesn't reveal:** account existence, password correctness, or account-specific throttling. The IP buckets don't depend on the account tried, and account-specific throttling still costs one verification.
- **Tests** (`backend/tests/auth/test_login_throttling.py`, 4 more PostgreSQL tests):
  - **IP limit, no hashing:** 50 attempts from one IP get 50 identical generic answers and exactly **20** password verifications. IP-throttled attempts create no account-bucket rows, and all 50 are counted against the IP.
  - **Account throttle still hashes:** within the IP limits, account-throttled attempts still do exactly one dummy verification each.
  - **Concurrency:** 30 concurrent attempts from one IP, through the app's own connection pool, reach exactly **20** verifications, with all 30 counted.
  - **Event loop stays free:** while a sign-in's verification takes 0.6 s, a ticker on the same event loop never stalls for more than 0.3 s, and the sign-in still succeeds.
- **Regression proof:** against the pre-F9 code, the unbounded-hashing, concurrency and event-loop tests **fail**. The account-throttled test passes on both, as intended: it checks behaviour F9 must keep.
- **Remaining limitations:**
  - **Distributed hashing load:** many IPs can still each cause up to 20 verifications per 15 minutes.
  - **Pool queue:** the thread pool bounds concurrent hashing, but its queue is not bounded. Under a large distributed flood, requests wait rather than fail. The pool is the default one, **shared** with the existing R2 and MP3/voice work, so heavy sign-in load can delay uploads (bounded per IP by the limits above).
  - **Not done:** an Nginx `limit_req` on `/api/v1/auth/` would cut this off before the API. It's a separate deployment-configuration change, not part of this fix.

### F10 (P3): password-reset links outlived a password change
- **Evidence (before the fix):**
  - `AuthRepository.set_password`, used by password reset **and** change, updated the password but left the user's open `password_reset_tokens` alone. Only `consume_reset_token` burned them.
  - Invite acceptance (`accept_invite`) and `bootstrap-admin` set a password the same way, without touching reset tokens.
  - `consume_reset_token` didn't check `users.is_active`.
- **Scenario:**
  - **Password change:** someone sees a reset link (a forwarded email, a shared inbox, brief mailbox access). The owner notices and changes the password. The old link still worked for up to 60 minutes and took the account over.
  - **Deactivation:** a link issued before an admin deactivated an account could still set its password, which then worked once the account was reactivated.
- **Severity: P3.** The impact is account takeover, but it requires an unexpired reset link: the 60-minute TTL, single use, and stored only as a hash.
- **Status: merged in #31 (`3b834d8`); not deployed.** No policy decision needed; standard practice.
  - **Burned whenever a password is set or the account is deactivated:** `AuthRepository.reset_password`, `AuthRepository.change_password`, `accept_invite`, `AdminRepository.bootstrap_admin` and `AdminRepository.update_user` (deactivation) burn every open reset token of the user **in the same transaction** (`burn_reset_tokens`).
  - **Active accounts only:** `AuthRepository.reset_password` redeems a token only for an **active** account; a deactivated account's link gets the generic "invalid or expired" answer.
  - **Unchanged:** a link requested after the change works; reset is still single use and still revokes all sessions.
  - **Race fixed (owner review of PR #31):**
    - **The race:** the first version still redeemed the link and saved the new password in **two transactions**, with about 50 ms of Argon2 hashing between them. A password change that finished in that gap was overwritten by the reset.
    - **The mirror case:** `change_password` checked the current password against the hash loaded when the request began, then saved unconditionally. A reset that finished meanwhile was overwritten by a change whose "current" password was no longer current.
    - **`AuthRepository.reset_password`:** now one transaction. It finds the link's user, **locks the user row** (`FOR UPDATE`), requires an active account, redeems the link only if unused and unexpired (re-checked under the lock), sets the password, and burns every open link.
    - **`AuthRepository.change_password`:** a compare-and-set. It updates only while the stored hash is still the one that was verified; otherwise the change gets the same generic "current password is incorrect".
    - **Hashing happens first:** both hash the new password **before** opening the transaction, so no lock is held while Argon2 runs. The reset rate limit is still checked before hashing.
    - **Lock order:** every password-setting path (change, reset, invite acceptance, `bootstrap-admin`) takes the users row before `password_reset_tokens`, so they can't deadlock.
    - **Removed:** the old `consume_reset_token` / `set_password` pair.
    - **Sign-in rehash (found in self-review):** a legacy bcrypt sign-in re-hashes the verified password to Argon2id and used to write it back unconditionally (`record_successful_login`), so a reset or change committed meanwhile could be undone by the **old** password. That write now happens inside `AuthRepository.password_sign_in` (below), which writes nothing and issues no session if the password changed since verification; the sign-in then fails with the generic 401. `record_successful_login` is now used only by Google sign-in, which writes no password.
    - **Deactivation (owner review):** `AdminRepository.update_user`, the only runtime path that sets `is_active` to false, now burns the account's open reset links in the same transaction (user row locked first, then links, then sessions). A link issued before deactivation stays invalid after reactivation; the password and normal activation are unchanged.
    - **Sign-in sessions (owner review):** a password sign-in now finishes in ONE transaction (`AuthRepository.password_sign_in`): it locks the user row, requires an active account whose stored hash is still the verified one, records the sign-in (with the optional rehash) and inserts the session.
      - **Reset** sets the password, burns the links and revokes **all** sessions in its own transaction.
      - **Change** sets the password, burns the links and revokes every **other** session in its own transaction (the current one is kept).
      - **Why this closes the window:** whichever commits second sees the first. A sign-in that commits first has its session revoked by the reset or change; one that comes after is refused (no session). A session authenticated against an old password can no longer outlive a completed reset or change. This closes the window recorded in the first version of this fix.
      - **Lock order:** users row, then `password_reset_tokens`, then `sessions`, on every path (`bootstrap-admin` now burns links before revoking sessions to match).
      - **Argon2 stays outside the transactions.** Google sign-in and invite acceptance were serialized the same way in two follow-ups (see Limitations below).
  - **Race tests** (`backend/tests/auth/test_password_races.py`, deterministic: each interleaving is forced with events or a held row lock, never by timing):
    - **Change finishes first:** a change that completes while a reset is between its hash and its write wins; the reset is refused (`bad_reset_token`).
    - **Reset finishes first:** a reset that completes while a change is between its verification and its write wins; the change is refused (`bad_current_password`).
    - **Open change transaction:** a reset arriving while a change transaction holds the user row waits for that row lock (seen in `pg_stat_activity`), then finds its link burned and is refused.
    - **Rehash race:** a legacy bcrypt sign-in paused while re-hashing, with a reset completing meanwhile: the sign-in is refused (401) and the reset's password stays.
    - **Regression proof:** all four **fail** against the previous PR #31 head (`9f3d5f4`).
    - **Second review round** (`tests/auth/test_auth_flow.py`, `tests/auth/test_password_races.py`):
      - **Deactivation:** an admin deactivates and reactivates an account, and its earlier reset link stays dead (no open link remains), while the original password still signs in and a new link works.
      - **Sign-in vs reset:** a sign-in paused right after its critical section while a reset completes ends with no valid session.
      - **Sign-in vs change:** the same against a password change; only the changing session survives.
      - **Lock contention:** a sign-in arriving during an open reset transaction waits for the user-row lock and gets no session.
      - **Regression proof:** all four **fail** against the previous head `c9d61406`.
- **Tests:**
  - **Password change:** a link sent before a password change is refused, the owner's new password stays, and a link requested afterwards works.
  - **Deactivated account:** a deactivated account's link is refused, and after reactivation the original password still works.
  - **Invite acceptance:** accepting an invite burns a password-less account's open reset link (`tests/auth/test_auth_flow.py`).
  - **`bootstrap-admin`:** does the same (`tests/admin/test_bootstrap_cli.py`).
- **Regression proof:** all four tests **fail** against the previous repository code.
- **Limitations:**
  - **Google sign-in session (follow-up: merged in #36, `c5b8751`; not deployed):**
    - **What the race really was:** Google sign-in checked `is_active`, then linked the identity, recorded the sign-in and created the session in three separate statements, without the user-row lock. Against a **reset** the session insert already waited for the reset's row lock through the `sessions.user_id` foreign key, so a Google session outlived a reset only when it was inserted after the reset committed: a sign-in after the reset, which never depended on the password. The real gaps were elsewhere: a **deactivation** committing between the check and the writes still produced a session (refused only on first use by `resolve`), a new identity link and a recorded sign-in; and a **failure** partway left the link and the record committed without a session.
    - **Fix:** `AuthRepository.google_sign_in` does all of it in **one transaction under the user-row lock**: `SELECT ... FOR UPDATE`, the active check, the identity link, the sign-in record and the session insert. Lock order users, then `user_identities`, then `sessions`, as on every other path. The early `is_active` check and its 403 stay; a deactivation caught under the lock gives the same 403 `account_disabled` and logs `google_login_rejected` (`account_disabled_meanwhile`).
    - **Contract (serialized on the user row):** sign-in first: a later reset or deactivation revokes its session. Reset first: the sign-in is a later, independent authentication and its session stays; every session that existed when the reset committed is revoked. Deactivation first: 403, nothing written. Refusing every Google sign-in that merely started before a reset was rejected: someone holding the Google account can sign in again a moment later, so it adds no protection.
    - **Tests** (`backend/tests/auth/test_google_races.py`, 7, deterministic: events and held row locks, synthetic accounts, the fake Google provider): sign-in then reset; a reset arriving while the sign-in holds the row (it waits, then revokes the new session); a reset completing during a sign-in (older sessions revoked, the later one stays); a sign-in waiting on an open reset transaction; a deactivation completing during a sign-in (real admin path) and an open deactivation transaction (403, no session, no link, no record); a failing session insert (link and record rolled back). **4 fail against the previous code** (the sign-in-holds-the-row order, both deactivation races, the rollback); the 3 reset-only outcomes pass on both and pin the contract the foreign key already gave.
  - **Invite acceptance session (follow-up: merged in #37, `b1edb2e`; not deployed):**
    - **The race:** `AuthRepository.accept_invite` consumed the invite, created the account (or gave a password-less one its password) and burned its reset links in one transaction, but the session was inserted afterwards in a separate one (`issue_session`). A reset whose link was requested after the acceptance committed could complete in that gap; its revocation ran before the invite's session existed, so that session outlived the reset. And a failing session insert left the invite consumed and the password set without a session.
    - **Fix:** the session insert moved into the acceptance transaction; the service hashes the password and prepares the session (token, CSRF, row) before it, and the repository fills in the accepted account's id. Lock order: `user_invites`, then `users` (from the upsert), then `password_reset_tokens`, then `sessions`; no path locks an invite row while holding a user row, so this cannot deadlock with resets, changes, sign-ins or deactivations. Token validation, expiry, revocation, password rules, the 400 / 409 answers and the cookies are unchanged.
    - **Contract:** acceptance first: a reset with a link from before the acceptance finds it burned (400); one with a link requested afterwards revokes the invite's session. Reset first (password-less account): the account has a password now, so the invite is refused (409) and stays unused. Two acceptances of one invite, or two invites for one account: exactly one succeeds, the other changes nothing. Any failure: no consumed invite, no account or password, no session.
    - **Tests** (`backend/tests/auth/test_invite_races.py`, 8, deterministic: events and held row locks, synthetic accounts, invites created directly in the scratch database): a reset right after the acceptance (the proven race); a reset after a completed acceptance; a reset arriving while the acceptance holds the account row (waits, older link refused, the invite session stays); an acceptance waiting on an open reset (409, invite unused); the same invite accepted twice at once; two invites for one password-less account; a failing session insert for a new and for a password-less account (nothing left behind). **3 fail against the previous code** (the proven race and both failure cases); the 5 ordering and concurrency tests pass on both and pin the behaviour the existing transaction already gave.
    - **Deactivated accounts (follow-up: merged in #38, `e346750`; not deployed):** an invite for an existing *deactivated* password-less account used to set its password, consume the invite, burn its reset links and issue a session that `resolve` refused only on first use, unlike every other sign-in path. Now the upsert's condition is `password_hash IS NULL AND is_active`; PostgreSQL locks the conflicting row even when that condition fails, so the status is read under the account-row lock inside the #37 transaction (same lock order). A password-less inactive account gets **403 `account_disabled`** ("This account is disabled", the existing answer of Google sign-in for disabled accounts; logged as `invite_rejected`), and the transaction rolls back: invite unused, no password, reset links and sessions untouched, `is_active` unchanged. An account that has a password still answers 409 `account_exists`, active or not. Disclosure: only the holder of the invite (sent to that address) can see it, and the 409 already tells that holder the account exists. **Tests** (`test_invite_races.py`, 4 new, plus 1 API test in `test_auth_flow.py`): the refused invite changes nothing (password, verification, links, sessions, `is_active`, invite); an acceptance waiting on an open deactivation is refused once it commits; a deactivation (real admin path) arriving during an acceptance waits and then revokes the invite's session and burns its links; an inactive account with a password still gets 409; the API answers 403 with no cookie. **The 3 refusal tests fail against the previous code**; every other invite, race and admin test passes on both.
  - **Links in the URL:** reset links still travel in the URL query (F4, frontend).

### F11 (P3, merged in #32, `9a33fe8`; not deployed): the forgot-password endpoint answered faster for unknown emails
- **Evidence:**
  - `AuthService.request_reset` returns at once for an unknown or inactive email.
  - For an existing active account it first inserts a token and **awaits the email send** (`self.sender.send`, an HTTPS call to Resend when email is enabled).
  - The response is the same 202, but the latency differs.
- **Impact:** once email delivery is enabled (D8), the response time can reveal which emails have accounts. The per-email (3 per hour) and per-IP (10 per hour) limits slow but don't stop this. Email is `disabled` on DEV and production today, so it's **not exploitable yet**.
- **Fix (merged in #32, `9a33fe8`; not deployed):**
  - `POST /auth/password/forgot` now does only the per-IP and per-email rate limits before answering. That work is the same for every email.
  - The account lookup, the reset token and the email run in `AuthService.send_reset_link`, scheduled with FastAPI `BackgroundTasks`. Starlette runs it **after the response body has been sent**, inside the API process (not a separate worker or queue). No new dependency.
  - **Unchanged:** the 202 and its body, the 429 over the per-IP limit, the per-email limit (silently no link), token hashing, 60-minute expiry, single use, and the `password_reset_requested` event (now logged when the link was actually sent).
  - **Failures:** an exception while looking up the account, storing the token or sending is caught and logged as `password_reset_email_failed` (level ERROR, exception class name, user id, client IP). The email address, token, link and exception message are never logged; the caller already had its 202.
- **Tests** (`tests/auth/test_forgot_password.py`):
  - unknown, inactive and active accounts get the same status and body; only the active one gets a link;
  - **response before delivery:** the ASGI app is driven directly on the test client's event loop with a sender that blocks; the complete 202 body is sent while the sender is still blocked (no timing thresholds);
  - the per-email and per-IP limits, single use and token storage still hold;
  - a failing sender: same 202 and body, one ERROR event, no email address, token or exception text in the logs;
  - a successful request logs the event but never the token or the email address.
- **Regression proof:** the response-before-delivery and failing-sender tests **fail** against the previous code.
- **Limitations:**
  - **Not a durable queue:** the task lives only in the API process's memory. A link whose send fails, or whose process is stopped or restarted before the task finishes, is never sent; the user asks again. The unused token row expires normally. Since the follow-up below, a restart no longer cuts a send midway: each run ends within 15 s and systemd waits 20 s (`TimeoutStopSec`) before killing the process.
  - **Rate-limit path:** the per-email limit row is still written for every email (the same work), and over the per-IP limit every email gets the same 429.
  - **Concurrency (follow-up: merged in #39, `7685425`; not deployed):**
    - **Topology:** each environment runs one API unit with `uvicorn --workers 2` (two processes; DEV port 8000, production 8001) behind Nginx. Production has no `hyperchat-worker` unit; the job worker runs on DEV only. Before this, the per-IP (10/h) and per-email (3/h) limits apply per key, so many IPs and addresses each started a task, and nothing capped the total in flight; each send was bounded only by httpx's per-step 10 s timeout.
    - **Fix:** `AuthService.send_reset_link` takes one of `RESET_EMAIL_SLOTS` = 4 per process (`asyncio.Semaphore`) and **sheds** the request when all are busy instead of queueing it: nothing looked up, no token, nothing sent, `password_reset_email_shed` logged (reason only, client IP). Each run (lookup, token, send) ends within `RESET_EMAIL_DEADLINE_SECONDS` = 15 (`asyncio.timeout`; logged as `password_reset_email_failed` / `TimeoutError`). The response is unchanged: shedding happens after it, so the generic 202, its timing and the per-IP / per-email limits are untouched.
    - **Is it global?** The cap is per process; the bound for an environment is workers x slots = 2 x 4 = 8 sends in flight, and memory holds no backlog. `tests/test_deploy_production.py` pins it: raising `--workers` or the cap above 8, or a deadline not below `TimeoutStopSec`, fails CI and forces a review.
    - **Failure behaviour:** provider errors and timeouts free their slot; no retry (the user asks again, within the per-email limit). During a flood, a genuine request can be shed and its per-email attempt is still counted.
    - **Tests:** `tests/auth/test_reset_dispatch.py` (7, no database, events only): saturation sheds without lookup, token or send; a 50-request burst never exceeds the slots; provider failures free their slots and log no address, token or link; a provider that never answers is cut off at the deadline; two service instances (the two workers) each enforce their own cap, total = workers x slots; the defaults; an unknown account uses a slot briefly. Plus one HTTP test (saturated process: same 202 and body, no token, no email) and the deploy bound. Against the previous code the saturation, burst, deadline, per-process and default tests and the deploy bound fail (the saturation test hangs: nothing sheds).
    - **Not done (needs an owner decision):** a durable, globally coordinated queue. The Phase 8 `jobs` table would need a migration (its kind CHECK), a payload rule (ids only, the token created at send time) and a production `hyperchat-worker` unit, which doesn't exist yet. It would add retries and survive restarts; for reset links, which the user can request again, the in-process bound above is proportionate while email is disabled. Revisit before enabling email if delivery guarantees are wanted.

### F12 (P3, merged in #34, `c53a8c9`; not deployed): bcrypt hashes of any cost were verified at sign-in
- **Evidence (found while reviewing the D1 importer, PR #34):** `verify_password` handed any `$2a$`/`$2b$`/`$2y$` value to `bcrypt.checkpw`. Each cost step doubles the work: on the 2-vCPU VPS cost 10 takes ~52 ms, 12 ~213 ms, 13 ~423 ms, and cost 31 would take ~30 hours (Argon2id: ~90 ms).
- **Impact:** a stored hash with a very high cost would make every sign-in attempt for that account tie up a hashing thread for hours. No such hash could exist before the importer (V2 writes only Argon2id and the ETL loads no hashes), so nothing was exploitable; the importer would have been the way in.
- **Fix:** `backend/app/auth/bcrypt_format.py` is the one definition of an accepted bcrypt hash: `$2a$`/`$2b$`/`$2y$`, cost 04-12 (`MAX_BCRYPT_COST`; GoTrue writes 10), `$`, then exactly 53 characters of bcrypt's base64 alphabet, 60 ASCII characters in all. The sign-in verifier hands only such hashes to bcrypt (anything else: one dummy Argon2 check, generic 401), and the importer loads the same function (anything else is `unsupported_hash_format` and blocks `--apply`).
- **Tests:** costs 04-12 accepted (real hashes verified at 04 and 12); costs 00-03 and 13-99, non-ASCII digits, invalid body characters, broken separators and wrong lengths never reach `bcrypt.checkpw` (monkeypatched); sign-in with a stored `$2a$31$` hash answers the generic 401 without bcrypt work; the importer refuses such hashes before its confirmation prompt and writes nothing.
- **Limitation: see F12-R** (accepted residual risk, below). The cost cap above is remediated; the sign-in timing
  difference for imported bcrypt hashes is **not** remediated and was accepted by the owner.

### F12-R (P3 residual of F12): imported bcrypt hashes answer faster. **Accepted residual risk, not remediated**
- **Owner decision (2026-10-11):** accept the temporary sign-in timing difference associated with imported bcrypt
  password hashes during the bcrypt-to-Argon2id migration. **No timing equalization is implemented.** This entry
  records the decision; it does not claim the risk is removed.
- **Scope (narrow):** a **wrong-password** attempt against an account whose stored hash is still an **imported bcrypt**
  hash (GoTrue cost 10, ~52 ms on the 2-vCPU VPS) can answer faster than an attempt for an **unknown email address**
  (one dummy Argon2id verification, ~90 ms). Measured once on synthetic hashes (F12 evidence). Accounts whose hash is
  Argon2id, and unknown addresses, are not affected by this difference; nothing else about sign-in changes.
- **What it can reveal:** that an email address belongs to a migrated account that has not signed in to V2 since the
  import. It reveals nothing about the password. F2's per-IP and account-at-IP throttling limit how many attempts can
  be timed.
- **How it shrinks:** a **successful** sign-in with the correct password re-hashes the password to Argon2id
  (`needs_rehash`, written in `AuthRepository.password_sign_in`), which removes this bcrypt-versus-Argon2id difference
  **for that account**. A password reset or an invite acceptance also replaces the hash with Argon2id. Accounts are
  **not** upgraded automatically: an account whose owner never signs in, resets or accepts an invite keeps its bcrypt
  hash.
- **Dormant accounts:** a migrated account that is never used again **may keep this timing difference indefinitely**.
- **Why accepted:** equalizing it would add one dummy bcrypt (~52 ms) to every non-bcrypt verification, or pad every
  verification to a fixed floor: about 52 ms and ~58% more hashing CPU per sign-in attempt for every user, which works
  against F9's hashing bound, to hide a difference that reveals account existence only, for a shrinking set of accounts.
- **Current count of imported bcrypt hashes: 0.** No real import has happened: the importer (#34) has never been run
  against real data, and the ETL loads no password hashes (`database/etl/reconcile.py` asserts `users: no password hashes
  migrated`). Last recorded aggregate on DEV: 0 of 137 users have any password hash (read-only count, 2026-10-10/11).
  Production was not queried for this (not permitted). After an approved import, the operator can obtain the remaining
  count as a single aggregate, with no emails or hashes, using the query already documented for rollback in
  `database/README.md` (`SELECT count(*) FROM public.users WHERE password_hash LIKE '$2_$%'`); V2 itself writes only
  Argon2id, so that count is exactly the not-yet-upgraded imported hashes.
- **Re-evaluate:**
  - **before the production cutover** (with the import run, D1);
  - **before the bcrypt migration is considered complete** (decide what happens to dormant accounts that still hold
    bcrypt hashes, e.g. leave, force a reset, or clear them; that is a separate owner decision);
  - **if the timing behaviour or the password-hash upgrade flow changes** (e.g. a different bcrypt cost cap, a change to
    `verify_password` / `needs_rehash` / the dummy verification, or to where the rehash is written).

## Delta review (2026-10-11, `main` at `7685425`)
Everything merged since the audit baseline `bfb9a1e` (#24-#39: F1-F3, F5, F6, F8-F12 and the F10 follow-ups, the
D1 tooling) was re-checked with the audit's method. Nothing is deployed: DEV still runs `9042fb7fff40`, production
`940137070657`.
- **Routes:** no route was added or removed since `bfb9a1e` (no router decorator changed); the only router changes
  are the F5 health trim and the F11 forgot-password route. The inventory below (82 routes) still holds.
- **Authentication paths:** every path that sets a password, changes `is_active` or issues a session now runs under
  the user-row lock in one transaction (password sign-in, Google sign-in, invite acceptance, reset, change,
  deactivation, bootstrap), with one lock order; each has deterministic race tests (`tests/auth/test_password_races.py`,
  `test_google_races.py`, `test_invite_races.py`). Reset-email work is bounded per process (F11 follow-up).
- **Static analysis** (Bandit 1.9.4, local, no network; `backend/app` and, new in scope, `database/etl`):
  - `backend/app`: 63 results (65 at the baseline: the removed lockout code and the F8 escapes); every one is a
    line-shifted repeat of a result already classified in the baseline (column lists or SQL identifiers with bound
    parameters, the Pusher protocol's MD5 body hash). No new class.
  - `database/etl` (not scanned before): B608 on SQL built from module constants and `psycopg.sql.Identifier`
    (false positives, no user input), B105 on the column name `razorpay-keysecret` (false positive), and **B101 on
    three `assert` statements used as safety guards: F13**.
- **Dependencies:** no pin changed since the 2026-10-10 scan, so it was not re-run (re-run before cutover, as planned).
- **Still open:** F4 (frontend), F7 (after D4 / SEC-L2), and every credential-dependent control in "Not verified".
  The F12 timing residual is an **accepted residual risk** (F12-R, owner decision 2026-10-11), not remediated.

### F13 (P3, found in the delta review; merged in #40, `06b884b`; not deployed): safety guards written as `assert`
- **Evidence:** the D1 tools' "counts only" output guards (`auth_readonly_report.render`, `auth_hash_import.render`)
  and the importer's accounting invariant (eligible + exclusions = rows with a legacy id) were `assert` statements.
  `python -O` (or `PYTHONOPTIMIZE`) removes asserts, so those checks would silently disappear.
- **Impact:** low. The documented command runs `python -B -E -s`, so `-O` is not used and `-E` ignores
  `PYTHONOPTIMIZE`; the guards held in practice. But a fail-closed check must not depend on how Python is started.
- **Fix:** the three asserts are now explicit checks: the renderers raise `ValueError` on any non-count value, and a
  plan whose counts don't add up raises `Refused` ("nothing was written") before anything can be applied. Found while
  validating this: both tools' `main()` caught every `ValueError` as "a port must be a number" (the importer would
  have mislabelled a counts-only failure that way, exit 2), and the report let a renderer failure escape as a
  traceback. Ports are now parsed explicitly (`Refused`: "<prompt> must be a number", ASCII digits only), the broad
  handlers are gone, and a counts-only failure ends as `failed: ...` with exit 1 in both tools, never printing the
  value.
- **Tests (6):** both renderers refuse a non-count in a subprocess started with `python -O`; a miscounted plan is
  refused; through each tool's `main()`, a non-count summary exits 1 with a clear message and without the value; a
  non-numeric (or non-ASCII-digit) port is refused by name. All six **fail against the previous code** (`7685425`):
  under `-O` the leak goes through, the invariant raised a bare `AssertionError`, and the messages were wrong.
- **`python -O` probe (all guards, synthetic secret):** with asserts disabled (`sys.flags.optimize == 1`), both
  renderers raise `ValueError` ("... holds counts only"), the miscounted plan raises `Refused`, the importer's `main()`
  exits 1 (`failed: ValueError`) and the report's exits 1 (`failed: the report holds counts only`); the secret
  appears in no message or output.

### F14 (P2, found in the local GoTrue rehearsal; merged in #47, `2c4b58b`; not deployed): row-level security silently filtered the D1 source
- **Evidence:**
  - Supabase's PostgreSQL 17.11 image enables row-level security on `auth.users` (and every `auth.*` table) with
    **no policies**. The legacy project is unverified.
  - A source role without `BYPASSRLS` therefore read zero rows, with no error.
  - The D1 report and importer checked columns and privileges but not row visibility.
  - **Locally:** the report counted every V2 account as missing in Supabase. The importer's dry run planned
    nothing and printed the fingerprint of an empty set.
- **Impact:**
  - **With Supabase's no-policy default:** a wrong reviewed dry run, but no wrong write. A plan with no updates
    imports nothing, and under the lock the empty set's fingerprint would match only an equally filtered source.
  - **With a policy that hides only some rows:** a wrong import. The excluded-account rules depend on seeing every
    source row. In the fixture, hiding one Supabase account that shares a V2 account's email (another Supabase id)
    made that V2 account eligible, and the old code planned its hash for import. The approved exclusion policy
    forbids that import.
- **Fix:**
  - Both tools read with `SET LOCAL row_security = off` in every source transaction (plan, report, and the lock
    transaction). PostgreSQL then raises instead of filtering whenever any policy would apply to the role: none,
    some or all rows visible, and the owner under FORCE.
  - The shared column check then probes the table by selecting one column it has just verified as readable
    (`SELECT id ... LIMIT 0`, no rows read; column-level grants are enough, as for the documented least-privilege
    role) and refuses with `row-level security on auth.users would hide rows from this role` (exit 2). The target is
    checked the same way.
  - Under the lock, a policy turned on after the plan raises `InsufficientPrivilege` (exit 1); nothing is written
    and the lock is released.
  - **Not a privilege:** verified on PostgreSQL 17.11, the setting never shows a restricted role a row. That role
    gets the error instead. Roles that RLS doesn't apply to read every row: `BYPASSRLS`, superusers, and the owner
    without FORCE.
- **Operator change:** the source role needs `BYPASSRLS` (`database/README.md`).
- **Tests:**
  - **10 new PostgreSQL tests** (RLS on the fixture's `auth.users`; no policy, a policy hiding one row, a policy
    showing all rows):
    - the documented column-grant-only source role (no table-level `SELECT`): every row read with RLS disabled or
      with `BYPASSRLS`, and refused under no policy or a partial policy (report and importer);
    - the report refuses, and the target is checked too;
    - the importer's plan refuses;
    - the CLI dry run and `--apply` exit 2, with nothing written and nothing sensitive printed;
    - a `BYPASSRLS` role gets the exact counts and the full fingerprint and imports;
    - RLS turned on while the operator confirms: nothing written, lock released;
    - target RLS refused before planning and before writing.
  - **8 of the 10 fail against the previous code** (`dab3d96`). The two that pass are positive controls.
  - **Review of #47 (Codex P1):** the first version probed with a zero-column `SELECT FROM ... LIMIT 0`, said to
    need table-level `SELECT`. On PostgreSQL 17.11 it did not: a query that references no column needs `SELECT` on
    any one column, and the column-grant-only role passed. That was verified directly, and by these tests passing
    against that version. The probe now selects a verified column anyway, so it depends on nothing but that grant.
    A deliberately broken probe (`SELECT *`, which needs every column) makes the column-grant tests fail
    (`permission denied for table users`), so they would catch the case the review describes.

### F15 (P2, found in the review of #48; merged in #49, `2c3efc7`; not deployed): D1 import recovery could mislead the operator
- **Evidence** (importer on `main` at `2c4b58b`):
  - **Ambiguous COMMIT:** a target COMMIT that took effect on the server but whose answer was lost (connection
    dropped) reached the generic error handler. It printed "nothing was written by this run unless APPLIED was
    printed" and exited 1, although the import had committed. Reproduced by the new test: the old code exits 1.
  - **No record of what a run wrote:** the only documented rollback cleared every bcrypt-looking hash.
  - **Upgraded accounts invisible:** an imported account upgraded to Argon2id by a V2 sign-in could not be told apart,
    because that upgrade sets `last_login_at` but not `password_changed_at`.
- **Impact:**
  - A committed import reported as not written could be followed by unfreezing legacy or a re-run, and the re-run
    keeps stale hashes because the importer never overwrites.
  - After exit 4 or a fingerprint mismatch, the broad rollback could clear hashes another run wrote, and missed
    accounts upgraded after the import.
- **Fix:**
  - **Ledger:** each run writes `public.d1_hash_import_ledger` (migration 0017) in the same transaction as the hashes,
    under an `import_run_id` printed before writing. A run's rows exist if and only if it committed.
  - **COMMIT outcome:** a COMMIT error is classified. A server-reported error on a live connection is a confirmed
    rollback (exit 1, as before). Anything else (no SQLSTATE, a broken connection, an interrupt) gives a new
    **exit 5**, "the import MAY HAVE COMMITTED", which never claims either outcome.
  - **Operator SQL:** `import_status.sql` settles the outcome and lists accounts changed since the run.
    `rollback_import.sql` clears only hashes still exactly as that run wrote them (SHA-256 match) and never touches
    the changed accounts.
  - **Runbook:** "Recovery after `--apply`" in `docs/operations/d1-auth-freeze/README.md`.
- **Tests** (real PostgreSQL 17):
  - the ledger records exactly what was written;
  - no ledger privilege means nothing is written;
  - a lost COMMIT acknowledgement: exit 5, the "MAY HAVE COMMITTED" wording, never "nothing was written", and the
    ledger settles it;
  - control: a COMMIT the server rejects means exit 1 and nothing written;
  - exit 4 with an unchanged source: the import is kept;
  - the full exit-4 recovery: a fingerprint mismatch, an account upgraded after the import, a rollback that clears
    only that run's unchanged hashes (twice, idempotent), the old fingerprint refused on retry, and the retry
    succeeding only with a freshly reviewed one;
  - migration 0017 round trip, privileges and constraints, with both operator SQL files run against the real schema.

  All 8 new or changed importer tests fail against the importer on `main` before #49.

## Verified controls

| Area | Control | Evidence |
|---|---|---|
| Client IP | Rate limits key on the real client IP; a forged `X-Forwarded-For` cannot change it | uvicorn 0.54 `--proxy-headers --forwarded-allow-ips 127.0.0.1` behind Nginx (`$proxy_add_x_forwarded_for`); experiment: `"1.2.3.4, 203.0.113.9"` resolves to `203.0.113.9` |
| Sessions | Random 32-byte token, stored hashed; cookie `Secure`, `HttpOnly`, `SameSite=Lax` (`None` refused in production); idle 7 d / absolute 30 d; inactive users' sessions revoked on use | `auth/dependencies.py`, `auth/service.py`; auth tests |
| CSRF | Every unsafe method with a session needs an allowlisted `Origin` **and** the session's HMAC CSRF token (constant-time compare) | `enforce_csrf`; tests in every dashboard / admin suite |
| Login | One generic error; the same hashing work for unknown or locked accounts; per-IP limit; Argon2id with rehash | `AuthService.login`, `auth/passwords.py` |
| Password reset | Per-IP and per-email limits; no account enumeration; token hashed, single use, 60 min; all sessions revoked after a reset | `request_reset`, `reset_password` |
| Google sign-in | Authorization code + PKCE + state (HMAC-signed cookie, 10 min) + nonce; RS256 with audience and issuer checks; `email_verified` required; only existing accounts; redirect limited to a path on the frontend origin; account check, identity link, sign-in record and session in one transaction under the user-row lock (merged in #36, not deployed) | `auth/google.py`, `routers/auth.py`, `AuthRepository.google_sign_in`; `tests/auth/test_google_races.py` |
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
| Signups | Trusted `Origin`, 5 per IP per hour, strict field rules, identical answer for repeated emails (D14, resolved 2026-10-15) | `routers/signups.py`; signup tests |
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
| Migrated-user password import (D1) | Importer merged (#34) and tested with synthetic hashes only; never run against real Supabase data, no hash imported, migrated sign-in not verified. Policy approved 2026-10-12 (import at cutover; not in DEV) | Before cutover: resolve account-specific identity conflicts and schedule the maintenance window. During cutover: freeze legacy password mutations, drain in-flight changes, snapshot, report, reviewed dry run, explicit approval for `--apply`, reconciliation and migrated sign-in verification; use the documented abort/rollback procedure if cutover stops. The freeze SQL and the importer's fingerprint / freeze guard (merged in #45, `f87e1ab`, with the post-merge follow-up merged in #46, `dab3d96`: after the confirmation `--apply` takes `SHARE` on `auth.users`, verifies the freeze, fingerprint and every planned hash under it and keeps it until the target has committed, so no source row can change in between) are tested on plain PostgreSQL 17 and were rehearsed against GoTrue v2.197.0 on a **local** Supabase stack only (2026-10-12, `docs/operations/d1-auth-freeze/README.md`: freeze, drain, GoTrue paths, lock behaviour and pauses as documented; finding F14 fixed in #47, the ENABLE ALWAYS re-run downgrade in #48 (`7399cab`); F15 import recovery, a per-run ledger and run-scoped rollback, in #49 (`2c3efc7`; migration 0017, not deployed)); hosted permissions are unverified. **Residual:** the dashboard role `postgres` could set `session_replication_role = replica` and write past the plain-enabled trigger, and cannot make it ENABLE ALWAYS (locally; open residual risk); a superuser, or the dashboard role `postgres` (a member of `pg_signal_backend`; it did so in the local rehearsal), can end the importer's source session (then: rolled back, or exit 4 if in the last instant, detection only), replace the trigger function during the lock, or bypass the freeze after the release; step 10 of the procedure (a fresh dry run before switching users) is the last detection point; operator controls are required for the window; the lock needs MAINTAIN (PG 17+) or a writable role, unverified on the legacy project, and pauses legacy sign-ins and token refreshes while held (2.8 s / 9.8 s for a 3 s / 10 s hold locally, no GoTrue error) |
| Frontend | Not built | Frontend phase; F4 is its first requirement |
| Media URLs | Public to anyone holding the URL (uuid4 key, no public listing), as legacy; a leaked URL stays valid while the object exists | **Accepted by the owner (D9, 2026-10-15)**; uploads that never became a donation are deleted after 24 hours (PLAT-11); paid media is kept for replay |

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
- **Status: merged in #27 (`0c6f03b`); no runtime change, so nothing to deploy beyond the next release.**
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

## Anonymous exposure review (2026-10-15)

All 90 routes were reviewed for data returned without any credential (CORS and rate limits are not authentication).
Nothing secret was anonymous (no ids, emails, tokens, order or payment data). Three minimum-exposure problems were
fixed (owner decisions 2026-10-15: the donor page keeps the goal bar but not the leaderboard; OBS and dashboard
settings are never anonymous; tokenless legacy goal-overlay URLs keep working):

| Finding | Was | Now |
|---|---|---|
| EXP-1 (P2) overlay settings | `GET /api/v1/public/streamers/{slug}/overlay-config` returned overlay template ids, `alert_box_scale`, `overlay_adjust_mode`, leaderboard flags, `goal_activated_at` for any active creator; its only consumer (the alerts overlay) always has an OBS token, and the realtime counterpart was already private | route removed; `GET /api/v1/obs/overlay-config` (OBS token, the token's creator, the token's channel scope): exactly the `settings-updated` fields |
| EXP-2 (P2) donor leaderboard | `GET …/{slug}/leaderboard` returned the top donor's name and lifetime total for any active creator | route removed; `GET /api/v1/obs/leaderboard` (OBS token, the token's creator): `{enabled, top_donor}`; `leaderboard_widget_enabled` left the public profile |
| EXP-3 (P3) creator-status oracle | `GET /api/v1/realtime/config` answered 200 for `pending` / `rejected` creators and 404 for unknown ones | active creators only; identical 404 otherwise |
| EXP-4 (P3) goal | name and target served while no goal was active | every field but `active` is `null` while inactive; `brand_color` and `goal_overlay_id` added so the tokenless goal overlay needs nothing else |

The public `{slug}-goal` channel already skipped non-active creators (renderer); now covered for `pending` and
`rejected`. Platform metrics stay public (owner decision).

## Creator applications (KYC PR2, migration 0021)

Admin-only review of the public signup form; the form's public contract (Origin, rate limit, identical answer, no account) is unchanged and every review field is server-controlled. Approval creates a pending creator in the same locked transaction (no duplicates under concurrency). Application invitations are bound to the application, its creator and the applicant's email; acceptance claims the creator only while unclaimed (row lock, `user_id IS NULL`), never transfers, never takes over an existing account (explicit admin linkage instead) and never activates. Resends revoke the previous link; tokens are stored hashed and never logged or kept in the history. Lock order stays invite -> creator -> users. Database-enforced: an application invite keeps its application binding (foreign key RESTRICT + immutable `signup_id`), and the review history is append-only for the runtime roles and cannot be erased by deleting its application (RESTRICT); only the schema owner (synthetic test cleanup) removes both.

## Creator lifecycle and donation eligibility (KYC PR1, migration 0020)

New creators start `pending` (column default too) and only an admin activates them; activation needs the owner's approved KYC, which does not exist yet, so it is refused (fail closed). `public.creator_donation_block(streamer_id)` (migration 0020) is the one rule for NEW donations and donor uploads: the creator is `active` and, until KYC exists, on the legacy track with its baseline owner still its owner; checkout, upload grants and uploads call it (a generic `503 {"error": "Donations are temporarily unavailable"}`, never the reason) and a `donations` insert trigger repeats it under a share lock on the creator row for every runtime-role insert, so no path can skip it. Payment readiness stays the existing checkout check (it may call Razorpay). Existing orders, their status / confirm / webhook completion and historical donations are never checked. The runtime roles cannot set `kyc_track` / `kyc_baseline_owner_id` (trigger) or edit the status audit (privileges). Owner decision 2026-10-15: an unverified new owner never accepts new donations, also before the rollout. No permanent KYC exemption exists: `legacy` only means "not yet enrolled"; the KYC PRs add verification, the audited rollout and expiring grants.

## Tracked decisions and approvals (unchanged by this audit)

| ID | Item | State |
|---|---|---|
| D14 | Repeated signup email: identical answer (current) or legacy message | **RESOLVED by the owner on 2026-10-15: KEEP IDENTICAL RESPONSE**: a repeated email (any letter case) gets the same `201` as a new signup, nothing is inserted or changed, no duplicate indicator; prevents email / account enumeration; existing code and tests satisfy it, no code change |
| D15 | Legacy Media Source URLs on `*.supabase.co` | recommendation in `docs/obs-cutover.md`; awaiting owner (cutover choice, not backend product work) |
| SEC-L2 | `RazorpayClient.fetch_order` (unused) | delete or keep; awaiting owner (P3 internal cleanup, no API effect; not a blocker) |
| PAY-11 | Stale smoke assertions | verified on DEV 2026-10-14 (Phase 6 29/29, Phase 7 30/30) |
| D4 / SEC-L1 | Partner API and the unused HMAC / partner-key layer | **resolved 2026-10-13** (keep ClipWave); SEC-L1 layer removed, verified on DEV |
| D5 | Discord link and contact form | **resolved 2026-10-15**, **verified on DEV** 2026-10-15: contact form retired (no submission API); static contact details and the Discord invite are intentionally public settings (`HYPERCHAT_PUBLIC_*`), validated at startup and served only through `GET /api/v1/public/config` with a fixed field allow-list |
| D9 | Media access model | **resolved 2026-10-15**, PLAT-11 **verified on DEV** 2026-10-15: keep public unguessable media; abandoned uploads deleted after 24 hours; production on a configured custom media domain; legacy URL prefixes rewritten by the ETL |
| D1 | Migrated-user credentials | **policy approved 2026-10-12**: import eligible Supabase bcrypt hashes at the production cutover with the existing importer and its safeguards; no real hashes in DEV without separate approval; excluded users recover by reset or Google only where configured and live-verified; legacy confirmation fields not mapped to `email_verified_at`. Policy only: no import, data access, external service, deployment or cutover authorized. Exclusion policy and password-change cutoff procedure approved 2026-10-12 (policy only); account-specific conflict resolution and the scheduled maintenance time remain pending. Actual cutover still requires separate explicit approval |
| F12-R | Sign-in timing difference for imported bcrypt hashes during the migration | **accepted by the owner 2026-10-11**; accepted residual risk, not remediated; re-evaluate before cutover, before the bcrypt migration is considered complete, and if the timing or upgrade flow changes |

## Remediation plan

1. **F1** (P1): **merged in #24** (`aa76d78`; not deployed). **F1-R** (P3, residual): **mitigated, merged in #24** (not deployed):
   - **Token paths:** they log errors only at `crit`, and the access log carries the upstream status and timing.
   - **DEV port 80:** logs without the query.
   - **Remaining:** only `crit`-level lines and the negligible bootstrap case; see F1-R above.

   Original plan: production log format without query strings + CI assertion. Small, configuration only; deploying it
   to production needs approval.
2. **F3** (P2): limit current-password checks: **merged in #25** (`dc4c7d8`; not deployed), policy approved 2026-10-11 (5 per user per hour, session kept, 429 until reset, weak new passwords not counted).
3. **F2** (P2): **merged in #29** (`1c018a7`; layered IP and account-at-IP throttling, policy approved 2026-10-11; not deployed).
3a. **F9** (P1): **merged in #30** (`4a30786`; no hashing over the IP limit; hashing off the event loop; not deployed); must ship together with or before F2.
3b. **F10** (P3): **merged in #31** (`3b834d8`; reset links burned whenever a password is set or the account is deactivated; not redeemable for inactive accounts; atomic reset, compare-and-set change, race-safe password sign-in sessions; not deployed). **F11** (P3): **merged in #32** (`9a33fe8`; reset email sent after the response via `BackgroundTasks`; failures logged without email or token; not deployed).
3c. **F12** (P3): **merged in #34** (`c53a8c9`; one shared bcrypt format check, cost 04-12, for sign-in and the importer; not deployed).
4. **F6, F5** (P3): production guard for invite links; trim the public health response: **merged in #28** (`ca76614`; not deployed).
5. **Dependency scan:** done 2026-10-10, no known vulnerabilities. **F8** (P3): write the literal bidi characters as `\u` escapes and add a CI check that rejects literal bidi characters in source (no behaviour change). Re-run the scan whenever pins change and before cutover. **F8: merged in #27** (`0c6f03b`; `\u` escapes, behaviour pinned over all code points, repository-wide CI check).
6. **F4**: carried into the frontend phase as a requirement.
7. **F7**: after D4 and SEC-L2.
8. **F13** (P3): **merged in #40** (`06b884b`; explicit checks instead of `assert`; not deployed).
9. **F12-R** (P3 residual): **accepted by the owner 2026-10-11**, not remediated; no timing equalization. Re-evaluate before cutover, before the bcrypt migration is considered complete, and if the timing or upgrade flow changes.
10. **F14** (P2): **merged in #47** (`2c4b58b`; not deployed): the D1 tools read with `row_security = off` and refuse a source (or target) filtered by row-level security. Before any real dry run, give the Supabase source role `BYPASSRLS`.
11. **F15** (P2): **merged in #49** (`2c3efc7`; not deployed): a per-run import ledger (migration 0017), exit 5 for a COMMIT with no reliable answer, and run-scoped status and rollback SQL. Before any real `--apply`, migration 0017 must be deployed to the target database.

## Evidence: route inventory and test runs

All 82 routes of `create_app()` (the dependency chain is what FastAPI resolves before the handler):

- **Session required** (`require_user`, CSRF on unsafe methods): `/api/v1/auth/{logout, logout-all, password/change}`,
  `/api/v1/me/*`.
- **Admin** (`require_admin`): `/api/v1/admin/*`, `POST /api/v1/auth/invites`.
- **Creator access** (`require_streamer_access`): every `/api/v1/dashboard/streamers/{streamer}/…` route and
  `/api/v1/streamers/{streamer}/access`.
- **OBS token** (`require_obs_token`): `/api/v1/obs/{session, overlay-config, leaderboard, audio/claim, audio/next}`
  (the creator comes from the token).
- **Trusted Origin + per-IP limit** (`_guard`): signups, upload grant and uploads, checkout, payment status and confirm.
- **Checked inside the handler:** login, password forgot / reset, invite accept (Origin), Google start / callback
  (state), the three webhooks (signatures), Pusher auth (session or OBS token).
- **Public by design:** `/health`, the donor-page reads under `/api/v1/public/` (profile, pricing, soundboard, goal,
  hypersounds, config) and `/api/v1/metrics/` (Nginx per-IP limit; not authentication), `/api/v1/realtime/config`
  (active creators only), `/obs/player*` (static; the token stays in the fragment). See "Anonymous exposure review".

Test runs on `bfb9a1e`, 2026-10-10:
- Disposable PostgreSQL 17 (created and dropped on the VPS): database **47 passed**; backend **4,511 passed, 0 failed**.
- pyflakes over `backend` and `database`: one warning (unused variable, `soundboard/audio.py:107`), no security
  relevance.
- GitHub CI on `bfb9a1e`: green.
