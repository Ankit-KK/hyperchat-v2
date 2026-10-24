# DEV migration rehearsal: 0019 → 0028

The controlled rehearsal of migrations 0020–0028 on the **DEV** environment, with the matching application release.
**Nothing in this document has been run.** Every phase below needs its own explicit owner approval. Approving one
phase never authorizes the next, and nothing here authorizes an improvised step.

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
| Backups | manual pre-deploy dumps only (newest `hyperchat_dev_pre_main229fe42_20261015T045216Z.dump`) | the old nightly script; result not yet checked |
| Disk | one 48 GB root filesystem, 39 GB free | same filesystem |

`deploy/check_isolation.sh` is not in the deployed DEV release (it arrived later).

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
- **How the rehearsal runs the A3 script:** directly from the root-only staging copy, with a cleared environment and the DEV profile fixed on the same command line:
  ```
  env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HYPERCHAT_BACKUP_ENV=dev bash /root/hyperchat-c2fe883/deploy/backup/hyperchat-pgbackup.sh
  ```
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
REL_OLD=/opt/hyperchat/releases/229fe42381e2
REL_NEW=/opt/hyperchat/releases/c2fe883be71d
STAGE=/root/hyperchat-c2fe883
ST=/root/rehearsal-dev
FAILED_DB=hyperchat_dev_failed_rehearsal
RESTORE_DB=hyperchat_dev_rollback_restore
# Q: one read-only query against the DEV database (port 5433 only; never production)
Q()  { sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 -p 5433 -d hyperchat_dev -Atc "$1"; }
# QD: the same against a named DEV-cluster database (used only in Phase F)
QD() { sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 -p 5433 -d "$1" -Atc "$2"; }
# QP: the DEV cluster's maintenance database (database-level statements; port 5433 only)
QP() { sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 -p 5433 -d postgres -Atc "$1"; }
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
enter_stopped_state() { systemctl stop hyperchat-cleanup.timer; systemctl stop hyperchat-worker; systemctl stop hyperchat-api
                        services_stopped && echo "DEV STOPPED: wait for the owner (see 'If a phase fails')"; }
# api_healthy RELEASE: polls the local /health for up to 2 minutes; succeeds only with status ok, database ok, that release
api_healthy() { local i out
  for i in $(seq 1 24); do
    out="$(curl -fsS --max-time 5 http://127.0.0.1:8000/health 2>/dev/null)" \
      && printf '%s' "$out" | python3 -c 'import json, sys; d = json.load(sys.stdin); sys.exit(0 if d.get("status") == "ok" and d.get("release") == sys.argv[1] and (d.get("database") or {}).get("status") == "ok" else 1)' "$1" \
      && { echo "API healthy on release $1"; return 0; }
    sleep 5
  done
  echo "STOP: API not healthy on release $1 within 2 minutes"; return 1; }
# dev_backup FILE: one DEV backup set (DEV profile on the same line as the script); succeeds only with exit 0 AND a
# "backup ok" line naming a complete set, whose name then goes to $ST/FILE
dev_backup() { local out rc name
  out="$(env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HYPERCHAT_BACKUP_ENV=dev bash "$STAGE/deploy/backup/hyperchat-pgbackup.sh" 2>&1)"; rc=$?
  printf '%s\n' "$out"
  [ "$rc" = 0 ] || { echo "STOP: backup exit $rc"; return 1; }
  name="$(printf '%s\n' "$out" | sed -n 's/^backup ok env=dev db=hyperchat_dev set=\([A-Za-z0-9_]*\) .*/\1/p')"
  [ -n "$name" ] && [ -f "/var/backups/hyperchat-dev/$name.manifest.json" ] || { echo "STOP: no complete set named in the output"; return 1; }
  printf '%s\n' "$name" > "$ST/$1" && echo "SET $name"; }
```
- **No `set -e`:** it would close an interactive shell on the first error.
- **Fail-closed functions:** every consequential step (anything that creates, renames, switches or starts) is a function whose steps are chained with `&&`. A failing step makes the function print `STOP: …` and return non-zero before anything later in it runs. Where the runbook writes `f || enter_stopped_state`, a failure leaves DEV stopped.
- **On `STOP`,** don't run the next command. Go to "If a phase fails" below.
- **Read-only blocks** (inspections, records) may print `STOP` and continue; they change nothing.
- **Where commands run:** commands marked **(workstation)** run on the operator's machine, in a checkout of this repository; `root@VPS` stands for the server's SSH target, as in `deploy/README.md`. Everything else runs on the VPS as root, after the session setup.

---

## Phase 0: Inspection gaps (read-only; approval: read-only VPS access)
These checks change nothing. **Phase C may not start until 0.1 and 0.4 are answered.**

### 0.1 The unexpected DEV database role `hyperchat`
`roles.sql` doesn't create a role named `hyperchat`, and no repository file maps or uses one. Find out what it is before `setup_cluster.sh` runs `REVOKE ALL ON DATABASE hyperchat_dev FROM PUBLIC` (Phase C), which could cut off anything that silently relies on it.
```
QP "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit, rolreplication, rolbypassrls, rolconnlimit, rolvaliduntil FROM pg_roles WHERE rolname = 'hyperchat'"
QP "SELECT rolpassword IS NOT NULL AS has_password FROM pg_authid WHERE rolname = 'hyperchat'"
QP "SELECT g.rolname AS granted_role, m.rolname AS member FROM pg_auth_members a JOIN pg_roles g ON g.oid = a.roleid JOIN pg_roles m ON m.oid = a.member WHERE 'hyperchat' IN (g.rolname, m.rolname)"
QP "SELECT datname, pg_get_userbyid(datdba) AS owner, datacl FROM pg_database ORDER BY 1"
for db in hyperchat_dev hyperchat_restore_test postgres; do QD "$db" "SELECT '$db', count(*) FROM pg_class WHERE relowner = 'hyperchat'::regrole"; done
QP "SELECT count(*) FROM pg_stat_activity WHERE usename = 'hyperchat'"
grep -nE '(^|[[:space:]])hyperchat([[:space:]]|$)' /etc/postgresql/15/restoretest/pg_hba.conf /etc/postgresql/15/restoretest/pg_ident.conf
```
**Record:** attributes, whether it can log in or has a password, memberships, owned objects per database, database ACLs, live sessions.
- **Decision (owner):** keep, document, or drop it.
- **Dropping is a separate approved change,** never part of this rehearsal.
- **If it owns objects in `hyperchat_dev` or `hyperchat_restore_test`, or has a live session:** stop and decide before Phase C.

### 0.2 Production backup: exit status, archive and integrity (read-only)
A timer that fired doesn't mean a backup succeeded. Check the last run's result and the newest archive itself:
```
systemctl show hyperchat-pgbackup.service -p Result -p ExecMainStatus -p ExecMainStartTimestamp -p ExecMainExitTimestamp
systemctl cat hyperchat-pgbackup.service | grep -E '^(ExecStart|Environment)='
journalctl -u hyperchat-pgbackup.service --since "3 days ago" --no-pager | tail -n 30
ls -lt /var/backups/hyperchat | head -n 10
N=$(ls -t /var/backups/hyperchat/hyperchat_*.dump | head -n 1); echo "$N"; stat -c '%s bytes, %y' "$N"
( cd /var/backups/hyperchat && sha256sum -c "$(basename "$N").sha256" )
/usr/lib/postgresql/17/bin/pg_restore -l "$N" | grep -c '^[0-9]'
```
**Record:**
- `Result=success` and `ExecMainStatus=0`;
- the newest archive's name, time (the 02:37 UTC run) and size;
- `sha256sum -c` → `OK`;
- a non-zero TOC entry count;
- the `ExecStart` path (expected `/usr/local/sbin/hyperchat-pgbackup`, which confirms the rule in "The backup script and production").

Any other answer is a production finding, reported to the owner. The DEV rehearsal doesn't depend on it, but production's backup must not be assumed healthy.

### 0.3 Isolation audit behaviour today (read-only; after Phase A1 exists)
The deployed DEV release has no `check_isolation.sh`, so it runs from the staging copy that Phase A1 creates. If Phase A isn't approved yet, run this right after A1.
```
bash "$STAGE/deploy/check_isolation.sh" dev; echo "exit=$?"
bash "$STAGE/deploy/check_isolation.sh" prod; echo "exit=$?"
```
- **Expected today:** exit 1, with `FAIL` lines for the pg_hba rule (lifecycle, auth and stepup not covered) and the pg_ident map (API user not mapped to lifecycle and auth), on both environments. File-ownership and unit lines should be `OK`.
- **Record:** the full output. Any FAIL outside pg_hba / pg_ident (release tree, `api.env`, units, service account) must be understood before Phase C. The script is read-only (`deploy/check_isolation.sh` header) and needs `namei` (util-linux).

### 0.4 Database-level settings of `hyperchat_dev` (read-only)
A restore into a new database (Phase F) recreates the schema and data, but not settings attached to the database itself.
```
QP "SELECT coalesce(r.rolname, '*') AS role, s.setconfig FROM pg_db_role_setting s LEFT JOIN pg_roles r ON r.oid = s.setrole WHERE s.setdatabase = (SELECT oid FROM pg_database WHERE datname = 'hyperchat_dev')"
QP "SELECT pg_get_userbyid(datdba), pg_encoding_to_char(encoding), datcollate, datctype FROM pg_database WHERE datname = 'hyperchat_dev'"
```
**Record:** every row. Phase F re-applies any per-database setting found here. No rows is the expected answer.

---

## Phase A: Preparation (approval: "Phase A")
Prepares files only. Changes no database, no running service, no unit, and nothing under `/usr/local/sbin`.

**A1. Staging copy and record directory.** The staging copy is root-only. It provides the scripts and the smoke suite, and is never run as a service.

**(workstation)**
```
git archive c2fe883be71d1a494232d4147ed4d6fbc4e70eef backend database deploy tests \
  | ssh root@VPS "install -d -o root -g root -m 0700 /root/hyperchat-c2fe883 && tar -x -C /root/hyperchat-c2fe883"
```
**(VPS)**
```
install -d -o root -g root -m 0700 "$ST"
ls "$STAGE/deploy/check_isolation.sh" "$STAGE/deploy/backup/hyperchat-pgbackup.sh" "$STAGE/deploy/postgres/setup_cluster.sh" || echo "STOP: staging copy incomplete"
cat "$REL_OLD/backend/RELEASE" > "$ST/old-release.id" && cat "$ST/old-release.id"     # the id /health reports for the 0019 release
api_healthy "$(cat "$ST/old-release.id")"      # read-only: the running API reports exactly that id
```

**A2. Build the target release**, without switching `current` ("Deploy a commit" steps 1–3; `deploy/README.md`). The directory name follows the 12-character convention of `229fe42381e2`.

**(workstation)**
```
git archive c2fe883be71d1a494232d4147ed4d6fbc4e70eef backend database \
  | ssh root@VPS "mkdir -p /opt/hyperchat/releases/c2fe883be71d && tar -x -C /opt/hyperchat/releases/c2fe883be71d"
```
**(VPS)**
```
build_release() {
  [ -d "$REL_NEW/backend" ] && [ ! -e "$REL_NEW/venv" ] || { echo "STOP: $REL_NEW missing, or a venv already exists"; return 1; }
  python3.14 -m venv "$REL_NEW/venv" && "$REL_NEW/venv/bin/pip" install -r "$REL_NEW/backend/requirements.txt" || { echo "STOP: venv"; return 1; }
  echo c2fe883be71d > "$REL_NEW/backend/RELEASE" \
    && chown -R root:hyperchat "$REL_NEW" && chmod -R g+rX,g-w,o-rwx "$REL_NEW" || { echo "STOP: RELEASE / ownership"; return 1; }
  ( cd "$REL_NEW/backend" && ../venv/bin/python -c "import app.main" ) || { echo "STOP: import"; return 1; }
  [ "$(readlink -f /opt/hyperchat/current)" = "$REL_OLD" ] || { echo "STOP: current is not $REL_OLD"; return 1; }
  echo "A2 OK: release built, nothing switched"
}
build_release
```
A failed build leaves an unused directory and nothing else. It is removed or rebuilt only by decision.

**A3. Create the DEV KYC witness** (`docs/kyc-rollout.md` "Server setup"), so that every backup in Phases B and E carries it:
```
install -d -o root -g root -m 0755 /var/lib/hyperchat-kyc
[ -e /var/lib/hyperchat-kyc/dev.witness ] && echo "STOP: dev.witness already exists; do not recreate it" \
  || { install -o hyperchat -g hyperchat -m 0600 /dev/null /var/lib/hyperchat-kyc/dev.witness && chattr +a /var/lib/hyperchat-kyc/dev.witness; }
stat -c '%U:%G %a %s bytes' "$W"; lsattr "$W"   # hyperchat:hyperchat 600 0 bytes; 'a' flag
witness_ok && echo "A3 OK"
```
- **An empty file is a fresh witness.** At 0019 nothing reads it.
- **After 0023:** the API appends a line only once enforcement history exists (`KycWitness.record` returns early at generation 0), so the file stays empty throughout this rehearsal.
- **From now on, nobody removes, truncates, replaces or `chattr -a`s it,** including during rollback.
- **Production's witness** (`prod.witness`) is not created here.

**A4. No backup units, no `/usr/local/sbin` install.** See "The backup script and production".

**Record:** A1–A3 outputs (`$ST/old-release.id`, `A2 OK`, `A3 OK`, witness `stat` / `lsattr`), the release's ownership (`stat -c '%U:%G %a' "$REL_NEW"`), and that `current` is unchanged. Run 0.3 now if it isn't done yet.

---

## Phase B: Fresh backup and disposable restore test (approval: "Phase B")
**B1. One verified DEV backup set** (the DEV profile on the same line; see "The backup script and production"):
```
rm -f "$ST/phaseB.set"; dev_backup phaseB.set
```
- **Required:** `SET hyperchat_dev_<UTC>`, written to `$ST/phaseB.set` only when the script exited 0 and named a complete set.
- **Any other result is a STOP, and `$ST/phaseB.set` stays absent:**
  - exit 3: witness missing, so A3 wasn't done;
  - 2: refused configuration;
  - 4: an older set fails verification (none should exist yet);
  - 75: another run in progress;
  - 1: failed.

**B2. Restore it into a disposable PostgreSQL 15** (`docs/backup-recovery.md` "Verifying a set"). The set is at 0019, so pass its own head (the default would be this checkout's 0028):
```
rm -f "$ST/B2-passed"
test -s "$ST/phaseB.set" \
  && ( cd "$REL_NEW/backend" && ../venv/bin/python -m app.backup.restore_check --env dev --dir /var/backups/hyperchat-dev \
         --set "$(cat "$ST/phaseB.set")" --pg-bin /usr/lib/postgresql/15/bin --scratch --scratch-user postgres \
         --expect-head 0019 --output "$ST/restorecheck-0019.json" ) \
  && date -u +%FT%TZ > "$ST/B2-passed" && echo "B2 OK" || echo "STOP: restore check failed (see $ST/restorecheck-0019.json)"
```
**Required:**
- `B2 OK` (`restore_check` exits 0 only when the restore is structurally valid and the witness verdict is consistent or not applicable);
- in the JSON: `structurally_valid: true`, `restore_valid: true`, `invariants.alembic_version: "0019"`;
- `kyc_witness` `not_applicable` (no KYC tables at 0019) or `consistent`;
- `live_operation_approved: false` (always).

The scratch cluster runs in a temporary directory with no TCP and is deleted afterwards. No live database is touched.

**B3. Fallback:** the older manual dump `hyperchat_dev_pre_main229fe42_20261015T045216Z.dump` stays where it is. Restoring it would lose 8 days of DEV changes. It is a fallback only if the B1 set turns out unusable, and only by separate decision.

**Record:** `$ST/phaseB.set`, `$ST/B2-passed`, the manifest's dump `sha256`, `$ST/restorecheck-0019.json`.

---

## Phase C: Roles, authentication rules, environment (approval: "Phase C"; requires 0.1 and 0.4 answered)
These changes are compatible with the running 0019 release. New roles and map lines are unused by it, and new `api.env` settings are ignored by it. Nothing is restarted.

**C1. Keep the originals** (fixed names; `cp -n` never overwrites an earlier copy):
```
cp -pn /etc/postgresql/15/restoretest/pg_hba.conf   "$ST/pg_hba.conf.before-C"
cp -pn /etc/postgresql/15/restoretest/pg_ident.conf "$ST/pg_ident.conf.before-C"
cp -pn /etc/hyperchat/api.env "$ST/api.env.before-C"
ls -l "$ST"
```

**C2. Roles, CONNECT grants and the pg_hba rule.** Preview the pg_hba change, then run `setup_cluster.sh` (idempotent). It:
- applies `roles.sql` (the three new roles);
- syncs the HyperChat pg_hba rule (keeping its own `pg_hba.conf.bak-<timestamp>`);
- reloads the cluster;
- revokes `CONNECT` from `PUBLIC` on `hyperchat_dev` and grants it to the seven HyperChat login roles.

Preview (read-only). Review the diff before applying:
```
bash "$STAGE/deploy/postgres/sync_pg_hba.sh" "$STAGE/deploy/postgres/pg_hba.conf" /etc/postgresql/15/restoretest/pg_hba.conf \
  | diff /etc/postgresql/15/restoretest/pg_hba.conf -
```
Apply, only after the preview shows exactly the HyperChat rule gaining `hyperchat_lifecycle,hyperchat_auth,hyperchat_stepup`:
```
bash "$STAGE/deploy/postgres/setup_cluster.sh" 15 restoretest 5433 hyperchat_dev && echo "C2 OK" || echo "STOP: setup_cluster.sh failed"
```
Name only `hyperchat_dev`; never `hyperchat_restore_test`, whose grants stay as they are. `setup_cluster.sh` runs nothing on the production cluster: every command takes the version, cluster and port given.

**C3. pg_ident lines** (`setup_cluster.sh` appends the map only when none exists, so DEV needs these added). Append each missing mapping of `deploy/postgres/pg_ident.conf`, compared by field so differences in spacing never cause duplicates:
```
add_ident() { local F=/etc/postgresql/15/restoretest/pg_ident.conf m u r
  for m in "root hyperchat_auth" "hyperchat hyperchat_lifecycle" "hyperchat hyperchat_auth" "hyperchat-stepup hyperchat_stepup"; do
    u="${m% *}"; r="${m#* }"
    awk -v u="$u" -v r="$r" '$1 == "hyperchat" && $2 == u && $3 == r {f=1} END {exit !f}' "$F" \
      || printf 'hyperchat   %-17s %s\n' "$u" "$r" >> "$F" || { echo "STOP: cannot append to $F"; return 1; }
  done
  grep -nE '^hyperchat[[:space:]]' "$F"
  pg_ctlcluster 15 restoretest reload && echo "C3 OK" || { echo "STOP: reload failed"; return 1; }
}
add_ident
```
- **The `hyperchat-stepup` line is harmless** before that OS user exists. The isolation audit requires that only that user maps to `hyperchat_stepup`.
- **Never add** a `hyperchat-prod` line here, or any DEV line on the production cluster.

**C4. `api.env` settings** (names from `deploy/api.env.example`; no secret value). Two passes: check every key first, append only if none conflicts. An existing key with another value is a STOP, and nothing is appended:
```
add_env() { local E=/etc/hyperchat/api.env kv k
  local want="HYPERCHAT_DB_LIFECYCLE_USER=hyperchat_lifecycle HYPERCHAT_DB_AUTH_USER=hyperchat_auth HYPERCHAT_ADMIN_SESSION_IDLE_HOURS=2 HYPERCHAT_ADMIN_SESSION_ABSOLUTE_HOURS=12 HYPERCHAT_KYC_WITNESS_PATH=/var/lib/hyperchat-kyc/dev.witness"
  for kv in $want; do k="${kv%%=*}"
    if grep -q "^$k=" "$E" && ! grep -qx "$kv" "$E"; then echo "STOP: $k already set to another value; nothing appended"; return 1; fi
  done
  for kv in $want; do grep -qx "$kv" "$E" || printf '%s\n' "$kv" >> "$E" || { echo "STOP: cannot append to $E"; return 1; }; done
  [ "$(stat -c '%U:%G %a' "$E")" = "root:hyperchat 640" ] || { echo "STOP: $E is not root:hyperchat 640"; return 1; }
  [ "$(grep -cE '^HYPERCHAT_(STEPUP_SOCKET|DIDIT_)' "$E")" = 0 ] || { echo "STOP: a verifier socket or Didit setting is present"; return 1; }
  echo "C4 OK"
}
add_env
```

**C5. Verify** (read-only):
```
QP "SELECT rolname FROM pg_roles WHERE rolname LIKE 'hyperchat%' ORDER BY 1"
QP "SELECT r, has_database_privilege(r, 'hyperchat_dev', 'CONNECT') FROM unnest(ARRAY['hyperchat_lifecycle','hyperchat_auth','hyperchat_stepup']) r"
bash "$STAGE/deploy/check_isolation.sh" dev && echo "isolation OK" || echo "STOP: isolation audit"   # every line OK
api_healthy "$(cat "$ST/old-release.id")"                        # the 0019 release still answers, healthy
```
**Record:** the pg_hba diff, `setup_cluster.sh` output, the pg_ident lines, the `api.env` mode, `check_isolation.sh dev` (all `OK`), `/health`.

---

## Phase D: The migration rehearsal (approval: "Phase D", with a time window)
DEV is unavailable for the window. Expect minutes.

**D1. Pre-flight.** It starts every Phase D attempt by **invalidating any earlier gate marker**, then checks the starting state. It must print `D1 PASSED`; otherwise STOP before anything is stopped.
```
d1_preflight() {
  rm -f "$ST/D3-passed" && [ ! -e "$ST/D3-passed" ] || { echo "STOP: cannot remove an old D3 marker"; return 1; }
  [ "$(readlink -f /opt/hyperchat/current)" = "$REL_OLD" ] || { echo "STOP: current is not $REL_OLD"; return 1; }
  head_is 0019 || return 1
  test -s "$ST/phaseB.set" && test -f "$ST/B2-passed" \
    && test -f "/var/backups/hyperchat-dev/$(cat "$ST/phaseB.set").manifest.json" || { echo "STOP: no verified Phase B set"; return 1; }
  witness_ok || return 1
  ! grep -q '^HYPERCHAT_BOTS_ENABLED=true' /etc/hyperchat/api.env || { echo "STOP: bots enabled"; return 1; }
  api_healthy "$(cat "$ST/old-release.id")" || return 1
  baseline > "$ST/baseline-before.txt" && cat "$ST/baseline-before.txt" || { echo "STOP: baseline"; return 1; }   # streamers|133|0, signups|233|…
  echo "D1 PASSED"
}
d1_preflight
```

**D2. Stop DEV services** (reverse of the recovery order in `docs/backup-recovery.md`). It must print `DEV STOPPED`:
```
enter_stopped_state
```

**D3. Migrate one revision at a time. Fail-stop.** Define the step function in this session:
```
migrate_one() {   # $1 = the revision; succeeds only when alembic succeeded AND the database is exactly at $1
  services_stopped || return 1
  ( cd "$REL_NEW/database" && HYPERCHAT_MIGRATION_DSN="postgresql+psycopg://hyperchat_migrator@/hyperchat_dev?host=/var/run/postgresql&port=5433" \
      ../venv/bin/alembic upgrade "$1" ) || { echo "STOP: alembic upgrade $1 failed"; return 1; }
  head_is "$1" || return 1
  echo "OK $1"
}
```
Then run **one line at a time**, and run the next line **only if the previous one printed `OK …`** and its check printed the expected value. There is deliberately no loop:

| Run | Then check (must match "Expect") | Expect |
|---|---|---|
| `migrate_one 0020` | `Q "SELECT kyc_track, count(*), count(*) FILTER (WHERE kyc_baseline_owner_id IS NOT DISTINCT FROM user_id) FROM streamers GROUP BY 1"` | `legacy\|133\|133` |
| | `Q "SELECT count(*) FROM streamers WHERE public.creator_donation_block(id) IS NULL" > "$ST/eligible-after-0020.txt"; cat "$ST/eligible-after-0020.txt"` | the number of `active` creators in the baseline (`Q "SELECT count(*) FROM streamers WHERE status = 'active'"`) |
| `migrate_one 0021` | `Q "SELECT review_status, count(*) FROM user_signups GROUP BY 1"` | `legacy_unreviewed\|<the baseline signups count>` (233, or more if signups arrived after the inspection) |
| `migrate_one 0022` | (none beyond `OK`) | |
| `migrate_one 0023` | `Q "SELECT state, generation FROM kyc_enforcement"` | `draft\|0` |
| `migrate_one 0024` | (none beyond `OK`) | |
| `migrate_one 0025` | `Q "SELECT state FROM stepup_enforcement"` | `draft` |
| `migrate_one 0026` | (none beyond `OK`) | |
| `migrate_one 0027` | (none beyond `OK`) | |
| `migrate_one 0028` | `Q "SELECT version_num FROM alembic_version"` | `0028` |

**D3 gate.** It removes any marker first and writes a new one **last**, only when every check succeeds:
```
d3_gate() {
  rm -f "$ST/D3-passed"
  services_stopped && head_is 0028 && drafts_ok && witness_ok || { echo "STOP: D3 gate failed; do not switch the release"; return 1; }
  date -u +%FT%TZ > "$ST/D3-passed" && echo "D3 PASSED"
}
d3_gate
```

**D4. Switch the release and start the services.** The marker is single-use: the function removes it before doing anything, so a stale or reused marker can never authorize a switch. Then it re-validates the state itself (services stopped, head 0028, KYC `draft|0`, MFA `draft`, witness empty and append-only) and switches. It starts the API, and starts the worker and the cleanup timer **only after `/health` reports `ok` on release `c2fe883be71d`**. Any failure leaves DEV stopped:
```
switch_to_new() {
  [ -f "$ST/D3-passed" ] || { echo "STOP: no D3 marker from this attempt; run d3_gate"; return 1; }
  rm -f "$ST/D3-passed" || { echo "STOP: cannot consume the D3 marker"; return 1; }
  services_stopped && head_is 0028 && drafts_ok && witness_ok || { echo "STOP: state is not what the gate checked; not switching"; return 1; }
  ln -sfn "$REL_NEW" /opt/hyperchat/current && [ "$(readlink -f /opt/hyperchat/current)" = "$REL_NEW" ] || { echo "STOP: release link not switched"; return 1; }
  systemctl start hyperchat-api || { echo "STOP: hyperchat-api did not start"; return 1; }
  api_healthy c2fe883be71d || return 1
  systemctl start hyperchat-worker || { echo "STOP: hyperchat-worker did not start"; return 1; }
  systemctl start hyperchat-cleanup.timer || { echo "STOP: hyperchat-cleanup.timer did not start"; return 1; }
  echo "D4 DONE: DEV runs release c2fe883be71d"
}
switch_to_new || enter_stopped_state
```
- **After a D4 failure:** DEV is stopped again (`DEV STOPPED`). The release link stays wherever the failure left it; switching back is Phase F. A retry needs a new `d3_gate` run, because the marker was consumed.
- **Then (read-only):**
  ```
  journalctl -u hyperchat-api -u hyperchat-worker --since "-5 min" --no-pager | grep -iE "error|critical|refus" | head
  ```
- **The cleanup service:** don't start it by hand. Its timer runs it daily with the same `api.env`, witness path included.

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

Phase E then uses a root-only copy of that PR's merge commit. Set `SMOKE_SHA` to its full 40-character SHA first.

**(workstation)**
```
SMOKE_SHA=   # the merge commit of the smoke PR, 40 hex characters
[ "${#SMOKE_SHA}" = 40 ] \
  && [ -z "$(git diff --stat c2fe883be71d1a494232d4147ed4d6fbc4e70eef "$SMOKE_SHA" -- backend database deploy ':(exclude)backend/tests' ':(exclude)*.md')" ] \
  && git archive "$SMOKE_SHA" backend tests | ssh root@VPS "install -d -o root -g root -m 0700 /root/hyperchat-smoke && tar -x -C /root/hyperchat-smoke" \
  && echo "SMOKE COPY OK" || echo "STOP: SMOKE_SHA unset, the smoke PR changes code outside tests/, or the copy failed"
```
**Record:** `SMOKE_SHA` and the empty `git diff --stat` output: application, migration and deployment code identical to `c2fe883`; only tests (`backend/tests`, `tests/`) and documentation may differ.

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

### E1. The smoke suites
```
cd /root/hyperchat-smoke
HYPERCHAT_SMOKE_EXPECT_HEAD=0028 /opt/hyperchat/current/venv/bin/python tests/e2e/smoke_dev_backend.py; echo "exit=$?"     # exit=0
HYPERCHAT_SMOKE_SAMESITE=none /opt/hyperchat/current/venv/bin/python tests/e2e/smoke_auth.py; echo "exit=$?"   # exit=0
```

### E2. Database checks (read-only; after the suites cleaned their synthetic rows)
```
baseline > "$ST/baseline-after.txt"; diff "$ST/baseline-before.txt" "$ST/baseline-after.txt" && echo baseline-unchanged
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

**Privilege isolation**, verified on a restored copy, never by probing the live database:
```
rm -f "$ST/phaseE.set" "$ST/E-restore-passed"
dev_backup phaseE.set \
  && ( cd "$REL_NEW/backend" && ../venv/bin/python -m app.backup.restore_check --env dev --dir /var/backups/hyperchat-dev \
         --set "$(cat "$ST/phaseE.set")" --pg-bin /usr/lib/postgresql/15/bin --scratch --scratch-user postgres \
         --output "$ST/restorecheck-0028.json" ) \
  && date -u +%FT%TZ > "$ST/E-restore-passed" && echo "E2 RESTORE OK" || echo "STOP: backup or restore check failed"
```
**Required:**
- `E2 RESTORE OK`, and the backup's manifest records alembic `0028` and KYC `draft`;
- the restore check exits 0 (default expected head: the release's own `0028`);
- in the JSON:
  - `structurally_valid: true`;
  - `invariants.alembic_matches_expected_head: true`;
  - `objects_not_owned_by_hyperchat_owner: 0`;
  - `public_table_privileges: 0`;
  - `guard_triggers_disabled: 0`;
  - `runtime_role_privilege_leaks: []`;
  - `stepup_enforcement: "draft"`;
  - `kyc_witness: "consistent"`.

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

**Record:** both suites' totals (passed / failed / blocked, every BLOCKED named and on the allowed list), the E2 outputs, `$ST/restorecheck-0028.json`, `check_isolation.sh` output. Phase E passes only when every mandatory item passes. Otherwise, "If a phase fails".

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
  QP "CREATE DATABASE \"$RESTORE_DB\" OWNER hyperchat_owner" && QP "REVOKE ALL ON DATABASE \"$RESTORE_DB\" FROM PUBLIC" \
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
| B | one DEV backup run (staging script, DEV profile), one scratch restore check | backup units or timers, anything in C–F |
| C | `setup_cluster.sh 15 restoretest 5433 hyperchat_dev`, the four pg_ident lines, five `api.env` settings, the cluster reload | restarts, migrations |
| D | stopping DEV services, migrations 0020–0028 behind the per-revision checks, the gated release switch, start; on failure, the stopped state only | restores, downgrades, re-runs, switching back, smoke tests |
| Smoke PR | the separate test-only PR (its own review and merge) | anything on the server |
| E | the updated smoke suites, the checks listed, one more DEV backup and scratch restore | `HYPERCHAT_SMOKE_R2=1` (separate), verifier install, Didit, activation |
| F | `restore_into_new` (the Phase B set into `hyperchat_dev_rollback_restore`), `swap_names` (rename 2 only after rename 1), on a partial swap `retry_rename2` or `undo_rename1`, CONNECT grants, `switch_to_old` | dropping either kept database, reverting roles / pg_hba / pg_ident / `api.env` |

## After a successful rehearsal (each separately)
- The A3 install for both environments: the script at `/usr/local/sbin`, the units, and a production witness. Production's backup changes there, so it needs its own approval.
- Decide on the `hyperchat` role (0.1).
- Install the step-up verifier on DEV (`docs/admin-stepup.md` "Deployment sequence" step 4).
- Didit sandbox verification (`docs/kyc-rollout.md` step 3).
- Production planning (0010→0028), which this rehearsal does not cover.
