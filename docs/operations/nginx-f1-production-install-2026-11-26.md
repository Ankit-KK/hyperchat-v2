# Production nginx F1/F1-R install, 2026-11-26

Deploy record. Security audit 2026-11-26 (lead NV-1) found that production was still running the pre-F1 site
(`6565d09`, `3958d882`), which wrote full query strings and referrers to `access.log`. On 2026-11-26 at 14:16Z
the owner installed `deploy/nginx/api.hyperchat.space.conf` from `a76df1d` with the reviewed change script
`nv1-prod-nginx-install.sh`. The script ran through the file, never inline (OPS-2 S1).

## Result
- Pre-checks passed. `nginx -t` passed, and nginx reloaded with the same master pid (19707).
- A probe to `/health?nv1probe=<UTC>` was logged without its query, so `hyperchat_prod_noquery` is live.
- DEV, catch-all and DEV snippet fingerprints were the same before and after.
- The read-only check afterwards printed `verdict MATCHES repo a76df1d`, with 1 definition of
  `hyperchat_prod_noquery` under `/etc/nginx`.
- The 3 older rotated log lines holding `token=` were scanner 404s on `/v1/<word>/<word>`. None had the OBS token
  shape (`obs_` + 64 hex), so no token was rotated. Normal log rotation removes those files.
- Backup and rollback source: `/root/nv1-f1-install-20261126T141620Z`. The production snippets were already present
  and equal to `a76df1d`, so this run installed none.

## Installed fingerprints (sha256 printed by the script after the change)
| File on the host | sha256 |
|---|---|
| `sites-available/api.hyperchat.space` | `b14edf786f91f916022216dad36e060b91531348d20395d885c5a1fbc7a3279e` |
| `snippets/hyperchat-prod-public-limit-zone.conf` | `2158db8253b77887a716ab1f108fec53a9e6cf08a2ab1410e7391e2d6f8afe0c` |
| `snippets/hyperchat-prod-public-limit.conf` | `8ae65880b7e118ed4fcdc639f6bbde46dc5f610a5b3cde2d88ea7738906fba2c` |
| `nginx.conf` | `a0270a8893cbefe39d2596383f25d07773c6a32da3aade030a3d43e42f4b378d` |
| `sites-available/00-catchall` | `b7a4736eddd3fed1a3ea12862b9c1bdc9ee01e939d254a5ca523ba759d325858` |
| `sites-available/api.quicktestspace.site` | `83309724941ce92d7390a504311174651b50e859b841edd025dc5ac442c44c6b` |
| `snippets/hyperchat-public-limit-zone.conf` | `f9618548cf577548948b258dffb4693c9da0e1b46c6facce15e9f65deba53d15` |
| `snippets/hyperchat-public-limit.conf` | `73ee342bfb69684a343b860ee789f0785b0c8907cebffe4a1dce14e7517bf7d3` |

`deploy/tools/nginx_fingerprints.txt` takes its rows from this table.
