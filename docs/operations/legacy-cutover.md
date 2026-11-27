# Legacy → V2 launch cutover (fresh start)

**Status: runbook only. Nothing here has run against production, DEV, Supabase or the legacy database.** Every step
needs the owner's approval for that window. Owner decisions of 2026-11-27 (binding):

1. **Fresh start.** Only database data migrates: creators, donations, donor totals, settings, moderators, users and
   their links.
2. **No legacy OBS tokens or URLs.** The ETL skips `obs_tokens`; streamers create new tokens at launch.
3. **Passwords are imported** at cutover with the approved D1 importer (`database/etl/auth_hash_import.py`, policy in
   `docs/backend-completion-checklist.md` D1). No real hashes in DEV.
4. **Files:** only each creator's profile picture (`brand_logo_url`) and background (`background_url`) are carried
   over (`docs/operations/legacy-asset-migration-plan.md`). Voice messages, donation media and soundboard files are
   not.

## What the ETL loads (head 0039)

`database/etl/rehearsal.py` loads one read-only snapshot of the restored legacy database into a **fresh** database at
head, in one transaction, then runs `database/etl/reconcile.py` and the PAY-15 verifier. Exit 0 only if every check
passes.

| Target | Source | Disposition |
|---|---|---|
| `users` | `auth_users` (+ `admin_emails` → `role = 'admin'`) | copied; `password_hash` NULL (step 4). **`admin` only from `admin_emails`** (NV3-2): a legacy `role = 'admin'` outside that list loads as `user`; the reconciliation counts the demotions and requires the V2 admins to equal the `admin_emails` accounts. Supabase-deleted / banned accounts are deactivated in step 4 (NV3-3) |
| `streamers` | `streamers` | copied; `razorpay_account` from the key-name pair; `kyc_track = 'legacy'`, `kyc_baseline_owner_id = user_id` (0020); an **active creator without an owner loads `pending`** (owner 2026-11-27); logo / background URLs kept, or rewritten with `--media-base-*` |
| `streamers_moderators`, `streamer_payment_health`, `payout_accounts`, `platform_voice_tiers`, `streamer_voice_tiers`, `soundboard_tnc_acceptances`, `terms_acceptances`, `partner_signups`, `tts_moderation_audit`, `rate_limits`, `system_alerts`, `visits`, `reviews`, `user_queries` | same names | copied column for column |
| `banned_donors` | same | copied; 0016's `unbanned_*` columns defaulted (NULL) |
| `user_signups` | same | copied; 0021's review columns defaulted, `review_status = 'legacy_unreviewed'` (what 0021 gave existing rows; the default is now `submitted`) |
| `donations` | 133 × `<slug>_donations` | copied (amounts, statuses, moderation, messages, audio timestamps, `is_hyperemote`, `legacy_source_table`); **`voice_message_url`, `hypersound_url`, `media_url`, `media_type`, `tts_audio_url`, `status_token_hash`, `upload_token_hash` loaded as NULL**; `payment_account`, `provider_payment_id` NULL (legacy recorded none). **Every unpaid legacy row (`pending` or `failed`) loads as terminal `failed` with `provider_order_id` NULL and no `order_lookup` row** (NV3-1): no late capture can complete a legacy order outside the creator / KYC rule and PAY-15; the reconciliation counts them and requires none open. The Razorpay webhook also refuses any imported order (`unknown_order`, nothing applied) |
| `donations.creditable_inr` / `credited_inr`, `donation_credit_events` (0031) | — | set by `public.donations_credit_backfill()` in the load transaction (see below) |
| `streamer_donator_totals` | same (`streamer_slug` → `streamer_id`) | copied **verbatim** |
| `order_lookup`, `moderation_actions` | same | copied (orphans: lookup dropped, moderation kept as audit); lookups of unpaid legacy orders are dropped (NV3-1) |
| `telegram_` / `discord_callback_mapping` | same | unexpired rows only; 0014's `recipient_id` / `moderator_id` defaulted (NULL) |
| `request_nonces` | same | unexpired rows only |
| `obs_tokens` | `obs_tokens` | **not migrated** (counted) |
| `platform_` / `streamer_soundboard_slots`, `soundboard_swap_log` | same | **not migrated** (counted): their files are not carried over |
| `api_keys`, `partner_api_keys` | same | **not migrated** (counted): old keys are reset, new ones issued in V2 (owner 2026-11-27) |
| `auth_sessions`, `password_reset_tokens`, `admin_emails`, `profiles`, `auth_migration_control` | same | not migrated (counted) |

Left empty (nothing to import; the guards are triggers keyed on the runtime roles or are paused for the loaded
tables, so they never block the migrator):

- PAY-15: `donation_reversals` (0032), `donation_accounting_evaluations`, `payment_accounting_events`, `payment_accounting_runs` (0034), the 0035 objects. `payment_accounting_state` keeps the migration's one row, `off`.
- Payment evidence: `payment_webhook_events`, `payment_integrity_events` (0029).
- Step-up (0025–0028, 0037): `webauthn_credentials`, `stepup_*`, `session_assurance`. `stepup_enforcement` keeps its `draft` row.
- KYC (0022–0023): `kyc_sessions`, `kyc_events`, `kyc_webhook_events`, `kyc_grace_grants`, `kyc_restrictions`, `kyc_enforcement_events`. `kyc_enforcement` keeps its `draft` row; `kyc_provider_config` may already be set.
- `creator_image_uploads` (0039): legacy logos and backgrounds get no image record ("validity is the row; legacy copies get no row"); step 6 copies them without one. The reconciliation requires the table empty.
- `creator_status_events`, `application_events` (0020–0021), `user_identities`, `user_invites`, `sessions`, `jobs`, `realtime_outbox`, `moderator_notifications`, `d1_hash_import_ledger` (written by step 4).
- Pricing (0036): its triggers check only `hyperchat_app` / `hyperchat_worker` writes (and are paused during the load), so legacy minimums and voice tiers outside today's bounds load unchanged and are never re-judged. With every trigger
  back on, the CI test shows the owner can still write such a value and `hyperchat_app` is refused (`check_violation`).

### PAY-15: how migrated donations and donor totals are loaded

Donations are copied with `credited_inr = 0` while the 0031 credit writer is paused, so no copied donation re-adds
itself to a total. Donor totals are copied verbatim. Then `donations_credit_backfill()` (0031: "also run by the cutover
ETL") allocates each bucket's total to its paid and approved donations, oldest first. Each credit is `min(amount_inr,
what is left)` and is recorded as one `backfill` audit row; the totals are not changed. A bucket whose total is below
its donations ("short") leaves its newest rows partly or not credited, so a later rejection can never debit below what
the total holds. The reconciliation reports how many buckets are short and by how much. Imported donations are never
reversed automatically (D17 PAY15-D8).

Evidence: `backend/tests/payments/test_legacy_cutover_etl_db.py` (CI, PostgreSQL 15 and 17). It covers exact, short,
above-sum, no-total and total-only buckets, case variants of one name, and paid-but-rejected / pending / unpaid rows.
On that load the verifier reports exactly one finding, the I10 `baseline_unverified` that every database at head
reports until Phase 2B pins the I10 baseline (`I10_BASELINE_CONFIRMED = False`). It reports the same after the D1
import and after rejecting a partly credited migrated donation through the writer. The reconciliation accepts exactly
that finding, and nothing more once the baseline is confirmed.

## Runbook

Run Python only on script files, with `-B` (`python -B -m etl.…`), from a release that contains `backend/` and
`database/`.

1. **Freeze the legacy writes.**
   - Announce the window.
   - Stop donations, dashboard edits and signups on legacy.
   - Apply the D1 auth freeze (`docs/operations/d1-auth-freeze/`): password changes and resets frozen, ordinary
     sign-ins allowed.
   - Drain the in-flight writes and verify they have finished.
2. **Take the final snapshot.**
   - Dump legacy only after the drain.
   - Restore it read-only as `hyperchat_restore_test` on the restore cluster.
   - Any legacy write after this point invalidates the snapshot.
3. **ETL into production.**
   - Production must be a database migrated to head (0039 or later) with nothing else in it. The load refuses
     anything else and changes nothing.
   - Production today is an idle stack at 0009 whose API is running, so its tables may already hold rows (rate
     limits, visits, sessions, a bootstrap admin). First stop the production API, worker and timers. Then either:
     - migrate it to head and run the load: if the load refuses as "not fresh", the database is not empty; or
     - drop and re-create `hyperchat` (owner `hyperchat_owner`, the `setup_cluster.sh` grants), then migrate it to
       head.
   - **Open item, owner decision:** how a drop and re-create interacts with the production KYC restore witness and
     the backup sets (`/var/lib/hyperchat-kyc/prod.witness`, `/var/backups/hyperchat`). Decide this with the backup
     session before the window. This runbook does not guess.
   - Rehearse the same command first on a scratch database at head on the same snapshot.
   - Run:
     ```
     python -B -m etl.rehearsal --source "<restore_test, read-only>" --target "<hyperchat as hyperchat_migrator>" \
       --verify-dsn "<hyperchat as hyperchat_readonly>" --report cutover-report.txt --production-cutover
     ```
   - Add `--media-base-from <legacy r2 base> … --media-base-to <production media base>` if creator images sit on a
     legacy R2 host.
   - Stop unless it exits 0. The report holds counts and sums only.
4. **Import the password hashes.** Follow `database/README.md` (D1): the read-only report, the dry run against the final
   Supabase state, owner review of every count, then `--apply` with the reviewed fingerprint. Administrator accounts
   are excluded (`target_admin_account`): migration 0024 refuses the apply role (`hyperchat_app`) every write to an
   admin row, so one admin in the plan would roll the whole run back. Admins set their password by the admin path
   (owner decision below).
   - **Deactivate the Supabase-deleted and -banned accounts (NV3-3).** Those flags live only in Supabase `auth.users`
     (`deleted_at`, `banned_until`), not in the public dump, so the load keeps those V2 accounts active. The same
     importer, against the same frozen source, plans them in every dry run (`deactivate_banned_or_deleted`,
     `banned_or_deleted_already_inactive`; matched by the exact legacy id). Then, with the reviewed fingerprint:
     ```
     python -B -m etl.auth_hash_import --deactivate --expected-fingerprint <the reviewed source_fingerprint>
     ```
     It runs as `hyperchat_migrator` (owner role): 0024 refuses runtime roles every admin-row write, and 0027 makes
     a runtime-role `is_active` change need a step-up grant. It types-confirms, locks and re-verifies the source,
     writes in one transaction, only ever sets `is_active = false` and never re-activates. Re-run the dry run
     afterwards: `deactivate_banned_or_deleted` must be 0.
   - **Grants:** none new. The Supabase read role already holds `SELECT (id, email, encrypted_password,
     email_confirmed_at, deleted_at, banned_until) ON auth.users` (D1, `database/README.md`), which covers both flags.
     The target role is the existing `hyperchat_migrator`.
5. **Reconcile and verify.**
   - Keep the step 3 report; it reconciles the load against the snapshot.
   - After step 4, re-run the importer's dry run: it must show 0 eligible and the applied rows as
     `target_already_has_password`, and 0 `deactivate_banned_or_deleted`.
   - Run `python -B -m app.payments.operator verify` as `hyperchat_readonly`. It must report clean, or only I10
     `baseline_unverified` before Phase 2B.
   - Sign in with a migrated account.
5a. **Switch KYC enforcement to `active`, with NO grace grants** (owner decision 2026-11-27: "KYC required at
   launch"). Needs the owner's approval reference for this window:
   ```
   python -B -m app.kyc.operator report
   python -B -m app.kyc.operator activate --confirm-database hyperchat --operator <name> --authorization <approval ref>
   ```
   (migration role, with `HYPERCHAT_KYC_WITNESS_PATH` set to the production witness; `docs/kyc-rollout.md`). Never run
   `grant`. Afterwards every migrated creator needs its owner's approved Didit KYC before new donations resume: the
   load report line "KYC at launch" lists how many will be `kyc_required` (all active creators) and requires that
   none is eligible and no grace grant exists. Creators without an owner were loaded `pending` and stay out until an
   admin links an owner and KYC follows.
6. **Copy the profile and background images** (`legacy-asset-migration-plan.md` section 8, option A).
   - Until this step, `brand_logo_url` / `background_url` still hold their legacy values, e.g. `/assets/streamers/…`
     paths.
   - Copy the objects to the production bucket, apply the reviewed per-creator mapping, and check that no
     `legacy_frontend_path` cell remains for a migrated creator.
7. **Switch DNS and the frontend** only after steps 3–6 (and 5a) pass. Keep legacy read-only for rollback; any write to V2 after
   the switch makes a rollback a separately approved plan.
8. **Streamers create new OBS tokens** in the V2 dashboard and re-add their browser sources (alert, leaderboard, goal).
   No legacy overlay URL works.

**Abort:** before step 7, drop the production database, re-create it at head and repeat from step 2. After a step 4
`--apply`, follow the D1 abort rules (never re-run the importer blindly).

## Owner decisions (2026-11-27)

1. **KYC of migrated creators: "KYC required at launch".** The ETL keeps every creator on `kyc_track = 'legacy'`
   with its owner as baseline. At cutover KYC enforcement is switched to `active` with no grace grants (step 5a), so
   under `public.kyc_donation_decision` every migrated creator answers `kyc_required` until its owner's Didit KYC is
   approved. The reconciliation reports the count and fails if any creator would be eligible or a grace grant exists.
   Phase B (0040 PAN evidence, 0041 automatic activation, the payout hold) may change these rules; revisit then.
2. **Active creators without an owner: "set to pending".** The ETL loads them `pending` (other statuses unchanged), so
   they take no donations until an admin links an owner and KYC follows. This also closes the draft rule's NULL =
   NULL baseline match. The reconciliation reports the count and fails if any active creator has no owner.
3. **Old links and keys: "reset all".** Donor `status_token_hash` / `upload_token_hash` are cleared; `api_keys` and
   `partner_api_keys` are not carried over (partners get re-issued keys).
4. **Admins set a new password:** they stay excluded from the D1 import (`target_admin_account`).
5. **Soundboards start empty**, platform defaults included (accepted under the fresh start).
6. **Open, for the production session:** how a drop and re-create of `hyperchat` interacts with the production KYC
   restore witness and the backup sets (step 3).

## Future migrations (onboarding Phase B, 0038–0042)

The ETL targets 0039. 0038 adds a function and a changed trigger function; 0039 adds `creator_image_uploads`, which the load leaves empty (checked by the reconciliation). Neither adds a column to a loaded table. When a Phase B migration merges, the ETL and its CI test must follow:

- **0038** (invite guard exception, merged 03e7c02): no ETL change (checked). Legacy users get no invite rows.
- **0039** (creator image records, merged aeb3ce8): `creator_image_uploads` stays empty for migrated creators; legacy images are copied without a record (step 6).
- **0040** (multiple Didit accounts / PAN evidence): migrated owners have no KYC sessions, so nothing to import.
  Confirm that the new columns default safely for legacy creators.
- **0041** (automatic activation) and the payout hold: decide what a `legacy` creator gets. Revisit owner decision 1.
- **0042**: same rule.
- Any column added to a straight-copied table must be listed in `etl.mapping.V2_ONLY_COLUMNS` (and `LEGACY_VALUES` if
  existing rows got a value). `test_etl_mapping` fails otherwise.
- Any new table that holds rows at head must be added to `rehearsal.SEEDED_TABLES`, or the fresh-target check
  refuses the load.
