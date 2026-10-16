# HyperChat v2 database (PostgreSQL + Alembic)

PostgreSQL is the application database. The schema is owned by Alembic: every change is a
migration in `migrations/versions/`, applied by `hyperchat_migrator` with `SET ROLE hyperchat_owner`.
The full table-by-table design, including how every legacy column was mapped, is in
[`../docs/target-schema.md`](../docs/target-schema.md).

## Layout
```
alembic.ini, migrations/   Alembic environment and revisions 0001-0017 (SQL-first, op.execute)
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
| 0017 | D1 import ledger `d1_hash_import_ledger` (run id, user id, SHA-256 of the imported hash, source fingerprint; written by the importer in the same transaction as the hashes); append-only for `hyperchat_app` (SELECT, INSERT), nothing for the worker, SELECT for `hyperchat_readonly` |
| 0019 | D13: Custom Soundboard terms acceptance is owner-specific: `soundboard_tnc_acceptances` unique key `(streamer_id, terms_version)` → `(streamer_id, terms_version, user_id)`; existing rows kept; migrate and switch the release in one deploy (the previous release's acceptance insert targets the old key); downgrade stops without changes when a creator holds one version's acceptances by more than one owner; applied on DEV 2026-10-15 (43 existing rows kept) |
| 0018 | D6: retires the `secondary` Razorpay account: `streamers.razorpay_account` `secondary` → `primary`, both account CHECK constraints narrowed to `primary` (NULL still allowed: legacy orders); stops without changes if any donation was created on `secondary` (manual review); downgrade widens the constraints and remaps nothing |
| 0020 | Creator lifecycle (manual approval + mandatory KYC, PR1): `streamers.status` gains `suspended`, default `pending`; `kyc_track` (`standard` default, every existing creator `legacy`) and `kyc_baseline_owner_id` (existing owner; no FK so a deleted owner fails closed), written only by migrations and the legacy import (trigger refuses the runtime roles); append-only `creator_status_events`; `creator_donation_block()` and the `donations` insert trigger. No status, owner, donation or `updated_at` changes; downgrade stops while a creator is suspended or an audit row exists |
| 0021 | Creator applications (KYC PR2): `user_signups` gains `review_status` (`legacy_unreviewed` for every existing row, `submitted` default, `approved`, `rejected`), `reviewed_by`, `reviewed_at`, `rejection_reason` (required while rejected), `streamer_id` (unique, RESTRICT; required exactly while approved) and `invite_id`; `user_invites.signup_id` (RESTRICT, immutable by trigger: an application invite can never become an ordinary one); append-only `application_events` (RESTRICT: a reviewed application cannot be deleted by the runtime roles, so its history cannot be erased indirectly). No existing signup, creator or donation changes; downgrade stops once anything was reviewed, noted or invited |
| 0022 | Owner identity verification (KYC PR3): `kyc_sessions` (per user; `vendor_data` = user id), `kyc_webhook_events` (event dedupe, no payload, 90-day cleanup), `kyc_events` (audit), `kyc_provider_config` (the Didit environment, workflow, application and **webhook secret**; no runtime role can read it; set with `etl/kyc_provider_config.py`), `user_kyc_approved(user, environment)`. The runtime role only reads the KYC tables; all writes are SECURITY DEFINER functions: `kyc_receive_webhook` (verifies X-Signature-V2 in the database; the only path to Approved), `kyc_create_session`, `kyc_apply_refresh` (never approves), `kyc_purge_webhook_receipts`. Users with KYC history cannot be deleted. Nothing existing changes; downgrade stops once a session or audit row exists |

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
- **Row-level security:** the role must not be subject to row-level security on `auth.users`. Supabase enables RLS
  on `auth.users` with no policies (seen on the local Supabase image, PostgreSQL 17.11; the legacy project is
  unverified), so a role without `BYPASSRLS` reads **zero rows, without any error**. Both D1 tools therefore run
  their reads with `row_security = off`: PostgreSQL then raises instead of filtering, and the tool refuses
  (`row-level security on auth.users would hide rows from this role`, exit 2) rather than report or plan a filtered
  set. That setting grants nothing: it never shows a row the role could not read. Give the read-only role
  `BYPASSRLS` (`ALTER ROLE ... BYPASSRLS`, which the local Supabase `postgres` role could run; unverified on hosted).
  `BYPASSRLS` skips policies only; the role still reads nothing beyond its column grants. The owner's earlier run
  counted 153 source accounts, so that role saw rows; whether a policy hid some of them cannot be told afterwards.
  The fresh report at the cutover settles it.
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
**Status:** merged in #34 (`c53a8c9`), hardened in #45–#47 and #49 (freeze guard and fingerprint, source lock, RLS fail-closed, recovery ledger with migration 0017; in `main`, not deployed); tested with synthetic hashes only. It has **not** been run against real
Supabase data and no hash has been imported anywhere. **D1 policy approved by the owner on 2026-10-12:** import
eligible hashes at the production cutover with this tool and its safeguards; never into DEV without separate approval;
legacy confirmation fields are not mapped to `email_verified_at`. That is policy only: running the tool against real
data still needs explicit operator approval at the cutover, after the open details below are settled.

**Exclusion policy approved by the owner on 2026-10-12 (policy only; execution pending):** keep email mismatches excluded until identity is manually reconciled; do not import for V2 IDs missing in Supabase; block V2 emails that match a different Supabase ID pending manual review; do not create V2 accounts automatically for source-only Supabase IDs; leave deleted/banned accounts and accounts with existing hashes, missing hashes or unsupported formats excluded under importer rules. Never import by email alone or change account ownership. Reset or Google recovery is allowed only after identity is resolved and the specific flow has been configured and live-verified; otherwise keep the account on hold for separate resolution. Account-specific conflicts and recovery paths remain to be resolved before cutover.

**Password-change cutoff procedure APPROVED by the owner on 2026-10-12 (policy only; no cutover authorized):** at the start of the scheduled maintenance window, temporarily disable legacy password-change and password-reset writes while allowing ordinary legacy sign-ins to continue; drain in-flight password mutations and verify they have finished; only then take the final source snapshot. Run a fresh read-only reconciliation report and dry-run import against that snapshot, review every eligibility and exclusion count, and obtain separate explicit operator approval before production `--apply`. Keep legacy password mutations frozen through import, post-import reconciliation, and migrated-account sign-in verification; switch authentication only after those checks pass. The exact maintenance timestamp must be agreed and communicated before the maintenance window starts. **Abort handling:** if aborted before `--apply`, keep V2 authentication inactive and resume legacy password mutations only after confirming legacy remains the source of truth. If aborted after `--apply`, keep V2 authentication inactive and do not simply re-run the importer or resume legacy password mutations; target rows now have hashes and the importer will not overwrite them. Stop for an explicitly approved rollback/reconciliation plan, then take a fresh snapshot and repeat the report/dry run before retry. Any password mutation after the final snapshot invalidates that snapshot and requires renewed reconciliation; no silent continuation. This procedure is documented policy only and does not authorize access, import, deployment, or cutover.

**Admin email correction (D11, `PUT /api/v1/admin/users/{id}/email`) in the cutover:** a controlled recovery / correction tool only, never a shortcut around the exclusion policy. Fix an address in **legacy** while legacy is the source of truth (its own tool updates Supabase Auth and `auth_users`). In V2, correct an email only **after the ETL and before the dry run**, or **after post-import reconciliation**; **never between `--apply` and reconciliation** (the import compares emails). Any email change after a dry run invalidates it: rerun the read-only report and the dry run. It can make an `id_match_email_mismatch` account importable once reliable identity evidence shows the Supabase address is the right one. It does **not** solve V2 legacy ids missing in Supabase, duplicate ownership, or an email owned by another Supabase identity (manual review; ownership changes are D13, not D11). The correction leaves the address unverified, revokes sessions, burns reset links and removes Google links, so do it before Google sign-in is opened to migrated users.

**Creator ownership transfer (D13, `POST /api/v1/admin/creators/{id}/owner/transfer`) in the cutover:** it moves a creator to another account; it does **not** establish identity. The admin first determines the rightful owner from reliable evidence. Prefer fixing ownership in **legacy** before the ETL (`streamers.user_id` and `auth_users.streamer_id` together). In V2, transfer only **after the ETL**, and **never between `--apply` and reconciliation**. Ownership is not part of the import fingerprint, so a transfer does not invalidate a dry run (an email change does: see D11). It can resolve the creator/account side of a "V2 email used by another Supabase id" case once the review has decided who owns the creator. It does **not** solve V2 legacy ids missing in Supabase or Supabase-only accounts, and it merges no accounts. Rotate the creator's OBS tokens when the previous owner must lose the overlays.

**Media URLs at cutover (D9):** production serves media from a configured custom domain (`HYPERCHAT_R2_PUBLIC_BASE_URL`), not `r2.dev`, and V2 accepts and plays only URLs on that base. Load with `--media-base-to <that base>` and one `--media-base-from <legacy base>` per legacy public host (legacy used more than one `pub-….r2.dev`): every donation media URL (`voice_message_url`, `hypersound_url`, `media_url`, `tts_audio_url`), soundboard `audio_url` / `image_url` and creator `brand_logo_url` / `background_url` that starts with a legacy base gets the new base, with the object key unchanged; NULL, URLs already on the new base and any other URL (e.g. Supabase Storage objects, which are copied separately) are left alone. The reconciliation then fails if a legacy-base URL remains. The rewrite is deterministic and idempotent. The objects themselves are not moved: the custom domain serves the same bucket. Keep the old `r2.dev` hosts readable where practical, since old links live on in chat history and screenshots; new data uses the custom domain only.

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
source snapshot, so the hashes are exactly the fingerprinted rows; that snapshot cannot see later changes. So after
the confirmation the importer locks the source (`SHARE` on `auth.users`, at most 5 s wait), verifies the freeze, the
fingerprint and every planned hash again under the lock, and keeps the lock until the target transaction has
committed: no source row can change in between. The source role for `--apply` therefore needs `MAINTAIN` on
`auth.users` (PostgreSQL 17+) or table update / delete / truncate (earlier; with `--accept-writable-source-role`).
Legacy sign-ins and token refreshes pause while the lock is held. Remaining risks (a superuser ending the importer's
session, the dashboard role's replica-mode bypass of a plain-enabled trigger, changes after the release) and the
operator controls are in that README. The freeze SQL is **proposed and rehearsed against GoTrue on a local Supabase
stack only** (2026-10-12); the hosted permission checks are still open (see that folder's README).

**How `--apply` writes:** after the plan it asks you to type `IMPORT <n> HASHES INTO <database>`. Then, in **one
transaction**, it locks the planned `users` rows (5 s lock timeout), re-checks each is unchanged (same legacy id,
same email, still no hash) and updates each with `WHERE ... AND coalesce(password_hash, '') = ''`. Any difference,
lock timeout or error rolls back **every** row. In the same transaction it records one row per imported account in
`public.d1_hash_import_ledger` (migration 0017) under the `import_run_id` it prints before writing, so a run can be
checked and undone exactly (`docs/operations/d1-auth-freeze/import_status.sql`, `rollback_import.sql`). `--apply` into
`hyperchat_dev` additionally needs `--allow-dev-target`; real hashes stay out of DEV unless the owner approves it
separately.

**Prerequisites:** as for the read-only report (a read-only Supabase role with `BYPASSRLS`, now with `SELECT (id, email,
encrypted_password, email_confirmed_at, deleted_at, banned_until)` on `auth.users`; row-level security that would
hide rows is refused in the plan and raises under the lock, so nothing is written; the Supabase CA file; the session
pooler host from an IPv4-only VPS; the code in a private directory such as `/root/d1-import`, i.e. `database/etl` **plus** `backend/app/__init__.py`,
`backend/app/auth/__init__.py` and `backend/app/auth/bcrypt_format.py`, which the importer loads; without them it
refuses to start: `git archive <commit> database/etl backend/app/__init__.py backend/app/auth/__init__.py
backend/app/auth/bcrypt_format.py`). The target role for
`--apply` needs `UPDATE` on `public.users.password_hash` and `INSERT` on `public.d1_hash_import_ledger` (migration 0017
must be deployed first), and must not be a superuser (default `hyperchat_app`).

**Run (as root on the VPS, in your own SSH terminal):**
```
cd /root/d1-import/database
/opt/hyperchat/releases/<release>/venv/bin/python -B -E -s -m etl.auth_hash_import            # dry run
/opt/hyperchat/releases/<release>/venv/bin/python -B -E -s -m etl.auth_hash_import --apply    # writes, after the phrase
```
Exit codes: 0 done (dry run, nothing to do, or APPLIED printed with the source lock proven held through the commit), 2
refused (nothing written; includes a source lock not granted in time), 3 confirmation not typed, 4 APPLIED but the
source lock could not be proven held through the commit (alert), 5 the target COMMIT got no reliable answer (the
import MAY HAVE COMMITTED: check `import_status.sql` before anything else), 1 error before the COMMIT or a COMMIT the
server rejected (rolled back; nothing written), 130 cancelled before writing. For every outcome after `--apply`,
follow "Recovery after `--apply`" in `docs/operations/d1-auth-freeze/README.md`.

**Cutover order:** (1) resolve account-specific identity conflicts and recovery paths; (2) schedule the maintenance window and temporarily freeze legacy password-change/reset writes while allowing ordinary legacy sign-ins; drain and verify all in-flight password mutations have completed; (3) take the final source snapshot/dump only after the drain, then run the ETL and fresh read-only reconciliation report; (4) run the importer dry run against that final snapshot and review every eligibility/exclusion count; (5) obtain separate explicit operator approval before `--apply` on the production database (prompted name, e.g. `hyperchat` on port 5432); (6) while legacy password mutations remain frozen and V2 authentication remains inactive, re-run the report/dry run, reconcile applied counts, and test migrated-account sign-in; (7) switch authentication only after all checks pass; (8) remove the temporary Supabase role and importer directory only after successful verification.

If any source password mutation occurs after the snapshot, the snapshot is invalid and the report/dry run must be repeated. If cutover aborts before `--apply`, keep V2 authentication inactive and resume legacy mutations only after confirming legacy remains authoritative. If it aborts after `--apply`, do not re-run the importer or resume legacy mutations by default: target hashes are not overwritten by the importer. Keep V2 authentication inactive and follow a separately approved rollback/reconciliation plan before retrying.

**Rollback:** undo one run, identified by its `import_run_id`, never by hash format:
```
psql -X -v ON_ERROR_STOP=1 -v run_id=<import_run_id> -f docs/operations/d1-auth-freeze/import_status.sql
psql -X -v ON_ERROR_STOP=1 -v run_id=<import_run_id> -f docs/operations/d1-auth-freeze/rollback_import.sql
```
It clears `password_hash` only where it is still exactly the hash that run wrote (the ledger's SHA-256). Accounts whose
hash changed since, for example upgraded to Argon2id at a V2 sign-in (which does not set `password_changed_at`), are
listed by `import_status.sql` and left untouched for an explicit decision. See "Recovery after `--apply`" in
`docs/operations/d1-auth-freeze/README.md`.
