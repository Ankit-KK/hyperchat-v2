"""The fresh-start legacy cutover end to end on PostgreSQL 15 / 17 (CI only): a SYNTHETIC legacy-shaped source covering
every table the ETL reads, loaded by database/etl/rehearsal.py into a FRESH database at head (the verify template
copy), then the reconciliation, the PAY-15 verifier, the D1 password import, a credited migrated donation rejected
through the one credit writer, and the verifier again. No real data: every name, email and hash is synthetic.

What the fixture proves is the ETL's documented contract (legacy column names = the V2 names for straight copies,
plus the legacy-only shapes the loader maps), not the real dump: a rehearsal against a restored snapshot stays a
runbook step (docs/operations/legacy-cutover.md). Straight-copy source tables mirror the target's columns, minus the
V2-only columns (etl.mapping.V2_ONLY_COLUMNS), with types and defaults read from the target's catalog."""

import hashlib
import os
import re
import secrets
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import bcrypt
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from app.payments import verify
from tests.integration import PG_HOST, requires_db, target

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "database"))
from etl import auth_hash_import as imp  # noqa: E402
from etl import rehearsal  # noqa: E402
from etl.mapping import MODERATION_FROM_LEGACY, V2_ONLY_COLUMNS  # noqa: E402
from etl.reconcile import verifier_result  # noqa: E402

pytestmark = [requires_db, pytest.mark.skipif(not os.environ.get("HYPERCHAT_TEST_VERIFY_ADMIN"),
                                              reason="HYPERCHAT_TEST_VERIFY_ADMIN not set")]

LEGACY_BASE, MEDIA_BASE = "https://pub-legacy0.r2.dev", "https://media.etltest.invalid"
D = "@etltest.invalid"
NOW = datetime.now(UTC)


def _u(n: int) -> str:
    return str(uuid.UUID(int=0xE7100000_0000_4000_8000_000000000000 + n))


U_ADMIN, U_S1, U_S2, U_PLAIN, U_ROGUE = _u(1), _u(2), _u(3), _u(4), _u(5)
SUPA = {U_ADMIN: _u(101), U_S1: _u(102), U_S2: _u(103), U_PLAIN: _u(104), U_ROGUE: _u(105)}
S_ALPHA, S_BETA, S_GAMMA, S_SAPPHIRE = _u(11), _u(12), _u(13), _u(14)
# 0046: legacy moderation settings, one combination per creator (manual; auto with media moderation off; auto with
# it NULL, read as on; auto with it on)
LEGACY_MODERATION = {"alpha": {"moderation_mode": "manual", "media_moderation_enabled": False},
                     "beta": {"moderation_mode": "auto_approve", "media_moderation_enabled": False},
                     "gamma": {"moderation_mode": None, "media_moderation_enabled": None},
                     "sapphire": {"moderation_mode": "auto_approve", "media_moderation_enabled": True}}
MOD = _u(21)

# (bucket name as given, amount, amount_inr, currency, payment_status, moderation_status) per creator, oldest first.
# PAY-15 bucket shapes: exact, short (newest partly credited), total above the sum, no total, a total without
# donations, case variants of one name, paid but rejected / pending, unpaid, a foreign currency.
DONATIONS = {
    "alpha": [("Exact", 100, 100, "INR", "success", "approved"), ("Exact", 50, 50, "INR", "success", "auto_approved"),
              ("Exact", 70, 70, "INR", "success", "rejected"), ("Exact", 30, 30, "INR", "success", "pending"),
              ("Exact", 40, 40, "INR", "failed", "pending"), ("Exact", 20, 20, "INR", "pending", "pending"),
              ("Short", 100, 100, "INR", "success", "approved"), ("Short", 80, 80, "INR", "success", "approved"),
              ("Over", 60, 60, "INR", "success", "approved"), ("Nototal", 40, 40, "INR", "success", "approved"),
              ("Case", 10, 10, "INR", "success", "approved"), ("CASE", 15, 15, "INR", "success", "approved")],
    "beta": [("Usd", 5, 415, "USD", "success", "approved")],
    "gamma": [("G", 20, 20, "INR", "success", "approved")],
    "sapphire": [("S", 25, 25, "INR", "pending", "pending")],
}
TOTALS = {"alpha": [("Exact", 150, 2), ("Short", 120, 2), ("Over", 200, 1), ("Ghost", 30, 1), ("Case", 25, 2)],
          "beta": [("Usd", 415, 1)], "gamma": [("G", 20, 1)]}
CREDITED = {("alpha", "exact"): 150, ("alpha", "short"): 120, ("alpha", "over"): 60, ("alpha", "nototal"): 0,
            ("alpha", "case"): 25, ("beta", "usd"): 415, ("gamma", "g"): 20}


def _admin() -> psycopg.Connection:
    return psycopg.connect(os.environ["HYPERCHAT_TEST_VERIFY_ADMIN"], autocommit=True)


@pytest.fixture
def legacy_db():
    """A throwaway synthetic legacy database on the CI server (dropped afterwards)."""
    name = f"etltest_legacy_{secrets.token_hex(5)}"
    with _admin() as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield name
    finally:
        with _admin() as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _dsn(db: str, user: str) -> str:
    return f"host={PG_HOST} port={target()[1]} dbname={db} user={user}"


def _src_conn(db: str) -> psycopg.Connection:
    admin = conninfo_to_dict(os.environ["HYPERCHAT_TEST_VERIFY_ADMIN"])
    return psycopg.connect(**{**admin, "dbname": db})


def _target_columns(tgt: psycopg.Connection, table: str) -> list[tuple[str, str, str | None]]:
    return tgt.execute(
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod), pg_get_expr(d.adbin, d.adrelid) "
        "FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped AND a.attgenerated = '' "
        "ORDER BY a.attnum", (f"public.{table}",)).fetchall()


def _mirror(src, tgt, table: str, drop=(), add: str = "") -> None:
    cols = []
    for name, typ, default in _target_columns(tgt, table):
        if name in drop:
            continue
        keep = default and "nextval(" not in default and "public." not in default and "txid" not in default
        cols.append(f'"{name}" {typ}' + (f" DEFAULT {default}" if keep else ""))
    src.execute(f"CREATE TABLE public.{table} ({', '.join(cols + ([add] if add else []))})")


def _insert(src, table: str, **values) -> None:
    cols = ", ".join(f'"{c}"' for c in values)
    src.execute(f"INSERT INTO public.{table} ({cols}) VALUES ({', '.join(['%s'] * len(values))})",
                [Jsonb(v) if isinstance(v, dict) else v for v in values.values()])


def build_legacy(src: psycopg.Connection, tgt: psycopg.Connection) -> None:
    src.execute("CREATE EXTENSION IF NOT EXISTS citext")
    for typ, labels in tgt.execute("SELECT t.typname, array_agg(e.enumlabel ORDER BY e.enumsortorder) FROM pg_type t "
                                   "JOIN pg_enum e ON e.enumtypid = t.oid WHERE t.typnamespace = 'public'::regnamespace "
                                   "GROUP BY 1").fetchall():
        src.execute(f"CREATE TYPE public.{typ} AS ENUM ({', '.join(sql.quote(x) for x in labels)})")

    # legacy-only shapes
    src.execute("CREATE TABLE public.auth_users (id uuid, email text, username text, role text, is_active boolean, "
                "failed_login_attempts integer, locked_until timestamptz, supabase_user_id uuid, created_at "
                "timestamptz, updated_at timestamptz, streamer_id uuid, password_hash text)")
    src.execute("CREATE TABLE public.admin_emails (email text)")
    _mirror(src, tgt, "streamers", drop=("razorpay_key_ref", "razorpay_secret_ref", "razorpay_account", "kyc_track",
                                         "kyc_baseline_owner_id", *MODERATION_FROM_LEGACY),  # 0046: V2-only
            add='"razorpay-keyid" text, "razorpay-keysecret" text, donation_table_id integer')
    src.execute("CREATE TABLE public.obs_tokens (id uuid, streamer_id uuid, token text, is_active boolean)")
    for table in (*rehearsal.STRAIGHT_COPIES, "api_keys", "partner_api_keys"):  # keys: read only to count them
        _mirror(src, tgt, table, drop=V2_ONLY_COLUMNS.get(table, ()))
    _mirror(src, tgt, "order_lookup", add="streamer_slug text, donation_table_id integer")
    _mirror(src, tgt, "moderation_actions", add="donation_table text")
    for table in ("telegram_callback_mapping", "discord_callback_mapping"):
        _mirror(src, tgt, table, add="table_name text")
    src.execute("CREATE TABLE public.streamer_donator_totals (id uuid DEFAULT gen_random_uuid(), streamer_slug text, "
                "donator_name text, donator_name_lower text, total_amount numeric(12,2), donation_count integer, "
                "updated_at timestamptz DEFAULT now())")
    _mirror(src, tgt, "request_nonces")
    for table in ("platform_soundboard_slots", "streamer_soundboard_slots", "soundboard_swap_log", "auth_sessions",
                  "password_reset_tokens", "profiles", "auth_migration_control"):
        src.execute(f"CREATE TABLE public.{table} (id uuid DEFAULT gen_random_uuid(), audio_url text)")
        _insert(src, table, audio_url=f"{LEGACY_BASE}/soundboards/x/{table}.mp3")
    donation_cols = ("id uuid, streamer_id uuid, order_id text, razorpay_order_id text, name text, amount numeric, "
                     "currency text, amount_inr numeric(12,2), message text, message_visible boolean, payment_status "
                     "text, moderation_status text, approved_at timestamptz, approved_by text, is_hyperemote boolean, "
                     "voice_message_url text, hypersound_url text, media_url text, media_type text, tts_audio_url "
                     "text, audio_scheduled_at timestamptz, audio_played_at timestamptz, mod_notified boolean, "
                     "status_token_hash text, upload_token_hash text, created_at timestamptz, updated_at timestamptz, "
                     "temp_voice_data text, selected_gif_id text")
    for slug in DONATIONS:
        extra = (", email text, payment_id text, signature text, media_enabled boolean DEFAULT false, tts_enabled "
                 "boolean DEFAULT false, status text DEFAULT 'pending'") if slug == "sapphire" else ""
        src.execute(f"CREATE TABLE public.{slug}_donations ({donation_cols}{extra})")

    # rows: users and creators
    # plain is a linked NON-owner account of alpha (users.streamer_id set, streamers.user_id = s1), as seen once in the
    # observed restore: reported by kind, never a failure
    for uid, email, role, sid in ((U_ADMIN, "admin" + D, "user", None), (U_S1, "s1" + D, "streamer", S_ALPHA),
                                  (U_S2, "s2" + D, "streamer", S_BETA), (U_PLAIN, "plain" + D, "user", S_ALPHA),
                                  (U_ROGUE, "rogue" + D, "admin", None)):  # NV3-2: legacy role 'admin', not listed
        _insert(src, "auth_users", id=uid, email=email, username=email.split("@")[0], role=role, is_active=True,
                failed_login_attempts=0, supabase_user_id=SUPA[uid], created_at=NOW, updated_at=NOW, streamer_id=sid)
    _insert(src, "admin_emails", email="ADMIN" + D)
    for sid, slug, owner, status, logo, bg in (
            (S_ALPHA, "alpha", U_S1, "active", f"{LEGACY_BASE}/creator/alpha.png", "/assets/streamers/alpha/bg.png"),
            (S_BETA, "beta", U_S2, "active", None, f"{LEGACY_BASE}/creator/beta-bg.png"),
            (S_GAMMA, "gamma", None, "pending", None, None), (S_SAPPHIRE, "sapphire", None, "active", None, None)):
        _insert(src, "streamers", id=sid, user_id=owner, streamer_slug=slug, streamer_name=f"ETL {slug}",
                status=status, brand_logo_url=logo, background_url=bg, **LEGACY_MODERATION[slug],
                **{"razorpay-keyid": "razorpay-keyid", "razorpay-keysecret": "razorpay-keysecret"})
    _insert(src, "obs_tokens", id=_u(31), streamer_id=S_ALPHA, token="obs_" + "a" * 32, is_active=True)

    # rows: every straight copy
    rows = {
        "streamers_moderators": dict(id=MOD, streamer_id=S_ALPHA, mod_name="ETL Mod"),
        "streamer_payment_health": dict(streamer_id=S_ALPHA),
        "payout_accounts": dict(user_id=U_S1, streamer_id=S_ALPHA, account_holder_name="ETL Holder",
                                bank_name="ETL Bank", ifsc_code="ETLT0000001", account_number_encrypted="enc:etltest",
                                account_number_last4="0000", account_type="savings"),
        "platform_voice_tiers": dict(min_amount_inr=150, max_duration_seconds=15),
        "streamer_voice_tiers": dict(streamer_id=S_ALPHA, min_amount_inr=100, max_duration_seconds=10),
        "soundboard_tnc_acceptances": dict(streamer_slug="alpha", streamer_id=S_ALPHA, user_id=U_S1),
        "terms_acceptances": dict(user_id=U_S1, accepted_version="etl-v1"),
        "banned_donors": dict(streamer_id=S_ALPHA, donor_name="Banned", banned_by_moderator_id=MOD),
        "partner_signups": dict(partner_slug="etlpartner", channel_name="c", mobile_number="0", youtube_channel="y",
                                email="partner" + D),
        "tts_moderation_audit": dict(donation_id=_u(999), streamer_id=S_ALPHA, supporter_identity_hash="h",
                                     blocked=False, moderation_version="1", normalization_version="1"),
        "rate_limits": dict(ip_address="192.0.2.1", endpoint="/etl"),
        "system_alerts": dict(source="etl", severity="info", message="synthetic", context={"k": "v"}),
        "user_signups": dict(name="ETL Applicant", mobile_number="0", email="applicant" + D),
        "visits": dict(ip_address="192.0.2.2"),
        "reviews": dict(name="ETL", content="synthetic"),
        "user_queries": dict(user_id=U_PLAIN, query_text="synthetic"),
    }
    assert set(rows) == set(rehearsal.STRAIGHT_COPIES)  # every mapped table has a row
    rows["api_keys"] = dict(streamer_slug="alpha", key_type="donation", key_hash="etl" + "0" * 61, key_prefix="etl")
    rows["partner_api_keys"] = dict(partner_slug="etlpartner", key_hash="etl" + "1" * 61, key_prefix="etl")
    for table, values in rows.items():
        _insert(src, table, **values)

    # rows: donations and their references
    n = 0
    first = {}
    for slug, items in DONATIONS.items():
        sid = {"alpha": S_ALPHA, "beta": S_BETA, "gamma": S_GAMMA, "sapphire": S_SAPPHIRE}[slug]
        for name, amount, inr, cur, pay, mod in items:
            n += 1
            did = _u(1000 + n)
            first.setdefault(slug, did)
            _insert(src, f"{slug}_donations", id=did, streamer_id=sid, order_id=f"etl-order-{n}",
                    razorpay_order_id=f"order_ETL{n:04d}", name=name, amount=amount, currency=cur, amount_inr=inr,
                    message="synthetic", message_visible=True, payment_status=pay, moderation_status=mod,
                    approved_by=MOD if n == 1 else ("ETL label" if n == 2 else None), is_hyperemote=False,
                    voice_message_url=f"{LEGACY_BASE}/voice-messages/{n}.webm" if n % 2 else None,
                    hypersound_url=f"{LEGACY_BASE}/hypersounds/{n}.mp3" if n == 3 else None,
                    media_url=f"{LEGACY_BASE}/donation-media/{n}.png" if n == 4 else None,
                    media_type="image" if n == 4 else None, tts_audio_url=f"{LEGACY_BASE}/tts-audio/{n}.mp3",
                    audio_scheduled_at=NOW - timedelta(minutes=5), audio_played_at=None if n == 1 else NOW,
                    mod_notified=True, status_token_hash="b" * 64, upload_token_hash="c" * 64 if n == 1 else None,
                    created_at=NOW - timedelta(days=30) + timedelta(minutes=n), updated_at=NOW)
    for slug, items in TOTALS.items():
        for name, total, count in items:
            _insert(src, "streamer_donator_totals", streamer_slug=slug, donator_name=name,
                    donator_name_lower=name.lower(), total_amount=total, donation_count=count)
    _insert(src, "order_lookup", razorpay_order_id="order_ETL0001", order_id="etl-order-1",
            donation_id=first["alpha"], streamer_slug="alpha")
    # NV3-1 (real at scale: 2,970 such legacy rows): a pending (#6) and a failed (#5) order with an order id AND a lookup
    for k in (5, 6):
        _insert(src, "order_lookup", razorpay_order_id=f"order_ETL{k:04d}", order_id=f"etl-order-{k}",
                donation_id=_u(1000 + k), streamer_slug="alpha")
    _insert(src, "moderation_actions", streamer_id=S_ALPHA, moderator_id=MOD, donation_id=first["alpha"],
            action_type="approve", action_source="telegram")
    for table in ("telegram_callback_mapping", "discord_callback_mapping"):
        _insert(src, table, short_id="etl1", donation_id=first["alpha"], streamer_id=S_ALPHA, action_type="approve",
                expires_at=NOW + timedelta(hours=1))
        _insert(src, table, short_id="etl2", donation_id=first["alpha"], streamer_id=S_ALPHA, action_type="reject",
                expires_at=NOW - timedelta(hours=1))
    _insert(src, "request_nonces", nonce="etl-live", user_id=U_PLAIN, expires_at=NOW + timedelta(hours=1))
    _insert(src, "request_nonces", nonce="etl-old", user_id=U_PLAIN, expires_at=NOW - timedelta(hours=1))


def _verify(db: str) -> dict:
    with psycopg.connect(_dsn(db, "hyperchat_readonly"), autocommit=True) as conn:
        return verify.verify(conn, max_seconds=600)


def _assert_verified(report: dict, why: str) -> None:
    ok, detail = verifier_result(report, getattr(verify, "I10_BASELINE_CONFIRMED", False))
    assert ok, (why, detail)


def test_fresh_start_cutover_load_reconciles_and_verifies(verify_db, legacy_db):
    with psycopg.connect(_dsn(verify_db, "hyperchat_migrator"), autocommit=True) as tgt, \
            _src_conn(legacy_db) as src:
        tgt.execute("SET ROLE hyperchat_owner")  # the migrator has no USAGE on public of its own (NOINHERIT)
        with src.transaction():
            build_legacy(src, tgt)

    with _src_conn(legacy_db) as src, psycopg.connect(_dsn(verify_db, "hyperchat_migrator")) as tgt:
        ok, text = rehearsal.run(src, tgt, _dsn(verify_db, "hyperchat_readonly"), verify_db,
                                 media_from=(LEGACY_BASE,), media_to=MEDIA_BASE)
    assert ok, text
    assert "[PASS] PAY-15 verifier on the loaded database" in text and "FAIL" not in text
    for line in ("rows: obs_tokens  -- source=1 loaded=0", "rows: platform_soundboard_slots  -- source=1 loaded=0",
                 "rows: user_signups  -- source=1 loaded=1", "rows: telegram_callback_mapping  -- source=2 loaded=1",
                 "rows: api_keys  -- source=1 loaded=0", "rows: partner_api_keys  -- source=1 loaded=0",
                 "loaded pending for no owner=1", "KYC at launch (enforcement active, no grace)",
                 "rows: order_lookup  -- source=3 loaded=1 skipped={'unpaid_legacy_order': 2}",
                 "admins=1 admin_emails accounts=1 demoted=1",
                 "kyc_required=2 ",
                 "owner=2 linked_non_owner=1 dangling=0 source_links=3; owners_without_their_link=0"):
        if line not in text:  # pytest.fail keeps the whole message (an assert repr is truncated)
            pytest.fail(f"missing: {line}\nreport lines:\n" + "\n".join(
                x for x in text.splitlines() if any(w in x for w in ("unpaid", "order_lookup", "admins="))))

    # NV3-1: the unpaid-legacy-order check passed, with these counts (parsed, not string-matched)
    unpaid = [x for x in text.splitlines() if "no unpaid legacy order can be completed" in x]
    assert len(unpaid) == 1 and unpaid[0].startswith("[PASS] "), unpaid
    counts = dict(re.findall(r"(non-terminal|with_order_id|lookups|loaded failed)=(\d+)", unpaid[0]))
    assert counts == {"loaded failed": "3", "non-terminal": "0", "with_order_id": "0", "lookups": "0"}, unpaid
    assert "source unpaid={'failed': 1, 'pending': 2}" in unpaid[0], unpaid

    with psycopg.connect(_dsn(verify_db, "hyperchat_migrator"), autocommit=True) as c:
        c.execute("SET ROLE hyperchat_owner")
        credited = {(slug, name): v for slug, name, v in c.execute(
            "SELECT s.streamer_slug, lower(d.donor_name), sum(d.credited_inr) FROM public.donations d JOIN "
            "public.streamers s ON s.id = d.streamer_id WHERE d.payment_status = 'success' "
            "AND d.moderation_status IN ('approved', 'auto_approved') GROUP BY 1, 2").fetchall()}
        assert credited == {k: Decimal(v) for k, v in CREDITED.items()}
        assert c.execute("SELECT count(*) FROM public.donations WHERE num_nonnulls(voice_message_url, hypersound_url, "
                         "media_url, media_type, tts_audio_url, status_token_hash, upload_token_hash) > 0"
                         ).fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM public.obs_tokens").fetchone()[0] == 0
        assert c.execute("SELECT brand_logo_url, background_url FROM public.streamers WHERE id IN (%s, %s) ORDER BY id",
                         (S_ALPHA, S_BETA)).fetchall() == [(f"{MEDIA_BASE}/creator/alpha.png",
                                                            "/assets/streamers/alpha/bg.png"),
                                                           (None, f"{MEDIA_BASE}/creator/beta-bg.png")]
        assert c.execute("SELECT review_status FROM public.user_signups").fetchall() == [("legacy_unreviewed",)]
        # 0046: each legacy creator keeps the approval behaviour it had (never the new-creator defaults)
        assert {s: m for s, *m in c.execute("SELECT streamer_slug, text_approval_required, voice_approval_required, "
                                            "media_approval_required FROM public.streamers").fetchall()} == {
            "alpha": [True, True, True], "beta": [False, False, False],
            "gamma": [False, True, True], "sapphire": [False, True, True]}
        assert c.execute("SELECT kyc_track, kyc_baseline_owner_id::text FROM public.streamers ORDER BY id"
                         ).fetchall() == [("legacy", U_S1), ("legacy", U_S2), ("legacy", None), ("legacy", None)]
        assert c.execute("SELECT role FROM public.users WHERE id = %s", (U_ADMIN,)).fetchone()[0] == "admin"
        # NV3-2: a legacy role 'admin' outside admin_emails loads as a normal user
        assert c.execute("SELECT role FROM public.users WHERE id = %s", (U_ROGUE,)).fetchone()[0] == "user"
        # NV3-1: every unpaid legacy row is terminal 'failed' with no provider order id and no order_lookup row
        assert c.execute("SELECT count(*), count(*) FILTER (WHERE payment_status = 'failed' AND provider_order_id "
                         "IS NULL) FROM public.donations WHERE payment_status <> 'success'").fetchone() == (3, 3)
        assert c.execute("SELECT count(*) FROM public.order_lookup o JOIN public.donations d ON d.id = o.donation_id "
                         "WHERE d.payment_status <> 'success'").fetchone()[0] == 0
        # CRE-4 / CRE-7 while KYC enforcement is draft: the owned legacy creators keep accepting donations
        decisions = dict(c.execute("SELECT streamer_slug, public.creator_donation_block(id) FROM public.streamers"
                                   ).fetchall())
        assert decisions == {"alpha": None, "beta": None, "gamma": "not_active", "sapphire": "not_active"}
        # owner 2026-11-27: an active creator without an owner loads 'pending'; a pending one stays pending
        assert dict(c.execute("SELECT streamer_slug, status FROM public.streamers").fetchall()) == {
            "alpha": "active", "beta": "active", "gamma": "pending", "sapphire": "pending"}
        # ...and at launch (enforcement active, no grace grants) no migrated creator takes donations before KYC
        launch = dict(c.execute("SELECT streamer_slug, public.kyc_donation_decision(id, 'active') "
                                "FROM public.streamers").fetchall())
        assert launch == {"alpha": "kyc_required", "beta": "kyc_required", "gamma": "not_active",
                          "sapphire": "not_active"}
        assert c.execute("SELECT count(*) FROM public.kyc_grace_grants").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM public.api_keys").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM public.partner_api_keys").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM public.creator_image_uploads").fetchone()[0] == 0  # 0039: no record
        assert c.execute("SELECT state FROM public.kyc_enforcement").fetchone()[0] == "draft"

    # 0036 with every trigger back on: the pricing rule binds the runtime roles only. A legacy value below today's
    # floors loads (the owner / migrator is exempt by design); the same write by the API role is refused.
    with psycopg.connect(_dsn(verify_db, "hyperchat_migrator"), autocommit=True) as c:
        c.execute("SET ROLE hyperchat_owner")
        with c.transaction(force_rollback=True):
            c.execute("UPDATE public.streamers SET min_voice_amount_inr = 20 WHERE id = %s", (S_ALPHA,))
            c.execute("UPDATE public.streamer_voice_tiers SET max_duration_seconds = 60 WHERE streamer_id = %s",
                      (S_ALPHA,))
    with psycopg.connect(_dsn(verify_db, "hyperchat_app"), autocommit=True) as c:
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("UPDATE public.streamers SET min_voice_amount_inr = 20 WHERE id = %s", (S_ALPHA,))
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("UPDATE public.streamer_voice_tiers SET max_duration_seconds = 60 WHERE streamer_id = %s",
                      (S_ALPHA,))
        # 0024: the D1 apply role may never write an administrator's credentials (why the importer excludes them)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE public.users SET password_hash = 'x' WHERE id = %s", (U_ADMIN,))

    # a second load into the same (no longer fresh) database refuses and changes nothing
    with _src_conn(legacy_db) as src, psycopg.connect(_dsn(verify_db, "hyperchat_migrator")) as tgt:
        with pytest.raises(SystemExit, match="holds append-only evidence"):
            rehearsal.run(src, tgt, _dsn(verify_db, "hyperchat_readonly"), verify_db)

    # D1: the password import on the loaded schema (as hyperchat_app, the documented apply role). The admin account
    # (admin_emails) is excluded: 0024 refuses a runtime-role write to an admin row, which would roll back the run.
    def digest(v: str) -> str:
        return hashlib.sha256(v.encode()).hexdigest()

    gotrue = "$2a$" + bcrypt.hashpw(b"Synthetic-Pass-42", bcrypt.gensalt(rounds=4)).decode()[4:]
    source = [imp.Source(SUPA[U_S1], digest("s1" + D), digest("s1" + D), gotrue, True, False, False),
              imp.Source(SUPA[U_ADMIN], digest("admin" + D), digest("admin" + D), gotrue, True, False, False),
              imp.Source(SUPA[U_S2], digest("s2" + D), digest("s2" + D), None, True, False, False)]
    with psycopg.connect(_dsn(verify_db, "hyperchat_app")) as conn:
        p = imp.plan(source, imp.read_targets(conn))
        conn.rollback()
        assert (p.counts["eligible"], p.counts["target_admin_account"], p.counts["source_without_password"]) == \
            (1, 1, 1) and not p.blocked
        p.fingerprint = digest("synthetic source snapshot")
        assert imp.apply(conn, p, run_id=str(uuid.uuid4())) == 1
    _assert_verified(_verify(verify_db), "after the password import")

    # NV3-3: Supabase-deleted (plain) and -banned (admin) accounts are deactivated by --deactivate as the migration
    # role (an admin row and a runtime-role is_active change are both guarded for hyperchat_app); never re-activated.
    # NV4-3: and the accounts the D1 exclusion policy keeps on hold until identity is resolved, though their own
    # Supabase row is unflagged: s2 has no Supabase row (a hard delete; it owns creator beta), rogue's Supabase email
    # differs from its V2 email. s1 matches exactly and stays active.
    flagged = [imp.Source(SUPA[U_PLAIN], digest("plain" + D), digest("plain" + D), None, True, True, False),
               imp.Source(SUPA[U_ADMIN], digest("admin" + D), digest("admin" + D), None, True, False, True),
               imp.Source(SUPA[U_S1], digest("s1" + D), digest("s1" + D), gotrue, True, False, False),
               imp.Source(SUPA[U_ROGUE], digest("renamed" + D), digest("renamed" + D), None, True, False, False)]
    with psycopg.connect(_dsn(verify_db, "hyperchat_migrator")) as conn:
        conn.execute("SET ROLE hyperchat_owner")  # as main() does for --deactivate
        conn.commit()
        p = imp.plan(flagged, imp.read_targets(conn))
        conn.rollback()
        assert {x.target_id: x.why for x in p.deactivations} == {
            U_PLAIN: "banned_or_deleted", U_ADMIN: "banned_or_deleted", U_S2: "target_legacy_id_not_in_source",
            U_ROGUE: "id_match_email_mismatch"}
        assert (p.counts["deactivate_banned_or_deleted"], p.counts["hold_target_legacy_id_not_in_source"],
                p.counts["hold_id_match_email_mismatch"]) == (2, 1, 1)
        p.fingerprint = digest("synthetic source snapshot")
        assert imp.deactivate(conn, p) == 4
        again = imp.plan(flagged, imp.read_targets(conn))
        conn.rollback()
        assert again.deactivations == [] and (again.counts["banned_or_deleted_already_inactive"],
                                              again.counts["hold_already_inactive"]) == (2, 2)
    with psycopg.connect(_dsn(verify_db, "hyperchat_migrator"), autocommit=True) as c:
        c.execute("SET ROLE hyperchat_owner")
        assert dict(c.execute("SELECT id::text, is_active FROM public.users WHERE id IN (%s, %s, %s, %s, %s)",
                              (U_PLAIN, U_ADMIN, U_S1, U_S2, U_ROGUE)).fetchall()) == {
            U_PLAIN: False, U_ADMIN: False, U_S1: True, U_S2: False, U_ROGUE: False}
        # a held owner's creator takes no donations under the launch KYC rule (no active owner)
        assert c.execute("SELECT public.kyc_donation_decision(%s, 'active')", (S_BETA,)).fetchone()[0] == "no_owner"

    # The live credit writer on migrated data: reject the partly credited newest 'Short' donation (credit 20 of 80)
    # as the runtime role; its exact credit leaves the donor total (120 -> 100), nothing floors, the verifier agrees.
    with psycopg.connect(_dsn(verify_db, "hyperchat_app"), autocommit=True) as c:
        did, credit = c.execute("SELECT id, credited_inr FROM public.donations WHERE donor_name = 'Short' "
                                "ORDER BY created_at DESC LIMIT 1").fetchone()
        assert credit == Decimal("20.00")
        c.execute("UPDATE public.donations SET moderation_status = 'rejected' WHERE id = %s", (did,))
        assert c.execute("SELECT total_amount FROM public.streamer_donator_totals WHERE streamer_id = %s AND "
                         "donator_name_lower = 'short'", (S_ALPHA,)).fetchone()[0] == Decimal("100.00")
    _assert_verified(_verify(verify_db), "after a writer debit on a migrated donation")


def test_the_load_refuses_a_target_that_is_not_fresh(verify_db):
    with psycopg.connect(_dsn(verify_db, "hyperchat_migrator"), autocommit=True) as c:
        c.execute("SET ROLE hyperchat_owner")
        c.execute("INSERT INTO public.visits (ip_address) VALUES ('192.0.2.9')")
        before = c.execute("SELECT count(*) FROM public.visits").fetchone()[0]
    with psycopg.connect(_dsn(verify_db, "hyperchat_migrator")) as tgt:
        with pytest.raises(SystemExit, match="not fresh"):
            rehearsal.Loader(None, tgt).prepare_target()
        tgt.rollback()
    with psycopg.connect(_dsn(verify_db, "hyperchat_migrator"), autocommit=True) as c:
        c.execute("SET ROLE hyperchat_owner")
        assert c.execute("SELECT count(*) FROM public.visits").fetchone()[0] == before
        assert c.execute("SELECT count(*) FROM pg_trigger t JOIN pg_class r ON r.oid = t.tgrelid WHERE "
                         "r.relnamespace = 'public'::regnamespace AND NOT t.tgisinternal AND t.tgenabled <> 'O'"
                         ).fetchone()[0] == 0
