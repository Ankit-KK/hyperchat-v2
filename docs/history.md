# History: legacy repository and v2

## Two repositories
| | `Ankit-KK/hyperchat` (legacy) | `Ankit-KK/hyperchat-v2` (this repository) |
|---|---|---|
| Role | the current production application; frozen reference | the replacement application |
| Stack | React/Vite frontend, Supabase Auth, ~67 Supabase Edge Functions, RLS, per-creator donation tables | new React frontend, FastAPI, PostgreSQL + Alembic, FastAPI-managed auth |
| Changes | none for v2 work; stays available as the functional reference | all backend, database, frontend, deployment and migration work |

The legacy repository remains the reference for **what** HyperChat does (features, business rules,
edge cases): every feature that is still needed is reimplemented here against the FastAPI API and
the new schema. Nothing is merged between the two repositories, and the legacy Edge Functions are
not recreated.

## Where this repository's starting point came from
The first v2 work (Phases 1 and 2) was developed in the legacy repository on feature branches,
under `backend/`, before the repositories were separated on 2026-10-05. It was brought into v2 as
clean commits instead of importing that history, so that v2 does not inherit the legacy layout or
the transitional Supabase code. Legacy commits, for provenance:

| Legacy commit | Content | Where it is in v2 |
|---|---|---|
| `2b26538` | FastAPI skeleton (health, config, JSON logging, DB pool, systemd/Nginx) | `backend/app/core`, `backend/app/db`, `backend/app/routers/health.py`, `deploy/` |
| `53ae4af` | security layer ported from the Edge Functions' shared code | generic parts in `backend/app/security/` (rewritten, see below) |
| `7e0d96e` | Alembic schema 0001–0007, ETL rehearsal, cluster setup, backups | `database/`, `deploy/postgres/`, `deploy/backup/`, `docs/target-schema.md` |
| `c54ff63` | `sapphire.status` dropped, reconciliation asserts it | `database/etl/reconcile.py`, `docs/target-schema.md` |
| `1fa9ac5` | FastAPI-managed authentication, migration 0008 | `backend/app/auth/`, `backend/app/routers/auth.py`, `database/migrations/versions/0008_auth_invites.py` |
| `7b54765` | auth tests: explicit opt-in for `hyperchat_dev` (never pushed to the legacy repository) | `backend/tests/auth/` |

Legacy branches that still hold that work: `backend/phase-1-skeleton` (`2b26538`) and
`backend/phase-2-auth` (`1fa9ac5`) in `Ankit-KK/hyperchat`. They were left in place; deleting them
is a separate, explicit decision.

The v2 development API (`api.quicktestspace.site`) was deployed from legacy commit `1fa9ac5`
(release `1fa9ac548c50`) before the separation. Its authentication code (`app/auth`, migration
0008) is the same as here; the surrounding app still contains the transitional Supabase-specific
security code that v2 dropped. The next deployment is made from this repository.

## What changed on the way into v2
Dropped (Supabase-specific or bound to the legacy schema):
- Supabase access-token verification (JWKS / `auth/v1/user`), Supabase JWT classification, the
  session bridge from Supabase users to legacy `auth_users`, and the request-token extraction
  (`auth_token` cookie, `x-auth-token`, Bearer user JWT). Browser auth is the cookie session + CSRF.
- Browser request signing keyed on the Supabase session (`signing_key`), the legacy nonce-less
  signature format and the TypeScript compatibility vectors generated from the Edge Functions.
- `stores.py` and `streamer_access.py`, which queried legacy tables (`auth_users`, `admin_emails`,
  plaintext `obs_tokens`, the `validate_obs_token_secure` RPC). Authorization is rebuilt in
  Phase 3 on `users.role` / `streamers.user_id`.
- Supabase-only settings (`supabase_url`, anon key, JWT audience, verifier choice).

Kept and generalized:
- HMAC request verification, now server-to-server only: explicit keys (rotation supported),
  nonce always required.
- OBS tokens: looked up by SHA-256 (the v2 schema stores `token_hash`), frontend origin only.
- Shared secrets fail closed (the legacy "open when unconfigured" cron behaviour is gone).
- Upload grants, nonce store, CORS (v2 frontend origins only; Supabase headers removed),
  security logging, crypto helpers.

Not brought over at all: the legacy frontend, Supabase Edge Functions and migrations, Supabase
configuration, `.env` files, credentials and production configuration.

## Working material outside the repositories
The restore test, backups, audits and ETL reports were produced in a local working directory and
on the VPS (`/root/hyperchat-restore-test`, the restored `hyperchat_restore_test` database). They
contain production data or dumps and are deliberately not stored in Git. The audit that matters
for v2 is included as [`migration-audit.md`](migration-audit.md); an earlier audit that planned a
FastAPI layer in front of Supabase was superseded and is not included.
