# DEV permission repair (117 paths): for review, not yet run

**Status:** prepared on 2026-10-24 and waiting for owner approval. Nothing here has run on a server except the read-only `reconcile.sh`.

**Why it's needed:** Phase A of [the DEV rehearsal](../dev-rehearsal-0020-0028.md) stopped at A1. The DEV isolation audit refused "other" access in `/opt/hyperchat`. The cause is described in the `deploy/README.md` "Rules".

**Scope:** DEV only. Production's 5 directories in `/opt/hyperchat-prod` need a separate change and a separate approval.

## What it changes
Exactly the paths in [`manifest.txt`](manifest.txt): 117 directories, sha256 `0f172cd0…62b9f9`. Each goes from `root:root 0755` to `root:hyperchat 0750`:

| Paths | Count | Before | After |
|---|---|---|---|
| `/opt/hyperchat`, `/opt/hyperchat/releases` | 2 | `root:root 0755` | `root:hyperchat 0750` |
| `__pycache__` directories in 18 releases (12 in the active `229fe42381e2`) | 115 | `root:root 0755` | `root:hyperchat 0750` |

- **Untouched:**
  - The 600 `.pyc` files inside those directories. They are `root:root 0640`, which the audit already accepts.
  - Every other file, directory and symlink, including `current`.
  - Production, PostgreSQL, the units and the services.
- **Change method:**
  - One `chgrp --no-dereference` and one `chmod 0750` per listed path.
  - No `-R` and no glob.
  - No restart or reload.
  - No path is created or removed.

## Files
| File | What it is |
|---|---|
| [`reconcile.sh`](reconcile.sh) / [`reconcile-2026-10-24.txt`](reconcile-2026-10-24.txt) | The read-only check that ran on 2026-10-24, and its output. The live state matched the earlier inspection: 117 directories, all `root:root 755`, in the same 18 releases. |
| [`manifest.txt`](manifest.txt) | The 117 paths, taken from that output. It is embedded in both scripts and checked by sha256. |
| [`apply.sh`](apply.sh) | `pf_pre && pf_top && pf_pycache && pf_verify`. It fails closed: each step prints `STOP: …` and stops at the first surprise. |
| [`rollback.sh`](rollback.sh) | Puts all 117 paths back to `root:root 0755`. Safe to re-run, and works from any partial state. |
| [`mocktest.sh`](mocktest.sh) | 61 local tests on a mock tree: the happy path, drift, symlink, mode change, unhealthy API, partial failure plus rollback, changed invariant, unexpected consumer, wrong active release, and fail-closed collection (production health failed or not ok, a failed sha256sum, stat or systemctl, no main PIDs, a snapshot failure after the change, a failed find, sort, lsattr or xargs with empty output before or after the change, an audit with empty output, rollback never OK on failed health, service or stat checks). Run it as root on a throwaway machine (WSL); it refuses to run if any path it mocks already exists. |

## How it runs (after approval)
`apply.sh` and `rollback.sh` are generated with the manifest and the runbook's `audit_expected` embedded verbatim. Each runs as a single SSH session:

    Get-Content -Raw apply.sh | ssh root@VPS "tr -d '\r' | bash -s"

Its records go to `/root/rehearsal-dev/permfix/`, created with mode 0700. That directory is single-use: a second run is refused.

- **`pf_pre` (read-only) STOPs unless all of these hold:**
  - The manifest's sha256 matches, it has 2 + 115 paths, and every path is inside the DEV release tree.
  - `current` points to `229fe42381e2`.
  - The consumers of `/opt/hyperchat` are exactly the `hyperchat-api`, `-worker` and `-cleanup` units with `User=hyperchat`, and nginx and cron name none.
  - The live audit violations equal the manifest exactly.
  - Every path is a plain directory: not a symlink, nothing symlinked on the way, `root:root 755`, not immutable.
  - `hyperchat` and its running processes hold group `hyperchat`.
  - `hyperchat` can read its active release.
  - The DEV API is healthy on `229fe42381e2`.
  - Every invariant command succeeds (production must be healthy, its API active). The invariants are the DEV `current` symlink, the production tree top and symlink, production health and services, the `pg_hba` and `pg_ident` of both clusters, the env-file metadata and the unit files.
- **`pf_top`:**
  1. Runs `chgrp` on both directories and checks the result.
  2. Runs `chmod 0750` on `releases`, then on `/opt/hyperchat`. After each, it checks that `hyperchat` can still read its release and that the API is healthy. On a failure, that directory goes back to 0755 at once, then STOP.
- **`pf_pycache`:** works through the 115 directories one by one. It re-checks each before changing it and stops at the first failure, reporting how many were done.
- **`pf_verify`:**
  - 0 audit-predicate violations, and all 117 paths are `root:hyperchat 750`.
  - `hyperchat` can read its release, and the API is healthy on the same release.
  - API and worker PIDs, restart counts and states are unchanged.
  - The invariants are unchanged.
  - `check_isolation.sh dev` exits 1 with empty stderr and prints exactly the runbook's `audit_expected dev` lines: 21 OK and the two legacy `pg_hba` / `pg_ident` FAILs that Phase C fixes.

**Service access throughout:** `hyperchat` keeps its access at every point. Before each directory loses "other", it already has group `hyperchat` with r-x. The running API and worker hold gid 987 (`/proc/<pid>/status`), so no restart is needed. Root-run tools keep access because root owns every path; that includes the DEV off-site backup unit if it is ever installed, which runs as root with an empty capability set.

## Rollback
- **Command:** `Get-Content -Raw rollback.sh | ssh root@VPS "tr -d '\r' | bash -s"`.
- **What it does:** for each manifest path, `chmod 0755` and then `chgrp --no-dereference root`. It then checks that all 117 are `root:root 755`, that `hyperchat` can read its release, and prints health and service state.
- **What it restores:** the exact before-state, recorded in `before.txt`. Only ctimes differ.

**Trigger rollback when:** any STOP after `pf_pre`, or any later doubt. A STOP inside `pf_pre` changed nothing, but its records directory exists: move it aside (`mv /root/rehearsal-dev/permfix /root/rehearsal-dev/permfix.stopped-<time>`) before a later run.

## After it passes
Phase A resumes at A1 (`a1_records`) only with its own approval.
