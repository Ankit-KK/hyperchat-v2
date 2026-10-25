# Backend readiness plan (after the DEV rehearsal)

**Status:** a plan for review. Nothing here has been run beyond the read-only checks it cites; every step marked
**[APPROVAL]** needs the owner's explicit approval at the time it runs. Basis: `main` after #99, DEV on release
`c2fe883be71d` at 0028 (Phase E passed 2026-10-25), production on `940137070657` at 0009.

## 1. Code complete is not verified end to end
| Area | Code and CI | Verified end to end on DEV | Needed before production |
|---|---|---|---|
| Migrations 0020–0028, roles, grants, guards | yes | yes (Phases C–E) | the production migration (§5) |
| Authentication, sessions, CSRF, admin boundary | yes | yes (E1 suites) | — |
| Admin step-up (passkeys), S1–S3 | yes (CI incl. enforcement `active`) | only `draft` (no verifier, nothing enrolled) | verifier install, enrollment, activation (§5) |
| KYC (Didit) | yes | read-only paths only | Didit sandbox, then live [APPROVAL] |
| Payments (Razorpay) | yes | validation and refusals only; no order, no payment | §4 |
| Payment health (D7) | yes | CLI installed; the probe was BLOCKED | §4 |
| Email (Resend), five categories (#105, #106) | yes (merged at `a9b9486`) | not deployed (DEV runs `c2fe883`); no key installed; `disabled` | §4 |
| Uploads and storage (R2), upload cleanup (D9) | yes | BLOCKED (R2 writes not approved) | `HYPERCHAT_SMOKE_R2=1` run [APPROVAL] |
| TTS, transcription, bots, Google sign-in | yes | not configured | per product scope |
| Backups and restore checks | yes | yes (DEV: two sets, scratch restores) | A3 install on production (§5) |
| Rollback (Phase F) | yes (fail-closed, #98) | control flow only (harness); not run on a real cluster | §3 rehearsal [APPROVAL] |

## 2. Restrict access to `hyperchat_restore_test` (DEV cluster only)
**Findings (read-only, 2026-10-25):**
- **ACL:** `{=Tc/postgres,postgres=CTc/postgres,hyperchat=c/postgres}`. `PUBLIC` holds CONNECT and TEMP, so all eight DEV login roles can connect.
- **Two paths from the API's OS user `hyperchat`:**
  - as `hyperchat_app` through the HyperChat peer map;
  - as role `hyperchat` through the generic `local all all peer` rule.
- **What a connected non-superuser can do:**
  - SELECT no table at all;
  - read the catalog, including the source of the 217 non-system functions (5 contain secret-like words; contents not read);
  - EXECUTE 49 functions, none of them `SECURITY DEFINER`;
  - create temporary objects.
- **Who depends on it:**
  - **The ETL** (`database/etl/rehearsal.py`) needs a read-only session on it. Because no non-superuser can read its tables, a working ETL source can only have been `postgres`.
  - **Nothing else found:** no cron entry or unit names it, no `.pgpass` exists, role `hyperchat` has no password and no pg_ident line, and its only grant is this CONNECT.
  - **Usage can't be attributed:** `log_connections` is off, and the `pg_stat_database` counters move with our own read-only checks.

**Plan (each step separately approved; fail-closed; DEV cluster `15/restoretest` only):**

| Step | What | Gate / expected result |
|---|---|---|
| R0 read-only | ACL exactly as recorded; no session on the database; every non-superuser login role has 0 SELECT on its tables; `hyperchat` attributes as recorded | any difference is a STOP |
| R1 [APPROVAL] (optional) | `ALTER SYSTEM SET log_connections = on` + reload for a few days, to see any client by name | reverse with `ALTER SYSTEM RESET log_connections` + reload |
| R2 [APPROVAL] | one transaction: `REVOKE CONNECT, TEMPORARY ON DATABASE hyperchat_restore_test FROM PUBLIC`, then check the ACL is exactly `{postgres=CTc/postgres,hyperchat=c/postgres}` before `COMMIT` | otherwise ROLLBACK |
| R3 verify | `has_database_privilege(<each runtime role>, 'hyperchat_restore_test', 'CONNECT')` false; DEV API healthy; isolation audit OK; `hyperchat_dev` unaffected | |
| R4 [APPROVAL] | `ALTER ROLE hyperchat NOLOGIN` (closes the peer path); an ETL run uses `postgres` read-only, or a dedicated reader role no service user can reach | reverse with `ALTER ROLE hyperchat LOGIN` |

The undo for R2 is `GRANT CONNECT, TEMPORARY ON DATABASE hyperchat_restore_test TO PUBLIC`. Production's cluster has no
`hyperchat_restore_test`, and its `hyperchat` database already lists named roles only.

## 3. Disposable rollback rehearsal (F3–F4 on a scratch cluster)
**Feasibility:** possible once #101 is merged, and only for F3–F4.
- **With #101:** every Phase F database call reaches the cluster named by `DEV_PGHOST` / `DEV_PGPORT`.
- **Before #101:** `restore_into_new`'s `pg_restore -p 5433` could not be redirected, because `sudo` drops `PGHOST`.
- **F1, F2, F5 and F6 never run here:** they act on the DEV units and the release link.

**Design [APPROVAL]:** on the VPS, as root, because the sets hold real legacy data and must not leave the server:
1. Create a PostgreSQL 15 scratch cluster as `postgres` under `/tmp/hc-frehearsal-<random>`. It listens on its own socket in that directory (`listen_addresses=''`, a free port above 50000) and is deleted afterwards, as `restore_check --scratch` does.
2. Load `roles.sql` from the pinned staging copy.
3. Restore the Phase E set as the scratch `hyperchat_dev` (the "failed" 0028 database).
4. In a separate shell with a scratch `ST`, set `DEV_PGHOST=/tmp/hc-frehearsal-<random>` and `DEV_PGPORT=<port>`, and override `services_stopped` to return 0. Then run the verbatim `restore_into_new`, `swap_names`, and on an injected partial swap `retry_rename2` / `undo_rename1`.

**Guards, checked before any write:**
- `DEV_PGHOST` is under `/tmp/hc-frehearsal-`, not `/var/run/postgresql`, and `DEV_PGPORT` is not 5432 or 5433;
- `SHOW data_directory` on the target is inside that scratch directory;
- `SELECT system_identifier FROM pg_control_system()` differs from both live clusters' identifiers, recorded read-only beforehand;
- `ST` is not `/root/rehearsal-dev`, so the Phase E and Phase F markers there are never touched.

**What it proves beyond the local test:** that PostgreSQL 15 restores the real Phase B set, and the real renames and grants.

Locally (#101) the same functions already ran against a real PostgreSQL 16 throwaway cluster.

## 4. P0 integration testing (DEV, test mode)
| Check | Credentials | On DEV now | Approvals |
|---|---|---|---|
| D7 payment health: `python -m app.cli --env-file /etc/hyperchat/api.env payment-health` and the admin result | Razorpay test key id, secret, webhook secret | **set**, key mode `test` | one Razorpay API call (`GET /payments?count=1`) |
| Checkout / order creation | the same | set | creating test-mode orders |
| PAY-7: a real test payment, status poll and confirm, webhook | the same, plus a test payment through Razorpay's test checkout | set | an owner-run test payment; the webhook URL in the Razorpay **test** dashboard pointing at the DEV API |
| Email: reset and invite delivery | per category: `HYPERCHAT_EMAIL_<CATEGORY>_FROM`, `_VERIFIED_DOMAIN`, `_RESEND_API_KEY` (one domain-restricted key each); `HYPERCHAT_EMAIL_ACTIVE_CATEGORIES`; `HYPERCHAT_EMAIL_BACKEND=resend` | **unset** (`disabled`; the DEV release predates the categories) | deploy `a9b9486` to DEV; install the five keys (`docs/operations/email-category-keys.md`); activate `onboarding`, then `authentication`, each with a restart and one owner-inbox send |

**Email status (2026-10-25):**
- **Code:** merged (`a9b9486`).
  - five independently configured categories, all off by default;
  - fixed routing: invitations, admin-role included, → onboarding; password reset → authentication;
  - no cross-category fallback;
  - startup refuses mismatched senders or domains, missing or shared keys, and any active category in production;
  - a 2xx is always success;
  - logs carry no recipient identifier;
  - F1 fixed (#105): an unconfirmed invitation send answers a neutral 502 and its link is revoked.
- **Resend:** all five DEV domains (`*.quicktestspace.site`) are verified, each with its own key. **Nothing is installed or active on DEV or production.**
- **Not implemented** (P1/P2, `docs/email-architecture-proposal.md` §6): the reset retry, the domain-check CLI, delivery records and any new email trigger.
- **Before relying on the opt-in check:** the #103 delivery check (`tests/e2e/smoke_integrations.py`) still reads the legacy `HYPERCHAT_RESEND_API_KEY` / `HYPERCHAT_EMAIL_FROM`, not the onboarding category settings. Moving it onto the category settings is a small code follow-up; until then, prove delivery through the activation step in the key runbook.

**How:**
- **Opt-in since #103:** without the opt-in variables, the smoke suite still reports these checks BLOCKED.
- **Proposed opt-in sections** (a test-only PR, then a runbook phase), modelled on `HYPERCHAT_SMOKE_R2`:
  - `HYPERCHAT_SMOKE_RAZORPAY=1` runs the D7 probe and creates a test-mode order. It refuses `rzp_live_` keys; the app's settings already refuse live keys outside production.
  - `HYPERCHAT_SMOKE_EMAIL_TO=<owner inbox>` sends one reset and one invite to that inbox only, never to `.invalid` synthetic addresses.
- **PAY-7 stays owner-run:** a test payment needs Razorpay's checkout, which needs a client (the frontend, or a minimal test page). Its webhook is then checked server-side (signature, idempotency, the donation's status).
- **Production keys are separate:** live Razorpay keys, the production Resend domain and the production webhook URL are configured only at cutover.

## 5. Production migration and cutover prerequisites
Production runs `940137070657` at **0009**, with the old `pg_hba` / `pg_ident`, no lifecycle, auth or stepup roles, no witness, no verifier, and no Razorpay keys. In order, each step **[APPROVAL]**:
1. **Production backup install (A3):** the fixed backup script, its units and a production KYC witness. Then one production backup and a scratch `restore_check` with `--roles-sql` (docs/backup-recovery.md).
2. **A production migration runbook, 0010 → 0028,** modelled on the DEV rehearsal. Production is on PostgreSQL 17 and starts at 0009, so it needs its own revisions list, baseline, gates and rollback (a Phase F equivalent against the A3 backup).
3. **Roles and authentication rules** on `17/hyperchat`: `roles.sql`, `setup_cluster.sh`, the production `pg_ident` lines; `check_isolation.sh prod` passes.
4. **Release build and switch** of the target commit, as in Phase D.
5. **Step-up verifier** (docs/admin-stepup.md "Deployment sequence" 4–6): install the `hyperchat-prod-stepup` unit and OS user, set `HYPERCHAT_STEPUP_SOCKET`, enroll two passkeys per administrator, operator approval, `readiness` = ready, then activation through the operator tool only.
6. **D1 hosted Supabase rehearsal** (docs/operations/d1-auth-freeze/hosted-rehearsal.md, gates G1–G6): the freeze and importer against a disposable hosted project, before any data cutover.
7. **Cutover:** the legacy freeze, the import into production, live Razorpay and Resend configuration, Didit live, then the frontend switch.

## 6. Prioritized next steps
| # | Item | Kind |
|---|---|---|
| 1 | Review and merge #100 (restore_check early error) and #101 (redirectable F3–F4) | code / docs review |
| 2 | D7 payment-health probe and test-mode order on DEV | [APPROVAL] (credentials already present) |
| 3 | Email: deploy `a9b9486` to DEV; install the five category keys; activate `onboarding`, then `authentication`, with one owner-inbox send each (DNS and domains already verified; code merged in #105/#106) | [APPROVAL] at each step |
| 4 | `hyperchat_restore_test` R0 → R2 (→ R4) | [APPROVAL] |
| 5 | Opt-in smoke sections for Razorpay and email: merged (#103); move the email check onto the onboarding category settings | code |
| 6 | Disposable F3–F4 rehearsal on the VPS | [APPROVAL] |
| 7 | Production A3 backup install; production migration runbook 0010 → 0028 | [APPROVAL] / docs |
| 8 | Step-up verifier on DEV, then production; enrollment; activation | [APPROVAL] |
| 9 | Didit sandbox; R2 run (`HYPERCHAT_SMOKE_R2=1`); D1 hosted rehearsal | [APPROVAL] |
| 10 | PAY-7 test payment (needs a client) | owner-run |

The backend is **code-complete for the launch scope** above. It is **not yet production-ready**: payments, email,
step-up enforcement, KYC and the production migration are implemented but not verified end to end.
