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
| 6 Donor uploads, hypersound library, creator soundboard on R2 | done; running on the v2 development API |
| 7 Razorpay checkout, payment status, webhooks (migration 0010) | done; running on the v2 development API |
| 8 Background jobs, worker, donation audio / TTS, transcription (0011) | done; running on the v2 development API (TTS / transcription providers disabled there) |
| 9 Realtime outbox, Pusher publishing, channel authorization (0012) | done; running on the v2 development API |
| 10 OBS audio queue and player, OBS tokens, replay (0013) | done; running on the v2 development API |
| 11 Telegram / Discord moderation bots (0014, 0015) | done; deployed on the v2 development API with the bots **disabled** |
| Backend completion (gaps from the full backend audit) | groups 1–4 in `main` (#18, #19, #20, #22; migration 0016; 0017 is the D1 import ledger from #49); security audit fixes F1-F3, F5, F6 and F8-F12 merged (#24-#32, #34) with the F10 and F11 follow-ups (#36-#39) and F13 (#40); F12-R (imported-bcrypt sign-in timing) is an accepted residual risk; D1 migrated-user password tooling merged (read-only report #33, importer #34, hardened in #45–#49: freeze SQL, source lock, RLS fail-closed, ENABLE ALWAYS kept on re-runs, import recovery ledger; the freeze is rehearsed on a local Supabase stack only, hosted checks pending; the importer has **not** been run against real data; D1 policy approved 2026-10-12: import at the production cutover; exclusion policy and password-change cutoff procedure approved (policy only); account-specific conflict resolution and maintenance scheduling pending; execution pending); deployed on DEV up to migration 0019 (2026-10-15; later DEV migrations: see the next row), not in production; items waiting on owner decisions remain — see [`docs/backend-completion-checklist.md`](docs/backend-completion-checklist.md) and [`docs/backend-security-audit.md`](docs/backend-security-audit.md) |
| Creator approval, Didit KYC, admin auth boundary, admin MFA (0020–0028); production worker; backups (A3) | merged and passing CI; migrations 0020–0028 deployed on DEV by the rehearsal ([`docs/operations/dev-rehearsal-0020-0028.md`](docs/operations/dev-rehearsal-0020-0028.md); phases 0–E passed, F not run) and 0029 on DEV 2026-11-02 per the C1 Gate M record (re-verify before the next DEV window); not in production; KYC and MFA enforcement stay `draft` |
| Payments integrity and accounting (PAY-15, HyperEmotes history guard; 0030–0035) | merged on `main` (#122–#129, #132–#137, #141–#144); **on DEV since 2026-11-24** (stage 6b window: release `b8c1596466f6`, head 0035); **accounting active on DEV since 2026-11-24 18:20Z** (stage 6d), 7-day soak 2026-11-25 to 2026-12-01; production no-go until the soak and the open findings close |
| Admin read APIs (#138), OPS-2 remote-command tooling (#139), CI test hardening T-4/T-5/T-7/T-8/T-9 (#140), `restore_check` head-aware (#145) | merged and passing CI 2026-11-23/24; #138 and #139 are in the DEV release, #140 and #145 are tests, CI and tooling |
| Backups | DEV: nightly local sets, off-site copy to a separate-account R2 bucket since 2026-11-23 with Discord failure alerting (2026-11-24); restore drills PASSED on a separate host for a 0029 set (2026-11-24 19:11Z) and a 0035 set (2026-11-25 06:14Z). **Production: local dumps only, no off-site copy** (interim C3) |
| 12 Admin, payouts, partners | partly in the backend-completion groups; payouts are out of scope for V2 (checklist D2, resolved 2026-10-13); the ClipWave partner API is kept and rebuilt in V2 (D4, [`docs/partner-api.md`](docs/partner-api.md)) |
| 13 New frontend | not started; **blocked** until the backend is fully functional and its security and deployment readiness are established (owner instruction) |
| 14–15 Production deployment, data migration, cutover | final phase — see [`docs/migration-audit.md`](docs/migration-audit.md) §M |

**Server state (as of 2026-11-25, from records outside the repo; re-verify before the next DEV window):**
- **`main`:** `a98eb13` (#144), migration head `0035`.
- **Development environment:** release `b8c1596466f6` at migration `0035` (PAY-15 6b window closure record, 2026-11-24 13:35Z); payment accounting active since 2026-11-24 18:20Z (6d record), in a 7-day soak to 2026-12-01. Bots disabled, no bot webhooks registered. KYC and MFA enforcement `draft`.
- **Backups:** DEV off-site to R2 live since 2026-11-23, Discord failure alerts since 2026-11-24, two restore drills passed (0029 and 0035 sets). Production has no off-site copy.
- **Production:** release `940137070657` at migration `0009`, no traffic, unchanged since Phase 5. Worker not installed.

**The backend is not complete:** see the status in [`docs/backend-completion-checklist.md`](docs/backend-completion-checklist.md). Which features were exercised against the real external services (Razorpay test mode, Pusher,
R2, the DEV bots) and which are only covered by automated tests (email, Google sign-in, TTS and transcription
providers, among others) is tracked per capability in the checklist.

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
the unit tests and the full database + integration suites against PostgreSQL 17 on every pull request and every
merge to `main` (branch pushes without a pull request run nothing; start the workflow by hand if needed). Changes
to migrations, roles, auth, KYC, step-up, admin, backup or CI also run the migration and permission suites against
PostgreSQL 15 (DEV's version).

## Documentation
- [`docs/architecture.md`](docs/architecture.md) — components, trust boundaries, environments
- [`docs/public-api.md`](docs/public-api.md) — public read API (Phase 4)
- [`docs/dashboard-api.md`](docs/dashboard-api.md) — creator dashboard API (Phase 5)
- [`docs/uploads-api.md`](docs/uploads-api.md) — donor upload grants and voice/media uploads to R2 (Phase 6)
- [`docs/soundboard-api.md`](docs/soundboard-api.md) — creator soundboard and hypersound library (Phase 6)
- [`docs/payments-api.md`](docs/payments-api.md) — Razorpay checkout, payment status, confirmation and webhooks (Phase 7)
- [`docs/audio-jobs.md`](docs/audio-jobs.md) — background jobs, worker, donation audio/TTS, moderation, voice transcription (Phase 8)
- [`docs/realtime.md`](docs/realtime.md) — realtime outbox, Pusher publishing, channel authorization, the Phase 10 OBS contract (Phase 9)
- [`docs/obs-audio.md`](docs/obs-audio.md) — OBS audio queue and player, OBS tokens, replay, `!vaani` (Phase 10)
- [`docs/obs-cutover.md`](docs/obs-cutover.md) — legacy OBS URLs → V2, cutover compatibility checklist (OBS-4)
- [`docs/bots.md`](docs/bots.md) — Telegram / Discord moderation bots and moderator notifications (Phase 11)
- [`docs/target-schema.md`](docs/target-schema.md) — PostgreSQL schema and legacy → v2 data mapping
- [`docs/migration-audit.md`](docs/migration-audit.md) — Supabase-exit audit and phase plan
- [`docs/backend-completion-checklist.md`](docs/backend-completion-checklist.md) — remaining backend gaps, owner decisions, verification status
- [`docs/history.md`](docs/history.md) — relationship to the legacy repository
- [`docs/backup-recovery.md`](docs/backup-recovery.md) — backups of both environments, restore verification, KYC witness recovery, off-site design, recovery runbooks
- [`deploy/README.md`](deploy/README.md) — VPS layout, deployment and rollback

## Rules
- Secrets never go into Git: only `*.example` templates. Real values live in `/etc/hyperchat/api.env`.
- Commits by `Ankit Kumar <ankitashuk20@gmail.com>`.
