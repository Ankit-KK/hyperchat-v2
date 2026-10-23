# Backup and disaster recovery

Repository-side backup, verification and recovery for HyperChat V2 (A3).

**Status:**
- **Code ready:** this document and its tools are in the repository.
- **Not installed:** nothing here runs on any server yet.
- **Approvals:** every server step is marked **[APPROVAL]**.
- **Off-site copies:** disabled until the owner decides the open questions below.

## What exists

| Piece | What it does | Where |
|---|---|---|
| Local backup | One verified **set** per run and environment | `deploy/backup/hyperchat-pgbackup.sh` (installed as `/usr/local/sbin/hyperchat-pgbackup`) |
| Production schedule | Production backup, 02:30 UTC (unchanged) | `hyperchat-pgbackup.{service,timer}` |
| DEV schedule | DEV backup, 02:00 UTC (new) | `hyperchat-dev-pgbackup.{service,timer}` |
| Set loader | Manifest, names and SHA-256 of every file; no symlinks; environment checks | `app.backup.sets` |
| Restore verification | Restores into a disposable PostgreSQL and checks schema and security invariants | `python -m app.backup.restore_check` |
| Witness selection | Chooses the KYC witness to keep after a recovery; read-only | `python -m app.backup.witness select` |
| Off-site copy | Optional; encrypted, verified, never overwrites or deletes | `python -m app.backup.offsite`, `hyperchat[-dev]-offsite.{service,timer}`, `offsite.env.example` |

### Environments
| | Production | DEV |
|---|---|---|
| Database | `hyperchat` | `hyperchat_dev` |
| Cluster | `17/hyperchat`, port 5432 | `15/restoretest`, port 5433 |
| Tools | `/usr/lib/postgresql/17/bin` | `/usr/lib/postgresql/15/bin` |
| Local directory | `/var/backups/hyperchat` (root 0700) | `/var/backups/hyperchat-dev` (root 0700) |
| KYC witness | `/var/lib/hyperchat-kyc/prod.witness` | `/var/lib/hyperchat-kyc/dev.witness` |
| Off-site prefix | `prod/` | `dev/` |

**Fixed per environment:**
- The script refuses any setting that crosses environments: database, port, cluster, directory or witness.
- `test` is a third profile for disposable `hc_backuptest_*` databases only. It never touches `/var/backups` or a real witness.

**Not backed up here:** `hyperchat_restore_test`, the read-only legacy source on the DEV cluster. It can be re-created from the legacy dump.

## A backup set
Each run writes one set (`<db>_<UTC>`), all or nothing:

| File | Content |
|---|---|
| `.dump` | `pg_dump -Fc`, made with the **source cluster's own major version**. The script checks the tools and the answering server against the named cluster. |
| `.dump.sha256` | `sha256sum -c` format, as before |
| `.witness` | A copy of the environment's KYC witness, taken **after** the dump |
| `.manifest.json` | Written **last**: environment, database, cluster, server version, Alembic head, KYC and MFA enforcement state before the dump, every file's SHA-256, archive TOC count. No secrets. |

- **Atomicity:**
  - Everything is built in a private `.partial-*` directory.
  - The archive is checked with `pg_restore -l` and fsynced.
  - Files are then renamed into place, with the manifest last.
  - A failed run leaves no set. A set without a manifest is **incomplete** and is never used.
- **Overlap:** `flock` refuses a second run on the same directory (exit 75).
- **Permissions:** the directory and files are root-only (umask 077). A directory that is group/other-accessible or a symlink is refused.
- **Retention:**
  - Removes complete sets of **this** database older than 14 days.
  - Always keeps the newest 3, however old (never the last verified one).
  - Removes old-layout dumps only once 3 complete sets exist.
  - Never follows a symlink and never touches other files.
- **Exit codes:**

  | Code | Meaning |
  |---|---|
  | 0 | ok |
  | 2 | refused configuration |
  | 1 | backup failed |
  | 3 | set written, but the witness is missing or unreadable (the unit fails visibly) |
  | 75 | another run is in progress |

- **Logs:** names, sizes, counts and hashes only.

## Verifying a set (backup validity vs recoverability)
```
sudo python -m app.backup.restore_check --env prod --dir /var/backups/hyperchat --set hyperchat_<UTC> \
     --pg-bin /usr/lib/postgresql/17/bin --scratch --scratch-user postgres --output /root/restorecheck.json
```
(from `/opt/hyperchat-prod/current/backend` with its venv)

**Where it restores:**
- `--scratch` creates a private cluster in a temporary directory: a Unix socket there, no TCP. It is stopped and deleted afterwards.
- `--server … --disposable-server` restores into a new database `hc_restorecheck_*` on a server the caller declares disposable, and drops it afterwards.
- The live socket directory and the live database names are refused.

**What it reports (two separate answers):**
- **`backup_valid`:** the manifest, the SHA-256 of every file, a readable archive whose TOC matches the manifest, and restore tools of the source's major version.
- **`restore_valid`:**
  - a complete single-transaction restore into the **same major version**;
  - the Alembic head equals both the manifest and this checkout's newest migration;
  - every object is owned by `hyperchat_owner`;
  - no privileges for `PUBLIC`;
  - every guard trigger (`aaa_`/`aab_`/`aac_`) is enabled;
  - no runtime role can execute owner-only functions or rewrite step-up / KYC enforcement tables;
  - the enforcement rows are sane;
  - the KYC generation is not behind the manifest.
- **`witness`:** how the restored database compares with the newest witness, exactly as the API would judge it. `restore_suspected` is the **expected** answer for an older backup.

**Neither answer means the application is recoverable.** Also needed:
- the environment's credentials (`api.env`, the verifier, the operator's Google client);
- the R2 media objects;
- Razorpay, Didit, Pusher and bot state;
- the newest KYC witness.

**Version rule:** restore into the source's major version. A production (17) set is verified with 17 tools; a DEV (15) set with 15 tools. Dumps of 17 can contain settings that 15 rejects.

## KYC restore witness (docs/kyc-rollout.md)
The witness is what stops an older database from silently undoing:
- later restrictions;
- revoked grace;
- an activated or closed enforcement.

It protects only while it is **not** restored together with an older backup: an old database plus its own old witness looks consistent.

**What the tooling guarantees:**
- **Capture:** each set carries a copy of the witness, taken **after** the dump, so it is at least as new as the database in that set. It gives disaster recovery a witness when the server itself is lost.
- **Selection:** after a recovery, keep the **newest copy that exists anywhere**: the live file, every local set, every off-site set.
  - `python -m app.backup.witness select <every copy>` requires all copies to be one append-only history (each a byte prefix of the newest), and picks the newest.
  - It refuses diverged copies: tampering, a re-initialised file, or another environment's file. It writes nothing.
- **Never automatic:** installing the chosen file (append-only, `chattr +a`) and acknowledging the restore stay manual operator steps:
  1. Compare the audit with the witness.
  2. Re-apply the lost revocations and restrictions.
  3. Run `python -m app.kyc.operator acknowledge-restore`.

  No tool here re-anchors anything.
- **Isolation:** DEV and production witnesses are separate files, separate copies and separate prefixes. A set of the other environment is refused.

**When the database and witness generations differ:**

| Database vs newest witness | Meaning | Action |
|---|---|---|
| Same generation and chain | Consistent | None. Donations allowed. |
| Database behind, or another chain at the anchor | Changes after the backup are lost | New donations stay blocked (`restore_suspected`). Follow the after-restore steps in `docs/kyc-rollout.md`. **[APPROVAL]** for `acknowledge-restore`. |
| Witness missing while the database has history | Protection lost | Blocked (`witness_missing`). Restore the newest witness copy from sets / off-site. Never create an empty one for a database with history. |
| Copies diverge | Possible tampering | Stop. The owner decides from the audit. |

## Off-site disaster recovery (optional; disabled)
**Design (implemented, configurable, not enabled):**
- **Destination:** any S3-compatible HTTPS endpoint. Cloudflare R2 is the candidate.
- **Credentials:** `/etc/hyperchat-backup/offsite-<env>.env` (root 0600; the tool refuses anything else).
  - It holds a **backup-only** token scoped to the backup bucket.
  - Never the API's media credential, never in `api.env`.
- **What is uploaded:** only a locally **verified** set of the configured environment.
- **Encryption at rest:** every file is encrypted on the server with GPG to the owner's **public** key.
  - The private key stays offline, so neither the server nor anyone who compromises it can read off-site copies.
  - The provider's own encryption at rest adds to this.
- **In transit:** HTTPS, plus `Content-MD5` on every PUT.
- **Verification:** each object is read back and its SHA-256 compared. `COMPLETE.json` is written last; an off-site set without it is incomplete.
- **Namespaces:** objects go under `prod/<set>/` or `dev/<set>/`. The prefix must equal the environment.
- **No overwrite:** HEAD first, then a conditional `If-None-Match: *` PUT. Existing objects with other content are refused.
- **No deletion:** the tool has no delete call at all. A rerun after a partial upload resumes.
- **Ransomware and stolen credentials:** deletion must be impossible for the uploader. Use **bucket object lock / retention** (R2 "bucket locks"), e.g. 35 days, plus a lifecycle rule for expiry. The upload token cannot shorten either.

**Owner decisions needed before enabling [APPROVAL]:**
1. **Storage:** provider and account (R2 or another), a **separate** bucket per environment or one bucket with `prod/` / `dev/` prefixes, and the region / jurisdiction.
2. **Credential:** a token scoped to the backup bucket. Who creates it and where it is kept.
3. **Retention:** retention and object-lock period, and the lifecycle expiry.
4. **Encryption key custody:** who holds the GPG private key, where (offline), the backup of that key, and its rotation. **Without it no off-site copy can be restored.**
5. **Restore drills:** cadence and who runs them.

**Enable [APPROVAL]:**
1. Install `/etc/hyperchat-backup/offsite-<env>.env` from `deploy/backup/offsite.env.example` (root 0600) with the public key file.
2. Install the units.
3. `systemctl enable --now hyperchat[-dev]-offsite.timer`.
4. Check: `python -m app.backup.offsite upload --config … --latest` once by hand, then `verify` and `list`.

## Recovery point and limits (honest)
- **RPO:**
  - **Local:** up to 24 hours (nightly logical dumps; no WAL archiving or point-in-time recovery).
  - **Off-site:** one more day of exposure until its copy runs.
  - Everything after the last set is lost on restore. That includes payments recorded by webhooks: Razorpay keeps its own record, see below.
- **RTO:** the time to provision PostgreSQL and the release, plus the restore (minutes for today's sizes; measure it in drills), plus credential and DNS work.
- **A database restore restores only the database.** It does not roll back or restore:
  - R2 media objects (separate bucket; uploads after the backup stay, deleted ones are not recreated);
  - Razorpay (payments captured after the backup exist at Razorpay but not in the restored database: reconcile from the Razorpay dashboard / webhooks before reopening donations);
  - Didit decisions (`docs/kyc-rollout.md`: a lost decline can revive an approval until Didit sends a new result);
  - Pusher, Telegram or Discord messages already delivered;
  - sent emails;
  - credentials in `api.env` / `stepup.env`.
- **Backups contain personal data** (users, donations, KYC metadata) and the stored hashes of secrets. Access to sets, off-site objects and the GPG private key is as sensitive as the database.

## Runbooks
Every step that changes a server is **[APPROVAL]**. "Read-only" steps can run any time.

### Database corruption (server alive)
1. **Contain:**
   - `systemctl stop hyperchat[-prod]-api hyperchat[-prod]-worker` **[APPROVAL]**.
   - Keep the damaged cluster: never delete it before the restore is verified.
   - Take a dump of what is readable, if possible.
2. **Choose a set:**
   - Run `restore_check` on the newest set (read-only, scratch).
   - If it is not valid, use the next one.
3. **Restore [APPROVAL]:**
   - Into a **new** database on the environment's cluster, using that cluster's tools: `pg_restore --exit-on-error --single-transaction`, roles from `deploy/postgres/roles.sql`.
   - Then switch `HYPERCHAT_DB_NAME`, or rename the databases after stopping every connection.
4. **Witness:**
   - `witness select` over the live file and every copy.
   - Keep the newest; never replace the live file with an older one.
5. **Reconcile external state:** Razorpay payments after the set's time, KYC changes (above).
6. **Start the services [APPROVAL]:** verifier first, then API, worker, cleanup.
7. **Check:** `/health`, `check_isolation.sh`, the KYC admin view `witness.problem`, a test donation path (DEV first).

### VPS failure / restoration to a new VPS
1. **Provision [APPROVAL]:**
   - A new Ubuntu VPS, PostgreSQL 17 (production) / 15 (DEV) clusters.
   - `deploy/postgres/setup_cluster.sh`, `roles.sql`, pg_hba / pg_ident (`deploy/README.md`).
   - OS users `hyperchat`, `hyperchat-prod`, `hyperchat-[prod-]stepup`.
2. **Releases:** deploy the same commit the old server ran; never an unreviewed one.
3. **Fetch the newest off-site set [APPROVAL]:**
   - `offsite list` / `verify`.
   - Download the objects of the chosen set.
   - Decrypt them **offline with the owner's private key**.
   - Check each file against `COMPLETE.json` (`plain_sha256`) and the set's manifest.
4. **Verify:** `restore_check --scratch` on the decrypted set.
5. **Restore [APPROVAL]:** as above, into the empty cluster.
6. **Witness:**
   - `witness select` over every surviving copy, from all off-site sets of this environment.
   - Install the newest as `/var/lib/hyperchat-kyc/<env>.witness`, owned by the service user, mode 0600, then `chattr +a`.
   - It will normally match the restored set or be newer. Then follow the KYC table above.
7. **Credentials [APPROVAL]:** re-create `api.env` and `stepup.env` from the owner's records. Generate a new `HYPERCHAT_AUTH_SECRET` (all sessions end).
8. **DNS:** TLS (certbot), DNS to the new IP.
9. **Services [APPROVAL]:** verifier socket, API, worker, cleanup and backup timers.
10. **Checks:** `check_isolation.sh`, smoke tests (`deploy/README.md`).
11. **Reconcile:** Razorpay and Didit.

### Lost storage credentials
- **Upload token lost or leaked:** revoke it in the provider console **[APPROVAL]** and issue a new backup-only token. Existing objects stay protected by object lock.
- **GPG private key lost:** the off-site copies are unreadable. Create a new key pair, switch the recipient file, and take a fresh set at once. Local sets remain plaintext on the server.

### Accidental deletion
- **Rows deleted in the application:** restore a set into a scratch / disposable database (`restore_check --server`, read-only use). Copy back only the needed rows by hand **[APPROVAL]**, through the owner role, with the guards in mind. Never overwrite the live database to recover single rows.
- **A local set deleted:** the off-site copy (if enabled) or the next set.
- **Off-site objects:** protected by object lock for the retention period (owner configuration).

### Failed migration
1. Migrations run in one transaction where possible. A failure leaves the previous head: check `alembic current`.
2. Take a backup **before** every deploy (`deploy/README.md`).
3. If a migration completed but the release must go back:
   - code rollback first;
   - `alembic downgrade` only by explicit decision (several downgrades refuse once data depends on them, e.g. 0023 after enforcement left draft);
   - otherwise restore the pre-deploy set **[APPROVAL]** and handle the witness and reconciliation as above.

### KYC witness mismatch
`kyc_enforcement_witness_mismatch` (critical) or `witness.problem` in the admin view:
1. Do not acknowledge blindly.
2. Find out why: a restore, a wrong database (`HYPERCHAT_DB_NAME`), a replaced or missing file, or the other environment's file.
3. Follow the generation table above and `docs/kyc-rollout.md` "After a restore".

### Service recovery order
1. PostgreSQL.
2. Verifier socket (`hyperchat[-prod]-stepup.socket`; admins need it only once MFA is active).
3. API.
4. Worker: realtime events older than 10 minutes expire, see `deploy/README.md`.
5. Cleanup timer.
6. Backup and off-site timers.

Stop order is the reverse. The worker can stop at any time.

## Installation (later; each step [APPROVAL])
- **Script:** `install -o root -g root -m 750 deploy/backup/hyperchat-pgbackup.sh /usr/local/sbin/hyperchat-pgbackup`. The same script serves both environments.
- **Units:** `install -o root -g root -m 644 deploy/backup/hyperchat-{,dev-}pgbackup.{service,timer} /etc/systemd/system/`, then `systemctl daemon-reload`.
- **Production:** keeps its existing timer. A manual run (`systemctl start hyperchat-pgbackup.service`) must end `backup ok`. Existing old-layout dumps stay until 3 new sets exist.
- **DEV:** `systemctl enable --now hyperchat-dev-pgbackup.timer`.
- **Then:** `restore_check --scratch` on the first set of each environment, and record the JSON result.
