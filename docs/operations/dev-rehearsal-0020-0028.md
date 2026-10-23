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
```
- **No `set -e`:** it would close an interactive shell on the first error. Every step instead checks its own result and prints `STOP: …`.
- **On `STOP`,** don't run the next command. Go to "If a phase fails" below.
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
```

**A2. Build the target release**, without switching `current` ("Deploy a commit" steps 1–3; `deploy/README.md`). The directory name follows the 12-character convention of `229fe42381e2`.

**(workstation)**
```
git archive c2fe883be71d1a494232d4147ed4d6fbc4e70eef backend database \
  | ssh root@VPS "mkdir -p /opt/hyperchat/releases/c2fe883be71d && tar -x -C /opt/hyperchat/releases/c2fe883be71d"
```
**(VPS)**
```
python3.14 -m venv "$REL_NEW/venv" && "$REL_NEW/venv/bin/pip" install -r "$REL_NEW/backend/requirements.txt" || echo "STOP: venv"
echo c2fe883be71d > "$REL_NEW/backend/RELEASE"
chown -R root:hyperchat "$REL_NEW" && chmod -R g+rX,g-w,o-rwx "$REL_NEW"
( cd "$REL_NEW/backend" && ../venv/bin/python -c "import app.main" ) && echo import-ok || echo "STOP: import"
readlink -f /opt/hyperchat/current      # still $REL_OLD: nothing switched
```

**A3. Create the DEV KYC witness** (`docs/kyc-rollout.md` "Server setup"), so that every backup in Phases B and E carries it:
```
install -d -o root -g root -m 0755 /var/lib/hyperchat-kyc
[ -e /var/lib/hyperchat-kyc/dev.witness ] && echo "STOP: dev.witness already exists; do not recreate it" \
  || { install -o hyperchat -g hyperchat -m 0600 /dev/null /var/lib/hyperchat-kyc/dev.witness && chattr +a /var/lib/hyperchat-kyc/dev.witness; }
stat -c '%U:%G %a %s bytes' /var/lib/hyperchat-kyc/dev.witness; lsattr /var/lib/hyperchat-kyc/dev.witness   # hyperchat:hyperchat 600 0 bytes; 'a' flag
```
- **An empty file is a fresh witness.** At 0019 nothing reads it.
- **After 0023:** the API appends a line only once enforcement history exists (`KycWitness.record` returns early at generation 0), so the file stays empty throughout this rehearsal.
- **From now on, nobody removes, truncates, replaces or `chattr -a`s it,** including during rollback.
- **Production's witness** (`prod.witness`) is not created here.

**A4. No backup units, no `/usr/local/sbin` install.** See "The backup script and production".

**Record:** A1–A3 outputs (`import-ok`, witness `stat` / `lsattr`), the release's ownership (`stat -c '%U:%G %a' "$REL_NEW"`), and that `current` is unchanged. Run 0.3 now if it isn't done yet.

---

## Phase B: Fresh backup and disposable restore test (approval: "Phase B")
**B1. One verified DEV backup set** (the DEV profile on the same line; see "The backup script and production"):
```
env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HYPERCHAT_BACKUP_ENV=dev bash "$STAGE/deploy/backup/hyperchat-pgbackup.sh"; echo "exit=$?"
```
- **Required:** `exit=0` and a line `backup ok env=dev db=hyperchat_dev set=hyperchat_dev_<UTC> …`.
- **Any other result stops Phase B:**
  - 3: witness missing, so A3 wasn't done;
  - 2: refused configuration;
  - 4: an older set fails verification (none should exist yet);
  - 75: another run in progress;
  - 1: failed.

Save the set name for later phases, which may run in other sessions:
```
ls -1t /var/backups/hyperchat-dev/*.manifest.json | head -n 1 | xargs -r basename | sed 's/\.manifest\.json$//' > "$ST/phaseB.set"
cat "$ST/phaseB.set"     # hyperchat_dev_<UTC>, the name printed by the backup line above; STOP if they differ
```

**B2. Restore it into a disposable PostgreSQL 15** (`docs/backup-recovery.md` "Verifying a set"). The set is at 0019, so pass its own head (the default would be this checkout's 0028):
```
( cd "$REL_NEW/backend" && ../venv/bin/python -m app.backup.restore_check --env dev --dir /var/backups/hyperchat-dev \
    --set "$(cat "$ST/phaseB.set")" --pg-bin /usr/lib/postgresql/15/bin --scratch --scratch-user postgres \
    --expect-head 0019 --output "$ST/restorecheck-0019.json" ); echo "exit=$?"
```
**Required:**
- `exit=0`;
- in the JSON: `structurally_valid: true`, `restore_valid: true`, `invariants.alembic_version: "0019"`;
- `kyc_witness` `not_applicable` (no KYC tables at 0019) or `consistent`;
- `live_operation_approved: false` (always).

The scratch cluster runs in a temporary directory with no TCP and is deleted afterwards. No live database is touched.

**B3. Fallback:** the older manual dump `hyperchat_dev_pre_main229fe42_20261015T045216Z.dump` stays where it is. Restoring it would lose 8 days of DEV changes. It is a fallback only if the B1 set turns out unusable, and only by separate decision.

**Record:** `$ST/phaseB.set`, the manifest's dump `sha256`, `$ST/restorecheck-0019.json`.

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

```
bash "$STAGE/deploy/postgres/sync_pg_hba.sh" "$STAGE/deploy/postgres/pg_hba.conf" /etc/postgresql/15/restoretest/pg_hba.conf \
  | diff /etc/postgresql/15/restoretest/pg_hba.conf -
bash "$STAGE/deploy/postgres/setup_cluster.sh" 15 restoretest 5433 hyperchat_dev; echo "exit=$?"
```
Name only `hyperchat_dev`; never `hyperchat_restore_test`, whose grants stay as they are. `setup_cluster.sh` runs nothing on the production cluster: every command takes the version, cluster and port given.

**C3. pg_ident lines** (`setup_cluster.sh` appends the map only when none exists, so DEV needs these added). Append each missing mapping of `deploy/postgres/pg_ident.conf`, compared by field so differences in spacing never cause duplicates:
```
F=/etc/postgresql/15/restoretest/pg_ident.conf
for m in "root hyperchat_auth" "hyperchat hyperchat_lifecycle" "hyperchat hyperchat_auth" "hyperchat-stepup hyperchat_stepup"; do
  set -- $m
  awk -v u="$1" -v r="$2" '$1 == "hyperchat" && $2 == u && $3 == r {f=1} END {exit !f}' "$F" \
    || printf 'hyperchat   %-17s %s\n' "$1" "$2" >> "$F"
done
grep -nE '^hyperchat[[:space:]]' "$F"
pg_ctlcluster 15 restoretest reload
```
- **The `hyperchat-stepup` line is harmless** before that OS user exists. The isolation audit requires that only that user maps to `hyperchat_stepup`.
- **Never add** a `hyperchat-prod` line here, or any DEV line on the production cluster.

**C4. `api.env` settings** (names from `deploy/api.env.example`; no secret value). Each key is added only if absent. An existing key with another value is a STOP:
```
E=/etc/hyperchat/api.env
for kv in HYPERCHAT_DB_LIFECYCLE_USER=hyperchat_lifecycle HYPERCHAT_DB_AUTH_USER=hyperchat_auth \
          HYPERCHAT_ADMIN_SESSION_IDLE_HOURS=2 HYPERCHAT_ADMIN_SESSION_ABSOLUTE_HOURS=12 \
          HYPERCHAT_KYC_WITNESS_PATH=/var/lib/hyperchat-kyc/dev.witness; do
  k="${kv%%=*}"
  if grep -q "^$k=" "$E"; then grep -qx "$kv" "$E" || echo "STOP: $k already set to another value"; else printf '%s\n' "$kv" >> "$E"; fi
done
stat -c '%U:%G %a' "$E"     # root:hyperchat 640, unchanged by appending
grep -cE '^HYPERCHAT_(STEPUP_SOCKET|DIDIT_)' "$E"    # 0: no verifier socket, no Didit (KYC sessions answer 503)
```

**C5. Verify** (read-only):
```
QP "SELECT rolname FROM pg_roles WHERE rolname LIKE 'hyperchat%' ORDER BY 1"
QP "SELECT r, has_database_privilege(r, 'hyperchat_dev', 'CONNECT') FROM unnest(ARRAY['hyperchat_lifecycle','hyperchat_auth','hyperchat_stepup']) r"
bash "$STAGE/deploy/check_isolation.sh" dev; echo "exit=$?"     # every line OK, exit=0
curl -fsS http://127.0.0.1:8000/health                          # the 0019 release still answers
```
**Record:** the pg_hba diff, `setup_cluster.sh` output, the pg_ident lines, the `api.env` mode, `check_isolation.sh dev` (all `OK`), `/health`.

---

## Phase D: The migration rehearsal (approval: "Phase D", with a time window)
DEV is unavailable for the window. Expect minutes.

**D1. Pre-flight.** Every line must print the expected value. Otherwise STOP before anything is stopped.
```
readlink -f /opt/hyperchat/current                                  # = $REL_OLD
Q "SELECT version_num FROM alembic_version"                         # 0019
test -s "$ST/phaseB.set" && ls -l /var/backups/hyperchat-dev/"$(cat "$ST/phaseB.set")".*   # the Phase B set
grep -q '"structurally_valid": true' "$ST/restorecheck-0019.json" && echo restorecheck-ok
stat -c %s /var/lib/hyperchat-kyc/dev.witness; lsattr /var/lib/hyperchat-kyc/dev.witness  # 0; 'a' flag
grep -q '^HYPERCHAT_BOTS_ENABLED=true' /etc/hyperchat/api.env && echo "STOP: bots enabled"
baseline > "$ST/baseline-before.txt"; cat "$ST/baseline-before.txt"   # streamers|133|0, signups|233|…
```

**D2. Stop DEV services** (reverse of the recovery order in `docs/backup-recovery.md`):
```
systemctl stop hyperchat-cleanup.timer; systemctl stop hyperchat-worker; systemctl stop hyperchat-api
systemctl is-active hyperchat-api hyperchat-worker hyperchat-cleanup.timer hyperchat-cleanup.service   # all "inactive"
```

**D3. Migrate one revision at a time. Fail-stop.** Define the step function in this session:
```
migrate_one() {   # $1 = the revision; succeeds only when alembic succeeded AND the database is exactly at $1
  ( cd "$REL_NEW/database" && HYPERCHAT_MIGRATION_DSN="postgresql+psycopg://hyperchat_migrator@/hyperchat_dev?host=/var/run/postgresql&port=5433" \
      ../venv/bin/alembic upgrade "$1" ) || { echo "STOP: alembic upgrade $1 failed"; return 1; }
  local now; now="$(Q 'SELECT version_num FROM alembic_version')" || { echo "STOP: cannot read alembic_version"; return 1; }
  [ "$now" = "$1" ] || { echo "STOP: database at '$now', expected $1"; return 1; }
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

**D3 gate.** It writes the marker that D4 requires, and only when everything holds:
```
[ "$(Q 'SELECT version_num FROM alembic_version')" = 0028 ] \
  && [ "$(Q "SELECT state || '|' || generation FROM kyc_enforcement")" = "draft|0" ] \
  && [ "$(Q 'SELECT state FROM stepup_enforcement')" = draft ] \
  && [ "$(stat -c %s /var/lib/hyperchat-kyc/dev.witness)" = 0 ] \
  && date -u +%FT%TZ > "$ST/D3-passed" && echo "D3 PASSED" || echo "STOP: D3 gate failed; do not switch the release"
```

**D4. Switch the release and start the services, only behind the gate.** One compound command, so a failed gate cannot be followed by a switch:
```
if [ -f "$ST/D3-passed" ] && [ "$(Q 'SELECT version_num FROM alembic_version')" = 0028 ]; then
  ln -sfn "$REL_NEW" /opt/hyperchat/current && systemctl start hyperchat-api && echo "API started on $(readlink -f /opt/hyperchat/current)"
else
  echo "STOP: gate not passed; services stay stopped"
fi
```
Then, only after `API started …`:
```
for i in $(seq 1 24); do curl -fsS http://127.0.0.1:8000/health && break; sleep 5; done   # ok, release c2fe883be71d
systemctl start hyperchat-worker && systemctl start hyperchat-cleanup.timer
systemctl is-active hyperchat-api hyperchat-worker hyperchat-cleanup.timer
journalctl -u hyperchat-api -u hyperchat-worker --since "-5 min" --no-pager | grep -iE "error|critical|refus" | head
```
Don't start the cleanup service by hand. Its timer runs it daily with the same `api.env`, witness path included.

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

**Enter the stopped state:**
```
systemctl stop hyperchat-cleanup.timer; systemctl stop hyperchat-worker; systemctl stop hyperchat-api
systemctl is-active hyperchat-api hyperchat-worker hyperchat-cleanup.timer      # all "inactive"
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
At `c2fe883`, `tests/e2e/smoke_dev_backend.py` knows migration heads only up to `0024` (`KNOWN_HEADS`), and it has no MFA / step-up section. **A failing head check is never accepted as part of a successful rehearsal.** Before Phase E, a separate, reviewed and merged **test-only PR** must:
- add `0025`–`0028` to `KNOWN_HEADS`;
- add an MFA section:
  - `stepup_enforcement` is `draft`;
  - `GET /api/v1/auth/mfa/status` and `POST /api/v1/auth/mfa/registration/options` as an admin answer `503` with reason `stepup_not_configured` while no verifier is installed;
  - anonymous and non-admin callers are refused;
  - ordinary admin routes still answer;
  - admin session expiry is at most 12 h after sign-in;
- change only files under `tests/`, so the deployed `c2fe883` code is unaffected.

Phase E then uses a root-only copy of that PR's merge commit. Set `SMOKE_SHA` to its full 40-character SHA first.

**(workstation)**
```
SMOKE_SHA=   # the merge commit of the smoke PR, 40 hex characters
[ "${#SMOKE_SHA}" = 40 ] || echo "STOP: set SMOKE_SHA"
git diff --stat c2fe883be71d1a494232d4147ed4d6fbc4e70eef "$SMOKE_SHA" -- backend database deploy   # must print nothing
git archive "$SMOKE_SHA" backend tests | ssh root@VPS "install -d -o root -g root -m 0700 /root/hyperchat-smoke && tar -x -C /root/hyperchat-smoke"
```
**Record:** `SMOKE_SHA` and the empty `git diff --stat` output.

### What must pass
- **Mandatory: PASS.** Every runnable check of both suites, including the migration-head check, plus every E2 and E3 row below.
- **Allowed: BLOCKED.** Only the third-party integrations or approvals that are deliberately unavailable on DEV:
  - email delivery (Resend);
  - Google sign-in;
  - TTS and transcription providers (MiniMax, ElevenLabs, Sarvam);
  - Didit;
  - the live Razorpay probe (D7);
  - R2-writing sections (`HYPERCHAT_SMOKE_R2=1` not approved);
  - bots (disabled).
- **Fails Phase E:** any FAIL, or a BLOCKED outside that list.

### E1. The smoke suites
```
cd /root/hyperchat-smoke
/opt/hyperchat/current/venv/bin/python tests/e2e/smoke_dev_backend.py; echo "exit=$?"     # exit=0
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
env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HYPERCHAT_BACKUP_ENV=dev bash "$STAGE/deploy/backup/hyperchat-pgbackup.sh"; echo "exit=$?"
ls -1t /var/backups/hyperchat-dev/*.manifest.json | head -n 1 | xargs -r basename | sed 's/\.manifest\.json$//' > "$ST/phaseE.set"
( cd "$REL_NEW/backend" && ../venv/bin/python -m app.backup.restore_check --env dev --dir /var/backups/hyperchat-dev \
    --set "$(cat "$ST/phaseE.set")" --pg-bin /usr/lib/postgresql/15/bin --scratch --scratch-user postgres \
    --output "$ST/restorecheck-0028.json" ); echo "exit=$?"
```
**Required:**
- the backup prints `exit=0`, and its manifest records alembic `0028` and KYC `draft`;
- the restore check prints `exit=0`;
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
| Admin authentication through `hyperchat_auth` | `smoke_auth.py` passes: admin and ordinary sign-in, sign-out, password change. Admin sign-in can only succeed through `hyperchat_auth` once 0024 is in place |
| Admin session limits | expiry at most 12 h after sign-in |
| Step-up fails safe | MFA endpoints `503` `stepup_not_configured`, a `stepup_unavailable` security event, ordinary admin routes answer (enforcement `draft`) |
| Applications and lifecycle | sections `applications` and `creator-lifecycle` pass (no longer BLOCKED) |
| KYC | section `kyc`: enforcement `draft`/0, witness agrees; KYC session start answers 503 (Didit not configured; an expected result, not a BLOCKED) |
| Payments and webhooks | sections `payments` (checkout validation; a Razorpay webhook with a bad signature is refused), `payments-account` (D6), `payment-health` (D7 route and CLI; the live probe may be BLOCKED). No order is created. Webhook duplicate and replay handling is covered by CI (`backend/tests/payments`), not by this smoke |
| OBS, realtime, worker | sections `obs`, `realtime`, `worker` pass; the worker log shows the synthetic job processed |
| DEV/production separation | `curl -fsS http://127.0.0.1:8000/health` reports development and `hyperchat_dev`; read-only: `curl -fsS http://127.0.0.1:8001/health` still reports production release `940137070657`, and `sudo -u postgres psql -X -p 5432 -d hyperchat -Atc "SELECT version_num FROM alembic_version"` still `0009` |

**Record:** both suites' totals (passed / failed / blocked, every BLOCKED named and on the allowed list), the E2 outputs, `$ST/restorecheck-0028.json`, `check_isolation.sh` output. Phase E passes only when every mandatory item passes. Otherwise, "If a phase fails".

---

## Phase F: Rollback (approval: "Phase F"; only after a failure and the owner's decision)
**Goal:** DEV back on release `229fe42381e2` with a database at 0019, the failed database kept for investigation, and **the KYC witness untouched**. Start with the session setup and the stopped state.

**F1. Preconditions:**
```
systemctl is-active hyperchat-api hyperchat-worker hyperchat-cleanup.timer      # all "inactive"; otherwise enter the stopped state first
stat -c %s /var/lib/hyperchat-kyc/dev.witness; lsattr /var/lib/hyperchat-kyc/dev.witness   # record; compared at the end
QP "SELECT datname FROM pg_database WHERE datname IN ('hyperchat_dev', '$RESTORE_DB', '$FAILED_DB') ORDER BY 1"
Q "SELECT version_num FROM alembic_version"
```

**F2. Choose the path:**
- **Database still at `0019`** (the failure was before 0020 committed, or in the release switch with no revision applied): the database is unchanged. Go to F5 (release only).
- **Any later revision committed:** restore with F3–F4. Never downgrade.

**F3. Restore the Phase B set into a new, unused database and verify it.** Nothing here touches `hyperchat_dev`.
```
SET_NAME="$(cat "$ST/phaseB.set")"; SET_PATH="/var/backups/hyperchat-dev/$SET_NAME"; echo "$SET_NAME"
( cd /var/backups/hyperchat-dev && sha256sum -c "$SET_NAME.dump.sha256" ) || echo "STOP: set checksum"
[ -z "$(QP "SELECT 1 FROM pg_database WHERE datname IN ('$RESTORE_DB', '$FAILED_DB')")" ] && echo names-free || echo "STOP: $RESTORE_DB or $FAILED_DB already exists"
QP "CREATE DATABASE \"$RESTORE_DB\" OWNER hyperchat_owner"
QP "REVOKE ALL ON DATABASE \"$RESTORE_DB\" FROM PUBLIC"
sudo -u postgres /usr/lib/postgresql/15/bin/pg_restore -p 5433 --exit-on-error --single-transaction -d "$RESTORE_DB" < "$SET_PATH.dump"; echo "exit=$?"
```
- **Why stdin:** root opens the dump and passes it on stdin, because the backup directory is root 0700 and the `postgres` user cannot read it.
- **If the restore fails:** `$RESTORE_DB` is left empty or partial. STOP and record. It may be dropped only by separate decision. `hyperchat_dev` is unchanged.

Verify the restored database **before any rename**:
```
QD "$RESTORE_DB" "SELECT version_num FROM alembic_version"          # 0019
baseline "$RESTORE_DB" > "$ST/baseline-restored.txt"; diff "$ST/baseline-before.txt" "$ST/baseline-restored.txt" && echo restored-matches-baseline
```
- **A difference** means DEV data changed between the Phase B backup and the Phase D baseline. STOP, record the diff, and let the owner decide whether that loss is acceptable before F4.
- **If 0.4 recorded per-database settings:** apply each one now to `$RESTORE_DB` with `ALTER DATABASE … SET …`, written out from the 0.4 record and reviewed before it runs. With no rows in 0.4 (the expected case), skip this.

**F4. Swap the names.** These are **two separate statements, not atomic**. Between them, no database is named `hyperchat_dev`, which is safe only because every DEV service is stopped.
```
QP "SELECT datname, count(*) FROM pg_stat_activity WHERE datname IN ('hyperchat_dev', '$RESTORE_DB') GROUP BY 1"   # no rows: no connections
QP "ALTER DATABASE \"hyperchat_dev\" RENAME TO \"$FAILED_DB\"" && echo rename1-ok || echo "STOP: rename 1 failed; nothing changed"
QP "ALTER DATABASE \"$RESTORE_DB\" RENAME TO \"hyperchat_dev\"" && echo rename2-ok || echo "STOP: rename 2 failed; see below"
QP "SELECT datname FROM pg_database WHERE datname IN ('hyperchat_dev', '$RESTORE_DB', '$FAILED_DB') ORDER BY 1"
```
- **Rename 1 fails** (usually a remaining connection): nothing changed. Find the connection with the `pg_stat_activity` query, then retry within Phase F.
- **Rename 2 fails after rename 1 succeeded:** the state is `$FAILED_DB` plus `$RESTORE_DB`, with **no `hyperchat_dev`**. Either:
  - retry rename 2 once its cause is cleared (e.g. a connection to `$RESTORE_DB`); or
  - undo rename 1 to return to the stopped state: `QP "ALTER DATABASE \"$FAILED_DB\" RENAME TO \"hyperchat_dev\""`.

  Never start a service while no `hyperchat_dev` exists.
- **After rename 2:** restore the database-level access as `setup_cluster.sh` sets it:
  ```
  QP "REVOKE ALL ON DATABASE \"hyperchat_dev\" FROM PUBLIC"
  QP "GRANT CONNECT ON DATABASE \"hyperchat_dev\" TO hyperchat_migrator, hyperchat_app, hyperchat_worker, hyperchat_readonly, hyperchat_lifecycle, hyperchat_auth, hyperchat_stepup"
  Q "SELECT version_num FROM alembic_version"                                    # 0019
  baseline > "$ST/baseline-rolledback.txt"; diff "$ST/baseline-restored.txt" "$ST/baseline-rolledback.txt" && echo rollback-db-ok
  ```
- **The failed database** `hyperchat_dev_failed_rehearsal` stays for investigation, untouched. Dropping it is a later, separate approval. No service refers to it by name.

**F5. Release back to `229fe42381e2` and start**, only with the database at 0019:
```
if [ "$(Q 'SELECT version_num FROM alembic_version')" = 0019 ]; then
  ln -sfn "$REL_OLD" /opt/hyperchat/current && systemctl start hyperchat-api && echo "API started on $(readlink -f /opt/hyperchat/current)"
else
  echo "STOP: hyperchat_dev is not at 0019; services stay stopped"
fi
```
Then, only after `API started …`:
```
for i in $(seq 1 24); do curl -fsS http://127.0.0.1:8000/health && break; sleep 5; done   # release 229fe42381e2
systemctl start hyperchat-worker && systemctl start hyperchat-cleanup.timer
```
- **`api.env`:** keep the Phase C settings. The 0019 release ignores them (`extra="ignore"`), and keeping `HYPERCHAT_KYC_WITNESS_PATH` costs nothing. Returning to `$ST/api.env.before-C` is a separate decision.
- **Roles, pg_hba and pg_ident from Phase C stay.** They are additive and unused by the 0019 release. Reverting them is a separate decision (copy the `before-C` files back, then `pg_ctlcluster 15 restoretest reload`).

**F6. KYC witness: mandatory checks.**
```
stat -c %s /var/lib/hyperchat-kyc/dev.witness; lsattr /var/lib/hyperchat-kyc/dev.witness   # identical to F1
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
| F | restoring the Phase B set into `hyperchat_dev_rollback_restore`, the two renames (or undoing rename 1), CONNECT grants, switching back to `229fe42381e2` | dropping either kept database, reverting roles / pg_hba / pg_ident / `api.env` |

## After a successful rehearsal (each separately)
- The A3 install for both environments: the script at `/usr/local/sbin`, the units, and a production witness. Production's backup changes there, so it needs its own approval.
- Decide on the `hyperchat` role (0.1).
- Install the step-up verifier on DEV (`docs/admin-stepup.md` "Deployment sequence" step 4).
- Didit sandbox verification (`docs/kyc-rollout.md` step 3).
- Production planning (0010→0028), which this rehearsal does not cover.
