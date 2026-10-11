# HyperChat v2 database (PostgreSQL + Alembic)

PostgreSQL is the application database. The schema is owned by Alembic: every change is a
migration in `migrations/versions/`, applied by `hyperchat_migrator` with `SET ROLE hyperchat_owner`.
The full table-by-table design, including how every legacy column was mapped, is in
[`../docs/target-schema.md`](../docs/target-schema.md).

## Layout
```
alembic.ini, migrations/   Alembic environment and revisions 0001-0016 (SQL-first, op.execute)
etl/rehearsal.py           load the legacy data (restored dump) into the new schema
etl/mapping.py             value mapping rules (currencies, roles, slugs, OBS token hashing, ...)
etl/reconcile.py           post-load reconciliation (counts, per-creator sums, invariants)
etl/auth_readonly_report.py READ-ONLY count report: Supabase auth.users vs public.users (D1 phase 1)
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
| 0012 | realtime: `realtime_outbox` (transactional outbox published to Pusher by the worker) |
| 0013 | OBS audio: `realtime_outbox` CHECKs allow the `alerts` channel, `audio-now-playing` and `vaani-celebration` |
| 0014 | moderation bots: `jobs.kind` + `moderator_notify`, `moderation_actions` source `discord`, `moderator_notifications`, callback-table columns |
| 0015 | bot audit: drops the UNIQUE `moderation_actions_unique_bot` index so a decision repeated after a reversal is recorded (no data change; downgrade does not rebuild it) |
| 0016 | donor unban audit: nullable `banned_donors.unbanned_at`, `unbanned_by_label`, `unbanned_by_user_id` (FK `users`, ON DELETE SET NULL) |

## Databases
| Database | Cluster | Purpose |
|---|---|---|
| `hyperchat` | `17/hyperchat` :5432 | production (schema at revision 0009, no traffic until cutover) |
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

## Supabase Auth read-only report (D1, phase 1)
`etl/auth_readonly_report.py` compares the live Supabase `auth.users` with `public.users` and prints
**aggregate counts only**. It is not an import: it has no write statements, runs inside `READ ONLY`
transactions on both databases, never selects a password hash (only whether `encrypted_password` is set),
and prints no ids, emails, hashes or connection details. It is run manually by the operator, in their own
SSH terminal; never by automation and never with credentials pasted into a chat or a file.

**What it checks:** accounts match by `auth.users.id = public.users.legacy_supabase_user_id`; the top-level
`auth.users.email` is cross-checked (`raw_user_meta_data` is never read). Emails are compared as SHA-256
digests computed by each server and kept in memory for the run only.

**Before running (owner):**
- A Supabase role that can only read: `USAGE` on schema `auth` and `SELECT (id, email, encrypted_password,
  confirmed_at)` on `auth.users`, nothing else. The tool refuses a role that can write to the table (or a
  superuser) unless `--accept-writable-source-role` is given; the transaction is read-only either way.
  Note: column `SELECT` on `encrypted_password` is what lets the server evaluate "is it set"; the role could
  therefore read hash values with other tools. Revoke or drop it after the run.
- The Supabase CA certificate (dashboard -> Database settings -> SSL configuration -> download) copied to the
  VPS, e.g. `/root/supabase-ca.crt` (mode 600). The connection uses `sslmode=verify-full` (certificate and
  host name); the tool refuses a missing file and a connection without TLS.
- A host the VPS can reach: the direct host `db.<project-ref>.supabase.co:5432` or the session pooler.
- The code on the VPS: a release or checkout that contains this file (DEV's current release does not).

**Run (on the VPS, as root, in a separate SSH terminal):**
```
cd /opt/hyperchat/current/database      # a release that contains etl/auth_readonly_report.py
../venv/bin/python -m etl.auth_readonly_report
```
It prompts for the Supabase host, CA file path, port (5432), database (`postgres`), role and password (not
echoed), then for the target (defaults: socket `/var/run/postgresql`, port `5433`, `hyperchat_dev`,
`hyperchat_readonly`, empty password = peer authentication). The target must be a local socket or loopback.
Nothing is passed on the command line, written to a file or logged; errors print only the error class and
SQLSTATE. Exit codes: 0 report printed, 2 refused by a safety check, 1 database error, 130 cancelled.

**Reading the report:** `id_and_email_exact` accounts match cleanly; `id_match_email_case_or_space_only`,
`id_match_email_mismatch`, `id_match_source_email_missing`, `target_legacy_id_not_in_source`,
`target_email_used_by_other_source_id` and the `duplicate_*` counts need a decision before any import.
`exact_match_source_password_target_none` is the number of accounts a later hash import could cover.
