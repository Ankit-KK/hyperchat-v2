# HyperChat v2 database (PostgreSQL + Alembic)

PostgreSQL is the application database. The schema is owned by Alembic: every change is a
migration in `migrations/versions/`, applied by `hyperchat_migrator` with `SET ROLE hyperchat_owner`.
The full table-by-table design, including how every legacy column was mapped, is in
[`../docs/target-schema.md`](../docs/target-schema.md).

## Layout
```
alembic.ini, migrations/   Alembic environment and revisions 0001-0009 (SQL-first, op.execute)
etl/rehearsal.py           load the legacy data (restored dump) into the new schema
etl/mapping.py             value mapping rules (currencies, roles, slugs, OBS token hashing, ...)
etl/reconcile.py           post-load reconciliation (counts, per-creator sums, invariants)
tests/                     migration round trip, role privileges, ETL mapping rules
```

| Revision | Content |
|---|---|
| 0001 | extensions (pgcrypto, citext), `set_updated_at()`, enums |
| 0002 | identity: `users`, `user_identities`, `sessions`, `password_reset_tokens` |
| 0003 | creators: `streamers`, moderators, OBS tokens (hashed), voices, soundboard, partner keys, ... |
| 0004 | the single normalized `donations` table + `order_lookup`, moderation, donor totals |
| 0005 | platform: nonces, rate limits, alerts, signups, visits, reviews, contact submissions |
| 0006 | integrity triggers |
| 0007 | least-privilege grants for the runtime roles |
| 0008 | `user_invites` (invite-only onboarding) |
| 0009 | `moderation_actions.actor_user_id` (the signed-in user behind dashboard moderation) |
| 0010 | payments: `streamers.razorpay_account`, `donations.payment_account` / `provider_payment_id`, `payment_webhook_events` |
| 0011 | background jobs: `jobs` (durable queue), `donation_transcripts` |

## Databases
| Database | Cluster | Purpose |
|---|---|---|
| `hyperchat` | `17/hyperchat` :5432 | production (schema only until cutover) |
| `hyperchat_dev` | `15/restoretest` :5433 | v2 development, loaded by `etl/rehearsal.py` |
| `hyperchat_restore_test` | `15/restoretest` :5433 | read-only restore of the legacy dump (ETL source only) |

## Commands (on the VPS, as root -> peer-mapped roles)
```
cd database
export HYPERCHAT_MIGRATION_DSN="postgresql+psycopg://hyperchat_migrator@/hyperchat_dev?host=/var/run/postgresql&port=5433"
../venv/bin/alembic upgrade head
../venv/bin/python -m etl.rehearsal --source "<dsn of hyperchat_restore_test>" --target "<dsn of hyperchat_dev>" --report report.txt
```
The rehearsal reads the source in one read-only snapshot and never writes to it; it prints counts
and sums only, never row data. Passwords are not migrated by the rehearsal (they are imported from
the legacy auth system at cutover).

## Tests
Uses the backend's pinned environment (`requirements.txt` -> `../backend/requirements-dev.txt`).
```
cd database && ../backend/.venv/bin/pytest
HYPERCHAT_TEST_MIGRATION_DSN="postgresql+psycopg://hyperchat_migrator@/<scratch_db>?host=/var/run/postgresql&port=5432" \
HYPERCHAT_TEST_ROLE_DB=<scratch_db>:5432 ../backend/.venv/bin/pytest
```
Database tests only run against a scratch database; they refuse `hyperchat`, `hyperchat_dev`
and `hyperchat_restore_test`.
