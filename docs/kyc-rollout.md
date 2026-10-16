# KYC enforcement and the legacy rollout (KYC PR4, migration 0023)

PR4 builds mandatory KYC enforcement but does **not** turn it on. After the migration, enforcement is `draft`, and nothing changes for existing creators until an operator runs the activation step below, with the approvals it requires. Didit identity verification itself is described in `docs/kyc.md`.

## The model
**Enforcement record:** `public.kyc_enforcement` is a single row whose state goes `draft` → `active` → `closed`, and never back. A trigger refuses any other transition for every role, the schema owner included.
- **Activation record:** activation stores the operator, the approval reference, the policy (`kyc-pr4-v1`), the time and the migration version. Neither it nor the grace maximum (90 days) can ever change.
- **Generation and chain:** each enforcement change is one audit row at the next `generation`. Its `chain` is sha256 over the previous chain, the generation, the row's random id, its content and its time: a hash chain over the whole history, which the restore witness below anchors on.

**The donation rule:** `public.creator_donation_block(streamer_id)`, the one rule from migration 0020. Checkout, upload grants and uploads call it, and the donations insert trigger runs it again for every runtime-role insert. Its checks, in order:

| # | Check | Result when it fails |
|---|---|---|
| 1 | The creator exists and is `active` | `not_found` / `not_active` |
| 2 | No open admin restriction | `restricted` |
| 3 | **`draft` only:** a legacy creator still owned by its baseline owner keeps its PR1 eligibility | (eligible) |
| 4 | It has an owner, and that owner is active | `no_owner` |
| 5 | The owner's Didit KYC is approved now (`user_kyc_approved`: the newest session, a durable veto, the workflow epoch) | (eligible) |
| 6 | **`active` only:** a legacy creator still owned by its baseline owner holds an unexpired, unrevoked grace grant bound to that owner, **and that owner never had a Didit decision** | (eligible) |
| 7 | Otherwise | `kyc_required` |

In `closed`, no grace counts: every creator needs its owner's KYC.

**Activating a creator** (admin `POST /admin/creators/{id}/activate`) needs, in addition to the expected status, an owner and a valid Razorpay account:
- the current owner's own approved KYC (`kyc_activation_block`): never the baseline, never grace;
- no open restriction.

Neither a Didit webhook nor anything else activates a creator automatically. **Reactivation** re-checks the full rule.

**One transition path:** eligibility is necessary but not sufficient. A creator's status changes only through `public.kyc_change_creator_status`. Only the dedicated database role `hyperchat_lifecycle` can execute it. That role is the admin API's own connection (`HYPERCHAT_DB_LIFECYCLE_USER`), and it has no table access of its own. In one transaction on the locked creator row, the function does all of the following:
- **compare-and-set** on the expected status;
- **activation checks:** an owner, the owner's own KYC and no restriction, plus a compare-and-set of the Razorpay account. The function trusts no account list: the API checks the account against this server's configuration, then the function confirms the locked row still has that account;
- **reactivation checks:** the full donation rule;
- **suspension:** revokes grace;
- **audit:** writes the `creator_status_events` row.

**No other way in:** the trigger `streamers_guard_activation` refuses every other runtime-role status write: a raw `UPDATE` of `status`, or an `INSERT` that isn't `pending`.

**The boundary is the role.** The application role (`hyperchat_app`) can't execute the function, whatever admin id it names; the actor check inside is for consistency only. A SQL injection in, or a leaked password for, the application's connection therefore can't activate, suspend or reactivate a creator directly.

**Remaining limit: forged web sessions.** The application role owns the authentication tables (`users`, `sessions`), as it must to sign people in. So someone holding only the application's database credentials can still write an admin session row and then call the admin API over HTTPS. The API then performs the transition legitimately, and the audit names that admin.

Closing this needs authentication itself behind a separate boundary. The smallest secure follow-up is to move admin sign-in to a dedicated database role:
- admin sessions, admin password hashes and role changes become writable only through SECURITY DEFINER functions owned by a separate `hyperchat_auth` role, used only by the login and session code;
- `hyperchat_app` keeps read access, but can no longer mint or extend an admin session.

The alternative is to require a step-up second factor for lifecycle actions, verified by a component outside the application's database role. Either is a change to the authentication system, not to KYC, and is outside PR4.

**Grace grants** (`kyc_grace_grants`):
- **Finite:** 1 to 90 days, also enforced by a CHECK.
- **Bound:** to the creator **and** its owner at grant time.
- **One-shot:** one grant per creator-owner pair, ever, so there are no rolling extensions.
- **Immutable** except for one revocation.
- **Who can grant:** the operator only (`kyc_grant_grace`, no runtime grant), and only for an active, unrestricted legacy creator still owned by its baseline owner, **whose owner never had a Didit decision**.
- **For never-verified owners only:** grace is a finite rollout allowance for owners who haven't verified yet. It never covers an owner whose verification failed, was revoked or expired.
  - **What counts as a decision:** an approval, a `Declined` or a `Kyc Expired`, in this environment.
  - **Where it's recorded:** durably, in `kyc_sessions.adverse_at` and the append-only `kyc_events`.
  - **Effect:** once the owner has any decision, grace never applies again, even after a later `Resubmitted`. Only a new approval restores eligibility, through KYC.
  - **Undecided sessions don't count:** a session still undecided (`In Progress`, `In Review` and so on) keeps grace.
- **What ends grace:**
  - expiry (the clock);
  - revocation (admin API);
  - creator suspension;
  - an owner transfer;
  - a restriction;
  - closure.

**Restrictions** (`kyc_restrictions`) are admin holds with a reason code: `identity_concern`, `owner_dispute`, `fraud_review`, `kyc_lost` or `admin_hold`.
- **Blocking:** an open restriction blocks new donations and activation, and adding one revokes the creator's grace.
- **Lifting:** **operator-only** (`python -m app.kyc.operator lift-restriction`, migration role).
  - The API and its database role can add restrictions, but can't remove them: `kyc_lift_restriction` has no runtime grant, and the tables refuse runtime writes.
  - Each restriction is lifted at most once, with a note, and is audited under the operator's name.
  - A lift never bypasses KYC: the rule still needs the owner's KYC, or valid grace while `active`.

**Audit** (`kyc_enforcement_events`): append-only.
- **Recorded:** activation, closure, grants, revocations, expiries (the daily cleanup records them), restrictions, lifts and restore acknowledgements.
- **Protected:** UPDATE is refused for every role, and the API's role can't insert or delete.
- **Not erasable by deleting a creator:** a creator with history can't be deleted (RESTRICT). Migration 0023 also makes `creator_status_events` RESTRICT, so deleting a creator no longer erases its status audit.

## Who can do what
| Action | Path | Role |
|---|---|---|
| View state, readiness, witness check | `GET /api/v1/admin/kyc/enforcement` | admin |
| Impact preview (read-only) | `GET /api/v1/admin/kyc/enforcement/preview`, or `python -m app.kyc.operator report` | admin / operator |
| Enforcement history | `GET /api/v1/admin/kyc/enforcement/events[?streamer_id=]` | admin |
| One creator's eligibility, restrictions, grants | `GET /api/v1/admin/creators/{id}/kyc` | admin |
| Add a restriction | `POST /api/v1/admin/creators/{id}/kyc/restrictions` `{reason, note?}` | admin (Origin + CSRF) |
| Lift a restriction | `python -m app.kyc.operator lift-restriction` | **operator** (migration role) |
| Revoke grace | `POST …/kyc/grace/revoke` `{reason}` | admin (Origin + CSRF) |
| Grant grace | `python -m app.kyc.operator grant` | **operator** (migration role) |
| Activate / close enforcement | `python -m app.kyc.operator activate` / `close` | **operator** (migration role) |
| Acknowledge a reconciled restore | `python -m app.kyc.operator acknowledge-restore` | **operator** (migration role) |

**API limits:**
- **Nothing in the API loosens enforcement:** no endpoint marks KYC approved, grants grace, lifts a restriction, or activates, closes or reopens enforcement.
- **What the API role can do:** call the fail-closed functions (restrict, revoke grace), the audited creator-status transition, and reads.
- **Refused to it:** writing the tables, and calling the operator functions.
- **Public errors stay generic:** `503 {"error": "Donations are temporarily unavailable"}`. Reason codes appear only in the admin views.

## Restore safety (and its limits)
**Why:** a database restored from an older snapshot would silently go back on all of this: re-open revoked grace, lift restrictions, or return to `draft`. Nothing inside that database can tell. So the API and the operator tool record the database's **generation, chain and state** in a **restore witness outside PostgreSQL**:
- **Where:** `HYPERCHAT_KYC_WITNESS_PATH`.
- **Format:** one JSON line per change, ids and codes only, fsynced.

**The anchor** is the witnessed line with the highest generation. The database is consistent only while both hold:
- it still holds **that exact chain at that generation**;
- it hasn't gone back in state.

**Why a restore can't be caught up:** a restored database fails this, and adding changes never heals it. Its history diverged, and its later audit rows carry other random ids, so its chain at the anchor's generation is different forever. Ordinary API changes, and any direct calls by the database role, therefore can't clear a restore mismatch, however far they advance the generation. Comparing generation numbers alone would allow exactly that.

**The check:**
- **When:** before every new donation (checkout, upload grants, uploads), the database's state and generation, and its chain at the anchor, are compared with the witness.
- **Failure:** on a mismatch, **new donations fail closed** (generic 503) and a critical `kyc_enforcement_witness_mismatch` event is logged. The admin view shows `witness.problem`.
- **Also fail closed:**
  - no path is configured but the database is no longer the untouched `draft`/0;
  - the file is **missing** while the database has history (an empty file is a fresh witness: create it at deploy, or with `witness-init`);
  - the file is unreadable.

**Recording:**
- **The API and the daily cleanup** append a line only when the database **extends** the anchored history. They never re-anchor.
- **The only re-anchoring:** the operator's `acknowledge-restore`, an audited database change, and `witness-init` when no file exists.
- **Admin changes** (restrict, revoke grace) need a witness path (503 otherwise). They stay allowed during a suspected restore, because they are how lost revocations are re-applied.
- **The operator tool** refuses every change without a witness path. It also refuses to loosen anything on a database inconsistent with its witness. Only `activate` and `close`, which tighten, may re-apply a state that a restore lost.

**Server setup (per environment, separate files):**
```
install -d -o root -g root -m 0755 /var/lib/hyperchat-kyc
install -o hyperchat -g hyperchat -m 0600 /dev/null /var/lib/hyperchat-kyc/<env>.witness  # empty: a fresh witness
chattr +a /var/lib/hyperchat-kyc/<env>.witness   # append-only: not even its owner can rewrite or delete it
```
- **Running the service:** set `HYPERCHAT_KYC_WITNESS_PATH` in that environment's api.env (DEV and production use different files). The cleanup service uses the same env file.
- **Running the operator tool:** run it as root with the same path.

**After a restore:**
1. Compare the audit in the restored database with the witness lines. They name each lost change (action, creator and grant ids).
2. Re-apply what was lost:
   - lost revocations and restrictions, through the admin API;
   - the state, with `activate` / `close`, if it went back.

   Lost lifts and grants are re-applied only after the acknowledgement, through the operator tool.
3. Run `python -m app.kyc.operator acknowledge-restore --operator NAME --detail "…" --confirm-database DB`.
   - **Effect:** it records an audited change past the witness and re-anchors the witness on the database's history.
   - **Refusals:** it refuses while the state is still behind, and when there is nothing to acknowledge.
4. New donations resume.

**Limits (deliberately not claimed):**
- **Read-time check only:** the witness is checked by the API and the operator tool, not by PostgreSQL itself. The donations trigger can't read a file, so a donation inserted directly with the application's database credentials, outside the API, still uses only the in-database rule.
- **Unwitnessed changes:** a change committed but not yet witnessed is accepted as an extension of the witnessed history. This covers a crash between the commit and the file write, and a direct database-role call. It is only protected against a later restore once a line records it: the next witnessed change, or the daily cleanup. A restore to a point between the last witnessed line and such a change goes undetected.
- **Only enforcement history is witnessed:** Didit results (`kyc_sessions`, migration 0022) are not. A restore that loses a later Didit decline or expiry revives that owner's approval until Didit sends a new result, or an admin refresh records the adverse state again.
- **File handling matters:** the witness protects only as long as the file is kept with the server, not inside the restored backup. Losing or replacing it (as root, with `chattr -a`) removes the protection.
- **Not a defence against a compromised host or operator:** the schema owner can disable any trigger. The witness is a detection mechanism for restores and wrong-snapshot mistakes.
- **Release switching:** an older application release (before PR4) still uses the database rule, because the rule lives in PostgreSQL. It lacks only the witness check. A database can't be downgraded below 0023 once enforcement left `draft` or has any history: the downgrade refuses.

## Rollout runbook (each step needs the owner's approval; none is automatic)
1. **Code merged.**
   - **Result:** PR4 is on `main`, and nothing runs.
2. **Migration deployment** (DEV first, then production; the normal deploy procedure, with a backup first).
   - **Before the migration:**
     - re-run `deploy/postgres/roles.sql`, which creates `hyperchat_lifecycle`;
     - grant it `CONNECT` on the database;
     - add its line to that cluster's `pg_ident` map;
     - set `HYPERCHAT_DB_LIFECYCLE_USER=hyperchat_lifecycle` in api.env.

     Without the role the migration fails, and that is safe. Without the env var, creator status changes answer 503.
   - **Before:** migrations 0020–0023 verified on PostgreSQL 15 (DEV's version).
   - **Check:** `SELECT state, generation FROM public.kyc_enforcement` gives `draft`, `0`.
   - **Set up:** the witness file (above), and set `HYPERCHAT_KYC_WITNESS_PATH`.
   - **Result:** existing creators are unaffected. The DEV smoke test reports the enforcement state.
3. **Didit sandbox verification on DEV** (`docs/kyc.md`):
   - configure Didit;
   - verify a test owner;
   - check that a standard creator can then be activated, and that a decline blocks it at once.
4. **Read-only legacy impact assessment.**
   - **Run:** `python -m app.kyc.operator report`, or `GET /admin/kyc/enforcement/preview`, in each environment.
   - **Review:**
     - creators that would lose new donations once active (`block_when_active`);
     - creators without an owner, or whose owner changed;
     - owners without KYC;
     - grace candidates.
   - **Contact:** the owners who need to verify.
5. **Grace-grant approval.**
   - **Decide:** the owner decides, for each creator, whether a finite grant is needed and how long (90 days at most).
   - **Run:** `python -m app.kyc.operator grant --creator ID --days N --reason "…" --operator NAME --confirm-database DB`.
   - **Then:** re-run the report.
6. **Explicit enforcement activation** (owner approval reference required).
   - **Run:** `python -m app.kyc.operator activate --operator NAME --authorization APPROVAL-REF --confirm-database DB`.
   - **Effect:** legacy creators without KYC or grace stop taking **new** donations at once. Existing orders still complete, because the rule governs new donations only.
7. **Monitoring and emergency procedures:**
   - **Watch:**
     - `kyc_enforcement_witness_mismatch`, which is critical;
     - `payment_rejected`/`creator_ineligible`;
     - the preview;
     - grace expiries in the audit (daily cleanup).
   - **Emergency for one creator:** add a restriction, or revoke grace. Both are immediate and audited.
   - **Restore:** follow the restore steps above.
   - **There is no switch-off:** enforcement can't be returned to `draft`. Re-enabling an individual creator needs its owner's KYC.
8. **Enforcement closure** (after the grace period, with owner approval).
   - **Run:** `python -m app.kyc.operator close --operator NAME --authorization APPROVAL-REF --confirm-database DB`.
   - **Effect:** all grace stops counting. Closure is permanent; nothing reopens it.

**Backups:**
- **Backup and enforcement state:** nightly backups include the enforcement tables, but a backup is only a database snapshot. Restoring one is what the witness detects.
- **Rollback:** after activation, roll back by restoring forward (re-apply), never by downgrading. The 0023 downgrade refuses.
