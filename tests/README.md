# Cross-component tests

Unit and integration tests live next to the code they test:
- `backend/tests/` - FastAPI app, authentication, security building blocks, public and dashboard APIs
- `database/tests/` - Alembic migrations, role privileges, ETL mapping rules

This directory holds checks that span components or run against a deployed environment.
CI (`.github/workflows/ci.yml`) runs the backend and database suites; `.github/scripts/setup-test-db.sh`
prepares the throwaway PostgreSQL it uses.

| Path | What it checks |
|---|---|
| `e2e/nginx_ratelimit_check.sh` | the public-API rate-limit snippets in a throwaway local nginx (burst, JSON 429, recovery, other paths unaffected) and a syntax check of the dev site file |
| `e2e/smoke_auth.py` | a deployed API over public HTTPS: login, cookie flags, CSRF/Origin enforcement, session revocation, forgot-password, disabled Google, CORS, no secrets in the service journal |

`e2e/smoke_auth.py` runs on the VPS (see its docstring); it creates one synthetic
`@authtest.invalid` account in the target database and removes it afterwards. It expects the secure
default `SameSite=Lax`; against the v2 dev API (cross-site development, `SameSite=None`) run it with
`HYPERCHAT_SMOKE_SAMESITE=none`.
