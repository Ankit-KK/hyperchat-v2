# D1 hosted rehearsal: results (TEMPLATE, not filled)

Copy this file to `hosted-rehearsal-<YYYY-MM-DD>.md` and fill it while running
[`hosted-rehearsal.md`](hosted-rehearsal.md).

**Never paste a password, key, token, hash or real email.** Synthetic account UUIDs may appear. Results are one of:
`PASS`, `FAIL`, `OBSERVED` (recorded, no expectation), `NOT POSSIBLE ON HOSTED`, `NOT TESTED` (reason given).

## Run metadata
| Field | Value |
|---|---|
| Date (UTC), operator, second person | |
| Owner approvals (G1–G6): date, wording | |
| Project name, ref, region, plan, cost shown | |
| Repository commit tested (`main`) | |
| PostgreSQL version (`SHOW server_version`) | |
| GoTrue version (`/auth/v1/health`) | |
| supautils settings (privileged role, allowed configs) | |
| Connection used: host type (pooler / direct), port, `verify-full` OK? | |
| SQL editor `current_user` / `session_user` | |

## Results
| ID | Check | Expected | Actual (SQLSTATE / HTTP / value) | Frozen digest unchanged? | Result | Notes |
|---|---|---|---|---|---|---|
| H1 | Identity, versions, TLS | `postgres`, `ssl on`, versions recorded | | n/a | | |
| H2 | Catalog capabilities | owner `supabase_auth_admin`, RLS on, `postgres` BYPASSRLS | | n/a | | |
| H3a | `SET session_replication_role = replica` | recorded | | n/a | | |
| H3b | `LOCK auth.users IN SHARE MODE` (READ ONLY) | succeeds | | n/a | | |
| H3c | `row_security = off` read | succeeds for `postgres` | | n/a | | |
| H3d | `pg_terminate_backend` of own session | recorded | | n/a | | |
| H4.1 | `freeze.sql` run 1 | exit 0 | | n/a | | |
| H4.2 | `freeze.sql` run 2 | exit 0, `O` | | n/a | | |
| H4.3 | `verify.sql` | `O`, 31, no cols, no WHEN, pinned sha256 | | n/a | | |
| H5 | `ENABLE ALWAYS` as `postgres` | 42501 (or record `A` path) | | n/a | | |
| H5b | Owner re-run keeps `A` / non-owner re-run fails safely | `NOT POSSIBLE ON HOSTED` unless H5 succeeded | | n/a | | |
| H6 | 3 synthetic accounts via admin API | 200/201 ×3, `$2a$10$` prefix | | n/a | | |
| H7.1 | Sign-in ×2 | 200 | | | | |
| H7.2 | Refresh ×3 | 200 | | | | |
| H7.3 | `GET /user` | 200 | | | | |
| H7.4 | Metadata update | 200 | | | | |
| H7.5 | User password change | 500 P0001 | | | | |
| H7.6 | Admin password change | 500 P0001 | | | | |
| H7.7 | Admin email change | 500 P0001 | | | | |
| H7.8 | Admin ban | 500 P0001 | | | | |
| H7.9 | Admin create | 500 P0001 | | | | |
| H7.10 | Admin delete | 500 P0001 | | | | |
| H7.11 | Public sign-up (only with G2) | 500 P0001 or NOT TESTED | | | | |
| H7.12 | SQL writes of frozen fields, insert, delete | P0001 each | | | | |
| H7c | Replica-mode `UPDATE email` (rolled back) | recorded (bypass?) | | | | |
| H8.1–H8.6 | Writes / TRUNCATE / trigger DDL while locked | 55P03 (or 42501) | | | | |
| H8.7 | Read while locked | immediate | | | | |
| H8.8 | Sign-in during a ~5 s hold | 200 after release; latency | | | | |
| H8.10 | Lock released afterwards | 0 ShareLocks | | | | |
| H9 | `SELECT 1` RTT from workstation (median of 10) | recorded | | n/a | | |
| H10.1 | Grants by `postgres` to a dedicated role | recorded (notices) | | n/a | | |
| H10.2 | Report as reader without BYPASSRLS | refused (row-level security), exit 2 | | n/a | | |
| H10.3 | `ALTER ROLE … BYPASSRLS` then report | 3 source accounts | | n/a | | |
| H10.4 | Source role chosen for `--apply` | dedicated / `postgres` + accept flag | | n/a | | |
| H11.1 | Importer dry run over TLS | eligible 3, freeze active, fingerprint | | | | |
| H11.2 | `--apply` into local target | APPLIED 3, exit 0, run id | | | | |
| H11.3 | `import_status.sql` (local) | imported 3, still 3 | | | | |
| H12.1 | `unfreeze.sql` ×2 | exit 0 | | n/a | | |
| H12.2 | `verify.sql` / dry run after | 0 rows / `missing` | | n/a | | |
| H12.3 | Password change and create after unfreeze | succeed | | n/a | | |

## Cleanup proof (all required)
| Step | Evidence | Done |
|---|---|---|
| Rehearsal roles dropped / freeze removed | | |
| Project deleted (listing without keys; health URL no longer answers) | | |
| Local container removed; `rehearsal.env`, `.pgpass`, passwords, ids, JSON, CA file shredded | | |
| Billing shows no additional project or charge | | |

## Conclusion
- Blocking outcomes found (from "Outcomes that block the production cutover"):
- Residual risks needing owner acceptance (replica-mode bypass, session termination / exit 4):
- Differences from the local rehearsal:
