# DEV migration rehearsal: 0019 → 0028

The controlled rehearsal of migrations 0020–0028 on the **DEV** environment, with the matching application release.
**Phases 0, A and B have run (2026-10-24; results in their sections). Phases C–F have not.** Every phase needs its own
explicit owner approval. Approving one phase never authorizes the next, and nothing here authorizes an improvised step.

## Scope and fixed rules
- **Environment:** DEV only.
  - API `api.quicktestspace.site`, database `hyperchat_dev` on cluster `15/restoretest`, port 5433.
  - OS user `hyperchat`; units `hyperchat-api`, `hyperchat-worker`, `hyperchat-cleanup.timer`.
- **Production is never changed.** That covers `17/hyperchat` (port 5432), `hyperchat-prod*`, `/etc/hyperchat-prod`, `/var/backups/hyperchat`, `/var/lib/hyperchat-kyc/prod.witness` and `/usr/local/sbin/hyperchat-pgbackup`. Only the read-only checks marked as such look at production.
- **Target:** `main` at `c2fe883be71d1a494232d4147ed4d6fbc4e70eef`, Alembic head `0028`.
- **Never in this rehearsal:**
  - activating KYC enforcement or MFA enforcement;
  - installing the step-up verifier;
  - configuring Didit, enabling bots, writing to R2 outside an approval-gated smoke section, creating Razorpay orders, or changing any credential;
  - installing the A3 backup units or script under `/usr/local/sbin` (see "The backup script and production" below);
  - `alembic downgrade`.
- **Enforcement stays `draft`:** KYC and MFA enforcement remain `draft` throughout. The rehearsal stops if either reads anything else.
- **Records:** every phase ends with a written record of what ran, the outputs named under "Record", and any deviation. The owner reads it before approving the next phase. Records go to `/root/rehearsal-dev/`, which Phase A creates; Phase 0 output is kept by the operator.

## Verified starting state (read-only inspection, 2026-10-23)
| | DEV | Production |
|---|---|---|
| Cluster | PostgreSQL 15 `restoretest`, port 5433, online | PostgreSQL 17 `hyperchat`, port 5432, online |
| Release (`readlink -f …/current`) | `/opt/hyperchat/releases/229fe42381e2` (merge of #67, head 0019) | `/opt/hyperchat-prod/releases/940137070657` |
| `alembic_version` | `0019` | `0009` |
| Services | `hyperchat-api` active, `hyperchat-worker` active, `hyperchat-stepup.socket` inactive | `hyperchat-prod-api` active, `hyperchat-prod-worker` inactive (not installed) |
| Timers | `hyperchat-cleanup.timer` only (no DEV backup timer) | `hyperchat-pgbackup.timer` (last run 02:37 UTC), `hyperchat-prod-cleanup.timer` |
| Roles (`hyperchat%`) | `hyperchat`, `hyperchat_app`, `_migrator`, `_owner`, `_readonly`, `_worker` | `hyperchat_app`, `_migrator`, `_owner`, `_readonly`, `_worker` |
| pg_hba | old 4-role rule (line 95) | old 4-role rule (line 123) |
| pg_ident | `root` → migrator, app, worker, readonly; `hyperchat` → app, worker | same for `hyperchat-prod` |
| `api.env` | none of `HYPERCHAT_DB_*_USER`, `HYPERCHAT_KYC_WITNESS_PATH`, `HYPERCHAT_STEPUP_SOCKET` | same |
| KYC witness | `/var/lib/hyperchat-kyc` absent | absent |
| Data | 133 creators, 0 without an owner; 233 `user_signups` | not queried |
| Backups | manual pre-deploy dumps only (newest `hyperchat_dev_pre_main229fe42_20261015T045216Z.dump`) | the old nightly script; verified 2026-10-24 (Phase 0.2) |
| Disk | one 48 GB root filesystem, 39 GB free | same filesystem |

`deploy/check_isolation.sh` is not in the deployed DEV release (it arrived later). Phase 0 (2026-10-24) found no drift and closed the open questions; see Phase 0 below.

## What changes between the DEV release and the target
Repository facts, `git diff 229fe42381e2 c2fe883`:

- **DEV units and nginx:** `deploy/systemd/hyperchat-{api,worker,cleanup}.*` and `deploy/nginx/` are unchanged. No unit or site reinstall.
- **`deploy/postgres/`:**
  - `roles.sql` adds `hyperchat_lifecycle`, `hyperchat_auth` and `hyperchat_stepup` (idempotent `IF NOT EXISTS`).
  - `pg_hba.conf` lists all seven login roles.
  - `pg_ident.conf` maps the API user to lifecycle and auth, and `hyperchat-stepup` to `hyperchat_stepup`.
  - `setup_cluster.sh` now syncs pg_hba through `sync_pg_hba.sh`.
- **`backend/requirements.txt`:** five new pinned dependencies (e.g. `webauthn`, `boto3`). The new release needs its own venv.
- **`deploy/api.env.example` gains these settings:**
  - `HYPERCHAT_DB_LIFECYCLE_USER`
  - `HYPERCHAT_DB_AUTH_USER`
  - `HYPERCHAT_ADMIN_SESSION_IDLE_HOURS=2`, `HYPERCHAT_ADMIN_SESSION_ABSOLUTE_HOURS=12`
  - commented `HYPERCHAT_STEPUP_SOCKET`, `HYPERCHAT_DIDIT_*` and `HYPERCHAT_KYC_WITNESS_PATH`
- **Settings compatibility:** both releases ignore unknown `HYPERCHAT_*` settings (`extra="ignore"` in `app/core/config.py`). New settings can be added to `api.env` while the old release still runs.
- **Code and schema switch together.** The 0019 release cannot run on 0024+:
  - 0024's guard triggers refuse `hyperchat_app` writes that create admin sessions;
  - the old code has no `hyperchat_auth` path;
  - so administrators could not sign in.

  The new release cannot run on 0019 either: its tables don't exist. Phase D therefore stops every DEV service, migrates, switches and starts.

### The backup script and production
- **The path is production's:** the production unit `hyperchat-pgbackup.service` installed from releases `940137070657` and `229fe42381e2` runs `ExecStart=/usr/local/sbin/hyperchat-pgbackup`. The A3 units in `c2fe883` use the same path. The production timer runs nightly (02:37 UTC on 2026-10-23).
- **Installing the A3 script there would change production's backup:** a new set layout, new retention, and a required witness that doesn't exist for production (exit 3, the unit fails).
- **So this rehearsal never writes to `/usr/local/sbin/hyperchat-pgbackup`** and never installs `deploy/backup/*.service|*.timer`. The A3 install for both environments is a separate, later approval.
- **The staged `c2fe883` script cannot back up DEV.** It fails on any schema without the enforcement tables: DEV at 0019, production at 0009 (Phase B1 STOP, 2026-10-24, exit 1).
  - **Cause:** PostgreSQL resolves every table a statement names while parsing it, even inside a `CASE` branch that never runs.
  - **Fix:** PR Ankit-KK/hyperchat-v2#88 checks existence first, requires exactly one row, and keeps the manifest format.
- **How the rehearsal runs the A3 script:** from a root-only copy of the **fixed** script, staged in B0 and pinned by its SHA-256 (`BACKUP_SCRIPT` and `BACKUP_SHA` in the session setup; `dev_backup` checks both before every run). It runs with a cleared environment and the DEV profile fixed on the same command line:
  ```
  env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HYPERCHAT_BACKUP_ENV=dev bash /root/hyperchat-backupfix/hyperchat-pgbackup.sh
  ```
  The rest of the `c2fe883` staging copy (A1 fingerprint) stays unchanged and in use.
- **Why this can't reach production:**
  - The `dev` profile fixes the database (`hyperchat_dev`), port (5433), cluster (`15/restoretest`), directory (`/var/backups/hyperchat-dev`, created root 0700 by the script) and witness (`/var/lib/hyperchat-kyc/dev.witness`).
  - The script refuses any crossed value (`die "dev backs up hyperchat_dev …"`), and `env -i` drops any `HYPERCHAT_BACKUP_*` override from the operator's shell.
  - The lock is per backup directory, so this run never waits for or blocks the production timer.
  - **Without `HYPERCHAT_BACKUP_ENV=dev` the script defaults to `prod`.** Never run it without that word on the same line.

## Migrations in this rehearsal
`transaction_per_migration=True` (`database/migrations/env.py`): each revision commits on its own, with `lock_timeout` 10 s and `statement_timeout` 5 min, as `hyperchat_migrator` with `SET ROLE hyperchat_owner`. A failed revision rolls back and leaves the previous revision in place.

| Rev | What it does to existing DEV data | Downgrade |
|---|---|---|
| 0020 | Adds creator lifecycle columns. Every existing creator becomes `kyc_track = 'legacy'` with `kyc_baseline_owner_id = user_id`; status, owner, donations and `updated_at` unchanged. `creator_donation_block()` and the donations insert trigger. | Refuses once a creator is `suspended` or a status event exists |
| 0021 | `user_signups` gains review columns; every existing row reads `review_status = 'legacy_unreviewed'` from the column default (no rewrite). `application_events`. | Refuses once any review, event or application invite exists |
| 0022 | Didit KYC tables and functions; nothing existing changes | Refuses once any KYC session or audit row exists |
| 0023 | `kyc_enforcement` (one row: `draft`, generation 0), audit chain, `kyc_change_creator_status` (executed by `hyperchat_lifecycle`). **Needs `hyperchat_lifecycle` to exist.** | Refuses once enforcement left `draft` or has history |
| 0024 | Admin auth boundary: guard triggers on users / sessions / identities / reset tokens / invites; grants to `hyperchat_auth`. **Needs `hyperchat_auth`.** | Drops triggers, revokes grants |
| 0025 | Step-up core (`hyperchat_stepup` tables and functions, `stepup_enforcement` = `draft`). **Needs `hyperchat_stepup`.** | Drops |
| 0026 | Operator-only admin Google linking | Drops |
| 0027 | Step-up guards on sensitive writes; inert while `draft` | Drops |
| 0028 | Audited return of MFA enforcement to `draft` (owner function); no grants | Drops |

**PostgreSQL 15:**
- The CI job "PostgreSQL 15 compatibility" runs the migration round trips and role privileges (`database/tests`) and the backend auth, KYC, step-up, admin and backup suites against `postgres:15`. It is green on `c2fe883` (run 39924256230).
- The full backend suite also passed locally on PostgreSQL 15.19.
- Only `database/tests/test_auth_hash_import.py` (the D1 importer, PostgreSQL 17 `MAINTAIN`) doesn't apply. It isn't part of this rehearsal.

**Recovery is a restore, never a downgrade** (Phase F). Downgrades refuse once data depends on them, and a downgrade path is not rehearsed.

---

## Session setup (paste at the start of EVERY root shell, in every phase)
Shell functions and variables don't survive a new SSH session or terminal, and later phases must not depend on
earlier ones still being in memory. Each phase therefore starts by pasting this block. It only defines names and
checks the user; it changes nothing.
```
# HyperChat DEV rehearsal: session setup (read-only definitions)
[ "$(id -u)" = 0 ] || echo "STOP: run as root"
# root-run Python in a release (alembic, restore_check, app.cli) must never write __pycache__ (deploy/README.md "Rules")
export PYTHONDONTWRITEBYTECODE=1
REL_OLD=/opt/hyperchat/releases/229fe42381e2
REL_NEW=/opt/hyperchat/releases/c2fe883be71d
STAGE=/root/hyperchat-c2fe883
ST=/root/rehearsal-dev
# the fixed backup script (PR #88), staged in B0 and pinned: dev_backup refuses any other content or owner
BACKUP_SCRIPT=/root/hyperchat-backupfix/hyperchat-pgbackup.sh
BACKUP_SHA=05509f5e915601169a37604067bd65f55f672ead9db6bb9a602489a2fd7ea854
# fp: one SHA-256 over the sorted SHA-256 of every file in a tree (the staging and release fingerprints)
fp() { (cd "$1" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-64); }
# roles.sql for restore_check's scratch cluster (B2, E2): the built release holds only backend/ and database/, so the
# tool's default <release>/deploy/postgres/roles.sql does not exist. The A1-verified staging copy's file is used,
# pinned (c2fe883 and main: the same file); the release is never changed.
ROLES_SQL="$STAGE/deploy/postgres/roles.sql"
ROLES_SQL_SHA=fbb277d57be9eea95bdfd8ddf38f44c5aa47a3db12e5a7c487bc4867b1b65c3a
roles_sql_ok() {
  [ "$(readlink -f "$STAGE")" = "$STAGE" ] && [ "$(stat -c '%U:%G %a' "$STAGE")" = "root:root 700" ] \
    || { echo "STOP: $STAGE is not the root-only staging directory"; return 1; }
  [ "$(fp "$STAGE")" = 83b9276f0484b72e1057e4698ec8d9a8c37a1fa4309d3e3116f2bad0dbac9e36 ] \
    && [ "$(find "$STAGE" -type f | wc -l)" = 381 ] || { echo "STOP: $STAGE is no longer the c2fe883 tree"; return 1; }
  [ "$ROLES_SQL" = "$STAGE/deploy/postgres/roles.sql" ] && [ -f "$ROLES_SQL" ] && [ ! -L "$ROLES_SQL" ] \
    && [ "$(readlink -f "$ROLES_SQL")" = "$ROLES_SQL" ] || { echo "STOP: $ROLES_SQL is not a regular file inside $STAGE"; return 1; }
  [ "$(sha256sum "$ROLES_SQL" | cut -d' ' -f1)" = "$ROLES_SQL_SHA" ] || { echo "STOP: $ROLES_SQL is not the pinned c2fe883 file"; return 1; }
}
FAILED_DB=hyperchat_dev_failed_rehearsal
RESTORE_DB=hyperchat_dev_rollback_restore
# Q: one read-only query against the DEV database (port 5433 only; never production)
Q()  { sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 -p 5433 -d hyperchat_dev -Atc "$1"; }
# QD: the same against a named DEV-cluster database (used only in Phase F)
QD() { sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 -p 5433 -d "$1" -Atc "$2"; }
# QP: the DEV cluster's maintenance database (database-level statements; port 5433 only)
QP() { sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 -p 5433 -d postgres -Atc "$1"; }
# the DEV cluster's configuration directory and the DEV environment file (Phase C)
PGCONF=/etc/postgresql/15/restoretest
API_ENV=/etc/hyperchat/api.env
# conn_as OSUSER ROLE: a NEW peer connection to hyperchat_dev (read-only session); prints the role it got, or nothing
conn_as() { (cd /tmp && sudo -u "$1" env PGOPTIONS='-c default_transaction_read_only=on' psql -X -q -At -p 5433 -d hyperchat_dev -U "$2" -c 'SELECT current_user' 2>/dev/null); }
# baseline: the data fingerprint compared before and after (read-only). Optional $1 = database name.
baseline() {
  local d="${1:-hyperchat_dev}"
  QD "$d" "SELECT 'streamers', count(*), count(*) FILTER (WHERE user_id IS NULL) FROM streamers"
  QD "$d" "SELECT 'streamers_md5', md5(string_agg(id::text || ':' || coalesce(user_id::text,'-') || ':' || streamer_slug || ':' || coalesce(status,'-'), ',' ORDER BY id)) FROM streamers"
  QD "$d" "SELECT 'signups', count(*), md5(string_agg(id::text || ':' || lower(email), ',' ORDER BY id)) FROM user_signups"
  QD "$d" "SELECT 'donations', count(*), count(*) FILTER (WHERE payment_status = 'success'), coalesce(sum(amount_inr) FILTER (WHERE payment_status = 'success'), 0) FROM donations"
  QD "$d" "SELECT 'users', role, count(*) FROM users GROUP BY role ORDER BY role"
  QD "$d" "SELECT 'obs_tokens_active', count(*) FILTER (WHERE revoked_at IS NULL) FROM obs_tokens"
  QD "$d" "SELECT 'soundboard_acceptances', count(*) FROM soundboard_tnc_acceptances"
}
# ── gate helpers: each returns 0 only when its condition holds, and prints STOP otherwise ──
W=/var/lib/hyperchat-kyc/dev.witness
head_is() { local now; now="$(Q 'SELECT version_num FROM alembic_version')" || { echo "STOP: cannot read alembic_version"; return 1; }
            [ "$now" = "$1" ] || { echo "STOP: database at '$now', expected $1"; return 1; }; }
drafts_ok() { [ "$(Q "SELECT state || '|' || generation FROM kyc_enforcement")" = "draft|0" ] || { echo "STOP: KYC enforcement is not draft|0"; return 1; }
              [ "$(Q 'SELECT state FROM stepup_enforcement')" = draft ] || { echo "STOP: MFA enforcement is not draft"; return 1; }; }
witness_ok() { [ -f "$W" ] && [ "$(stat -c %s "$W")" = 0 ] && lsattr "$W" 2>/dev/null | awk '{print $1}' | grep -q a \
               || { echo "STOP: $W missing, non-empty or not append-only"; return 1; }; }
services_stopped() { local s st; for s in hyperchat-api hyperchat-worker hyperchat-cleanup.timer hyperchat-cleanup.service; do
                       st="$(systemctl is-active "$s")"; case "$st" in inactive|failed) ;; *) echo "STOP: $s is $st"; return 1 ;; esac
                     done; }
# enter_stopped_state: stops the timer, a cleanup run in progress, the worker and the API; the unit STATES decide (a
# failed `systemctl stop` is caught by services_stopped); non-zero with STOP unless every unit is inactive or failed
enter_stopped_state() { systemctl stop hyperchat-cleanup.timer hyperchat-cleanup.service; systemctl stop hyperchat-worker; systemctl stop hyperchat-api
                        services_stopped || { echo "STOP: DEV is NOT fully stopped; escalate (see 'If a phase fails')"; return 1; }
                        echo "DEV STOPPED: wait for the owner (see 'If a phase fails')"; }
# Phase E: the root-only copy of the smoke PR's merge commit (#82, test-only), pinned by its tree fingerprint
# first copy (E0, 2026-10-24; kept unchanged as evidence): #82's merge commit
SMOKE1=/root/hyperchat-smoke
SMOKE1_SHA=f8bd98b3bf0070a3fcfe24cdefb5a6b26c69eb13
SMOKE1_FP=23dc26875b484ce5c5f7c5bcf25600a30a0a4b8e40433b2ecb8f8b2910af6933   # LF content, as the cmd.exe git archive pipe transfers it
# the corrected suite: #94's merge commit (creator-linked fixtures through the schema owner, migration 0027), used by the resumed E1-E3
SMOKE=/root/hyperchat-smoke-2
SMOKE_SHA=ae60aa5b144517384ea6b9fbb50f6654eadd07b4
SMOKE_FP=02ddd7f723329b18a841fab7c56d4ae0dc751f98369fac7d93ba73cecef5930c    # 296 files, LF content
# Phase D: the DEV release link, and how long D4 watches the started services for restarts (seconds)
APP_CURRENT=/opt/hyperchat/current
D4_SETTLE=30
# api_healthy RELEASE: polls the local /health for up to 2 minutes; succeeds only with status ok, database ok, that release
api_healthy() { local i out
  for i in $(seq 1 24); do
    out="$(curl -fsS --max-time 5 http://127.0.0.1:8000/health 2>/dev/null)" \
      && printf '%s' "$out" | python3 -c 'import json, sys; d = json.load(sys.stdin); sys.exit(0 if d.get("status") == "ok" and d.get("release") == sys.argv[1] and (d.get("database") or {}).get("status") == "ok" else 1)' "$1" \
      && { echo "API healthy on release $1"; return 0; }
    sleep 5
  done
  echo "STOP: API not healthy on release $1 within 2 minutes"; return 1; }
# dev_backup FILE: one DEV backup set with the pinned script (DEV profile on the same line); succeeds only with exit 0 AND a
# "backup ok" line naming a complete set, whose name then goes to $ST/FILE
dev_backup() { local out rc name
  [ "$(stat -c '%U:%G %a' "${BACKUP_SCRIPT%/*}" 2>/dev/null)" = "root:root 700" ] && [ ! -L "$BACKUP_SCRIPT" ] \
    && [ "$(stat -c '%U:%G %a' "$BACKUP_SCRIPT" 2>/dev/null)" = "root:root 600" ] \
    && [ "$(sha256sum "$BACKUP_SCRIPT" | cut -d' ' -f1)" = "$BACKUP_SHA" ] || { echo "STOP: $BACKUP_SCRIPT is not the pinned fixed script (B0)"; return 1; }
  out="$(env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HYPERCHAT_BACKUP_ENV=dev bash "$BACKUP_SCRIPT" 2>&1)"; rc=$?
  printf '%s\n' "$out"
  [ "$rc" = 0 ] || { echo "STOP: backup exit $rc"; return 1; }
  name="$(printf '%s\n' "$out" | sed -n 's/^backup ok env=dev db=hyperchat_dev set=\([A-Za-z0-9_]*\) .*/\1/p')"
  [ -n "$name" ] && [ -f "/var/backups/hyperchat-dev/$name.manifest.json" ] || { echo "STOP: no complete set named in the output"; return 1; }
  printf '%s\n' "$name" > "$ST/$1" && echo "SET $name"; }
```
- **No bytecode:** the session exports `PYTHONDONTWRITEBYTECODE=1`. Every root-run Python in Phases A–F (the migrations in D, `restore_check` in B, E and F) then writes no root-owned `__pycache__` into a release, which the isolation audits in A, C and E would refuse.
- **No `set -e`:** it would close an interactive shell on the first error.
- **Fail-closed functions:** every consequential step (anything that creates, renames, switches or starts) is a function whose steps are chained with `&&`. A failing step makes the function print `STOP: …` and return non-zero before anything later in it runs. Phase D's release switch runs as `d4_switch`: on a failure it returns DEV to the stopped state AND returns non-zero. (Phase F's `… || enter_stopped_state` lines still return 0 when the stop succeeds; read their output, not only the exit status.)
- **On `STOP`,** don't run the next command. Go to "If a phase fails" below.
- **Read-only blocks** (inspections, records) may print `STOP` and continue; they change nothing.
- **Where commands run:** commands marked **(workstation)** run on the operator's machine, in a checkout of this repository; `root@VPS` stands for the server's SSH target, as in `deploy/README.md`. Everything else runs on the VPS as root, after the session setup.

---

## Phase 0: Inspection gaps (read-only; approval: read-only VPS access)
**Done on 2026-10-24** (owner-approved, one root SSH session, nothing changed on the server). Results are recorded below.
**Phase C may not start until 0.1 and 0.4 are answered; both are answered.** 0.3 waits for Phase A1.

### The commands that ran
They are read-only by construction:
- **SQL sessions:** forced read-only by the server (`PGOPTIONS='-c default_transaction_read_only=on'`). On throwaway PostgreSQL 15 and 17, such a session refused both an `INSERT` and an `ALTER ROLE` while the queries were validated. `-X` skips `~/.psqlrc`, and `-c` keeps no history.
- **No sensitive values printed:** setting values never appear (setting names only), and a password is reported only as present or absent.
- **What else ran:** only listings, `systemctl show` / `cat`, the journal, `sha256sum -c` and `pg_restore -l`.
- **Ordinary read side effects only:** reading files may update their access time (the root filesystem is mounted `relatime`), and `sudo` and PostgreSQL log their sessions.

Each line says which cluster or files it reads: DEV (`15/restoretest`, port 5433) or PRODUCTION (`17/hyperchat`, port 5432, `/var/backups/hyperchat`).
```
# HyperChat DEV rehearsal - Phase 0 (read-only). One root SSH session; nothing is written, started, stopped or changed.
# Every SQL session is forced read-only by the server (default_transaction_read_only); -X skips ~/.psqlrc, -c keeps no history.
DEVSQL()  { sudo -u postgres env PGOPTIONS='-c default_transaction_read_only=on' psql -X -q -At -v ON_ERROR_STOP=1 -p 5433 "$@"; }  # DEV cluster 15/restoretest
PRODSQL() { sudo -u postgres env PGOPTIONS='-c default_transaction_read_only=on' psql -X -q -At -v ON_ERROR_STOP=1 -p 5432 "$@"; }  # PRODUCTION cluster 17/hyperchat
s() { echo; echo "## $*"; }

s "0.0 drift since 2026-10-23 (DEV + PRODUCTION)"
date -u +%FT%TZ
readlink -f /opt/hyperchat/current /opt/hyperchat-prod/current
systemctl is-active hyperchat-api hyperchat-worker hyperchat-cleanup.timer hyperchat-prod-api hyperchat-prod-worker hyperchat-pgbackup.timer
DEVSQL  -d hyperchat_dev -c "SELECT 'dev alembic', version_num FROM alembic_version"
PRODSQL -d hyperchat     -c "SELECT 'prod alembic', version_num FROM alembic_version"
findmnt -no OPTIONS /

s "0.1 DEV role hyperchat: attributes, password present (boolean only), memberships"
DEVSQL -d postgres -c "SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, rolconnlimit, rolvaliduntil FROM pg_roles WHERE rolname = 'hyperchat'"
DEVSQL -d postgres -c "SELECT 'has_password', rolpassword IS NOT NULL FROM pg_authid WHERE rolname = 'hyperchat'"
DEVSQL -d postgres -c "SELECT g.rolname AS granted_role, m.rolname AS member, a.admin_option FROM pg_auth_members a JOIN pg_roles g ON g.oid = a.roleid JOIN pg_roles m ON m.oid = a.member WHERE 'hyperchat' IN (g.rolname, m.rolname)"
s "0.1 every dependency on the role, all databases (shared catalog: o=owner, a=ACL, r=policy, i=initial ACL)"
DEVSQL -d postgres -c "SELECT coalesce(d.datname, '(cluster)'), s.classid::regclass, s.deptype, count(*) FROM pg_shdepend s LEFT JOIN pg_database d ON d.oid = s.dbid WHERE s.refobjid = (SELECT oid FROM pg_roles WHERE rolname = 'hyperchat') GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
DEVSQL -d postgres -c "SELECT d.datname, pg_get_userbyid(d.datdba) AS owner, EXISTS (SELECT 1 FROM aclexplode(d.datacl) x WHERE x.grantee = r.oid) AS role_in_acl FROM pg_database d, pg_roles r WHERE r.rolname = 'hyperchat' ORDER BY 1"
for db in $(DEVSQL -d postgres -c "SELECT datname FROM pg_database WHERE datallowconn ORDER BY 1"); do
  DEVSQL -d "$db" -c "SELECT '$db', 'relations', count(*) FROM pg_class WHERE relowner = (SELECT oid FROM pg_roles WHERE rolname = 'hyperchat')
                      UNION ALL SELECT '$db', 'functions', count(*) FROM pg_proc WHERE proowner = (SELECT oid FROM pg_roles WHERE rolname = 'hyperchat')
                      UNION ALL SELECT '$db', 'schemas', count(*) FROM pg_namespace WHERE nspowner = (SELECT oid FROM pg_roles WHERE rolname = 'hyperchat')
                      UNION ALL SELECT '$db', 'default ACLs', count(*) FROM pg_default_acl WHERE defaclrole = (SELECT oid FROM pg_roles WHERE rolname = 'hyperchat')"
done
s "0.1 sessions now, role-level settings (names only), and who could connect as it"
DEVSQL -d postgres -c "SELECT datname, application_name, client_addr IS NULL AS unix_socket, state, count(*) FROM pg_stat_activity WHERE usename = 'hyperchat' GROUP BY 1, 2, 3, 4"
DEVSQL -d postgres -c "SELECT coalesce(d.datname, '*'), array(SELECT split_part(x, '=', 1) FROM unnest(s.setconfig) x) FROM pg_db_role_setting s LEFT JOIN pg_database d ON d.oid = s.setdatabase WHERE s.setrole = (SELECT oid FROM pg_roles WHERE rolname = 'hyperchat')"
grep -nE '^[^#]*[[:space:]]hyperchat([[:space:]]|,|$)' /etc/postgresql/15/restoretest/pg_hba.conf /etc/postgresql/15/restoretest/pg_ident.conf
grep -nE '^[^#]*local[[:space:]]+all[[:space:]]+all' /etc/postgresql/15/restoretest/pg_hba.conf
grep -hoE '^HYPERCHAT_DB_[A-Z_]*USER=[a-z_]*' /etc/hyperchat/api.env /etc/hyperchat-prod/api.env
DEVSQL -d postgres -c "SELECT name, setting FROM pg_settings WHERE name IN ('log_connections', 'log_line_prefix')"
grep -cE 'user=hyperchat([ ,]|$)' /var/log/postgresql/postgresql-15-restoretest.log

s "0.2 PRODUCTION backup: unit result (not the timer), unit definition, installed script fingerprint"
systemctl show hyperchat-pgbackup.service -p Result -p ExecMainStatus -p ExecMainStartTimestamp -p ExecMainExitTimestamp
systemctl show hyperchat-pgbackup.timer -p LastTriggerUSec -p NextElapseUSecRealtime
systemctl cat hyperchat-pgbackup.service | grep -E '^(ExecStart|Environment|User)='
ls -l /usr/local/sbin/hyperchat-pgbackup; sha256sum /usr/local/sbin/hyperchat-pgbackup
journalctl -u hyperchat-pgbackup.service --since "-3 days" --no-pager -o short-iso | tail -n 20
s "0.2 PRODUCTION archives: newest set, size, SHA-256, archive readability"
ls -lt --time-style=+%FT%T /var/backups/hyperchat | head -n 12
ls /var/backups/hyperchat | grep -c '\.dump$'; ls /var/backups/hyperchat | grep -c '\.partial$'
N=$(ls -t /var/backups/hyperchat/hyperchat_*.dump | head -n 1); echo "$N"; stat -c '%s bytes, modified %y' "$N"
( cd /var/backups/hyperchat && sha256sum -c "$(basename "$N").sha256" )
/usr/lib/postgresql/17/bin/pg_restore --version
/usr/lib/postgresql/17/bin/pg_restore -l "$N" | grep -vc '^;'

s "0.3 isolation audit: does the read-only script exist on the server? (no copy is made)"
ls -l /opt/hyperchat/current/deploy/check_isolation.sh /opt/hyperchat/releases/*/deploy/check_isolation.sh /root/hyperchat-*/deploy/check_isolation.sh 2>&1
command -v namei

s "0.4 DEV database-level configuration (setting names only)"
DEVSQL -d postgres -c "SELECT datname, pg_get_userbyid(datdba), pg_encoding_to_char(encoding), datcollate, datctype, datconnlimit, datallowconn, datistemplate, datacl FROM pg_database WHERE datname = 'hyperchat_dev'"
DEVSQL -d postgres -c "SELECT coalesce(r.rolname, '*'), array(SELECT split_part(x, '=', 1) FROM unnest(s.setconfig) x) FROM pg_db_role_setting s LEFT JOIN pg_roles r ON r.oid = s.setrole WHERE s.setdatabase = (SELECT oid FROM pg_database WHERE datname = 'hyperchat_dev')"
DEVSQL -d postgres -c "SELECT pg_size_pretty(pg_database_size('hyperchat_dev'))"
```

### 0.0 Drift since 2026-10-23: none
- DEV: `current` → `229fe42381e2`, alembic `0019`; `hyperchat-api`, `hyperchat-worker`, `hyperchat-cleanup.timer` active.
- Production: `current` → `940137070657`, alembic `0009`; `hyperchat-prod-api` active, `hyperchat-prod-worker` inactive (not installed), `hyperchat-pgbackup.timer` active.
- Root filesystem `rw,relatime`.

The 2026-10-23 results therefore stand: releases, alembic versions, roles, pg_hba / pg_ident lines, `api.env` setting names, no KYC witness, DEV counts and disk. Phase D's pre-flight checks them again at the time.

### 0.1 The DEV role `hyperchat`: retain, document, decide separately
| Question | Answer (2026-10-24) |
|---|---|
| Attributes | `LOGIN`, `NOSUPERUSER`, `NOINHERIT`, no create-role / create-db / replication / bypass-RLS, connection limit 10, no expiry |
| Password | none (`has_password = false`): it can only log in through peer authentication |
| Memberships | none, in either direction |
| Role-level settings | in every database: `default_transaction_read_only` and `statement_timeout` (names only) |
| Dependencies (all databases, `pg_shdepend`) | exactly one: an entry in a database ACL, which is `hyperchat_restore_test` (owned by `postgres`) |
| Owned objects | none in any database (relations, functions, schemas, default ACLs all 0) |
| Access to `hyperchat_dev` | none: `hyperchat_dev`'s ACL lists only `hyperchat_owner`, `hyperchat_migrator`, `hyperchat_app`, `hyperchat_worker`, `hyperchat_readonly` (no `PUBLIC`, no `hyperchat`) |
| Sessions | none at the time |
| Configured use | no pg_ident line maps to it; neither `api.env` names it (both use `HYPERCHAT_DB_USER=hyperchat_app`); no repository file creates or names it |

**What it most likely is (inference, not proven):** a read-only login for the legacy restore database. `database/etl/rehearsal.py` requires its source to be a read-only session on `hyperchat_restore_test`, which matches the role's settings and its only grant. Whether anything still uses it isn't established. The DEV PostgreSQL log prefix is `%m [%p] %q%u@%d`, so the Phase 0 count of `user=hyperchat` lines (0) proves nothing. A later read-only check would count `hyperchat@hyperchat_restore_test` lines instead.

**Isolation finding (pre-existing; not changed by the rehearsal; re-checked read-only on 2026-10-24 before Phase C):**
- **Two paths from the API's OS user `hyperchat` to `hyperchat_restore_test`** (the restore of the legacy production dump):
  - **As role `hyperchat`:** the generic rule `local all all peer` (line 96) lets an OS user log in as the role of the same name. Confirmed: a session opens as `hyperchat`, read-only.
  - **As `hyperchat_app` / `hyperchat_worker`:** the HyperChat rule (line 95) covers database `all`, and `hyperchat_restore_test` grants `CONNECT` and `TEMP` to **`PUBLIC`** (`=Tc/postgres`, plus `hyperchat=c/postgres`). After Phase C, `hyperchat_lifecycle` and `hyperchat_auth` reach it the same way.
- **What such a session can read:**
  - **Table data: none today.** All 176 tables are owned by `postgres`, and none of `hyperchat`, `hyperchat_app`, `hyperchat_worker`, `hyperchat_readonly` or `hyperchat_migrator` has `SELECT` on any of them.
  - **Still possible:** the catalog (table, column and function names and definitions), temporary objects (`TEMP` for `PUBLIC`), and any function executable by `PUBLIC`.
  - **Legacy grants:** the schema `public` grants `USAGE` to `PUBLIC` and to the legacy roles `anon`, `authenticated` and `service_role`.
- **Why it matters:** a compromised DEV API process can connect to the legacy production restore and read its structure. A single later `GRANT SELECT` there, or a `PUBLIC`-executable function, would expose its data.

**Effect on the rehearsal:** none.
- The role has no access to `hyperchat_dev`.
- Phase C's `REVOKE ALL ON DATABASE hyperchat_dev FROM PUBLIC` is already in effect (no `PUBLIC` entry), so it cuts nothing off.
- No phase touches `hyperchat_restore_test`; C2 records its ACL before and checks it unchanged after.

**Decision (owner, separate approval, not part of this rehearsal):**
- **Recommended:** keep the role while the ETL needs a read-only source.
- **Close the paths from the API's OS user:**
  - `REVOKE CONNECT, TEMP ON DATABASE hyperchat_restore_test FROM PUBLIC` (it then admits only the roles granted by name);
  - for the role `hyperchat`, either `ALTER ROLE hyperchat NOLOGIN` between ETL runs, or give the ETL a reader role that no service user can reach through peer authentication, then drop `hyperchat`.
- **Before either:** the log check above, to confirm nothing uses it.

### 0.2 Production backup: verified healthy (and its limits)
| Check | Result |
|---|---|
| Last run of `hyperchat-pgbackup.service` | `Result=success`, `ExecMainStatus=0`, started and finished 2026-10-24 02:38:01 UTC (the timer's last trigger; next 2026-10-25 02:30 UTC) |
| Unit | `ExecStart=/usr/local/sbin/hyperchat-pgbackup`, no `Environment=` override (the path rule in "The backup script and production" holds) |
| Installed script | 1,103 bytes, 2026-10-04; SHA-256 `c839856d30dbe62922907f633046d17e14a70f242061b2488d9ed20c3028fed3`, identical to `deploy/backup/hyperchat-pgbackup.sh` at `940137070657` and `229fe42381e2` (the pre-A3 script) |
| Journal | `backup ok db=hyperchat file=hyperchat_20261024T023801Z.dump bytes=269344 toc_entries=303 sha256=07cd0690…f31f7`; the day before likewise (`toc_entries=303`) |
| Newest archive | `hyperchat_20261024T023801Z.dump`, 269,344 bytes, 2026-10-24 02:38:01 UTC |
| SHA-256 | `sha256sum -c` → `OK` |
| Archive readable | `pg_restore -l` (17.11) reads it: 303 TOC entries, the same count the script recorded |
| Leftovers | 24 `.dump` files in the directory, no `.partial` file |

**Limits:**
- **Not a restore drill:** a readable archive isn't a restored database. The first drill is still to be run (`docs/backup-recovery.md`).
- **Retention:** the installed (pre-A3) script deletes every dump older than 14 days unconditionally, with no minimum kept. If backups ever stopped, all copies would be gone within 14 days. A3's verified retention (keep the newest 3) replaces it only with the separately approved production A3 install.
- **One copy:** everything is on the same VPS (no off-site copy).
- **Idle database:** production takes no traffic, so the daily dumps are the same size.

### 0.3 Isolation audit: deferred to Phase A1
- **Phase 0 result:** `check_isolation.sh` doesn't exist on the server (not in `/opt/hyperchat/current/deploy`, any release, or `/root/hyperchat-*`), because releases carry only `backend/` and `database/`. `namei` is installed (`/usr/bin/namei`).
- **Why it waits:** Phase 0 copies nothing. The audit runs inside A1 (`audit_check dev A1` / `prod A1`), from the staging copy, and again in A2.
- **Enforced:**
  - **Output:** exactly the expected lines (`audit_expected`): every check OK except the two legacy pg_hba / pg_ident FAIL lines that Phase C fixes.
  - **Run:** exit status 1, nothing on stderr.
  - **Records:** full output kept in `$ST`.
  - **Anything else is a STOP:** another FAIL, a missing or extra check, unexplained output, or another exit status. The script is read-only, and nothing is fixed automatically.

### 0.4 `hyperchat_dev` database-level configuration
| Property | Value (2026-10-24) |
|---|---|
| Owner | `hyperchat_owner` |
| Encoding / collation / ctype | `UTF8` / `C.UTF-8` / `C.UTF-8` |
| Connection limit, connections allowed, template | -1, yes, no |
| ACL | `hyperchat_owner=CTc`, `CONNECT` for `hyperchat_migrator`, `hyperchat_app`, `hyperchat_worker`, `hyperchat_readonly`; no `PUBLIC` |
| Per-database settings (`pg_db_role_setting`) | none |
| Size | 32 MB |

**Consequence for Phase F:** the restore database is created with exactly this encoding and locale (`TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8'`), and `verify_restored` checks it. Otherwise a restore could silently change text ordering. There are no per-database settings to re-apply.

---

## Phase A: Preparation (approval: "Phase A")
Phase A prepares files only. It changes no database, no running service, no unit, and nothing under `/usr/local/sbin`.

**How to run it:**
- Every step is one root shell: the session setup, then the Phase A helpers below, then the step's block exactly as written here.
- **Step gates:** each step writes a marker in `$ST` only when everything in it succeeded, and the next step (including the remote side of each workstation transfer) refuses to start without it: A0 → `A0-passed` → A1 → `A1-passed` → A2 → `A2-passed` → A3 → `A3-passed` → A9.
- **Failures:** any `STOP` ends Phase A (see "If a phase fails"). A marker is never written by hand.

**Workstation pipes:** the transfer commands pipe a `git archive` into `ssh`. On Windows, run them through `cmd.exe /c "…"`, with `C:\Windows\System32\OpenSSH\ssh.exe` and the ssh-agent. A PowerShell pipe re-encodes binary data.

### Phase A helpers (read-only definitions; paste in every Phase A shell, after the session setup)
```
# Phase A helpers (read-only definitions)
PRODSQL() { sudo -u postgres env PGOPTIONS='-c default_transaction_read_only=on' psql -X -q -At -v ON_ERROR_STOP=1 -p 5432 "$@"; }
# fp (a tree's content fingerprint, compared with values computed from c2fe883): defined in the session setup
# production configuration and state Phase A must not change: exactly 9 lines, compared before (A0) and after (A9)
prod_snapshot() {
  sha256sum /usr/local/sbin/hyperchat-pgbackup
  ls -l --time-style=+%FT%T /usr/local/sbin | sha256sum
  systemctl cat hyperchat-pgbackup.service hyperchat-pgbackup.timer 2>&1 | sha256sum
  systemctl list-unit-files 'hyperchat*' --no-legend | sort | sha256sum
  systemctl is-active hyperchat-prod-api hyperchat-pgbackup.timer hyperchat-prod-cleanup.timer | tr '\n' ' '; echo
  readlink -f /opt/hyperchat-prod/current
  PRODSQL -d hyperchat -c "SELECT 'prod alembic ' || version_num FROM alembic_version"
  ls -l --time-style=+%FT%T /etc/hyperchat-prod/api.env | awk '{print $1, $3, $4, $5, $6}'
  if [ -e /var/lib/hyperchat-kyc/prod.witness ]; then echo "prod.witness PRESENT"; else echo "prod.witness absent"; fi
}
# DEV state Phase A must not change: exactly 3 lines, compared before and after
dev_snapshot() {
  readlink -f /opt/hyperchat/current
  systemctl is-active hyperchat-api hyperchat-worker hyperchat-cleanup.timer | tr '\n' ' '; echo
  Q "SELECT 'dev alembic ' || version_num FROM alembic_version"
}
# observations that legitimately change with time (a nightly backup may run during Phase A): shown, never compared;
# failing to collect them is a failure (never an empty, success-looking report)
prod_observations() {
  local newest result
  newest="$(ls -t /var/backups/hyperchat/hyperchat_*.dump 2>/dev/null | head -n 1)" && [ -n "$newest" ] || return 1
  result="$(systemctl show hyperchat-pgbackup.service -p Result -p ExecMainExitTimestamp)" && [ -n "$result" ] || return 1
  echo "newest production backup: $newest"; printf '%s\n' "$result" | tr '\n' ' '; echo
}
# the exact lines check_isolation.sh must print before Phase C (one per check): every check OK except the two
# legacy lines Phase C fixes (pg_hba lacks lifecycle / auth / stepup; pg_ident maps the API user to app and worker
# only). Formats are the script's own: ok "OK    <label>", fail "FAIL  <label>: <output, lines joined>".
audit_expected() {  # $1 = dev | prod
  local svc other app etc rel units u mode
  case "$1" in
    dev)  svc=hyperchat; other=hyperchat-prod; app=/opt/hyperchat; etc=/etc/hyperchat; rel=229fe42381e2
          units="hyperchat-api hyperchat-worker hyperchat-cleanup" ;;
    prod) svc=hyperchat-prod; other=hyperchat; app=/opt/hyperchat-prod; etc=/etc/hyperchat-prod; rel=940137070657
          units="hyperchat-prod-api hyperchat-prod-worker hyperchat-prod-cleanup" ;;
    *) return 1 ;;
  esac
  mode="$(stat -c '%U:%G %a' "$etc/api.env")"
  case "$mode" in "root:$svc 640" | "root:$svc 600" | "root:root 600") ;; *) mode="root:$svc 640" ;; esac
  printf '%s\n' "OK    release tree $app owned by root, not writable by group/other, no access for other" \
    "OK    no writable directory on the way to $app" \
    "OK    every symlink under $app resolves only through root-owned, non-writable paths" \
    "OK    every directory reached through a symlink is root's and not writable by group/other, recursively" \
    "OK    no mount point inside $app or a directory it links to" \
    "OK    current -> $app/releases/$rel" \
    "OK    env directory $etc owned by root, not writable by group/other" \
    "OK    no writable directory on the way to $etc/api.env" \
    "OK    api.env is $mode"
  for u in $units; do
    if [ -f "/etc/systemd/system/$u.service" ]; then
      printf '%s\n' "OK    $u.service owned by root, not writable by group/other" "OK    $u runs as $svc"
    else
      printf '%s\n' "OK    $u.service not installed (skipped)"
    fi
  done
  printf '%s\n' "OK    $svc has no login shell" "OK    $svc is not in sudo/adm/wheel/root/$other" \
    "FAIL  pg_hba HyperChat rule covers every login role: missing: hyperchat_auth missing: hyperchat_lifecycle missing: hyperchat_stepup" \
    "OK    pg_hba HyperChat rule takes effect (no earlier local rule matches its roles)" \
    "FAIL  pg_ident maps $svc to: 'hyperchat_app hyperchat_worker ' (want 'hyperchat_app hyperchat_auth hyperchat_lifecycle hyperchat_worker ')" \
    "OK    pg_ident never maps $other" \
    "OK    only $svc-stepup maps to hyperchat_stepup, and $svc-stepup to nothing else" \
    "OK    step-up verifier not installed (skipped)"
}
# run the audit, keep its full output, stderr and exit status, and accept exactly the expected lines (read-only)
audit_check() {  # $1 = dev | prod, $2 = record tag (A1 | A2)
  local out="$ST/isolation-$2-$1" rc
  bash "$STAGE/deploy/check_isolation.sh" "$1" > "$out.txt" 2> "$out.err"; rc=$?
  echo "$rc" > "$out.exit"
  [ "$rc" = 1 ] || { echo "STOP: $1 audit exited $rc (expected 1: the two legacy FAILs only)"; return 1; }
  [ ! -s "$out.err" ] || { echo "STOP: $1 audit wrote to stderr:"; head -n 5 "$out.err"; return 1; }
  audit_expected "$1" > "$out.expected" || { echo "STOP: cannot build the expected $1 lines"; return 1; }
  diff <(LC_ALL=C sort "$out.expected") <(LC_ALL=C sort "$out.txt") > "$out.diff" \
    || { echo "STOP: $1 audit differs from the expected lines (< expected / missing, > printed / unexpected):"; cat "$out.diff"; return 1; }
  echo "audit $2 $1: exactly the expected lines ($(grep -c '^OK' "$out.txt") OK, 2 legacy FAIL)"
}
```

### A0. Preconditions and the "before" snapshots
A0 writes nothing unless every precondition holds. Only then does it create the records directory, save the snapshots in it, and write `A0-passed`. It must print `A0 OK`.
```
a0_preconditions() {
  [ ! -e "$STAGE" ] || { echo "STOP: $STAGE already exists"; return 1; }
  [ ! -e "$REL_NEW" ] || { echo "STOP: $REL_NEW already exists"; return 1; }
  [ ! -e "$ST" ] || { echo "STOP: $ST already exists"; return 1; }
  [ ! -e /var/lib/hyperchat-kyc ] || { echo "STOP: /var/lib/hyperchat-kyc already exists"; ls -la /var/lib/hyperchat-kyc; return 1; }
  [ "$(readlink -f /opt/hyperchat/current)" = "$REL_OLD" ] || { echo "STOP: current is not $REL_OLD"; return 1; }
  head_is 0019 || return 1
  api_healthy "$(cat "$REL_OLD/backend/RELEASE")" || return 1
  command -v python3.14 >/dev/null || { echo "STOP: python3.14 missing"; return 1; }
  id hyperchat >/dev/null || { echo "STOP: no hyperchat user"; return 1; }
  local prod dev
  prod="$(prod_snapshot 2>&1)" && dev="$(dev_snapshot 2>&1)" || { echo "STOP: a snapshot command failed"; return 1; }
  [ "$(printf '%s\n' "$prod" | wc -l)" = 9 ] && [ "$(printf '%s\n' "$dev" | wc -l)" = 3 ] \
    || { echo "STOP: incomplete snapshot"; printf '%s\n' "$prod" "$dev"; return 1; }
  case "$prod" in c839856d30dbe62922907f633046d17e14a70f242061b2488d9ed20c3028fed3*) ;;
    *) echo "STOP: /usr/local/sbin/hyperchat-pgbackup is not the script verified in Phase 0.2"; return 1 ;; esac
  install -d -o root -g root -m 0700 "$ST" || { echo "STOP: cannot create $ST"; return 1; }
  printf '%s\n' "$prod" > "$ST/A0-prod.snapshot" && printf '%s\n' "$dev" > "$ST/A0-dev.snapshot" \
    || { echo "STOP: cannot save the snapshots"; return 1; }
  date -u +%FT%TZ > "$ST/A0-passed"
  echo "A0 OK: preconditions hold; snapshots saved in $ST"
}
a0_preconditions
```

### A1. Staging copy, records, isolation audit
The staging copy is root-only. It provides the scripts and the smoke suite, and is never run as a service. The transfer runs only after A0 passed, and never into an existing directory.

**(workstation)**
```
git archive c2fe883be71d1a494232d4147ed4d6fbc4e70eef backend database deploy tests \
  | ssh root@VPS "[ -f /root/rehearsal-dev/A0-passed ] && [ ! -e /root/hyperchat-c2fe883 ] && install -d -o root -g root -m 0700 /root/hyperchat-c2fe883 && tar -x -C /root/hyperchat-c2fe883"
```
**(VPS)**: must print `A1 OK`.
- **Allowed FAILs:** the audit (0.3) may report only the two legacy FAIL lines that Phase C fixes.
- **Everything else stops A1:** any other FAIL, a missing or extra check, other output, stderr, or an exit status other than 1. That covers release tree, `api.env`, units, service account, symlinks and mounts. Nothing is fixed automatically.
```
a1_records() {
  [ -f "$ST/A0-passed" ] || { echo "STOP: A0 has not passed"; return 1; }
  [ ! -e "$ST/A1-passed" ] || { echo "STOP: A1 already passed once"; return 1; }
  [ "$(stat -c '%U:%G %a' "$ST")" = "root:root 700" ] || { echo "STOP: $ST is not root:root 700"; return 1; }
  [ "$(fp "$STAGE")" = 83b9276f0484b72e1057e4698ec8d9a8c37a1fa4309d3e3116f2bad0dbac9e36 ] \
    && [ "$(find "$STAGE" -type f | wc -l)" = 381 ] || { echo "STOP: $STAGE is not the c2fe883 tree"; return 1; }
  [ "$(stat -c '%U:%G %a' "$STAGE")" = "root:root 700" ] || { echo "STOP: $STAGE is not root:root 700"; return 1; }
  cat "$REL_OLD/backend/RELEASE" > "$ST/old-release.id" && [ -s "$ST/old-release.id" ] || { echo "STOP: old release id"; return 1; }
  api_healthy "$(cat "$ST/old-release.id")" || return 1   # read-only: the running API reports exactly that id
  audit_check dev A1 && audit_check prod A1 || return 1
  date -u +%FT%TZ > "$ST/A1-passed"
  echo "A1 OK: staging copy verified; audits show only the two legacy FAILs"
}
a1_records
```

### A2. Build the target release
This follows "Deploy a commit" steps 1–3 (`deploy/README.md`), without switching `current`. The directory name follows the 12-character convention of `229fe42381e2`. The transfer runs only after A1 passed, and never into an existing directory.

**(workstation)**
```
git archive c2fe883be71d1a494232d4147ed4d6fbc4e70eef backend database \
  | ssh root@VPS "[ -f /root/rehearsal-dev/A1-passed ] && [ ! -e /opt/hyperchat/releases/c2fe883be71d ] && mkdir -p /opt/hyperchat/releases/c2fe883be71d && tar -x -C /opt/hyperchat/releases/c2fe883be71d"
```
**(VPS)**: must print `A2 OK`.
```
build_release() {
  [ -f "$ST/A1-passed" ] || { echo "STOP: A1 has not passed"; return 1; }
  [ ! -e "$ST/A2-passed" ] || { echo "STOP: A2 already passed once"; return 1; }
  [ "$(fp "$REL_NEW")" = 1ffdf1e5c931d4e67eb9e4be9f1c9a69284a63ec691efec4042c2293e7df1d78 ] \
    && [ "$(find "$REL_NEW" -type f | wc -l)" = 335 ] || { echo "STOP: $REL_NEW is not the c2fe883 tree"; return 1; }
  [ -d "$REL_NEW/backend" ] && [ ! -e "$REL_NEW/venv" ] || { echo "STOP: $REL_NEW missing, or a venv already exists"; return 1; }
  python3.14 -m venv "$REL_NEW/venv" && "$REL_NEW/venv/bin/pip" install -q -r "$REL_NEW/backend/requirements.txt" || { echo "STOP: venv"; return 1; }
  echo c2fe883be71d > "$REL_NEW/backend/RELEASE" \
    && chown -R root:hyperchat "$REL_NEW" && chmod -R g+rX,g-w,o-rwx "$REL_NEW" || { echo "STOP: RELEASE / ownership"; return 1; }
  # as the service user (proves it can read the release) and without writing bytecode: a root import would leave
  # root-owned, world-readable __pycache__ directories, which the isolation audit (no access for other) refuses
  ( cd "$REL_NEW/backend" && sudo -u hyperchat env PYTHONDONTWRITEBYTECODE=1 ../venv/bin/python -c "import app.main" ) \
    || { echo "STOP: import"; return 1; }
  [ "$(find "$REL_NEW" ! -user root | wc -l)" = 0 ] && [ "$(find "$REL_NEW" ! -type l -perm /g=w | wc -l)" = 0 ] \
    && [ "$(find "$REL_NEW" ! -type l -perm /o=rwx | wc -l)" = 0 ] \
    && [ "$(find "$REL_NEW/backend" "$REL_NEW/database" -name __pycache__ | wc -l)" = 0 ] \
    || { echo "STOP: release ownership / permissions / bytecode not as required"; return 1; }
  [ "$(cat "$REL_NEW/backend/RELEASE")" = c2fe883be71d ] || { echo "STOP: RELEASE"; return 1; }
  "$REL_NEW/venv/bin/pip" check >/dev/null || { echo "STOP: pip check"; return 1; }
  [ "$(readlink -f /opt/hyperchat/current)" = "$REL_OLD" ] || { echo "STOP: current is not $REL_OLD"; return 1; }
  api_healthy "$(cat "$ST/old-release.id")" || return 1   # the running API still serves the old release
  # the new release is now in the audited tree: the audits must be exactly as after A1 (no new failure)
  audit_check dev A2 && audit_check prod A2 || return 1
  cmp -s "$ST/isolation-A1-dev.txt" "$ST/isolation-A2-dev.txt" && cmp -s "$ST/isolation-A1-prod.txt" "$ST/isolation-A2-prod.txt" \
    || { echo "STOP: the audit output changed between A1 and A2"; return 1; }
  date -u +%FT%TZ > "$ST/A2-passed"
  echo "A2 OK: release built, nothing switched, audits unchanged"
}
build_release
```
If the build fails, an unused directory is left, and nothing else. It is removed or rebuilt only by decision.

### A3. Create the DEV KYC witness
The witness follows `docs/kyc-rollout.md` "Server setup", so that every backup in Phases B and E carries it. A3 runs only after A2 passed, and must print `A3 OK`.
- **Fail-closed:** an existing witness (or anything at that path) is a STOP, and the function then touches nothing: no recreate, truncate or attribute change.
- **The directory:** an existing directory is used only when it is a real `root:root 0755` directory; it is never re-moded.
```
create_dev_witness() {
  local D=/var/lib/hyperchat-kyc
  [ -f "$ST/A2-passed" ] || { echo "STOP: A2 has not passed"; return 1; }
  if [ -e "$W" ] || [ -L "$W" ]; then
    echo "STOP: $W already exists; it is never recreated, truncated or modified"
    stat -c '%U:%G %a %s bytes' "$W"; lsattr "$W"
    return 1
  fi
  if [ -e "$D" ] || [ -L "$D" ]; then
    if [ -L "$D" ] || [ ! -d "$D" ] || [ "$(stat -c '%U:%G %a' "$D")" != "root:root 755" ]; then
      echo "STOP: $D exists but is not a root:root 0755 directory"; return 1
    fi
  else
    install -d -o root -g root -m 0755 "$D" || { echo "STOP: cannot create $D"; return 1; }
  fi
  install -o hyperchat -g hyperchat -m 0600 /dev/null "$W" || { echo "STOP: cannot create $W"; return 1; }
  if ! chattr +a "$W"; then
    echo "STOP: $W was created but is NOT append-only; do not retry or delete it: escalate"; return 1
  fi
  [ "$(stat -c '%U:%G %a %s' "$W")" = "hyperchat:hyperchat 600 0" ] && witness_ok >/dev/null \
    || { echo "STOP: $W is not an empty hyperchat:hyperchat 0600 append-only file"; return 1; }
  date -u +%FT%TZ > "$ST/A3-passed"
  echo "A3 OK: $(stat -c '%U:%G %a %s bytes' "$W"), $(lsattr "$W" | awk '{print $1}')"
}
create_dev_witness
```
- **An empty file is a fresh witness.** At 0019 nothing reads it.
- **After 0023:** the API appends a line only once enforcement history exists (`KycWitness.record` returns early at generation 0), so the file stays empty throughout this rehearsal.
- **From now on, nobody removes, truncates, replaces or `chattr -a`s it,** including during rollback.
- **Production's witness** (`prod.witness`) is not created here.

**A4. No backup units, no `/usr/local/sbin` install.** See "The backup script and production".

### A9. After: compare, don't just display
- **Must be identical to A0's saved snapshots:** the production snapshot (backup script and its directory, backup units and timers, unit files, service states, release, alembic, `api.env`, no production witness) and the DEV snapshot (release, services, 0019).
- **Shown but not compared:** observations that legitimately change with time (the newest production backup archive, its last result).

It must print `A9 OK`.
```
a9_after() {
  [ -f "$ST/A3-passed" ] || { echo "STOP: A3 has not passed"; return 1; }
  prod_snapshot > "$ST/A9-prod.snapshot" 2>&1 && dev_snapshot > "$ST/A9-dev.snapshot" 2>&1 \
    || { echo "STOP: a snapshot command failed"; return 1; }
  diff "$ST/A0-prod.snapshot" "$ST/A9-prod.snapshot" || { echo "STOP: PRODUCTION CHANGED during Phase A (diff above)"; return 1; }
  diff "$ST/A0-dev.snapshot" "$ST/A9-dev.snapshot" || { echo "STOP: DEV release, services or schema changed (diff above)"; return 1; }
  api_healthy "$(cat "$ST/old-release.id")" || return 1
  echo "observations (not compared):"
  prod_observations || { echo "STOP: cannot collect the production observations"; return 1; }
  echo "A9 OK: production and DEV unchanged by Phase A"
}
a9_after && ls -la "$ST"
```

**Record:** the files in `$ST`:
- the markers;
- `old-release.id`;
- the A0 and A9 snapshots;
- each audit's `.txt`, `.err`, `.exit`, `.expected`, `.diff`;
- and the printed `A0 OK` … `A9 OK` lines.

---

## Phase B: Fresh backup and disposable restore test (approval: "Phase B")
**Requires:** PR Ankit-KK/hyperchat-v2#88 merged into `main` (the fixed backup script), and Phase A complete (A9 OK).

**B0. Stage the fixed backup script** (root-only, pinned). The transfer runs only after A3 passed, and never into an existing directory.

**(workstation)**, after `git fetch origin` (a different script on `main` fails the pin below):
```
git show origin/main:deploy/backup/hyperchat-pgbackup.sh \
  | ssh root@VPS "[ -f /root/rehearsal-dev/A3-passed ] && [ ! -e /root/hyperchat-backupfix ] && install -d -o root -g root -m 0700 /root/hyperchat-backupfix && cat > /root/hyperchat-backupfix/hyperchat-pgbackup.sh && chmod 0600 /root/hyperchat-backupfix/hyperchat-pgbackup.sh"
```
**(VPS)**: must print `B0 OK`.
```
b0_staged() {
  [ -f "$ST/A3-passed" ] || { echo "STOP: Phase A is not complete"; return 1; }
  [ ! -e "$ST/B0-passed" ] || { echo "STOP: B0 already passed once"; return 1; }
  [ "$(stat -c '%U:%G %a' "${BACKUP_SCRIPT%/*}")" = "root:root 700" ] && [ ! -L "$BACKUP_SCRIPT" ] \
    && [ "$(stat -c '%U:%G %a' "$BACKUP_SCRIPT")" = "root:root 600" ] || { echo "STOP: $BACKUP_SCRIPT is not root-only"; return 1; }
  [ "$(sha256sum "$BACKUP_SCRIPT" | cut -d' ' -f1)" = "$BACKUP_SHA" ] || { echo "STOP: $BACKUP_SCRIPT is not the pinned script"; return 1; }
  bash -n "$BACKUP_SCRIPT" || { echo "STOP: syntax"; return 1; }
  [ "$(sha256sum /usr/local/sbin/hyperchat-pgbackup | cut -d' ' -f1)" = c839856d30dbe62922907f633046d17e14a70f242061b2488d9ed20c3028fed3 ] \
    || { echo "STOP: the production backup script changed"; return 1; }   # never touched by B0
  date -u +%FT%TZ > "$ST/B0-passed"
  echo "B0 OK: $BACKUP_SCRIPT is the pinned fixed script"
}
b0_staged
```

**B1. One verified DEV backup set** (the pinned script, DEV profile on the same line; see "The backup script and production"):
```
b1_backup() {  # non-zero on any failure; $ST/phaseB.set exists only after a verified backup
  rm -f "$ST/phaseB.set"
  [ -f "$ST/B0-passed" ] || { echo "STOP: B0 has not passed"; return 1; }
  dev_backup phaseB.set || { rm -f "$ST/phaseB.set"; echo "STOP: the backup failed; $ST/phaseB.set stays absent"; return 1; }
}
b1_backup
```
- **The backup directory already exists:** `/var/backups/hyperchat-dev` (root 0700, only `.lock`) is left from the stopped 2026-10-24 attempt. The script accepts it as it is; never remove it by hand.
- **Required:** `SET hyperchat_dev_<UTC>`, written to `$ST/phaseB.set` only when the script exited 0 and named a complete set.
- **Any other result is a STOP, and `$ST/phaseB.set` stays absent:**
  - exit 3: witness missing, so A3 wasn't done;
  - 2: refused configuration, or an enforcement fact that doesn't parse;
  - 4: an older set fails verification (none should exist yet);
  - 75: another run in progress;
  - 1: failed (including a failed query, or an enforcement table without exactly one row: never recorded as absent).

**B2. Restore it into a disposable PostgreSQL 15** (`docs/backup-recovery.md` "Verifying a set"). The set is at 0019, so pass its own head (the default would be this checkout's 0028). The scratch cluster gets its roles from the pinned staging `roles.sql` (`roles_sql_ok`, `--roles-sql`). It fails closed:
- `B2-passed` is written only after `restore_check` exited 0, the scratch cluster is gone, and every required JSON field holds.
- Any failure returns non-zero, with `STOP` and no marker.
```
b2_restore() { local rc
  rm -f "$ST/B2-passed"
  test -s "$ST/phaseB.set" || { echo "STOP: no Phase B set recorded"; return 1; }
  # the recorded B1 set, reused as it is: complete (manifest) and its dump matching its checksum file
  [ -f "/var/backups/hyperchat-dev/$(cat "$ST/phaseB.set").manifest.json" ] \
    && ( cd /var/backups/hyperchat-dev && sha256sum -c --quiet "$(cat "$ST/phaseB.set").dump.sha256" ) \
    || { echo "STOP: the Phase B set is incomplete or its checksum does not match"; return 1; }
  roles_sql_ok || return 1
  [ -z "$(ls -d /tmp/hc-restorecheck-* 2>/dev/null)" ] || { echo "STOP: a scratch directory is left from an earlier run"; return 1; }
  ( cd "$REL_NEW/backend" && ../venv/bin/python -m app.backup.restore_check --env dev --dir /var/backups/hyperchat-dev \
      --set "$(cat "$ST/phaseB.set")" --pg-bin /usr/lib/postgresql/15/bin --scratch --scratch-user postgres \
      --roles-sql "$ROLES_SQL" --expect-head 0019 --output "$ST/restorecheck-0019.json" ); rc=$?
  [ "$rc" = 0 ] || { echo "STOP: restore_check exited $rc (see $ST/restorecheck-0019.json)"; return 1; }
  [ -z "$(ls -d /tmp/hc-restorecheck-* 2>/dev/null)" ] || { echo "STOP: the scratch cluster was not removed (do not delete it by hand)"; return 1; }
  python3 -I - "$ST/restorecheck-0019.json" "$(cat "$ST/phaseB.set")" <<'PY' || { echo "STOP: the restore check result is not as required"; return 1; }
import json, sys
d = json.load(open(sys.argv[1]))
checks = {"set is the B1 set": d.get("set") == sys.argv[2],
          "structurally_valid: true": d.get("structurally_valid") is True,
          "restore_valid: true": d.get("restore_valid") is True,
          "invariants.alembic_version: 0019": (d.get("invariants") or {}).get("alembic_version") == "0019",
          "kyc_witness: not_applicable or consistent": d.get("kyc_witness") in ("not_applicable", "consistent"),
          "live_operation_approved: false": d.get("live_operation_approved") is False}
for name, ok in checks.items():
    print(("OK    " if ok else "FAIL  ") + name)
sys.exit(0 if all(checks.values()) else 1)
PY
  date -u +%FT%TZ > "$ST/B2-passed" && echo "B2 OK"
}
b2_restore
```
**Required:**
- `B2 OK`. `restore_check` exits 0 only when the restore is structurally valid and the witness verdict is `consistent` or `not_applicable`.
- Every JSON line printed is `OK`:
  - the set is the B1 set;
  - `structurally_valid: true`, `restore_valid: true`;
  - `invariants.alembic_version: "0019"`;
  - `kyc_witness` is `not_applicable` (no KYC tables at 0019) or `consistent`;
  - `live_operation_approved: false` (always).

The scratch cluster runs in a temporary directory (`/tmp/hc-restorecheck-*`) with no TCP and is deleted afterwards. A directory left behind is a STOP, never removed by hand. No live database is touched.

**Re-running B2:** `b2_restore` may be run again on its own after a STOP. It reuses the recorded B1 set (`$ST/phaseB.set`) after checking its manifest and checksum, and never repeats B0 or B1.
- **2026-10-24 stop:** B2 stopped with `restore_check` exit 1 (`CalledProcessError`). Before the explicit `--roles-sql`, the tool looked for `<release>/deploy/postgres/roles.sql`, which the built release doesn't have.
- **What that run left:** the scratch cluster was removed, no `B2-passed` was written, and the B1 set `hyperchat_dev_20261024T140604Z` is intact.

**B3. Fallback:** the older manual dump `hyperchat_dev_pre_main229fe42_20261015T045216Z.dump` stays where it is. Restoring it would lose 8 days of DEV changes. It is a fallback only if the B1 set turns out unusable, and only by separate decision.

**Record:** `$ST/B0-passed` with the staged script's SHA-256, `$ST/phaseB.set`, `$ST/B2-passed`, the manifest's dump `sha256`, `$ST/restorecheck-0019.json`.

---

## Phase C: Roles, authentication rules, environment (approval: "Phase C"; requires 0.1 and 0.4 answered)
These changes are compatible with the running 0019 release:
- New roles and map lines are unused by it, and new `api.env` settings are ignored by it (`extra="ignore"`; it defines none of the five keys).
- `roles.sql` leaves the five existing roles exactly as they are (connection limits, timeouts and membership already match; re-checked 2026-10-24).
- Nothing is restarted. A reload affects only NEW connections; the running API's pool keeps its connections.

**How to run it:**
- **One root shell per step:** the session setup, then the step's block exactly as written.
- **Step gates:** each step writes a marker in `$ST` only when everything in it succeeded, and returns non-zero with `STOP` otherwise. The next step refuses to start without it: B2 → C1 → `C1-passed` → C2 → `C2-passed` → C3 → `C3-passed` → C4 → `C4-passed` → C5 → `C5-passed`.
- **Any `STOP`:** record and escalate ("If a phase fails"). Undoing C's file changes (below) is a separate owner decision.

**Expected changes (read-only preflight, 2026-10-24):**
- **`pg_hba.conf`:** one line. The HyperChat rule (line 95) gains `hyperchat_lifecycle,hyperchat_auth,hyperchat_stepup`. Every other line is unchanged, and there are no parse errors.
- **`pg_ident.conf`:** four lines are added.
  - `root → hyperchat_auth`, `hyperchat → hyperchat_lifecycle`, `hyperchat → hyperchat_auth`, `hyperchat-stepup → hyperchat_stepup`.
  - The OS user `hyperchat-stepup` doesn't exist yet.
- **Roles:**
  - **New:** `hyperchat_lifecycle` (connection limit 5, timeout 15 s), `hyperchat_auth` (10, 15 s), `hyperchat_stepup` (5, 5 s). All are `LOGIN`, with no superuser, create-db, create-role, replication or bypass-RLS.
  - **Grants:** `CONNECT` on `hyperchat_dev` for the three new roles.
- **`api.env`:** five keys are appended, none present today.
  - The file is `root:hyperchat 600`, ending in a newline, with no step-up socket or Didit keys.

**C1. Keep the originals** (single-use, verified copies):
```
c1_originals() { local f d
  [ -f "$ST/B2-passed" ] || { echo "STOP: Phase B has not passed"; return 1; }
  [ ! -e "$ST/C1-passed" ] || { echo "STOP: C1 already passed once"; return 1; }
  for f in "$PGCONF/pg_hba.conf" "$PGCONF/pg_ident.conf" "$API_ENV"; do
    d="$ST/$(basename "$f").before-C"
    [ ! -e "$d" ] || { echo "STOP: $d already exists; it is never overwritten"; return 1; }
  done
  for f in "$PGCONF/pg_hba.conf" "$PGCONF/pg_ident.conf" "$API_ENV"; do
    d="$ST/$(basename "$f").before-C"
    cp -p "$f" "$d" && cmp -s "$f" "$d" || { echo "STOP: cannot copy $f"; return 1; }
  done
  QP "SELECT coalesce(datacl::text, '') FROM pg_database WHERE datname = 'hyperchat_restore_test'" > "$ST/restore_test-acl.before-C" \
    || { echo "STOP: cannot record the hyperchat_restore_test ACL"; return 1; }
  date -u +%FT%TZ > "$ST/C1-passed" && echo "C1 OK: originals kept in $ST"
}
c1_originals
```

**C2. Roles, CONNECT grants and the pg_hba rule** (`setup_cluster.sh`, idempotent). It:
- applies `roles.sql` (the three new roles);
- syncs the HyperChat pg_hba rule, keeping its own `pg_hba.conf.bak-<timestamp>`;
- reloads the cluster;
- revokes `CONNECT` from `PUBLIC` on `hyperchat_dev` and grants it to the seven HyperChat login roles.

Before it runs, the preview must be **exactly** the expected one-line change, checked by the function, not by eye. Name only `hyperchat_dev`; `hyperchat_restore_test` must stay as it is. `setup_cluster.sh` runs nothing on the production cluster: every command takes the version, cluster and port given.
```
c2_roles_hba() { local H="$PGCONF/pg_hba.conf" old new rc
  old='local   all   hyperchat_migrator,hyperchat_app,hyperchat_worker,hyperchat_readonly   peer map=hyperchat'
  new="$(grep -v '^#' "$STAGE/deploy/postgres/pg_hba.conf" | grep -v '^[[:space:]]*$')"
  [ -f "$ST/C1-passed" ] || { echo "STOP: C1 has not passed"; return 1; }
  [ ! -e "$ST/C2-passed" ] || { echo "STOP: C2 already passed once"; return 1; }
  cmp -s "$H" "$ST/pg_hba.conf.before-C" || { echo "STOP: pg_hba.conf changed since C1"; return 1; }
  bash "$STAGE/deploy/postgres/sync_pg_hba.sh" "$STAGE/deploy/postgres/pg_hba.conf" "$H" > "$ST/pg_hba.conf.C2-preview" \
    || { echo "STOP: sync_pg_hba.sh refused the live pg_hba.conf"; return 1; }
  diff "$H" "$ST/pg_hba.conf.C2-preview" > "$ST/pg_hba.C2.diff"; rc=$?
  [ "$rc" = 1 ] && [ "$(grep -c '^[<>]' "$ST/pg_hba.C2.diff")" = 2 ] && grep -qxF "< $old" "$ST/pg_hba.C2.diff" \
    && grep -qxF "> $new" "$ST/pg_hba.C2.diff" || { echo "STOP: the pg_hba change is not exactly the expected one line:"; cat "$ST/pg_hba.C2.diff"; return 1; }
  bash "$STAGE/deploy/postgres/setup_cluster.sh" 15 restoretest 5433 hyperchat_dev > "$ST/setup_cluster.C2.log" 2>&1 \
    || { echo "STOP: setup_cluster.sh failed (see $ST/setup_cluster.C2.log)"; tail -n 5 "$ST/setup_cluster.C2.log"; return 1; }
  cmp -s "$H" "$ST/pg_hba.conf.C2-preview" || { echo "STOP: the installed pg_hba.conf is not the previewed one"; return 1; }
  [ "$(QP "SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL")" = 0 ] || { echo "STOP: pg_hba.conf has parse errors"; return 1; }
  [ "$(QP "SELECT count(*) FROM pg_roles WHERE rolname IN ('hyperchat_lifecycle','hyperchat_auth','hyperchat_stepup') AND rolcanlogin AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)")" = 3 ] \
    || { echo "STOP: the three new roles are not all present as plain login roles"; return 1; }
  [ "$(QP "SELECT count(*) FROM unnest(ARRAY['hyperchat_migrator','hyperchat_app','hyperchat_worker','hyperchat_readonly','hyperchat_lifecycle','hyperchat_auth','hyperchat_stepup']) r WHERE has_database_privilege(r, 'hyperchat_dev', 'CONNECT')")" = 7 ] \
    || { echo "STOP: CONNECT on hyperchat_dev is not granted to all seven roles"; return 1; }
  [ "$(QP "SELECT coalesce(datacl::text, '') FROM pg_database WHERE datname = 'hyperchat_restore_test'")" = "$(cat "$ST/restore_test-acl.before-C")" ] \
    || { echo "STOP: the hyperchat_restore_test ACL changed"; return 1; }
  date -u +%FT%TZ > "$ST/C2-passed" && echo "C2 OK: roles, CONNECT grants, the HyperChat pg_hba rule"
}
c2_roles_hba
```

**C3. pg_ident lines.** `setup_cluster.sh` appends the map only when none exists, so DEV needs these lines added. Each missing mapping of `deploy/postgres/pg_ident.conf` is appended, compared by field so that differences in spacing never cause duplicates:
```
c3_ident() { local F="$PGCONF/pg_ident.conf" m u r
  [ -f "$ST/C2-passed" ] || { echo "STOP: C2 has not passed"; return 1; }
  [ ! -e "$ST/C3-passed" ] || { echo "STOP: C3 already passed once"; return 1; }
  ! grep -qE '^[^#]*[[:space:]]hyperchat-prod[[:space:]]' "$F" || { echo "STOP: a hyperchat-prod line is in the DEV map"; return 1; }
  [ -z "$(tail -c1 "$F")" ] || { echo "STOP: $F does not end with a newline"; return 1; }
  for m in "root hyperchat_auth" "hyperchat hyperchat_lifecycle" "hyperchat hyperchat_auth" "hyperchat-stepup hyperchat_stepup"; do
    u="${m% *}"; r="${m#* }"
    awk -v u="$u" -v r="$r" '$1 == "hyperchat" && $2 == u && $3 == r {f=1} END {exit !f}' "$F" \
      || printf 'hyperchat   %-17s %s\n' "$u" "$r" >> "$F" || { echo "STOP: cannot append to $F"; return 1; }
  done
  for m in "root hyperchat_auth" "hyperchat hyperchat_lifecycle" "hyperchat hyperchat_auth" "hyperchat-stepup hyperchat_stepup"; do
    u="${m% *}"; r="${m#* }"
    [ "$(awk -v u="$u" -v r="$r" '$1 == "hyperchat" && $2 == u && $3 == r' "$F" | wc -l)" = 1 ] || { echo "STOP: '$m' is not in $F exactly once"; return 1; }
  done
  pg_ctlcluster 15 restoretest reload || { echo "STOP: reload failed"; return 1; }
  [ "$(QP "SELECT count(*) FROM pg_ident_file_mappings WHERE error IS NOT NULL")" = 0 ] || { echo "STOP: pg_ident.conf has parse errors"; return 1; }
  grep -nE '^hyperchat[[:space:]]' "$F"
  date -u +%FT%TZ > "$ST/C3-passed" && echo "C3 OK: pg_ident lines"
}
c3_ident
```
- **The `hyperchat-stepup` line is harmless** before that OS user exists. The isolation audit requires that only that user maps to `hyperchat_stepup`.
- **Never add** a `hyperchat-prod` line here, or any DEV line on the production cluster.

**C4. `api.env` settings** (names from `deploy/api.env.example`; no secret value). The live file is replaced in one atomic step, never edited in place:
- **Checked before anything is written:**
  - the file's ownership and mode (`root:hyperchat` 600 or 640, as the isolation audit accepts);
  - not a symlink, a final newline;
  - no verifier socket or Didit setting;
  - none of the five keys with another value or more than once;
  - no staging file left from an earlier run;
  - unchanged since C1 (its `.before-C` copy);
  - a DEV path (never `/etc/hyperchat-prod/`).
- **Staged:** the complete proposed file is written to `mktemp` in the same directory (same filesystem; root-owned, mode 0600 from creation). It holds the current file unchanged, followed by the missing keys.
- **Validated:** the staged file must start with the current file byte-for-byte, add exactly the missing keys and nothing else, and hold each of the five settings exactly once with its value. It then takes the live file's owner, group and mode.
- **Installed:** `mv -T` renames it over the live file (`rename(2)`: atomic; the live file is either the old one or the complete new one). Any failure before this removes the staged file and leaves the live file untouched.
- **Verified after installation:** the content again, the owner and group, and the mode. A failure here leaves the complete new file installed, and C4 STOPs without a marker. Recovery: `c_undo_files` (below, owner approval) puts back the C1 copy; the running API doesn't read `api.env` until it restarts, and Phase C restarts nothing.
```
c4_env() { local E="$API_ENV" N kv k mode add
  local want="HYPERCHAT_DB_LIFECYCLE_USER=hyperchat_lifecycle HYPERCHAT_DB_AUTH_USER=hyperchat_auth HYPERCHAT_ADMIN_SESSION_IDLE_HOURS=2 HYPERCHAT_ADMIN_SESSION_ABSOLUTE_HOURS=12 HYPERCHAT_KYC_WITNESS_PATH=/var/lib/hyperchat-kyc/dev.witness"
  [ -f "$ST/C3-passed" ] || { echo "STOP: C3 has not passed"; return 1; }
  [ ! -e "$ST/C4-passed" ] || { echo "STOP: C4 already passed once"; return 1; }
  case "$E" in /etc/hyperchat-prod/*) echo "STOP: $E is production's; never changed"; return 1 ;; esac
  # 1. the live file, before anything is written
  cmp -s "$E" "$ST/api.env.before-C" || { echo "STOP: $E changed since C1; nothing written"; return 1; }
  mode="$(stat -c '%U:%G %a' "$E")"
  case "$mode" in "root:hyperchat 600" | "root:hyperchat 640") ;; *) echo "STOP: $E is $mode (want root:hyperchat 600 or 640); nothing written"; return 1 ;; esac
  [ ! -L "$E" ] && [ -z "$(tail -c1 "$E")" ] || { echo "STOP: $E is a symlink or does not end with a newline; nothing written"; return 1; }
  [ "$(grep -cE '^HYPERCHAT_(STEPUP_SOCKET|DIDIT_)' "$E")" = 0 ] || { echo "STOP: a verifier socket or Didit setting is present; nothing written"; return 1; }
  add=""
  for kv in $want; do k="${kv%%=*}"
    case "$(grep -c "^$k=" "$E")" in
      0) add="$add$kv"$'\n' ;;
      1) grep -qxF "$kv" "$E" || { echo "STOP: $k already set to another value; nothing written"; return 1; } ;;
      *) echo "STOP: $k is set more than once; nothing written"; return 1 ;;
    esac
  done
  [ -z "$(ls -A "${E%/*}" | grep '^\.api\.env\.C4\.')" ] || { echo "STOP: a staging file from an earlier run is in ${E%/*}; nothing written"; return 1; }
  # 2. stage the complete proposed file next to it (same filesystem), root-only from creation
  N="$(mktemp "${E%/*}/.api.env.C4.XXXXXX")" || { echo "STOP: cannot create a staging file; nothing written"; return 1; }
  [ "$(stat -c '%U %a' "$N")" = "root 600" ] && [ ! -L "$N" ] || { rm -f -- "$N"; echo "STOP: the staging file is not root-only; nothing written"; return 1; }
  { cat -- "$E" && printf '%s' "$add"; } > "$N" || { rm -f -- "$N"; echo "STOP: staging failed; nothing written"; return 1; }
  # 3. validate the whole proposed file
  cmp -s -n "$(stat -c %s "$E")" "$E" "$N" && [ "$(tail -c +"$(( $(stat -c %s "$E") + 1 ))" "$N")" = "${add%$'\n'}" ] \
    || { rm -f -- "$N"; echo "STOP: the staged file is not the current file plus exactly the new keys; nothing written"; return 1; }
  for kv in $want; do k="${kv%%=*}"
    [ "$(grep -c "^$k=" "$N")" = 1 ] && grep -qxF "$kv" "$N" || { rm -f -- "$N"; echo "STOP: $k is not set exactly once in the staged file; nothing written"; return 1; }
  done
  chown --reference="$E" -- "$N" && chmod --reference="$E" -- "$N" && [ "$(stat -c '%U:%G %a' "$N")" = "$mode" ] \
    || { rm -f -- "$N"; echo "STOP: cannot give the staged file $mode; nothing written"; return 1; }
  sync -- "$N"
  # 4. install atomically
  mv -T -- "$N" "$E" || { rm -f -- "$N"; echo "STOP: the atomic install failed; $E is unchanged"; return 1; }
  # 5. verify the installed file
  [ ! -L "$E" ] && [ "$(stat -c '%U:%G %a' "$E")" = "$mode" ] && cmp -s -n "$(stat -c %s "$ST/api.env.before-C")" "$ST/api.env.before-C" "$E" \
    || { echo "STOP: the installed $E is not as staged (owner, mode or content); recover with c_undo_files (owner approval)"; return 1; }
  for kv in $want; do k="${kv%%=*}"
    [ "$(grep -c "^$k=" "$E")" = 1 ] && grep -qxF "$kv" "$E" || { echo "STOP: $k is not set exactly once in the installed $E; recover with c_undo_files (owner approval)"; return 1; }
  done
  date -u +%FT%TZ > "$ST/C4-passed" && echo "C4 OK: five settings, $E replaced atomically, still $mode"
}
c4_env
```

**C5. Verify** (read-only apart from its marker). C5 checks all of the following:
- **Parse errors:** none in either file.
- **New connections:** fresh peer connections work for every OS-user / role pair the map allows, and the old pairs still work.
- **Refused pairs:** `hyperchat` must not reach `hyperchat_stepup` or `hyperchat_migrator`.
- **Audit:** the isolation audit is now all `OK`.
- **API:** the running 0019 API is still healthy.
```
c5_verify() { local pair u r got out rc
  [ -f "$ST/C4-passed" ] || { echo "STOP: C4 has not passed"; return 1; }
  [ "$(QP "SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL") $(QP "SELECT count(*) FROM pg_ident_file_mappings WHERE error IS NOT NULL")" = "0 0" ] \
    || { echo "STOP: pg_hba.conf or pg_ident.conf has parse errors"; return 1; }
  for pair in "hyperchat hyperchat_app" "hyperchat hyperchat_worker" "hyperchat hyperchat_lifecycle" "hyperchat hyperchat_auth" \
              "root hyperchat_migrator" "root hyperchat_readonly" "root hyperchat_auth"; do
    u="${pair% *}"; r="${pair#* }"; got="$(conn_as "$u" "$r")"
    [ "$got" = "$r" ] || { echo "STOP: OS user $u cannot connect as $r (got '$got')"; return 1; }
  done
  for r in hyperchat_stepup hyperchat_migrator; do
    [ -z "$(conn_as hyperchat "$r")" ] || { echo "STOP: OS user hyperchat can connect as $r"; return 1; }
  done
  out="$(bash "$STAGE/deploy/check_isolation.sh" dev 2>&1)"; rc=$?
  printf '%s\n' "$out" > "$ST/isolation-C5-dev.txt"
  [ "$rc" = 0 ] && ! printf '%s\n' "$out" | grep -q '^FAIL' || { echo "STOP: the isolation audit is not all OK (exit $rc):"; grep '^FAIL' "$ST/isolation-C5-dev.txt"; return 1; }
  api_healthy "$(cat "$ST/old-release.id")" || return 1   # the 0019 release still answers, healthy
  date -u +%FT%TZ > "$ST/C5-passed" && echo "C5 OK: Phase C verified"
}
c5_verify
```
**Record:** `$ST/pg_hba.C2.diff`, `$ST/setup_cluster.C2.log`, the pg_ident lines (C3 output), the `api.env` mode (C4 output), `$ST/isolation-C5-dev.txt` (all `OK`), `/health`, and the markers.

**Failure states and undoing C's file changes** (only with the owner's approval; never automatic). After a STOP, nothing has been restarted, and the running API keeps its pooled connections. What is left depends on the step:

| Stopped in | State left | Undo |
|---|---|---|
| C1 | at most the `.before-C` copies | nothing to undo |
| C2 before `setup_cluster.sh` | the preview files only | nothing to undo |
| C2 inside or after `setup_cluster.sh` | possibly the new pg_hba rule (reloaded), the new roles, the CONNECT grants | restore pg_hba (below) |
| C3 | possibly some of the four map lines (reloaded or not) | restore pg_ident (below) |
| C4 before the install | at most a staging file `.api.env.C4.*` (removed on every handled failure; one left by a killed shell is root 0600 and makes the next C4 STOP) | nothing to undo; remove a leftover staging file by hand after checking it |
| C4 after the install | the complete new `api.env`; the running API never reads it (it reads `api.env` only at start) | restore `api.env` (below) |
| C5 | nothing changed by C5 | as needed |

```
c_undo_files() {  # put back the C1 copies of the files C changed, reload, and verify new connections (owner approval)
  local f
  for f in "$PGCONF/pg_hba.conf" "$PGCONF/pg_ident.conf" "$API_ENV"; do
    [ -f "$ST/$(basename "$f").before-C" ] || { echo "STOP: no C1 copy of $f"; return 1; }
  done
  for f in "$PGCONF/pg_hba.conf" "$PGCONF/pg_ident.conf" "$API_ENV"; do
    cmp -s "$f" "$ST/$(basename "$f").before-C" || cp -p "$ST/$(basename "$f").before-C" "$f" || { echo "STOP: cannot restore $f"; return 1; }
  done
  pg_ctlcluster 15 restoretest reload || { echo "STOP: reload failed"; return 1; }
  [ "$(QP "SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL") $(QP "SELECT count(*) FROM pg_ident_file_mappings WHERE error IS NOT NULL")" = "0 0" ] \
    || { echo "STOP: parse errors after the restore"; return 1; }
  [ "$(conn_as hyperchat hyperchat_app)" = hyperchat_app ] && [ "$(conn_as hyperchat hyperchat_worker)" = hyperchat_worker ] \
    || { echo "STOP: the API's roles cannot connect after the restore"; return 1; }
  api_healthy "$(cat "$ST/old-release.id")" || return 1
  echo "C UNDONE: pg_hba.conf, pg_ident.conf and api.env are the C1 copies again"
}
```
- **What it doesn't undo:** the three new roles and their `CONNECT` grants stay. They are unused by the 0019 release and reachable by no one once the map lines are gone. Dropping them is a separate decision. `setup_cluster.sh`'s own `pg_hba.conf.bak-<timestamp>` stays as well.
- **If the DEV API ever loses database access after a reload** (new connections refused while the pool still answers): run `c_undo_files` with the owner's approval. A reload is enough; no restart is needed, and the API's pool reconnects through the restored rules.
- **A pg_hba or pg_ident file with errors is not applied on reload.** PostgreSQL keeps the previous rules and logs the error, which is why every step checks `pg_hba_file_rules` and `pg_ident_file_mappings` explicitly.

---

## Phase D: The migration rehearsal (approval: "Phase D", with a time window)
DEV is unavailable for the window: about 5–10 minutes (see "Expected window" after D4).

**How to run it:**
- **One root shell per step:** the session setup, then the step's block exactly as written. D3 runs one revision per line.
- **Step gates:** each step writes a marker in `$ST` only when everything in it succeeded, and returns non-zero with `STOP` otherwise. The next step refuses to start without it: D1 → `D1-passed` → D2 → `D2-passed` → D3 (`D3-<rev>-ok` per revision, in order) → `d3_gate` → `D3-passed` → D4 → `D4-passed`.
- **A new attempt starts with D1,** which removes every Phase D marker first, so no marker from an earlier attempt can authorize anything.

**D1. Pre-flight.** It checks the starting state and writes `D1-passed`; otherwise it STOPs before anything is stopped. The checks:
- Phase C completed;
- the built target release unchanged since A2 (its fingerprint, `RELEASE`, venv tools, ownership and permissions, no bytecode);
- DEV on the old release at 0019, healthy;
- the verified Phase B set;
- the witness;
- no bots.

It also records the expected values for D3's checks.
```
d1_preflight() { local f n
  rm -f "$ST/D1-passed" "$ST/D2-passed" "$ST/D3-passed" "$ST/D4-passed" "$ST"/D3-00[0-9][0-9]-ok "$ST/D-expect.txt" \
    && [ -z "$(ls "$ST" | grep -E '^D[1-4]-passed$|^D3-[0-9]{4}-ok$|^D-expect\.txt$')" ] || { echo "STOP: cannot remove the markers of an earlier attempt"; return 1; }
  [ -f "$ST/C5-passed" ] || { echo "STOP: Phase C has not passed"; return 1; }
  f="$(cd "$REL_NEW" && find . -type f ! -path './venv/*' ! -path './backend/RELEASE' -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-64)"
  n="$(cd "$REL_NEW" && find . -type f ! -path './venv/*' ! -path './backend/RELEASE' | wc -l)"
  [ "$f" = 1ffdf1e5c931d4e67eb9e4be9f1c9a69284a63ec691efec4042c2293e7df1d78 ] && [ "$n" = 335 ] \
    && [ "$(cat "$REL_NEW/backend/RELEASE")" = c2fe883be71d ] && [ -x "$REL_NEW/venv/bin/alembic" ] && [ -x "$REL_NEW/venv/bin/uvicorn" ] \
    || { echo "STOP: $REL_NEW is not the release built in A2"; return 1; }
  [ "$(find "$REL_NEW" ! -user root | wc -l)" = 0 ] && [ "$(find "$REL_NEW" ! -type l -perm /g=w | wc -l)" = 0 ] \
    && [ "$(find "$REL_NEW" ! -type l -perm /o=rwx | wc -l)" = 0 ] && [ "$(find "$REL_NEW/backend" "$REL_NEW/database" -name __pycache__ | wc -l)" = 0 ] \
    || { echo "STOP: $REL_NEW ownership, permissions or bytecode not as built"; return 1; }
  [ "$(readlink -f "$APP_CURRENT")" = "$REL_OLD" ] || { echo "STOP: current is not $REL_OLD"; return 1; }
  head_is 0019 || return 1
  test -s "$ST/phaseB.set" && test -f "$ST/B2-passed" \
    && test -f "/var/backups/hyperchat-dev/$(cat "$ST/phaseB.set").manifest.json" || { echo "STOP: no verified Phase B set"; return 1; }
  witness_ok || return 1
  ! grep -q '^HYPERCHAT_BOTS_ENABLED=true' "$API_ENV" || { echo "STOP: bots enabled"; return 1; }
  api_healthy "$(cat "$ST/old-release.id")" || return 1
  baseline > "$ST/baseline-before.txt" && cat "$ST/baseline-before.txt" || { echo "STOP: baseline"; return 1; }
  { echo "streamers=$(Q 'SELECT count(*) FROM streamers')"; echo "active=$(Q "SELECT count(*) FROM streamers WHERE status = 'active'")"
    echo "signups=$(Q 'SELECT count(*) FROM user_signups')"; } > "$ST/D-expect.txt" \
    && [ "$(grep -cE '^(streamers|active|signups)=[0-9]+$' "$ST/D-expect.txt")" = 3 ] || { echo "STOP: cannot record the expected counts"; return 1; }
  cat "$ST/D-expect.txt"
  date -u +%FT%TZ > "$ST/D1-passed" && echo "D1 PASSED"
}
d1_preflight
```

**D2. Stop DEV services** (reverse of the recovery order in `docs/backup-recovery.md`). It refuses without `D1-passed`, and stops:
- the timer;
- a cleanup run in progress;
- the worker;
- the API.

Then every unit must be `inactive` or `failed`, and no runtime-role session may remain on `hyperchat_dev` (waited for up to 30 s). It must print `D2 OK`:
```
d2_stop() { local i n
  [ -f "$ST/D1-passed" ] || { echo "STOP: D1 has not passed"; return 1; }
  [ ! -e "$ST/D2-passed" ] || { echo "STOP: D2 already passed in this attempt"; return 1; }
  enter_stopped_state || return 1
  for i in $(seq 1 30); do
    n="$(QP "SELECT count(*) FROM pg_stat_activity WHERE datname = 'hyperchat_dev' AND usename IN ('hyperchat_app','hyperchat_worker','hyperchat_lifecycle','hyperchat_auth')")" || { echo "STOP: cannot read pg_stat_activity"; return 1; }
    [ "$n" = 0 ] && break; sleep 1
  done
  [ "$n" = 0 ] || { echo "STOP: $n runtime-role sessions remain on hyperchat_dev"; return 1; }
  date -u +%FT%TZ > "$ST/D2-passed" && echo "D2 OK: DEV stopped, no runtime sessions"
}
d2_stop
```

**D3. Migrate one revision at a time. Fail-stop.** Define the step functions in this session:
- **`migrate_one`:** runs one Alembic revision and checks the head.
- **`d3_check`:** the revision's expected result, checked by machine against the counts D1 recorded.
- **`migrate_checked`:** refuses unless D2 passed and the previous revision's marker exists, then runs both and writes `D3-<rev>-ok`.
```
migrate_one() {   # $1 = the revision; succeeds only when alembic succeeded AND the database is exactly at $1
  services_stopped || return 1
  ( cd "$REL_NEW/database" && HYPERCHAT_MIGRATION_DSN="postgresql+psycopg://hyperchat_migrator@/hyperchat_dev?host=/var/run/postgresql&port=5433" \
      ../venv/bin/alembic upgrade "$1" ) || { echo "STOP: alembic upgrade $1 failed"; return 1; }
  head_is "$1" || return 1
  echo "OK $1"
}
d3_check() { local streamers active signups got   # $1 = the revision just applied
  streamers="$(sed -n 's/^streamers=//p' "$ST/D-expect.txt")"; active="$(sed -n 's/^active=//p' "$ST/D-expect.txt")"; signups="$(sed -n 's/^signups=//p' "$ST/D-expect.txt")"
  [ -n "$streamers" ] && [ -n "$active" ] && [ -n "$signups" ] || { echo "STOP: no expected counts from D1"; return 1; }
  case "$1" in
    0020) got="$(Q "SELECT kyc_track || '|' || count(*) || '|' || count(*) FILTER (WHERE kyc_baseline_owner_id IS NOT DISTINCT FROM user_id) FROM streamers GROUP BY kyc_track")"
          [ "$got" = "legacy|$streamers|$streamers" ] || { echo "STOP: 0020 creators: '$got', expected 'legacy|$streamers|$streamers'"; return 1; }
          got="$(Q "SELECT count(*) FROM streamers WHERE public.creator_donation_block(id) IS NULL")"
          printf '%s\n' "$got" > "$ST/eligible-after-0020.txt"
          [ "$got" = "$active" ] || { echo "STOP: 0020 eligible creators: $got, expected $active (the active ones)"; return 1; } ;;
    0021) got="$(Q "SELECT review_status || '|' || count(*) FROM user_signups GROUP BY review_status")"
          [ "$got" = "legacy_unreviewed|$signups" ] || { echo "STOP: 0021 signups: '$got', expected 'legacy_unreviewed|$signups'"; return 1; } ;;
    0023) got="$(Q "SELECT state || '|' || generation FROM kyc_enforcement")"
          [ "$got" = "draft|0" ] || { echo "STOP: 0023 KYC enforcement: '$got', expected 'draft|0'"; return 1; } ;;
    0025) got="$(Q "SELECT state FROM stepup_enforcement")"
          [ "$got" = draft ] || { echo "STOP: 0025 MFA enforcement: '$got', expected 'draft'"; return 1; } ;;
    0022|0024|0026|0027|0028) ;;   # the head check in migrate_one is the check
    *) echo "STOP: unknown revision $1"; return 1 ;;
  esac
}
migrate_checked() { local prev=0019 r
  [ -f "$ST/D2-passed" ] || { echo "STOP: D2 has not passed"; return 1; }
  for r in 0020 0021 0022 0023 0024 0025 0026 0027 0028; do [ "$r" = "$1" ] && break; prev="$r"; done
  [ "$r" = "$1" ] || { echo "STOP: $1 is not one of 0020-0028"; return 1; }
  [ "$prev" = 0019 ] || [ -f "$ST/D3-$prev-ok" ] || { echo "STOP: $prev has not passed its check"; return 1; }
  [ ! -e "$ST/D3-$1-ok" ] || { echo "STOP: $1 already applied in this attempt"; return 1; }
  migrate_one "$1" && d3_check "$1" || return 1
  date -u +%FT%TZ > "$ST/D3-$1-ok" && echo "D3 $1 OK"
}
```
Then run **one line at a time**, and run the next line **only after the previous one printed `D3 <rev> OK`**. There is deliberately no loop.
- `migrate_checked 0020`
- `migrate_checked 0021`
- `migrate_checked 0022`
- `migrate_checked 0023`
- `migrate_checked 0024`
- `migrate_checked 0025`
- `migrate_checked 0026`
- `migrate_checked 0027`
- `migrate_checked 0028`

What `d3_check` requires (2026-10-24 values; D1 records the counts at the time):

| Revision | Check | Expect |
|---|---|---|
| 0020 | creators by `kyc_track`, and those whose baseline owner is their owner | `legacy\|<streamers>\|<streamers>` (133) |
| 0020 | creators eligible for donations (`creator_donation_block(id) IS NULL`, kept in `$ST/eligible-after-0020.txt`) | the active creators (133) |
| 0021 | signups by `review_status` | `legacy_unreviewed\|<signups>` (233) |
| 0023 | `kyc_enforcement` | `draft\|0` |
| 0025 | `stepup_enforcement` | `draft` |
| 0022, 0024, 0026, 0027, 0028 | the head check in `migrate_one` | the revision itself |

**D3 gate.** It removes any marker first and writes a new one **last**, only when all nine revisions passed their checks in this attempt and the state is right:
```
d3_gate() { local r
  rm -f "$ST/D3-passed"
  for r in 0020 0021 0022 0023 0024 0025 0026 0027 0028; do
    [ -f "$ST/D3-$r-ok" ] || { echo "STOP: $r has not passed its check; do not switch the release"; return 1; }
  done
  services_stopped && head_is 0028 && drafts_ok && witness_ok || { echo "STOP: D3 gate failed; do not switch the release"; return 1; }
  date -u +%FT%TZ > "$ST/D3-passed" && echo "D3 PASSED"
}
d3_gate
```

**D4. Switch the release and start the services.** The marker is single-use: the function removes it before doing anything, so a stale or reused marker can never authorize a switch.
- **Re-validation:** it checks the state itself (services stopped, head 0028, KYC `draft|0`, MFA `draft`, witness empty and append-only), then switches.
- **Start order:** it starts the API, and starts the worker and the cleanup timer **only after `/health` reports `ok` on release `c2fe883be71d`**.
- **Restart watch:** for `D4_SETTLE` seconds it checks that the API and worker don't restart (same main PID, same restart count, still `active`), then checks `/health` again.
- **On any failure:** `d4_switch` returns DEV to the stopped state AND returns non-zero.
```
switch_to_new() { local s api_pid wrk_pid api_n wrk_n
  [ -f "$ST/D3-passed" ] || { echo "STOP: no D3 marker from this attempt; run d3_gate"; return 1; }
  rm -f "$ST/D3-passed" || { echo "STOP: cannot consume the D3 marker"; return 1; }
  services_stopped && head_is 0028 && drafts_ok && witness_ok || { echo "STOP: state is not what the gate checked; not switching"; return 1; }
  ln -sfn "$REL_NEW" "$APP_CURRENT" && [ "$(readlink -f "$APP_CURRENT")" = "$REL_NEW" ] || { echo "STOP: release link not switched"; return 1; }
  systemctl start hyperchat-api || { echo "STOP: hyperchat-api did not start"; return 1; }
  api_healthy c2fe883be71d || return 1
  systemctl start hyperchat-worker || { echo "STOP: hyperchat-worker did not start"; return 1; }
  systemctl start hyperchat-cleanup.timer || { echo "STOP: hyperchat-cleanup.timer did not start"; return 1; }
  api_pid="$(systemctl show -p MainPID --value hyperchat-api)"; wrk_pid="$(systemctl show -p MainPID --value hyperchat-worker)"
  api_n="$(systemctl show -p NRestarts --value hyperchat-api)"; wrk_n="$(systemctl show -p NRestarts --value hyperchat-worker)"
  sleep "$D4_SETTLE"
  for s in hyperchat-api hyperchat-worker hyperchat-cleanup.timer; do
    [ "$(systemctl is-active "$s")" = active ] || { echo "STOP: $s is not active after ${D4_SETTLE}s"; return 1; }
  done
  [ "$api_pid" -gt 0 ] 2>/dev/null && [ "$wrk_pid" -gt 0 ] 2>/dev/null \
    && [ "$(systemctl show -p MainPID --value hyperchat-api)" = "$api_pid" ] && [ "$(systemctl show -p MainPID --value hyperchat-worker)" = "$wrk_pid" ] \
    && [ "$(systemctl show -p NRestarts --value hyperchat-api)" = "$api_n" ] && [ "$(systemctl show -p NRestarts --value hyperchat-worker)" = "$wrk_n" ] \
    || { echo "STOP: the API or the worker restarted within ${D4_SETTLE}s"; return 1; }
  api_healthy c2fe883be71d || return 1
  date -u +%FT%TZ > "$ST/D4-passed" && echo "D4 DONE: DEV runs release c2fe883be71d"
}
d4_switch() {  # non-zero on ANY failure; after a failure DEV is stopped again (or enter_stopped_state says it is not)
  switch_to_new && return 0
  echo "STOP: D4 failed; returning DEV to the stopped state"
  enter_stopped_state
  return 1
}
d4_switch
```
- **After a D4 failure:** `d4_switch` exits non-zero. DEV is stopped again (`DEV STOPPED`), or `enter_stopped_state` names the unit that didn't stop. The release link stays wherever the failure left it; switching back is Phase F. A retry is a new attempt from D1, which refuses to start because the database is no longer at 0019. So after a D4 failure the owner decides (Phase F or a written fix-forward plan).
- **Then (read-only):**
  ```
  journalctl -u hyperchat-api -u hyperchat-worker --since "-5 min" --no-pager | grep -iE "error|critical|refus" | head
  ```
- **The cleanup service:** don't start it by hand. Its timer runs it daily with the same `api.env`, witness path included.

**Expected window (2026-10-24 preflight values):**
- **D2:** a few seconds. The API stops within at most 20 s and the worker within 1 min (their stop timeouts).
- **D3:** nine revisions on a 32 MB database: seconds each, about 2–5 minutes with the operator checks between lines.
- **D4:** the API start and `/health` (up to 2 minutes), then 30 s of restart watching.
- **Total:** about 5–10 minutes of DEV downtime.
- **Timing:** schedule it away from the cleanup timer's daily run (next 2026-10-25 03:16 UTC); D2 stops the timer anyway.
- **On a STOP:** DEV stays down until the owner chooses Phase F or a fix-forward plan.

### Stop conditions
Any of these is a Phase D failure:
- Any line prints `STOP`, or exits non-zero, or a check differs from "Expect".
- A revision exceeds its lock or statement timeout (it rolls back by itself; `alembic_version` shows the previous one).
- `/health` isn't `ok` within 2 minutes of the API start, or the API or worker restarts in a loop.
- The KYC or MFA enforcement state reads anything but `draft`, or the witness is non-empty or lost its `a` flag.
- Any log line names production (`hyperchat-prod`, `api.hyperchat.space`, port 5432).

On any of them, go to "If a phase fails" at once. **Never continue to the next revision, the gate or the release switch after a STOP.**

---

## If a phase fails (stopped state and escalation)
Phase D approval covers exactly one recovery action: **bringing DEV to a safe stopped state**. It never covers a restore, a downgrade, a re-run, a configuration change or a release switch back. Those need the owner's separate approval (Phase F, or a written fix-forward plan).

**Enter the stopped state** (must print `DEV STOPPED`; if a unit refuses to stop, record it and escalate):
```
rm -f "$ST/D3-passed"; enter_stopped_state
```
**Record:**
```
{ date -u +%FT%TZ; readlink -f /opt/hyperchat/current; Q "SELECT version_num FROM alembic_version";
  stat -c '%s bytes' /var/lib/hyperchat-kyc/dev.witness; lsattr /var/lib/hyperchat-kyc/dev.witness;
  journalctl -u hyperchat-api -u hyperchat-worker --since "-30 min" --no-pager | tail -n 50; } > "$ST/failure-record.txt" 2>&1
```
- **Then hold:**
  - Leave the services stopped, the database as it is, the release link as it is, and the witness untouched.
  - Don't downgrade, don't re-run a migration, don't switch the release.
- **What stays affected:** DEV stays offline. Nothing else is affected; production keeps running on its own cluster and units.
- **Escalate to the owner** with `$ST/failure-record.txt`: the failing step, its output, the database revision, the release link, and the witness size and flags.
- **The owner chooses** one of:
  - **Approve Phase F** (restore and switch back, or the release-only path when the database is still at 0019).
  - **Approve a written fix-forward plan** (a separate document and approval).
  - **Keep DEV stopped** while the failure is investigated.

The same applies to a failure in Phases B, C or E: stop what that phase started, record, escalate, and wait for the owner.

---

## Phase E: Post-migration smoke tests (approval: "Phase E")
### Prerequisite: smoke suite updated in a separate test-only PR
At `c2fe883`, `tests/e2e/smoke_dev_backend.py` knew migration heads only up to `0024`, had no MFA / step-up section, and created and removed its synthetic admin as `hyperchat_app`, which 0024 refuses. **A failing head check is never accepted as part of a successful rehearsal.** The test-only PR "DEV smoke-test readiness" (#82) does the following; it must be reviewed and merged before Phase E:
- **Heads:** `KNOWN_HEADS` runs through `0028` (`tests/e2e/smoke_policy.py`).
- **Rehearsal mode:** `HYPERCHAT_SMOKE_EXPECT_HEAD=0028` requires exactly that head, and turns every BLOCKED that is not an approved external integration (e.g. "not deployed on DEV yet") into a FAIL.
- **An `mfa` section:**
  - KYC enforcement `draft` / generation 0 and MFA enforcement `draft`, before and after the section;
  - anonymous callers get 401, and a signed-in non-admin gets 403, on all six passkey endpoints;
  - the admin's `GET /api/v1/auth/mfa/status` answers 200 `{"enforcement": "draft", "aal": <2, "mfa_required": false, "verifier": "not_configured", "credentials": null}`;
  - `POST …/registration/options` and `POST …/assertion/options`, and `GET …/credentials`, answer exactly `503` with reason `stepup_not_configured`;
  - registration without the CSRF token, or from a foreign Origin, gets 403;
  - admin routes still answer;
  - admin sessions end within 12 h;
  - passkey, challenge and grant counts are unchanged (nothing is enrolled).
- **Admin fixture:** the synthetic admin is created and removed through the schema owner.
- **Cleanup:** it runs statement by statement and reports what is left.
- **Scope:** only files under `tests/` and `backend/tests/` change, so the deployed `c2fe883` code is unaffected.

Phase E then uses a root-only copy of that PR's merge commit: **#82, merged as `f8bd98b3bf0070a3fcfe24cdefb5a6b26c69eb13`** (`SMOKE_SHA` in the session setup). It descends from `c2fe883`, and compared with it changes only tests (`tests/e2e/*`, `backend/tests/test_smoke_harness.py`) and documentation. Its `backend tests` tree is pinned by `SMOKE_FP` (295 files). The fingerprint is over the LF content that `git archive` emits from `cmd.exe` or Linux git; a Git-Bash `git archive` with `core.autocrlf=true` emits CRLF and a different fingerprint. The copy is verified on the server, so a copy of anything else is a STOP.

**How to run it:**
- **One root shell per VPS step:** the session setup, then the step's block exactly as written.
- **Step gates:** each step writes a marker in `$ST` only when everything in it succeeded, and returns non-zero with `STOP` otherwise. The next step refuses to start without it: D4 → E0 → `E0-passed` → (the E1 stop and its resume: R1 → `E-resume-checked` → R2 → `E0b-passed`) → E1 → `E1-passed` → E2 checks → `E2-checks-passed` → E2 restore → `E-restore-passed` → E3 → `E3-passed`.

**E0. The smoke copy.**

**(workstation)**, a read-only check that must print nothing (no application, migration or deployment change):
```
git diff --stat c2fe883be71d1a494232d4147ed4d6fbc4e70eef f8bd98b3bf0070a3fcfe24cdefb5a6b26c69eb13 -- backend database deploy ':(exclude)backend/tests' ':(exclude)*.md'
```
**(workstation)**, the copy. It runs only after D4 passed and never into an existing directory, and its exit status is the remote command's:
```
git archive f8bd98b3bf0070a3fcfe24cdefb5a6b26c69eb13 backend tests \
  | ssh root@VPS "[ -f /root/rehearsal-dev/D4-passed ] && [ ! -e /root/hyperchat-smoke ] && install -d -o root -g root -m 0700 /root/hyperchat-smoke && tar -x -C /root/hyperchat-smoke"
```
**(VPS)**: must print `E0 OK`.
```
e0_smoke_ok() {
  [ -f "$ST/D4-passed" ] || { echo "STOP: Phase D has not passed"; return 1; }
  [ ! -e "$ST/E0-passed" ] || { echo "STOP: E0 already passed once"; return 1; }
  [ -d "$SMOKE1" ] && [ ! -L "$SMOKE1" ] && [ "$(stat -c '%U:%G %a' "$SMOKE1")" = "root:root 700" ] || { echo "STOP: $SMOKE1 is not a root-only directory"; return 1; }
  [ "$(fp "$SMOKE1")" = "$SMOKE1_FP" ] && [ "$(find "$SMOKE1" -type f | wc -l)" = 295 ] || { echo "STOP: $SMOKE1 is not the $SMOKE1_SHA backend+tests tree"; return 1; }
  date -u +%FT%TZ > "$ST/E0-passed" && echo "E0 OK: $SMOKE1 is the pinned smoke tree"
}
e0_smoke_ok
```
**Record:** the empty `git diff --stat` output and the E0 output.

### What must pass
- **Mandatory: PASS.** Every runnable check of both suites, including the migration-head check, plus every E2 and E3 row below.
- **Allowed: BLOCKED.** Only the third-party integrations or approvals that are deliberately unavailable on DEV. In rehearsal mode the suite itself turns any other BLOCKED into a FAIL:
  - email delivery (Resend);
  - Google sign-in;
  - TTS and transcription providers (MiniMax, ElevenLabs, Sarvam);
  - the Didit session start (no sandbox credentials);
  - the live Razorpay probe (D7), Razorpay checkout / order creation, and the PAY-7 status poll / confirm with a real test payment;
  - R2-writing sections (`HYPERCHAT_SMOKE_R2=1` not approved);
  - bots (disabled);
  - the hosted Supabase rehearsal (D1).
- **Fails Phase E:** any FAIL, or a BLOCKED outside that list.

### Resuming Phase E after the E1 stop (2026-10-24)
**What happened:**
- **E0** passed: `E0-passed`, and `$SMOKE1` holds the `f8bd98b` copy.
- **E1** stopped. `smoke_dev_backend.py` exited 1 at its setup: its fixture inserted a creator-linked account as `hyperchat_app`, and migration 0027's `stepup_guard_users` refuses that (`InsufficientPrivilege: an admin or creator-linked account is created only by an invitation`).
- **The suite's cleanup** reported nothing left, and `smoke_auth.py` didn't run.
- **The guard was right.** PR #94 (test-only, merged as `ae60aa5b144517384ea6b9fbb50f6654eadd07b4`) makes the suite write that account through the schema owner, like its admins and creators. Its `backend tests` tree is pinned by `SMOKE_FP` (296 files, LF content as the `cmd.exe` or Linux `git archive` emits it).

**Resuming needs the owner's explicit approval of this procedure.** It never repeats the first transfer, never overwrites `$SMOKE1`, and never reuses a marker: E1 requires all three of `E0-passed`, `E-resume-checked` and `E0b-passed`.

**R1. Check the stopped state** (VPS; read-only apart from moving the first attempt's log aside and its marker). It must print `RESUME CHECK OK`. It requires:
- **The exact E1 failure:** `E0-passed` exists; none of the E1–E3 markers exist; the first attempt's log shows exactly that failure (1 failed); `smoke_auth.py` never ran.
- **No synthetic rows:** no users, creators, signups or invitations left.
- **DEV as after Phase D:** on `c2fe883be71d` at 0028 and healthy; KYC and MFA `draft`; the witness intact; the baseline identical; eligibility unchanged; the isolation audit all `OK`.
- **Production unchanged:** on `940137070657` at 0009.
- **The two copies:** the first copy still matches `SMOKE1_FP`, and `$SMOKE` doesn't exist yet.
```
e_resume_check() { local out rc
  [ -f "$ST/E0-passed" ] || { echo "STOP: E0 has not passed"; return 1; }
  [ ! -e "$ST/E-resume-checked" ] || { echo "STOP: the resume check already passed once"; return 1; }
  [ -z "$(ls "$ST" | grep -E '^E1-passed$|^E2-checks-passed$|^E-restore-passed$|^E3-passed$|^phaseE\.set$')" ] \
    || { echo "STOP: a later Phase E marker exists; this is not the stopped E1 state"; return 1; }
  [ -f "$ST/smoke-dev-backend.txt" ] && [ ! -e "$ST/smoke-auth.txt" ] \
    && grep -qF 'FAILED [suite] runs to completion: InsufficientPrivilege: an admin or creator-linked account is created only by an invitation' "$ST/smoke-dev-backend.txt" \
    && grep -qE '^[0-9]+ passed, 1 failed, [0-9]+ blocked' "$ST/smoke-dev-backend.txt" && grep -qF 'PASS  [cleanup] no synthetic row left' "$ST/smoke-dev-backend.txt" \
    || { echo "STOP: the first E1 attempt's log is not the recorded 0027 fixture failure"; return 1; }
  [ "$(Q "SELECT (SELECT count(*) FROM users WHERE email LIKE '%@authtest.invalid') + (SELECT count(*) FROM streamers WHERE streamer_slug LIKE 'authtest-%') + (SELECT count(*) FROM user_signups WHERE lower(email) LIKE '%@authtest.invalid') + (SELECT count(*) FROM user_invites WHERE lower(email) LIKE '%@authtest.invalid')")" = 0 ] \
    || { echo "STOP: synthetic rows remain"; return 1; }
  [ "$(readlink -f "$APP_CURRENT")" = "$REL_NEW" ] && head_is 0028 && api_healthy c2fe883be71d && drafts_ok && witness_ok || return 1
  baseline > "$ST/baseline-resume.txt" && diff "$ST/baseline-before.txt" "$ST/baseline-resume.txt" || { echo "STOP: the data baseline changed"; return 1; }
  [ "$(Q "SELECT count(*) FROM streamers WHERE public.creator_donation_block(id) IS NULL")" = "$(cat "$ST/eligible-after-0020.txt")" ] || { echo "STOP: eligibility changed"; return 1; }
  out="$(bash "$STAGE/deploy/check_isolation.sh" dev 2>&1)"; rc=$?
  [ "$rc" = 0 ] && ! printf '%s\n' "$out" | grep -q '^FAIL' || { echo "STOP: the isolation audit is not all OK"; return 1; }
  curl -fsS --max-time 5 http://127.0.0.1:8001/health | python3 -c 'import json, sys; d = json.load(sys.stdin); sys.exit(0 if d.get("status") == "ok" and d.get("release") == "940137070657" else 1)' \
    && [ "$(sudo -u postgres env PGOPTIONS='-c default_transaction_read_only=on' psql -X -q -At -p 5432 -d hyperchat -c 'SELECT version_num FROM alembic_version')" = 0009 ] \
    || { echo "STOP: production is not on 940137070657 at 0009"; return 1; }
  [ "$(fp "$SMOKE1")" = "$SMOKE1_FP" ] || { echo "STOP: the first copy $SMOKE1 changed"; return 1; }
  [ ! -e "$SMOKE" ] || { echo "STOP: $SMOKE already exists"; return 1; }
  mv -n "$ST/smoke-dev-backend.txt" "$ST/smoke-dev-backend.attempt1.txt" && [ ! -e "$ST/smoke-dev-backend.txt" ] || { echo "STOP: cannot keep the first attempt's log"; return 1; }
  date -u +%FT%TZ > "$ST/E-resume-checked" && echo "RESUME CHECK OK: stopped E1 state verified; first log kept as smoke-dev-backend.attempt1.txt"
}
e_resume_check
```

**R2. Copy the corrected suite to a new path.**

**(workstation)**, a read-only check that must print nothing. Application and migration code must be identical to `c2fe883`; `deploy/` changed after it (the backup script, #88) but isn't part of the copy:
```
git diff --stat c2fe883be71d1a494232d4147ed4d6fbc4e70eef ae60aa5b144517384ea6b9fbb50f6654eadd07b4 -- backend database ':(exclude)backend/tests' ':(exclude)*.md'
```
**(workstation)**, through `cmd.exe`. It runs only after the resume check, and never into an existing directory:
```
git archive ae60aa5b144517384ea6b9fbb50f6654eadd07b4 backend tests \
  | ssh root@VPS "[ -f /root/rehearsal-dev/E-resume-checked ] && [ ! -e /root/hyperchat-smoke-2 ] && install -d -o root -g root -m 0700 /root/hyperchat-smoke-2 && tar -x -C /root/hyperchat-smoke-2"
```
**(VPS)**: must print `E0b OK`.
```
e0b_smoke_ok() {
  [ -f "$ST/E-resume-checked" ] || { echo "STOP: the resume check has not passed"; return 1; }
  [ ! -e "$ST/E0b-passed" ] || { echo "STOP: E0b already passed once"; return 1; }
  [ -d "$SMOKE" ] && [ ! -L "$SMOKE" ] && [ "$(stat -c '%U:%G %a' "$SMOKE")" = "root:root 700" ] || { echo "STOP: $SMOKE is not a root-only directory"; return 1; }
  [ "$(fp "$SMOKE")" = "$SMOKE_FP" ] && [ "$(find "$SMOKE" -type f | wc -l)" = 296 ] || { echo "STOP: $SMOKE is not the $SMOKE_SHA backend+tests tree"; return 1; }
  [ "$(fp "$SMOKE1")" = "$SMOKE1_FP" ] || { echo "STOP: the first copy $SMOKE1 changed"; return 1; }
  date -u +%FT%TZ > "$ST/E0b-passed" && echo "E0b OK: $SMOKE is the pinned corrected smoke tree"
}
e0b_smoke_ok
```
**Then:** E1, the E2 checks, the E2 restore and E3 below, unchanged, each in its own root shell. E1 runs the suites from `$SMOKE`, the corrected copy.

**Record:** the R1 and R2 outputs, `$ST/smoke-dev-backend.attempt1.txt`, `$ST/baseline-resume.txt`, and the markers `E-resume-checked` and `E0b-passed`.

### E1. The smoke suites
Both suites run as root from the smoke copy with the release's venv, against DEV only. They refuse the production API and database and the restore source, and they require environment `development`.
- **What they write:** synthetic rows only (`@authtest.invalid`, `authtest-*`), removed and verified at the end. The login rate-limit rows they consume are cleared. Approving a synthetic donation makes the DEV worker publish realtime events to the DEV Pusher app; nothing else leaves the server.
- **No bytecode:** they run with `PYTHONDONTWRITEBYTECODE=1`, so nothing is written into the release's venv or into the smoke copy, which E1 checks afterwards.
- **Pass:** each suite's own exit code must be 0. In rehearsal mode, `smoke_dev_backend.py` exits non-zero for any BLOCKED outside the allowed list above.
```
e1_smoke() { local rc venv_pyc
  [ -f "$ST/E0-passed" ] && [ -f "$ST/E-resume-checked" ] && [ -f "$ST/E0b-passed" ] \
    || { echo "STOP: E0, the resume check and E0b must have passed (see 'Resuming Phase E after the E1 stop')"; return 1; }
  [ ! -e "$ST/E1-passed" ] || { echo "STOP: E1 already passed once"; return 1; }
  [ "$(readlink -f "$APP_CURRENT")" = "$REL_NEW" ] || { echo "STOP: current is not $REL_NEW"; return 1; }
  head_is 0028 && api_healthy c2fe883be71d || return 1
  venv_pyc="$(find "$REL_NEW/venv" -name '*.pyc' | wc -l)"
  ( cd "$SMOKE" && PYTHONDONTWRITEBYTECODE=1 HYPERCHAT_SMOKE_EXPECT_HEAD=0028 "$APP_CURRENT/venv/bin/python" tests/e2e/smoke_dev_backend.py ) \
    > "$ST/smoke-dev-backend.txt" 2>&1; rc=$?
  tail -n 25 "$ST/smoke-dev-backend.txt"
  [ "$rc" = 0 ] || { echo "STOP: smoke_dev_backend.py exited $rc (see $ST/smoke-dev-backend.txt)"; return 1; }
  ( cd "$SMOKE" && PYTHONDONTWRITEBYTECODE=1 HYPERCHAT_SMOKE_SAMESITE=none "$APP_CURRENT/venv/bin/python" tests/e2e/smoke_auth.py ) \
    > "$ST/smoke-auth.txt" 2>&1; rc=$?
  tail -n 10 "$ST/smoke-auth.txt"
  [ "$rc" = 0 ] || { echo "STOP: smoke_auth.py exited $rc (see $ST/smoke-auth.txt)"; return 1; }
  [ "$(find "$REL_NEW/venv" -name '*.pyc' | wc -l)" = "$venv_pyc" ] && [ "$(find "$REL_NEW/backend" "$REL_NEW/database" -name __pycache__ | wc -l)" = 0 ] \
    && [ "$(fp "$SMOKE")" = "$SMOKE_FP" ] || { echo "STOP: the suites wrote into the release or the smoke copy"; return 1; }
  date -u +%FT%TZ > "$ST/E1-passed" && echo "E1 OK: both suites passed"
}
e1_smoke
```

### E2. Database checks (read-only; after the suites cleaned their synthetic rows)
`e2_checks` checks every row of the table below by machine and writes `E2-checks-passed` only when all hold:
```
e2_checks() { local signups out rc
  [ -f "$ST/E1-passed" ] || { echo "STOP: E1 has not passed"; return 1; }
  baseline > "$ST/baseline-after.txt" || { echo "STOP: baseline"; return 1; }
  diff "$ST/baseline-before.txt" "$ST/baseline-after.txt" || { echo "STOP: the data baseline changed (diff above)"; return 1; }
  signups="$(sed -n 's/^signups=//p' "$ST/D-expect.txt")"
  [ -n "$signups" ] && [ "$(Q "SELECT review_status || '|' || count(*) FROM user_signups GROUP BY review_status")" = "legacy_unreviewed|$signups" ] \
    || { echo "STOP: signups are not all legacy_unreviewed"; return 1; }
  [ "$(Q "SELECT count(*) FROM streamers WHERE public.creator_donation_block(id) IS NULL")" = "$(cat "$ST/eligible-after-0020.txt")" ] \
    || { echo "STOP: eligible creators differ from after 0020"; return 1; }
  drafts_ok && witness_ok || return 1
  out="$(bash "$STAGE/deploy/check_isolation.sh" dev 2>&1)"; rc=$?
  printf '%s\n' "$out" > "$ST/isolation-E2-dev.txt"
  [ "$rc" = 0 ] && ! printf '%s\n' "$out" | grep -q '^FAIL' || { echo "STOP: the isolation audit is not all OK (exit $rc)"; return 1; }
  date -u +%FT%TZ > "$ST/E2-checks-passed" && echo "E2 CHECKS OK: baseline unchanged"
}
e2_checks
```
| Area | Check | Expect |
|---|---|---|
| Creators, signups, donations, users, tokens, acceptances | the `baseline` diff above | `baseline-unchanged`: 133 creators, 0 unowned, md5s, counts and sums identical |
| Legacy signups | `Q "SELECT review_status, count(*) FROM user_signups GROUP BY 1"` | only `legacy_unreviewed` for the baseline rows |
| Ownership and eligibility | `Q "SELECT count(*) FROM streamers WHERE public.creator_donation_block(id) IS NULL"` | equals `$ST/eligible-after-0020.txt` |
| KYC enforcement | `Q "SELECT state, generation FROM kyc_enforcement"` | `draft\|0` |
| MFA enforcement | `Q "SELECT state FROM stepup_enforcement"` | `draft` |
| Witness | `stat -c %s /var/lib/hyperchat-kyc/dev.witness; lsattr /var/lib/hyperchat-kyc/dev.witness` | `0`, `a` flag |
| Process isolation | `bash "$STAGE/deploy/check_isolation.sh" dev` | only `OK`, exit 0 |

**Privilege isolation**, verified on a restored copy, never by probing the live database. `e2_restore` runs the following, and any failure returns non-zero without a marker:
- **Backup:** a new backup set, whose manifest must record alembic `0028`, KYC `draft`/0, MFA `draft` and a copied witness, and whose dump must match its checksum.
- **Before the restore:** the pinned staging `roles.sql`, and no scratch directory or scratch postmaster (a `postgres`-owned `…/bin/postgres -D /tmp/hc-restorecheck-…`) left over.
- **Restore check:** the release's `restore_check`, which must exit 0.
- **After it:** the scratch directory gone and no scratch postmaster left.
- **JSON:** every required field checked explicitly.
- **Marker:** `E-restore-passed`, written last.
```
e2_restore() { local rc set
  [ -f "$ST/E2-checks-passed" ] || { echo "STOP: the E2 checks have not passed"; return 1; }
  rm -f "$ST/phaseE.set" "$ST/E-restore-passed"
  dev_backup phaseE.set || { rm -f "$ST/phaseE.set"; echo "STOP: backup failed"; return 1; }
  set="$(cat "$ST/phaseE.set")"
  ( cd /var/backups/hyperchat-dev && sha256sum -c --quiet "$set.dump.sha256" ) \
    && python3 -I - "/var/backups/hyperchat-dev/$set.manifest.json" <<'PY' || { echo "STOP: the Phase E set's manifest is not as required"; return 1; }
import json, sys
m = json.load(open(sys.argv[1]))
ok = (m.get("alembic_version") == "0028" and m.get("kyc_enforcement") == {"state": "draft", "generation": 0}
      and m.get("stepup_enforcement") == "draft" and m.get("witness_status") == "copied")
print("manifest:", m.get("alembic_version"), m.get("kyc_enforcement"), m.get("stepup_enforcement"), m.get("witness_status"))
sys.exit(0 if ok else 1)
PY
  roles_sql_ok || return 1
  # a scratch cluster's postmaster runs as the scratch user with its data directory under /tmp/hc-restorecheck-*
  # (pg_ctl starts '<bin>/postgres -D <data> ...'); matching only that never matches a shell or an unrelated command
  [ -z "$(ls -d /tmp/hc-restorecheck-* 2>/dev/null)" ] && ! pgrep -u postgres -f '/bin/postgres -D /tmp/hc-restorecheck-' >/dev/null \
    || { echo "STOP: a scratch directory or cluster is left from an earlier run"; return 1; }
  ( cd "$REL_NEW/backend" && ../venv/bin/python -m app.backup.restore_check --env dev --dir /var/backups/hyperchat-dev \
      --set "$set" --pg-bin /usr/lib/postgresql/15/bin --scratch --scratch-user postgres \
      --roles-sql "$ROLES_SQL" --expect-head 0028 --output "$ST/restorecheck-0028.json" ); rc=$?
  [ "$rc" = 0 ] || { echo "STOP: restore check failed (restore_check exited $rc; see $ST/restorecheck-0028.json)"; return 1; }
  [ -z "$(ls -d /tmp/hc-restorecheck-* 2>/dev/null)" ] && ! pgrep -u postgres -f '/bin/postgres -D /tmp/hc-restorecheck-' >/dev/null \
    || { echo "STOP: the scratch cluster was not removed (do not delete it by hand)"; return 1; }
  python3 -I - "$ST/restorecheck-0028.json" "$set" <<'PY' || { echo "STOP: the restore check result is not as required"; return 1; }
import json, sys
d = json.load(open(sys.argv[1])); inv = d.get("invariants") or {}
checks = {"set is the Phase E set": d.get("set") == sys.argv[2],
          "structurally_valid: true": d.get("structurally_valid") is True,
          "restore_valid: true": d.get("restore_valid") is True,
          "invariants.alembic_version: 0028": inv.get("alembic_version") == "0028",
          "alembic matches the head and the manifest": inv.get("alembic_matches_expected_head") is True and inv.get("alembic_matches_manifest") is True,
          "objects_not_owned_by_hyperchat_owner: 0": inv.get("objects_not_owned_by_hyperchat_owner") == 0,
          "public_table_privileges: 0": inv.get("public_table_privileges") == 0,
          "guard triggers: enabled > 0, disabled 0": (inv.get("guard_triggers_enabled") or 0) > 0 and inv.get("guard_triggers_disabled") == 0,
          "runtime_role_privilege_leaks: []": inv.get("runtime_role_privilege_leaks") == [],
          "stepup_enforcement: draft": inv.get("stepup_enforcement") == "draft",
          "kyc_enforcement: draft/0": inv.get("kyc_enforcement") == {"state": "draft", "generation": 0},
          "kyc_witness: consistent": d.get("kyc_witness") == "consistent",
          "live_operation_approved: false": d.get("live_operation_approved") is False}
for name, ok in checks.items():
    print(("OK    " if ok else "FAIL  ") + name)
sys.exit(0 if all(checks.values()) else 1)
PY
  date -u +%FT%TZ > "$ST/E-restore-passed" && echo "E2 RESTORE OK"
}
e2_restore
```
**Required:** `E2 RESTORE OK`, and every manifest and JSON line printed `OK`.

This also proves the 0028 set restores on PostgreSQL 15.

### E3. API checks (covered by the updated suites; listed so the record can tick each)
| Area | Expect |
|---|---|
| Admin authentication through `hyperchat_auth` | `smoke_dev_backend.py` section `admin`: the synthetic admin signs in and uses the admin routes (only possible through `hyperchat_auth` once 0024 is in place). `smoke_auth.py` covers ordinary sign-in, sign-out and session handling |
| Admin session limits | section `mfa`: admin sessions end within 12 h |
| Step-up fails safe | section `mfa`: status 200 with `verifier: "not_configured"` and no credentials; ceremonies `503` `stepup_not_configured`; anonymous 401, non-admin 403, missing CSRF or foreign Origin 403; admin routes answer; nothing enrolled; enforcement `draft` |
| Applications and lifecycle | sections `applications` and `creator-lifecycle` pass (no longer BLOCKED) |
| KYC | section `kyc`: the owner's KYC view, unsigned webhook refused, enforcement view and witness agree; section `mfa`: enforcement `draft`/0. The Didit session start is an allowed BLOCKED (no sandbox credentials) |
| Payments and webhooks | sections `payments` (checkout validation; a Razorpay webhook with a bad signature is refused), `payments-account` (D6), `payment-health` (D7 route and CLI; the live probe may be BLOCKED). No order is created. Webhook duplicate and replay handling is covered by CI (`backend/tests/payments`), not by this smoke |
| OBS, realtime, worker | sections `obs`, `realtime`, `worker` pass; the worker log shows the synthetic job processed |
| DEV/production separation | `curl -fsS http://127.0.0.1:8000/health` reports development and `hyperchat_dev`; read-only: `curl -fsS http://127.0.0.1:8001/health` still reports production release `940137070657`, and `sudo -u postgres psql -X -p 5432 -d hyperchat -Atc "SELECT version_num FROM alembic_version"` still `0009` |

The table's API rows are covered by E1's exit codes. The DEV/production separation row is checked by machine:
```
e3_separation() {
  [ -f "$ST/E-restore-passed" ] || { echo "STOP: the E2 restore has not passed"; return 1; }
  curl -fsS --max-time 5 http://127.0.0.1:8000/health | python3 -c 'import json, sys; d = json.load(sys.stdin); sys.exit(0 if d.get("status") == "ok" and d.get("release") == "c2fe883be71d" and d.get("environment") == "development" and "hyperchat_dev" in (d.get("database") or {}).get("label", "") and (d.get("database") or {}).get("status") == "ok" else 1)'     || { echo "STOP: DEV /health is not development / hyperchat_dev / c2fe883be71d"; return 1; }
  curl -fsS --max-time 5 http://127.0.0.1:8001/health | python3 -c 'import json, sys; d = json.load(sys.stdin); sys.exit(0 if d.get("status") == "ok" and d.get("release") == "940137070657" and d.get("environment") == "production" else 1)'     || { echo "STOP: production /health is not production / 940137070657"; return 1; }
  [ "$(sudo -u postgres env PGOPTIONS='-c default_transaction_read_only=on' psql -X -q -At -p 5432 -d hyperchat -c 'SELECT version_num FROM alembic_version')" = 0009 ]     || { echo "STOP: production is not at 0009"; return 1; }
  date -u +%FT%TZ > "$ST/E3-passed" && echo "E3 OK: PHASE E PASSED"
}
e3_separation
```
**Record:**
- both suites' totals (passed / failed / blocked, every BLOCKED named and on the allowed list): `$ST/smoke-dev-backend.txt`, `$ST/smoke-auth.txt`;
- the E2 outputs, `$ST/restorecheck-0028.json`, `$ST/isolation-E2-dev.txt`;
- the markers.

Phase E passes only when `E3 OK: PHASE E PASSED` is printed. Otherwise, "If a phase fails".

---

## Phase F: Rollback (approval: "Phase F"; only after a failure and the owner's decision)
**Goal:** DEV back on release `229fe42381e2` with a database at 0019, the failed database kept for investigation, and **the KYC witness untouched**. Start with the session setup and the stopped state.

Phase F defines its own fail-closed functions. Paste them after the session setup, in the shell that runs Phase F:
```
# db_names: which of the three names exist on the DEV cluster (read-only)
db_names() { QP "SELECT string_agg(datname, ',' ORDER BY datname) FROM pg_database WHERE datname IN ('hyperchat_dev', '$RESTORE_DB', '$FAILED_DB')"; }
no_sessions() { [ -z "$(QP "SELECT 1 FROM pg_stat_activity WHERE datname IN ('hyperchat_dev', '$RESTORE_DB') AND pid <> pg_backend_pid() LIMIT 1")" ] \
                || { echo "STOP: sessions connected to hyperchat_dev or $RESTORE_DB"; QP "SELECT datname, usename, application_name FROM pg_stat_activity WHERE datname IN ('hyperchat_dev', '$RESTORE_DB')"; return 1; }; }
dev_grants() { QP "REVOKE ALL ON DATABASE \"hyperchat_dev\" FROM PUBLIC" \
               && QP "GRANT CONNECT ON DATABASE \"hyperchat_dev\" TO hyperchat_migrator, hyperchat_app, hyperchat_worker, hyperchat_readonly, hyperchat_lifecycle, hyperchat_auth, hyperchat_stepup" \
               || { echo "STOP: CONNECT grants on hyperchat_dev"; return 1; }; }

# verify_restored: the restored copy is at 0019 and matches the Phase D baseline; only then the F3 marker
verify_restored() {
  rm -f "$ST/F3-verified"
  [ "$(QD "$RESTORE_DB" 'SELECT version_num FROM alembic_version')" = 0019 ] || { echo "STOP: $RESTORE_DB is not at 0019"; return 1; }
  [ "$(QP "SELECT pg_encoding_to_char(encoding) || '|' || datcollate || '|' || datctype FROM pg_database WHERE datname = '$RESTORE_DB'")" \
    = "$(QP "SELECT pg_encoding_to_char(encoding) || '|' || datcollate || '|' || datctype FROM pg_database WHERE datname = 'hyperchat_dev'")" ] \
    || { echo "STOP: $RESTORE_DB encoding / locale differs from hyperchat_dev"; return 1; }
  baseline "$RESTORE_DB" > "$ST/baseline-restored.txt" || { echo "STOP: baseline of $RESTORE_DB"; return 1; }
  diff "$ST/baseline-before.txt" "$ST/baseline-restored.txt" || { echo "STOP: restored data differs from the Phase D baseline (owner decision)"; return 1; }
  date -u +%FT%TZ > "$ST/F3-verified" && echo "F3 VERIFIED"
}

# restore_into_new: the Phase B set into a NEW database; never touches hyperchat_dev
restore_into_new() { local name path
  services_stopped || return 1
  name="$(cat "$ST/phaseB.set")" && [ -n "$name" ] || { echo "STOP: no Phase B set recorded"; return 1; }
  path="/var/backups/hyperchat-dev/$name"
  ( cd /var/backups/hyperchat-dev && sha256sum -c "$name.dump.sha256" ) || { echo "STOP: set checksum"; return 1; }
  [ "$(db_names)" = "hyperchat_dev" ] || { echo "STOP: expected only hyperchat_dev, found '$(db_names)'"; return 1; }
  # the same encoding and locale as hyperchat_dev (Phase 0.4: UTF8, C.UTF-8 / C.UTF-8), never template1's defaults
  QP "CREATE DATABASE \"$RESTORE_DB\" OWNER hyperchat_owner TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8'" \
    && QP "REVOKE ALL ON DATABASE \"$RESTORE_DB\" FROM PUBLIC" \
    || { echo "STOP: cannot create $RESTORE_DB"; return 1; }
  sudo -u postgres /usr/lib/postgresql/15/bin/pg_restore -p 5433 --exit-on-error --single-transaction -d "$RESTORE_DB" < "$path.dump" \
    || { echo "STOP: pg_restore failed; $RESTORE_DB is empty and kept; hyperchat_dev unchanged"; return 1; }
  verify_restored
}

# swap_names: rename 2 runs ONLY if rename 1 succeeded; any failure returns before every later step
swap_names() {
  [ -f "$ST/F3-verified" ] || { echo "STOP: no F3 verification; run verify_restored"; return 1; }
  rm -f "$ST/F3-verified"
  services_stopped && no_sessions || return 1
  [ "$(db_names)" = "hyperchat_dev,$RESTORE_DB" ] || { echo "STOP: expected hyperchat_dev,$RESTORE_DB, found '$(db_names)'"; return 1; }
  QP "ALTER DATABASE \"hyperchat_dev\" RENAME TO \"$FAILED_DB\"" \
    || { echo "STOP: rename 1 failed; nothing changed (hyperchat_dev is still the failed database)"; return 1; }
  QP "ALTER DATABASE \"$RESTORE_DB\" RENAME TO \"hyperchat_dev\"" \
    || { echo "STOP: rename 2 failed AFTER rename 1: no database is named hyperchat_dev. Use retry_rename2 or undo_rename1"; return 1; }
  finish_swap
}
finish_swap() {
  dev_grants && head_is 0019 || return 1
  baseline > "$ST/baseline-rolledback.txt" && diff "$ST/baseline-restored.txt" "$ST/baseline-rolledback.txt" || { echo "STOP: rolled-back data check"; return 1; }
  echo "F4 DONE: hyperchat_dev is the restored 0019 database; $FAILED_DB kept"
}

# partial-swap recovery: only from exactly the state rename-1-done / rename-2-failed
retry_rename2() {
  services_stopped && no_sessions || return 1
  [ "$(db_names)" = "$FAILED_DB,$RESTORE_DB" ] || { echo "STOP: not the partial-swap state; found '$(db_names)'"; return 1; }
  QP "ALTER DATABASE \"$RESTORE_DB\" RENAME TO \"hyperchat_dev\"" || { echo "STOP: rename 2 failed again"; return 1; }
  finish_swap
}
undo_rename1() {
  services_stopped || return 1
  [ "$(db_names)" = "$FAILED_DB,$RESTORE_DB" ] || { echo "STOP: not the partial-swap state; found '$(db_names)'"; return 1; }
  QP "ALTER DATABASE \"$FAILED_DB\" RENAME TO \"hyperchat_dev\"" || { echo "STOP: undo failed; escalate"; return 1; }
  echo "UNDONE: hyperchat_dev is the failed database again; $RESTORE_DB kept; DEV stays stopped"
}

# switch_to_old: only with hyperchat_dev at 0019; worker and timer only after a healthy API on the old release
switch_to_old() { local old
  old="$(cat "$ST/old-release.id")" && [ -n "$old" ] || { echo "STOP: $ST/old-release.id missing"; return 1; }
  services_stopped && head_is 0019 && witness_ok || { echo "STOP: not switching back"; return 1; }
  ln -sfn "$REL_OLD" /opt/hyperchat/current && [ "$(readlink -f /opt/hyperchat/current)" = "$REL_OLD" ] || { echo "STOP: release link not switched back"; return 1; }
  systemctl start hyperchat-api || { echo "STOP: hyperchat-api did not start"; return 1; }
  api_healthy "$old" || return 1
  systemctl start hyperchat-worker || { echo "STOP: hyperchat-worker did not start"; return 1; }
  systemctl start hyperchat-cleanup.timer || { echo "STOP: hyperchat-cleanup.timer did not start"; return 1; }
  echo "F5 DONE: DEV runs release $old on 0019"
}
```

**F1. Preconditions:** must print `F1 OK`.
```
rm -f "$ST/D3-passed" "$ST/F3-verified"
services_stopped || enter_stopped_state
{ stat -c %s "$W"; lsattr "$W"; } > "$ST/witness-F1.txt"; cat "$ST/witness-F1.txt"
services_stopped && witness_ok && echo "F1 OK; databases: $(db_names); hyperchat_dev at $(Q 'SELECT version_num FROM alembic_version')"
```
A non-empty witness or one without its `a` flag stops Phase F (see F6).

**F2. Choose the path:**
- **Database still at `0019`** (the failure was before 0020 committed, or in the release switch with no revision applied): the database is unchanged. Go to F5 (release only).
- **Any later revision committed:** restore with F3–F4. Never downgrade.

**F3. Restore into a new database and verify it, before any rename:**
```
restore_into_new
```
- **Required:** `F3 VERIFIED`. The copy is at 0019 and its data fingerprint equals the Phase D baseline.
- **Why stdin:** root opens the dump and passes it on stdin to `pg_restore` running as `postgres`, because the backup directory is root 0700.
- **If the restore fails:** `$RESTORE_DB` stays, empty or partial, and `hyperchat_dev` is unchanged. Dropping it is a separate decision.
- **If the fingerprint differs:** DEV data changed between the Phase B backup and the Phase D baseline. The diff is printed, no marker is written, and the owner decides. With the owner's written acceptance of that loss, `date -u +%FT%TZ > "$ST/F3-verified"` records it. The 0019 check has already passed.
- **If 0.4 recorded per-database settings:** apply each one now to `$RESTORE_DB` with `ALTER DATABASE … SET …`, written out from the 0.4 record and reviewed before it runs. With no rows in 0.4 (the expected case), skip this.

**F4. Swap the names.** These are **two separate statements, not atomic**. Rename 2 runs only if rename 1 succeeded, and nothing after a failed rename runs.
```
swap_names
```
- **Required:** `F4 DONE`, with `hyperchat_dev` at 0019, CONNECT granted to the seven roles and not to `PUBLIC`, and `$FAILED_DB` kept.
- **Rename 1 fails** (usually a remaining connection; `no_sessions` lists them): nothing changed and the F3 marker is consumed. Clear the cause, run `verify_restored` again, then `swap_names` again.
- **Rename 2 fails after rename 1:** the state is exactly `$FAILED_DB` and `$RESTORE_DB` (`db_names` prints them sorted: `hyperchat_dev_failed_rehearsal,hyperchat_dev_rollback_restore`), with **no `hyperchat_dev`**. DEV stays stopped, and no service may start. Then either:
  - `retry_rename2`, once the cause is cleared; it finishes the swap and the checks; or
  - `undo_rename1`, which brings back the pre-swap state (`hyperchat_dev` is the failed database again). After that, `verify_restored` and `swap_names` may be retried.

  Both refuse unless the cluster is in exactly that partial state.
- **The failed database** `hyperchat_dev_failed_rehearsal` stays for investigation, untouched. Dropping it is a later, separate approval. No service refers to it by name.

**F5. Release back to `229fe42381e2` and start.** Database at 0019, then a healthy API on the old release, then worker and timer. Any failure leaves DEV stopped:
```
switch_to_old || enter_stopped_state
```
- **`api.env`:** keep the Phase C settings. The 0019 release ignores them (`extra="ignore"`), and keeping `HYPERCHAT_KYC_WITNESS_PATH` costs nothing. Returning to `$ST/api.env.before-C` is a separate decision.
- **Roles, pg_hba and pg_ident from Phase C stay.** They are additive and unused by the 0019 release. Reverting them is a separate decision (copy the `before-C` files back, then `pg_ctlcluster 15 restoretest reload`).

**F6. KYC witness: mandatory checks.**
```
{ stat -c %s "$W"; lsattr "$W"; } | diff "$ST/witness-F1.txt" - && witness_ok && echo "F6 OK: witness unchanged"
```
- **Never** `rm`, truncate, replace, recreate or `chattr -a` `/var/lib/hyperchat-kyc/dev.witness`. No step above touches it.
- **Why it stays correct:** enforcement never left `draft`/generation 0, so the witness is empty. An empty witness is consistent with the 0019 database (no KYC tables) and with a later rehearsal attempt.
- **If it is non-empty at F1,** something changed enforcement against the rules above:
  1. stop;
  2. keep the file;
  3. copy it read-only (`cp -p /var/lib/hyperchat-kyc/dev.witness "$ST/dev.witness.copy"`);
  4. record it for the owner.

  A later attempt follows `docs/kyc-rollout.md` "After a restore". Never re-anchor or acknowledge automatically.

**Record:** the path taken, every output, the three baseline files and their diffs, the witness size and flags before and after.

---

## Approvals checklist
| Phase | Approves | Does not approve |
|---|---|---|
| 0 | read-only commands 0.1–0.4 (0.3 after A1) | any change |
| A | staging copy and `/root/rehearsal-dev`, target release build (not switched), DEV witness file | backup runs, units or `/usr/local/sbin`, roles, env, migrations |
| B | staging the fixed backup script (B0, pinned SHA-256, root-only), one DEV backup run with it (DEV profile), one scratch restore check | backup units or timers, `/usr/local/sbin`, anything in C–F |
| C | C1–C5: the `.before-C` copies, `setup_cluster.sh 15 restoretest 5433 hyperchat_dev`, the four pg_ident lines, five `api.env` settings, the cluster reloads | restarts, migrations, `c_undo_files` (a separate decision), dropping roles, anything on `hyperchat_restore_test` |
| D | D1–D4: stopping DEV services (cleanup run included), migrations 0020–0028 behind the machine-checked per-revision checks, the gated release switch, start and the restart watch; on failure, the stopped state only | restores, downgrades, re-runs, switching back, smoke tests |
| Smoke PR | the separate test-only PR (its own review and merge) | anything on the server |
| E | E0–E3: the pinned smoke copy, the updated smoke suites, the machine-checked database checks, one more DEV backup and scratch restore, the separation checks | `HYPERCHAT_SMOKE_R2=1` (separate), verifier install, Didit, activation |
| F | `restore_into_new` (the Phase B set into `hyperchat_dev_rollback_restore`), `swap_names` (rename 2 only after rename 1), on a partial swap `retry_rename2` or `undo_rename1`, CONNECT grants, `switch_to_old` | dropping either kept database, reverting roles / pg_hba / pg_ident / `api.env` |

## After a successful rehearsal (each separately)
- The A3 install for both environments: the script at `/usr/local/sbin`, the units, and a production witness. Production's backup changes there, so it needs its own approval.
- Decide on the `hyperchat` role and close the peer path from the API's OS user to `hyperchat_restore_test` (0.1).
- Install the step-up verifier on DEV (`docs/admin-stepup.md` "Deployment sequence" step 4).
- Didit sandbox verification (`docs/kyc-rollout.md` step 3).
- Production planning (0010→0028), which this rehearsal does not cover.
