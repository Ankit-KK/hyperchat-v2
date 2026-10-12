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

## Fingerprint, snapshots and what each check can see
The dry run prints `source_fingerprint`: SHA-256, computed **inside PostgreSQL**, over every `auth.users` row in `id`
order, serialized as `"<chars>:<value>|"` per field (`"-|"` for NULL) for `id`, exact `email`, the password hash
(empty = none), then `d0|`/`d1|` (deleted) and `b0`/`b1` (banned now), one row per line, prefixed with
`hyperchat-d1-source-v1`. No hash leaves the server to compute it. `email_confirmed_at` is only counted by the
importer, so it is not part of it.

`--apply` requires `--expected-fingerprint <the reviewed value>`. It then works with the source like this:

1. **Plan snapshot** (REPEATABLE READ): freeze check, fingerprint and the hashes to import, all from one snapshot, so
   the hashes are exactly the fingerprinted rows; a fingerprint that differs from the reviewed one is refused before
   the confirmation prompt. This snapshot **cannot see anything committed after it began**: a later change, including
   one made by someone bypassing the freeze, leaves its fingerprint and its hashes unchanged. Hence step 2.
2. **Source lock** (after the operator has typed the confirmation): a fresh source transaction takes
   `LOCK TABLE auth.users IN SHARE MODE` (waiting at most 5 s for writes already in progress), then, under the lock,
   checks that the freeze is exactly the reviewed one, that the fingerprint equals the reviewed value, and that every
   planned hash is still the current hash of an eligible account. Any failure: refused, nothing written. The snapshot
   is taken after the lock is granted, so it is the current state.
3. **Target transaction under the lock:** the target rows are locked, re-checked and written; just before the target
   COMMIT the importer checks that its source transaction is still alive and still holds the lock (otherwise: rolled
   back, nothing written); then the target commits **while the source lock is still held**.
4. **Proof and release:** right after the commit, the importer checks once more that the same source transaction still
   holds the lock. PostgreSQL keeps a table lock until its transaction ends and ends it on any error, so success proves
   the lock was held without a break from step 2 until after the commit: no source row could change in between. Then
   the lock is released. If that proof fails (the source session ended in the last instant), the run prints `ALERT`
   and exits **4**. That is **detection, not prevention**: the import has already committed.

While the lock is held, PostgreSQL makes every insert, update, delete and `TRUNCATE` on `auth.users` wait, including
writes with `session_replication_role = replica`, and also enabling, disabling, dropping or replacing the trigger
(verified on PostgreSQL 17.11). It does **not** stop a superuser from replacing the trigger's function or editing
`pg_trigger` directly; neither can change account data while the lock is held, but either can weaken the freeze for
the time after the release. Reads are not blocked.

| Source change | When | Outcome |
|---|---|---|
| Any change to a fingerprinted field | before the dry run that was reviewed | part of the reviewed fingerprint |
| Same | between that review and the start of the `--apply` run | **detected**: plan fingerprint differs, refused before the prompt |
| Same, or the freeze dropped / disabled / changed | after the plan snapshot, before the source lock is granted (e.g. while the operator is at the prompt) | **detected** under the lock: refused, nothing written |
| Any write to `auth.users`, `TRUNCATE`, trigger disable / drop / replace, replica-mode writes | while the source lock is held (verification until after the target commit) | **prevented**: it waits for the lock and lands only after the commit (tested for each) |
| The source session ends (a privileged `pg_terminate_backend`, the 20 s idle limit, a network failure), then the source changes | before the pre-commit lock check | **detected**: the target rolls back, nothing written |
| Same | in the instant after the pre-commit lock check, before the target COMMIT | **not prevented**; **detected** by the post-commit proof: exit 4, rollback needed |
| A superuser replaces the trigger function or edits `pg_trigger` | while the lock is held | not prevented and not detected by this run; cannot change data until the release; caught by step 10 |
| Any change | after the release, until users are switched to V2 | **prevented** only by the active freeze; a privileged bypass is detected only by comparing a later fresh dry run (procedure step 10) |
| A privileged change that restores the identical values | any time | harmless to the import (same fingerprint) |
| Non-fingerprinted columns (sign-in times, tokens, `email_confirmed_at`, ...) | any time | not checked, by design: they don't affect the import |

**Who may take the lock.** The source role used for `--apply` needs `MAINTAIN` on `auth.users` (PostgreSQL 17 and
later; it allows the lock without any write privilege) or, on earlier versions, table-level `UPDATE`, `DELETE` or
`TRUNCATE` (column-level `UPDATE` is not enough). On earlier versions the role is therefore writable and the importer
refuses it unless `--accept-writable-source-role` is given; the importer's own source transaction stays `READ ONLY`
either way (tested). A role without the privilege is refused before anything is written. The dry run does not lock
and keeps working with the read-only role. Which PostgreSQL version the legacy project runs, and whether such a grant
can be made there, is **unverified** (rehearsal step 9). If it cannot, `--apply` cannot run: there is no fallback
mode without the lock. The source role must also bypass row-level security (`BYPASSRLS`): Supabase enables it on
`auth.users` with no policies, and both D1 tools read with `row_security = off`, so a role it applies to is refused
(dry run and plan) or raises under the lock, instead of silently seeing no rows (`database/README.md`).

**Effect on legacy sign-ins (GoTrue writes).** While the lock is held, every write to `auth.users` waits; a sign-in
updates `last_sign_in_at`, so sign-ins pause and then complete (reads, e.g. fetching the current user, do not wait).
Measured on the throwaway PostgreSQL 17.11 cluster with local connections, importing 134 synthetic accounts while a
loop of sign-in style updates ran: lock requested to released 17, 17 and 22 ms in three runs; the longest sign-in
update 17 to 24 ms; none over 50 ms. In production the importer talks to Supabase over the network: after the lock is
granted it makes about seven more round trips to the source before releasing (freeze check, fingerprint, two to
re-read the hashes, the lock check before and after the commit, the release), and the target writes are local to the
VPS. Expect a pause of roughly 7 to 8 times the round-trip time plus local work (about 0.4 s at 50 ms, 1.2 s at
150 ms). Worst cases: while the importer waits for the lock (at most 5 s), new sign-in writes queue behind its request;
a stalled importer is cut off by the server after 20 s idle inside the locked transaction, which frees the lock and
rolls the import back (or, in the last instant, exits 4). Whether GoTrue's own timeouts tolerate such a pause is
**unverified** (rehearsal step 10).

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
8. `python -m etl.auth_hash_import --apply --expected-fingerprint <recorded value>` with the lock-capable source role,
   then the typed phrase. Exit 0 = committed with the source lock proven held through the commit; 2 = refused, nothing
   written (e.g. the lock was not granted within 5 s: run it again); 4 = committed, but the lock could not be proven
   held through the commit (abort rules).
9. Reconciliation: dry run again: `eligible` 0, `target_already_has_password` up by the applied count,
   `source_fingerprint` unchanged, `source_freeze active`; then the read-only report.
10. Immediately before switching users to V2, one more dry run: `source_fingerprint` must still equal the recorded
    value and `source_freeze active`. This is the only check that catches a privileged bypass made after the
    import committed. If it differs, do not switch: abort rules.
11. Keep the freeze on until users are switched to V2. Run `unfreeze.sql` only per the rules below.

## Abort rules
- **Before `--apply`:** nothing was written to V2. `unfreeze.sql`; legacy continues.
- **Exit 4:** the import committed, but the source session ended at the last instant, so the source may have
  changed. Keep V2 sign-in off and legacy frozen and run a fresh dry run at once: if `source_fingerprint` still equals
  the recorded value and the freeze is active, the import matches the source; otherwise treat it as below.
- **A later fingerprint mismatch (step 10, or after exit 4):** the source changed after the import committed. Keep V2
  sign-in off and legacy frozen, undo the import with the rollback in `database/README.md`, find out who changed the
  source and why, then start again from a fresh report and dry run.
- **After `--apply`, before the switch:** keep the freeze. If legacy must be unfrozen, first undo the import in V2 with
  the rollback in `database/README.md` (clears the imported bcrypt hashes; accounts already upgraded to Argon2id keep
  theirs). The importer never overwrites a target hash, so a later re-import cannot repair hashes made stale by legacy
  password changes.
- **After the switch:** legacy auth is retired; remove the freeze when Supabase is decommissioned.

## Limitations (known)
- **Privileged bypass cannot be prevented, only detected (within limits).** The table owner or a superuser (on
  Supabase: the dashboard `postgres` role, the SQL editor, anyone with the database password, and the GoTrue service
  role `supabase_auth_admin`) can drop or disable the trigger, `TRUNCATE auth.users` (row triggers don't fire), or,
  unless the trigger is ENABLE ALWAYS, write with `session_replication_role = replica`. PostgreSQL gives no way to stop
  a superuser outright. What the importer adds: changes made before the source lock is granted are detected; while the
  lock is held, data changes are prevented (they wait), except that ending the importer's source session frees the
  lock (then: rolled back, or exit 4 in the last instant); after the release only the freeze, the operator controls
  and step 10 remain.
- **`ENABLE ALWAYS`** only closes the `session_replication_role = replica` path, and only for non-owners; it needs
  ownership of `auth.users` to set (unverified on Supabase). It does not stop an owner or superuser from disabling
  or dropping the trigger.
- **The source lock is short but real:** it is taken only after the confirmation and released right after the target
  commit, but legacy sign-ins pause for its duration (see "Effect on legacy sign-ins" above), and it needs the
  lock privilege described above (on PostgreSQL before 17, a writable role).
- **Required operator controls for the window:** only the named operator holds the legacy database password, the
  service-role key and dashboard access during the window (others' sessions ended, keys not shared); no other
  migrations, SQL editor or dashboard user edits; nobody ends other sessions (`pg_terminate_backend`) during the run;
  a second person watches the import; the fingerprint is recorded at review, at `--apply` and at step 10;
  afterwards, check the Supabase logs for `ALTER TABLE` / `DROP TRIGGER` / `TRUNCATE` / `CREATE OR REPLACE FUNCTION`
  on `auth.users` or `hyperchat_ops` during the window.
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
9. **Lock privilege:** note `SHOW server_version`. As the role intended for `--apply` (MAINTAIN on PostgreSQL 17+,
   table UPDATE before that): `BEGIN READ ONLY; SET LOCAL lock_timeout = '5s'; LOCK TABLE auth.users IN SHARE MODE;
   ROLLBACK;` must succeed. Record how that role was granted the privilege and by whom.
10. **GoTrue during the lock:** in psql, `BEGIN READ ONLY; LOCK TABLE auth.users IN SHARE MODE;`, hold it for 3 s,
    and meanwhile sign in through the API: the sign-in must complete (not fail) once you `ROLLBACK`; record its
    latency and any GoTrue error. Repeat with 10 s to see where GoTrue's own timeouts are.
11. **Round trips:** from the host that will run the importer, time `SELECT 1` against the Supabase pooler
    (`\timing on` in psql, several runs) and estimate the sign-in pause as 7 to 8 times that plus local work.
12. Optional, closer to production permissions: repeat steps 3–10 on a **new, empty** hosted Supabase project (no real
    users) to check that the dashboard role can run `freeze.sql`, can grant the lock privilege, and whether
    `ENABLE ALWAYS` is allowed.
13. `supabase stop --no-backup`. Record every result (status codes, error text, before/after md5, timings) in the PR
    or the D1 checklist row. Only then may the freeze and the lock be called rehearsed.
