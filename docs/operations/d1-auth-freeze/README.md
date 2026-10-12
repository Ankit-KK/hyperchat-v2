# D1 auth freeze (legacy Supabase), for the production auth cutover

**Status: PROPOSED, REHEARSED LOCALLY ONLY, NOT PRODUCTION-READY.** The SQL is tested against plain PostgreSQL 17
fixtures (`database/tests/test_auth_hash_import.py`) and was rehearsed against GoTrue on a **local, disposable**
Supabase stack (GoTrue v2.197.0, PostgreSQL 17.11; results below and in
[`rehearsal-2026-10-12-local.md`](rehearsal-2026-10-12-local.md)). It has **not** been run on any hosted Supabase
project, and the hosted permission checks are still open. Nothing in this folder may be run against the legacy
production project until those checks have passed and the owner has approved it for the maintenance window.

## Why
The D1 importer reads the live Supabase `auth.users` (not a dump). Between the reviewed dry run and `--apply`, and
until users are switched to V2, a legacy password change, email change, new or deleted account, or ban would either
be silently lost or change who is imported. Legacy password changes go straight from the browser to GoTrue (no
backend of ours in between), so only a database-level freeze can stop them (feasibility audit, 2026-10-12).

## Files
| File | What it does |
|---|---|
| `freeze.sql` | Installs `hyperchat_ops.d1_auth_freeze_guard()` and the trigger `hyperchat_d1_auth_freeze` (BEFORE INSERT OR UPDATE OR DELETE, FOR EACH ROW) on `auth.users`. One transaction, idempotent (`CREATE OR REPLACE`), `lock_timeout` 10 s. A trigger already made ENABLE ALWAYS stays ALWAYS; if the running role cannot restore that (not the table owner), the whole script fails and changes nothing. |
| `verify.sql` | Read-only: shows the trigger's enabled state, type, column list, WHEN, function and the function body's SHA-256. |
| `unfreeze.sql` | Removes the trigger and the function. Idempotent. |
| `import_status.sql` | V2 target, read-only: what one import run (`import_run_id`) wrote, whether each hash is still exactly that, and which accounts changed since. |
| `rollback_import.sql` | V2 target: undoes one import run, clearing only hashes still exactly as that run wrote them. Idempotent. |

## What is frozen, and what is not
- **Rejected:** `INSERT` (sign-up, admin-created users, first-time OAuth users), `DELETE` (admin deletion), and any
  `UPDATE` that changes `id`, `email`, `encrypted_password`, `deleted_at` or `banned_until`, from any path: browser
  (`PUT /auth/v1/user`), recovery links, the admin API, the dashboard, SQL. These are exactly the fields the importer
  matches on or decides eligibility from (and that the source fingerprint covers).
- **Allowed:** every other `UPDATE`, e.g. `last_sign_in_at` / `updated_at` written by a sign-in and the recovery-token
  columns written when someone requests a reset email (finishing that reset is then rejected). Sessions and refresh
  tokens live in other tables, but the refresh-token grant also writes to `auth.users` (in the rehearsal it waited for
  the source lock like a sign-in); it changes no frozen column and works while frozen.
- **Account creation and deletion are frozen on purpose:** either one changes the reviewed import set (a deletion
  turns an eligible account into an orphan; a creation changes the fingerprint and could collide on email). Blocking
  them does not block sign-in for existing accounts. Expect GoTrue errors for sign-ups during the window.
- **Errors (rehearsed locally, GoTrue v2.197.0):** the trigger raises `P0001` with a message starting
  `hyperchat_d1_freeze:`. GoTrue passes it through: API calls (sign-up, admin create, password change by the user, by
  recovery session or by the admin API, admin email change, ban, delete) answer **HTTP 500** with JSON `code: P0001`
  and `message: "hyperchat_d1_freeze: <reason> is paused for maintenance"`. The email-change confirmation link answers
  **303** to the site URL with `error=server_error`, `error_code=unexpected_failure`,
  `error_description=Error confirm email`. Users see a failure, not a maintenance page; a notice on the legacy pages
  (procedure step 2) is the only friendlier message.

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

**Effect on legacy sign-ins and token refreshes (GoTrue writes).** While the lock is held, every write to
`auth.users` waits; a sign-in updates `last_sign_in_at` and a refresh-token grant also writes the user row, so both
pause and then complete (reads, e.g. fetching the current user, do not wait). Browsers refresh tokens in the
background, so already signed-in users see the pause too, not only people signing in.
Measured on the throwaway PostgreSQL 17.11 cluster with local connections, importing 134 synthetic accounts while a
loop of sign-in style updates ran: lock requested to released 17, 17 and 22 ms in three runs; the longest sign-in
update 17 to 24 ms; none over 50 ms. In production the importer talks to Supabase over the network: after the lock is
granted it makes about seven more round trips to the source before releasing (freeze check, fingerprint, two to
re-read the hashes, the lock check before and after the commit, the release), and the target writes are local to the
VPS. Expect a pause of roughly 7 to 8 times the round-trip time plus local work (about 0.4 s at 50 ms, 1.2 s at
150 ms). Worst cases: while the importer waits for the lock (at most 5 s), new sign-in writes queue behind its request;
a stalled importer is cut off by the server after 20 s idle inside the locked transaction, which frees the lock and
rolls the import back (or, in the last instant, exits 4). **Local rehearsal (GoTrue v2.197.0, one host):** with the
lock held 3 s, a sign-in took 2.8 s and a refresh 2.7 s; held 10 s, 9.8 s and 9.7 s; all answered 200, each about
10 ms after the release, with no GoTrue error or timeout; `GET /auth/v1/user` stayed at about 40 ms. A sign-in
queued behind `freeze.sql` waiting on an open write took 8.6 s (10 s lock timeout), and one queued behind the
importer's lock request 4.0 s (5 s timeout). An import of 3 accounts held the lock 8 to 10 ms, with concurrent
sign-ins unaffected. Hosted timeouts (Kong, the pooler, the browser client) and the production round-trip time (step
11) are **unverified**.

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
- **Any outcome of `--apply`** (exit 0, 1, 4 or 5, or a later fingerprint mismatch): follow "Recovery after
  `--apply`" below. Until it says otherwise, keep legacy frozen and V2 sign-in off.
- **After `--apply`, before the switch:** keep the freeze. If legacy must be unfrozen, first undo the run with
  `rollback_import.sql` (below). The importer never overwrites a target hash, so a later re-import cannot repair hashes
  made stale by legacy password changes.
- **After the switch:** legacy auth is retired; remove the freeze when Supabase is decommissioned.

## Recovery after `--apply`
Every `--apply` prints `import_run_id <uuid>` **before** it writes. Record it with the reviewed fingerprint.

The run writes the hashes and one ledger row per account (`public.d1_hash_import_ledger`, migration 0017) in **one
transaction**: its ledger rows exist if and only if its hashes were committed.

On the V2 target:
- `import_status.sql -v run_id=<id>` shows:
  - `imported` (0 = not committed; the planned count = committed);
  - `still_imported_hash`;
  - `cleared`;
  - `changed_since_import`, and the user ids behind it.
- `rollback_import.sql -v run_id=<id>` clears only hashes that are still exactly what that run wrote.

**Throughout:** legacy stays frozen and V2 sign-in stays off until the outcome is established, and, if needed, the run
is rolled back.

| What happened | What it means | What to do |
|---|---|---|
| **Exit 0**, `APPLIED` printed | Committed, and the source lock was proven held through the commit | `import_status.sql`: `imported` = the APPLIED count. Continue the cutover procedure (step 10, a fresh dry run, before switching users). |
| **Exit 2 or 3** | Refused or cancelled before anything was written | No V2 change. Fix the cause; re-run from a reviewed dry run. |
| **Exit 1** (`failed: … nothing was written by this run`) | An error before the COMMIT, or a COMMIT the server rejected on a live connection: rolled back | Optional proof: `import_status.sql` shows `imported` = 0. Re-run only from a reviewed dry run. |
| **Exit 5** (`the import MAY HAVE COMMITTED`) | The COMMIT got no reliable answer (connection lost, interrupted): **outcome unknown** | **Do not retry, do not unfreeze.** Run `import_status.sql` with the printed run id: `imported` = the planned count means it committed (go to the exit 0 row, then step 10); 0 means it rolled back (exit 1 row). **Any other count, or a target you cannot reach: stop and escalate.** |
| **Exit 4** (`ALERT`) | Committed, but the source lock was not proven held through the COMMIT: the source may have changed in that instant | Run a fresh dry run at once. **Fingerprint equal** to the reviewed value **and** freeze active: the import matches the source; keep it (`import_status.sql`: `still_imported_hash` = `imported`) and continue. **Fingerprint different, or freeze not active:** roll back this run (next row). |
| **Fingerprint mismatch** (after exit 4, or at step 10) | The source changed after this run's snapshot; some imported hashes may be stale | 1. `import_status.sql`, and note `changed_since_import`. 2. `rollback_import.sql` (as the importer's target role, V2 sign-in off). 3. `import_status.sql` again: `still_imported_hash` must be 0. 4. Decide every `changed_since_import` account (below). 5. Find who changed the source, and why. 6. Retry only as below. |

**Accounts changed after the import** (`changed_since_import`): their hash is no longer what the run wrote.
- **Why it happens:** a V2 sign-in with the imported bcrypt hash upgrades it to Argon2id. That includes the operator's
  own migrated-account sign-in check in procedure step 6. A password reset or change does the same.
- **Why no timestamp can be used:** the sign-in upgrade sets `last_login_at` but **not** `password_changed_at`, so only
  the ledger's digest identifies these accounts reliably.
- **The rollback never touches them.** After a fingerprint mismatch, such an account may hold a password derived from a
  stale legacy hash.
- **Each one needs an explicit owner decision**, recorded before the switch: keep it, or clear it (then reset, which
  needs the D8 credentials). Revoke its V2 sessions when clearing. **Never clear them in bulk by hash format.**

**When a retry is safe:** all of these must hold.
- The previous run's outcome is established: exit 0/1/2/3, or exit 5 resolved by `import_status.sql`.
- Any run to undo is rolled back (`still_imported_hash` = 0).
- The `changed_since_import` accounts are decided.
- The freeze is active.
- A **fresh** dry run has been reviewed and **its** fingerprint is passed to `--apply`. The old fingerprint is
  refused, as it should be.

A fresh dry run alone does **not** make a retry safe. The importer skips accounts that already hold a hash, so
re-running over an import that was not rolled back would keep stale hashes.

## Limitations (known)
- **Privileged bypass cannot be prevented, only detected (within limits).** The table owner or a superuser (on
  Supabase: the GoTrue service role `supabase_auth_admin` owns `auth.users`) can drop or disable the trigger,
  `TRUNCATE auth.users` (row triggers don't fire), or, unless the trigger is ENABLE ALWAYS, write with
  `session_replication_role = replica`. The dashboard role `postgres` (the SQL editor, anyone with the database
  password) is neither owner nor superuser, but in the local rehearsal it could do the same in practice: drop the
  trigger (`unfreeze.sql` worked), `TRUNCATE` (it holds the privilege), replace the guard function (it owns it), and
  **set `session_replication_role = replica` and update `email` past the `O` trigger** (verified in a rolled-back
  transaction). Since it cannot make the trigger ALWAYS (next point), that bypass stays open to anyone with the
  database password; hosted behaviour is unverified. PostgreSQL gives no way to stop a superuser outright. What the importer adds: changes made before the source lock is granted are detected; while the
  lock is held, data changes are prevented (they wait), except that ending the importer's source session frees the
  lock (then: rolled back, or exit 4 in the last instant); after the release only the freeze, the operator controls
  and step 10 remain.
- **`ENABLE ALWAYS`** only closes the `session_replication_role = replica` path, and only for non-owners; it needs
  ownership of `auth.users` to set. In the local rehearsal the dashboard role `postgres` got `42501 must be owner of
  table users`; only the owner `supabase_auth_admin` (not a login available on hosted projects) could set it. It does
  not stop an owner or superuser from disabling or dropping the trigger. Re-running `freeze.sql` used to reset an
  ALWAYS trigger to `O` silently (`CREATE OR REPLACE TRIGGER` does); the script now restores ALWAYS, or fails and
  changes nothing when the running role cannot (tested on plain PostgreSQL 17 and, as the dashboard role `postgres`, on the local Supabase image `supabase/postgres:17.11.0.004`; not yet on a hosted project). Treat the replica-mode bypass as an **open residual risk**
  covered only by the operator controls, the source lock (it blocks replica-mode writes while held) and step 10,
  unless an owner-level role on the hosted project can set ALWAYS and the owner decides to use it.
- **The source lock is short but real:** it is taken only after the confirmation and released right after the target
  commit, but legacy sign-ins pause for its duration (see "Effect on legacy sign-ins" above), and it needs the
  lock privilege described above (on PostgreSQL before 17, a writable role).
- **Required operator controls for the window:** only the named operator holds the legacy database password, the
  service-role key and dashboard access during the window (others' sessions ended, keys not shared); no other
  migrations, SQL editor or dashboard user edits; nobody ends other sessions (`pg_terminate_backend`) during the run;
  a second person watches the import; the fingerprint is recorded at review, at `--apply` and at step 10;
  afterwards, check the Supabase logs for `ALTER TABLE` / `DROP TRIGGER` / `TRUNCATE` / `CREATE OR REPLACE FUNCTION`
  on `auth.users` or `hyperchat_ops` during the window.
- **Verified locally, unverified on hosted Supabase:** the dashboard `postgres` role created, re-created and removed
  the trigger, could not set ENABLE ALWAYS, could set `session_replication_role`, and could not grant `MAINTAIN` on
  `auth.users` or `USAGE` on schema `auth` to another role (the `GRANT` only warns `no privileges were granted`; it
  could grant `BYPASSRLS`). The legacy project's PostgreSQL and GoTrue versions and its older `AFTER INSERT` trigger on
  `auth.users` are unverified.
- **GoTrue behaviour (local rehearsal, GoTrue v2.197.0):** password sign-in, refresh-token grants, `GET /user`,
  metadata updates, reset-email requests and opening a recovery link work while frozen and change no frozen column;
  every blocked path fails as described under "Errors". **Unverified:** OAuth sign-ins (a first-time OAuth user is an
  `INSERT`, so it fails), and the legacy project's GoTrue version.
- **Lock queue:** while `freeze.sql` waits (at most 10 s), sign-ins and token refreshes that update `auth.users` wait
  behind it (a sign-in waited 8.6 s in the rehearsal).
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
12. **Required before production use** (the local rehearsal showed Supabase-specific role behaviour that plain
    PostgreSQL and catalog queries do not predict, e.g. `has_parameter_privilege` said `postgres` could not set
    `session_replication_role`, yet it could): repeat the permission checks on a **new, empty** hosted Supabase project
    (no real users) as the dashboard role: `freeze.sql` twice and `verify.sql`; `ENABLE ALWAYS`; a replica-mode
    `UPDATE` in a rolled-back transaction; `unfreeze.sql` twice; the lock statement of step 9 as the intended source
    role; whether that role has or can be given `BYPASSRLS`; and `relrowsecurity` on `auth.users`.
13. `supabase stop --no-backup`. Record every result (status codes, error text, before/after md5, timings) in the PR
    or the D1 checklist row. Only then may the freeze and the lock be called rehearsed.

## Rehearsal results (local, 2026-10-12)
**Where:** a disposable local stack in a WSL2 distro on the owner's PC. It was torn down afterwards with
`supabase stop --no-backup`.

**Versions:**
- Supabase CLI 2.120.0;
- GoTrue `supabase/gotrue:v2.197.0`;
- `supabase/postgres:17.11.0.004` (PostgreSQL 17.11);
- Kong 2.8.1 and Mailpit v1.31.3.

**Data:** synthetic `@example.com` accounts and random passwords only. Code at `3cf791b`, i.e. PR #46. Every case
with status codes, sanitized GoTrue log lines and the frozen-field digest is in
[`rehearsal-2026-10-12-local.md`](rehearsal-2026-10-12-local.md).

| Area | Result |
|---|---|
| Install, verify, re-run, remove (steps 4–5, 8) | **Passed** as the dashboard role `postgres`. Details below. |
| Drain and timeout (step 4) | **Passed.** Details below. |
| While frozen (steps 6–7) | **Passed.** Details below. |
| After unfreeze (step 8) | **Passed:** every blocked operation succeeded and changed the field. |
| Lock privilege (step 9) | MAINTAIN is enough with `BEGIN READ ONLY`; a column-SELECT-only role gets `42501`. Details below. |
| Importer under the lock (PR #46) | **Passed:** writes, `TRUNCATE` and trigger DDL waited; reads did not; failure paths behaved as documented. Details below. |
| GoTrue during the lock (step 10) | **Passed.** See "Effect on legacy sign-ins and token refreshes". |

**Install, verify, re-run, remove:**
- `freeze.sql` committed in about 0.08 s and was idempotent.
- `verify.sql` matched the pinned hash, and the importer reported the freeze active.
- `unfreeze.sql` worked twice, and the importer then reported the trigger `missing`.

**Drain and timeout:**
- `freeze.sql` waited for an open password write and committed 7 ms after it.
- Against an open write held longer than 10 s, it gave up at 10.06 s (`lock timeout`) and installed nothing.

**While frozen:**
- **Worked:** password sign-in, chained refresh-token grants, `GET /user`, a metadata update, a reset-email request
  and opening a recovery link. All answered 200 or 303, and the frozen fields stayed unchanged.
- **Rejected:** sign-up, admin create, password change (by the user, by recovery session, by the admin API), the
  email-change confirmation, admin email change, ban, delete, and SQL `UPDATE` of every frozen column, `INSERT` and
  `DELETE`. Each failed with `hyperchat_d1_freeze`, left the frozen-field digest unchanged, and logged the cause in
  GoTrue.

**Lock privilege:**
- `postgres` already has MAINTAIN, plus BYPASSRLS.
- A dedicated MAINTAIN role needed the owners to grant it.

**Importer under the lock:**
- A 3-account import held the lock 8–10 ms.
- `UPDATE`, `INSERT`, `DELETE`, `TRUNCATE … CASCADE`, superuser replica-mode writes, `DISABLE TRIGGER`,
  `DROP TRIGGER` and `CREATE OR REPLACE TRIGGER` all waited. `SELECT` and `GET /user` did not.
- The lock timeout (5.00 s), a missing privilege, session loss before the pre-commit check (rolled back), session
  loss right after it (committed, post-commit proof false, exit 4), and an importer stalled past the 20 s idle limit
  (session ended at 20.0 s) all behaved as documented.

**Findings, and where each went:**
- **Row-level security hides `auth.users`.** A role without BYPASSRLS read 0 rows silently. → Fixed in #47 (F14):
  both tools refuse; the source role needs BYPASSRLS.
- **Re-running `freeze.sql` reset ENABLE ALWAYS to `O`.** → Fixed here: ALWAYS is kept, or the run fails and changes
  nothing.
- **`postgres` cannot set ALWAYS** but can write in replica mode past `O`. → Documented as an open residual risk
  (Limitations).
- **Token refresh also pauses under the lock and behind `freeze.sql`.** → Documented ("Effect on legacy sign-ins and
  token refreshes").
- **GoTrue answers rejected requests with HTTP 500** carrying the trigger message, or a 303 error redirect. →
  Documented ("Errors").
- **Email change completed on the first confirmation link** although `double_confirm_changes = true`. While frozen,
  both links fail; this is a GoTrue behaviour note only.

**Not covered:**
- hosted permissions (step 12);
- the legacy project's versions and its older trigger;
- OAuth sign-ins;
- the production round-trip time (step 11);
- `main()` end to end against a TLS source.

**Differences from hosted:**
- raised local rate limits (`email_sent` 100, `sign_in_sign_ups` 1000);
- no TLS;
- local demo credentials;
- the owner and superuser logins (`supabase_auth_admin`, `supabase_admin`) used for the documented elevated steps
  (ENABLE ALWAYS, the dedicated role's grants, owner-side lock probes) do not exist for users of hosted projects.
