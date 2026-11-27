"""Alembic round trip on a SCRATCH database (never production or the restore source).

Enabled when HYPERCHAT_TEST_MIGRATION_DSN points at a scratch database, e.g.
  postgresql+psycopg://hyperchat_migrator@/hyperchat_migtest?host=/var/run/postgresql&port=5432
"""

import io
import os
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from etl import kyc_provider_account, kyc_provider_config

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
    "user_invites", "payment_webhook_events", "jobs", "donation_transcripts", "realtime_outbox", "moderator_notifications",
    "d1_hash_import_ledger", "creator_status_events", "application_events", "kyc_sessions", "kyc_webhook_events",
    "kyc_events", "kyc_provider_config", "kyc_enforcement", "kyc_grace_grants", "kyc_restrictions",
    "kyc_enforcement_events", "webauthn_credentials", "stepup_challenges", "stepup_grants", "stepup_grant_effects",
    "session_assurance",
    "stepup_events", "stepup_rate_limits", "stepup_enforcement", "payment_integrity_events",
    "donation_credit_events", "donation_reversals",
    "payment_accounting_state", "payment_accounting_events", "payment_accounting_runs", "donation_accounting_evaluations",
    "creator_image_uploads",  # 0039
    "kyc_provider_accounts", "kyc_account_pins", "kyc_account_reservations", "kyc_account_events",  # 0040
    "kyc_document_policy", "kyc_payout_holds", "kyc_pan_acknowledgements",  # 0041
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


def _actor_user_id() -> tuple | None:
    """(type, NOT NULL, has default, FK on-delete action, FK points at users) of moderation_actions.actor_user_id."""
    with psycopg.connect(_libpq(DSN)) as conn:
        # Catalog lookups only: the migrator has no USAGE on schema public outside SET ROLE.
        return conn.execute("""
            WITH t AS (SELECT oid, relname FROM pg_class WHERE relnamespace = 'public'::regnamespace
                       AND relname IN ('moderation_actions', 'users'))
            SELECT format_type(a.atttypid, a.atttypmod), a.attnotnull, a.atthasdef, c.confdeltype::text,
                   c.confrelid = (SELECT oid FROM t WHERE relname = 'users')
            FROM pg_attribute a
            LEFT JOIN pg_constraint c ON c.conrelid = a.attrelid AND c.contype = 'f' AND c.conkey = ARRAY[a.attnum]
            WHERE a.attrelid = (SELECT oid FROM t WHERE relname = 'moderation_actions')
              AND a.attname = 'actor_user_id' AND NOT a.attisdropped
        """).fetchone()


def test_0009_actor_user_id_round_trip(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    assert _actor_user_id() == ("uuid", False, False, "n", True)  # nullable, no default, ON DELETE SET NULL
    command.downgrade(cfg, "0008")
    assert _actor_user_id() is None
    command.upgrade(cfg, "head")
    assert _actor_user_id() == ("uuid", False, False, "n", True)


def _owner():
    """Migrator connection acting as the schema owner (as env.py runs migrations)."""
    conn = psycopg.connect(_libpq(DSN), autocommit=True)
    conn.execute("SET ROLE hyperchat_owner")
    return conn


def _payment_columns() -> dict:
    with _owner() as conn:
        rows = conn.execute("""
            SELECT table_name || '.' || column_name, data_type, is_nullable, column_default
            FROM information_schema.columns WHERE table_schema = 'public' AND (
                (table_name = 'streamers' AND column_name = 'razorpay_account')
                OR (table_name = 'donations' AND column_name IN ('payment_account', 'provider_payment_id')))
        """).fetchall()
    return {r[0]: r[1:] for r in rows}


MIGTEST = ("migtest-primary", "migtest-secondary", "migtest-null", "migtest-other")


def test_0010_payments_round_trip_and_account_backfill(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0009")
    assert _payment_columns() == {} and "payment_webhook_events" not in _tables()
    with _owner() as conn:
        conn.execute("DELETE FROM public.streamers WHERE streamer_slug = ANY(%s)", (list(MIGTEST),))
        for slug, key, secret in zip(MIGTEST, ("razorpay-keyid", "razorpay-keyid(test)", None, "razorpay-keyid"),
                                     ("razorpay-keysecret", "razorpay-keysecret(test)", None, "other-secret")):
            conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, razorpay_key_ref, "
                         "razorpay_secret_ref) VALUES (%s, 'Migration Test', %s, %s)", (slug, key, secret))
    try:
        command.upgrade(cfg, "0010")  # 0018 later folds 'secondary' into 'primary' (D6)
        assert _payment_columns() == {
            "streamers.razorpay_account": ("text", "YES", "'primary'::text"),
            "donations.payment_account": ("text", "YES", None),
            "donations.provider_payment_id": ("text", "YES", None),
        }
        with _owner() as conn:
            accounts = dict(conn.execute("SELECT streamer_slug, razorpay_account FROM public.streamers "
                                         "WHERE streamer_slug = ANY(%s)", (list(MIGTEST),)).fetchall())
            assert accounts == {"migtest-primary": "primary", "migtest-secondary": "secondary",
                                "migtest-null": None, "migtest-other": None}
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute("UPDATE public.streamers SET razorpay_account = 'tertiary' WHERE streamer_slug = %s",
                             (MIGTEST[0],))
            event = ("INSERT INTO public.payment_webhook_events (event_id, event_type, account, outcome, received_at) "
                     "VALUES (%s, 'payment.captured', 'primary', %s, now())")
            conn.execute(event, ("migtest-evt", "completed"))
            with pytest.raises(psycopg.errors.UniqueViolation):
                conn.execute(event, ("migtest-evt", "already_paid"))  # same event id twice
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(event, ("migtest-evt-2", "not-an-outcome"))
            unique = conn.execute("SELECT indexdef FROM pg_indexes WHERE indexname = "
                                  "'donations_provider_payment_key'").fetchone()[0]
            assert "UNIQUE" in unique and "WHERE (provider_payment_id IS NOT NULL)" in unique
        command.downgrade(cfg, "0009")
        assert _payment_columns() == {} and "payment_webhook_events" not in _tables()
        with _owner() as conn:  # the legacy name columns are untouched by the round trip
            refs = conn.execute("SELECT streamer_slug, razorpay_key_ref, razorpay_secret_ref FROM public.streamers "
                                "WHERE streamer_slug = ANY(%s) ORDER BY 1", (list(MIGTEST),)).fetchall()
            assert refs == [("migtest-null", None, None), ("migtest-other", "razorpay-keyid", "other-secret"),
                            ("migtest-primary", "razorpay-keyid", "razorpay-keysecret"),
                            ("migtest-secondary", "razorpay-keyid(test)", "razorpay-keysecret(test)")]
    finally:
        command.upgrade(cfg, "head")
        with _owner() as conn:
            conn.execute("DELETE FROM public.streamers WHERE streamer_slug = ANY(%s)", (list(MIGTEST),))
    with _owner() as conn:  # new creators get the primary account
        conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name) VALUES ('migtest-new', 'New')")
        try:
            assert conn.execute("SELECT razorpay_account FROM public.streamers "
                                "WHERE streamer_slug = 'migtest-new'").fetchone()[0] == "primary"
        finally:
            conn.execute("DELETE FROM public.streamers WHERE streamer_slug = 'migtest-new'")


MIG18 = ("migtest18-secondary", "migtest18-primary", "migtest18-null")


def _accounts18() -> dict:
    with _owner() as conn:
        return dict(conn.execute("SELECT streamer_slug, razorpay_account FROM public.streamers "
                                 "WHERE streamer_slug = ANY(%s)", (list(MIG18),)).fetchall())


HEAD = sorted(p.name[:4] for p in (DATABASE / "migrations" / "versions").glob("[0-9]*_*.py"))[-1]


def _version() -> str:
    with _owner() as conn:
        return conn.execute("SELECT version_num FROM public.alembic_version").fetchone()[0]


def test_0018_retires_secondary(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0017")
    try:
        with _owner() as conn:
            conn.execute("DELETE FROM public.streamers WHERE streamer_slug = ANY(%s)", (list(MIG18),))
            for slug, account in zip(MIG18, ("secondary", "primary", None)):
                sid = conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, razorpay_account) "
                                   "VALUES (%s, 'Migration 18', %s) RETURNING id", (slug, account)).fetchone()[0]
            legacy = conn.execute(  # a legacy order of the NULL creator: no account recorded
                "INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, amount_inr, payment_status) "
                "VALUES (%s, 'migtest18-legacy', 'D', 10, 10, 'success') RETURNING id", (sid,)).fetchone()[0]
        command.upgrade(cfg, "0018")
        assert _accounts18() == {"migtest18-secondary": "primary", "migtest18-primary": "primary",
                                 "migtest18-null": None}
        with _owner() as conn:
            assert conn.execute("SELECT payment_account FROM public.donations WHERE id = %s",
                                (legacy,)).fetchone()[0] is None
            for statement in ("UPDATE public.streamers SET razorpay_account = 'secondary' WHERE streamer_slug = %s",
                              "UPDATE public.donations SET payment_account = 'secondary' WHERE order_id = %s"):
                with pytest.raises(psycopg.errors.CheckViolation):
                    conn.execute(statement, ("migtest18-primary" if "streamers" in statement else "migtest18-legacy",))
            conn.execute("UPDATE public.donations SET payment_account = 'primary' WHERE id = %s", (legacy,))
            conn.execute("UPDATE public.donations SET payment_account = NULL WHERE id = %s", (legacy,))
        command.downgrade(cfg, "0017")  # widens again; remaps nothing back
        assert _accounts18()["migtest18-secondary"] == "primary"
        with _owner() as conn:
            conn.execute("UPDATE public.donations SET payment_account = 'secondary' WHERE id = %s", (legacy,))
            conn.execute("UPDATE public.streamers SET razorpay_account = 'secondary' "
                         "WHERE streamer_slug = 'migtest18-secondary'")
        with pytest.raises(Exception, match="created on the secondary Razorpay account"):
            command.upgrade(cfg, "0018")  # an order on 'secondary' needs manual review: nothing changes
        assert _version() == "0017" and _accounts18()["migtest18-secondary"] == "secondary"
        with _owner() as conn:
            assert conn.execute("SELECT payment_account FROM public.donations WHERE id = %s",
                                (legacy,)).fetchone()[0] == "secondary"
    finally:
        with _owner() as conn:
            conn.execute("DELETE FROM public.donations WHERE order_id = 'migtest18-legacy'")
            conn.execute("DELETE FROM public.streamers WHERE streamer_slug = ANY(%s)", (list(MIG18),))
        command.upgrade(cfg, "head")


def _tnc_keys() -> set[str]:
    with _owner() as conn:
        return {r[0] for r in conn.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = "
                                           "'public.soundboard_tnc_acceptances'::regclass AND contype = 'u'")}


def test_0019_owner_specific_soundboard_terms(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0018")
    assert _tnc_keys() == {"UNIQUE (streamer_id, terms_version)"}
    insert = ("INSERT INTO public.soundboard_tnc_acceptances (streamer_slug, streamer_id, user_id, terms_version) "
              "VALUES ('migtest19', %s, %s, 'v1')")
    try:
        with _owner() as conn:
            conn.execute("DELETE FROM public.streamers WHERE streamer_slug = 'migtest19'")
            conn.execute("DELETE FROM public.users WHERE email::text LIKE 'migtest19-%%'")
            a, b = (conn.execute("INSERT INTO public.users (email) VALUES (%s) RETURNING id",
                                 (f"migtest19-{n}@x.invalid",)).fetchone()[0] for n in "ab")
            sid = conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, user_id) "
                               "VALUES ('migtest19', 'M19', %s) RETURNING id", (a,)).fetchone()[0]
            conn.execute(insert, (sid, a))
        command.upgrade(cfg, "0019")  # existing rows are kept as they are
        assert _tnc_keys() == {"UNIQUE (streamer_id, terms_version, user_id)"}
        with _owner() as conn:
            conn.execute(insert, (sid, b))  # a second owner accepts the same version
            with pytest.raises(psycopg.errors.UniqueViolation):
                conn.execute(insert, (sid, b))  # still once per owner
        with pytest.raises(Exception, match="acceptances by more than one owner"):
            command.downgrade(cfg, "0018")  # never deletes or merges an acceptance
        assert _version() == "0019" and _tnc_keys() == {"UNIQUE (streamer_id, terms_version, user_id)"}
        with _owner() as conn:
            assert conn.execute("SELECT count(*) FROM public.soundboard_tnc_acceptances WHERE streamer_id = %s",
                                (sid,)).fetchone()[0] == 2
            conn.execute("DELETE FROM public.soundboard_tnc_acceptances WHERE streamer_id = %s AND user_id = %s",
                         (sid, b))
        command.downgrade(cfg, "0018")
        assert _version() == "0018" and _tnc_keys() == {"UNIQUE (streamer_id, terms_version)"}
    finally:
        with _owner() as conn:
            conn.execute("DELETE FROM public.streamers WHERE streamer_slug = 'migtest19'")  # cascades acceptances
            conn.execute("DELETE FROM public.users WHERE email::text LIKE 'migtest19-%%'")
        command.upgrade(cfg, "head")


MIG20 = "migtest20-%"


def _mig20_rows() -> list[tuple]:
    with _owner() as conn:
        return conn.execute("SELECT streamer_slug, status, user_id, updated_at FROM public.streamers "
                            "WHERE streamer_slug LIKE %s ORDER BY streamer_slug", (MIG20,)).fetchall()


def test_0020_creator_lifecycle_keeps_every_existing_creator(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0019")
    try:
        with _owner() as conn:
            conn.execute("DELETE FROM public.donations WHERE order_id = 'migtest20-d'")
            conn.execute("DELETE FROM public.streamers WHERE streamer_slug LIKE %s", (MIG20,))
            conn.execute("DELETE FROM public.users WHERE email::text LIKE 'migtest20-%%'")
            owner = conn.execute("INSERT INTO public.users (email) VALUES ('migtest20-o@x.invalid') RETURNING id"
                                 ).fetchone()[0]
            owned = conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, user_id) "
                                 "VALUES ('migtest20-a', 'A', %s) RETURNING id", (owner,)).fetchone()[0]
            conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name) VALUES ('migtest20-b', 'B')")
            conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, status) "
                         "VALUES ('migtest20-p', 'P', 'pending')")
            conn.execute("INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, "
                         "amount_inr, payment_status) VALUES (%s, 'migtest20-d', 'D', 5, 'INR', 5, 'success')",
                         (owned,))
        before = _mig20_rows()
        command.upgrade(cfg, "0020")
        assert _mig20_rows() == before  # no status, owner or updated_at changed
        with _owner() as conn:
            assert conn.execute("SELECT streamer_slug, kyc_track, kyc_baseline_owner_id, "
                                "public.creator_donation_block(id) FROM public.streamers "
                                "WHERE streamer_slug LIKE %s ORDER BY streamer_slug", (MIG20,)).fetchall() == [
                ("migtest20-a", "legacy", owner, None),          # existing owned creator: still accepts donations
                ("migtest20-b", "legacy", None, None),           # existing unowned creator: unchanged
                ("migtest20-p", "legacy", None, "not_active")]   # existing pending creator: still not public
            new = conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name) "
                               "VALUES ('migtest20-n', 'N') RETURNING status, kyc_track").fetchone()
            assert new == ("pending", "standard")
            assert conn.execute("SELECT count(*) FROM public.donations WHERE order_id = 'migtest20-d'"
                                ).fetchone()[0] == 1
            conn.execute("UPDATE public.streamers SET status = 'suspended' WHERE streamer_slug = 'migtest20-b'")
        with pytest.raises(Exception, match="suspended creator"):
            command.downgrade(cfg, "0019")  # never drops a status it cannot represent
        assert _version() == "0020"
        with _owner() as conn:
            conn.execute("UPDATE public.streamers SET status = 'active' WHERE streamer_slug = 'migtest20-b'")
            conn.execute("DELETE FROM public.streamers WHERE streamer_slug = 'migtest20-n'")
        command.downgrade(cfg, "0019")
        assert _version() == "0019"
        with _owner() as conn:
            assert conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name) "
                                "VALUES ('migtest20-n', 'N') RETURNING status").fetchone()[0] == "active"
    finally:
        with _owner() as conn:
            conn.execute("DELETE FROM public.donations WHERE order_id = 'migtest20-d'")
            conn.execute("DELETE FROM public.streamers WHERE streamer_slug LIKE %s", (MIG20,))
            conn.execute("DELETE FROM public.users WHERE email::text LIKE 'migtest20-%%'")
        command.upgrade(cfg, "head")


def test_0021_creator_applications_keep_existing_signups(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0020")
    cleanup = "DELETE FROM public.user_signups WHERE email LIKE 'migtest21-%%'"
    try:
        with _owner() as conn:
            conn.execute(cleanup)
            conn.execute("INSERT INTO public.user_signups (name, email, mobile_number) "
                         "VALUES ('Old', 'migtest21-old@x.invalid', '12345678')")
            before = conn.execute("SELECT id, name, email, mobile_number, created_at FROM public.user_signups "
                                  "WHERE email LIKE 'migtest21-%%'").fetchall()
        command.upgrade(cfg, "0021")
        with _owner() as conn:
            assert conn.execute("SELECT id, name, email, mobile_number, created_at FROM public.user_signups "
                                "WHERE email LIKE 'migtest21-%%'").fetchall() == before  # nothing rewritten
            assert conn.execute("SELECT review_status, streamer_id, invite_id FROM public.user_signups "
                                "WHERE email = 'migtest21-old@x.invalid'").fetchone() == ("legacy_unreviewed", None,
                                                                                         None)
            new = conn.execute("INSERT INTO public.user_signups (name, email, mobile_number) VALUES "
                               "('New', 'migtest21-new@x.invalid', '12345678') RETURNING id, review_status").fetchone()
            assert new[1] == "submitted"
            delete_rules = dict(conn.execute(
                "SELECT conrelid::regclass::text, confdeltype FROM pg_constraint WHERE contype = 'f' AND confrelid = "
                "'public.user_signups'::regclass AND conrelid IN ('public.user_invites'::regclass, "
                "'public.application_events'::regclass)").fetchall())
            assert delete_rules == {"user_invites": "r", "application_events": "r"}  # RESTRICT: no silent loss
            with pytest.raises(psycopg.errors.CheckViolation):  # approved needs its creator
                conn.execute("UPDATE public.user_signups SET review_status = 'approved' WHERE id = %s", (new[0],))
            conn.execute("UPDATE public.user_signups SET review_status = 'rejected', rejection_reason = 'x' "
                         "WHERE id = %s", (new[0],))
        with pytest.raises(Exception, match="reviewed application"):
            command.downgrade(cfg, "0020")  # never drops a review decision
        assert _version() == "0021"
        with _owner() as conn:
            conn.execute("DELETE FROM public.user_signups WHERE email = 'migtest21-new@x.invalid'")
        command.downgrade(cfg, "0020")
        assert _version() == "0020"
        with _owner() as conn:
            assert conn.execute("SELECT count(*) FROM public.user_signups WHERE email = 'migtest21-old@x.invalid'"
                                ).fetchone()[0] == 1
    finally:
        with _owner() as conn:
            conn.execute(cleanup)
        command.upgrade(cfg, "head")


WF22 = "6ff16570-fe12-4212-9647-b12b8898cc09"


def test_0022_kyc_core_changes_nothing_existing_and_guards_its_history(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0021")
    try:
        with _owner() as conn:
            conn.execute("DELETE FROM public.users WHERE email::text LIKE 'migtest22-%%'")
            uid = conn.execute("INSERT INTO public.users (email) VALUES ('migtest22-u@x.invalid') RETURNING id"
                               ).fetchone()[0]
            users_before = conn.execute("SELECT count(*), md5(string_agg(to_jsonb(u)::text, ',' ORDER BY id)) "
                                        "FROM public.users u").fetchone()
        command.upgrade(cfg, "0022")
        with _owner() as conn:
            assert conn.execute("SELECT count(*), md5(string_agg(to_jsonb(u)::text, ',' ORDER BY id)) "
                                "FROM public.users u").fetchone() == users_before
            assert conn.execute("SELECT public.user_kyc_approved(%s, 'sandbox')", (uid,)).fetchone()[0] is False
            conn.execute("INSERT INTO public.kyc_provider_config (environment, workflow_id, webhook_secret) "
                         "VALUES ('sandbox', %s, 'whsec_migtest_0123456789')", (WF22,))  # opens the workflow epoch
            since = conn.execute("SELECT workflow_since FROM public.kyc_provider_config").fetchone()[0]
            sid = conn.execute("INSERT INTO public.kyc_sessions (user_id, vendor_data, session_id, workflow_id, "
                               "environment) VALUES (%s, %s, gen_random_uuid(), %s, 'sandbox') "
                               "RETURNING session_id", (uid, str(uid), WF22)).fetchone()[0]
            assert conn.execute("SELECT public.kyc_record_provider_status(%s, 'Approved', now(), '{}'::jsonb, "
                                "'webhook', NULL)", (sid,)).fetchone()[0] == "applied"
            assert conn.execute("SELECT public.user_kyc_approved(%s, 'sandbox'), public.user_kyc_approved(%s, 'live')",
                                (uid, uid)).fetchone() == (True, False)  # a sandbox approval never counts live
            assert conn.execute("SELECT public.kyc_record_provider_status(%s, 'In Progress', now() - interval '1 h', "
                                "NULL, 'webhook', NULL)", (sid,)).fetchone()[0] == "stale"
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute("SELECT public.kyc_record_provider_status(%s, 'Bogus', now(), NULL, 'webhook', NULL)",
                             (sid,))
            # The trust boundary: no runtime role reads the webhook secret or calls the internal writers.
            for role in ("hyperchat_app", "hyperchat_worker", "hyperchat_readonly"):
                assert conn.execute("SELECT has_table_privilege(%s, 'public.kyc_provider_config', 'SELECT')",
                                    (role,)).fetchone()[0] is False, role
            for function, allowed in (("kyc_record_provider_status(uuid, text, timestamptz, jsonb, text, uuid)", False),
                                      ("kyc_hmac_sha256(bytea, bytea)", False),
                                      ("kyc_receive_webhook(text, text, text)", True),
                                      ("kyc_apply_refresh(uuid, text, jsonb, timestamptz, uuid)", True),
                                      ("kyc_create_session(uuid, uuid, uuid, integer, text)", True)):
                assert conn.execute("SELECT has_function_privilege('hyperchat_app', %s, 'EXECUTE')",
                                    ("public." + function,)).fetchone()[0] is allowed, function
                assert conn.execute("SELECT has_function_privilege('hyperchat_worker', %s, 'EXECUTE')",
                                    ("public." + function,)).fetchone()[0] is False, function
            for table, privilege in (("kyc_sessions", "INSERT"), ("kyc_sessions", "UPDATE"), ("kyc_events", "INSERT"),
                                     ("kyc_webhook_events", "INSERT"), ("kyc_webhook_events", "DELETE"),
                                     ("kyc_webhook_events", "UPDATE")):
                assert conn.execute("SELECT has_table_privilege('hyperchat_app', %s, %s)",
                                    ("public." + table, privilege)).fetchone()[0] is False, (table, privilege)
        # The operator's tool stores the webhook secret (read from stdin, never argv).
        monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
        assert kyc_provider_config.main(["--environment", "sandbox", "--workflow-id", WF22, "--secret-stdin"]) == 2
        monkeypatch.setattr("sys.stdin", io.StringIO("  whsec_migtest_0123456789  \n"))
        assert kyc_provider_config.main(["--environment", "sandbox", "--workflow-id", WF22, "--secret-stdin"]) == 0
        with _owner() as conn:
            assert conn.execute("SELECT environment, workflow_id::text, application_id, webhook_secret "
                                "FROM public.kyc_provider_config").fetchall() == [
                ("sandbox", WF22, None, "whsec_migtest_0123456789")]
            # A secret rotation keeps the workflow epoch; a workflow change (even back) starts a new one.
            assert conn.execute("SELECT workflow_since FROM public.kyc_provider_config").fetchone()[0] == since
            assert conn.execute("SELECT public.user_kyc_approved(%s, 'sandbox')", (uid,)).fetchone()[0] is True
            conn.execute("UPDATE public.kyc_provider_config SET workflow_id = gen_random_uuid()")
            conn.execute("UPDATE public.kyc_provider_config SET workflow_id = %s, workflow_since = '2000-01-01'",
                         (WF22,))
            assert conn.execute("SELECT workflow_since FROM public.kyc_provider_config").fetchone()[0] > since
            assert conn.execute("SELECT public.user_kyc_approved(%s, 'sandbox')", (uid,)).fetchone()[0] is False
        with pytest.raises(Exception, match="KYC session"):
            command.downgrade(cfg, "0021")  # never drops verification history
        assert _version() == "0022"
        with _owner() as conn:
            conn.execute("DELETE FROM public.kyc_events WHERE user_id = %s", (uid,))
            conn.execute("DELETE FROM public.kyc_sessions WHERE user_id = %s", (uid,))
        command.downgrade(cfg, "0021")
        assert _version() == "0021"
    finally:
        with _owner() as conn:
            if "kyc_sessions" in _tables():
                conn.execute("DELETE FROM public.kyc_events WHERE user_id IN (SELECT id FROM public.users "
                             "WHERE email::text LIKE 'migtest22-%%')")
                conn.execute("DELETE FROM public.kyc_sessions WHERE user_id IN (SELECT id FROM public.users "
                             "WHERE email::text LIKE 'migtest22-%%')")
            conn.execute("DELETE FROM public.users WHERE email::text LIKE 'migtest22-%%'")
        command.upgrade(cfg, "head")


def _fk_action() -> str:
    with _owner() as conn:
        return conn.execute("SELECT confdeltype FROM pg_constraint "
                            "WHERE conname = 'creator_status_events_streamer_id_fkey'").fetchone()[0]


def _creator_snapshot(conn) -> tuple:
    return conn.execute("""
        SELECT (SELECT md5(string_agg(to_jsonb(s)::text, ',' ORDER BY id)) FROM public.streamers s),
               (SELECT md5(string_agg(to_jsonb(d)::text, ',' ORDER BY id)) FROM public.donations d),
               (SELECT md5(string_agg(to_jsonb(u)::text, ',' ORDER BY id)) FROM public.users u)""").fetchone()


def test_0023_enforcement_starts_inactive_changes_nothing_existing_and_guards_its_history(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0022")
    clean = ("DELETE FROM public.donations WHERE order_id LIKE 'migtest23-%%'",
             "DELETE FROM public.streamers WHERE streamer_slug LIKE 'migtest23-%%'",
             "DELETE FROM public.users WHERE email::text LIKE 'migtest23-%%'")
    try:
        with _owner() as conn:
            for sql in clean:
                conn.execute(sql)
            uid = conn.execute("INSERT INTO public.users (email) VALUES ('migtest23-u@x.invalid') RETURNING id"
                               ).fetchone()[0]
            sid = conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, user_id, status, kyc_track, "
                               "kyc_baseline_owner_id) VALUES ('migtest23-legacy', 'L', %s, 'active', 'legacy', %s) "
                               "RETURNING id", (uid, uid)).fetchone()[0]
            conn.execute("INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, amount_inr) "
                         "VALUES (%s, 'migtest23-o1', 'D', 5, 'INR', 5)", (sid,))
            before = _creator_snapshot(conn)
            assert conn.execute("SELECT public.creator_donation_block(%s)", (sid,)).fetchone()[0] is None
        assert _fk_action() == "c"
        command.upgrade(cfg, "0023")
        with _owner() as conn:
            assert _creator_snapshot(conn) == before  # no creator, donation or user changes
            assert conn.execute("SELECT state, generation FROM public.kyc_enforcement").fetchone() == ("draft", 0)
            assert conn.execute("SELECT public.creator_donation_block(%s)", (sid,)).fetchone()[0] is None  # as PR1
            assert conn.execute("SELECT count(*) FROM public.kyc_enforcement_events").fetchone()[0] == 0
            for function, app_allowed in (("kyc_enforcement_activate(text, text)", False),
                                          ("kyc_enforcement_close(text, text)", False),
                                          ("kyc_grant_grace(uuid, integer, text, text)", False),
                                          ("kyc_acknowledge_restore(text, bigint, text)", False),
                                          ("kyc_donation_decision(uuid, text)", False),
                                          ("kyc_lift_restriction(uuid, uuid, text, text)", False),
                                          ("kyc_owner_decided(uuid, text)", False),
                                          ("kyc_change_creator_status(uuid, text, text, uuid, text, text)", False),
                                          ("kyc_restrict_creator(uuid, text, text, uuid)", True),
                                          ("kyc_revoke_grace(uuid, text, uuid)", True)):
                for role, allowed in (("hyperchat_app", app_allowed), ("hyperchat_worker", False)):
                    assert conn.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                                        (role, "public." + function)).fetchone()[0] is allowed, (role, function)
            assert conn.execute("SELECT has_function_privilege('hyperchat_lifecycle', "
                                "'public.kyc_change_creator_status(uuid, text, text, uuid, text, text)', 'EXECUTE')"
                                ).fetchone()[0] is True  # the dedicated role, and only it
            assert conn.execute("SELECT count(*) FROM information_schema.role_table_grants "
                                "WHERE grantee = 'hyperchat_lifecycle'").fetchone()[0] == 0  # no table access
            assert conn.execute("SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND prosecdef "
                                "AND has_function_privilege('public', oid, 'EXECUTE')").fetchone()[0] == 0
            conn.execute("SELECT public.kyc_restrict_creator(%s, 'admin_hold', NULL, NULL)", (sid,))
        assert _fk_action() == "r"  # deleting a creator no longer erases its status audit
        with pytest.raises(Exception, match="downgrade of 0023 stopped"):
            command.downgrade(cfg, "0022")  # never drops enforcement history
        assert _version() == "0023"
        with _owner() as conn:  # scratch only: remove the history, then the round trip works
            conn.execute("DELETE FROM public.kyc_enforcement_events")
            conn.execute("DELETE FROM public.kyc_restrictions")
            conn.execute("ALTER TABLE public.kyc_enforcement DISABLE TRIGGER kyc_enforcement_guard")
            conn.execute("UPDATE public.kyc_enforcement SET generation = 0")
            conn.execute("ALTER TABLE public.kyc_enforcement ENABLE TRIGGER kyc_enforcement_guard")
        command.downgrade(cfg, "0022")
        assert _version() == "0022" and _fk_action() == "c"
        with _owner() as conn:
            assert conn.execute("SELECT public.creator_donation_block(%s)", (sid,)).fetchone()[0] is None
    finally:
        with _owner() as conn:
            if "kyc_enforcement_events" in _tables():
                conn.execute("DELETE FROM public.kyc_enforcement_events")
                conn.execute("DELETE FROM public.kyc_restrictions")
            for sql in clean:
                conn.execute(sql)
        command.upgrade(cfg, "head")


def _auth_guards() -> set[str]:
    with _owner() as conn:
        return {r[0] for r in conn.execute("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'aaa_auth_guard_%%'")}


def test_0024_admin_auth_boundary_round_trip(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    guards = {"aaa_auth_guard_users", "aaa_auth_guard_sessions", "aaa_auth_guard_user_identities",
              "aaa_auth_guard_password_reset_tokens", "aaa_auth_guard_user_invites"}
    assert _auth_guards() == guards
    with _owner() as conn:
        assert conn.execute("SELECT has_table_privilege('hyperchat_auth', 'public.users', 'UPDATE'), "
                            "has_table_privilege('hyperchat_auth', 'public.donations', 'SELECT'), "
                            "has_column_privilege('hyperchat_auth', 'public.streamers', 'status', 'UPDATE'), "
                            "has_column_privilege('hyperchat_auth', 'public.streamers', 'user_id', 'UPDATE')"
                            ).fetchone() == (True, False, False, True)
    command.downgrade(cfg, "0023")
    try:
        assert _auth_guards() == set()
        with _owner() as conn:
            assert conn.execute("SELECT has_table_privilege('hyperchat_auth', 'public.users', 'SELECT')"
                                ).fetchone()[0] is False
    finally:
        command.upgrade(cfg, "head")
    assert _auth_guards() == guards


STEPUP_TABLES = {"webauthn_credentials", "stepup_challenges", "stepup_grants", "stepup_grant_effects",
                 "session_assurance", "stepup_events", "stepup_rate_limits", "stepup_enforcement"}


def _stepup_privileges() -> dict:
    """{(role, table): privileges} on the step-up tables, and the enforcement state (None when absent)."""
    with _owner() as conn:
        rows = conn.execute("""
            SELECT r.rolname, c.relname, string_agg(p.priv, ',' ORDER BY p.priv)
            FROM pg_class c CROSS JOIN pg_roles r
            CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']) AS p(priv)
            WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' AND c.relname = ANY(%s)
              AND r.rolname IN ('hyperchat_app', 'hyperchat_worker', 'hyperchat_auth', 'hyperchat_lifecycle',
                                'hyperchat_readonly', 'hyperchat_stepup')
              AND (has_table_privilege(r.oid, c.oid, p.priv) OR (p.priv IN ('INSERT', 'UPDATE', 'SELECT')
                   AND has_any_column_privilege(r.oid, c.oid, p.priv)))
            GROUP BY 1, 2
        """, (list(STEPUP_TABLES),)).fetchall()
        state = conn.execute("SELECT to_regclass('public.stepup_enforcement') IS NOT NULL").fetchone()[0] and             conn.execute("SELECT state FROM public.stepup_enforcement").fetchone()[0]
    return {(r[0], r[1]): r[2] for r in rows}, state


def test_0025_stepup_core_round_trip(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    privileges, state = _stepup_privileges()
    assert state == "draft"
    assert {role for role, _ in privileges} <= {"hyperchat_stepup", "hyperchat_app", "hyperchat_worker", "hyperchat_auth",
                                               "hyperchat_lifecycle"}
    # The API's roles read the enforcement state and nothing else (0007's default DML grants were revoked).
    assert {k: v for k, v in privileges.items() if k[0] != "hyperchat_stepup"} == {
        (role, "stepup_enforcement"): "SELECT"
        for role in ("hyperchat_app", "hyperchat_worker", "hyperchat_auth", "hyperchat_lifecycle")}
    assert {t: v for (r, t), v in privileges.items() if r == "hyperchat_stepup"} == {
        "webauthn_credentials": "INSERT,SELECT,UPDATE", "stepup_challenges": "INSERT,SELECT,UPDATE",
        "stepup_grants": "INSERT,SELECT", "session_assurance": "INSERT,SELECT,UPDATE", "stepup_events": "INSERT,SELECT",
        "stepup_rate_limits": "DELETE,INSERT,SELECT,UPDATE", "stepup_enforcement": "SELECT"}
    with _owner() as conn:  # only the sign count and last use of a passkey are updatable by the verifier
        assert conn.execute("SELECT has_column_privilege('hyperchat_stepup', 'public.webauthn_credentials', 'status', "
                            "'UPDATE'), has_column_privilege('hyperchat_stepup', 'public.webauthn_credentials', "
                            "'sign_count', 'UPDATE'), has_column_privilege('hyperchat_stepup', 'public.users', "
                            "'password_hash', 'SELECT')").fetchone() == (False, True, False)
    command.downgrade(cfg, "0024")
    try:
        assert not STEPUP_TABLES & _tables()
        with _owner() as conn:
            assert conn.execute("SELECT count(*) FROM pg_proc WHERE proname LIKE 'stepup%%'").fetchone()[0] == 0
            assert conn.execute("SELECT has_schema_privilege('hyperchat_stepup', 'public', 'USAGE'), "
                                "has_any_column_privilege('hyperchat_stepup', 'public.users', 'SELECT')"
                                ).fetchone() == (False, False)
    finally:
        command.upgrade(cfg, "head")
    assert STEPUP_TABLES <= _tables() and _stepup_privileges()[1] == "draft"


def _identity_guard() -> bool:
    with _owner() as conn:
        return conn.execute("SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = "
                            "'aab_auth_guard_admin_identity_link')").fetchone()[0]


def test_0026_admin_identity_link_round_trip(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    assert _identity_guard()
    command.downgrade(cfg, "0025")
    try:
        assert not _identity_guard()
        with _owner() as conn:
            assert conn.execute("SELECT count(*) FROM pg_proc WHERE proname IN ('auth_guard_admin_identity_link', "
                                "'admin_link_google_identity')").fetchone()[0] == 0
    finally:
        command.upgrade(cfg, "head")
    assert _identity_guard()


def _stepup_guards() -> tuple[int, int]:
    """(0027's guard triggers, its functions)."""
    with _owner() as conn:
        return conn.execute("""
            SELECT (SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'aac_stepup_guard_%%'),
                   (SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname IN (
                        'stepup_canonical_json', 'stepup_params_hash', 'stepup_authorize', 'stepup_effect_or_legacy',
                        'stepup_guard_streamers', 'stepup_guard_streamer_insert', 'stepup_guard_user_signups',
                        'stepup_guard_user_invites', 'stepup_guard_users', 'stepup_guard_partner_keys'))
        """).fetchone()


def _api_role_privileges() -> set:
    """Every table and function privilege the runtime roles hold in public (0027 must grant none)."""
    with _owner() as conn:
        return set(conn.execute("""
            SELECT r.rolname, c.relname, p.priv FROM pg_class c CROSS JOIN pg_roles r
            CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']) AS p(priv)
            WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' AND r.rolname LIKE 'hyperchat\\_%%'
              AND r.rolname NOT IN ('hyperchat_owner', 'hyperchat_migrator') AND has_table_privilege(r.oid, c.oid, p.priv)
            UNION ALL
            SELECT r.rolname, f.oid::regprocedure::text, 'EXECUTE' FROM pg_proc f CROSS JOIN pg_roles r
            WHERE f.pronamespace = 'public'::regnamespace AND r.rolname LIKE 'hyperchat\\_%%'
              AND r.rolname NOT IN ('hyperchat_owner', 'hyperchat_migrator') AND has_function_privilege(r.oid, f.oid, 'EXECUTE')
        """).fetchall())


def test_0027_stepup_enforcement_round_trip(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    assert _stepup_guards() == (6, 10)
    # 0029's downgrade (on the way to 0026) restores the payment evidence grants, and 0031's restores the 0007
    # grants on donor totals and drops the credit audit: not 0027's concern (test_0029 / test_0031 pin those)
    # 0032's (reversal state, its function's EXECUTE for the API role) likewise
    evidence = ("payment_webhook_events", "payment_integrity_events", "streamer_donator_totals", "donation_reversals",
                "donation_accounting_apply(uuid,bigint,text,text[],text[])",
                "donation_credit_events",
                # 0034 (accounting state, its audit, runs, evaluation metadata, the API's state lock)
                "payment_accounting_state", "payment_accounting_events", "payment_accounting_runs",
                "donation_accounting_evaluations", "payment_accounting_lock_state()",
                # 0038 (onboarding invite exception): its function's EXECUTE for hyperchat_lifecycle, which the
                # downgrade below 0038 drops with the function; test_0038 pins that grant
                "onboarding_issue_invite(uuid,text,integer,uuid,boolean)",
                # 0039 (creator image records): hyperchat_app's SELECT, INSERT, UPDATE; test_0039 pins them
                "creator_image_uploads",
                # 0040 (several Didit accounts): its tables' and two functions' grants; test_0040 pins them
                "kyc_account_pins", "kyc_account_events", "kyc_account_reservations",
                "kyc_assign_account(uuid)", "kyc_create_session(uuid,uuid,uuid,integer,text,text)",
                # 0041 (PAN evidence): its tables' and two functions' grants; test_0041 pins them
                "kyc_document_policy", "kyc_payout_holds", "kyc_pan_acknowledgements",
                "kyc_pan_status(uuid,text)", "kyc_payout_hold(uuid)")
    with_guards = {p for p in _api_role_privileges() if p[1] not in evidence}
    at_head = _api_role_privileges()  # each exclusion names a real head privilege (not a typo that hides nothing)
    assert ("hyperchat_lifecycle", "onboarding_issue_invite(uuid,text,integer,uuid,boolean)", "EXECUTE") in at_head
    assert ("hyperchat_app", "creator_image_uploads", "INSERT") in at_head
    assert ("hyperchat_app", "kyc_assign_account(uuid)", "EXECUTE") in at_head
    assert ("hyperchat_app", "kyc_pan_acknowledgements", "INSERT") in at_head
    command.downgrade(cfg, "0026")
    try:
        assert _stepup_guards() == (0, 0)
        # 0027 neither granted nor restored anything to any API role
        assert {p for p in _api_role_privileges() if p[1] not in evidence} == with_guards
        assert _stepup_privileges()[1] == "draft"  # the enforcement state is 0025's and survives
    finally:
        command.upgrade(cfg, "head")
    assert _stepup_guards() == (6, 10) and {p for p in _api_role_privileges() if p[1] not in evidence} == with_guards


def _rollback_function() -> tuple[bool, bool]:
    """(stepup_enforcement_draft exists, stepup_enforcement.change_reason exists)."""
    with _owner() as conn:
        return conn.execute("""
            SELECT to_regprocedure('public.stepup_enforcement_draft(text, text)') IS NOT NULL,
                   EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'
                           AND table_name = 'stepup_enforcement' AND column_name = 'change_reason')
        """).fetchone()


def test_0028_stepup_enforcement_rollback_round_trip(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    assert _rollback_function() == (True, True)
    with_function = _api_role_privileges()
    assert not any("stepup_enforcement_draft" in name for _, name, _ in with_function)  # no API role may call it
    command.downgrade(cfg, "0027")
    try:
        assert _rollback_function() == (False, False)
        assert _stepup_privileges()[1] == "draft"
        with _owner() as conn:  # 0025's guard is back: an owner change still records its event
            conn.execute("UPDATE public.stepup_enforcement SET changed_by = 'migtest-0028'")
            assert conn.execute("SELECT detail FROM public.stepup_events WHERE operator = 'migtest-0028' "
                                "ORDER BY id DESC LIMIT 1").fetchone()[0] == {"from": "draft", "to": "draft"}
    finally:
        command.upgrade(cfg, "head")
    assert _rollback_function() == (True, True) and _api_role_privileges() == with_function


def _evidence_shape() -> tuple:
    """(webhook FK on-delete action, outcome CHECK has the 0029 outcomes, app privileges, worker privileges, guards)."""
    with _owner() as conn:
        fk = conn.execute("SELECT confdeltype::text FROM pg_constraint "
                          "WHERE conname = 'payment_webhook_events_donation_id_fkey'").fetchone()[0]
        check = conn.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                             "WHERE conname = 'payment_webhook_events_outcome_check'").fetchone()[0]
        privileges = tuple(
            tuple(p for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
                  if conn.execute("SELECT has_table_privilege(%s, 'public.payment_webhook_events', %s)",
                                  (role, p)).fetchone()[0])
            for role in ("hyperchat_app", "hyperchat_worker"))
        guards = {r[0] for r in conn.execute("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'payment_%%_guard%%'")}
    return fk, "integrity_recorded" in check, *privileges, guards


def _webhook_rows(conn) -> list[tuple]:
    return conn.execute("SELECT id, event_id, event_type, account, provider_order_id, provider_payment_id, donation_id, "
                        "outcome, received_at, processed_at FROM public.payment_webhook_events "
                        "WHERE event_id LIKE 'migtest29-%%' ORDER BY event_id").fetchall()


GUARDS_0029 = ("payment_integrity_events_guard", "payment_webhook_events_guard")


def _clean_0029(conn) -> None:
    """Scratch only: lift the evidence guards (owner, one transaction), remove the synthetic evidence and rows."""
    with conn.transaction():
        present = [r[0] for r in conn.execute("SELECT tgname FROM pg_trigger WHERE tgname = ANY(%s)",
                                              (list(GUARDS_0029),))]
        for name in present:
            conn.execute(f"ALTER TABLE public.{name.removesuffix('_guard')} DISABLE TRIGGER {name}")
        if "payment_integrity_events" in _tables():
            conn.execute("DELETE FROM public.payment_integrity_events WHERE webhook_event_id IN (SELECT id FROM "
                         "public.payment_webhook_events WHERE event_id LIKE 'migtest29-%%')")
        conn.execute("DELETE FROM public.payment_webhook_events WHERE event_id LIKE 'migtest29-%%'")
        for name in present:
            conn.execute(f"ALTER TABLE public.{name.removesuffix('_guard')} ENABLE TRIGGER {name}")
        conn.execute("DELETE FROM public.donations WHERE order_id LIKE 'migtest29-%%'")
        conn.execute("DELETE FROM public.streamers WHERE streamer_slug LIKE 'migtest29-%%'")
        conn.execute("DELETE FROM public.users WHERE email::text LIKE 'migtest29-%%'")


def test_0029_payment_integrity_keeps_existing_evidence_and_refuses_to_lose_it(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0028")
    full = ("SELECT", "INSERT", "UPDATE", "DELETE")
    try:
        with _owner() as conn:
            _clean_0029(conn)
            uid = conn.execute("INSERT INTO public.users (email) VALUES ('migtest29-u@x.invalid') RETURNING id"
                               ).fetchone()[0]
            sid = conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, user_id, status, kyc_track, "
                               "kyc_baseline_owner_id) VALUES ('migtest29-c', 'C', %s, 'active', 'legacy', %s) "
                               "RETURNING id", (uid, uid)).fetchone()[0]
            did = conn.execute("INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, "
                               "amount_inr, payment_status) VALUES (%s, 'migtest29-o', 'D', 5, 'INR', 5, 'success') "
                               "RETURNING id", (sid,)).fetchone()[0]
            for event_id, donation_id, outcome in (("migtest29-a-linked", did, "completed"),
                                                   ("migtest29-b-unlinked", None, "unknown_order"),
                                                   ("migtest29-c-ignored", None, "ignored")):
                conn.execute("INSERT INTO public.payment_webhook_events (event_id, event_type, account, donation_id, "
                             "outcome, received_at) VALUES (%s, 'payment.captured', 'primary', %s, %s, now())",
                             (event_id, donation_id, outcome))
            before = _webhook_rows(conn)
        assert _evidence_shape() == ("n", False, full, full, set())

        command.upgrade(cfg, "0029")
        assert _evidence_shape() == ("r", True, ("SELECT", "INSERT"), (), {
            "payment_webhook_events_guard", "payment_webhook_events_guard_truncate",
            "payment_integrity_events_guard", "payment_integrity_events_guard_truncate"})
        with _owner() as conn:
            assert _webhook_rows(conn) == before  # ids, links and timestamps untouched; nothing backfilled
            assert conn.execute("SELECT count(*) FROM public.payment_integrity_events").fetchone()[0] == 0
            for sql, state in (("UPDATE public.payment_webhook_events SET outcome = 'ignored' "
                                "WHERE event_id = 'migtest29-a-linked'", "42501"),
                               ("DELETE FROM public.payment_webhook_events WHERE event_id = 'migtest29-b-unlinked'",
                                "42501"),
                               # refused by PostgreSQL itself (the integrity table references it) before any
                               # trigger; the CASCADE form below reaches the guard
                               ("TRUNCATE public.payment_webhook_events", "0A000"),
                               ("TRUNCATE public.payment_webhook_events CASCADE", "42501"),
                               ("TRUNCATE public.donations CASCADE", "42501"),
                               ("DELETE FROM public.donations WHERE order_id = 'migtest29-o'", "23503")):
                with pytest.raises(psycopg.Error) as exc, conn.transaction(force_rollback=True):
                    conn.execute(sql)
                assert exc.value.sqlstate == state, sql
            with conn.transaction(force_rollback=True):  # an EMPTY evidence table may be truncated (cutover target)
                conn.execute("TRUNCATE public.payment_integrity_events")
            assert _webhook_rows(conn) == before
            conn.execute("INSERT INTO public.payment_webhook_events (event_id, event_type, account, outcome, received_at) "
                         "VALUES ('migtest29-d-new', 'payment.dispute.created', 'primary', 'integrity_unknown_payment', "
                         "now())")
        with pytest.raises(Exception, match="downgrade of 0029 stopped"):
            command.downgrade(cfg, "0028")  # never drops or rewrites evidence
        assert _version() == "0029"

        from etl.rehearsal import Loader  # the cutover / rehearsal load refuses BEFORE changing anything
        tgt = psycopg.connect(_libpq(DSN))
        try:
            with pytest.raises(SystemExit, match="holds append-only evidence"):
                Loader(None, tgt).prepare_target()
            tgt.rollback()
        finally:
            tgt.close()
        with _owner() as conn:
            assert _webhook_rows(conn)[:3] == before
            assert conn.execute("SELECT count(*) FROM public.donations WHERE order_id = 'migtest29-o'").fetchone()[0] == 1
            assert conn.execute("SELECT count(*) FROM public.streamers").fetchone()[0] > 0  # nothing truncated
            _clean_0029(conn)
        command.downgrade(cfg, "0028")  # no 0029 evidence left: the round trip works
        assert _evidence_shape()[:4] == ("n", False, full, full)
    finally:
        with _owner() as conn:
            _clean_0029(conn)
        command.upgrade(cfg, "head")


def test_0011_jobs_round_trip_and_constraints(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0010")
    assert not {"jobs", "donation_transcripts"} & _tables()
    command.upgrade(cfg, "head")
    assert {"jobs", "donation_transcripts"} <= _tables()
    ins = ("INSERT INTO public.jobs (kind, dedupe_key, payload, status, max_attempts, locked_by, locked_until) "
           "VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s)")
    with _owner() as conn:
        try:
            conn.execute(ins, ("donation_audio", "migtest-1", '{"donation_id": "x"}', "queued", 4, None, None))
            with pytest.raises(psycopg.errors.UniqueViolation):  # one ACTIVE job per (kind, dedupe_key)
                conn.execute(ins, ("donation_audio", "migtest-1", "{}", "queued", 4, None, None))
            conn.execute("UPDATE public.jobs SET status = 'succeeded' WHERE dedupe_key = 'migtest-1'")
            conn.execute(ins, ("donation_audio", "migtest-1", "{}", "queued", 4, None, None))  # allowed once finished
            conn.execute(ins, ("voice_transcription", "migtest-1", "{}", "queued", 3, None, None))  # other kind
            for bad in (("other_kind", "migtest-2", "{}", "queued", 4, None, None),
                        ("donation_audio", "migtest-3", "{}", "paused", 4, None, None),
                        ("donation_audio", "migtest-4", "[]", "queued", 4, None, None),
                        ("donation_audio", "migtest-5", "{}", "queued", 0, None, None),
                        ("donation_audio", "migtest-6", "{}", "running", 4, None, None),  # running needs a lease
                        ("donation_audio", "", "{}", "queued", 4, None, None)):
                with pytest.raises(psycopg.errors.CheckViolation):
                    conn.execute(ins, bad)
            indexes = {r[0] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'jobs'")}
            assert {"jobs_active_dedupe_key", "jobs_queued_idx", "jobs_running_lease_idx", "jobs_finished_idx"} <= indexes
            fks = conn.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = "
                               "'public.donation_transcripts'::regclass AND contype = 'f' ORDER BY 1").fetchall()
            assert [f[0] for f in fks] == ["FOREIGN KEY (donation_id) REFERENCES donations(id) ON DELETE CASCADE",
                                           "FOREIGN KEY (streamer_id) REFERENCES streamers(id) ON DELETE CASCADE"]
        finally:
            conn.execute("DELETE FROM public.jobs WHERE dedupe_key LIKE 'migtest-%'")


def test_0012_realtime_outbox_round_trip_and_constraints(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0011")
    assert "realtime_outbox" not in _tables() and {"jobs", "donation_transcripts"} <= _tables()
    command.upgrade(cfg, "head")
    assert "realtime_outbox" in _tables()
    ins = ("INSERT INTO public.realtime_outbox (streamer_id, channel, event, facts, coalesce_key, status, locked_by, "
           "locked_until) VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s)")
    with _owner() as conn:
        conn.execute("DELETE FROM public.streamers WHERE streamer_slug = 'migtest-rt'")
        sid = conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name) VALUES ('migtest-rt', 'RT') "
                           "RETURNING id").fetchone()[0]
        try:
            conn.execute(ins, (sid, "goal", "goal-progress", "{}", "k1", "pending", None, None))
            with pytest.raises(psycopg.errors.UniqueViolation):  # one PENDING row per coalesce key
                conn.execute(ins, (sid, "goal", "goal-progress", "{}", "k1", "pending", None, None))
            conn.execute("UPDATE public.realtime_outbox SET status = 'published' WHERE coalesce_key = 'k1'")
            conn.execute(ins, (sid, "goal", "goal-progress", "{}", "k1", "pending", None, None))  # allowed again
            conn.execute(ins, (sid, "dashboard", "new-donation", "{}", None, "pending", None, None))
            conn.execute(ins, (sid, "dashboard", "new-donation", "{}", None, "pending", None, None))  # NULL keys
            for bad in ((sid, "overlay", "new-donation", "{}", None, "pending", None, None),      # unknown channel
                        (sid, "dashboard", "stats-update", "{}", None, "pending", None, None),    # event
                        (sid, "dashboard", "new-donation", "[]", None, "pending", None, None),    # facts object
                        (sid, "dashboard", "new-donation", '{"x": "%s"}' % ("y" * 600), None, "pending", None, None),
                        (sid, "dashboard", "new-donation", "{}", "", "pending", None, None),      # empty key
                        (sid, "dashboard", "new-donation", "{}", None, "sent", None, None),       # status
                        (sid, "dashboard", "new-donation", "{}", None, "publishing", None, None)):  # needs a lease
                with pytest.raises(psycopg.errors.CheckViolation):
                    conn.execute(ins, bad)
            ids = [r[0] for r in conn.execute("SELECT id FROM public.realtime_outbox WHERE streamer_id = %s ORDER BY id",
                                              (sid,))]
            assert ids == sorted(ids) and len(ids) == 4
            indexes = {r[0] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'realtime_outbox'")}
            assert {"realtime_outbox_coalesce", "realtime_outbox_open", "realtime_outbox_due", "realtime_outbox_lease",
                    "realtime_outbox_finished"} <= indexes
            fk = conn.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = "
                              "'public.realtime_outbox'::regclass AND contype = 'f'").fetchone()[0]
            assert fk == "FOREIGN KEY (streamer_id) REFERENCES streamers(id) ON DELETE CASCADE"
            privileges = conn.execute("SELECT has_table_privilege('hyperchat_app', 'public.realtime_outbox', 'INSERT'), "
                                      "has_table_privilege('hyperchat_app', 'public.realtime_outbox', 'DELETE'), "
                                      "has_sequence_privilege('hyperchat_app', pg_get_serial_sequence("
                                      "'public.realtime_outbox', 'id'), 'USAGE')").fetchone()
            assert privileges == (True, True, True)
        finally:
            conn.execute("DELETE FROM public.streamers WHERE id = %s", (sid,))  # its outbox rows cascade
        assert conn.execute("SELECT count(*) FROM public.realtime_outbox WHERE streamer_id = %s", (sid,)).fetchone()[0] == 0


def test_0013_obs_audio_events_round_trip(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    ins = "INSERT INTO public.realtime_outbox (streamer_id, channel, event) VALUES (%s, %s, %s)"
    with _owner() as conn:
        conn.execute("DELETE FROM public.streamers WHERE streamer_slug = 'migtest-obs'")
        sid = conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name) VALUES ('migtest-obs', 'O') "
                           "RETURNING id").fetchone()[0]
    try:
        with _owner() as conn:
            conn.execute(ins, (sid, "alerts", "audio-now-playing"))
            conn.execute(ins, (sid, "alerts", "vaani-celebration"))
            conn.execute(ins, (sid, "dashboard", "new-donation"))
            for bad in (("alerts", "stats-update"), ("overlay", "audio-now-playing")):
                with pytest.raises(psycopg.errors.CheckViolation):
                    conn.execute(ins, (sid, *bad))
        command.downgrade(cfg, "0012")
        with _owner() as conn:  # the new events are removed by the downgrade; older rows stay
            events = [r[0] for r in conn.execute("SELECT event FROM public.realtime_outbox WHERE streamer_id = %s",
                                                 (sid,))]
            assert events == ["new-donation"]
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(ins, (sid, "alerts", "audio-now-playing"))
        command.upgrade(cfg, "head")
        with _owner() as conn:
            conn.execute(ins, (sid, "alerts", "audio-now-playing"))
    finally:
        command.upgrade(cfg, "head")
        with _owner() as conn:
            conn.execute("DELETE FROM public.streamers WHERE id = %s", (sid,))


def test_0014_moderation_bots_round_trip(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    assert "moderator_notifications" in _tables()
    with _owner() as conn:
        conn.execute("DELETE FROM public.streamers WHERE streamer_slug = 'migtest-bots'")
        sid = conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name) VALUES ('migtest-bots', 'B') "
                           "RETURNING id").fetchone()[0]
        did = conn.execute("INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, amount_inr) "
                           "VALUES (%s, 'migtest-bots-1', 'D', 1, 1) RETURNING id", (sid,)).fetchone()[0]
    try:
        with _owner() as conn:
            conn.execute("INSERT INTO public.jobs (kind, dedupe_key, max_attempts) VALUES ('moderator_notify', %s, 5)",
                         (str(did),))
            conn.execute("INSERT INTO public.moderation_actions (streamer_id, donation_id, action_type, action_source) "
                         "VALUES (%s, %s, 'approve', 'discord')", (sid, did))
            ins = ("INSERT INTO public.moderator_notifications (donation_id, streamer_id, platform, target_kind, "
                   "target_id, status) VALUES (%s, %s, %s, %s, %s, %s)")
            conn.execute(ins, (did, sid, "telegram", "dm", "123", "pending"))
            with pytest.raises(psycopg.errors.UniqueViolation):  # one delivery per donation x recipient
                conn.execute(ins, (did, sid, "telegram", "dm", "123", "pending"))
            for bad in (("sms", "dm", "1", "pending"), ("telegram", "group", "1", "pending"),
                        ("telegram", "dm", "", "pending"), ("telegram", "dm", "2", "delivered")):
                with pytest.raises(psycopg.errors.CheckViolation):
                    conn.execute(ins, (did, sid, *bad))
            conn.execute("INSERT INTO public.telegram_callback_mapping (short_id, donation_id, streamer_id, action_type, "
                         "recipient_id) VALUES ('ABCDEFGHIJKLMNOP', %s, %s, 'approve', '123')", (did, sid))
            for bad in (("other_kind", "dashboard"), ("donation_audio", "sms")):
                with pytest.raises(psycopg.errors.CheckViolation):
                    if bad[1] == "dashboard":
                        conn.execute("INSERT INTO public.jobs (kind, dedupe_key, max_attempts) VALUES (%s, 'x', 1)",
                                     (bad[0],))
                    else:
                        conn.execute("INSERT INTO public.moderation_actions (streamer_id, donation_id, action_type, "
                                     "action_source) VALUES (%s, %s, 'approve', %s)", (sid, did, bad[1]))
        command.downgrade(cfg, "0013")
        assert "moderator_notifications" not in _tables()
        with _owner() as conn:
            assert conn.execute("SELECT count(*) FROM public.jobs WHERE kind = 'moderator_notify'").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM public.moderation_actions WHERE action_source = 'discord'"
                                ).fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM public.telegram_callback_mapping WHERE donation_id = %s",
                                (did,)).fetchone()[0] == 0  # 16-character ids cannot go back to varchar(8)
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute("INSERT INTO public.jobs (kind, dedupe_key, max_attempts) VALUES ('moderator_notify', 'y', 5)")
        command.upgrade(cfg, "head")
        assert "moderator_notifications" in _tables()
    finally:
        command.upgrade(cfg, "head")
        with _owner() as conn:
            conn.execute("DELETE FROM public.jobs WHERE dedupe_key = %s", (str(did),))
            conn.execute("DELETE FROM public.moderation_actions WHERE streamer_id = %s", (sid,))
            conn.execute("DELETE FROM public.donations WHERE streamer_id = %s", (sid,))
            conn.execute("DELETE FROM public.streamers WHERE id = %s", (sid,))


def test_0015_bot_audit_repeats_round_trip(monkeypatch):
    """0015 drops the UNIQUE bot audit index: a decision repeated after a reversal gets its own row. The round trip
    works WITH such rows present (downgrade deliberately does not rebuild the UNIQUE index)."""
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0003")  # 0015's downgrade keeps the index dropped: rebuild 0004 for a real 0014 schema
    command.upgrade(cfg, "0014")

    def bot_index():
        with _owner() as conn:
            return conn.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'moderation_actions_unique_bot'"
                                ).fetchone()

    assert "UNIQUE" in bot_index()[0]
    command.upgrade(cfg, "head")
    assert bot_index() is None
    with _owner() as conn:
        conn.execute("DELETE FROM public.streamers WHERE streamer_slug = 'migtest-audit'")
        sid = conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name) VALUES ('migtest-audit', 'A') "
                           "RETURNING id").fetchone()[0]
        did = conn.execute("INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, amount_inr) "
                           "VALUES (%s, 'migtest-audit-1', 'D', 1, 1) RETURNING id", (sid,)).fetchone()[0]
    try:
        with _owner() as conn:
            for _ in range(2):  # reject, (approve), reject again: same donation, action, source and user
                conn.execute("INSERT INTO public.moderation_actions (streamer_id, donation_id, action_type, "
                             "action_source, moderator_telegram_id) VALUES (%s, %s, 'reject', 'telegram', '1')",
                             (sid, did))
        command.downgrade(cfg, "0014")
        assert bot_index() is None
        command.upgrade(cfg, "head")
        with _owner() as conn:
            assert conn.execute("SELECT count(*) FROM public.moderation_actions WHERE donation_id = %s",
                                (did,)).fetchone()[0] == 2
    finally:
        command.upgrade(cfg, "head")
        with _owner() as conn:
            conn.execute("DELETE FROM public.moderation_actions WHERE streamer_id = %s", (sid,))
            conn.execute("DELETE FROM public.donations WHERE streamer_id = %s", (sid,))
            conn.execute("DELETE FROM public.streamers WHERE id = %s", (sid,))


def test_0016_banned_donor_unban_round_trip(monkeypatch):
    """0016 adds the nullable unban audit columns to banned_donors (existing rows keep NULL); downgrade drops them."""
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    cols = ("unbanned_at", "unbanned_by_label", "unbanned_by_user_id")

    def present():
        with _owner() as conn:
            return {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE "
                                               "table_name = 'banned_donors' AND column_name = ANY(%s)", (list(cols),))}

    assert present() == set(cols)
    with _owner() as conn:
        conn.execute("DELETE FROM public.streamers WHERE streamer_slug = 'migtest-unban'")
        sid = conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name) VALUES ('migtest-unban', 'U') "
                           "RETURNING id").fetchone()[0]
        uid = conn.execute("INSERT INTO public.users (email) VALUES ('migtest-unban@authtest.invalid') "
                           "RETURNING id").fetchone()[0]
    try:
        with _owner() as conn:
            ban = conn.execute("INSERT INTO public.banned_donors (streamer_id, donor_name, is_active, unbanned_at, "
                               "unbanned_by_label, unbanned_by_user_id) VALUES (%s, 'D', false, now(), 'L', %s) "
                               "RETURNING id", (sid, uid)).fetchone()[0]
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute("INSERT INTO public.banned_donors (streamer_id, donor_name, unbanned_by_label) "
                             "VALUES (%s, 'E', %s)", (sid, "x" * 101))
            conn.execute("DELETE FROM public.users WHERE id = %s", (uid,))  # ON DELETE SET NULL keeps the history
            assert conn.execute("SELECT unbanned_by_user_id, unbanned_by_label FROM public.banned_donors "
                                "WHERE id = %s", (ban,)).fetchone() == (None, "L")
        command.downgrade(cfg, "0015")
        assert present() == set()
        with _owner() as conn:  # the ban row itself survives the downgrade
            assert conn.execute("SELECT count(*) FROM public.banned_donors WHERE id = %s", (ban,)).fetchone()[0] == 1
        command.upgrade(cfg, "head")
        assert present() == set(cols)
    finally:
        command.upgrade(cfg, "head")
        with _owner() as conn:
            conn.execute("DELETE FROM public.users WHERE email = 'migtest-unban@authtest.invalid'")
            conn.execute("DELETE FROM public.streamers WHERE id = %s", (sid,))


def test_0017_d1_hash_import_ledger_round_trip_privileges_and_constraints(monkeypatch):
    """The D1 import ledger: append-only for the API role, nothing for the worker, readable for reporting; one row per
    (run, user); digests and fingerprints are 64 lowercase hex characters; a deleted user takes its rows with it."""
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0016")
    assert "d1_hash_import_ledger" not in _tables() and "banned_donors" in _tables()
    command.upgrade(cfg, "head")
    assert "d1_hash_import_ledger" in _tables()
    checks = [("hyperchat_app", "SELECT", True), ("hyperchat_app", "INSERT", True), ("hyperchat_app", "UPDATE", False),
              ("hyperchat_app", "DELETE", False), ("hyperchat_app", "TRUNCATE", False),
              ("hyperchat_worker", "SELECT", False), ("hyperchat_worker", "INSERT", False),
              ("hyperchat_readonly", "SELECT", True), ("hyperchat_readonly", "INSERT", False)]
    ins = "INSERT INTO public.d1_hash_import_ledger (run_id, user_id, hash_sha256, source_fingerprint) VALUES (%s, %s, %s, %s)"
    run, digest = "00000000-0000-0000-0000-0000000d1017", "a" * 64
    with _owner() as conn:
        got = conn.execute("SELECT " + ", ".join(f"has_table_privilege('{r}', 'public.d1_hash_import_ledger', '{p}')"
                                                 for r, p, _ in checks)).fetchone()
        assert list(got) == [want for _, _, want in checks]
        conn.execute("DELETE FROM public.users WHERE email = 'migtest-ledger@authtest.invalid'")
        uid = conn.execute("INSERT INTO public.users (email) VALUES ('migtest-ledger@authtest.invalid') "
                           "RETURNING id").fetchone()[0]
        try:
            conn.execute(ins, (run, uid, digest, digest))
            with pytest.raises(psycopg.errors.UniqueViolation):  # one row per run and user
                conn.execute(ins, (run, uid, digest, digest))
            for bad in (("A" * 64, digest), ("a" * 63, digest), (digest, "not-a-fingerprint")):
                with pytest.raises(psycopg.errors.CheckViolation):
                    conn.execute(ins, ("00000000-0000-0000-0000-0000000d1018", uid, *bad))
            # the operator SQL in docs/operations/d1-auth-freeze/ runs against the real schema, as written
            ops = DATABASE.parent / "docs" / "operations" / "d1-auth-freeze"
            conn.execute("UPDATE public.users SET password_hash = %s WHERE id = %s", ("x", uid))
            conn.execute("UPDATE public.d1_hash_import_ledger SET hash_sha256 = encode(sha256('x'::bytea), 'hex') "
                         "WHERE run_id = %s", (run,))
            for name in ("import_status.sql", "rollback_import.sql"):
                conn.execute((ops / name).read_text(encoding="utf-8").replace(":'run_id'", f"'{run}'"))
            assert conn.execute("SELECT password_hash FROM public.users WHERE id = %s", (uid,)).fetchone()[0] is None
        finally:
            conn.execute("DELETE FROM public.users WHERE id = %s", (uid,))  # its ledger rows cascade
        assert conn.execute("SELECT count(*) FROM public.d1_hash_import_ledger WHERE user_id = %s",
                            (uid,)).fetchone()[0] == 0


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


def _history_guard() -> tuple:
    """(trigger present, function present, any API role may execute the function) for migration 0030."""
    with _owner() as conn:
        trigger = conn.execute("SELECT count(*) FROM pg_trigger WHERE tgname = 'donations_protect_history_columns' "
                               "AND tgrelid = 'public.donations'::regclass").fetchone()[0] == 1
        fn = conn.execute("SELECT to_regprocedure('public.donations_protect_history_columns()')").fetchone()[0]
        executable = fn is not None and any(
            conn.execute("SELECT has_function_privilege(%s, 'public.donations_protect_history_columns()', 'EXECUTE')",
                         (role,)).fetchone()[0]
            for role in ("hyperchat_app", "hyperchat_worker", "hyperchat_auth", "hyperchat_lifecycle",
                         "hyperchat_stepup", "hyperchat_readonly"))
    return trigger, fn is not None, executable


def _clean_0030(conn) -> None:
    conn.execute("DELETE FROM public.donations WHERE order_id LIKE 'migtest30-%%'")
    conn.execute("DELETE FROM public.streamers WHERE streamer_slug LIKE 'migtest30-%%'")


def test_0030_donation_history_guard_round_trip_keeps_every_row(monkeypatch):
    """0030 adds only a trigger: imported HyperEmote history and V2 rows are read, never changed; the downgrade
    removes exactly the trigger and its function."""
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0029")
    try:
        assert _history_guard() == (False, False, False)
        with _owner() as conn:
            _clean_0030(conn)
            sid = conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, status, kyc_track) "
                               "VALUES ('migtest30-c', 'C', 'active', 'legacy') RETURNING id").fetchone()[0]
            for order_id, emote, source in (("migtest30-imported", True, "migtest30_donations"),
                                            ("migtest30-v2", False, None)):
                conn.execute("INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, "
                             "amount_inr, payment_status, is_hyperemote, legacy_source_table) "
                             "VALUES (%s, %s, 'D', 5, 'INR', 5, 'success', %s, %s)", (sid, order_id, emote, source))
            before = conn.execute("SELECT order_id, is_hyperemote, legacy_source_table, updated_at FROM public.donations "
                                  "WHERE order_id LIKE 'migtest30-%%' ORDER BY order_id").fetchall()

        command.upgrade(cfg, "0030")
        assert _history_guard() == (True, True, False)
        with _owner() as conn:  # the owner (migrations, the legacy import) is not an API role: still free
            assert conn.execute("SELECT order_id, is_hyperemote, legacy_source_table, updated_at FROM public.donations "
                                "WHERE order_id LIKE 'migtest30-%%' ORDER BY order_id").fetchall() == before
            conn.execute("UPDATE public.donations SET is_hyperemote = false WHERE order_id = 'migtest30-imported'")
            conn.execute("UPDATE public.donations SET is_hyperemote = true WHERE order_id = 'migtest30-imported'")

        command.downgrade(cfg, "0029")
        assert _history_guard() == (False, False, False)
    finally:
        with _owner() as conn:
            _clean_0030(conn)
        command.upgrade(cfg, "head")
    assert _history_guard() == (True, True, False)


def _credit_shape() -> tuple:
    """(credit columns present, writer + delete guard triggers present, app/worker privileges on totals)."""
    with _owner() as conn:
        cols = conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND "
                            "table_name = 'donations' AND column_name IN ('creditable_inr', 'credited_inr')"
                            ).fetchone()[0]
        writer = conn.execute("SELECT count(*) FROM pg_trigger WHERE tgname IN ('donations_credit_writer', "
                              "'donations_credit_delete_guard', 'donations_credit_delete_guard_truncate')").fetchone()[0]
        privileges = tuple(
            tuple(p for p in ("SELECT", "INSERT", "UPDATE", "DELETE")
                  if conn.execute("SELECT has_table_privilege(%s, 'public.streamer_donator_totals', %s)",
                                  (role, p)).fetchone()[0])
            for role in ("hyperchat_app", "hyperchat_worker"))
    return cols, writer, *privileges


def _clean_0031(conn) -> None:
    """Scratch only: lift the audit guard (owner, one transaction), remove the synthetic audit, totals and rows."""
    with conn.transaction():
        if "donation_credit_events" in _tables():
            conn.execute("ALTER TABLE public.donation_credit_events DISABLE TRIGGER donation_credit_events_guard")
            conn.execute("DELETE FROM public.donation_credit_events WHERE streamer_id IN "
                         "(SELECT id FROM public.streamers WHERE streamer_slug LIKE 'migtest31-%%')")
            conn.execute("ALTER TABLE public.donation_credit_events ENABLE TRIGGER donation_credit_events_guard")
        guarded = conn.execute("SELECT count(*) FROM pg_trigger WHERE tgname = 'donations_credit_delete_guard'"
                               ).fetchone()[0]
        conn.execute("DELETE FROM public.streamer_donator_totals WHERE streamer_id IN "
                     "(SELECT id FROM public.streamers WHERE streamer_slug LIKE 'migtest31-%%')")
        if guarded:
            conn.execute("ALTER TABLE public.donations DISABLE TRIGGER donations_credit_delete_guard")
        conn.execute("DELETE FROM public.donations WHERE order_id LIKE 'migtest31-%%'")
        if guarded:
            conn.execute("ALTER TABLE public.donations ENABLE TRIGGER donations_credit_delete_guard")
        conn.execute("DELETE FROM public.streamers WHERE streamer_slug LIKE 'migtest31-%%'")


FULL_DML = ("SELECT", "INSERT", "UPDATE", "DELETE")
# (order_id suffix, donor, amount, imported, days ago, paid + approved, expected credit after the 0031 backfill).
# Per bucket: V2 rows first, then imported rows (even older ones), oldest first, each min(amount, what is left).
DONATIONS_0031 = (
    ("full-v2-old", "Fan", 100, False, 9, True, 100), ("full-v2-new", "Fan", 50, False, 1, True, 50),
    ("full-imported", "fan", 80, True, 30, True, 80),                     # same bucket (lower-cased name)
    ("full-pending", "Fan", 70, False, 2, False, None),                   # not approved: untouched, ceiling = amount
    ("short-v2-old", "Short", 100, False, 9, True, 100), ("short-v2-new", "Short", 60, False, 1, True, 20),
    ("short-imported", "Short", 40, True, 30, True, 0),
    ("imp-v2", "Imp", 30, False, 5, True, 30), ("imp-imported", "Imp", 50, True, 40, True, 30),
    ("none-v2", "Nobody", 25, False, 3, True, 0),                         # no totals row at all
)
TOTALS_0031 = (("Fan", 230, 3), ("Short", 120, 2), ("Imp", 60, 2))


def test_0031_capped_backfill_keeps_every_donor_total_and_refuses_to_lose_its_audit(monkeypatch):
    """0031 credits existing donations up to what each donor total really holds (short buckets leave the newest rows
    partly credited, imported rows after V2 rows); no total and no updated_at changes; the downgrade refuses while
    the credit audit has rows."""
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0030")
    try:
        assert _credit_shape() == (0, 0, FULL_DML, FULL_DML)
        with _owner() as conn:
            _clean_0031(conn)
            sid = conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, status, kyc_track) "
                               "VALUES ('migtest31-c', 'C', 'active', 'legacy') RETURNING id").fetchone()[0]
            for suffix, donor, amount, imported, days, approved, _ in DONATIONS_0031:
                conn.execute("INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, "
                             "amount_inr, payment_status, moderation_status, legacy_source_table, created_at) VALUES "
                             "(%s, %s, %s, %s, 'INR', %s, 'success', %s, %s, now() - make_interval(days => %s))",
                             (sid, "migtest31-" + suffix, donor, amount, amount, "approved" if approved else "pending",
                              "migtest31_donations" if imported else None, days))
            for donor, total, count in TOTALS_0031:
                conn.execute("INSERT INTO public.streamer_donator_totals (streamer_id, donator_name, "
                             "donator_name_lower, total_amount, donation_count) VALUES (%s, %s, lower(%s), %s, %s)",
                             (sid, donor, donor, total, count))
            totals_sql = ("SELECT donator_name, donator_name_lower, total_amount, donation_count, updated_at FROM "
                          "public.streamer_donator_totals WHERE streamer_id = %s ORDER BY donator_name_lower")
            rows_sql = ("SELECT order_id, updated_at FROM public.donations WHERE order_id LIKE 'migtest31-%%' "
                        "ORDER BY order_id")
            totals_before = conn.execute(totals_sql, (sid,)).fetchall()
            rows_before = conn.execute(rows_sql).fetchall()

        command.upgrade(cfg, "0031")
        assert _credit_shape() == (2, 3, ("SELECT",), ("SELECT",))
        with _owner() as conn:
            assert conn.execute(totals_sql, (sid,)).fetchall() == totals_before  # byte-identical donor totals
            assert conn.execute(rows_sql).fetchall() == rows_before              # no donation looks modified
            got = {r[0]: (r[1], r[2]) for r in conn.execute(
                "SELECT order_id, credited_inr, creditable_inr FROM public.donations "
                "WHERE order_id LIKE 'migtest31-%%'").fetchall()}
            for suffix, _, _, _, _, approved, credit in DONATIONS_0031:
                want = (Decimal(credit), Decimal(credit)) if approved else (Decimal(0), None)
                assert got["migtest31-" + suffix] == want, suffix  # exact numeric(12,2) values
                assert all(v is None or isinstance(v, Decimal) for v in got["migtest31-" + suffix]), suffix
            audit = conn.execute("SELECT d.order_id, e.cause, e.delta_inr, e.delta_count, e.total_after FROM "
                                 "public.donation_credit_events e JOIN public.donations d ON d.id = e.donation_id "
                                 "WHERE e.streamer_id = %s", (sid,)).fetchall()
            assert {r[0].removeprefix("migtest31-"): r[2] for r in audit} == {  # one row per credit > 0
                d[0]: d[6] for d in DONATIONS_0031 if d[5] and d[6]}
            assert len(audit) == len({r[0] for r in audit})
            assert {(r[1], r[3], r[4]) for r in audit} == {("backfill", 0, None)}
            for donor, total, _ in TOTALS_0031:  # never credited beyond the total: a later debit cannot fail
                assert conn.execute("SELECT sum(credited_inr) FROM public.donations WHERE streamer_id = %s AND "
                                    "lower(donor_name) = lower(%s)", (sid, donor)).fetchone()[0] <= total
            with pytest.raises(psycopg.Error) as again, conn.transaction(force_rollback=True):
                conn.execute("SELECT * FROM public.donations_credit_backfill()")
            assert again.value.sqlstate == "55000"                               # it runs once
            conn.execute("UPDATE public.donations SET moderation_status = 'rejected' "
                         "WHERE order_id = 'migtest31-short-v2-new'")
            assert conn.execute("SELECT total_amount FROM public.streamer_donator_totals WHERE streamer_id = %s "
                                "AND donator_name_lower = 'short'", (sid,)).fetchone()[0] == 100  # its 20 ceiling only

        with pytest.raises(Exception, match="downgrade of 0031 stopped"):
            command.downgrade(cfg, "0030")
        assert _version() == "0031"
        with _owner() as conn:
            _clean_0031(conn)
        command.downgrade(cfg, "0030")  # no audit left: the round trip works and the 0007 grants are back
        assert _credit_shape() == (0, 0, FULL_DML, FULL_DML)
    finally:
        with _owner() as conn:
            _clean_0031(conn)
        command.upgrade(cfg, "head")
    assert _credit_shape()[:2] == (2, 3)


def _reversal_shape() -> tuple:
    """(reversal table, apply function, roles that may EXECUTE it, app privileges on the table, ledger columns)."""
    with _owner() as conn:
        table = conn.execute("SELECT to_regclass('public.donation_reversals') IS NOT NULL").fetchone()[0]
        fn = "public.donation_accounting_apply(uuid, bigint, text, text[], text[])"
        exists = conn.execute("SELECT to_regprocedure(%s) IS NOT NULL", (fn,)).fetchone()[0]
        callers = tuple(r for r in ("hyperchat_app", "hyperchat_worker", "hyperchat_auth", "hyperchat_lifecycle",
                                    "hyperchat_stepup", "hyperchat_readonly")
                        if exists and conn.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                                                   (r, fn)).fetchone()[0])
        app = tuple(p for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
                    if table and conn.execute("SELECT has_table_privilege('hyperchat_app', 'public.donation_reversals', "
                                              "%s)", (p,)).fetchone()[0])
        cols = conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND "
                            "table_name = 'donation_credit_events' AND column_name IN ('reversed_before_paise', "
                            "'reversed_after_paise', 'hold_before', 'hold_after', 'evidence_refs', 'txid')"
                            ).fetchone()[0]
    return table, exists, callers, app, cols


def _clean_0032(conn) -> None:
    """Scratch only (owner, one transaction, guards lifted explicitly): this test's reversal state, history, rows."""
    with conn.transaction():
        if "donation_reversals" in _tables():
            conn.execute("DELETE FROM public.donation_reversals WHERE donation_id IN (SELECT id FROM public.donations "
                         "WHERE order_id LIKE 'migtest32-%%')")
        if "donation_credit_events" in _tables():
            conn.execute("ALTER TABLE public.donation_credit_events DISABLE TRIGGER donation_credit_events_guard")
            conn.execute("DELETE FROM public.donation_credit_events WHERE streamer_id IN "
                         "(SELECT id FROM public.streamers WHERE streamer_slug LIKE 'migtest32-%%')")
            conn.execute("ALTER TABLE public.donation_credit_events ENABLE TRIGGER donation_credit_events_guard")
        conn.execute("DELETE FROM public.streamer_donator_totals WHERE streamer_id IN "
                     "(SELECT id FROM public.streamers WHERE streamer_slug LIKE 'migtest32-%%')")
        guarded = conn.execute("SELECT count(*) FROM pg_trigger WHERE tgname = 'donations_credit_delete_guard'"
                               ).fetchone()[0]
        if guarded:
            conn.execute("ALTER TABLE public.donations DISABLE TRIGGER donations_credit_delete_guard")
        conn.execute("DELETE FROM public.donations WHERE order_id LIKE 'migtest32-%%'")
        if guarded:
            conn.execute("ALTER TABLE public.donations ENABLE TRIGGER donations_credit_delete_guard")
        conn.execute("DELETE FROM public.streamers WHERE streamer_slug LIKE 'migtest32-%%'")


def test_0032_reversal_accounting_keeps_0031_history_changes_no_money_and_refuses_to_lose_state(monkeypatch):
    """0032 adds the reversal state, its function and the ledger columns; 0031's ledger rows stay valid and
    unchanged; with no reversal the writer moves exactly what 0031's did; the downgrade refuses while reversal state
    or accounting history exists, otherwise restores 0031 exactly."""
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0031")
    try:
        assert _reversal_shape() == (False, False, (), (), 0)
        with _owner() as conn:
            _clean_0032(conn)
            sid = conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, status, kyc_track) "
                               "VALUES ('migtest32-c', 'C', 'active', 'legacy') RETURNING id").fetchone()[0]
            for order_id, moderation in (("migtest32-a", "approved"), ("migtest32-b", "pending")):
                conn.execute("INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, "
                             "amount_inr, payment_status, moderation_status, provider_payment_id, payment_account, "
                             "provider_order_id) VALUES (%s, %s, 'Fan', 100, 'INR', 100, 'success', %s, %s, 'primary', %s)",
                             (sid, order_id, moderation, "pay_MIG" + order_id[-1], "order_MIG" + order_id[-1]))
            history_sql = ("SELECT e.id, e.cause, e.delta_inr, e.total_after, e.created_at FROM public.donation_credit_events e "
                           "WHERE e.streamer_id = %s ORDER BY e.id")
            history = conn.execute(history_sql, (sid,)).fetchall()  # 0031's writer row for the approved donation
            assert [r[1] for r in history] == ["writer"]

        command.upgrade(cfg, "0032")
        assert _reversal_shape() == (True, True, ("hyperchat_app",), ("SELECT",), 6)
        with _owner() as conn:
            assert conn.execute(history_sql, (sid,)).fetchall() == history  # 0031's rows unchanged and still valid
            assert conn.execute("SELECT reversed_before_paise, hold_after, txid FROM public.donation_credit_events "
                                "WHERE streamer_id = %s", (sid,)).fetchall() == [(None, None, None)]
            conn.execute("UPDATE public.donations SET moderation_status = 'approved' WHERE order_id = 'migtest32-b'")
            assert conn.execute("SELECT total_amount, donation_count FROM public.streamer_donator_totals WHERE "
                                "streamer_id = %s", (sid,)).fetchone() == (200, 2)  # no reversal: 0031's amounts
            writer = conn.execute("SELECT reversed_before_paise, reversed_after_paise, txid IS NOT NULL FROM "
                                  "public.donation_credit_events WHERE streamer_id = %s ORDER BY id DESC LIMIT 1",
                                  (sid,)).fetchone()
            assert writer == (0, 0, True)
            did = conn.execute("SELECT id FROM public.donations WHERE order_id = 'migtest32-a'").fetchone()[0]
            assert conn.execute("SELECT public.donation_accounting_apply(%s, NULL, 'H-CONFLICT', '{}', '{}')",
                                (did,)).fetchone()[0] == "hold"

        with pytest.raises(Exception, match="downgrade of 0032 stopped"):
            command.downgrade(cfg, "0031")
        assert _version() == "0032"
        with _owner() as conn:
            _clean_0032(conn)
        command.downgrade(cfg, "0031")  # nothing left: 0031 exactly (no columns, no table, no function)
        assert _reversal_shape() == (False, False, (), (), 0)
        assert _credit_shape() == (2, 3, ("SELECT",), ("SELECT",))  # 0031's writer and guards are back
    finally:
        with _owner() as conn:
            _clean_0032(conn)
        command.upgrade(cfg, "head")
    assert _reversal_shape()[:3] == (True, True, ("hyperchat_app",))


def _snapshot_shape() -> tuple:
    """(column present, CHECK present, app may SELECT / INSERT it, worker may SELECT it, readonly may SELECT it)."""
    with _owner() as conn:
        col = conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND "
                           "table_name = 'payment_integrity_events' AND column_name = 'payment_amount_refunded_paise'"
                           ).fetchone()[0] == 1
        check = conn.execute("SELECT count(*) FROM pg_constraint WHERE conname = 'payment_integrity_refund_snapshot'"
                             ).fetchone()[0] == 1
        if not col:
            return col, check, None, None, None
        priv = lambda role, p: conn.execute(  # noqa: E731
            "SELECT has_column_privilege(%s, 'public.payment_integrity_events', 'payment_amount_refunded_paise', %s)",
            (role, p)).fetchone()[0]
        return col, check, (priv("hyperchat_app", "SELECT"), priv("hyperchat_app", "INSERT")), \
            priv("hyperchat_worker", "SELECT"), priv("hyperchat_readonly", "SELECT")


def _clean_0033(conn) -> None:
    """Scratch only: lift the 0029 evidence guards (owner, one transaction), remove this test's evidence and rows."""
    with conn.transaction():
        for name in GUARDS_0029:
            conn.execute(f"ALTER TABLE public.{name.removesuffix('_guard')} DISABLE TRIGGER {name}")
        conn.execute("DELETE FROM public.payment_integrity_events WHERE webhook_event_id IN (SELECT id FROM "
                     "public.payment_webhook_events WHERE event_id LIKE 'migtest33-%%')")
        conn.execute("DELETE FROM public.payment_webhook_events WHERE event_id LIKE 'migtest33-%%'")
        for name in GUARDS_0029:
            conn.execute(f"ALTER TABLE public.{name.removesuffix('_guard')} ENABLE TRIGGER {name}")


def _evidence33(conn, event_id: str, ref: str, snapshot=None, with_column: bool = True):
    wid = conn.execute("INSERT INTO public.payment_webhook_events (event_id, event_type, account, outcome, received_at) "
                       "VALUES (%s, 'refund.processed', 'primary', 'integrity_unknown_payment', now()) RETURNING id",
                       (event_id,)).fetchone()[0]
    cols = ", payment_amount_refunded_paise" if with_column else ""
    vals = ", %s" if with_column else ""
    conn.execute("INSERT INTO public.payment_integrity_events (webhook_event_id, match_type, account, kind, event_type, "
                 f"provider_ref, provider_payment_id, status, amount_paise, currency, consistent, received_at{cols}) "
                 "VALUES (%s, 'none', 'primary', 'refund', 'refund.processed', %s, 'pay_MIG33x', 'processed', 100, "
                 f"'INR', true, now(){vals})", (wid, ref, *((snapshot,) if with_column else ())))


def test_0033_refund_snapshot_column_keeps_evidence_append_only_and_refuses_to_lose_snapshots(monkeypatch):
    """0033 adds one nullable, non-negative column: old rows stay NULL, the view and the append-only guard are
    unchanged, table-level grants cover it; the downgrade refuses while any snapshot exists."""
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0032")
    try:
        assert _snapshot_shape()[:2] == (False, False)
        with _owner() as conn:
            _clean_0033(conn)
            _evidence33(conn, "migtest33-old", "rfnd_MIG33old", with_column=False)  # recorded before 0033
            view_cols = [r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND "
                "table_name = 'payment_integrity_current' ORDER BY ordinal_position")]

        command.upgrade(cfg, "0033")
        assert _snapshot_shape() == (True, True, (True, True), False, True)
        with _owner() as conn:
            assert conn.execute("SELECT payment_amount_refunded_paise FROM public.payment_integrity_events e JOIN "
                                "public.payment_webhook_events w ON w.id = e.webhook_event_id WHERE w.event_id = "
                                "'migtest33-old'").fetchone()[0] is None  # no backfill
            assert [r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND "
                "table_name = 'payment_integrity_current' ORDER BY ordinal_position")] == view_cols  # view unchanged
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction(force_rollback=True):
                _evidence33(conn, "migtest33-neg", "rfnd_MIG33neg", snapshot=-1)
            _evidence33(conn, "migtest33-new", "rfnd_MIG33new", snapshot=100)
            for sql in ("UPDATE public.payment_integrity_events SET payment_amount_refunded_paise = 0 "
                        "WHERE provider_ref = 'rfnd_MIG33new'",
                        "DELETE FROM public.payment_integrity_events WHERE provider_ref = 'rfnd_MIG33new'"):
                with pytest.raises(psycopg.Error) as exc, conn.transaction(force_rollback=True):
                    conn.execute(sql)
                assert exc.value.sqlstate == "42501", sql  # still append-only for the owner

        with pytest.raises(Exception, match="downgrade of 0033 stopped"):
            command.downgrade(cfg, "0032")
        assert _version() == "0033"
        with _owner() as conn:
            _clean_0033(conn)  # controlled scratch recovery: no snapshot left
        command.downgrade(cfg, "0032")
        assert _snapshot_shape()[:2] == (False, False)
    finally:
        with _owner() as conn:
            _clean_0033(conn)
        command.upgrade(cfg, "head")
    assert _snapshot_shape()[:2] == (True, True)


ACTIVATION_TABLES = ("payment_accounting_state", "payment_accounting_events", "payment_accounting_runs",
                     "donation_accounting_evaluations")
APPLY_32 = "public.donation_accounting_apply(uuid, bigint, text, text[], text[])"


def _activation_shape() -> tuple:
    """(0034 tables present, run_id column present, H-DISPUTE-MODE allowed on reversals, the apply function's body,
    runtime-role EXECUTE on the 0034 functions)."""
    with _owner() as conn:
        tables = tuple(t for t in ACTIVATION_TABLES
                       if conn.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{t}",)).fetchone()[0])
        run_col = conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND "
                               "table_name = 'donation_credit_events' AND column_name = 'run_id'").fetchone()[0] == 1
        holds = conn.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = "
                             "'donation_reversals_hold_check'").fetchone()[0]
        body = conn.execute("SELECT pg_get_functiondef(%s::regprocedure)", (APPLY_32,)).fetchone()[0]
        # one string per (grantee, function): every runtime role, and PUBLIC, that may EXECUTE a 0034 function
        executes = tuple(sorted(row[0] for row in conn.execute("""
            SELECT r.rolname || ':' || f.proname FROM pg_proc f CROSS JOIN pg_roles r
            WHERE f.pronamespace = 'public'::regnamespace AND (f.proname LIKE 'payment\\_accounting\\_%%'
                  OR f.proname IN ('payment_evidence_fp', 'donation_accounting_core'))
              AND r.rolname LIKE 'hyperchat\\_%%' AND r.rolname NOT IN ('hyperchat_owner', 'hyperchat_migrator')
              AND has_function_privilege(r.oid, f.oid, 'EXECUTE')
            UNION ALL
            SELECT 'PUBLIC:' || f.proname FROM pg_proc f
            WHERE f.pronamespace = 'public'::regnamespace AND (f.proname LIKE 'payment\\_accounting\\_%%'
                  OR f.proname IN ('payment_evidence_fp', 'donation_accounting_core'))
              AND has_function_privilege('public', f.oid, 'EXECUTE')""").fetchall()))
    return tables, run_col, "H-DISPUTE-MODE" in holds, body, executes


def _clean_0034(conn) -> None:
    """Scratch only (owner, one transaction, guards lifted explicitly): back to the state 0034 creates."""
    if "payment_accounting_state" not in _tables():
        return
    with conn.transaction():
        conn.execute("DELETE FROM public.donation_accounting_evaluations")  # first: 0035's ack_run key names runs
        for table in ("payment_accounting_events", "payment_accounting_runs"):
            conn.execute(f"ALTER TABLE public.{table} DISABLE TRIGGER {table}_guard")
            conn.execute(f"DELETE FROM public.{table}")
            conn.execute(f"ALTER TABLE public.{table} ENABLE TRIGGER {table}_guard")
        conn.execute("ALTER TABLE public.payment_accounting_state DISABLE TRIGGER payment_accounting_state_guard")
        conn.execute("UPDATE public.payment_accounting_state SET state = 'off', dispute_mode = 'hold'")
        conn.execute("ALTER TABLE public.payment_accounting_state ENABLE TRIGGER payment_accounting_state_guard")


def test_0034_accounting_state_starts_off_is_guarded_and_the_downgrade_restores_0032_apply(monkeypatch):
    """0034 adds the switch (off, dispute mode hold), its audit, runs and evaluation metadata; the runtime roles may
    only read them (the API also takes the state lock and calls apply); the downgrade refuses once anything was
    changed or recorded, otherwise restores 0033 exactly, 0032's apply function byte for byte."""
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    with _owner() as conn:
        _clean_0034(conn)
    command.downgrade(cfg, "0033")
    try:
        before = _activation_shape()
        assert before[:3] == ((), False, False) and before[4] == ()

        command.upgrade(cfg, "0034")
        tables, run_col, dispute_hold, body, executes = _activation_shape()
        assert (tables, run_col, dispute_hold) == (ACTIVATION_TABLES, True, True)
        assert body != before[3] and "payment_accounting_state" in body
        assert executes == ("hyperchat_app:payment_accounting_lock_state",)  # apply is 0032's grant
        with _owner() as conn:
            assert conn.execute("SELECT state, dispute_mode, changed_by FROM public.payment_accounting_state"
                                ).fetchone() == ("off", "hold", "migration 0034")
            assert sorted(conn.execute(  # S6A-D6: the 5 s lock timeout is the function's own setting
                "SELECT proconfig FROM pg_proc WHERE oid = 'public.payment_accounting_set_state(text, text, text, "
                "text, text)'::regprocedure").fetchone()[0]) == ["lock_timeout=5s", "search_path=pg_catalog, public"]
            assert conn.execute("SELECT count(*) FROM public.payment_accounting_events").fetchone()[0] == 0
            for role in ("hyperchat_app", "hyperchat_worker"):
                for table in ACTIVATION_TABLES:
                    assert [p for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE") if conn.execute(
                        "SELECT has_table_privilege(%s, %s, %s)", (role, f"public.{table}", p)).fetchone()[0]
                    ] == ["SELECT"], (role, table)
            for sql, state in (("UPDATE public.payment_accounting_state SET state = 'active'", "42501"),  # no reason
                               ("SELECT public.payment_accounting_set_state('frozen', NULL, 'op', "
                                "'off cannot go to frozen', NULL)", "22023"),
                               ("DELETE FROM public.payment_accounting_state", "42501")):
                with pytest.raises(psycopg.Error) as exc, conn.transaction(force_rollback=True):
                    conn.execute(sql)
                assert exc.value.sqlstate == state, sql
            assert conn.execute("SELECT public.donation_accounting_apply(gen_random_uuid(), 0, NULL, '{}', '{}')"
                                ).fetchone()[0] == "inactive"  # off: nothing is evaluated, not even looked up
            assert conn.execute("SELECT public.payment_accounting_set_state('active', NULL, 'op-migtest', "
                                "'a scratch activation', NULL)").fetchone()[0] == "active"

        with pytest.raises(Exception, match="downgrade of 0034 stopped"):
            command.downgrade(cfg, "0033")
        assert _version() == "0034"
        with _owner() as conn:
            _clean_0034(conn)  # controlled scratch recovery: state off / hold, no audit or metadata
        command.downgrade(cfg, "0033")
        assert _activation_shape() == before  # 0032's apply restored exactly, 0032's hold vocabulary, no run_id
    finally:
        with _owner() as conn:
            _clean_0034(conn)
        command.upgrade(cfg, "head")
    assert _activation_shape()[:3] == (ACTIVATION_TABLES, True, True)


# ── 0035: F-7 (run keys, the owner-function-only acknowledgement) and F-11 (runs bound to their scope) ──
OPERATIONS_0035 = ("payment_accounting_reconcile", "payment_accounting_keep", "payment_accounting_apply_evaluation",
                   "payment_accounting_revert")


def _hardening_shape() -> tuple:
    """(the two run keys, the ack guard trigger, the scope function, md5(prosrc) of the four operations)."""
    with _owner() as conn:
        keys = tuple(r[0] for r in conn.execute(
            "SELECT conname FROM pg_constraint WHERE conname IN ('donation_accounting_evaluations_ack_run', "
            "'donation_credit_events_run_id') ORDER BY conname"))
        guard = conn.execute("SELECT count(*) FROM pg_trigger WHERE tgname = "
                             "'donation_accounting_evaluations_ack_guard'").fetchone()[0] == 1
        scope = conn.execute("SELECT to_regproc('public.payment_accounting_scope_covers') IS NOT NULL").fetchone()[0]
        bodies = tuple(conn.execute("SELECT md5(prosrc) FROM pg_proc WHERE proname = %s", (n,)).fetchone()[0]
                       for n in OPERATIONS_0035)
    return keys, guard, scope, bodies


def test_0035_binds_runs_to_their_scope_guards_the_acknowledgement_and_downgrades_to_0034_exactly(monkeypatch):
    """0035 adds the two run keys and the acknowledgement guard (F-7) and the use-time scope check (F-11); an unscoped
    run can still be opened but not used; the downgrade loses no data and restores 0034's four bodies byte for byte."""
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    with _owner() as conn:
        _clean_0034(conn)
    command.downgrade(cfg, "0034")
    try:
        before = _hardening_shape()
        assert before[:3] == ((), False, False)
        command.upgrade(cfg, "0035")
        keys, guard, scope, bodies = _hardening_shape()
        assert keys == ("donation_accounting_evaluations_ack_run", "donation_credit_events_run_id") and guard and scope
        assert all(a != b for a, b in zip(bodies, before[3])), "every operation gains the scoped run check"
        with _owner() as conn:
            for role in ("hyperchat_app", "hyperchat_worker", "hyperchat_readonly"):
                assert not conn.execute("SELECT has_function_privilege(%s, 'public.payment_accounting_scope_covers("
                                        "jsonb, timestamp with time zone, text, uuid)', 'EXECUTE')",
                                        (role,)).fetchone()[0], role
            assert conn.execute("SELECT public.payment_accounting_set_state('active', NULL, 'op-migtest', "
                                "'a scratch activation', NULL)").fetchone()[0] == "active"
            unscoped = conn.execute("SELECT public.payment_accounting_open_run('reconcile', 'op-migtest', "
                                    "'a scratch unscoped run', NULL, '{}')").fetchone()[0]  # still allowed: D-10
            listed = conn.execute(
                "SELECT public.payment_accounting_open_run('reconcile', 'op-migtest', 'a scratch scoped run', NULL, "
                "jsonb_build_object('kind', 'donations', 'donations', jsonb_build_array(%s::text), 'expires_at', "
                "to_char(now() AT TIME ZONE 'UTC' + interval '1 hour', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"+00:00\"')))",
                ("00000000-0000-4000-8000-000000000035",)).fetchone()[0]
            reconcile = "SELECT public.payment_accounting_reconcile(%s::uuid, 0, NULL, '{}', '{}', %s)"
            for did, run in (("00000000-0000-4000-8000-000000000035", unscoped),   # unscoped: never usable
                             ("00000000-0000-4000-8000-000000000036", listed)):    # not listed
                with pytest.raises(psycopg.Error) as exc, conn.transaction(force_rollback=True):
                    conn.execute(reconcile, (did, run))
                assert exc.value.sqlstate == "42501" and "(F-11)" in str(exc.value), (did, run)
            with pytest.raises(psycopg.Error) as exc, conn.transaction(force_rollback=True):
                conn.execute(reconcile, ("00000000-0000-4000-8000-000000000035", listed))
            assert exc.value.sqlstate == "22023" and "no donation" in str(exc.value)  # the scope passed; the core ran
            ack = ("INSERT INTO public.donation_accounting_evaluations (donation_id, evidence_fp, evaluated_at, state, "
                   "dispute_mode, outcome, ack_fp, ack_run) VALUES (gen_random_uuid(), 'x', now(), 'active', 'hold', "
                   "'kept', 'x', %s)")
            with pytest.raises(psycopg.Error) as exc, conn.transaction(force_rollback=True):
                conn.execute(ack, (listed,))  # no keep marked the transaction
            assert exc.value.sqlstate == "42501" and "(F-7)" in str(exc.value)
            with pytest.raises(psycopg.Error) as exc, conn.transaction(force_rollback=True):
                conn.execute("SELECT set_config('hyperchat.accounting_ack', '1', true)")
                conn.execute(ack, (987654321,))  # a run that does not exist
            assert exc.value.sqlstate == "23503"
            with conn.transaction(force_rollback=True):  # an evaluation without an acknowledgement is unaffected
                conn.execute("INSERT INTO public.donation_accounting_evaluations (donation_id, evidence_fp, "
                             "evaluated_at, state, dispute_mode, outcome) VALUES (gen_random_uuid(), 'x', now(), "
                             "'active', 'hold', 'evaluation')")
        command.downgrade(cfg, "0034")  # nothing is lost: not refused, even with runs present
        assert _hardening_shape() == before  # 0034's bodies byte for byte
    finally:
        with _owner() as conn:
            _clean_0034(conn)
        command.upgrade(cfg, "head")
    assert _hardening_shape()[:3] == (("donation_accounting_evaluations_ack_run", "donation_credit_events_run_id"),
                                      True, True)


# ── PAY-15 stage 6b: the DEV packet's SQL on a fresh 0001 -> 0029 -> 0035 database ─────────────────────────────────

PACKET_6B = DATABASE.parent / "deploy" / "pay15-6b"
DONATIONS_6B = (  # (suffix, donor, amount, moderation, minutes ago): a full bucket, a short bucket, no credit
    ("full-a", "Fan", 100, "approved", 60), ("full-b", "Fan", 50, "auto_approved", 50),
    ("short-a", "Short", 100, "approved", 40), ("short-b", "Short", 60, "approved", 30),  # oldest credited first
    ("pending", "Fan", 70, "pending", 20), ("rejected", "Fan", 40, "rejected", 10),
)
TOTALS_6B = (("Fan", 150, 2), ("Short", 120, 2))  # Short holds 120 of 160: the newest row is credited 20 of 60


def _packet_sql(name: str) -> list[str]:
    """The packet's SQL file, run as psql -At would (one single-column line per statement, read only)."""
    statements = [s.strip() for s in (PACKET_6B / "sql" / name).read_text().split(";\n") if s.strip()]
    statements = ["\n".join(line for line in s.splitlines() if not line.lstrip().startswith("--")) for s in statements]
    with psycopg.connect(_libpq(DSN), autocommit=True) as conn:
        conn.execute("SET ROLE hyperchat_owner")  # reads every table, as the packet's postgres session does on DEV
        conn.execute("SET default_transaction_read_only = on")
        return [conn.execute(s).fetchone()[0] for s in statements if s.strip()]


def _verify_expected() -> list[str]:
    common = (PACKET_6B / "common.sh").read_text()
    return re.search(r'^VERIFY_EXPECTED="(.*?)"$', common, re.S | re.M).group(1).splitlines()


def _clean_6b(conn) -> None:
    """Scratch only (owner, one transaction, guards lifted explicitly): this test's evidence, audit, totals and rows."""
    with conn.transaction():
        for name in GUARDS_0029:  # the 0029 append-only guards: evidence and webhook rows of this test only
            conn.execute(f"ALTER TABLE public.{name.removesuffix('_guard')} DISABLE TRIGGER {name}")
        conn.execute("DELETE FROM public.payment_integrity_events WHERE webhook_event_id IN (SELECT id FROM "
                     "public.payment_webhook_events WHERE event_id LIKE 'migtest6b-%%')")
        conn.execute("DELETE FROM public.payment_webhook_events WHERE event_id LIKE 'migtest6b-%%'")
        for name in GUARDS_0029:
            conn.execute(f"ALTER TABLE public.{name.removesuffix('_guard')} ENABLE TRIGGER {name}")
        if "donation_credit_events" in _tables():
            conn.execute("ALTER TABLE public.donation_credit_events DISABLE TRIGGER donation_credit_events_guard")
            conn.execute("DELETE FROM public.donation_credit_events WHERE streamer_id IN "
                         "(SELECT id FROM public.streamers WHERE streamer_slug LIKE 'migtest6b-%%')")
            conn.execute("ALTER TABLE public.donation_credit_events ENABLE TRIGGER donation_credit_events_guard")
        conn.execute("DELETE FROM public.streamer_donator_totals WHERE streamer_id IN "
                     "(SELECT id FROM public.streamers WHERE streamer_slug LIKE 'migtest6b-%%')")
        guarded = conn.execute("SELECT count(*) FROM pg_trigger WHERE tgname = 'donations_credit_delete_guard'"
                               ).fetchone()[0]
        if guarded:
            conn.execute("ALTER TABLE public.donations DISABLE TRIGGER donations_credit_delete_guard")
        conn.execute("DELETE FROM public.donations WHERE order_id LIKE 'migtest6b-%%'")
        if guarded:
            conn.execute("ALTER TABLE public.donations ENABLE TRIGGER donations_credit_delete_guard")
        conn.execute("DELETE FROM public.streamers WHERE streamer_slug LIKE 'migtest6b-%%'")


def test_pay15_6b_packet_sql_on_a_fresh_0029_to_0035_upgrade(monkeypatch):
    """The 6b packet's rehearsal, permanent and repeatable (PostgreSQL 15 and 17 jobs): a database built from 0001 to
    0029 with DEV-shaped data, then Alembic exactly to 0035 as the window runs it (a subprocess, its own output).
    Asserted: prestate.sql has its 9 lines and is unchanged by the upgrade and by a no-op; every exact line of
    verify_0035.sql equals the packet's VERIFY_EXPECTED; V11 records the capped backfill; the 0031 backfill NOTICE never
    reaches Alembic's output (psycopg drops notices without a handler, so the packet relies on V11); the downgrade to
    0029 stops at the first migration whose history exists (rollback = the window's backup restore)."""
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    with _owner() as conn:
        _clean_6b(conn)
    command.downgrade(cfg, "0029")
    try:
        assert _version() == "0029"
        with _owner() as conn:
            sid = conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, status, kyc_track) "
                               "VALUES ('migtest6b-c', 'C', 'active', 'legacy') RETURNING id").fetchone()[0]
            for suffix, donor, amount, moderation, minutes in DONATIONS_6B:
                conn.execute("INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, "
                             "amount_inr, payment_status, moderation_status, created_at) VALUES (%s, %s, %s, %s, 'INR', "
                             "%s, %s, %s, now() - make_interval(mins => %s))",
                             (sid, "migtest6b-" + suffix, donor, amount, amount,
                              "pending" if moderation == "pending" else "success", moderation, minutes))
            for donor, total, count in TOTALS_6B:
                conn.execute("INSERT INTO public.streamer_donator_totals (streamer_id, donator_name, "
                             "donator_name_lower, total_amount, donation_count) VALUES (%s, %s, lower(%s), %s, %s)",
                             (sid, donor, donor, total, count))
            # webhook + evidence rows as DEV has them (2 processed refunds): 0033 adds a column to the evidence table,
            # so the pre-state row hashes must name their columns (gate R on DEV, 2026-11-24, hashed e::text)
            for n in (1, 2):
                _evidence33(conn, f"migtest6b-{n}", f"rfnd_MIG6B{n}", with_column=False)
        before = _packet_sql("prestate.sql")
        assert len(before) == 9 and before[0].startswith("totals ") and before[-1].startswith("jobs "), before
        evidence = [line for line in before if line.startswith(("evidence", "webhook"))]
        assert "evidence refund/processed=2" in evidence and all(not line.endswith(" -") for line in evidence), evidence

        upgrade = subprocess.run([sys.executable, "-B", "-m", "alembic", "upgrade", "0035"], cwd=DATABASE,
                                 env={**os.environ, "HYPERCHAT_MIGRATION_DSN": DSN}, capture_output=True, text=True)
        assert upgrade.returncode == 0, upgrade.stderr[-2000:]
        assert _version() == "0035"
        assert "credit backfill" not in upgrade.stdout + upgrade.stderr  # the NOTICE is not printed: V11 records it
        assert _packet_sql("prestate.sql") == before                       # V2: the totals checksum and every record
        with _owner() as conn:
            conn.execute("ANALYZE public.donations")                         # a no-op for the record
        assert _packet_sql("prestate.sql") == before

        got = _packet_sql("verify_0035.sql")
        assert [line for line in got if not line.startswith("V11 ")] == _verify_expected(), got
        assert [line for line in got if line.startswith("V11 ")] == ["V11 backfill rows 4 credited 270.00"], got

        with pytest.raises(Exception, match=r"downgrade of 003[0-4] stopped"):
            command.downgrade(cfg, "0029")
        assert _version() in ("0031", "0032")  # 0035 / 0034 / 0033 undo cleanly; the credit history stops it
        assert _packet_sql("prestate.sql")[0] == before[0]  # the totals survive the refused downgrade
    finally:
        with _owner() as conn:
            _clean_6b(conn)
        command.upgrade(cfg, "head")
    assert _version() == HEAD


# ── 0036: PAY-14 creator pricing limits (runtime roles only; backend/tests/dashboard/test_pricing_settings.py proves
#    the refusals and the boundaries as hyperchat_app) ──
def _pricing_guards() -> set[str]:
    with _owner() as conn:
        return {r[0] for r in conn.execute(
            "SELECT tgname FROM pg_trigger WHERE tgname IN ('streamers_check_pricing', 'streamer_voice_tiers_check') "
            "AND NOT tgisinternal")} | {r[0] for r in conn.execute(
            "SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace "
            "AND proname IN ('streamers_check_pricing', 'streamer_voice_tiers_check')")}


def test_0036_pricing_limits_round_trip_keep_legacy_rows_and_never_restrict_the_owner(monkeypatch):
    """Legacy rows out of today's range (below the 40 / 150 / 100 floors, above 10,000, 6 tiers, 60-90 s) exist
    before 0036: the upgrade neither rewrites nor refuses them; the owner stays unrestricted; the downgrade removes
    exactly the two triggers and their functions and keeps every row."""
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0035")
    assert _pricing_guards() == set()
    try:
        with _owner() as conn:
            conn.execute("DELETE FROM public.streamers WHERE streamer_slug = 'migtest36'")
            sid = conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, min_text_amount_inr, "
                               "min_voice_amount_inr, media_min_amount, message_char_tiers) VALUES ('migtest36', "
                               "'M36', 10, 0, 20000, '[{\"min_amount\": 0, \"max_chars\": 900}]') "
                               "RETURNING id").fetchone()[0]
            conn.execute("INSERT INTO public.streamer_voice_tiers (streamer_id, min_amount_inr, max_duration_seconds) "
                         "VALUES (%s, 0, 60)", (sid,))
            before = conn.execute("SELECT min_text_amount_inr, min_voice_amount_inr, media_min_amount, "
                                  "message_char_tiers, updated_at FROM public.streamers WHERE id = %s",
                                  (sid,)).fetchone()
        command.upgrade(cfg, "0036")
        assert _version() == "0036"
        assert _pricing_guards() == {"streamers_check_pricing", "streamer_voice_tiers_check"}
        with _owner() as conn:  # nothing existing rewritten; the owner (migrations, legacy import) is not limited
            assert conn.execute("SELECT min_text_amount_inr, min_voice_amount_inr, media_min_amount, "
                                "message_char_tiers, updated_at FROM public.streamers WHERE id = %s",
                                (sid,)).fetchone() == before
            for n in range(6):
                conn.execute("INSERT INTO public.streamer_voice_tiers (streamer_id, min_amount_inr, "
                             "max_duration_seconds) VALUES (%s, %s, 90)", (sid, 100 + n))
            conn.execute("UPDATE public.streamers SET min_text_amount_inr = 50000 WHERE id = %s", (sid,))
            assert conn.execute("SELECT bool_or(has_function_privilege(r, 'public.streamers_check_pricing()', "
                                "'EXECUTE') OR has_function_privilege(r, 'public.streamer_voice_tiers_check()', "
                                "'EXECUTE')) FROM unnest(ARRAY['hyperchat_app', 'hyperchat_worker']) r"
                                ).fetchone()[0] is False  # no grant
        command.downgrade(cfg, "0035")  # drops only the guards: never refused, rows kept
        assert _version() == "0035" and _pricing_guards() == set()
        with _owner() as conn:
            assert conn.execute("SELECT count(*) FROM public.streamer_voice_tiers WHERE streamer_id = %s",
                                (sid,)).fetchone()[0] == 7
    finally:
        with _owner() as conn:
            conn.execute("DELETE FROM public.streamers WHERE streamer_slug = 'migtest36'")  # cascades tiers
        command.upgrade(cfg, "head")
    assert _version() == HEAD




PAYMENT_DEFINER_FUNCTIONS = ("donation_accounting_apply", "donation_accounting_core", "donations_credit_writer",
                             "payment_accounting_apply_evaluation", "payment_accounting_keep",
                             "payment_accounting_lock_state", "payment_accounting_open_run",
                             "payment_accounting_reconcile", "payment_accounting_revert", "payment_accounting_set_state",
                             "payment_evidence_fp")
NV3_FUNCTIONS = ("stepup_guard_user_signups", "stepup_guard_streamer_insert")


def _functions_0037() -> dict:
    """{name: (md5 of the body, proconfig)} of the NV-3 guards and of the PAY-15-pinned DEFINER functions, and the
    user_signups / streamers triggers."""
    with _owner() as conn:
        rows = conn.execute("SELECT p.proname, md5(p.prosrc), coalesce(p.proconfig, '{}')::text[] FROM pg_proc p "
                            "WHERE p.pronamespace = 'public'::regnamespace AND p.proname = ANY(%s)",
                            (list(PAYMENT_DEFINER_FUNCTIONS + NV3_FUNCTIONS),)).fetchall()
        triggers = conn.execute("SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                                "WHERE NOT t.tgisinternal AND c.relnamespace = 'public'::regnamespace "
                                "AND c.relname IN ('user_signups', 'streamers')").fetchall()
    return {"functions": {n: (h, tuple(c)) for n, h, c in rows}, "triggers": sorted(triggers)}


def _data_0037() -> list:
    """Every user_signups row and every streamer's updated_at, as the owner sees them: 0037 must never change data."""
    with _owner() as conn:
        return [conn.execute("SELECT * FROM public.user_signups ORDER BY id").fetchall(),
                conn.execute("SELECT id, updated_at FROM public.streamers ORDER BY id").fetchall()]


def test_0037_round_trip_changes_only_its_guards_and_triggers_never_data_or_payment_functions(monkeypatch):
    """0037 (SEC-A + NV-3): the upgrade re-creates the two 0027 guards with the applicant's address and adds the two
    SEC-A triggers; it leaves every PAY-15-pinned SECURITY DEFINER payment function exactly as it was (body and
    proconfig); the downgrade to 0036 restores exactly the 0036 catalog of those objects; no row is read or written."""
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0036")
    try:
        with _owner() as conn:
            conn.execute("DELETE FROM public.user_signups WHERE email = 'migtest37@authtest.invalid'")
            conn.execute("INSERT INTO public.user_signups (name, email, mobile_number) VALUES "
                         "('M37', 'migtest37@authtest.invalid', '+91 90000 00037')")
        at_0036, data = _functions_0037(), _data_0037()
        assert set(at_0036["functions"]) == set(PAYMENT_DEFINER_FUNCTIONS + NV3_FUNCTIONS)
        command.upgrade(cfg, "0037")
        assert _version() == "0037" and _data_0037() == data
        at_0037 = _functions_0037()
        for name in PAYMENT_DEFINER_FUNCTIONS:  # no payment object: the verifier's no_payment_delta holds
            assert at_0037["functions"][name] == at_0036["functions"][name], name
        for name in NV3_FUNCTIONS:
            assert at_0037["functions"][name][0] != at_0036["functions"][name][0], name  # re-created with the email
            assert at_0037["functions"][name][1] == at_0036["functions"][name][1], name
        added = set(at_0037["triggers"]) - set(at_0036["triggers"])
        assert added == {("streamers", "aad_stepup_guard_soundboard"), ("user_signups", "aad_stepup_guard_reopen")}
        assert set(at_0036["triggers"]) <= set(at_0037["triggers"])
        command.downgrade(cfg, "0036")  # restores exactly the 0036 objects, keeps every row
        assert _version() == "0036" and _functions_0037() == at_0036 and _data_0037() == data
    finally:
        with _owner() as conn:
            conn.execute("DELETE FROM public.user_signups WHERE email = 'migtest37@authtest.invalid'")
        command.upgrade(cfg, "head")
    assert _version() == HEAD


def _objects_0038() -> dict:
    """{name: (md5 of the body, proconfig, ACL)} of the invite guard, onboarding_issue_invite and the PAY-15-pinned
    DEFINER functions, and the user_invites triggers."""
    with _owner() as conn:
        rows = conn.execute("SELECT p.proname, md5(p.prosrc), coalesce(p.proconfig, '{}')::text[], "
                            "coalesce(p.proacl, '{}')::text[] FROM pg_proc p "
                            "WHERE p.pronamespace = 'public'::regnamespace AND p.proname = ANY(%s)",
                            (list(PAYMENT_DEFINER_FUNCTIONS + ("stepup_guard_user_invites",
                                                               "onboarding_issue_invite")),)).fetchall()
        triggers = conn.execute("SELECT t.tgname FROM pg_trigger t WHERE NOT t.tgisinternal "
                                "AND t.tgrelid = 'public.user_invites'::regclass ORDER BY 1").fetchall()
    return {"functions": {n: (h, tuple(c), tuple(a)) for n, h, c, a in rows}, "triggers": triggers}


def _data_0038() -> list:
    """Every application, invitation and application event, as the owner sees them: 0038 never changes data."""
    with _owner() as conn:
        return [conn.execute(f"SELECT * FROM public.{t} ORDER BY id").fetchall()
                for t in ("user_signups", "user_invites", "application_events")]


def test_0038_round_trip_changes_only_the_invite_guard_and_its_function_never_data(monkeypatch):
    """0038 (onboarding invite guard exception): the upgrade adds onboarding_issue_invite (EXECUTE for
    hyperchat_lifecycle only) and re-creates 0027's user_invites guard with the one exception; no trigger and no
    PAY-15-pinned function changes; the downgrade restores exactly the 0037 catalog; no row is read or written."""
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0037")
    try:
        with _owner() as conn:
            conn.execute("DELETE FROM public.user_signups WHERE email = 'migtest38@authtest.invalid'")
            conn.execute("INSERT INTO public.user_signups (name, email, mobile_number) VALUES "
                         "('M38', 'migtest38@authtest.invalid', '+91 90000 00038')")
        at_0037, data = _objects_0038(), _data_0038()
        assert "onboarding_issue_invite" not in at_0037["functions"]
        command.upgrade(cfg, "0038")
        assert _version() == "0038" and _data_0038() == data
        at_0038 = _objects_0038()
        for name in PAYMENT_DEFINER_FUNCTIONS:  # no payment object: the verifier's no_payment_delta holds
            assert at_0038["functions"][name] == at_0037["functions"][name], name
        guard_37 = at_0037["functions"]["stepup_guard_user_invites"]
        guard_38 = at_0038["functions"]["stepup_guard_user_invites"]
        assert guard_38[0] != guard_37[0] and guard_38[1:] == guard_37[1:]  # body only: same config, same ACL
        _, config, acl = at_0038["functions"]["onboarding_issue_invite"]
        assert "search_path=pg_catalog, public, pg_temp" in config
        assert {entry.split("=", 1)[0] for entry in acl} == {"hyperchat_owner", "hyperchat_lifecycle"}, acl
        assert at_0038["triggers"] == at_0037["triggers"]
        command.downgrade(cfg, "0037")  # restores exactly the 0037 objects, keeps every row
        assert _version() == "0037" and _objects_0038() == at_0037 and _data_0038() == data
    finally:
        with _owner() as conn:
            conn.execute("DELETE FROM public.user_signups WHERE email = 'migtest38@authtest.invalid'")
        command.upgrade(cfg, "head")
    assert _version() == HEAD


def _objects_0039() -> dict:
    with _owner() as conn:
        return {
            "table": conn.execute("SELECT to_regclass('public.creator_image_uploads') IS NOT NULL").fetchone()[0],
            "guard": conn.execute("SELECT count(*) FROM pg_proc WHERE proname = 'creator_image_uploads_guard'"
                                  ).fetchone()[0],
            "functions": conn.execute("SELECT p.proname, md5(p.prosrc) FROM pg_proc p WHERE p.proname = ANY(%s) "
                                      "ORDER BY 1", (list(PAYMENT_DEFINER_FUNCTIONS),)).fetchall(),
            "streamers": conn.execute("SELECT id, updated_at, brand_logo_url, background_url FROM public.streamers "
                                      "ORDER BY id").fetchall(),
        }


def _privileges_0039() -> dict:
    with _owner() as conn:
        return {role: [p for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
                       if conn.execute("SELECT has_table_privilege(%s, 'public.creator_image_uploads', %s)",
                                       (role, p)).fetchone()[0]]
                for role in ("hyperchat_app", "hyperchat_worker", "hyperchat_auth", "hyperchat_lifecycle",
                             "hyperchat_stepup", "hyperchat_readonly")}


def test_0039_round_trip_adds_only_the_image_records_and_refuses_to_drop_any(monkeypatch):
    """0039 (creator_image_uploads): the table, its guard and grants (the API: SELECT, INSERT, UPDATE; no DELETE; the
    worker nothing); no streamer row and no PAY-15-pinned function changes; the downgrade refuses while a record
    exists, changing nothing, and on an empty table restores the 0038 catalog."""
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0038")
    sid = None
    try:
        before = _objects_0039()
        assert (before["table"], before["guard"]) == (False, 0)
        command.upgrade(cfg, "0039")
        after = _objects_0039()
        assert (after["table"], after["guard"]) == (True, 1)
        assert after["functions"] == before["functions"] and after["streamers"] == before["streamers"]
        assert _privileges_0039() == {"hyperchat_app": ["SELECT", "INSERT", "UPDATE"], "hyperchat_worker": [],
                                      "hyperchat_auth": [], "hyperchat_lifecycle": [], "hyperchat_stepup": [],
                                      "hyperchat_readonly": ["SELECT"]}
        with _owner() as conn:
            sid = conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, razorpay_account) VALUES "
                               "('migtest-0039', 'M39', 'primary') RETURNING id::text").fetchone()[0]
            conn.execute("INSERT INTO public.creator_image_uploads (streamer_id, kind, state, variants) VALUES "
                         "(%s, 'profile', 'ready', %s::jsonb)",
                         (sid, f'[{{"key": "creator-assets/{sid}/logo/{"a" * 64}.webp"}}]'))
        with pytest.raises(Exception, match="downgrade of 0039 stopped"):
            command.downgrade(cfg, "0038")
        assert _version() == "0039" and _objects_0039()["table"]  # nothing was changed
        with _owner() as conn:
            conn.execute("DELETE FROM public.creator_image_uploads WHERE streamer_id = %s", (sid,))
            conn.execute("DELETE FROM public.streamers WHERE id = %s", (sid,))
        sid = None
        command.downgrade(cfg, "0038")
        assert _version() == "0038" and _objects_0039() == before
    finally:
        if sid is not None:
            with _owner() as conn:
                conn.execute("DELETE FROM public.creator_image_uploads WHERE streamer_id = %s", (sid,))
                conn.execute("DELETE FROM public.streamers WHERE id = %s", (sid,))
        command.upgrade(cfg, "head")
    assert _version() == HEAD


WF40 = "a3b2c1d0-4444-4555-8666-777777777777"
FUNCTIONS_0040 = ("kyc_create_session", "kyc_receive_webhook", "kyc_effective")


def _objects_0040() -> dict:
    with _owner() as conn:
        return {
            "tables": sorted(t for t in _tables() if t.startswith("kyc_")),
            "functions": conn.execute("SELECT p.oid::regprocedure::text, md5(p.prosrc), "
                                      "coalesce(p.proacl, '{}')::text[] FROM pg_proc p "
                                      "WHERE p.pronamespace = 'public'::regnamespace AND p.proname LIKE 'kyc\\_%%' "
                                      "ORDER BY 1").fetchall(),
            "columns": conn.execute("SELECT table_name, column_name FROM information_schema.columns WHERE "
                                    "table_schema = 'public' AND table_name LIKE 'kyc\\_%%' ORDER BY 1, 2").fetchall(),
            "payment": conn.execute("SELECT p.proname, md5(p.prosrc) FROM pg_proc p WHERE p.proname = ANY(%s) "
                                    "ORDER BY 1", (list(PAYMENT_DEFINER_FUNCTIONS),)).fetchall(),
        }


def test_0040_round_trip_keeps_the_primary_and_refuses_to_drop_another_account(monkeypatch):
    """0040 (several Didit accounts): the 0022 functions are replaced (the five-argument kyc_create_session stays,
    for the previous release), the new tables are unreadable for every runtime role where they hold secrets, no
    PAY-15 function changes, and the downgrade refuses while an extra account exists, then restores the exact 0039
    catalog."""
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0039")
    added = False
    try:
        before = _objects_0040()
        command.upgrade(cfg, "0040")
        after = _objects_0040()
        assert after["payment"] == before["payment"]
        assert set(after["tables"]) - set(before["tables"]) == {
            "kyc_provider_accounts", "kyc_account_pins", "kyc_account_reservations", "kyc_account_events"}
        names = {f for f, _, _ in after["functions"]}
        assert {"kyc_create_session(uuid,uuid,uuid,integer,text)",
                "kyc_create_session(uuid,uuid,uuid,integer,text,text)", "kyc_assign_account(uuid)"} <= names
        with _owner() as conn:
            for role in ("hyperchat_app", "hyperchat_worker", "hyperchat_readonly", "hyperchat_auth",
                         "hyperchat_lifecycle", "hyperchat_stepup"):
                for table in ("kyc_provider_accounts", "kyc_provider_config"):
                    assert not conn.execute("SELECT has_table_privilege(%s, %s, 'SELECT')",
                                            (role, "public." + table)).fetchone()[0], (role, table)
            assert conn.execute("SELECT has_function_privilege('hyperchat_app', 'public.kyc_create_session(uuid, uuid, "
                                "uuid, integer, text)', 'EXECUTE')").fetchone()[0]  # the previous release's call
            conn.execute("INSERT INTO public.kyc_provider_config (environment, workflow_id, application_id, "
                         "webhook_secret) VALUES ('sandbox', %s, gen_random_uuid(), 'whsec_migtest_0123456789') "
                         "ON CONFLICT (singleton) DO UPDATE SET environment = EXCLUDED.environment, "
                         "workflow_id = EXCLUDED.workflow_id, application_id = EXCLUDED.application_id", (WF40,))
            added = True
        # The operator's tool: an account is added with its secret from stdin (never argv), then disabled, audited.
        monkeypatch.setattr("sys.stdin", io.StringIO("whsec_migtest_b_0123456789\nwhsec_migtest_p_0123456789\n"))
        assert kyc_provider_account.main(["--operator", "migtest", "add", "--account", "migtest-b", "--application-id",
                                          "5e5e5e5e-0000-4000-8000-000000000040", "--workflow-id", WF40,
                                          "--secret-stdin"]) == 0
        assert kyc_provider_account.main(["--operator", "migtest", "add", "--account", "primary", "--application-id",
                                          "5e5e5e5e-0000-4000-8000-000000000041", "--workflow-id", WF40,
                                          "--secret-stdin"]) == 2  # the primary is not an extra account
        assert kyc_provider_account.main(["--operator", "migtest", "disable", "--account", "migtest-b"]) == 0
        with _owner() as conn:
            assert conn.execute("SELECT webhook_secret, enabled FROM public.kyc_provider_accounts "
                                "WHERE account_id = 'migtest-b'").fetchone() == ("whsec_migtest_b_0123456789", False)
            assert conn.execute("SELECT action, operator, detail::text NOT LIKE '%%whsec%%' FROM "
                                "public.kyc_account_events ORDER BY created_at").fetchall() == [
                ("account_added", "migtest", True), ("account_changed", "migtest", True)]
        with pytest.raises(Exception, match="downgrade of 0040 stopped"):
            command.downgrade(cfg, "0039")
        assert _version() == "0040"
        with _owner() as conn:
            conn.execute("DELETE FROM public.kyc_provider_accounts WHERE account_id = 'migtest-b'")
            conn.execute("DELETE FROM public.kyc_account_events")
            conn.execute("DELETE FROM public.kyc_provider_config")
            added = False
        command.downgrade(cfg, "0039")
        assert _version() == "0039" and _objects_0040() == before
    finally:
        if added:
            with _owner() as conn:
                conn.execute("DELETE FROM public.kyc_provider_accounts WHERE account_id = 'migtest-b'")
                conn.execute("DELETE FROM public.kyc_account_events")
                conn.execute("DELETE FROM public.kyc_provider_config")
        command.upgrade(cfg, "head")
    assert _version() == HEAD


def _objects_0041() -> dict:
    with _owner() as conn:
        return {
            "tables": sorted(t for t in _tables() if t.startswith("kyc_")),
            "functions": conn.execute("SELECT p.oid::regprocedure::text, md5(p.prosrc), "
                                      "coalesce(p.proacl, '{}')::text[] FROM pg_proc p "
                                      "WHERE p.pronamespace = 'public'::regnamespace AND p.proname LIKE 'kyc%' "
                                      "ORDER BY 1").fetchall(),
            "triggers": conn.execute("SELECT tgrelid::regclass::text, tgname FROM pg_trigger WHERE NOT tgisinternal "
                                     "AND tgname LIKE 'kyc_payout%' ORDER BY 1, 2").fetchall(),
            "payment": conn.execute("SELECT p.proname, md5(p.prosrc) FROM pg_proc p WHERE p.proname = ANY(%s) "
                                    "ORDER BY 1", (list(PAYMENT_DEFINER_FUNCTIONS),)).fetchall(),
        }


def test_0041_round_trip_adds_the_pan_evidence_and_refuses_to_drop_an_acknowledgement(monkeypatch):
    """0041 (PAN evidence): the policy is seeded with IND + TAX_CARD_STANDARD, the payout holds and acknowledgements
    are not writable by the worker and holds not by the API, no PAY-15 function changes; the downgrade refuses while
    an acknowledgement exists, then restores the exact 0040 catalog (0022's kyc_check_statuses / kyc_apply_refresh)."""
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", DSN)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0040")
    uid = None
    try:
        before = _objects_0041()
        command.upgrade(cfg, "0041")
        after = _objects_0041()
        assert after["payment"] == before["payment"]
        assert set(after["tables"]) - set(before["tables"]) == {"kyc_document_policy", "kyc_payout_holds",
                                                                "kyc_pan_acknowledgements"}
        with _owner() as conn:
            assert conn.execute("SELECT issuing_state, document_type, document_subtype FROM public.kyc_document_policy"
                                ).fetchall() == [("IND", None, "TAX_CARD_STANDARD")]
            for table, role, privilege, allowed in (
                    ("kyc_payout_holds", "hyperchat_app", "INSERT", False), ("kyc_payout_holds", "hyperchat_app",
                                                                             "SELECT", True),
                    ("kyc_document_policy", "hyperchat_app", "UPDATE", False),
                    ("kyc_pan_acknowledgements", "hyperchat_app", "INSERT", True),
                    ("kyc_pan_acknowledgements", "hyperchat_app", "DELETE", False),
                    ("kyc_pan_acknowledgements", "hyperchat_worker", "SELECT", False)):
                assert conn.execute("SELECT has_table_privilege(%s, %s, %s)",
                                    (role, "public." + table, privilege)).fetchone()[0] is allowed, (table, role)
            uid = conn.execute("INSERT INTO public.users (email) VALUES ('migtest41@x.invalid') RETURNING id"
                               ).fetchone()[0]
            conn.execute("INSERT INTO public.kyc_pan_acknowledgements (user_id, text_version, text_sha256) VALUES "
                         "(%s, 'v', repeat('a', 64))", (uid,))
        with pytest.raises(Exception, match="downgrade of 0041 stopped"):
            command.downgrade(cfg, "0040")
        assert _version() == "0041"
        with _owner() as conn:
            conn.execute("DELETE FROM public.kyc_pan_acknowledgements WHERE user_id = %s", (uid,))
            conn.execute("DELETE FROM public.users WHERE id = %s", (uid,))
            uid = None
        command.downgrade(cfg, "0040")
        assert _version() == "0040" and _objects_0041() == before
    finally:
        if uid is not None:
            with _owner() as conn:
                conn.execute("DELETE FROM public.kyc_pan_acknowledgements WHERE user_id = %s", (uid,))
                conn.execute("DELETE FROM public.users WHERE id = %s", (uid,))
        command.upgrade(cfg, "head")
    assert _version() == HEAD
