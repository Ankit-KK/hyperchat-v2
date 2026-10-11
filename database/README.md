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
etl/auth_hash_import.py     Supabase bcrypt hash import into public.users, dry run by default (D1 phase 2)
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
and `hyperchat_restore_test`. The D1 report and importer tests build their own throwaway fixture databases and run
only where `HYPERCHAT_CI_DB_SETUP=1` (CI's service container or the throwaway cluster).

## Supabase Auth read-only report (D1, phase 1)
**Status:** merged in #33 (`3e9c687`); run once by the owner from an isolated copy on the VPS (counts in the
checklist, AUTH-6); the temporary files and the Supabase role were removed afterwards.

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
- A host the VPS can reach: the VPS has no IPv6, so use the **session pooler** (IPv4) rather than the direct host
  `db.<project-ref>.supabase.co`, which is usually IPv6-only.
- The code on the VPS: an isolated copy (e.g. `git archive <commit> database/etl` into a private directory), run
  with a release's interpreter and `-B -E -s` so nothing is written into the release; DEV's current release does
  not contain it.

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

## Supabase password-hash import (D1, phase 2)
**Status:** merged in #34 (`c53a8c9`); tested with synthetic hashes only. It has **not** been run against real
Supabase data and no hash has been imported anywhere. **D1 policy approved by the owner on 2026-10-12:** import
eligible hashes at the production cutover with this tool and its safeguards; never into DEV without separate approval;
legacy confirmation fields are not mapped to `email_verified_at`. That is policy only: running the tool against real
data still needs explicit operator approval at the cutover, after the open details below are settled.

**Exclusion policy approved by the owner on 2026-10-12 (policy only; execution pending):** keep email mismatches excluded until identity is manually reconciled; do not import for V2 IDs missing in Supabase; block V2 emails that match a different Supabase ID pending manual review; do not create V2 accounts automatically for source-only Supabase IDs; leave deleted/banned accounts and accounts with existing hashes, missing hashes or unsupported formats excluded under importer rules. Never import by email alone or change account ownership. Reset or Google recovery is allowed only after identity is resolved and the specific flow has been configured and live-verified; otherwise keep the account on hold for separate resolution. Account-specific conflicts and recovery paths remain to be resolved before cutover.

**Password-change cutoff procedure APPROVED by the owner on 2026-10-12 (policy only; no cutover authorized):** at the start of the scheduled maintenance window, temporarily disable legacy password-change and password-reset writes while allowing ordinary legacy sign-ins to continue; drain in-flight password mutations and verify they have finished; only then take the final source snapshot. Run a fresh read-only reconciliation report and dry-run import against that snapshot, review every eligibility and exclusion count, and obtain separate explicit operator approval before production `--apply`. Keep legacy password mutations frozen through import, post-import reconciliation, and migrated-account sign-in verification; switch authentication only after those checks pass. The exact maintenance timestamp must be agreed and communicated before the maintenance window starts. **Abort handling:** if aborted before `--apply`, keep V2 authentication inactive and resume legacy password mutations only after confirming legacy remains the source of truth. If aborted after `--apply`, keep V2 authentication inactive and do not simply re-run the importer or resume legacy password mutations; target rows now have hashes and the importer will not overwrite them. Stop for an explicitly approved rollback/reconciliation plan, then take a fresh snapshot and repeat the report/dry run before retry. Any password mutation after the final snapshot invalidates that snapshot and requires renewed reconciliation; no silent continuation. This procedure is documented policy only and does not authorize access, import, deployment, or cutover.

**Before the maintenance window:** resolve account-specific identity conflicts under the approved exclusion policy and set the actual maintenance time. Keep the legacy mutation freeze in place until the cutover is either validated or safely aborted. The importer will not overwrite a target row that already has a password hash, so an abort after `--apply` must not be handled by casually rerunning it. Use an explicitly approved rollback/reconciliation plan first.

`etl/auth_hash_import.py` copies Supabase bcrypt hashes into `public.users.password_hash` so migrated users keep
their passwords. **Dry run by default.** Nothing is ever written to a file, log or the terminal except counts.

**Rules (enforced in code):**
- A target row is eligible only if `auth.users.id = legacy_supabase_user_id` **and** the top-level email matches
  exactly (case-only differences are excluded), the Supabase account is not deleted (`deleted_at`) or currently
  banned (`banned_until`), the source hash is a bcrypt hash the V2 verifier accepts (`$2a$`/`$2b$`/`$2y$`, cost
  04-12, 60 characters) and the target hash is NULL or empty.
- **One shared format check (`backend/app/auth/bcrypt_format.py`, used by both the sign-in verifier and the importer):**
  prefix `$2a$`/`$2b$`/`$2y$`, cost 04-12, `$`, then exactly 53 characters of bcrypt's base64 alphabet (60 ASCII
  characters, nothing else). Anything else is never handed to bcrypt at sign-in and is unsupported for import.
- **Cost cap 12 (`MAX_BCRYPT_COST`):** GoTrue writes cost 10.
  Measured on the 2-vCPU VPS: cost 10 ~52 ms, 11 ~106 ms, 12 ~213 ms, 13 ~423 ms per verification (Argon2id ~90
  ms); every step doubles, so cost 31 would take ~30 hours. 12 leaves two steps of headroom over GoTrue's default
  at about 2.4x the cost of a normal V2 sign-in. A higher cost is "unsupported": it blocks `--apply`, and the
  verifier never runs bcrypt on it (one dummy Argon2 check, generic 401).
- Never by email alone. No account is created or deleted; only `password_hash` is written (roles, creator ownership,
  activation, `email_verified_at`, `password_changed_at` stay as they are). `email_confirmed_at` is only counted
  (`eligible_email_confirmed`); `confirmed_at` is never used (it can be a phone confirmation).
- Excluded rows are counted by reason: orphaned target ids, target email belonging to another Supabase id, email
  mismatch, deleted, banned, already has a password, no source hash, unsupported format.
- `--apply` refuses on any blocker: duplicate ids on either side, or **any** unsupported hash format among otherwise
  eligible rows.

**Freeze and fingerprint guard:** the dry run also prints `source_fingerprint` (SHA-256 over every source account's
id, email, password hash and deleted / banned state, computed inside PostgreSQL) and `source_freeze` (whether the D1
freeze trigger from `docs/operations/d1-auth-freeze/` is in place exactly as reviewed). `--apply` additionally needs
`--expected-fingerprint <the reviewed value>` and refuses, before the confirmation prompt, when the freeze is missing,
disabled or malformed or when the fingerprint differs. The freeze check, the fingerprint and the hashes come from one
source snapshot, so a matching fingerprint proves exactly what will be written. The freeze SQL is **proposed and not
yet rehearsed** against GoTrue; see that folder's README for the procedure and the rehearsal.

**How `--apply` writes:** after the plan it asks you to type `IMPORT <n> HASHES INTO <database>`. Then, in **one
transaction**, it locks the planned `users` rows (5 s lock timeout), re-checks each is unchanged (same legacy id,
same email, still no hash) and updates each with `WHERE ... AND coalesce(password_hash, '') = ''`. Any difference,
lock timeout or error rolls back **every** row. `--apply` into `hyperchat_dev` additionally needs
`--allow-dev-target`; real hashes stay out of DEV unless the owner approves it separately.

**Prerequisites:** as for the read-only report (a read-only Supabase role, now with `SELECT (id, email,
encrypted_password, email_confirmed_at, deleted_at, banned_until)` on `auth.users`; the Supabase CA file; the session
pooler host from an IPv4-only VPS; the code in a private directory such as `/root/d1-import`, i.e. `database/etl` **plus** `backend/app/__init__.py`,
`backend/app/auth/__init__.py` and `backend/app/auth/bcrypt_format.py`, which the importer loads; without them it
refuses to start: `git archive <commit> database/etl backend/app/__init__.py backend/app/auth/__init__.py
backend/app/auth/bcrypt_format.py`). The target role for
`--apply` needs `UPDATE` on `public.users.password_hash` and must not be a superuser (default `hyperchat_app`).

**Run (as root on the VPS, in your own SSH terminal):**
```
cd /root/d1-import/database
/opt/hyperchat/releases/<release>/venv/bin/python -B -E -s -m etl.auth_hash_import            # dry run
/opt/hyperchat/releases/<release>/venv/bin/python -B -E -s -m etl.auth_hash_import --apply    # writes, after the phrase
```
Exit codes: 0 done (dry run, nothing to do, or APPLIED printed), 2 refused, 3 confirmation not typed, 1 error (no
row was written unless APPLIED was printed), 130 cancelled.

**Cutover order:** (1) resolve account-specific identity conflicts and recovery paths; (2) schedule the maintenance window and temporarily freeze legacy password-change/reset writes while allowing ordinary legacy sign-ins; drain and verify all in-flight password mutations have completed; (3) take the final source snapshot/dump only after the drain, then run the ETL and fresh read-only reconciliation report; (4) run the importer dry run against that final snapshot and review every eligibility/exclusion count; (5) obtain separate explicit operator approval before `--apply` on the production database (prompted name, e.g. `hyperchat` on port 5432); (6) while legacy password mutations remain frozen and V2 authentication remains inactive, re-run the report/dry run, reconcile applied counts, and test migrated-account sign-in; (7) switch authentication only after all checks pass; (8) remove the temporary Supabase role and importer directory only after successful verification.

If any source password mutation occurs after the snapshot, the snapshot is invalid and the report/dry run must be repeated. If cutover aborts before `--apply`, keep V2 authentication inactive and resume legacy mutations only after confirming legacy remains authoritative. If it aborts after `--apply`, do not re-run the importer or resume legacy mutations by default: target hashes are not overwritten by the importer. Keep V2 authentication inactive and follow a separately approved rollback/reconciliation plan before retrying.

**Rollback:** V2 itself only ever writes Argon2id hashes and the ETL loads none, so imported hashes are exactly the
bcrypt ones still stored. To undo the import (users who already signed in keep their upgraded Argon2id hash):
```
BEGIN;
SELECT count(*) FROM public.users WHERE password_hash LIKE '$2_$%';   -- compare with the applied count
UPDATE public.users SET password_hash = NULL WHERE password_hash LIKE '$2_$%' AND legacy_supabase_user_id IS NOT NULL;
COMMIT;
```
