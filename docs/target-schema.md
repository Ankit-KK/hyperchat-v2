# HyperChat target schema (Alembic-managed)

Source of truth for the design: the restored Supabase schema (`hyperchat_restore_test`, PostgreSQL 15,
dump of 2026-10-04). Every change below was checked against that data (counts only) before it was made.
Migrations: `database/migrations/versions/0001…0016` (the per-revision list is in `database/README.md`); data load: `database/etl/`.

## Clusters and roles
| Cluster | Port | Databases | Purpose |
|---|---|---|---|
| `17/hyperchat` | 5432 (localhost) | `hyperchat` | Production schema (revision 0009, not head), empty until cutover |
| `15/restoretest` | 5433 (localhost) | `hyperchat_restore_test` (read-only source), `hyperchat_dev` | Migration source + rehearsal target |

Roles (no passwords; Unix-socket peer auth through the `hyperchat` ident map):
`hyperchat_owner` (NOLOGIN, owns everything) · `hyperchat_migrator` (Alembic, `SET ROLE hyperchat_owner`, NOINHERIT) ·
`hyperchat_app` / `hyperchat_worker` (DML only) · `hyperchat_readonly` (SELECT, read-only transactions).

## Table dispositions (source → target)
| Source | Target | Change |
|---|---|---|
| 133 × `<slug>_donations` | `donations` | merged; see mapping below |
| `auth_users` | `users` | same ids; `supabase_user_id` → `legacy_supabase_user_id`; `password_hash` NULL until cutover import; email citext; new verification/login columns |
| `admin_emails` | `users.role = 'admin'` | folded in |
| — | `user_identities` | new (Google OAuth) |
| `auth_sessions` | `sessions` | new design (cookie + CSRF); legacy rows not migrated |
| — | `user_invites` | new (0008): invite-only onboarding, only the token's SHA-256 stored |
| `password_reset_tokens` | `password_reset_tokens` | FK → users; expired rows not migrated |
| `streamers` | `streamers` | `donation_table_id` dropped; Razorpay name columns → `razorpay_key_ref` / `razorpay_secret_ref`; `user_id` FK added; `razorpay_account` (0010) derived from the two name pairs (`etl.mapping.RAZORPAY_ACCOUNTS`) |
| `obs_tokens` | `obs_tokens` | plaintext `token` → `token_hash` (SHA-256); streamer_id NOT NULL |
| `order_lookup` | `order_lookup` | `streamer_slug`, `donation_table_id` dropped; FK → donations |
| `moderation_actions` | `moderation_actions` | `donation_table` dropped; `donation_id` without FK (audit, orphans kept); `actor_user_id` (0009, FK `users` ON DELETE SET NULL): the web user behind a dashboard action |
| `telegram_/discord_callback_mapping` | same | `table_name` dropped; FK → donations; only unexpired rows migrated |
| `streamer_donator_totals` | same | `streamer_slug` → `streamer_id` FK |
| slug-keyed soundboard tables, `api_keys` | same | FK on `streamer_slug` (ON UPDATE CASCADE) |
| other 20 tables | same | structure copied; FKs to `users` instead of `auth_users` / `auth.users` |
| — | `contact_submissions` | new (the contact form wrote to a table that never existed) |
| `profiles`, `auth_migration_control` | — | Supabase artifacts, not migrated |

## `donations` mapping
| Target | Source | Evidence |
|---|---|---|
| `id` | `id` | 13,840 rows, all distinct across the 133 tables |
| `streamer_id` NOT NULL | `streamer_id` | 0 null, 0 mismatched with the owning table |
| `order_id` UNIQUE | `order_id` | unique on every row |
| `payment_provider` = `razorpay` | derived | every row is a Razorpay order |
| `provider_order_id` | `razorpay_order_id` | 1 duplicate pair → indexed, not unique |
| `donor_name` | `name` | no NULL/empty values |
| `amount`, `currency` char(3), `amount_inr` numeric(12,2) | same | 8 currency codes; no precision loss |
| `payment_status` | same | pending / success / failed |
| `moderation_status` | same | pending / approved / auto_approved / rejected |
| `approved_by_moderator_id` / `approved_by_label` | `approved_by` | 655 UUIDs (all moderators) / 1,429 text values |
| media/audio/token columns | same | unchanged |
| `legacy_source_table` | table name | provenance; removed after cutover |
| dropped | `temp_voice_data`, `selected_gif_id`, sapphire `email/payment_id/signature/media_enabled/tts_enabled` | 0 non-null values |
| dropped | sapphire `status` | always the column default `pending` (58/58 rows) and never written by any application code — Edge Functions, DB functions/triggers, cron jobs or frontend (investigated 2026-10-05). The real lifecycle is in `payment_status` / `moderation_status`. The reconciliation asserts the single value `pending` and fails if it changes before cutover. |

## Deferred to the phases that use them
Business functions (`claim_next_audio`, donor totals, soundboard RPCs, rate-limit and metrics functions) and
the HyperEmote auto-approve rules are rewritten against `donations` with their endpoints. Legacy custom-auth
and Supabase-only functions, RLS policies, publications and roles are not recreated.
