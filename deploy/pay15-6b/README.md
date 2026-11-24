# PAY-15 stage 6b: the DEV window gates (migrations 0030–0035, accounting switch OFF)

These are the gate scripts of the stage 6b DEV packet. The packet adds one file, `release.env`, plus the release
archives, the dependency lock and a manifest. Nothing here runs by itself: every gate needs its own owner approval,
and it runs on the DEV host only, started from a reviewed wrapper file, never an inline ssh command.

| Gate | File | What it does |
|---|---|---|
| G0 | `g0_precheck.sh` | read-only pre-checks; records the facts every later gate compares against |
| B | `b_build.sh` | builds the inert release directory from the hash-pinned lock (nothing switched) |
| R | `r_rehearsal.sh` | `restore_check --scratch` (with the running release's checker: the set is at 0029) plus the 0029 → 0035 upgrade on a private scratch cluster |
| W | `w_window.sh` | stops the DEV API and worker, takes one backup, migrates exactly to 0035, switches the release, starts it |
| S | `s_post.sh` | the DEV smoke, the integrity report, the verifier, and the L4 deploy-check against the previous release id |
| X | `x_rollback.sh` | restores the window's backup by a database rename swap; nothing is dropped |

**Proved in CI:**
- `database/tests/test_migrations.py::test_pay15_6b_packet_sql_on_a_fresh_0029_to_0035_upgrade` runs
  `sql/prestate.sql` and `sql/verify_0035.sql` on a fresh 0001 → 0029 → 0035 database, on PostgreSQL 15 and 17.
- `backend/tests/test_pay15_6b_gates.py` runs the gates on a fake host (`B6_TEST=1`, which is refused on a real
  server).
