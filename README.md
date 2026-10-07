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
| 3 Authorization (creator ownership), admin bootstrap + user management, database-backed security primitives, explicit origins, cleanup, CI | done; running on the v2 development API |
| 4 Public read API (donation pages, overlays, landing page) | done; running on the v2 development API |
| 5 Creator dashboard API (settings, donations, moderation, moderators, terms; migration 0009) | done; running on the v2 development API |
| 6–13 Remaining feature APIs, new frontend | next — built and tested against the development API |
| 14–15 Production deployment, data migration, cutover | final phase — see [`docs/migration-audit.md`](docs/migration-audit.md) §M |

**V2 is not production-ready.** All development, frontend integration, previews and testing target
the development environment only:

| | Development (active) | Production (reserved, final phase only) |
|---|---|---|
| Frontend | `https://app.quicktestspace.site` (+ `http://localhost:5173`) | `https://hyperchat.space` (still the legacy site) |
| API | `https://api.quicktestspace.site` | `https://api.hyperchat.space` (deployed, idle, not used) |
| Database | `hyperchat_dev` | `hyperchat` (empty apart from the first admin) |

The production API was provisioned early so that it exists at cutover; its existence does not mean V2
is ready. No frontend, preview or test points at `api.hyperchat.space` or the production database
until the final cutover phase.
Production (`api.hyperchat.space`, database `hyperchat`) has the schema but takes no traffic yet.

## Running the tests
```
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pytest
cd ../database && ../backend/.venv/bin/pytest
```
Database-backed tests are opt-in via environment variables and only ever use scratch databases;
see `backend/README.md` and `database/README.md`. GitHub Actions (`.github/workflows/ci.yml`) runs
the unit tests and the full database + integration suites against PostgreSQL 17 on every push.

## Documentation
- [`docs/architecture.md`](docs/architecture.md) — components, trust boundaries, environments
- [`docs/public-api.md`](docs/public-api.md) — public read API (Phase 4)
- [`docs/dashboard-api.md`](docs/dashboard-api.md) — creator dashboard API (Phase 5)
- [`docs/uploads-api.md`](docs/uploads-api.md) — donor upload grants and voice/media uploads to R2 (Phase 6, in progress)
- [`docs/soundboard-api.md`](docs/soundboard-api.md) — creator soundboard and hypersound library (Phase 6, in progress)
- [`docs/target-schema.md`](docs/target-schema.md) — PostgreSQL schema and legacy → v2 data mapping
- [`docs/migration-audit.md`](docs/migration-audit.md) — Supabase-exit audit and phase plan
- [`docs/history.md`](docs/history.md) — relationship to the legacy repository
- [`deploy/README.md`](deploy/README.md) — VPS layout, deployment and rollback

## Rules
- Secrets never go into Git: only `*.example` templates. Real values live in `/etc/hyperchat/api.env`.
- Commits by `Ankit Kumar <ankitashuk20@gmail.com>`.
