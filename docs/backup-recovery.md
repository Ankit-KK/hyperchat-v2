# Backup and disaster recovery

Repository-side backup, verification and recovery for HyperChat V2 (A3).

**Status:**
- **DEV:** the backup and the off-site copy run nightly. The DEV off-site copy has been live since 2026-11-23: a
  Cloudflare R2 bucket in a separate backup-only account, object lock, and an offline GPG key.
- **Production:** the nightly backup still runs the pre-manifest script, and there is no off-site copy yet. The
  production off-site plan (backup item F-1) uses the standalone tools tree below; each step needs approval.
- **Approvals:** every server step is marked **[APPROVAL]**.

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
| Production backup tools | `app/backup/{sets,offsite}.py` of a merged commit and a venv with the boto3 closure, for production's off-site unit only | `/opt/hyperchat-backup-tools/releases/<commit>`, `current` link ([below](#production-tools-tree)) |

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
| `.witness` | A copy of the environment's KYC witness, taken **after** the dump. **Absent** when the witness file does not exist and the database has no KYC history (no enforcement table, or draft at generation 0). The manifest then records `witness_status: "not_applicable"`. That is the API's own untouched state, where `app.kyc.witness` writes nothing yet, as in production before its first enforcement change |
| `.manifest.json` | Written **last**: environment, database, cluster, server version, Alembic head, KYC and MFA enforcement state before the dump, every file's SHA-256, archive TOC count. No secrets. A schema without an enforcement table (production at 0009, DEV at 0019) records it as `""` (KYC generation `0`). An enforcement table that exists but doesn't hold exactly one row, or any failed query, fails the backup: it is never recorded as absent. |

- **Atomicity:**
  - Everything is built in a private `.partial-*` directory.
  - The archive is checked with `pg_restore -l` and fsynced.
  - Files are then renamed into place, with the manifest last.
  - A failed run leaves no set. A set without a manifest is **incomplete** and is never used.
- **Overlap:** `flock` refuses a second run on the same directory (exit 75).
- **Permissions:** the directory and files are root-only (umask 077). A directory that is group/other-accessible or a symlink is refused.
- **Retention (fail-closed):**
  - Before pruning, every set of **this** database is **verified**: regular files only (no symlinks), the manifest is
    readable and names this database and set, the dump matches the manifest's SHA-256 and its `.dump.sha256` file, the
    witness matches when the manifest names one, and a witness is present whenever the set has KYC history (state
    other than draft, or generation above 0).
  - Only verified sets count toward the newest 3 (`KEEP_MIN`). A verified set older than 14 days is removed only while
    more than 3 newer verified sets exist, so broken newer sets can never push out the last recoverable one.
  - A set that fails verification is **never removed automatically**: it is reported, and the unit exits 4.
  - If verification itself cannot run, nothing counts as verified and nothing is pruned.
  - A set is removed manifest first. Leftovers of an interrupted removal (files without a manifest) and old-layout dumps
    are removed only once 3 verified sets exist.
  - Never follows a symlink and never touches other files or another database's sets.
- **Exit codes:**

  | Code | Meaning |
  |---|---|
  | 0 | ok |
  | 2 | refused configuration |
  | 1 | backup failed |
  | 3 | set written, but the witness is missing while the database has KYC history, or the witness path exists but is not a readable plain file (the unit fails visibly) |
  | 4 | set written, but an older set failed verification; it is kept for the operator (never pruned) |
  | 75 | another run is in progress |

- **Logs:** names, sizes, counts and hashes only.

## Verifying a set (backup validity vs recoverability)
```
sudo PYTHONDONTWRITEBYTECODE=1 ../venv/bin/python -m app.backup.restore_check --env prod --dir /var/backups/hyperchat \
     --set hyperchat_<UTC> --pg-bin /usr/lib/postgresql/17/bin --scratch --scratch-user postgres \
     --roles-sql /root/hyperchat-roles-<commit>/roles.sql --output /root/restorecheck.json
```
(from `/opt/hyperchat-prod/current/backend`; on DEV: `--env dev --dir /var/backups/hyperchat-dev --set hyperchat_dev_<UTC> --pg-bin /usr/lib/postgresql/15/bin`)

**`--roles-sql` is required on the servers.**
- `--scratch` first creates the HyperChat roles in the empty scratch cluster from `deploy/postgres/roles.sql`.
- The tool's default is that file inside the release, but a release holds only `backend/` and `database/` (deploy/README.md), so the default path does not exist there.
- Pass a root-only copy of the **deployed commit's** `roles.sql`, staged and checked from the workstation:
  ```
  git show <commit>:deploy/postgres/roles.sql | ssh root@VPS "install -d -o root -g root -m 0700 /root/hyperchat-roles-<commit> && umask 077 && cat > /root/hyperchat-roles-<commit>/roles.sql"
  git show <commit>:deploy/postgres/roles.sql | sha256sum                  # workstation
  ssh root@VPS "sha256sum /root/hyperchat-roles-<commit>/roles.sql"       # must print the same hash
  ```
- The file only creates roles inside the scratch cluster, which is deleted afterwards; it never touches a live cluster.
- `--server … --disposable-server` does not read it: that server's roles must already exist.

**Where it restores:**
- `--scratch` creates a private cluster in a temporary directory: a Unix socket there, no TCP. It is stopped and deleted afterwards.
- `--server … --disposable-server` restores into a new database `hc_restorecheck_*` on a server the caller declares disposable, and drops it afterwards.
- The live socket directory and the live database names are refused.

**What it reports (three separate answers, never one "ok"):**
- **`structurally_valid`** = `backup_valid` and `restore_valid`:
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
- **`kyc_witness`:** how the restored database compares with the newest witness given, exactly as the API would judge
  it (details in `witness`):
  - `consistent`, or `not_applicable` (no witness, and the database has no KYC history: draft, generation 0);
  - `inconsistent` (`restore_suspected`): the **expected** answer for an older backup. New donations stay blocked;
  - `missing`: the database has KYC history but no usable witness was given.
- **`live_operation_approved`:** always `false`. The tool never approves resuming live operation, never acknowledges
  a restore and never installs or replaces a witness. `live_operation_blockers` lists what remains, e.g.
  `kyc_witness_inconsistent`, `sessions_revoke_required`, `external_reconciliation_required`, `owner_approval_required`.

**Exit codes:** 0 structurally valid with the witness consistent / not applicable; 3 structurally valid but the
witness inconsistent, missing or not checked; 1 not structurally valid, or the check failed.

**No answer means the application is recoverable.** Also needed:
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

## Off-site disaster recovery (DEV live; production planned)
**Design (implemented; enabled on DEV):**
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
- **No overwrite:** HEAD first, then a conditional `If-None-Match: *` PUT. An existing object is accepted only if its
  **downloaded bytes** hash to the ciphertext this set recorded (object metadata alone never vouches for it); anything
  else is refused.
- **Resumable upload:** GPG encryption is randomized, so a retry must not re-encrypt. Each file is encrypted once into
  the local state directory (`/var/lib/hyperchat-offsite/<env>`, root 0700, the unit's `StateDirectory`; never a
  symlink), with a binding record: environment, database, set, file, plaintext SHA-256, recipient-key SHA-256 and
  ciphertext SHA-256. A rerun reuses that ciphertext and refuses when the local plaintext, the recipient key, the record's
  set or the stored ciphertext changed, or when remote objects exist but the local records are gone (nothing is
  guessed). `COMPLETE.json` also carries the recipient-key hash. The state is removed once the set is complete.
- **Already complete:** a rerun on a complete off-site set checks `COMPLETE.json` (environment, database, set,
  recipient, plaintext hashes against the local set) and verifies every object; it uploads nothing.
- **No deletion:** the tool has no delete call at all.
- **Ransomware and stolen credentials:** deletion must be impossible for the uploader. Use **bucket object lock / retention** (R2 "bucket locks"), e.g. 35 days, plus a lifecycle rule for expiry.
- **What the lock and the token do and do not stop** (Cloudflare's R2 documentation, read 2026-11-28):
  - **Confirmed:**
    - A bucket lock rule blocks **deletion and overwriting** of every object under it for the retention period. A write to a locked key is refused (`409 ObjectLockedByBucketPolicy`), and lifecycle deletion waits for the lock.
    - A lock rule does **not** prevent creating **new** objects, so the bucket's Object Read & Write token can always add new keys, including a whole new set.
    - Changing lock or lifecycle rules needs the account's dashboard or an admin-level token, not the bucket's Object Read & Write token.
  - **Not confirmed by the documentation:**
    - whether the Object Read & Write token may delete objects outside a lock;
    - whether removing a lock rule frees the objects it protected;
    - whether a retention period can be cut short.
    
    Treat these as possible.
  - **So the lock protects history but not authenticity.** Whoever holds the token (a leak, a copy outside the server) could plant a self-consistent "newest" set: the objects only need to be encrypted to the public recipient key. Signing (next item) is what refuses such a set.
- **Signed sets (security audit run-4, NV4-4):**
  - **The signature:** the upload signs the exact `COMPLETE.json` bytes with the environment's own ed25519 key (`ssh-keygen -Y sign`, namespace `hyperchat-offsite`, principal `hyperchat-offsite-<env>`). It uploads the signature as `COMPLETE.json.sig` **before** the marker.
  - **Accepted only when signed:** `list`, `verify`, `fetch` and a re-run of `upload` accept a set only when that signature verifies against the environment's **allowed-signers** file.
    - Unsigned, foreign-signed or re-used signatures are refused, and so is a name that is not a set of this database.
    - `list` reports them under `refused` and never as a set.
  - **Fixed paths:** `/etc/hyperchat-backup/offsite-sign-<env>.key` (private, root 0600, upload only) and `/etc/hyperchat-backup/offsite-sign-<env>.allowed` (public, root, not writable by others). The owner can install them before the code that uses them; the env file is unchanged.
  - **Off-server copy:** the owner keeps the allowed-signers line (one line, public) in their own records, off the server. A recovery takes it from there, **never from the bucket or from a backup**.
  - **Stamps and upload times:** a set whose stamp is later than its provider upload time (plus 10 minutes) is refused, which defeats a future-stamped plant. `verify` reports the upload time (`uploaded_at`).
  - **Limit:** the signing key is on the server. Someone who controls the server as root can sign, so after a server compromise only sets uploaded **before** the compromise are trusted. Choose by `uploaded_at`, which the uploader cannot set.
  - **Older DEV sets:** sets uploaded before signing have no `COMPLETE.json.sig` and are refused by `verify` and `fetch`. They stay locked until their lifecycle expiry; recovery uses signed sets.

**Owner decisions (the questions; the answers follow):**
1. **Storage:** provider and account (R2 or another), a **separate** bucket per environment or one bucket with `prod/` / `dev/` prefixes, and the region / jurisdiction.
2. **Credential:** a token scoped to the backup bucket. Who creates it and where it is kept.
3. **Retention:** retention and object-lock period, and the lifecycle expiry.
4. **Encryption key custody:** who holds the GPG private key, where (offline), the backup of that key, and its rotation. **Without it no off-site copy can be restored.**
5. **Restore drills:** cadence and who runs them.

**Decided:**
- **Storage:** Cloudflare R2, one bucket per environment, in a backup-only account that is separate from the media
  account.
- **Lock and expiry:** object lock of 35 days, lifecycle expiry after 90 days.
- **Credential:** a token per bucket (Object Read & Write, that bucket only).
- **Key:** an offline GPG key per environment, its private half on paper. DEV's key is DEV-only.
- **Signing:** an ed25519 signing key per environment, created on the server. Its private half never leaves the
  server, and the owner keeps the public allowed-signers line off the server.

**Enable [APPROVAL]:**
1. **Owner:** installs `/etc/hyperchat-backup/offsite-<env>.env` from `deploy/backup/offsite.env.example` (root 0600) and the environment's **own** public key file:
   - DEV: `backup-recipient.asc`;
   - production: `backup-recipient-prod.asc`, so the two environments never share or swap a key.
   
   **The signing key**, created as root on the server:
   ```
   ssh-keygen -q -t ed25519 -N "" -C hyperchat-offsite-<env> -f /etc/hyperchat-backup/offsite-sign-<env>.key
   printf 'hyperchat-offsite-<env> namespaces="hyperchat-offsite" %s\n' "$(cut -d' ' -f1,2 /etc/hyperchat-backup/offsite-sign-<env>.key.pub)" > /etc/hyperchat-backup/offsite-sign-<env>.allowed
   chmod 600 /etc/hyperchat-backup/offsite-sign-<env>.key; chmod 644 /etc/hyperchat-backup/offsite-sign-<env>.allowed
   cat /etc/hyperchat-backup/offsite-sign-<env>.allowed
   ```
   Copy the printed line, which is public, into the owner's records. Without it, a recovery cannot tell genuine sets from planted ones.
2. **Units:**
   - Install the units, a `hyperchat[-dev]-offsite.timer.d/schedule.conf` drop-in with the approved time, and an `alert.conf` drop-in (`OnFailure=hyperchat-offsite-alert@%n.service`).
   - The repo timer times are placeholders: set the time outside the maintenance windows, after the environment's backup.
3. **Production also:** the same `OnFailure=` drop-in on `hyperchat-pgbackup.service`. When production's backup fails, `--latest` re-verifies yesterday's complete set and succeeds, so the off-site unit alone never reports a stale copy.
4. **First upload:** `systemctl start hyperchat[-dev]-offsite.service` once by hand, then `verify` and `list`.
5. **Restore check:** the owner downloads that first set and checks the signature on their own PC, with the allowed-signers line from their records:
   ```
   cmd /c "ssh-keygen -Y verify -f allowed -I hyperchat-offsite-<env> -n hyperchat-offsite -s COMPLETE.json.sig < COMPLETE.json"
   ```
   The `cmd` redirect passes the exact bytes. The result must be `Good "hyperchat-offsite" signature…`. Then the owner decrypts offline and checks the hashes against `COMPLETE.json` and the manifest.
6. **Then:** `systemctl enable --now hyperchat[-dev]-offsite.timer`.

### Production tools tree
Production's off-site unit runs from `/opt/hyperchat-backup-tools/current`, not from a production release.
- **Why:** a release lacking the tool must not keep production without off-site copies, and an API deploy or rollback
  never changes the backup path. This is the same reason the backup script is an installed copy.
- **Contents of `releases/<commit12>/`:**
  - `app/__init__.py` and `app/backup/{__init__,sets,offsite}.py` (`git archive` of a merged commit; `offsite.py` imports only `sets.py`, the standard library and boto3);
  - a `python3.14` venv with exactly the boto3 closure of `backend/requirements.txt` (boto3, botocore, jmespath, python-dateutil, s3transfer, six, urllib3), wheels only, every file compared with the DEV release's venv;
  - a `SOURCE` file: commit, file hashes, pins.
- **Ownership:** root-owned, nothing secret, and no unit but `hyperchat-offsite.service` uses it. The unit runs `python -B`, and `ProtectSystem=strict` keeps the tree read-only.
- **Upgrade [APPROVAL]:** a new `releases/<commit12>`, then the `current` link switched.
- **Rollback:** the link back, or the tree removed, once no unit references it.

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
  - credentials in `api.env` / `stepup.env`;
  - revocations made after the set: sessions come back as they were (every runbook below ends them all before the
    services start); admin role or deactivation changes, passkey, OBS-token and partner-key revocations after the set
    must be re-applied by hand.
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
   - **Sessions [APPROVAL]:** before any service starts, end every restored session:
     `sudo -u postgres psql -X -p <cluster port> -d <restored database> -c "UPDATE public.sessions SET revoked_at = now() WHERE revoked_at IS NULL"`.
     The set's session rows are as they were at its time, so a session revoked after it (logout, password reset,
     admin revoke, deactivation) is live again otherwise. Rotating `HYPERCHAT_AUTH_SECRET` does **not** end sessions:
     sessions are found by the hash of their token, and the secret only signs CSRF tokens and the OAuth state.
6. **Start the services [APPROVAL]:** verifier first, then API, worker, cleanup.
7. **Check:** `/health`, `check_isolation.sh`, the KYC admin view `witness.problem`, a test donation path (DEV first).

### VPS failure / restoration to a new VPS
1. **Provision [APPROVAL]:**
   - A new Ubuntu VPS, PostgreSQL 17 (production) / 15 (DEV) clusters.
   - `deploy/postgres/setup_cluster.sh`, `roles.sql`, pg_hba / pg_ident (`deploy/README.md`).
   - OS users `hyperchat`, `hyperchat-prod`, `hyperchat-[prod-]stepup`.
2. **Releases:** deploy the same commit the old server ran; never an unreviewed one.
3. **Fetch the newest off-site set [APPROVAL]:**
   - **First, the allowed-signers file:** put the environment's allowed-signers line **from the owner's own records** into `/etc/hyperchat-backup/offsite-sign-<env>.allowed` (root, 0644). Never take it from the bucket, a backup or the lost server.
   - `offsite list` / `verify`: only sets signed by that key are listed; anything else is under `refused`.
     - Choose the newest set whose `uploaded_at` is before any suspected compromise of the old server.
     - An unexpected refused set (planted, unsigned, future-stamped) is a security event: report it before going on.
   - `offsite fetch` the chosen set. It keeps `COMPLETE.json` and `COMPLETE.json.sig`.
   - **Where the files are decrypted (the owner's PC), check the signature again:**
     `cmd /c "ssh-keygen -Y verify -f allowed -I hyperchat-offsite-<env> -n hyperchat-offsite -s COMPLETE.json.sig < COMPLETE.json"` must print `Good "hyperchat-offsite" signature…`.
   - Decrypt them **offline with the owner's private key**.
   - Check each file against the signed `COMPLETE.json` (`plain_sha256`) and the set's manifest.
4. **Verify:** `restore_check --scratch` on the decrypted set.
5. **Restore [APPROVAL]:** as above, into the empty cluster.
6. **Witness:**
   - `witness select` over every surviving copy, from all off-site sets of this environment.
   - Install the newest as `/var/lib/hyperchat-kyc/<env>.witness`, owned by the service user, mode 0600, then `chattr +a`.
   - It will normally match the restored set or be newer. Then follow the KYC table above.
7. **Credentials [APPROVAL]:** re-create `api.env` and `stepup.env` from the owner's records. Generate a new `HYPERCHAT_AUTH_SECRET`
   (this ends in-flight Google sign-ins and the CSRF tokens clients hold, not sessions).
   - **Sessions [APPROVAL]:** before any service starts, end every restored session:
     `sudo -u postgres psql -X -p <cluster port> -d <restored database> -c "UPDATE public.sessions SET revoked_at = now() WHERE revoked_at IS NULL"`.
     The set's session rows are as they were at its time, so a session revoked after it (logout, password reset,
     admin revoke, deactivation) is live again otherwise. Rotating `HYPERCHAT_AUTH_SECRET` does **not** end sessions:
     sessions are found by the hash of their token, and the secret only signs CSRF tokens and the OAuth state.
8. **DNS:** TLS (certbot), DNS to the new IP.
9. **Services [APPROVAL]:** verifier socket, API, worker, cleanup and backup timers.
10. **Checks:** `check_isolation.sh`, smoke tests (`deploy/README.md`).
11. **Reconcile:** Razorpay and Didit.

### Lost storage credentials
- **Upload token lost or leaked:** revoke it in the provider console **[APPROVAL]** and issue a new backup-only token.
  - Existing objects stay protected by object lock.
  - Objects the token holder **added** are possible (the lock does not stop new keys). They are refused unless signed: run `offsite list` and report anything under `refused`.
- **Signing key exposed (server compromised):**
  1. Create a new signing key on the rebuilt or cleaned server, and record its line.
  2. Keep the old line in the owner's records, marked with the compromise time.
  3. Old sets still verify with the old line. Trust only those whose `uploaded_at` is before the compromise.
  
  Do not use `valid-before` in the file for this: ssh-keygen checks it against the current time, so old sets would stop verifying.
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
   - otherwise restore the pre-deploy set **[APPROVAL]** and handle the witness, the sessions step and reconciliation as above.

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
- **Sandbox (audit S7):** both backup units carry `NoNewPrivileges`, `ProtectSystem=strict` with `ReadWritePaths` for the environment's own backup directory only, `PrivateTmp` and `ProtectHome`.
  - **Before** a sandboxed unit is installed on a server, a transient-unit probe with the same properties must show that `sudo -u postgres psql` works there. The host's sudo is sudo-rs.
  - Then one manual run per unit must end `backup ok`.
  - **DEV:** the S0-6 backup monitor pins the DEV unit's hash, so the unit and a re-pinned monitor go in together, between two monitor runs.
- **Production:** keeps its existing timer. A manual run (`systemctl start hyperchat-pgbackup.service`) must end `backup ok`. Existing old-layout dumps stay until 3 new sets exist.
- **DEV:** `systemctl enable --now hyperchat-dev-pgbackup.timer`.
- **Then:** `restore_check --scratch` on the first set of each environment, and record the JSON result.
