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

## What the ETL loads (head 0037)

`database/etl/rehearsal.py` loads one read-only snapshot of the restored legacy database into a **fresh** database at
head, in one transaction, then runs `database/etl/reconcile.py` and the PAY-15 verifier. Exit 0 only if every check
passes.

| Target | Source | Disposition |
|---|---|---|
| `users` | `auth_users` (+ `admin_emails` → `role = 'admin'`) | copied; `password_hash` NULL (step 4) |
| `streamers` | `streamers` | copied; `razorpay_account` from the key-name pair; `kyc_track = 'legacy'`, `kyc_baseline_owner_id = user_id` (0020); logo / background URLs kept, or rewritten with `--media-base-*` |
| `streamers_moderators`, `streamer_payment_health`, `payout_accounts`, `platform_voice_tiers`, `streamer_voice_tiers`, `soundboard_tnc_acceptances`, `terms_acceptances`, `api_keys`, `partner_api_keys`, `partner_signups`, `tts_moderation_audit`, `rate_limits`, `system_alerts`, `visits`, `reviews`, `user_queries` | same names | copied column for column |
| `banned_donors` | same | copied; 0016's `unbanned_*` columns defaulted (NULL) |
| `user_signups` | same | copied; 0021's review columns defaulted, `review_status = 'legacy_unreviewed'` (what 0021 gave existing rows; the default is now `submitted`) |
| `donations` | 133 × `<slug>_donations` | copied (amounts, statuses, moderation, messages, audio timestamps, `is_hyperemote`, `legacy_source_table`); **`voice_message_url`, `hypersound_url`, `media_url`, `media_type`, `tts_audio_url`, `status_token_hash`, `upload_token_hash` loaded as NULL**; `payment_account`, `provider_payment_id` NULL (legacy recorded none) |
| `donations.creditable_inr` / `credited_inr`, `donation_credit_events` (0031) | — | set by `public.donations_credit_backfill()` in the load transaction (see below) |
| `streamer_donator_totals` | same (`streamer_slug` → `streamer_id`) | copied **verbatim** |
| `order_lookup`, `moderation_actions` | same | copied (orphans: lookup dropped, moderation kept as audit) |
| `telegram_` / `discord_callback_mapping` | same | unexpired rows only; 0014's `recipient_id` / `moderator_id` defaulted (NULL) |
| `request_nonces` | same | unexpired rows only |
| `obs_tokens` | `obs_tokens` | **not migrated** (counted) |
| `platform_` / `streamer_soundboard_slots`, `soundboard_swap_log` | same | **not migrated** (counted): their files are not carried over |
| `auth_sessions`, `password_reset_tokens`, `admin_emails`, `profiles`, `auth_migration_control` | same | not migrated (counted) |

Left empty (nothing to import; the guards are triggers keyed on the runtime roles or are paused for the loaded
tables, so they never block the migrator):

- PAY-15: `donation_reversals` (0032), `donation_accounting_evaluations`, `payment_accounting_events`, `payment_accounting_runs` (0034), the 0035 objects. `payment_accounting_state` keeps the migration's one row, `off`.
- Payment evidence: `payment_webhook_events`, `payment_integrity_events` (0029).
- Step-up (0025–0028, 0037): `webauthn_credentials`, `stepup_*`, `session_assurance`. `stepup_enforcement` keeps its `draft` row.
- KYC (0022–0023): `kyc_sessions`, `kyc_events`, `kyc_webhook_events`, `kyc_grace_grants`, `kyc_restrictions`, `kyc_enforcement_events`. `kyc_enforcement` keeps its `draft` row; `kyc_provider_config` may already be set.
- `creator_status_events`, `application_events` (0020–0021), `user_identities`, `user_invites`, `sessions`, `jobs`, `realtime_outbox`, `moderator_notifications`, `d1_hash_import_ledger` (written by step 4).
- Pricing (0036): its triggers check only `hyperchat_app` / `hyperchat_worker` writes (and are paused during the load), so legacy minimums and voice tiers below today's floors load unchanged. The CI test loads a voice tier below the 0036 floor.

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
   - Production must be a new database migrated to head (0037 or later) with nothing else in it. The load refuses
     anything else and changes nothing.
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
5. **Reconcile and verify.**
   - Keep the step 3 report; it reconciles the load against the snapshot.
   - After step 4, re-run the importer's dry run: it must show 0 eligible and the applied rows as
     `target_already_has_password`.
   - Run `python -B -m app.payments.operator verify` as `hyperchat_readonly`. It must report clean, or only I10
     `baseline_unverified` before Phase 2B.
   - Sign in with a migrated account.
6. **Copy the profile and background images** (`legacy-asset-migration-plan.md` section 8, option A).
   - Until this step, `brand_logo_url` / `background_url` still hold their legacy values, e.g. `/assets/streamers/…`
     paths.
   - Copy the objects to the production bucket, apply the reviewed per-creator mapping, and check that no
     `legacy_frontend_path` cell remains for a migrated creator.
7. **Switch DNS and the frontend** only after steps 3–6 pass. Keep legacy read-only for rollback; any write to V2 after
   the switch makes a rollback a separately approved plan.
8. **Streamers create new OBS tokens** in the V2 dashboard and re-add their browser sources (alert, leaderboard, goal).
   No legacy overlay URL works.

**Abort:** before step 7, drop the production database, re-create it at head and repeat from step 2. After a step 4
`--apply`, follow the D1 abort rules (never re-run the importer blindly).

## Owner decisions needed before the cutover

1. **KYC state of migrated creators (CRE-4 / CRE-7).** Every migrated creator is `kyc_track = 'legacy'`, keeps its
   legacy `status`, and has its owner as `kyc_baseline_owner_id`. Under `public.kyc_donation_decision`:
   - enforcement **draft** (today): an active legacy creator whose owner is still the baseline accepts donations without
     KYC. That includes an active creator with **no owner** (NULL = NULL), which the reconciliation reports as
     `legacy/active/no_owner`. A pending or rejected creator is `not_active`.
   - **active**: a legacy creator needs its owner's approved Didit KYC, or an operator grace grant (at most 90 days,
     bound to that owner, only if the owner never had a Didit decision). Otherwise new donations answer
     `kyc_required`. No-owner creators answer `no_owner`.
   - **closed**: every creator needs its owner's own KYC.

   Options:
   - (a) launch on draft and roll out with grace grants per creator;
   - (b) activate before launch with grace grants for all active creators;
   - (c) activate without grace: legacy creators stop accepting donations until KYC;
   - plus: whether active no-owner creators are set to `pending` in legacy before the snapshot.

   Phase B (0041 automatic activation, 0040 PAN evidence and the payout hold) may change these rules.
2. **Admin passwords:** confirm admins are excluded from the D1 import and set theirs by the admin reset/bootstrap path
   (the alternative, an import run as a non-runtime role, would bypass the 0024 boundary).
3. **Donor links:** `status_token_hash` / `upload_token_hash` are cleared, so legacy donation-status and upload links
   stop working. Confirm, or keep `status_token_hash` as read-only history.
4. **`api_keys` / `partner_api_keys`** (legacy donation / audio API keys, partner keys) are copied and stay valid.
   Confirm, or rotate them as part of the fresh start.
5. **Soundboards** start empty, platform defaults included (no files are carried over).

## Future migrations (onboarding Phase B, 0038–0042)

The ETL targets 0037. When a Phase B migration merges, the ETL and its CI test must follow:

- **0038** (invite guard exception): no ETL change expected. Legacy users get no invite rows.
- **0039** (creator image assets): if it adds an image table or columns, migrated creators need a decision: no rows
  (images are registered by step 6), or rows built from `brand_logo_url` / `background_url` after the copy. A new
  `streamers` column without a default fails the load, because the streamers copy is column for column; it must be
  mapped in `load_users_and_streamers`.
- **0040** (multiple Didit accounts / PAN evidence): migrated owners have no KYC sessions, so nothing to import.
  Confirm that the new columns default safely for legacy creators.
- **0041** (automatic activation) and the payout hold: decide what a `legacy` creator gets. Revisit owner decision 1.
- **0042**: same rule.
- Any column added to a straight-copied table must be listed in `etl.mapping.V2_ONLY_COLUMNS` (and `LEGACY_VALUES` if
  existing rows got a value). `test_etl_mapping` fails otherwise.
- Any new table that holds rows at head must be added to `rehearsal.SEEDED_TABLES`, or the fresh-target check
  refuses the load.
