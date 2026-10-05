# HyperChat v2

The new HyperChat: a ground-up replacement of the Supabase-based application with

- a **new React frontend** (`hyperchat.space`, Vercel),
- a **FastAPI backend** (`api.hyperchat.space`) that is the only API,
- **PostgreSQL** as the application database, schema owned by **Alembic**,
- **FastAPI-managed authentication** (cookie sessions + CSRF, Argon2id, invites, Google OAuth),
- external services called only by the backend: Razorpay, Pusher, Cloudflare R2, Resend, Google OAuth.

There is **no Supabase** in this architecture: no Supabase Auth, Edge Functions, RLS-based
browser access or direct database calls from the browser.

The legacy application lives in [`Ankit-KK/hyperchat`](https://github.com/Ankit-KK/hyperchat) and
is kept unchanged as the functional reference; see [`docs/history.md`](docs/history.md).

```
New React frontend ──HTTPS/API──▶ FastAPI backend ──▶ PostgreSQL
 (hyperchat.space)                (api.hyperchat.space)  └─▶ Razorpay · Pusher · R2 · Resend · Google OAuth
```

## Repository layout
```
backend/    FastAPI application, its unit and integration tests
database/   Alembic migrations, ETL rehearsal tooling (legacy data -> new schema), database tests
frontend/   new React frontend (not started; architecture notes only)
deploy/     systemd, Nginx, PostgreSQL setup, backups, environment template
tests/      cross-component / deployed-environment checks
docs/       architecture, target schema, migration audit, history
```

## Status
| Phase | State |
|---|---|
| 1 Platform: PostgreSQL clusters, roles, Alembic schema (normalized `donations`), ETL rehearsal, backups | done |
| 2 FastAPI-managed authentication | done; running on the v2 development API |
| 3 Authorization/security, then feature APIs, new frontend, cutover | next — see [`docs/migration-audit.md`](docs/migration-audit.md) §M |

Development API: `https://api.quicktestspace.site` (database `hyperchat_dev`).
Production (`api.hyperchat.space`, database `hyperchat`) has the schema but takes no traffic yet.

## Running the tests
```
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pytest
cd ../database && ../backend/.venv/bin/pytest
```
Database-backed tests are opt-in via environment variables and only ever use scratch databases;
see `backend/README.md` and `database/README.md`.

## Documentation
- [`docs/architecture.md`](docs/architecture.md) — components, trust boundaries, environments
- [`docs/target-schema.md`](docs/target-schema.md) — PostgreSQL schema and legacy → v2 data mapping
- [`docs/migration-audit.md`](docs/migration-audit.md) — Supabase-exit audit and phase plan
- [`docs/history.md`](docs/history.md) — relationship to the legacy repository
- [`deploy/README.md`](deploy/README.md) — VPS layout, deployment and rollback

## Rules
- Secrets never go into Git: only `*.example` templates. Real values live in `/etc/hyperchat/api.env`.
- Commits by `Ankit Kumar <ankitashuk20@gmail.com>`.
