"""Alembic round trip on a SCRATCH database (never production or the restore source).

Enabled when HYPERCHAT_TEST_MIGRATION_DSN points at a scratch database, e.g.
  postgresql+psycopg://hyperchat_migrator@/hyperchat_migtest?host=/var/run/postgresql&port=5432
"""

import os
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

DSN = os.environ.get("HYPERCHAT_TEST_MIGRATION_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="HYPERCHAT_TEST_MIGRATION_DSN not set")

DATABASE = Path(__file__).resolve().parents[1]
EXPECTED_TABLES = {
    "alembic_version", "users", "user_identities", "sessions", "password_reset_tokens", "streamers",
    "streamers_moderators", "obs_tokens", "streamer_payment_health", "payout_accounts", "platform_voice_tiers",
    "streamer_voice_tiers", "platform_soundboard_slots", "streamer_soundboard_slots", "soundboard_swap_log",
    "soundboard_tnc_acceptances", "terms_acceptances", "banned_donors", "api_keys", "partner_api_keys",
    "partner_signups", "donations", "order_lookup", "moderation_actions", "telegram_callback_mapping",
    "discord_callback_mapping", "tts_moderation_audit", "streamer_donator_totals", "request_nonces",
    "rate_limits", "system_alerts", "user_signups", "visits", "reviews", "user_queries", "contact_submissions",
    "user_invites",
}


def _cfg() -> Config:
    cfg = Config(str(DATABASE / "alembic.ini"))
    cfg.set_main_option("script_location", str(DATABASE / "migrations"))
    return cfg


def _libpq(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://")


def _tables() -> set[str]:
    with psycopg.connect(_libpq(DSN)) as conn:
        return {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}


def test_db_name_is_scratch():
    with psycopg.connect(_libpq(DSN)) as conn:
        name = conn.execute("SELECT current_database()").fetchone()[0]
    assert name not in ("hyperchat", "hyperchat_restore_test", "hyperchat_dev"), "tests must use a scratch DB"


def test_round_trip_upgrade_downgrade_upgrade(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    assert _tables() == EXPECTED_TABLES
    command.downgrade(cfg, "base")
    assert _tables() <= {"alembic_version"}
    command.upgrade(cfg, "head")
    assert _tables() == EXPECTED_TABLES


def test_no_supabase_artifacts():
    with psycopg.connect(_libpq(DSN)) as conn:
        roles = {r[0] for r in conn.execute("SELECT rolname FROM pg_roles WHERE rolname IN "
                                            "('anon', 'authenticated', 'service_role', 'supabase_admin')")}
        schemas = {r[0] for r in conn.execute("SELECT nspname FROM pg_namespace WHERE nspname IN "
                                              "('auth', 'storage', 'realtime', 'extensions', 'net', 'cron')")}
        policies = conn.execute("SELECT count(*) FROM pg_policies").fetchone()[0]
    assert not schemas and policies == 0
    # Roles are cluster-wide; on the dev cluster they exist for the restore source, so only report.
    print(f"supabase roles present in this cluster: {sorted(roles)}")


def test_everything_owned_by_owner_and_public_locked_down():
    with psycopg.connect(_libpq(DSN)) as conn:
        foreign = conn.execute("""
            SELECT count(*) FROM pg_class c
            WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r','v','m','S','i','p')
              AND pg_get_userbyid(c.relowner) <> 'hyperchat_owner'
        """).fetchone()[0]
        public_table_privs = conn.execute("""
            SELECT count(*) FROM pg_class c
            WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
              AND has_table_privilege('public', c.oid, 'SELECT')
        """).fetchone()[0]
    assert foreign == 0
    assert public_table_privs == 0
