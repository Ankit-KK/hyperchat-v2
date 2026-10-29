# Incident 2026-10-28: HyperChat nginx configuration files overwritten (resolved 2026-10-29)

**Status:** CLOSED by the owner on 2026-10-29.
**Impact:** no traffic was affected.
**Root cause:** an agent-issued remote command whose quoting was changed by Windows PowerShell.

## Timeline (UTC)
| When | What |
|---|---|
| 2026-10-28 18:09:13 | During a check meant to be read-only (deployed nginx files vs the repo), an inline `ssh` command sent from Windows PowerShell 5.1 overwrote the 7 HyperChat nginx files with one-line `<hash> <path>` strings; `nginx -t` failed from then on |
| 2026-10-28, minutes later | Detected by the same check. Recovery sources identified read-only; restore package prepared; owner informed |
| 2026-10-29 12:40 | Read-only recheck: nginx master unchanged, files still the corrupted versions, the 06:22 `apt-daily-upgrade` run had not restarted nginx |
| 2026-10-29 ~12:55 | Owner-approved restore (package `7aaafd81…`, one run): **PASS** |
| 2026-10-29 | Incident closed by the owner |

## Affected files (shared DEV/production VPS)
- `/etc/nginx/sites-available/00-catchall`
- `/etc/nginx/sites-available/api.quicktestspace.site` (DEV)
- `/etc/nginx/sites-available/api.hyperchat.space` (production)
- `/etc/nginx/snippets/hyperchat-public-limit-zone.conf`
- `/etc/nginx/snippets/hyperchat-public-limit.conf`
- `/etc/nginx/snippets/hyperchat-prod-public-limit-zone.conf`
- `/etc/nginx/snippets/hyperchat-prod-public-limit.conf`

## Root cause
The intended read-only line was:
```bash
echo "$(sha256sum < $(readlink -f $f) | cut -c1-16) $f -> $(readlink -f $f)"
```

- **How it broke:** Windows PowerShell 5.1 drops embedded double quotes when it passes an argument to a native program. On the server the `echo` was no longer quoted, so `-> $(readlink -f $f)` became an output redirect into each resolved file.
- **Contributing factors:**
  - the command ran as root;
  - it was sent inline instead of as a reviewed script file;
  - nothing on the host stops a "read-only" session from writing.

## Impact
- **Why there was no outage:** the running nginx master (started 2026-10-28 06:28) kept its in-memory configuration. A reload is also harmless, because a master that fails to parse a new config keeps the old one.
- **The latent risk:** any nginx restart or host reboot before the restore would have failed, taking down DEV and production. `apt-daily-upgrade` had restarted nginx on 2026-10-28 at 06:28.
- **Untouched:** application code, databases, payments, credentials and certificates were not affected.

## Recovery
The restored versions are the historically deployed ones, chosen from deploy records and git, and verified by hash:

| File | Restored version | sha256 (prefix) | Evidence |
|---|---|---|---|
| `00-catchall` | `f592286` (only version) | `b7a4736e` | git history |
| `api.quicktestspace.site` | `f15c5a8` (= `main`) | `83309724` | install record 2026-10-12: "installed sha: 83309724941ce92d" |
| `api.hyperchat.space` | `6565d09` (before F1/F1-R) | `3958d882` | installed 2026-10-06 with `git archive 6565d09`; F1/F1-R documented as not deployed to production |
| the 4 snippets | the only version of each | see the package manifest | sizes listed just before the overwrite equal git |

**What the restore script did:**
- verified the package and that the destinations still held the recorded corrupted hashes;
- kept the corrupted files with their metadata as evidence;
- installed the 7 files as `root:root 0644`;
- checked the installed hashes;
- ran `nginx -t`.

**What it did not do:** reload, restart or touch any package.

**Verified after the restore:**
- `nginx -t` passes;
- nginx master pid unchanged (385333);
- DEV and production health over HTTPS and locally;
- API, worker and production API active with unchanged PIDs;
- `api.env` (DEV and production), PostgreSQL configuration, `nginx.conf`, `default` and `sites-enabled` unchanged.

**Evidence on the server:**
- `/root/nginx-incident-20261028T180913Z/`;
- `/root/hyperchat-nginx-restore-7aaafd81.log`.

**Limit:** a manual edit made on the server after those install dates without any record could not have been detected.

## State after the incident
- **The DEV site, catch-all and snippets** equal `main`.
- **The production site** is the `6565d09` version. F1/F1-R (no query strings or referrers in the production logs; OBS tokens kept out of the error log) is still **not deployed to production** and needs its own approval.

## Safeguards (OPS-2; owner-approved as review-only repo tooling on 2026-10-29)
| # | Safeguard | State |
|---|---|---|
| S1 | Remote commands only as reviewed script files run with `cmd /c 'ssh … "bash -s" < file'` from Windows; never inline through PowerShell; no `>` text inside `echo` strings | in effect |
| S2 | A sandboxed read-only runner (`systemd-run` with `ProtectSystem=strict`), so a mistaken redirect cannot write to `/etc`, `/opt`, `/var` or `/root` | proposed; host use needs separate approval after a dry test |
| S3 | A local lint that refuses writing constructs in scripts meant to be read-only | repo tooling (OPS-2 PR) |
| S4 | An nginx drift check (fingerprints vs the recorded deployed hashes) plus `nginx -t` | repo tooling (OPS-2 PR); runs on the host only by approval |
| S5 | Host-side configuration history (etckeeper or a nightly `/etc/nginx` archive) | proposed; host change, later |
