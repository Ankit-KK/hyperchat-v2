# Cross-component tests

Unit and integration tests live next to the code they test:
- `backend/tests/` - FastAPI app, authentication, security building blocks, public and dashboard APIs
- `database/tests/` - Alembic migrations, role privileges, ETL mapping rules

This directory holds checks that span components or run against a deployed environment.
CI (`.github/workflows/ci.yml`) runs the backend and database suites; `.github/scripts/setup-test-db.sh`
prepares the throwaway PostgreSQL it uses.

| Path | What it checks |
|---|---|
| `e2e/nginx_ratelimit_check.sh` | the public-API rate-limit snippets in a throwaway local nginx (burst, JSON 429, recovery, other paths unaffected) and a syntax check of the site files; fully isolated in a temp directory (pid, logs, all temp paths) and fails if the host nginx's state directories change owner/mode (CI also proves this on the Ubuntu 26.04 nginx build) |
| `e2e/smoke_auth.py` | a deployed API over public HTTPS: login, cookie flags, CSRF/Origin enforcement, session revocation, forgot-password, disabled Google, CORS, no secrets in the service journal |
| `e2e/smoke_dev_backend.py` | the whole deployed DEV backend over public HTTPS: platform (health, release, units, migration head, 0016/0017 schema and privileges), auth lifecycles and throttles, admin/invites/onboarding, role authorization, dashboard and moderation, worker jobs, realtime, OBS, public APIs, payment refusals without provider calls, the Custom Soundboard terms gate (D3), the ClipWave partner API with a synthetic DEV-only key (D4), the one-Razorpay-account model without any Razorpay call (D6), bots disabled, no secrets in the logs, verified cleanup; lists the checks it cannot run as BLOCKED (including a D3/D4/D6 section the deployed release does not have yet) |

`e2e/smoke_auth.py` runs on the VPS (see its docstring); it creates one synthetic
`@authtest.invalid` account in the target database and removes it afterwards. It expects the secure
default `SameSite=Lax`; against the v2 dev API (cross-site development, `SameSite=None`) run it with
`HYPERCHAT_SMOKE_SAMESITE=none`. The frontend origin it signs in from is `HYPERCHAT_SMOKE_ORIGIN`
(default `https://app.quicktestspace.site`, the origin the dev API trusts); the production origin is
used only against the production API at cutover.

`e2e/smoke_dev_backend.py` also runs on the VPS, as root, from a checkout at the deployed commit:
```
/opt/hyperchat/current/venv/bin/python tests/e2e/smoke_dev_backend.py
```
It refuses to start (exit 2) unless the local API reports `environment: development`, the database is not
`hyperchat` or `hyperchat_restore_test`, and DEV bots are disabled. It seeds synthetic rows directly
(users `@authtest.invalid`, creators `authtest-sm<hex><a|b|c>`, orders `authtest-*`), deletes them at the
end, even after a failure, and fails if any are left. It also clears the `auth:*` and other rate-limit
counters it consumed, as `smoke_auth.py` does. It never creates a Razorpay order, writes to R2, sends email,
calls a TTS or transcription provider, or enables bots. Those checks print as `BLOCKED` with what unblocks
them, and do not fail the run. The one outbound effect is that approving a synthetic donation makes the
worker publish realtime events to the DEV Pusher app. A run takes about two minutes, mostly waiting for the
worker. Exit 0 means every runnable check passed, 1 means a check failed. Safe to repeat.
