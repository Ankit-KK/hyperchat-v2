# D1 auth freeze: local GoTrue rehearsal, 2026-10-12 (evidence)

**What this is:** the raw results behind "Rehearsal results" in [`README.md`](README.md).

**What this is not:** a rehearsal on a hosted Supabase project. It is not proof of the legacy project's behaviour,
and not an approval to run anything there.

**Environment:**
- **Host:** WSL2 distro on the owner's PC (Ubuntu 24.04.5, Docker Engine 29.1.3). Services were reachable from that
  PC only.
- **Stack:** Supabase CLI 2.120.0, auth-only, with every other service excluded:
  - GoTrue `public.ecr.aws/supabase/gotrue:v2.197.0`;
  - `supabase/postgres:17.11.0.004` (PostgreSQL 17.11);
  - Kong 2.8.1;
  - Mailpit v1.31.3.
- **Teardown:** `supabase stop --no-backup` afterwards.

**Code:** the D1 importer and these SQL files at `3cf791b` (PR #46).

**Data:**
- 3 synthetic `@example.com` accounts plus the sign-ups created by the tests.
- Random synthetic passwords.
- Hashes written only by GoTrue or pgcrypto for those passwords.

**Not touched:** the VPS, hosted Supabase projects, HyperChat databases, and real data.

**Local configuration differences from hosted:**
- `auth.rate_limit.email_sent` 2 → 100 and `sign_in_sign_ups` 30 → 1000, so the run could proceed;
- no TLS;
- local demo credentials.

**How to read the table:**
- **Frozen fields unchanged?** compares the row count plus the md5 over `id | email | encrypted_password |
  deleted_at | banned_until` of every `auth.users` row against the value recorded right after the freeze was
  installed.
- **Sanitized log evidence** is the auth container's error lines for that request. Query strings are stripped, and
  JWTs, keys, tokens and bcrypt hashes are redacted. Synthetic account UUIDs appear in admin-API paths.
- **Results:**
  - `PASS` / `FAIL`: compared with the stated expectation.
  - `OBSERVED`: recorded where the expectation was unknown, e.g. a permission that had to be discovered.

**The three `FAIL` rows:**
- **Section 6, run 1 (two rows):** run before the source role had `BYPASSRLS`.
  - It read 0 rows because Supabase enables row-level security on `auth.users` with no policies.
  - That is the finding fixed in #47 (security audit F14).
  - Section 6 was then repeated as "run 2" with `BYPASSRLS` granted, and all of run 2 passed.
- **Section 7, "SQL UPDATE id" after unfreeze:** fails because of the foreign key `identities_user_id_fkey`, with or
  without the freeze. While frozen, the trigger rejects it first.

**Elevated steps (local only; these logins do not exist for users of hosted projects):**
- **As the table owner `supabase_auth_admin`:**
  - `ENABLE ALWAYS`;
  - `GRANT MAINTAIN` to the dedicated rehearsal role;
  - owner-side probes under the lock (`DISABLE` / `DROP TRIGGER`, rolled back).
- **As the superuser `supabase_admin`:**
  - `GRANT USAGE ON SCHEMA auth` to the rehearsal roles;
  - replica-mode probes (rolled back).
- **As `postgres`:**
  - `ALTER ROLE … BYPASSRLS` on the two rehearsal roles;
  - creating the synthetic target database and roles;
  - `pg_terminate_backend` on the importer's own session, for the session-loss cases.

| # | Section | Test case | Expected | Actual | HTTP/SQL result | Frozen fields unchanged? | Sanitized log evidence | Result |
|---|---|---|---|---|---|---|---|---|
| 1 | 1 setup | versions + synthetic users + S0 | admin create 200 x3, sign-in 200 | ok | HTTP 200 | n/a | gotrue {'version': 'v2.197.0', 'name': 'GoTrue', 'status': 200} | PASS |
| 2 | 3 freeze install/permissions | freeze.sql as postgres (dashboard role), run 1 | commits; trigger + function installed | rc=0 in 0.08s; state={'triggers': [['hyperchat_d1_auth_freeze', 'O']], 'hyperchat_ops_functions': 1, 'hyperchat_ops_schema': 1} | (no output) | n/a | - | PASS |
| 3 | 3 freeze install/permissions | verify.sql after run 1 | 1 row: O, 31, no cols, no WHEN, guard fn, plpgsql, pinned sha256 | rows=[['hyperchat_d1_auth_freeze', 'O', 31, '', False, 'hyperchat_ops.d1_auth_freeze_guard', 'plpgsql', True]] | body_sha256 == pinned: True | n/a | - | PASS |
| 4 | 3 freeze install/permissions | importer freeze_problem() after run 1 | None (active) | None |  | n/a | - | PASS |
| 5 | 3 freeze install/permissions | freeze.sql run 2 (idempotent) | commits; still exactly one trigger, same definition | rc=0 in 0.06s; state={'triggers': [['hyperchat_d1_auth_freeze', 'O']], 'hyperchat_ops_functions': 1, 'hyperchat_ops_schema': 1} | psql:docs/operations/d1-auth-freeze/freeze.sql:27: NOTICE:  schema "hyperchat_ops" already exists, skipping | n/a | - | PASS |
| 6 | 3 freeze install/permissions | ENABLE ALWAYS as postgres | unverified (needs table ownership; auth.users owner is supabase_auth_admin) | error | SQLSTATE 42501: must be owner of table users | n/a | - | OBSERVED |
| 7 | 3 freeze install/permissions | ENABLE ALWAYS as supabase_auth_admin (table owner; ELEVATED, local only, documented) | tgenabled 'A'; importer accepts 'A' | ok ok rowcount=-1; state={'triggers': [['hyperchat_d1_auth_freeze', 'A']], 'hyperchat_ops_functions': 1, 'hyperchat_ops_schema': 1} | freeze_problem=None | n/a | - | PASS |
| 8 | 3 freeze install/permissions | replica-mode UPDATE email as postgres (rolled back) | postgres may not set it, or 'A' rejects it | error | SQLSTATE P0001: hyperchat_d1_freeze: email changes are paused for maintenance | yes | - | PASS |
| 9 | 3 freeze install/permissions | replica-mode UPDATE email as superuser with 'A' (rolled back) | rejected by the ALWAYS trigger | error | SQLSTATE P0001: hyperchat_d1_freeze: email changes are paused for maintenance | yes | - | PASS |
| 10 | 3 freeze install/permissions | freeze.sql re-run while ENABLE ALWAYS | observe: does CREATE OR REPLACE TRIGGER keep 'A'? | rc=0; state={'triggers': [['hyperchat_d1_auth_freeze', 'O']], 'hyperchat_ops_functions': 1, 'hyperchat_ops_schema': 1} | psql:docs/operations/d1-auth-freeze/freeze.sql:27: NOTICE:  schema "hyperchat_ops" already exists, skipping | n/a | - | OBSERVED |
| 11 | 3 freeze install/permissions | replica-mode UPDATE email as superuser with tgenabled=O (rolled back) | with 'O': passes (documented privileged bypass); with 'A': rejected | ok | ok rowcount=1 | yes | - | OBSERVED |
| 12 | 3 freeze install/permissions | unfreeze.sql run 1 as postgres | commits; trigger + function removed | rc=0 in 0.06s; state={'triggers': [], 'hyperchat_ops_functions': 0, 'hyperchat_ops_schema': 1} | (no output) | n/a | - | PASS |
| 13 | 3 freeze install/permissions | unfreeze.sql run 2 as postgres | commits; trigger + function removed | rc=0 in 0.06s; state={'triggers': [], 'hyperchat_ops_functions': 0, 'hyperchat_ops_schema': 1} | psql:docs/operations/d1-auth-freeze/unfreeze.sql:5: NOTICE:  trigger "hyperchat_d1_auth_freeze" for relation "auth.users" does not exist, skipping psql:docs/operations/d1-auth-freeze/unfreeze.sql:6: NOTICE:  function hyperchat_ops.d1_auth_freeze_guard() does not exist, skipping | n/a | - | PASS |
| 14 | 3 freeze install/permissions | verify.sql + importer after unfreeze | 0 rows; importer reports missing | rows=0 | freeze_problem='the trigger hyperchat_d1_auth_freeze is missing on auth.users (run freeze.sql)' | n/a | - | PASS |
| 15 | 4 write drain | 4a freeze.sql vs an uncommitted write held > 10 s | waits on Lock, fails with lock timeout after ~10 s | rc=3 after 10.06s; B wait=[('Lock', 'relation')] | psql:docs/operations/d1-auth-freeze/freeze.sql:27: NOTICE:  schema "hyperchat_ops" already exists, skipping psql:docs/operations/d1-auth-freeze/freeze.sql:51: ERROR:  canceling statement due to lock timeout | n/a | - | PASS |
| 16 | 4 write drain | 4a nothing installed after the timeout | no trigger, no function, schema unchanged | state={'triggers': [], 'hyperchat_ops_functions': 0, 'hyperchat_ops_schema': 1} (schema before=1) |  | n/a | - | PASS |
| 17 | 4 write drain | 4a GoTrue sign-in (other row) started while freeze.sql waits | queues behind the pending freeze lock, then completes | HTTP 200, 8555 ms (started ~1.5 s into the 10 s wait) |  | n/a | - | PASS |
| 18 | 4 write drain | 4b freeze.sql waits for an in-flight hash write (session A uncommitted) | B waits on Lock; no trigger while A is open | B wait=[('Lock', 'relation')]; running=True; trigger absent=True |  | n/a | - | PASS |
| 19 | 4 write drain | 4b commit A, then B completes | B commits right after A; A's hash is in place; freeze active | rc=0; B finished 7 ms after A's commit (total 1.54s); canary hash == A's write: True | psql:docs/operations/d1-auth-freeze/freeze.sql:27: NOTICE:  schema "hyperchat_ops" already exists, skipping | n/a | - | PASS |
| 20 | 4 write drain | 4b GoTrue sign-in with the drained hash (pgcrypto bf, cost 06) | 200 | HTTP 200, 63 ms |  | n/a | - | PASS |
| 21 | 4 write drain | 4b verify.sql + importer after the drain | active | verify ok=True | freeze_problem=None | n/a | - | PASS |
| 22 | 5 GoTrue while frozen | password sign-in #1 | 200; no freeze error in GoTrue log | HTTP 200, 68 ms |  | yes | no error lines | PASS |
| 23 | 5 GoTrue while frozen | password sign-in #2 | 200; no freeze error in GoTrue log | HTTP 200, 67 ms |  | yes | no error lines | PASS |
| 24 | 5 GoTrue while frozen | refresh-token grant #1 (chained, S0) | 200 | HTTP 200, 108 ms |  | yes | no error lines | PASS |
| 25 | 5 GoTrue while frozen | refresh-token grant #2 (chained, S0) | 200 | HTTP 200, 114 ms |  | yes | no error lines | PASS |
| 26 | 5 GoTrue while frozen | refresh-token grant #3 (chained, S0) | 200 | HTTP 200, 103 ms |  | yes | no error lines | PASS |
| 27 | 5 GoTrue while frozen | GET /user (S0) | 200 | HTTP 200, 63 ms |  | yes | no error lines | PASS |
| 28 | 5 GoTrue while frozen | PUT /user metadata update | 200; metadata stored | HTTP 200, 85 ms; stored='metadata-while-frozen' |  | yes | no error lines | PASS |
| 29 | 5 GoTrue while frozen | POST /signup (new synthetic email) | rejected; frozen fields unchanged; freeze error in log | HTTP 500, 95 ms  | code=P0001 message=hyperchat_d1_freeze: account creation is paused for maintenance | yes | error 500 POST /signup request completed failed to close prepared statement: ERROR: current transaction is aborted, commands ignored until end of transaction block (SQLSTATE 25P02): ERROR: hyperchat_d1_freeze: account creation is paused for maintenance (SQLSTATE P0001) | PASS |
| 30 | 5 GoTrue while frozen | admin POST /admin/users (create) | rejected; frozen fields unchanged; freeze error in log | HTTP 500, 93 ms  | code=P0001 message=hyperchat_d1_freeze: account creation is paused for maintenance | yes | error 500 POST /admin/users request completed failed to close prepared statement: ERROR: current transaction is aborted, commands ignored until end of transaction block (SQLSTATE 25P02): ERROR: hyperchat_d1_freeze: account creation is paused for maintenance (SQLSTATE P0001) | PASS |
| 31 | 5 GoTrue while frozen | PUT /user password (signed-in user) | rejected; frozen fields unchanged; freeze error in log | HTTP 500, 117 ms canary md5/email unchanged: True | code=P0001 message=hyperchat_d1_freeze: password changes are paused for maintenance | yes | error 500 PUT /user request completed ERROR: hyperchat_d1_freeze: password changes are paused for maintenance (SQLSTATE P0001) | PASS |
| 32 | 5 GoTrue while frozen | admin PUT /admin/users/<id> password | rejected; frozen fields unchanged; freeze error in log | HTTP 500, 102 ms canary md5/email unchanged: True | code=P0001 message=hyperchat_d1_freeze: password changes are paused for maintenance | yes | error 500 PUT /admin/users/39df4bf0-ab7f-46bc-864a-cb982485417b request completed ERROR: hyperchat_d1_freeze: password changes are paused for maintenance (SQLSTATE P0001) | PASS |
| 33 | 5 GoTrue while frozen | POST /recover (request reset email) | 200 (writes recovery-token columns only, allowed) | HTTP 200, 67 ms |  | yes | no error lines | PASS |
| 34 | 5 GoTrue while frozen | recovery link from Mailpit -> /auth/v1/verify | 303 with a recovery session (allowed columns only) | mail subject='Reset your password'; HTTP 303, 31 ms; session=yes | {"type": "recovery"} | yes | no error lines | PASS |
| 35 | 5 GoTrue while frozen | PUT /user password with the recovery session | rejected; frozen fields unchanged; freeze error in log | HTTP 500, 124 ms canary md5/email unchanged: True | code=P0001 message=hyperchat_d1_freeze: password changes are paused for maintenance | yes | error 500 PUT /user request completed ERROR: hyperchat_d1_freeze: password changes are paused for maintenance (SQLSTATE P0001) | PASS |
| 36 | 5 GoTrue while frozen | PUT /user email (request change) | 200; stored in email_change (allowed) | HTTP 200, 106 ms; email_change pending=True |  | yes | no error lines | PASS |
| 37 | 5 GoTrue while frozen | email change confirm: current-address link | first link accepted; the final confirm rejected (email write) | HTTP 303, 17 ms; email now=unchanged | {"error": "server_error", "error_code": "unexpected_failure", "error_description": "Error confirm email"} | yes | info  GET /verify 500: Error confirm email ERROR: hyperchat_d1_freeze: email changes are paused for maintenance (SQLSTATE P0001) | OBSERVED |
| 38 | 5 GoTrue while frozen | email change confirm: new-address link | first link accepted; the final confirm rejected (email write) | HTTP 303, 23 ms; email now=unchanged | {"error": "server_error", "error_code": "unexpected_failure", "error_description": "Error confirm email"} | yes | info  GET /verify 500: Error confirm email ERROR: hyperchat_d1_freeze: email changes are paused for maintenance (SQLSTATE P0001) | OBSERVED |
| 39 | 5 GoTrue while frozen | email change outcome | canary email unchanged | email unchanged=True |  | yes | - | PASS |
| 40 | 5 GoTrue while frozen | admin PUT /admin/users/<id> email | rejected; frozen fields unchanged; freeze error in log | HTTP 500, 69 ms  | code=P0001 message=hyperchat_d1_freeze: email changes are paused for maintenance | yes | error 500 PUT /admin/users/db0baf81-247a-430a-aab2-db603686d9e0 request completed ERROR: hyperchat_d1_freeze: email changes are paused for maintenance (SQLSTATE P0001) | PASS |
| 41 | 5 GoTrue while frozen | admin PUT /admin/users/<id> ban_duration (extra) | rejected; frozen fields unchanged; freeze error in log | HTTP 500, 56 ms  | code=P0001 message=hyperchat_d1_freeze: account status changes are paused for maintenance | yes | error 500 PUT /admin/users/db0baf81-247a-430a-aab2-db603686d9e0 request completed ERROR: hyperchat_d1_freeze: account status changes are paused for maintenance (SQLSTATE P0001) | PASS |
| 42 | 5 GoTrue while frozen | admin DELETE /admin/users/<second account> | rejected; frozen fields unchanged; freeze error in log | HTTP 500, 43 ms victim row present=True; identities unchanged=True | code=P0001 message=hyperchat_d1_freeze: account deletion is paused for maintenance | yes | error 500 DELETE /admin/users/f42cc843-9d7c-4953-ae0e-c8762cefbe22 request completed ERROR: hyperchat_d1_freeze: account deletion is paused for maintenance (SQLSTATE P0001) | PASS |
| 43 | 5 GoTrue while frozen | SQL UPDATE id as postgres | P0001 hyperchat_d1_freeze | error | SQLSTATE P0001: hyperchat_d1_freeze: account status changes are paused for maintenance | yes | - | PASS |
| 44 | 5 GoTrue while frozen | SQL UPDATE email as postgres | P0001 hyperchat_d1_freeze | error | SQLSTATE P0001: hyperchat_d1_freeze: email changes are paused for maintenance | yes | - | PASS |
| 45 | 5 GoTrue while frozen | SQL UPDATE encrypted_password as postgres | P0001 hyperchat_d1_freeze | error | SQLSTATE P0001: hyperchat_d1_freeze: password changes are paused for maintenance | yes | - | PASS |
| 46 | 5 GoTrue while frozen | SQL UPDATE deleted_at as postgres | P0001 hyperchat_d1_freeze | error | SQLSTATE P0001: hyperchat_d1_freeze: account status changes are paused for maintenance | yes | - | PASS |
| 47 | 5 GoTrue while frozen | SQL UPDATE banned_until as postgres | P0001 hyperchat_d1_freeze | error | SQLSTATE P0001: hyperchat_d1_freeze: account status changes are paused for maintenance | yes | - | PASS |
| 48 | 5 GoTrue while frozen | SQL INSERT as postgres | P0001 hyperchat_d1_freeze | error | SQLSTATE P0001: hyperchat_d1_freeze: account creation is paused for maintenance | yes | - | PASS |
| 49 | 5 GoTrue while frozen | SQL DELETE as postgres | P0001 hyperchat_d1_freeze | error | SQLSTATE P0001: hyperchat_d1_freeze: account deletion is paused for maintenance | yes | - | PASS |
| 50 | 5 GoTrue while frozen | replica-mode UPDATE email as postgres with tgenabled 'O' (rolled back) | documented privileged bypass if postgres may set session_replication_role | ok | ok rowcount=1 | yes | - | OBSERVED |
| 51 | 5 GoTrue while frozen | SQL UPDATE of a non-frozen column (control, rolled back) | allowed | ok | ok rowcount=1 | yes | - | PASS |
| 52 | 6 source lock (PR #46 @ 3cf791b) | GRANT USAGE ON SCHEMA auth TO d1_importer as postgres | unverified | statement ok; ['WARNING: no privileges were granted for "auth"'] | effective (usage, select, maintain, writable) = (False, False, False, False) | n/a | - | OBSERVED |
| 53 | 6 source lock (PR #46 @ 3cf791b) | GRANT SELECT (columns) ON auth.users TO d1_importer as postgres | unverified | statement ok | effective (usage, select, maintain, writable) = (False, True, False, False) | n/a | - | OBSERVED |
| 54 | 6 source lock (PR #46 @ 3cf791b) | GRANT MAINTAIN ON auth.users TO d1_importer as postgres | unverified | statement ok; ['WARNING: no privileges were granted for "users"'] | effective (usage, select, maintain, writable) = (False, True, False, False) | n/a | - | OBSERVED |
| 55 | 6 source lock (PR #46 @ 3cf791b) | GRANT USAGE ON SCHEMA auth TO d1_noperm as postgres | unverified | statement ok; ['WARNING: no privileges were granted for "auth"'] | effective (usage, select, maintain, writable) = (False, False, False, False) | n/a | - | OBSERVED |
| 56 | 6 source lock (PR #46 @ 3cf791b) | GRANT SELECT (columns) ON auth.users TO d1_noperm as postgres | unverified | statement ok | effective (usage, select, maintain, writable) = (False, True, False, False) | n/a | - | OBSERVED |
| 57 | 6 source lock (PR #46 @ 3cf791b) | GRANT USAGE ON SCHEMA auth TO d1_importer as supabase_admin (ELEVATED: owner of the object, local only, documented) | granted | ok | ok rowcount=-1; effective = (True, True, False, False) | n/a | - | OBSERVED |
| 58 | 6 source lock (PR #46 @ 3cf791b) | GRANT MAINTAIN ON auth.users TO d1_importer as supabase_auth_admin (ELEVATED: owner of the object, local only, documented) | granted | ok | ok rowcount=-1; effective = (True, True, True, False) | n/a | - | OBSERVED |
| 59 | 6 source lock (PR #46 @ 3cf791b) | GRANT USAGE ON SCHEMA auth TO d1_noperm as supabase_admin (ELEVATED: owner of the object, local only, documented) | granted | ok | ok rowcount=-1; effective = (True, True, False, False) | n/a | - | OBSERVED |
| 60 | 6 source lock (PR #46 @ 3cf791b) | step 9: BEGIN READ ONLY; LOCK auth.users IN SHARE MODE as d1_importer (PG 17.11) | ok for MAINTAIN/table-write roles; 42501 otherwise | ok |  | n/a | - | PASS |
| 61 | 6 source lock (PR #46 @ 3cf791b) | step 9: BEGIN READ ONLY; LOCK auth.users IN SHARE MODE as postgres (PG 17.11) | ok for MAINTAIN/table-write roles; 42501 otherwise | ok |  | n/a | - | PASS |
| 62 | 6 source lock (PR #46 @ 3cf791b) | step 9: BEGIN READ ONLY; LOCK auth.users IN SHARE MODE as d1_noperm (PG 17.11) | ok for MAINTAIN/table-write roles; 42501 otherwise | SQLSTATE 42501: permission denied for table users |  | n/a | - | PASS |
| 63 | 6 source lock (PR #46 @ 3cf791b) | read_plan as d1_importer (MAINTAIN + column SELECT, not writable) | plan; freeze active; guard ok | counts={'source_accounts': 0, 'target_accounts': 3, 'target_with_legacy_id': 3, 'target_without_legacy_id': 0, 'eligible': 0, 'eligible_email_confirmed': 0, 'target_legacy_id_not_in_source': 3, 'target_email_used_by_other_source_id': 0, 'id_match_email_mismatch': 0, 'source_deleted': 0, 'source_banned': 0, 'target_already_has_password': 0, 'source_without_password': 0, 'unsupported_hash_format': 0, 'source_ids_not_in | freeze_problem=None; updates=0 | yes | connect_source() TLS check not exercised: the local stack has no TLS | FAIL |
| 64 | 6 source lock (PR #46 @ 3cf791b) | apply_under_source_lock run 1 (d1_importer) + GoTrue sign-in loop | updated all; lock held through commit; released; sign-ins 200 | updated=0/0 lock_held=True target==source=True lock_after=0 | lock seen held ~7.2 ms (1 ms poll); apply wall 10 ms; sign-ins=6 statuses=[200] max=92 ms median=87 ms | yes | - | PASS |
| 65 | 6 source lock (PR #46 @ 3cf791b) | apply_under_source_lock run 2 (d1_importer) + GoTrue sign-in loop | updated all; lock held through commit; released; sign-ins 200 | updated=0/0 lock_held=True target==source=True lock_after=0 | lock seen held ~5.5 ms (1 ms poll); apply wall 9 ms; sign-ins=6 statuses=[200] max=92 ms median=87 ms | yes | - | PASS |
| 66 | 6 source lock (PR #46 @ 3cf791b) | apply_under_source_lock run 3 (d1_importer) + GoTrue sign-in loop | updated all; lock held through commit; released; sign-ins 200 | updated=0/0 lock_held=True target==source=True lock_after=0 | lock seen held ~5.5 ms (1 ms poll); apply wall 8 ms; sign-ins=6 statuses=[200] max=88 ms median=87 ms | yes | - | PASS |
| 67 | 6 source lock (PR #46 @ 3cf791b) | read_plan as postgres without --accept-writable-source-role | refused (writable role) | Refused: source: this role can write to auth.users; use a read-only role (or pass --accept-writable-source-role to rely on the read-only transaction alone) |  | n/a | - | PASS |
| 68 | 6 source lock (PR #46 @ 3cf791b) | apply as postgres with --accept-writable-source-role | ok; source transaction READ ONLY | updated=3 lock_held=True; fingerprint same as d1_importer's=False | open_readonly sets read_only=True (n/a) | yes | - | FAIL |
| 69 | 6 source lock (PR #46 @ 3cf791b) | while locked: UPDATE last_sign_in_at (sign-in style) as postgres (rolled back) | waits (lock timeout) | error | SQLSTATE 55P03: canceling statement due to lock timeout (1001 ms) | yes | - | PASS |
| 70 | 6 source lock (PR #46 @ 3cf791b) | while locked: INSERT as postgres (rolled back) | waits (lock timeout) | error | SQLSTATE 55P03: canceling statement due to lock timeout (1001 ms) | yes | - | PASS |
| 71 | 6 source lock (PR #46 @ 3cf791b) | while locked: DELETE as postgres (rolled back) | waits (lock timeout) | error | SQLSTATE 55P03: canceling statement due to lock timeout (1001 ms) | yes | - | PASS |
| 72 | 6 source lock (PR #46 @ 3cf791b) | while locked: TRUNCATE auth.users CASCADE as postgres (rolled back) | waits (lock timeout) | error | SQLSTATE 55P03: canceling statement due to lock timeout (1001 ms) | yes | - | PASS |
| 73 | 6 source lock (PR #46 @ 3cf791b) | while locked: replica-mode UPDATE email (superuser) as supabase_admin (rolled back) | waits (lock timeout) | error | SQLSTATE 55P03: canceling statement due to lock timeout (1001 ms) | yes | - | PASS |
| 74 | 6 source lock (PR #46 @ 3cf791b) | while locked: ALTER TABLE DISABLE TRIGGER (owner) as supabase_auth_admin (rolled back) | waits (lock timeout) | error | SQLSTATE 55P03: canceling statement due to lock timeout (1001 ms) | yes | - | PASS |
| 75 | 6 source lock (PR #46 @ 3cf791b) | while locked: DROP TRIGGER (owner) as supabase_auth_admin (rolled back) | waits (lock timeout) | error | SQLSTATE 55P03: canceling statement due to lock timeout (1001 ms) | yes | - | PASS |
| 76 | 6 source lock (PR #46 @ 3cf791b) | while locked: CREATE OR REPLACE TRIGGER (postgres) as postgres (rolled back) | waits (lock timeout) | error | SQLSTATE 55P03: canceling statement due to lock timeout (1001 ms) | yes | - | PASS |
| 77 | 6 source lock (PR #46 @ 3cf791b) | while locked: SELECT count(*) (read) as postgres (rolled back) | not blocked | ok | ok rowcount=1 (4 ms) | yes | - | PASS |
| 78 | 6 source lock (PR #46 @ 3cf791b) | while locked: CREATE OR REPLACE FUNCTION guard -> no-op (function owner) as postgres (rolled back) | documented: not blocked by SHARE | ok | ok rowcount=-1 (4 ms) | yes | - | OBSERVED |
| 79 | 6 source lock (PR #46 @ 3cf791b) | while locked: GoTrue GET /user (read) | 200, not delayed | HTTP 200, 72 ms |  | yes | - | PASS |
| 80 | 6 source lock (PR #46 @ 3cf791b) | lock continuity + release after probes | held before/after probes; 0 after release; freeze intact | held before=True after=True (8.4s); lock_after=0 | verify ok=True | yes | - | PASS |
| 81 | 6 source lock (PR #46 @ 3cf791b) | GoTrue sign-in baseline (no lock), 5 runs | 200 | statuses=[200] | ms=[88, 83, 86, 84, 85] | yes | - | PASS |
| 82 | 6 source lock (PR #46 @ 3cf791b) | GoTrue during a 3 s source lock | sign-in waits, completes after release; reads not delayed | {"sign-in": "HTTP 200 2809 ms", "refresh": "HTTP 200 2709 ms", "get-user": "HTTP 200 39 ms"} | sign-in finished 9 ms after release | yes | no error lines | PASS |
| 83 | 6 source lock (PR #46 @ 3cf791b) | GoTrue during a 10 s source lock | sign-in waits, completes after release; reads not delayed | {"sign-in": "HTTP 200 9814 ms", "refresh": "HTTP 200 9715 ms", "get-user": "HTTP 200 39 ms"} | sign-in finished 12 ms after release | yes | no error lines | PASS |
| 84 | 6 source lock (PR #46 @ 3cf791b) | lock timeout: uncommitted auth.users write in flight | Refused after ~5 s; target unchanged; released | Refused: could not lock the source within 5s (writes to auth.users in progress); nothing was written; try again (5.00s) | target hashes=0; lock_after=0; GoTrue sign-in queued behind the lock request: HTTP 200 4008 ms | yes | - | PASS |
| 85 | 6 source lock (PR #46 @ 3cf791b) | no lock privilege (d1_noperm: column SELECT only) | plan ok; apply Refused; target unchanged | Refused: the source role may not lock auth.users: it needs MAINTAIN on it (PostgreSQL 17+) or table update, delete or truncate (earlier versions, with --accept-writable-source-role); nothing was written | plan updates=0; target hashes=0 | yes | - | PASS |
| 86 | 6 source lock (PR #46 @ 3cf791b) | session loss before the pre-commit check (pg_terminate_backend by postgres) | target rolls back; nothing written | terminated=True; Refused: the source lock was lost before the target commit (the source session ended or failed); nothing was written | target hashes=0 | yes | - | PASS |
| 87 | 6 source lock (PR #46 @ 3cf791b) | session loss right after the pre-commit check (residual window) | commits; post-commit proof fails -> main() exits 4 (detection only) | updated=0 lock_held_after_commit=False | target hashes=0 (committed) | yes | - | PASS |
| 88 | 6 source lock (PR #46 @ 3cf791b) | importer stalls inside the locked transaction (> 20 s idle) | server ends the session at ~20 s; lock freed; apply refused; nothing written | lock seen held 20.0 s; held at 23 s=False; Refused: the source lock was lost before the target commit (the source session ended or failed); nothing was written | target hashes=0; sign-in after: HTTP 200 86 ms | yes | - | PASS |
| 89 | 6 source lock (PR #46 @ 3cf791b) | after section 6 | freeze intact; frozen fields unchanged | verify ok=True |  | yes | - | PASS |
| 90 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | GRANT USAGE ON SCHEMA auth TO d1_importer as postgres | unverified | statement ok; ['WARNING: no privileges were granted for "auth"'] | effective (usage, select, maintain, writable) = (True, True, True, False) | n/a | - | OBSERVED |
| 91 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | GRANT SELECT (columns) ON auth.users TO d1_importer as postgres | unverified | statement ok | effective (usage, select, maintain, writable) = (True, True, True, False) | n/a | - | OBSERVED |
| 92 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | GRANT MAINTAIN ON auth.users TO d1_importer as postgres | unverified | statement ok; ['WARNING: no privileges were granted for "users"'] | effective (usage, select, maintain, writable) = (True, True, True, False) | n/a | - | OBSERVED |
| 93 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | GRANT USAGE ON SCHEMA auth TO d1_noperm as postgres | unverified | statement ok; ['WARNING: no privileges were granted for "auth"'] | effective (usage, select, maintain, writable) = (True, True, False, False) | n/a | - | OBSERVED |
| 94 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | GRANT SELECT (columns) ON auth.users TO d1_noperm as postgres | unverified | statement ok | effective (usage, select, maintain, writable) = (True, True, False, False) | n/a | - | OBSERVED |
| 95 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | ALTER ROLE d1_importer BYPASSRLS as postgres (auth.users: RLS on, no policies) | unverified | ok | ok rowcount=-1; rolbypassrls=True | n/a | - | OBSERVED |
| 96 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | ALTER ROLE d1_noperm BYPASSRLS as postgres (auth.users: RLS on, no policies) | unverified | ok | ok rowcount=-1; rolbypassrls=True | n/a | - | OBSERVED |
| 97 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | step 9: BEGIN READ ONLY; LOCK auth.users IN SHARE MODE as d1_importer (PG 17.11) | ok for MAINTAIN/table-write roles; 42501 otherwise | ok |  | n/a | - | PASS |
| 98 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | step 9: BEGIN READ ONLY; LOCK auth.users IN SHARE MODE as postgres (PG 17.11) | ok for MAINTAIN/table-write roles; 42501 otherwise | ok |  | n/a | - | PASS |
| 99 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | step 9: BEGIN READ ONLY; LOCK auth.users IN SHARE MODE as d1_noperm (PG 17.11) | ok for MAINTAIN/table-write roles; 42501 otherwise | SQLSTATE 42501: permission denied for table users |  | n/a | - | PASS |
| 100 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | read_plan as d1_importer (MAINTAIN + column SELECT, not writable) | plan; freeze active; guard ok | counts={'source_accounts': 3, 'target_accounts': 3, 'target_with_legacy_id': 3, 'target_without_legacy_id': 0, 'eligible': 3, 'eligible_email_confirmed': 3, 'target_legacy_id_not_in_source': 0, 'target_email_used_by_other_source_id': 0, 'id_match_email_mismatch': 0, 'source_deleted': 0, 'source_banned': 0, 'target_already_has_password': 0, 'source_without_password': 0, 'unsupported_hash_format': 0, 'source_ids_not_in | freeze_problem=None; updates=3 | yes | connect_source() TLS check not exercised: the local stack has no TLS | PASS |
| 101 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | apply_under_source_lock run 1 (d1_importer) + GoTrue sign-in loop | updated all; lock held through commit; released; sign-ins 200 | updated=3/3 lock_held=True target==source=True lock_after=0 | lock seen held ~10.1 ms (1 ms poll); apply wall 14 ms; sign-ins=6 statuses=[200] max=88 ms median=85 ms | yes | - | PASS |
| 102 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | apply_under_source_lock run 2 (d1_importer) + GoTrue sign-in loop | updated all; lock held through commit; released; sign-ins 200 | updated=3/3 lock_held=True target==source=True lock_after=0 | lock seen held ~9.6 ms (1 ms poll); apply wall 13 ms; sign-ins=6 statuses=[200] max=89 ms median=87 ms | yes | - | PASS |
| 103 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | apply_under_source_lock run 3 (d1_importer) + GoTrue sign-in loop | updated all; lock held through commit; released; sign-ins 200 | updated=3/3 lock_held=True target==source=True lock_after=0 | lock seen held ~7.6 ms (1 ms poll); apply wall 12 ms; sign-ins=6 statuses=[200] max=85 ms median=84 ms | yes | - | PASS |
| 104 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | read_plan as postgres without --accept-writable-source-role | refused (writable role) | Refused: source: this role can write to auth.users; use a read-only role (or pass --accept-writable-source-role to rely on the read-only transaction alone) |  | n/a | - | PASS |
| 105 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | apply as postgres with --accept-writable-source-role | ok; source transaction READ ONLY | updated=3 lock_held=True; fingerprint same as d1_importer's=True | open_readonly sets read_only=True | yes | - | PASS |
| 106 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | while locked: UPDATE last_sign_in_at (sign-in style) as postgres (rolled back) | waits (lock timeout) | error | SQLSTATE 55P03: canceling statement due to lock timeout (1001 ms) | yes | - | PASS |
| 107 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | while locked: INSERT as postgres (rolled back) | waits (lock timeout) | error | SQLSTATE 55P03: canceling statement due to lock timeout (1001 ms) | yes | - | PASS |
| 108 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | while locked: DELETE as postgres (rolled back) | waits (lock timeout) | error | SQLSTATE 55P03: canceling statement due to lock timeout (1001 ms) | yes | - | PASS |
| 109 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | while locked: TRUNCATE auth.users CASCADE as postgres (rolled back) | waits (lock timeout) | error | SQLSTATE 55P03: canceling statement due to lock timeout (1001 ms) | yes | - | PASS |
| 110 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | while locked: replica-mode UPDATE email (superuser) as supabase_admin (rolled back) | waits (lock timeout) | error | SQLSTATE 55P03: canceling statement due to lock timeout (1001 ms) | yes | - | PASS |
| 111 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | while locked: ALTER TABLE DISABLE TRIGGER (owner) as supabase_auth_admin (rolled back) | waits (lock timeout) | error | SQLSTATE 55P03: canceling statement due to lock timeout (1001 ms) | yes | - | PASS |
| 112 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | while locked: DROP TRIGGER (owner) as supabase_auth_admin (rolled back) | waits (lock timeout) | error | SQLSTATE 55P03: canceling statement due to lock timeout (1001 ms) | yes | - | PASS |
| 113 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | while locked: CREATE OR REPLACE TRIGGER (postgres) as postgres (rolled back) | waits (lock timeout) | error | SQLSTATE 55P03: canceling statement due to lock timeout (1001 ms) | yes | - | PASS |
| 114 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | while locked: SELECT count(*) (read) as postgres (rolled back) | not blocked | ok | ok rowcount=1 (1 ms) | yes | - | PASS |
| 115 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | while locked: CREATE OR REPLACE FUNCTION guard -> no-op (function owner) as postgres (rolled back) | documented: not blocked by SHARE | ok | ok rowcount=-1 (3 ms) | yes | - | OBSERVED |
| 116 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | while locked: GoTrue GET /user (read) | 200, not delayed | HTTP 200, 42 ms |  | yes | - | PASS |
| 117 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | lock continuity + release after probes | held before/after probes; 0 after release; freeze intact | held before=True after=True (8.3s); lock_after=0 | verify ok=True | yes | - | PASS |
| 118 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | GoTrue sign-in baseline (no lock), 5 runs | 200 | statuses=[200] | ms=[89, 90, 90, 92, 90] | yes | - | PASS |
| 119 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | GoTrue during a 3 s source lock | sign-in waits, completes after release; reads not delayed | {"sign-in": "HTTP 200 2812 ms", "refresh": "HTTP 200 2707 ms", "get-user": "HTTP 200 41 ms"} | sign-in finished 11 ms after release | yes | no error lines | PASS |
| 120 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | GoTrue during a 10 s source lock | sign-in waits, completes after release; reads not delayed | {"sign-in": "HTTP 200 9809 ms", "refresh": "HTTP 200 9707 ms", "get-user": "HTTP 200 40 ms"} | sign-in finished 9 ms after release | yes | no error lines | PASS |
| 121 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | lock timeout: uncommitted auth.users write in flight | Refused after ~5 s; target unchanged; released | Refused: could not lock the source within 5s (writes to auth.users in progress); nothing was written; try again (5.00s) | target hashes=0; lock_after=0; GoTrue sign-in queued behind the lock request: HTTP 200 4008 ms | yes | - | PASS |
| 122 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | no lock privilege (d1_noperm: column SELECT only) | plan ok; apply Refused; target unchanged | Refused: the source role may not lock auth.users: it needs MAINTAIN on it (PostgreSQL 17+) or table update, delete or truncate (earlier versions, with --accept-writable-source-role); nothing was written | plan updates=3; target hashes=0 | yes | - | PASS |
| 123 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | session loss before the pre-commit check (pg_terminate_backend by postgres) | target rolls back; nothing written | terminated=True; Refused: the source lock was lost before the target commit (the source session ended or failed); nothing was written | target hashes=0 | yes | - | PASS |
| 124 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | session loss right after the pre-commit check (residual window) | commits; post-commit proof fails -> main() exits 4 (detection only) | updated=3 lock_held_after_commit=False | target hashes=3 (committed) | yes | - | PASS |
| 125 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | importer stalls inside the locked transaction (> 20 s idle) | server ends the session at ~20 s; lock freed; apply refused; nothing written | lock seen held 20.0 s; held at 23 s=False; Refused: the source lock was lost before the target commit (the source session ended or failed); nothing was written | target hashes=0; sign-in after: HTTP 200 83 ms | yes | - | PASS |
| 126 | 6 source lock (PR #46 @ 3cf791b) [run 2, BYPASSRLS] | after section 6 | freeze intact; frozen fields unchanged | verify ok=True |  | yes | - | PASS |
| 127 | 7 unfreeze + repeat | unfreeze.sql run 1 as postgres | commits; trigger + function removed | rc=0 in 0.05s; state={'triggers': [], 'hyperchat_ops_functions': 0, 'hyperchat_ops_schema': 1} | (no output) | n/a | - | PASS |
| 128 | 7 unfreeze + repeat | unfreeze.sql run 2 as postgres | commits; trigger + function removed | rc=0 in 0.04s; state={'triggers': [], 'hyperchat_ops_functions': 0, 'hyperchat_ops_schema': 1} | psql:docs/operations/d1-auth-freeze/unfreeze.sql:5: NOTICE:  trigger "hyperchat_d1_auth_freeze" for relation "auth.users" does not exist, skipping psql:docs/operations/d1-auth-freeze/unfreeze.sql:6: NOTICE:  function hyperchat_ops.d1_auth_freeze_guard() does not exist, skipping | n/a | - | PASS |
| 129 | 7 unfreeze + repeat | verify.sql + importer after unfreeze | 0 rows; missing | rows=0 | 'the trigger hyperchat_d1_auth_freeze is missing on auth.users (run freeze.sql)' | n/a | - | PASS |
| 130 | 7 unfreeze + repeat | PUT /user password | succeeds | HTTP 200, 110 ms; canary hash changed |  | changed as requested | no error lines | PASS |
| 131 | 7 unfreeze + repeat | recovery: request + link from Mailpit | 200, 303 with session | recover HTTP 200; verify HTTP 303; session=yes | {"type": "recovery"} | n/a | - | PASS |
| 132 | 7 unfreeze + repeat | PUT /user password with the recovery session | succeeds | HTTP 200, 143 ms; canary hash changed |  | changed as requested | no error lines | PASS |
| 133 | 7 unfreeze + repeat | admin PUT password | succeeds | HTTP 200, 104 ms; canary hash changed |  | changed as requested | no error lines | PASS |
| 134 | 7 unfreeze + repeat | sign-in with the new password | 200 | HTTP 200 |  | n/a | - | PASS |
| 135 | 7 unfreeze + repeat | email change (request + both confirm links) | email changed | request HTTP 200; links: ['HTTP 303 {"type": "email_change"}', 'HTTP 303 {"error": "access_denied", "error_code": "otp_expired", "error_description": "Email link is invalid or has expired"}'] |  | changed as requested | - | PASS |
| 136 | 7 unfreeze + repeat | admin PUT email | succeeds | HTTP 200, 43 ms; observer email changed |  | changed as requested | no error lines | PASS |
| 137 | 7 unfreeze + repeat | admin PUT ban_duration 1h | succeeds | HTTP 200, 40 ms; banned_until set |  | changed as requested | no error lines | PASS |
| 138 | 7 unfreeze + repeat | POST /signup | succeeds | HTTP 200, 96 ms; user created |  | changed as requested | no error lines | PASS |
| 139 | 7 unfreeze + repeat | admin POST /admin/users | succeeds | HTTP 200, 80 ms; user created |  | changed as requested | no error lines | PASS |
| 140 | 7 unfreeze + repeat | admin DELETE second account | succeeds | HTTP 200, 50 ms; victim row gone |  | changed as requested | no error lines | PASS |
| 141 | 7 unfreeze + repeat | SQL UPDATE id as postgres (rolled back) | succeeds | error | SQLSTATE 23503: update or delete on table "users" violates foreign key constraint "identities_user_id_fkey" on table "identities" | n/a | - | FAIL |
| 142 | 7 unfreeze + repeat | SQL UPDATE email as postgres (rolled back) | succeeds | ok | ok rowcount=1 | n/a | - | PASS |
| 143 | 7 unfreeze + repeat | SQL UPDATE encrypted_password as postgres (rolled back) | succeeds | ok | ok rowcount=1 | n/a | - | PASS |
| 144 | 7 unfreeze + repeat | SQL UPDATE deleted_at as postgres (rolled back) | succeeds | ok | ok rowcount=1 | n/a | - | PASS |
| 145 | 7 unfreeze + repeat | SQL UPDATE banned_until as postgres (rolled back) | succeeds | ok | ok rowcount=1 | n/a | - | PASS |
| 146 | 7 unfreeze + repeat | SQL INSERT as postgres (rolled back) | succeeds | ok | ok rowcount=1 | n/a | - | PASS |
| 147 | 7 unfreeze + repeat | SQL DELETE as postgres (rolled back) | succeeds | ok | ok rowcount=1 | n/a | - | PASS |
