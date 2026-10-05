# Cross-component tests

Unit and integration tests live next to the code they test:
- `backend/tests/` - FastAPI app, authentication, security building blocks
- `database/tests/` - Alembic migrations, role privileges, ETL mapping rules

This directory holds checks that span components or run against a deployed environment.
CI (`.github/workflows/ci.yml`) runs the backend and database suites; `.github/scripts/setup-test-db.sh`
prepares the throwaway PostgreSQL it uses.

| Path | What it checks |
|---|---|
| `e2e/smoke_auth.py` | a deployed API over public HTTPS: login, cookie flags, CSRF/Origin enforcement, session revocation, forgot-password, disabled Google, CORS, no secrets in the service journal |

`e2e/smoke_auth.py` runs on the VPS (see its docstring); it creates one synthetic
`@authtest.invalid` account in the target database and removes it afterwards.
