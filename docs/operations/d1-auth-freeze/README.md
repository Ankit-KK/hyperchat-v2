# D1 auth freeze (legacy Supabase), for the production auth cutover

**Status: PROPOSED, NOT REHEARSED, NOT PRODUCTION-READY.** The SQL here has been exercised only against plain
PostgreSQL 17 test fixtures (`database/tests/test_auth_hash_import.py`). It has **not** been run against GoTrue or
any Supabase project. Nothing in this folder may be run against the legacy production project until the rehearsal
below has passed and the owner has approved it for the maintenance window.

## Why
The D1 importer reads the live Supabase `auth.users` (not a dump). Between the reviewed dry run and `--apply`, and
until users are switched to V2, a legacy password change, email change, new or deleted account, or ban would either
be silently lost or change who is imported. Legacy password changes go straight from the browser to GoTrue (no
backend of ours in between), so only a database-level freeze can stop them (feasibility audit, 2026-10-12).

## Files
| File | What it does |
|---|---|
| `freeze.sql` | Installs `hyperchat_ops.d1_auth_freeze_guard()` and the trigger `hyperchat_d1_auth_freeze` (BEFORE INSERT OR UPDATE OR DELETE, FOR EACH ROW) on `auth.users`. One transaction, idempotent (`CREATE OR REPLACE`), `lock_timeout` 10 s. |
| `verify.sql` | Read-only: shows the trigger's enabled state, type, column list, WHEN, function and the function body's SHA-256. |
| `unfreeze.sql` | Removes the trigger and the function. Idempotent. |

## What is frozen, and what is not
- **Rejected:** `INSERT` (sign-up, admin-created users, first-time OAuth users), `DELETE` (admin deletion), and any
  `UPDATE` that changes `id`, `email`, `encrypted_password`, `deleted_at` or `banned_until`, from any path: browser
  (`PUT /auth/v1/user`), recovery links, the admin API, the dashboard, SQL. These are exactly the fields the importer
  matches on or decides eligibility from (and that the source fingerprint covers).
- **Allowed:** every other `UPDATE`, e.g. `last_sign_in_at` / `updated_at` written by a sign-in and the recovery-token
  columns written when someone requests a reset email (finishing that reset is then rejected). Sessions and refresh
  tokens live in other tables.
- **Account creation and deletion are frozen on purpose:** either one changes the reviewed import set (a deletion
  turns an eligible account into an orphan; a creation changes the fingerprint and could collide on email). Blocking
  them does not block sign-in for existing accounts. Expect GoTrue errors for sign-ups during the window.
- **Errors:** the trigger raises `P0001` with a message starting `hyperchat_d1_freeze:`. GoTrue will most likely
  return a generic server error to the browser (unverified; see rehearsal).

## Proof that the freeze is in place
A trigger name proves nothing. The importer (`etl/auth_hash_import.py`, `freeze_problem`) accepts the freeze only if,
on the source database:
1. exactly the trigger `hyperchat_d1_auth_freeze` exists on `auth.users` and is enabled (`tgenabled` `O`, or `A` if
   made ENABLE ALWAYS); `D` (disabled) and `R` (replica only) are refused;
2. it is row-level, BEFORE, on INSERT, UPDATE and DELETE (`tgtype` 31), without a column list or WHEN condition;
3. it calls `hyperchat_ops.d1_auth_freeze_guard` in `plpgsql`, whose body hashes (SHA-256, CR removed) to the value
   pinned in `FREEZE_FUNCTION_SHA256` (a test pins that value to the body in `freeze.sql`).

Every failure prints why (`missing`, `not enabled ('D')`, `malformed`, `calls <other function>`, `body differs`) and
stops `--apply` before anything is written. `verify.sql` shows the same facts to the operator.

## Fingerprint and the time-of-check problem
The dry run prints `source_fingerprint`: SHA-256, computed **inside PostgreSQL**, over every `auth.users` row in `id`
order, serialized as `"<chars>:<value>|"` per field (`"-|"` for NULL) for `id`, exact `email`, the password hash
(empty = none), then `d0|`/`d1|` (deleted) and `b0`/`b1` (banned now), one row per line, prefixed with
`hyperchat-d1-source-v1`. No hash leaves the server to compute it. `email_confirmed_at` is only counted by the
importer, so it is not part of it.

`--apply` requires `--expected-fingerprint <the reviewed value>`. In the same run, the freeze check, the fingerprint
and the hashes to import are all read in **one REPEATABLE READ snapshot** of the source, so a matching fingerprint
proves that the hashes being written are exactly the reviewed set (a test commits a change between the fingerprint
query and the hash read: the run still uses the snapshot's values). Changes after that snapshot cannot affect what is
written; they would only be lost updates in legacy, which is what the freeze prevents. Keep the freeze on until users
are switched to V2.

A ban that expires on its own between the dry run and `--apply` flips `b1` to `b0`, changes the fingerprint and makes
`--apply` refuse: re-run the report and the dry run.

## Cutover procedure (each step needs the approvals in the D1 policy)
1. Rehearsal (below) passed; the owner approves the window.
2. Optional: a maintenance notice on the legacy forgot / reset pages (cosmetic only; it enforces nothing).
3. Run `freeze.sql` in the legacy project as a role that can create triggers on `auth.users`. If it fails with
   `lock timeout`, nothing was installed: run it again. A commit means every in-flight write to `auth.users` has
   finished (the drain).
4. Run `verify.sql` and compare with "Proof" above.
5. Canary (owner's decision how): with a dedicated canary account created **before** the freeze, a password change
   through the real site must fail, and signing in must still work.
6. Fresh read-only report (`etl.auth_readonly_report`), then the importer dry run: it must print
   `source_freeze active`. Record `source_fingerprint`; review every exclusion count.
7. Owner approval for production `--apply`.
8. `python -m etl.auth_hash_import --apply --expected-fingerprint <recorded value>`, then the typed phrase.
9. Reconciliation: dry run again: `eligible` 0, `target_already_has_password` up by the applied count,
   `source_fingerprint` unchanged, `source_freeze active`; then the read-only report.
10. Keep the freeze on until users are switched to V2. Run `unfreeze.sql` only per the rules below.

## Abort rules
- **Before `--apply`:** nothing was written to V2. `unfreeze.sql`; legacy continues.
- **After `--apply`, before the switch:** keep the freeze. If legacy must be unfrozen, first undo the import in V2 with
  the rollback in `database/README.md` (clears the imported bcrypt hashes; accounts already upgraded to Argon2id keep
  theirs). The importer never overwrites a target hash, so a later re-import cannot repair hashes made stale by legacy
  password changes.
- **After the switch:** legacy auth is retired; remove the freeze when Supabase is decommissioned.

## Limitations (known)
- **Privileged bypass:** the table owner or a superuser can drop or disable the trigger, `TRUNCATE auth.users` (row
  triggers don't fire) or, unless the trigger is ENABLE ALWAYS, write with `session_replication_role = replica`. The
  checks prove the trigger is present and correct at that moment; they cannot prove nobody with those rights bypassed
  it. The fingerprint comparison still catches any change to the fingerprinted fields between review and `--apply`.
- **Unverified on Supabase:** whether the dashboard `postgres` role may create a trigger on `auth.users` today (the
  legacy project has an older `AFTER INSERT` trigger there), and whether `ENABLE ALWAYS` is permitted (needs
  ownership; `auth.users` is owned by `supabase_auth_admin`).
- **Unverified GoTrue behaviour:** that sign-in and token refresh never change the frozen columns (if a sign-in rewrote
  `encrypted_password`, that sign-in would fail while frozen), what users see on rejected requests, and how email
  change confirmation and OAuth sign-ins behave.
- **Lock queue:** while `freeze.sql` waits (at most 10 s), sign-ins that update `auth.users` wait behind it.
- **Scope:** only `auth.users` is frozen. Other GoTrue tables (identities, sessions) are not part of the import.

## Staging rehearsal (must pass before any production use)
Run on a workstation with Docker, never against a hosted project with real users. Record the Supabase CLI version, the
GoTrue (`supabase/gotrue`) image tag and the Postgres version, and compare them with the legacy project's (dashboard)
before relying on the result.

1. `supabase init` in an empty directory, then `supabase start`. Note the API URL (`http://127.0.0.1:54321`), the DB
   URL (`postgresql://postgres:postgres@127.0.0.1:54322/postgres`), the local demo anon and service-role keys, and the
   mail inbox URL (Inbucket / Mailpit, `http://127.0.0.1:54324`). These are local demo values only.
2. Create a canary user with the admin API (`POST /auth/v1/admin/users`, service-role key, `email_confirm: true`) and
   a second user for the deletion check.
3. Sign in (`POST /auth/v1/token?grant_type=password`, anon key); keep the access and refresh tokens (session S0).
   Record `md5(encrypted_password)` and `email` of the canary with psql (local demo data only).
4. **Drain:** in psql session A: `BEGIN; UPDATE auth.users SET encrypted_password = crypt('Drain-Pass-1', gen_salt('bf'))
   WHERE email = '<canary>';` (do not commit). In session B: `\i freeze.sql`. B must wait. Commit A; B must complete.
   The canary's new hash must be in place (B ran after it).
5. `\i verify.sql`: one row, `enabled` O, `tgtype` 31, empty column list, no WHEN, function
   `hyperchat_ops.d1_auth_freeze_guard`, `body_sha256` equal to `FREEZE_FUNCTION_SHA256`.
6. **While frozen, expect these to WORK:** password sign-in (200); refresh-token grant (200); `GET /auth/v1/user` with
   S0 (200). Afterwards no `hyperchat_d1_freeze` error in the auth container's log (`docker logs supabase_auth_<project>`).
7. **While frozen, expect these to FAIL, and the canary's hash / email to be unchanged:**
   - `PUT /auth/v1/user {"password": ...}` with a session;
   - recovery: `POST /auth/v1/recover` (the email is sent), open the link from the inbox (`/auth/v1/verify`), then
     `PUT /auth/v1/user {"password": ...}` with the recovery session;
   - `PUT /auth/v1/admin/users/<id> {"password": ...}` with the service-role key;
   - `UPDATE auth.users SET encrypted_password = ...` in psql (error `hyperchat_d1_freeze: password changes ...`);
   - email change: `PUT /auth/v1/user {"email": ...}` (GoTrue may first store it in `email_change`, which is allowed;
     confirming it must fail) and `PUT /auth/v1/admin/users/<id> {"email": ...}`;
   - `POST /auth/v1/signup` and `DELETE /auth/v1/admin/users/<second user>`.
8. `\i unfreeze.sql`, then repeat step 7: each must now succeed.
9. Optional, closer to production permissions: repeat steps 3–8 on a **new, empty** hosted Supabase project (no real
   users) to check that the dashboard role can run `freeze.sql` and whether `ENABLE ALWAYS` is allowed.
10. `supabase stop --no-backup`. Record every result (status codes, error text, before/after md5) in the PR or the
    D1 checklist row. Only then may the freeze be called rehearsed.
