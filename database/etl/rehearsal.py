"""Load the restored legacy (Supabase) data into a FRESH Alembic-managed database at head: the launch cutover and its
rehearsals.

    python -B -m etl.rehearsal --source "<dsn of hyperchat_restore_test>" --target "<dsn, as hyperchat_migrator>" \
        --verify-dsn "<dsn of the target as hyperchat_readonly>" --report report.txt \
        [--production-cutover] [--media-base-from https://pub-<legacy>.r2.dev ... --media-base-to <media base>]

Fresh start (owner decisions, 2026-11-27): only database data migrates (creators, donations, donor totals, settings,
moderators, users and their links). Not migrated, counted in the report:
* obs_tokens: no legacy OBS token or URL keeps working; streamers create new tokens at launch.
* platform / streamer soundboard slots and the swap log: their files are not carried over.
* donation media, voice messages and donor links (etl.mapping.DONATION_CLEARED_COLUMNS): loaded as NULL.
* api_keys and partner_api_keys: reset; new keys are issued in V2.
An active creator without an owner loads 'pending'. creator_image_uploads (0039) stays empty: legacy images are
copied by the asset plan without an image record.
Creator profile pictures and backgrounds are carried over (URLs kept, or rewritten with --media-base-*); the files are
copied afterwards by docs/operations/legacy-asset-migration-plan.md.

* Source is read in ONE read-only REPEATABLE READ snapshot (load AND reconciliation); nothing is ever written to it.
* Target must be FRESH: migrated to head and holding nothing but the migrations' own singleton rows (enforcement and
  accounting state) and the optional KYC provider config. The load refuses, before it changes anything, when any other
  table holds a row, the payment evidence tables (0029 / 0031 / 0032 / 0034) hold rows, or the PAY-15 accounting
  state is not 'off'. Nothing is ever truncated (the step-up audit tables refuse TRUNCATE; the singletons must stay).
* Target is loaded in ONE transaction as hyperchat_owner. User triggers of the tables it writes are disabled during
  the load (the data already passed the legacy rules; the guards key on the runtime roles; the 0031 credit writer must
  not credit totals that are copied verbatim); FK, CHECK and NOT NULL constraints stay enforced. Then, still in that
  transaction, public.donations_credit_backfill() (0031) gives each paid and approved donation its credit, capped by
  the copied donor totals, which are NOT changed (the documented cutover path).
* Strict: unexpected NULLs, values or columns make the load fail instead of inventing defaults. The only target columns
  a source may lack are listed in etl.mapping.V2_ONLY_COLUMNS.
* Passwords are NOT migrated here: etl/auth_hash_import.py imports them afterwards (D1).
* Output: counts and sums only; no row data is printed. Exit 0 only when every reconciliation check passes, including
  the PAY-15 verifier on the loaded database.
"""

import argparse
import sys
from collections import Counter
from datetime import UTC, datetime

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from etl.mapping import (
    DONATION_CLEARED_COLUMNS,
    LEGACY_VALUES,
    MEDIA_URL_COLUMNS,
    RAZORPAY_ACCOUNTS,
    V2_ONLY_COLUMNS,
    media_base,
    normalize_currency,
    rewrite_media_url,
    slug_from_table,
    split_approved_by,
    user_role,
)
from etl.reconcile import reconcile

# Append-only for every role: a load never runs where they hold rows (0029 payment evidence; 0031 donation credit
# audit; 0032 / 0034 reversal and accounting history).
EVIDENCE_TABLES = ("payment_webhook_events", "payment_integrity_events", "donation_credit_events",
                   "donation_reversals", "donation_accounting_evaluations", "payment_accounting_events",
                   "payment_accounting_runs")
# Rows the migrations themselves create (0023 kyc_enforcement, 0025 stepup_enforcement, 0034 payment_accounting_state)
# and operator configuration that may already be set (0022 kyc_provider_config; 0040 kyc_provider_accounts and the
# audit rows that adding or configuring them writes): allowed in a fresh target.
SEEDED_TABLES = ("alembic_version", "kyc_enforcement", "stepup_enforcement", "payment_accounting_state",
                 "kyc_provider_config", "kyc_provider_accounts", "kyc_account_events",
                 "kyc_document_policy")  # 0041: the PAN policy row the migration seeds

DONATION_COLUMNS = (
    "id", "streamer_id", "order_id", "payment_provider", "provider_order_id", "donor_name", "amount", "currency",
    "amount_inr", "message", "message_visible", "payment_status", "moderation_status", "approved_at",
    "approved_by_moderator_id", "approved_by_label", "is_hyperemote", "voice_message_url", "hypersound_url",
    "media_url", "media_type", "tts_audio_url", "audio_scheduled_at", "audio_played_at", "mod_notified",
    "status_token_hash", "upload_token_hash", "legacy_source_table", "created_at", "updated_at",
)

# Tables copied column-for-column (same names in source and target), in FK dependency order.
STRAIGHT_COPIES = (
    "streamers_moderators", "streamer_payment_health", "payout_accounts", "platform_voice_tiers",
    "streamer_voice_tiers", "soundboard_tnc_acceptances", "terms_acceptances", "banned_donors", "partner_signups",
    "tts_moderation_audit", "rate_limits", "system_alerts", "user_signups", "visits", "reviews", "user_queries",
)
OWNERLESS_PENDING = "CASE WHEN user_id IS NULL AND status = 'active' THEN 'pending' ELSE status END"
# Every table the load writes (their user triggers are paused for the load).
LOADED_TABLES = ("users", "streamers", *STRAIGHT_COPIES, "donations", "order_lookup", "moderation_actions",
                 "telegram_callback_mapping", "discord_callback_mapping", "streamer_donator_totals", "request_nonces")
# Source tables read only to count them: (table, why it is not migrated).
NOT_MIGRATED = (
    ("obs_tokens", "fresh start: no legacy OBS token or URL; streamers create new tokens at launch"),
    ("platform_soundboard_slots", "fresh start: soundboard files are not carried over"),
    ("streamer_soundboard_slots", "fresh start: soundboard files are not carried over"),
    ("soundboard_swap_log", "fresh start: soundboard files are not carried over"),
    ("api_keys", "fresh start: legacy creator API keys are reset; new keys are issued in V2"),
    ("partner_api_keys", "fresh start: partner keys are reset; partners get re-issued keys"),
    ("auth_sessions", "legacy custom-auth sessions (all expired)"),
    ("password_reset_tokens", "expired one-time tokens"),
    ("admin_emails", "folded into users.role"),
    ("profiles", "Supabase-trigger artifact, unused by the app"),
    ("auth_migration_control", "Supabase migration switch"),
)


class Loader:
    def __init__(self, src: psycopg.Connection, tgt: psycopg.Connection, *, media_from: tuple[str, ...] = (),
                 media_to: str | None = None) -> None:
        self.src, self.tgt = src, tgt
        self.stats: dict[str, dict] = {}
        self.media_from, self.media_to = media_from, media_to
        self.media_rewritten: Counter = Counter()  # "table.column" -> URLs rewritten (reported, never the URLs)
        self.credit_backfill: dict[str, int] = {}  # the 0031 credit backfill's counts, for the report

    def media(self, table: str, columns: tuple[str, ...] | list[str], rows: list[tuple]) -> list[tuple]:
        """Rewrite the legacy media base in this table's media URL columns (no-op without --media-base-to)."""
        targets = [(i, c) for i, c in enumerate(columns) if c in MEDIA_URL_COLUMNS.get(table, ())]
        if not self.media_to or not targets:
            return rows
        out = []
        for row in rows:
            row = list(row)
            for i, c in targets:
                new = rewrite_media_url(row[i], self.media_from, self.media_to)
                if new != row[i]:
                    self.media_rewritten[f"{table}.{c}"] += 1
                    row[i] = new
            out.append(tuple(row))
        return out

    # ── helpers ────────────────────────────────────────────────────────────
    def fetch(self, query: str, params: tuple | None = None) -> list[tuple]:
        # Only pass params when there are some: psycopg treats '%' as a placeholder otherwise.
        with self.src.cursor() as cur:
            cur.execute(query, params or None)
            return cur.fetchall()

    def columns(self, conn: psycopg.Connection, table: str) -> list[str]:
        with conn.cursor() as cur:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
                        "AND table_name = %s AND is_generated = 'NEVER' ORDER BY ordinal_position", (table,))
            return [r[0] for r in cur.fetchall()]

    def copy(self, table: str, columns: tuple[str, ...] | list[str], rows: list[tuple]) -> int:
        with self.tgt.cursor() as cur:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
                        "AND table_name = %s AND data_type IN ('json', 'jsonb')", (table,))
            json_cols = {r[0] for r in cur.fetchall()}
        json_idx = [i for i, c in enumerate(columns) if c in json_cols]
        stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
            sql.Identifier("public", table), sql.SQL(", ").join(map(sql.Identifier, columns)))
        with self.tgt.cursor() as cur, cur.copy(stmt) as cp:
            for row in rows:
                if json_idx:
                    row = tuple(Jsonb(v) if i in json_idx and v is not None else v for i, v in enumerate(row))
                cp.write_row(row)
        return len(rows)

    def note(self, table: str, source: int, loaded: int, **skipped: int) -> None:
        self.stats[table] = {"source": source, "loaded": loaded, "skipped": {k: v for k, v in skipped.items() if v}}

    # ── steps ──────────────────────────────────────────────────────────────
    def prepare_target(self) -> None:
        """Refuse, before any statement changes the target, unless it is a fresh database at head."""
        with self.tgt.cursor() as cur:
            cur.execute("SET ROLE hyperchat_owner")
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            tables = {r[0] for r in cur.fetchall()}

            def has_rows(table: str) -> bool:
                cur.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(sql.Identifier("public", table)))
                return cur.fetchone()[0]

            for table in EVIDENCE_TABLES:
                if table in tables and has_rows(table):
                    raise SystemExit(f"refusing to load: public.{table} holds append-only evidence (migrations "
                                     "0029-0034 keep it); nothing was changed")
            missing = [t for t in (*LOADED_TABLES, "payment_accounting_state") if t not in tables]
            if missing:
                raise SystemExit(f"refusing to load: the target is not at head (missing {missing}); "
                                 "nothing was changed")
            cur.execute("SELECT array_agg(state) FROM public.payment_accounting_state")
            if cur.fetchone()[0] != ["off"]:
                raise SystemExit("refusing to load: PAY-15 accounting is not 'off' (one row); nothing was changed")
            used = sorted(t for t in tables - set(SEEDED_TABLES) if has_rows(t))
            if used:
                raise SystemExit(f"refusing to load: the target is not fresh ({len(used)} table(s) hold rows, e.g. "
                                 f"public.{used[0]}); load only into a new database at head; nothing was changed")
            for t in LOADED_TABLES:
                cur.execute(sql.SQL("ALTER TABLE {} DISABLE TRIGGER USER").format(sql.Identifier("public", t)))

    def finish_target(self) -> None:
        with self.tgt.cursor() as cur:
            for t in LOADED_TABLES:
                cur.execute(sql.SQL("ALTER TABLE {} ENABLE TRIGGER USER").format(sql.Identifier("public", t)))

    def backfill_credits(self) -> None:
        """PAY-15 (0031): per-donation credits for the loaded donations, capped by the loaded donor totals, which
        are not changed (public.donations_credit_backfill, once per database)."""
        with self.tgt.cursor() as cur:
            cur.execute("SELECT credited, short_v2, short_imported, short_buckets "
                        "FROM public.donations_credit_backfill()")
            self.credit_backfill = dict(zip(("credited", "short_v2", "short_imported", "short_buckets"),
                                            cur.fetchone()))

    def load_users_and_streamers(self) -> None:
        admins = {r[0].lower() for r in self.fetch("SELECT email FROM public.admin_emails")}
        users = self.fetch("SELECT id, email, username, role, is_active, failed_login_attempts, locked_until, "
                           "supabase_user_id, created_at, updated_at, streamer_id FROM public.auth_users")
        rows, roles, demoted = [], Counter(), 0
        for (uid, email, username, role, active, failed, locked, supa, created, updated, _sid) in users:
            r = user_role(role, email, admins)
            roles[r] += 1
            demoted += role == "admin" and r != "admin"  # NV3-2: admin only from admin_emails
            rows.append((uid, email, None, username, r, None, active, failed, locked, supa, None, None, None,
                         created, updated))
        self.copy("users", ("id", "email", "password_hash", "username", "role", "streamer_id", "is_active",
                            "failed_login_attempts", "locked_until", "legacy_supabase_user_id",
                            "email_verified_at", "password_changed_at", "last_login_at", "created_at",
                            "updated_at"), rows)
        unmatched_admins = len(admins - {u[1].lower() for u in users})
        self.note("users", len(users), len(rows))
        self.stats["users"]["roles"] = dict(roles)
        self.stats["users"]["admin_emails_without_account"] = unmatched_admins
        self.stats["users"]["passwords_loaded"] = 0
        self.stats["users"]["legacy_admin_role_demoted"] = demoted

        tgt_cols = self.columns(self.tgt, "streamers")
        account = sql.SQL("CASE {} END").format(sql.SQL(" ").join(
            sql.SQL('WHEN "razorpay-keyid" = {} AND "razorpay-keysecret" = {} THEN {}').format(
                sql.Literal(key), sql.Literal(secret), sql.Literal(name))
            for (key, secret), name in RAZORPAY_ACCOUNTS.items())).as_string(self.src)
        # Legacy creators keep their own status, except an ACTIVE creator without an owner, which loads 'pending'
        # (owner decision 2026-11-27: no donations until an admin links an owner and KYC follows; it also closes the
        # draft rule's NULL = NULL baseline match). Every one is on the legacy KYC track, with its imported owner as
        # the baseline owner (migration 0020: the runtime roles can never set these two columns).
        src_expr = [{"razorpay_key_ref": '"razorpay-keyid"', "razorpay_secret_ref": '"razorpay-keysecret"',
                     "razorpay_account": account, "kyc_track": "'legacy'", "kyc_baseline_owner_id": '"user_id"',
                     "status": OWNERLESS_PENDING}
                    .get(c, sql.Identifier(c).as_string(self.src)) for c in tgt_cols]
        streamers = self.media("streamers", tgt_cols,
                               self.fetch(f"SELECT {', '.join(src_expr)} FROM public.streamers"))
        self.note("streamers", len(streamers), self.copy("streamers", tgt_cols, streamers))
        self.stats["streamers"]["ownerless_active_loaded_pending"] = self.fetch(
            "SELECT count(*) FROM public.streamers WHERE user_id IS NULL AND status = 'active'")[0][0]

        with self.tgt.cursor() as cur:
            for uid, sid in ((u[0], u[10]) for u in users if u[10] is not None):
                cur.execute("UPDATE public.users SET streamer_id = %s WHERE id = %s", (sid, uid))

    def load_straight(self) -> None:
        for table in STRAIGHT_COPIES:
            v2_only = V2_ONLY_COLUMNS.get(table, ())
            fixed = LEGACY_VALUES.get(table, {})
            cols = [c for c in self.columns(self.tgt, table) if c not in v2_only]
            src_cols = set(self.columns(self.src, table))
            missing = [c for c in cols if c not in src_cols]
            if missing:
                raise RuntimeError(f"{table}: target columns missing in source: {missing}")
            rows = self.media(table, cols, self.fetch(sql.SQL("SELECT {} FROM {}").format(
                sql.SQL(", ").join(map(sql.Identifier, cols)), sql.Identifier("public", table)).as_string(self.src)))
            if fixed:
                rows = [(*r, *fixed.values()) for r in rows]
            self.note(table, len(rows), self.copy(table, [*cols, *fixed], rows))

    def load_donations(self) -> None:
        tables = [r[0] for r in self.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
            "AND table_type = 'BASE TABLE' AND table_name LIKE '%\\_donations' ORDER BY 1")]
        moderators = {r[0] for r in self.fetch("SELECT id::text FROM public.streamers_moderators")}
        slugs = {r[0]: r[1] for r in self.fetch("SELECT streamer_slug, id::text FROM public.streamers")}
        split = Counter()
        cleared = Counter()
        unpaid = Counter()  # NV3-1: legacy payment_status of the rows loaded as uncompletable 'failed'
        drop = [DONATION_COLUMNS.index(c) for c in DONATION_CLEARED_COLUMNS]
        rows: list[tuple] = []
        for table in tables:
            owner = slugs[slug_from_table(table)]
            data = self.fetch(sql.SQL(
                "SELECT id, streamer_id::text, order_id, razorpay_order_id, name, amount, currency::text, amount_inr, "
                "message, message_visible, payment_status, moderation_status, approved_at, approved_by::text, "
                "is_hyperemote, voice_message_url, hypersound_url, media_url, media_type, tts_audio_url, "
                "audio_scheduled_at, audio_played_at, mod_notified, status_token_hash, upload_token_hash, "
                "created_at, updated_at FROM {}").format(sql.Identifier("public", table)).as_string(self.src))
            for d in data:
                (did, sid, order_id, rzp, name, amount, cur, amount_inr, msg, visible, pay, mod, appr_at, appr_by,
                 hyper, voice, hs, media, mtype, tts, sched, played, notified, st_hash, ut_hash, created, updated) = d
                if sid != owner:
                    raise RuntimeError(f"{table}: donation streamer_id does not match the owning streamer")
                for field, value in (("order_id", order_id), ("name", name), ("amount_inr", amount_inr),
                                     ("payment_status", pay), ("moderation_status", mod), ("created_at", created),
                                     ("updated_at", updated), ("message_visible", visible),
                                     ("is_hyperemote", hyper), ("mod_notified", notified)):
                    if value is None:
                        raise RuntimeError(f"{table}: unexpected NULL in {field}")
                mod_id, label = split_approved_by(appr_by, moderators)
                split["moderator_id" if mod_id else "label" if label else "none"] += 1
                row = [did, sid, order_id, "razorpay", rzp, name, amount, normalize_currency(cur), amount_inr,
                       msg, visible, pay, mod, appr_at, mod_id, label, hyper, voice, hs, media, mtype, tts,
                       sched, played, notified, st_hash, ut_hash, table, created, updated]
                if pay != "success":
                    # NV3-1: an unpaid legacy order can never be completed in V2 (a late capture would bypass the
                    # creator / KYC rule and PAY-15): terminal 'failed', no provider order id, no order_lookup row
                    unpaid[pay] += 1
                    row[DONATION_COLUMNS.index("payment_status")] = "failed"
                    row[DONATION_COLUMNS.index("provider_order_id")] = None
                for i in drop:  # fresh start: legacy media and donor links are never loaded
                    if row[i] is not None:
                        cleared[DONATION_COLUMNS[i]] += 1
                        row[i] = None
                rows.append(tuple(row))
        self.note("donations", len(rows), self.copy("donations", DONATION_COLUMNS, rows))
        self.stats["donations"]["source_tables"] = len(tables)
        self.stats["donations"]["approved_by_split"] = dict(split)
        self.stats["donations"]["cleared_non_null"] = dict(cleared)
        self.stats["donations"]["unpaid_loaded_failed"] = dict(unpaid)
        self.donation_ids = {r[0] for r in rows}
        self.paid_ids = {r[0] for r in rows if r[DONATION_COLUMNS.index("payment_status")] == "success"}

    def load_donation_references(self) -> None:
        now = datetime.now(UTC)
        ol = self.fetch("SELECT id, razorpay_order_id, order_id, donation_id, created_at FROM public.order_lookup")
        known = [r for r in ol if r[3] in self.donation_ids]
        keep = [r for r in known if r[3] in self.paid_ids]  # NV3-1: no lookup for an unpaid legacy order
        self.note("order_lookup", len(ol), self.copy("order_lookup", ("id", "razorpay_order_id", "order_id",
                                                                      "donation_id", "created_at"), keep),
                  orphan_donation=len(ol) - len(known), unpaid_legacy_order=len(known) - len(keep))

        ma = self.fetch("SELECT id, streamer_id, moderator_id, moderator_telegram_id, moderator_name, donation_id, "
                        "action_type, action_source, previous_status, new_status, notes, created_at "
                        "FROM public.moderation_actions")
        self.note("moderation_actions", len(ma), self.copy(
            "moderation_actions", ("id", "streamer_id", "moderator_id", "moderator_telegram_id", "moderator_name",
                                   "donation_id", "action_type", "action_source", "previous_status", "new_status",
                                   "notes", "created_at"), ma))
        self.stats["moderation_actions"]["orphan_donation_kept_as_audit"] = sum(
            1 for r in ma if r[5] not in self.donation_ids)

        for table in ("telegram_callback_mapping", "discord_callback_mapping"):
            src = self.fetch(f"SELECT id, short_id, donation_id, streamer_id, action_type, created_at, expires_at "
                             f"FROM public.{table}")
            live = [r for r in src if r[6] is not None and r[6] > now]
            keep = [r for r in live if r[2] in self.donation_ids]
            self.note(table, len(src), self.copy(table, ("id", "short_id", "donation_id", "streamer_id",
                                                         "action_type", "created_at", "expires_at"), keep),
                      expired=len(src) - len(live), orphan_donation=len(live) - len(keep))

        slugs = {r[0]: r[1] for r in self.fetch("SELECT streamer_slug, id FROM public.streamers")}
        totals = self.fetch("SELECT id, streamer_slug, donator_name, donator_name_lower, total_amount, "
                            "donation_count, updated_at FROM public.streamer_donator_totals")
        rows = [(r[0], slugs[r[1]], *r[2:]) for r in totals]
        self.note("streamer_donator_totals", len(totals), self.copy(
            "streamer_donator_totals", ("id", "streamer_id", "donator_name", "donator_name_lower", "total_amount",
                                        "donation_count", "updated_at"), rows))

        nonces = self.fetch("SELECT nonce, user_id, expires_at, created_at FROM public.request_nonces")
        live = [r for r in nonces if r[2] > now]
        self.note("request_nonces", len(nonces),
                  self.copy("request_nonces", ("nonce", "user_id", "expires_at", "created_at"), live),
                  expired=len(nonces) - len(live))

        for table, why in NOT_MIGRATED:
            n = self.fetch(f"SELECT count(*) FROM public.{table}")[0][0]
            self.stats[table] = {"source": n, "loaded": 0, "skipped": {"not_migrated": n}, "reason": why}


def run(src: psycopg.Connection, tgt: psycopg.Connection, verify_dsn: str, target_db: str, *,
        media_from: tuple[str, ...] = (), media_to: str | None = None) -> tuple[bool, str]:
    """The load, committed, then the reconciliation (still in the load's source snapshot) and the verifier."""
    src.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    src.read_only = True
    loader = Loader(src, tgt, media_from=media_from, media_to=media_to)
    loader.prepare_target()
    loader.load_users_and_streamers()
    loader.load_straight()
    loader.load_donations()
    loader.load_donation_references()
    loader.finish_target()
    loader.backfill_credits()
    tgt.commit()
    ok, text = reconcile(src, tgt, loader.stats, target_db, legacy_media_bases=media_from,
                         media_rewritten=dict(loader.media_rewritten), credit_backfill=loader.credit_backfill,
                         verify_dsn=verify_dsn)
    src.rollback()
    tgt.rollback()
    return ok, text


def check_databases(source_db: str, source_ro: str, target_db: str, production: bool) -> None:
    """The source is the read-only legacy restore; the target is a dev database, or production only with
    --production-cutover (and then nothing else)."""
    if source_ro != "on" or source_db != "hyperchat_restore_test":
        raise SystemExit("refusing to run: the source must be the read-only hyperchat_restore_test")
    if target_db == "hyperchat_restore_test":
        raise SystemExit("refusing to run: the target is the legacy source")
    if (target_db == "hyperchat") != production:
        raise SystemExit("refusing to run: the production database 'hyperchat' is loaded only with "
                         "--production-cutover, and --production-cutover loads only 'hyperchat'")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--target", required=True, help="the target as hyperchat_migrator")
    ap.add_argument("--verify-dsn", required=True, help="the same target as hyperchat_readonly (PAY-15 verifier)")
    ap.add_argument("--report", required=True)
    ap.add_argument("--production-cutover", action="store_true", help="the launch load into production 'hyperchat'")
    ap.add_argument("--media-base-from", action="append", default=[], help="legacy public media base (repeatable)")
    ap.add_argument("--media-base-to", help="the configured V2 media base (HYPERCHAT_R2_PUBLIC_BASE_URL)")
    args = ap.parse_args()
    try:
        media_from = tuple(media_base(u) for u in args.media_base_from)
        media_to = media_base(args.media_base_to) if args.media_base_to else None
    except ValueError as exc:
        raise SystemExit(f"refusing to run: {exc}") from None
    if bool(media_from) != bool(media_to) or (media_to and media_to in media_from):
        raise SystemExit("refusing to run: give --media-base-to together with at least one different "
                         "--media-base-from, or neither")

    with psycopg.connect(args.source) as src, psycopg.connect(args.target) as tgt:
        src.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        src.read_only = True
        with src.cursor() as cur:
            cur.execute("SELECT current_database(), current_setting('transaction_read_only')")
            db, ro = cur.fetchone()
        src.rollback()
        with tgt.cursor() as cur:
            cur.execute("SELECT current_database()")
            target_db = cur.fetchone()[0]
        tgt.rollback()
        check_databases(db, ro, target_db, args.production_cutover)
        ok, text = run(src, tgt, args.verify_dsn, target_db, media_from=media_from, media_to=media_to)
    with open(args.report, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
