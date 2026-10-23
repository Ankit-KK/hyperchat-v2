# DEV migration rehearsal: 0019 → 0028

The controlled rehearsal of migrations 0020–0028 on the **DEV** environment, with the matching application release.
**Nothing in this document has been run.** Every phase below needs its own explicit owner approval, and approving one
phase never authorizes the next.

## Scope and fixed rules
- **Environment:** DEV only.
  - API `api.quicktestspace.site`, database `hyperchat_dev` on cluster `15/restoretest`, port 5433.
  - OS user `hyperchat`; units `hyperchat-api`, `hyperchat-worker`, `hyperchat-cleanup.timer`.
  - Production (`17/hyperchat`, `hyperchat-prod*`, `/etc/hyperchat-prod`, `/var/backups/hyperchat`) is never touched, except for the read-only checks in Phase 0.
- **Target:** `main` at `c2fe883be71d1a494232d4147ed4d6fbc4e70eef`, Alembic head `0028`.
- **Never in this rehearsal:**
  - activating KYC enforcement (`app.kyc.operator activate`) or MFA enforcement;
  - installing the step-up verifier;
  - configuring Didit, enabling bots, R2 writes outside the approval-gated smoke section, Razorpay orders, or any credential change.
- **Enforcement stays `draft`:** both KYC and MFA enforcement remain `draft` throughout. The rehearsal stops if either reads anything else.
- **Output:** every phase ends with a short written record: what ran, the outputs named under "Record", and any deviation. The owner reads it before approving the next phase.

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
| Backups | manual pre-deploy dumps only (newest `hyperchat_dev_pre_main229fe42_20261015T045216Z.dump`) | old-layout nightly script; result not yet checked |
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

## Migrations in this rehearsal
`transaction_per_migration=True` (`database/migrations/env.py`): each revision commits on its own, with `lock_timeout` 10 s and `statement_timeout` 5 min, as `hyperchat_migrator` with `SET ROLE hyperchat_owner`. A failure leaves the previous revision in place.

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

**Practical rollback is a restore, not a downgrade** (Phase F). Downgrades exist but refuse once data depends on them, and a partial downgrade path is not rehearsed.

---

## Phase 0: Inspection gaps (read-only; approval: read-only VPS access)
These three checks close the gaps of the 2026-10-23 inspection. They change nothing. **Phase C may not start until 0.1 is answered.**

### 0.1 The unexpected DEV database role `hyperchat`
`roles.sql` doesn't create a role named `hyperchat`, and no repository file maps or uses one. Find out what it is before `setup_cluster.sh` runs `REVOKE ALL ON DATABASE hyperchat_dev FROM PUBLIC` (Phase C), which could cut off anything that silently depends on it.
```
sudo -u postgres psql -p 5433 -Atc "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit, rolreplication, rolbypassrls, rolconnlimit, rolvaliduntil FROM pg_roles WHERE rolname = 'hyperchat'"
sudo -u postgres psql -p 5433 -Atc "SELECT rolpassword IS NOT NULL AS has_password FROM pg_authid WHERE rolname = 'hyperchat'"
sudo -u postgres psql -p 5433 -Atc "SELECT g.rolname AS granted_role, m.rolname AS member FROM pg_auth_members a JOIN pg_roles g ON g.oid = a.roleid JOIN pg_roles m ON m.oid = a.member WHERE 'hyperchat' IN (g.rolname, m.rolname)"
sudo -u postgres psql -p 5433 -Atc "SELECT datname, pg_get_userbyid(datdba) AS owner, datacl FROM pg_database ORDER BY 1"
for db in hyperchat_dev hyperchat_restore_test postgres; do sudo -u postgres psql -p 5433 -d "$db" -Atc "SELECT '$db', count(*) FROM pg_class WHERE relowner = 'hyperchat'::regrole"; done
sudo -u postgres psql -p 5433 -Atc "SELECT count(*) FROM pg_stat_activity WHERE usename = 'hyperchat'"
grep -n "hyperchat[^_-]" /etc/postgresql/15/restoretest/pg_hba.conf /etc/postgresql/15/restoretest/pg_ident.conf
```
**Record:** attributes, whether it can log in or has a password, memberships, owned objects per database, database ACLs, live sessions.
- **Decision (owner):** keep, document, or drop it.
- **Dropping is a separate approved change,** never part of this rehearsal.
- **If it owns objects in `hyperchat_dev` or `hyperchat_restore_test`, or has a live session:** stop and decide before Phase C.

### 0.2 Production backup: exit status, archive and integrity
A timer that fired doesn't mean a backup succeeded. Check the last run's result and the newest archive itself:
```
systemctl show hyperchat-pgbackup.service -p Result -p ExecMainStatus -p ExecMainStartTimestamp -p ExecMainExitTimestamp
systemctl cat hyperchat-pgbackup.service | grep -E '^(ExecStart|Environment)='
journalctl -u hyperchat-pgbackup.service --since "3 days ago" --no-pager | tail -n 30
ls -lt /var/backups/hyperchat | head -n 10
N=$(ls -t /var/backups/hyperchat/hyperchat_*.dump | head -n 1); echo "$N"; stat -c '%s bytes, %y' "$N"
cd /var/backups/hyperchat && sha256sum -c "$(basename "$N").sha256"
/usr/lib/postgresql/17/bin/pg_restore -l "$N" | grep -c '^[0-9]'
```
**Record:**
- `Result=success` and `ExecMainStatus=0`;
- the newest archive's name, time (today's 02:37 UTC run) and size;
- `sha256sum -c` → `OK`;
- a non-zero TOC entry count from `pg_restore -l`.

Any other answer is a production finding, reported to the owner. The DEV rehearsal doesn't depend on it, but production's backup must not be assumed healthy.

### 0.3 Isolation audit behaviour today
The deployed DEV release has no `check_isolation.sh`. Run it from a **root-only staging copy** of the target commit, which Phase A also uses. Making the copy is a write under `/root`, so it belongs to Phase A. If Phase A isn't approved yet, run this check after it.
```
bash /root/hyperchat-c2fe883/deploy/check_isolation.sh dev
bash /root/hyperchat-c2fe883/deploy/check_isolation.sh prod
```
- **Expected today:** `FAIL` lines for the pg_hba rule (lifecycle, auth and stepup not covered) and the pg_ident map (API user not mapped to lifecycle and auth), on both environments. File-ownership and unit lines should be `OK`.
- **Record:** the full output. Any FAIL outside pg_hba / pg_ident (release tree, `api.env`, units, service account) must be understood before Phase C. The script needs `namei` (util-linux) and changes nothing (`deploy/check_isolation.sh` header).

---

## Phase A: DEV backup infrastructure (approval: "Phase A")
Prepares the tools. Changes no database and no running service.

**A1. Staging copy of the target commit**, root-only. It provides the scripts, units and smoke suite, and is never run as a service:
```
git archive c2fe883be71d1a494232d4147ed4d6fbc4e70eef backend database deploy tests \
  | ssh root@VPS "install -d -o root -g root -m 0700 /root/hyperchat-c2fe883 && tar -x -C /root/hyperchat-c2fe883"
```

**A2. Build the target release**, without switching `current` ("Deploy a commit" steps 1–3; `deploy/README.md`):
```
git archive c2fe883be71d1a494232d4147ed4d6fbc4e70eef backend database \
  | ssh root@VPS "mkdir -p /opt/hyperchat/releases/c2fe883be71d && tar -x -C /opt/hyperchat/releases/c2fe883be71d"
R=/opt/hyperchat/releases/c2fe883be71d
python3.14 -m venv $R/venv && $R/venv/bin/pip install -r $R/backend/requirements.txt
echo c2fe883be71d > $R/backend/RELEASE
chown -R root:hyperchat $R && chmod -R g+rX,g-w,o-rwx $R
$R/venv/bin/python -c "import app.main" && echo import-ok   # run from $R/backend
```
The release directory name follows the existing 12-character convention (`229fe42381e2`).

**A3. Install the backup script and the DEV units** (`docs/backup-recovery.md` "Installation"):
```
install -o root -g root -m 750 /root/hyperchat-c2fe883/deploy/backup/hyperchat-pgbackup.sh /usr/local/sbin/hyperchat-pgbackup
install -o root -g root -m 644 /root/hyperchat-c2fe883/deploy/backup/hyperchat-dev-pgbackup.service \
  /root/hyperchat-c2fe883/deploy/backup/hyperchat-dev-pgbackup.timer /etc/systemd/system/
systemctl daemon-reload
install -d -o root -g root -m 0700 /var/backups/hyperchat-dev
```
- **Do not install** the production backup units, and don't change the production timer: its upgrade is a separate production approval.
- **The script and production:** `/usr/local/sbin/hyperchat-pgbackup` is also the path the A3 production unit uses. Check with `systemctl cat hyperchat-pgbackup.service` (0.2) that the production unit doesn't already execute that path. If it does, stop: installing the new script would change production's backup.
- **Don't enable the DEV timer yet.** Enabling it (`systemctl enable --now hyperchat-dev-pgbackup.timer`) is a separate decision after Phase B.

**A4. Create the DEV KYC witness** (`docs/kyc-rollout.md` "Server setup"), so that the backup in Phase B carries it:
```
install -d -o root -g root -m 0755 /var/lib/hyperchat-kyc
install -o hyperchat -g hyperchat -m 0600 /dev/null /var/lib/hyperchat-kyc/dev.witness
chattr +a /var/lib/hyperchat-kyc/dev.witness
lsattr /var/lib/hyperchat-kyc/dev.witness        # must show the 'a' flag
```
- **An empty file is a fresh witness.** At 0019 nothing reads it.
- **After 0023:** the API appends a line only when enforcement history exists (`KycWitness.record` skips generation 0), so it stays empty throughout this rehearsal.
- **Never remove it, truncate it or `chattr -a` it,** not even during rollback (Phase F).

**Record:** A1–A4 outputs, `import-ok`, `lsattr` output, the release's directory listing and ownership.

---

## Phase B: Fresh backup and disposable restore test (approval: "Phase B")
**B1. A verified DEV set** with the installed script, DEV profile:
```
systemctl start hyperchat-dev-pgbackup.service
journalctl -u hyperchat-dev-pgbackup.service -n 20 --no-pager      # must end "backup ok env=dev db=hyperchat_dev ..."
systemctl show hyperchat-dev-pgbackup.service -p Result -p ExecMainStatus   # success / 0
ls -l /var/backups/hyperchat-dev
```
- **Exit codes:** 0 ok; 3 means the witness was missing (A4 not done: stop); 2 means a refused configuration; 1 means failed. Any of these stops Phase B.
- **Content:** the set `hyperchat_dev_<UTC>.{dump,dump.sha256,witness,manifest.json}` records `alembic_version` 0019 in its manifest.

**B2. Restore it into a disposable PostgreSQL 15** (`docs/backup-recovery.md` "Verifying a set"). Run it from the new release, which contains `app.backup`. Because the set is at 0019, pass the set's own head; the default would be this checkout's 0028.
```
cd /opt/hyperchat/releases/c2fe883be71d/backend
../venv/bin/python -m app.backup.restore_check --env dev --dir /var/backups/hyperchat-dev \
  --set hyperchat_dev_<UTC> --pg-bin /usr/lib/postgresql/15/bin --scratch --scratch-user postgres \
  --expect-head 0019 --output /root/restorecheck-dev-0019.json
```
**Required:**
- exit 0;
- `structurally_valid: true`;
- `restore_valid: true`;
- `invariants.alembic_version: "0019"`;
- `kyc_witness` `consistent` or `not_applicable`;
- `live_operation_approved: false`, which is always the case.

The scratch cluster lives in a temporary directory with no TCP and is deleted afterwards. No live database is touched.

- **Old-release invariants:** `invariants` checks objects that exist at 0019 only (step-up and KYC checks apply when their tables exist). A leak or ownership finding at 0019 is a pre-existing condition. Record it and stop.

**B3. Keep the earlier pre-deploy dump as a second fallback.** `hyperchat_dev_pre_main229fe42_20261015T045216Z.dump` is older, so restoring it loses 8 days of DEV changes. Use it only if the B1 set is unusable.

**Record:** the set name, `sha256` from the manifest, the JSON result file.

---

## Phase C: Roles, authentication rules, environment (approval: "Phase C"; requires 0.1 answered)
These changes are compatible with the running 0019 release. New roles and map lines are unused by it, and new `api.env` settings are ignored by it. Nothing is restarted.

**C1. Back up what changes:**
```
TS=$(date -u +%Y%m%dT%H%M%SZ)
cp -p /etc/postgresql/15/restoretest/pg_hba.conf   /root/pg_hba.conf.dev.bak-$TS
cp -p /etc/postgresql/15/restoretest/pg_ident.conf /root/pg_ident.conf.dev.bak-$TS
cp -p /etc/hyperchat/api.env /root/api.env.dev.bak-$TS
```

**C2. Roles, CONNECT grants and the pg_hba rule.** Run `setup_cluster.sh`, which is idempotent. It:
- applies `roles.sql` (the three new roles);
- syncs the HyperChat pg_hba rule (keeping its own `.bak-<timestamp>`);
- reloads the cluster;
- runs `REVOKE ALL ON DATABASE hyperchat_dev FROM PUBLIC`;
- grants `CONNECT` to all seven HyperChat login roles.

Preview the pg_hba change first:
```
bash /root/hyperchat-c2fe883/deploy/postgres/sync_pg_hba.sh /root/hyperchat-c2fe883/deploy/postgres/pg_hba.conf \
  /etc/postgresql/15/restoretest/pg_hba.conf | diff /etc/postgresql/15/restoretest/pg_hba.conf -
bash /root/hyperchat-c2fe883/deploy/postgres/setup_cluster.sh 15 restoretest 5433 hyperchat_dev
```
Name only `hyperchat_dev`. Never name `hyperchat_restore_test`, so its grants are untouched.

**C3. pg_ident lines** (`setup_cluster.sh` appends the map only when none exists, so DEV needs them added). Append exactly the lines of `deploy/postgres/pg_ident.conf` that are missing:
```
hyperchat   root              hyperchat_auth
hyperchat   hyperchat         hyperchat_lifecycle
hyperchat   hyperchat         hyperchat_auth
hyperchat   hyperchat-stepup  hyperchat_stepup
```
Then reload:
```
pg_ctlcluster 15 restoretest reload
```
- **The `hyperchat-stepup` line is harmless** before that OS user exists, and the isolation audit expects only that user to map to `hyperchat_stepup`.
- **Never add** a `hyperchat-prod` line to this file, or any DEV line to the production cluster.

**C4. `api.env`**: add exactly these settings (names from `deploy/api.env.example`; no secret value involved):
```
HYPERCHAT_DB_LIFECYCLE_USER=hyperchat_lifecycle
HYPERCHAT_DB_AUTH_USER=hyperchat_auth
HYPERCHAT_ADMIN_SESSION_IDLE_HOURS=2
HYPERCHAT_ADMIN_SESSION_ABSOLUTE_HOURS=12
HYPERCHAT_KYC_WITNESS_PATH=/var/lib/hyperchat-kyc/dev.witness
```
- **Leave unset:** `HYPERCHAT_STEPUP_SOCKET` (no verifier: the passkey endpoints answer 503 `stepup_not_configured`) and every `HYPERCHAT_DIDIT_*` (KYC sessions answer 503).
- **Keep ownership and mode:** `root:hyperchat`, 0640.

**C5. Verify** (read-only):
```
sudo -u postgres psql -p 5433 -Atc "SELECT rolname FROM pg_roles WHERE rolname LIKE 'hyperchat%' ORDER BY 1"
sudo -u postgres psql -p 5433 -Atc "SELECT has_database_privilege(r, 'hyperchat_dev', 'CONNECT') FROM unnest(ARRAY['hyperchat_lifecycle','hyperchat_auth','hyperchat_stepup']) r"
bash /root/hyperchat-c2fe883/deploy/check_isolation.sh dev      # must print only OK (release tree and api.env included)
curl -fsS http://127.0.0.1:8000/health                          # the 0019 release still answers
```
**Record:** the diff, `setup_cluster.sh` output, `check_isolation.sh dev` output (all `OK`), `/health`.

---

## Phase D: The migration rehearsal (approval: "Phase D", with a time window)
DEV is unavailable for the window. Expect minutes.

**D1. Pre-flight** (stop if any fails):
```
readlink -f /opt/hyperchat/current            # /opt/hyperchat/releases/229fe42381e2
sudo -u postgres psql -p 5433 -d hyperchat_dev -Atc "SELECT version_num FROM alembic_version"     # 0019
ls -l /var/backups/hyperchat-dev                # the Phase B set exists; its restore check passed
lsattr /var/lib/hyperchat-kyc/dev.witness       # 'a' flag; size 0
```
**Baseline fingerprints** (record all; compared in Phase E):
```
Q() { sudo -u postgres psql -p 5433 -d hyperchat_dev -Atc "$1"; }
Q "SELECT count(*), count(*) FILTER (WHERE user_id IS NULL) FROM streamers"                         # 133 | 0
Q "SELECT md5(string_agg(id::text || ':' || coalesce(user_id::text,'-') || ':' || streamer_slug || ':' || coalesce(status,'-'), ',' ORDER BY id)) FROM streamers"
Q "SELECT count(*), md5(string_agg(id::text || ':' || lower(email), ',' ORDER BY id)) FROM user_signups"   # 233 | …
Q "SELECT count(*), count(*) FILTER (WHERE payment_status = 'success'), coalesce(sum(amount_inr) FILTER (WHERE payment_status = 'success'), 0) FROM donations"
Q "SELECT count(*) FROM users"; Q "SELECT role, count(*) FROM users GROUP BY 1 ORDER BY 1"
Q "SELECT count(*) FILTER (WHERE revoked_at IS NULL) FROM obs_tokens"
Q "SELECT count(*) FROM soundboard_tnc_acceptances"
```
If any table or column name differs on the server, record the error and adapt the query read-only. Never modify data to make a query work.

**D2. Stop DEV services** (reverse of the recovery order in `docs/backup-recovery.md`):
```
systemctl stop hyperchat-cleanup.timer
systemctl stop hyperchat-worker
systemctl stop hyperchat-api
systemctl is-active hyperchat-api hyperchat-worker hyperchat-cleanup.service   # all inactive
```
Bots are disabled on DEV (no webhook registered). If `HYPERCHAT_BOTS_ENABLED=true` is found in `api.env`, stop: the rehearsal assumes bots off.

**D3. Migrate one revision at a time**, with the target release's Alembic, as root (peer-mapped to `hyperchat_migrator`):
```
cd /opt/hyperchat/releases/c2fe883be71d/database
export HYPERCHAT_MIGRATION_DSN="postgresql+psycopg://hyperchat_migrator@/hyperchat_dev?host=/var/run/postgresql&port=5433"
for rev in 0020 0021 0022 0023 0024 0025 0026 0027 0028; do
  ../venv/bin/alembic upgrade "$rev" || { echo "STOP at $rev"; break; }
  ../venv/bin/alembic current
done
```
Run the loop body by hand if the operator prefers to check between revisions. Check after each revision:

| After | Check (read-only) | Expect |
|---|---|---|
| 0020 | `Q "SELECT kyc_track, count(*), count(*) FILTER (WHERE kyc_baseline_owner_id IS NOT DISTINCT FROM user_id) FROM streamers GROUP BY 1"` | `legacy | 133 | 133` |
| 0020 | `Q "SELECT count(*) FROM streamers WHERE public.creator_donation_block(id) IS NULL"` | the number of creators that accepted donations before (every `active` creator) |
| 0021 | `Q "SELECT review_status, count(*) FROM user_signups GROUP BY 1"` | `legacy_unreviewed | 233` (plus any signup submitted after the inspection, also `legacy_unreviewed`) |
| 0023 | `Q "SELECT state, generation FROM kyc_enforcement"` | `draft | 0` |
| 0025 | `Q "SELECT state FROM stepup_enforcement"` | `draft` |
| 0028 | `Q "SELECT version_num FROM alembic_version"` | `0028` |

**D4. Switch the release and start the services in recovery order:**
```
ln -sfn /opt/hyperchat/releases/c2fe883be71d /opt/hyperchat/current
systemctl start hyperchat-api
curl -fsS http://127.0.0.1:8000/health          # release c2fe883be71d, database ok
systemctl start hyperchat-worker
systemctl start hyperchat-cleanup.timer
systemctl is-active hyperchat-api hyperchat-worker hyperchat-cleanup.timer
journalctl -u hyperchat-api -u hyperchat-worker --since "-5 min" --no-pager | grep -iE "error|critical|refus" | head
```
Don't start the cleanup service by hand. Its timer runs it daily, and it uses the same `api.env` (witness path included).

### Stop conditions (go to Phase F; never improvise)
- Any Phase D command exits non-zero, or a check above differs from "Expect".
- A migration exceeds its lock or statement timeout (the revision rolls back by itself; `alembic current` shows the previous one).
- `/health` isn't `ok` within 2 minutes of the start, or the API or worker restarts in a loop.
- The KYC or MFA enforcement state reads anything but `draft`, or the witness file is non-empty or lost its `a` flag.
- Any log line names production (`hyperchat-prod`, `api.hyperchat.space`, port 5432).

---

## Phase E: Post-migration smoke tests (approval: "Phase E")
Synthetic data only. No Razorpay order, no email, no provider call, no R2 write unless `HYPERCHAT_SMOKE_R2=1` is separately approved.

**E1. The repository's smoke suites**, from the staging copy at the deployed commit:
```
cd /root/hyperchat-c2fe883
/opt/hyperchat/current/venv/bin/python tests/e2e/smoke_dev_backend.py
HYPERCHAT_SMOKE_SAMESITE=none /opt/hyperchat/current/venv/bin/python tests/e2e/smoke_auth.py
```
**Known limit of `smoke_dev_backend.py` at `c2fe883`:**
- `KNOWN_HEADS` lists heads up to `0024`, so its "database at a known migration head (0028)" check **fails**.
- It has no MFA / step-up section.
- Either accept that single known failure in the record, or approve a small test-only PR first that adds 0025–0028 and the E3 checks (open decision).

**E2. Database checks** (read-only, compared with the D1 baseline):

| Area | Check | Expect |
|---|---|---|
| Creators preserved | the D1 `streamers` count and md5 query | 133, 0 without owner, md5 unchanged (status values unchanged by 0020) |
| Legacy signups | `Q "SELECT review_status, count(*) FROM user_signups GROUP BY 1"` and the D1 md5 | 233 `legacy_unreviewed`; md5 unchanged |
| Eligibility | `Q "SELECT count(*) FROM streamers WHERE public.creator_donation_block(id) IS NULL"` | same as after 0020 in D3 |
| Donations, users, tokens, acceptances | the D1 queries | unchanged, apart from synthetic smoke rows that the suites remove |
| KYC enforcement | `Q "SELECT state, generation FROM kyc_enforcement"` | `draft | 0` |
| MFA enforcement | `Q "SELECT state FROM stepup_enforcement"` | `draft` |
| Witness | `stat -c %s /var/lib/hyperchat-kyc/dev.witness; lsattr /var/lib/hyperchat-kyc/dev.witness` | size 0, `a` flag |
| Privilege isolation | a second backup and restore check at 0028 (below) | `restore_valid: true`, `runtime_role_privilege_leaks: []`, `guard_triggers_disabled: 0` |
| Process isolation | `bash /root/hyperchat-c2fe883/deploy/check_isolation.sh dev` | only `OK` |

Privilege isolation is verified on a restored copy, never by probing the live database:
```
systemctl start hyperchat-dev-pgbackup.service      # must end "backup ok", manifest alembic 0028, kyc draft
cd /opt/hyperchat/current/backend && ../venv/bin/python -m app.backup.restore_check --env dev \
  --dir /var/backups/hyperchat-dev --set hyperchat_dev_<UTC> --pg-bin /usr/lib/postgresql/15/bin \
  --scratch --scratch-user postgres --output /root/restorecheck-dev-0028.json
```
**Required:**
- exit 0;
- `structurally_valid: true`;
- `invariants.alembic_matches_expected_head: true`;
- `objects_not_owned_by_hyperchat_owner: 0`;
- `public_table_privileges: 0`;
- `guard_triggers_disabled: 0`;
- `runtime_role_privilege_leaks: []`;
- `stepup_enforcement: "draft"`;
- `kyc_witness`: `consistent`.

This also proves the 0028 set is restorable on PostgreSQL 15.

**E3. API checks** (synthetic accounts from the smoke suites, or `curl` against `https://api.quicktestspace.site`):

| Area | Check | Expect |
|---|---|---|
| Admin authentication through `hyperchat_auth` | `smoke_auth.py`: an admin and an ordinary user sign in, sign out, change password; the session cookie is set | all pass (admin sign-in can only succeed through `hyperchat_auth` once 0024 is in place) |
| Admin session limits | admin session `expires_at` from the smoke admin's sign-in | at most 12 h after sign-in (A4) |
| Step-up fails safe | as a signed-in admin: `GET /api/v1/auth/mfa/status`, `POST /api/v1/auth/mfa/registration/options` (Origin + CSRF) | `503 {"error": …, "reason": "stepup_not_configured"}`; a `stepup_unavailable` security event; ordinary admin routes still answer (enforcement `draft`) |
| Applications and lifecycle | `smoke_dev_backend.py` sections `applications` and `creator-lifecycle` | no longer BLOCKED; pass |
| KYC | section `kyc`: enforcement reported `draft`/0; witness agrees; `/api/v1/me/kyc` without Didit configured | pass; KYC session start answers 503 |
| Payments and webhooks | sections `payments` (checkout validation; a Razorpay webhook with a bad signature is refused), `payments-account` (D6) and `payment-health` (D7) | pass; no Razorpay order created (the suite never creates one). Webhook duplicate and replay handling is covered by CI (`backend/tests/payments`), not by this smoke |
| OBS, realtime, worker | OBS token routes and the audio queue for a synthetic approved donation; the worker publishes to the DEV Pusher app; `realtime/config` for the synthetic creator | pass; worker log shows the job processed |
| DEV/production separation | `curl -fsS http://127.0.0.1:8000/health` (environment `development`, database `hyperchat_dev`); `curl -fsS http://127.0.0.1:8001/health` (production release `940137070657`, unchanged); `sudo -u postgres psql -p 5432 -d hyperchat -Atc "SELECT version_num FROM alembic_version"` | DEV reports itself; production still `0009` and its release unchanged |

**Record:** both smoke outputs (counts of passed / failed / blocked, with every failure explained), the E2 table filled in, the restore-check JSON, `check_isolation.sh` output.

**Phase E passes only when** every check passes or is a recorded, explained known limit (E1). Otherwise go to Phase F.

---

## Phase F: Rollback (approval: "Phase F"; only if a stop condition hit)
Goal: DEV back on release `229fe42381e2` and a database at 0019 with today's data, **with the KYC witness untouched.**

**F1. Stop DEV services** (as D2).

**F2. Choose the database path:**
- **The failure happened before 0020 committed** (`alembic current` = 0019): the database is unchanged. Go to F4.
- **Any later revision committed:** restore the Phase B set. Don't run `alembic downgrade`: 0020–0023 refuse once data depends on them, and a partial downgrade path is not rehearsed.

**F3. Restore the Phase B set into a fresh database, then swap names.** Never `pg_restore --clean` over the live one.
```
SET_=/var/backups/hyperchat-dev/hyperchat_dev_<UTC>
cd /var/backups/hyperchat-dev && sha256sum -c "$(basename $SET_).dump.sha256"
sudo -u postgres psql -p 5433 -d postgres -c 'CREATE DATABASE hyperchat_dev_restore OWNER hyperchat_owner'
sudo -u postgres /usr/lib/postgresql/15/bin/pg_restore -p 5433 --exit-on-error --single-transaction -d hyperchat_dev_restore "$SET_.dump"
sudo -u postgres psql -p 5433 -d hyperchat_dev_restore -Atc "SELECT version_num FROM alembic_version"   # 0019
sudo -u postgres psql -p 5433 -d postgres -c 'ALTER DATABASE hyperchat_dev RENAME TO hyperchat_dev_failed_<UTC>'
sudo -u postgres psql -p 5433 -d postgres -c 'ALTER DATABASE hyperchat_dev_restore RENAME TO hyperchat_dev'
sudo -u postgres psql -p 5433 -d postgres -c 'REVOKE ALL ON DATABASE hyperchat_dev FROM PUBLIC'
sudo -u postgres psql -p 5433 -d postgres -c 'GRANT CONNECT ON DATABASE hyperchat_dev TO hyperchat_migrator, hyperchat_app, hyperchat_worker, hyperchat_readonly, hyperchat_lifecycle, hyperchat_auth, hyperchat_stepup'
```
- **Changes are lost:** any DEV change made between the Phase B backup and the failure (DEV is a test environment; the Phase D window has no traffic).
- **Keep `hyperchat_dev_failed_<UTC>`** for diagnosis. Dropping it is a later, separate approval.
- **Re-check after the restore:** the D1 baseline queries (133 creators, 233 signups, md5s).

**F4. Release and configuration:**
```
ln -sfn /opt/hyperchat/releases/229fe42381e2 /opt/hyperchat/current
systemctl start hyperchat-api && curl -fsS http://127.0.0.1:8000/health    # release 229fe42381e2
systemctl start hyperchat-worker && systemctl start hyperchat-cleanup.timer
```
- **`api.env`:** keep the Phase C settings. The 0019 release ignores them (`extra="ignore"`), and keeping `HYPERCHAT_KYC_WITNESS_PATH` costs nothing. To return to the exact earlier file, copy `/root/api.env.dev.bak-<TS>` back (`root:hyperchat`, 0640).
- **Roles, pg_hba and pg_ident from Phase C stay.** They are additive and unused by the 0019 release. Reverting them is a separate decision: copy the C1 backups back, then `pg_ctlcluster 15 restoretest reload`.

**F5. KYC witness preservation (mandatory):**
- **Never** `rm`, truncate, replace or `chattr -a` `/var/lib/hyperchat-kyc/dev.witness`.
- **Why the empty witness stays correct:** in this rehearsal enforcement never left `draft`/generation 0, so the witness stays empty. An empty witness is consistent with both the 0019 database (no KYC tables) and a later re-run of the rehearsal.
- **If the file is non-empty when rollback starts,** something changed enforcement against the rules above:
  1. stop;
  2. keep the file as is;
  3. copy it read-only (`cp -p … /root/dev.witness.copy-<UTC>`);
  4. record it for the owner.

  The next rehearsal attempt then follows `docs/kyc-rollout.md` "After a restore". Never re-anchor or acknowledge automatically.

**Record:** the path taken, every output, the D1 baseline comparison after rollback, the witness size and flags.

---

## Approvals checklist
| Phase | Approves | Does not approve |
|---|---|---|
| 0 | read-only commands 0.1–0.3 | any change |
| A | staging copy, target release build (not switched), backup script and DEV units installed (timer not enabled), DEV witness file | backup runs, roles, env, migrations |
| B | one DEV backup run, one scratch restore check | enabling the DEV timer, anything in C–F |
| C | `setup_cluster.sh` on `15/restoretest` for `hyperchat_dev`, pg_ident lines, `api.env` settings | restarts, migrations |
| D | stopping DEV services, migrations 0020–0028, release switch, start | smoke tests with R2, any activation |
| E | the smoke suites and checks listed, one more DEV backup and scratch restore | `HYPERCHAT_SMOKE_R2=1` (separate), verifier install, Didit, activation |
| F | restore of the Phase B set and switch back to `229fe42381e2` | dropping `hyperchat_dev_failed_*`, reverting roles / pg_hba / pg_ident |

## After a successful rehearsal (each separately)
- Enable the DEV backup timer.
- Decide on the `hyperchat` role (0.1).
- Update `smoke_dev_backend.py` for 0025–0028 and MFA (test-only PR).
- Install the step-up verifier on DEV (`docs/admin-stepup.md` "Deployment sequence" step 4).
- Didit sandbox verification (`docs/kyc-rollout.md` step 3).
- Production planning (0010→0028), which this rehearsal does not cover.
