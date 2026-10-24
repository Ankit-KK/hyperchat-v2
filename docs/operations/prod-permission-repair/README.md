# Production permission repair (5 paths): for review, not yet run

**Status:** prepared on 2026-10-24 and waiting for explicit owner approval. Nothing here has run on a server except the read-only `reconcile.sh`.

**Why it's needed:**
- The production isolation audit refuses "other" access to 5 `__pycache__` directories in the active release.
- Root-run Python wrote them on 2026-10-06 at 17:05, after the API started at 14:56 (see the `deploy/README.md` "Rules").
- They also block A1 of [the DEV rehearsal](../dev-rehearsal-0020-0028.md), which runs `check_isolation.sh prod`.

**Reference:** this follows the DEV repair (`docs/operations/dev-permission-repair/`, PR Ankit-KK/hyperchat-v2#86), which ran successfully on 2026-10-24.

**Scope:** production only, and exactly these 5 directories. DEV is checked unchanged, never touched.

## What it changes
Exactly the paths in [`manifest.txt`](manifest.txt) (sha256 `c61c0dc4…53fbb1c`). Each goes from `root:root 0755` to `root:hyperchat-prod 0750`:

    /opt/hyperchat-prod/releases/940137070657/backend/app/__pycache__
    /opt/hyperchat-prod/releases/940137070657/backend/app/admin/__pycache__
    /opt/hyperchat-prod/releases/940137070657/backend/app/auth/__pycache__
    /opt/hyperchat-prod/releases/940137070657/backend/app/core/__pycache__
    /opt/hyperchat-prod/releases/940137070657/backend/app/security/__pycache__

- **Untouched:**
  - The 13 `.pyc` files inside these directories. They are `root:root 0640`, which the audit already accepts.
  - The parent directories. They are `root:hyperchat-prod 0750`, as are `/opt/hyperchat-prod`, `releases` and the release itself.
  - Every other file, directory and symlink, including `current`.
  - DEV, PostgreSQL, the units, the services and the backup script.
- **Change method:**
  - One `chgrp --no-dereference` and one `chmod 0750` per listed path.
  - No `-R` and no glob.
  - No restart or reload, no deployment, no database access.
  - No path is created or removed.

## Production state from the read-only inspection (2026-10-24, [`reconcile-2026-10-24.txt`](reconcile-2026-10-24.txt))
- **Violations:** exactly the 5 paths, all `root:root 755` plain directories, 0 nested directories, no immutable flag. Nothing else in `/opt/hyperchat-prod` violates the audit's strict predicate.
- **Release:** one release, `940137070657`. `current` points to it.
- **Units reading `/opt/hyperchat-prod`:** `hyperchat-prod-api` and `hyperchat-prod-cleanup`, both `User=hyperchat-prod`.
  - `hyperchat-prod-worker` and `hyperchat-offsite` are not installed.
  - nginx and cron name none of these paths, and `/usr/local/sbin/hyperchat-pgbackup` names none.
- **Running service:** `hyperchat-prod-api` is active, PID 471695, 0 restarts, gid 986 (`hyperchat-prod`). `hyperchat-prod-cleanup.timer` is active.
- **Health:** `/health` on 127.0.0.1:8001 is ok, release `940137070657`, database ok. DEV `/health` is ok on `229fe42381e2`.
- **Production audit:** only the release-tree FAIL above plus the two legacy `pg_hba` / `pg_ident` FAILs that Phase C fixes.

## Anticipated service impact
None.
- **Service access:** `hyperchat-prod` already cannot read the `.pyc` files (`root:root 0640`), so Python already compiles from source and ignores them. With group `hyperchat-prod` and r-x on these directories, it sees exactly what it sees now.
- **Parents:** the parent directories don't change, so access to every module stays the same.
- **Root-run tools:** root owns every path, so their access is unchanged.
- **No restart:** no restart or reload is needed or done.

## Files
| File | What it is |
|---|---|
| [`reconcile.sh`](reconcile.sh) / [`reconcile-2026-10-24.txt`](reconcile-2026-10-24.txt) | The read-only inspection and its output |
| [`manifest.txt`](manifest.txt) | The 5 paths, embedded in both scripts and checked by sha256 |
| [`apply.sh`](apply.sh) | `pf_pre && pf_pycache && pf_verify`. It fails closed: each step prints `STOP: …` and stops at the first surprise. |
| [`rollback.sh`](rollback.sh) | Puts the 5 paths back to `root:root 0755`. Safe to re-run, works from any partial state, and never reports OK when a check fails. |
| [`mocktest.sh`](mocktest.sh) | 65 local tests on a mock production tree plus a mock DEV tree. Run it as root on a throwaway machine (WSL); it refuses to run if any path it mocks exists. |

## How it runs (after approval only)
`apply.sh` and `rollback.sh` have the manifest and the runbook's `audit_expected` embedded verbatim. Each runs as a single SSH session:

    Get-Content -Raw apply.sh | ssh root@VPS "tr -d '\r' | bash -s"

Its records go to `/root/prod-permfix/`, created with mode 0700. That directory is single-use: a second run is refused.

- **`pf_pre` (read-only apart from its records) STOPs unless all of these hold:**
  - The manifest's sha256 matches, and it lists exactly 5 `__pycache__` directories of `940137070657`.
  - `current` points to `940137070657`.
  - `/opt/hyperchat-prod`, `releases` and the release are `root:hyperchat-prod 750`.
  - The consumers are exactly the api and cleanup units.
  - Every invariant command succeeds. The invariants are:
    - production's `current`, tree top, release directory and env-file metadata
    - DEV's `current`, tree top, health (ok on `229fe42381e2`) and services (active)
    - the sha256 of both clusters' `pg_hba`/`pg_ident`, of the unit files and of the backup script
  - The violations scan succeeds and equals the manifest.
  - Each path is a plain directory: not a symlink, nothing symlinked on the way, `root:root 755`, parent `root:hyperchat-prod 750`, readable attributes, not immutable.
  - The before-state is recorded.
  - The running API's main PID holds gid `hyperchat-prod`.
  - `hyperchat-prod` can read its release.
  - Production is healthy on `940137070657`.
  - The API and cleanup timer are active.
- **`pf_pycache`:** works through the 5 paths one by one. It re-checks each immediately before changing it and stops at the first failure, reporting how many were done.
- **`pf_verify`:**
  - The scan succeeds and finds 0 violations, and all 5 paths are `root:hyperchat-prod 750`.
  - Service access and production health still hold.
  - The API PID, restart count and states are unchanged.
  - The invariants are unchanged.
  - Both `check_isolation.sh prod` and `check_isolation.sh dev` exit 1 with empty stderr and print exactly the runbook's `audit_expected` lines: production 20 OK, DEV 21 OK, plus the two legacy FAILs each.

## Rollback
- **Command:** `Get-Content -Raw rollback.sh | ssh root@VPS "tr -d '\r' | bash -s"`.
- **What it does:** for each of the 5 paths, `chmod 0755` and then `chgrp --no-dereference root`. It then checks all of these, and reports INCOMPLETE (exit 1) if any fails:
  - all 5 paths are `root:root 755`
  - `hyperchat-prod` can read its release
  - production `/health` is ok on `940137070657`
  - the API and cleanup timer are active
- **What it restores:** the exact before-state, recorded in `before.txt`. Only ctimes differ.

**Trigger rollback when:** any STOP after `pf_pre`, or any later doubt. A STOP inside `pf_pre` changed nothing, but its records directory exists: move it aside (`mv /root/prod-permfix /root/prod-permfix.stopped-<time>`) before a later run.
