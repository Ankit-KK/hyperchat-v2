"""Load the restored Supabase data into the Alembic-managed schema.

    python -m etl.rehearsal --source "<dsn of hyperchat_restore_test>" \
        --target "<dsn of hyperchat_dev>" --report report.txt

* Source is read in ONE read-only REPEATABLE READ snapshot; nothing is ever written to it.
* Target is loaded in ONE transaction as hyperchat_owner: TRUNCATE everything, then COPY.
  User triggers are disabled during the load (data already passed them in the source);
  FK constraints stay enforced.
* Strict: unexpected NULLs or values make the load fail instead of inventing defaults.
* Passwords are NOT migrated here (current hashes come from Supabase Auth at cutover).
* Output: counts and sums only; no row data is printed.
"""

import argparse
import sys
from collections import Counter
from datetime import UTC, datetime

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from etl.mapping import (
    RAZORPAY_ACCOUNTS,
    normalize_currency,
    obs_token_hash,
    slug_from_table,
    split_approved_by,
    user_role,
)
from etl.reconcile import reconcile

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
    "streamer_voice_tiers", "platform_soundboard_slots", "streamer_soundboard_slots", "soundboard_swap_log",
    "soundboard_tnc_acceptances", "terms_acceptances", "banned_donors", "api_keys", "partner_api_keys",
    "partner_signups", "tts_moderation_audit", "rate_limits", "system_alerts", "user_signups", "visits",
    "reviews", "user_queries",
)


class Loader:
    def __init__(self, src: psycopg.Connection, tgt: psycopg.Connection) -> None:
        self.src, self.tgt = src, tgt
        self.stats: dict[str, dict] = {}

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
        with self.tgt.cursor() as cur:
            cur.execute("SET ROLE hyperchat_owner")
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'")
            tables = [r[0] for r in cur.fetchall()]
            cur.execute(sql.SQL("TRUNCATE {} CASCADE").format(
                sql.SQL(", ").join(sql.Identifier("public", t) for t in tables)))
            for t in tables:
                cur.execute(sql.SQL("ALTER TABLE {} DISABLE TRIGGER USER").format(sql.Identifier("public", t)))
        self.target_tables = tables

    def finish_target(self) -> None:
        with self.tgt.cursor() as cur:
            for t in self.target_tables:
                cur.execute(sql.SQL("ALTER TABLE {} ENABLE TRIGGER USER").format(sql.Identifier("public", t)))

    def load_users_and_streamers(self) -> None:
        admins = {r[0].lower() for r in self.fetch("SELECT email FROM public.admin_emails")}
        users = self.fetch("SELECT id, email, username, role, is_active, failed_login_attempts, locked_until, "
                           "supabase_user_id, created_at, updated_at, streamer_id FROM public.auth_users")
        rows, roles = [], Counter()
        for (uid, email, username, role, active, failed, locked, supa, created, updated, _sid) in users:
            r = user_role(role, email, admins)
            roles[r] += 1
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

        tgt_cols = self.columns(self.tgt, "streamers")
        account = sql.SQL("CASE {} END").format(sql.SQL(" ").join(
            sql.SQL('WHEN "razorpay-keyid" = {} AND "razorpay-keysecret" = {} THEN {}').format(
                sql.Literal(key), sql.Literal(secret), sql.Literal(name))
            for (key, secret), name in RAZORPAY_ACCOUNTS.items())).as_string(self.src)
        src_expr = [{"razorpay_key_ref": '"razorpay-keyid"', "razorpay_secret_ref": '"razorpay-keysecret"',
                     "razorpay_account": account}
                    .get(c, sql.Identifier(c).as_string(self.src)) for c in tgt_cols]
        streamers = self.fetch(f"SELECT {', '.join(src_expr)} FROM public.streamers")
        self.note("streamers", len(streamers), self.copy("streamers", tgt_cols, streamers))

        with self.tgt.cursor() as cur:
            for uid, sid in ((u[0], u[10]) for u in users if u[10] is not None):
                cur.execute("UPDATE public.users SET streamer_id = %s WHERE id = %s", (sid, uid))

    def load_obs_tokens(self) -> None:
        cols = [c for c in self.columns(self.tgt, "obs_tokens") if c != "token_hash"]
        src = self.fetch(f"SELECT token, {', '.join(cols)} FROM public.obs_tokens")
        rows = [(obs_token_hash(r[0]), *r[1:]) for r in src]
        self.note("obs_tokens", len(src), self.copy("obs_tokens", ["token_hash", *cols], rows))

    def load_straight(self) -> None:
        for table in STRAIGHT_COPIES:
            cols = self.columns(self.tgt, table)
            src_cols = set(self.columns(self.src, table))
            missing = [c for c in cols if c not in src_cols]
            if missing:
                raise RuntimeError(f"{table}: target columns missing in source: {missing}")
            rows = self.fetch(sql.SQL("SELECT {} FROM {}").format(
                sql.SQL(", ").join(map(sql.Identifier, cols)), sql.Identifier("public", table)).as_string(self.src))
            self.note(table, len(rows), self.copy(table, cols, rows))

    def load_donations(self) -> None:
        tables = [r[0] for r in self.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
            "AND table_type = 'BASE TABLE' AND table_name LIKE '%\\_donations' ORDER BY 1")]
        moderators = {r[0] for r in self.fetch("SELECT id::text FROM public.streamers_moderators")}
        slugs = {r[0]: r[1] for r in self.fetch("SELECT streamer_slug, id::text FROM public.streamers")}
        split = Counter()
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
                rows.append((did, sid, order_id, "razorpay", rzp, name, amount, normalize_currency(cur), amount_inr,
                             msg, visible, pay, mod, appr_at, mod_id, label, hyper, voice, hs, media, mtype, tts,
                             sched, played, notified, st_hash, ut_hash, table, created, updated))
        self.note("donations", len(rows), self.copy("donations", DONATION_COLUMNS, rows))
        self.stats["donations"]["source_tables"] = len(tables)
        self.stats["donations"]["approved_by_split"] = dict(split)
        self.donation_ids = {r[0] for r in rows}

    def load_donation_references(self) -> None:
        now = datetime.now(UTC)
        ol = self.fetch("SELECT id, razorpay_order_id, order_id, donation_id, created_at FROM public.order_lookup")
        keep = [r for r in ol if r[3] in self.donation_ids]
        self.note("order_lookup", len(ol), self.copy("order_lookup", ("id", "razorpay_order_id", "order_id",
                                                                      "donation_id", "created_at"), keep),
                  orphan_donation=len(ol) - len(keep))

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

        for table, why in (("auth_sessions", "legacy custom-auth sessions (all expired)"),
                           ("password_reset_tokens", "expired one-time tokens"),
                           ("admin_emails", "folded into users.role"),
                           ("profiles", "Supabase-trigger artifact, unused by the app"),
                           ("auth_migration_control", "Supabase migration switch")):
            n = self.fetch(f"SELECT count(*) FROM public.{table}")[0][0]
            self.stats[table] = {"source": n, "loaded": 0, "skipped": {"not_migrated": n}, "reason": why}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--report", required=True)
    args = ap.parse_args()

    with psycopg.connect(args.source) as src, psycopg.connect(args.target) as tgt:
        src.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        src.read_only = True
        with src.cursor() as cur:
            cur.execute("SELECT current_database(), current_setting('transaction_read_only')")
            db, ro = cur.fetchone()
        with tgt.cursor() as cur:
            cur.execute("SELECT current_database()")
            target_db = cur.fetchone()[0]
        if ro != "on" or db != "hyperchat_restore_test" or target_db in ("hyperchat", "hyperchat_restore_test"):
            raise SystemExit("refusing to run: source must be read-only hyperchat_restore_test, "
                             "target must be a dev database")

        loader = Loader(src, tgt)
        loader.prepare_target()
        loader.load_users_and_streamers()
        loader.load_obs_tokens()
        loader.load_straight()
        loader.load_donations()
        loader.load_donation_references()
        loader.finish_target()
        tgt.commit()
        src.rollback()

        ok, text = reconcile(src, tgt, loader.stats, target_db)
    with open(args.report, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
