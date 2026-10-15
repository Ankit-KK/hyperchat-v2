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
    "user_invites", "payment_webhook_events", "jobs", "donation_transcripts", "realtime_outbox", "moderator_notifications",
    "d1_hash_import_ledger", "creator_status_events", "application_events",
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
