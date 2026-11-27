import contextlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from etl.mapping import (
    normalize_currency,
    razorpay_account,
    slug_from_table,
    split_approved_by,
    user_role,
)

MOD = "6f0e8f2a-1c2b-4d3e-9f40-0a1b2c3d4e5f"


@pytest.mark.parametrize("value, expected", [
    (None, (None, None)),
    ("", (None, None)),
    (MOD, (MOD, None)),
    (MOD.upper(), (MOD, None)),
    ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", (None, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")),  # unknown UUID stays text
    ("system", (None, "system")),
    ("Some Moderator", (None, "Some Moderator")),
])
def test_split_approved_by(value, expected):
    assert split_approved_by(value, {MOD}) == expected


@pytest.mark.parametrize("value, expected", [("INR", "INR"), (" usd ", "USD"), (None, "INR"), ("eur", "EUR")])
def test_normalize_currency(value, expected):
    assert normalize_currency(value) == expected


@pytest.mark.parametrize("value", ["RUPEE", "1NR", "", "US"])
def test_normalize_currency_rejects_invalid(value):
    with pytest.raises(ValueError):
        normalize_currency(value)


def test_user_role_folds_admin_emails_case_insensitively():
    admins = {"admin@example.invalid"}
    assert user_role("user", "Admin@Example.Invalid", admins) == "admin"
    assert user_role("streamer", "s@example.invalid", admins) == "streamer"
    assert user_role(None, "u@example.invalid", admins) == "user"
    # NV3-2: admin_emails is the only source of 'admin'; a legacy role 'admin' elsewhere is a normal user
    assert user_role("admin", "removed@example.invalid", admins) == "user"
    assert user_role("admin", "ADMIN@example.invalid", admins) == "admin"
    assert user_role("streamer", "admin@example.invalid", admins) == "admin"


@pytest.mark.parametrize("key, secret, expected", [
    ("razorpay-keyid", "razorpay-keysecret", "primary"),
    ("razorpay-keyid(test)", "razorpay-keysecret(test)", "primary"),  # D6: test-mode keys, not a 2nd account
    ("razorpay-keyid", "razorpay-keysecret(test)", None),  # mixed pair: no account
    (None, None, None),
    ("razorpay-keyid", None, None),
    ("RAZORPAY-KEYID", "RAZORPAY-KEYSECRET", None),  # names are exact
])
def test_razorpay_account(key, secret, expected):
    assert razorpay_account(key, secret) == expected


def test_slug_from_table():
    assert slug_from_table("gaming_with_latifa_donations") == "gaming_with_latifa"
    with pytest.raises(ValueError):
        slug_from_table("streamers")


def test_both_legacy_key_pairs_are_the_one_account_and_orders_get_none():
    """D6: the "(test)" pair held test-mode keys, an environment concern, not a second account. Legacy orders never
    recorded an account, so the loader writes none (donations.payment_account stays NULL)."""
    from etl.mapping import RAZORPAY_ACCOUNTS
    from etl.rehearsal import DONATION_COLUMNS

    assert set(RAZORPAY_ACCOUNTS.values()) == {"primary"} and len(RAZORPAY_ACCOUNTS) == 2
    assert "payment_account" not in DONATION_COLUMNS and "provider_payment_id" not in DONATION_COLUMNS


# ── D9: media URL base rewrite ─────────────────────────────────────────────────

LEGACY = ("https://pub-aaa.r2.dev", "https://pub-bbb.r2.dev")
TARGET = "https://media.example.test"
VOICE = "voice-messages/creator_1700000000000_" + "a" * 32 + ".webm"


@pytest.mark.parametrize("url, expected", [
    (f"{LEGACY[0]}/{VOICE}", f"{TARGET}/{VOICE}"),                                    # voice
    (f"{LEGACY[1]}/donation-media/x_1700000000000_{'b' * 32}.png",
     f"{TARGET}/donation-media/x_1700000000000_{'b' * 32}.png"),                       # the second legacy host
    (f"{LEGACY[0]}/soundboards/s/x.mp3", f"{TARGET}/soundboards/s/x.mp3"),            # soundboard
    (f"{LEGACY[0]}/tts-audio/a/b.mp3", f"{TARGET}/tts-audio/a/b.mp3"),
    (f"{TARGET}/{VOICE}", f"{TARGET}/{VOICE}"),                                       # already on the target
    ("https://xyz.supabase.co/storage/v1/object/public/a.png",
     "https://xyz.supabase.co/storage/v1/object/public/a.png"),                        # unrelated: unchanged
    ("https://pub-aaa.r2.dev.evil.example/" + VOICE, "https://pub-aaa.r2.dev.evil.example/" + VOICE),
    (f"{LEGACY[0]}", f"{LEGACY[0]}"), (f"{LEGACY[0]}/", f"{LEGACY[0]}/"),           # no key: unchanged
    (None, None),
])
def test_rewrite_media_url(url, expected):
    from etl.mapping import rewrite_media_url
    assert rewrite_media_url(url, LEGACY, TARGET) == expected
    assert rewrite_media_url(expected, LEGACY, TARGET) == expected  # idempotent
    assert rewrite_media_url(url, LEGACY, None) == url               # no target: copy unchanged


@pytest.mark.parametrize("value, ok", [
    ("https://media.example.test", "https://media.example.test"), ("https://media.example.test/", "https://media.example.test"),
    ("https://x.example/media/", "https://x.example/media"), ("http://media.example.test", None), ("", None),
    ("https://media.example.test/?a=1", None), ("https://u:p@media.example.test", None), ("media.example.test", None),
])
def test_media_base(value, ok):
    from etl.mapping import media_base
    if ok is None:
        with pytest.raises(ValueError):
            media_base(value)
    else:
        assert media_base(value) == ok


def test_loader_rewrites_only_the_creator_image_columns_and_counts():
    """Fresh start: only the creator profile picture and background are carried over and rewritten; donation and
    soundboard media are never rewritten (donation media is cleared, soundboard slots are not migrated)."""
    from etl.mapping import MEDIA_URL_COLUMNS
    from etl.rehearsal import DONATION_COLUMNS, Loader

    assert MEDIA_URL_COLUMNS == {"streamers": ("brand_logo_url", "background_url")}
    loader = Loader(None, None, media_from=LEGACY, media_to=TARGET)
    cols = ("id", "brand_logo_url", "background_url", "streamer_name")
    logo = f"{LEGACY[0]}/creator/logo.png"
    row = ("x", logo, "/assets/streamers/x/bg.png", logo)
    out = loader.media("streamers", cols, [row])[0]
    assert out == ("x", f"{TARGET}/creator/logo.png", "/assets/streamers/x/bg.png", logo)
    assert dict(loader.media_rewritten) == {"streamers.brand_logo_url": 1}
    assert loader.media("streamers", cols, [out])[0] == out  # idempotent
    voice = (f"{LEGACY[0]}/{VOICE}",) * len(DONATION_COLUMNS)
    assert loader.media("donations", DONATION_COLUMNS, [voice]) == [voice]
    assert Loader(None, None).media("streamers", cols, [row]) == [row]  # no rewrite configured


def test_fresh_start_clears_donation_media_and_skips_obs_tokens_and_soundboard_slots():
    from etl.mapping import DONATION_CLEARED_COLUMNS
    from etl.rehearsal import DONATION_COLUMNS, LOADED_TABLES, NOT_MIGRATED, STRAIGHT_COPIES

    assert set(DONATION_CLEARED_COLUMNS) == {"voice_message_url", "hypersound_url", "media_url", "media_type",
                                             "tts_audio_url", "status_token_hash", "upload_token_hash"}
    assert set(DONATION_CLEARED_COLUMNS) <= set(DONATION_COLUMNS)
    skipped = {t for t, _ in NOT_MIGRATED}
    assert {"obs_tokens", "platform_soundboard_slots", "streamer_soundboard_slots", "soundboard_swap_log",
            "api_keys", "partner_api_keys"} <= skipped  # owner 2026-11-27: old links and keys are reset
    assert not skipped & set(LOADED_TABLES) and set(STRAIGHT_COPIES) <= set(LOADED_TABLES)
    import etl.mapping
    assert not hasattr(etl.mapping, "obs_token_hash")


def _upgrade_added_columns(table: str) -> set[str]:
    """Columns that migrations 0010+ add to `table` in their upgrade() (the V2-only columns of a straight copy)."""
    found = set()
    for f in sorted((Path(__file__).resolve().parents[1] / "migrations" / "versions").glob("*.py")):
        if int(f.name[:4]) < 10:  # 0001-0009 copied the legacy structure (the rehearsed baseline)
            continue
        up = f.read_text(encoding="utf-8").split("def downgrade")[0]
        for m in re.finditer(r"ALTER TABLE public\.%s\b(.*?)(?=op\.execute|\Z)" % table, up, re.S):
            found |= set(re.findall(r"ADD COLUMN (\w+)", m.group(1)))
    return found


def test_v2_only_columns_are_exactly_what_the_migrations_added_to_straight_copies():
    from etl.mapping import LEGACY_VALUES, V2_ONLY_COLUMNS
    from etl.rehearsal import STRAIGHT_COPIES

    added = {t: _upgrade_added_columns(t) for t in STRAIGHT_COPIES}
    assert {t: set(c) for t, c in V2_ONLY_COLUMNS.items()} == {t: c for t, c in added.items() if c}
    assert LEGACY_VALUES == {"user_signups": {"review_status": "legacy_unreviewed"}}


@pytest.mark.parametrize("source, ro, target, production, ok", [
    ("hyperchat_restore_test", "on", "hyperchat_dev", False, True),
    ("hyperchat_restore_test", "on", "hyperchat_cutover_rehearsal", False, True),
    ("hyperchat_restore_test", "on", "hyperchat", True, True),
    ("hyperchat_restore_test", "on", "hyperchat", False, False),      # production needs the flag
    ("hyperchat_restore_test", "on", "hyperchat_dev", True, False),   # the flag loads only production
    ("hyperchat_restore_test", "off", "hyperchat_dev", False, False),  # writable source
    ("hyperchat_dev", "on", "hyperchat_x", False, False),              # not the legacy restore
    ("hyperchat_restore_test", "on", "hyperchat_restore_test", False, False),
])
def test_database_guards(source, ro, target, production, ok):
    from etl.rehearsal import check_databases

    if ok:
        check_databases(source, ro, target, production)
    else:
        with pytest.raises(SystemExit, match="refusing to run"):
            check_databases(source, ro, target, production)


def _finding(inv, cls, problem=None):
    return {"invariant": inv, "class": cls, "detail": {"problem": problem} if problem else {}}


def test_verifier_result_accepts_clean_or_exactly_the_pre_phase_2b_i10_finding():
    from etl.reconcile import verifier_result

    i10 = _finding("I10", "structural", "baseline_unverified")
    assert verifier_result({"status": "clean", "findings": []}, False)[0]
    assert verifier_result({"status": "violation", "findings": [i10]}, False)[0]
    assert not verifier_result({"status": "violation", "findings": [i10]}, True)[0]  # once pinned: never accepted
    assert not verifier_result({"status": "violation", "findings": [i10, _finding("I7", "structural")]}, False)[0]
    assert not verifier_result({"status": "attention", "findings": [_finding("L-2", "legacy_uncertain")]}, False)[0]
    assert not verifier_result({"status": "refused", "findings": []}, False)[0]
    assert "I7/structural/-=1" in verifier_result({"status": "violation", "findings": [_finding("I7", "structural")]},
                                                  False)[1]


def test_loader_puts_every_legacy_creator_on_the_legacy_kyc_track():
    """Migration 0020: legacy has no kyc_track / kyc_baseline_owner_id. The import writes 'legacy' and the creator's
    imported owner as the baseline, and keeps its own status, except that an active creator without an owner loads
    'pending' (owner decision 2026-11-27)."""
    from etl.rehearsal import Loader

    queries: list[str] = []
    loader = Loader(None, None)
    loader.fetch = lambda query, params=None: queries.append(query) or [(0,)] * query.startswith("SELECT count")
    loader.columns = lambda conn, table: ["id", "streamer_slug", "status", "user_id", "kyc_track",
                                          "kyc_baseline_owner_id"]
    loader.copy = lambda table, columns, rows: 0
    loader.tgt = SimpleNamespace(cursor=lambda: contextlib.nullcontext(SimpleNamespace(execute=lambda *a: None)))
    loader.load_users_and_streamers()
    assert queries[-2] == ('SELECT "id", "streamer_slug", CASE WHEN user_id IS NULL AND status = \'active\' '
                           'THEN \'pending\' ELSE status END, "user_id", \'legacy\', "user_id" FROM public.streamers')


def test_legacy_creators_keep_their_moderation_in_the_per_mode_columns():
    """Migration 0046: production is built fresh at head and loaded afterwards, so the migration's backfill never sees
    a legacy creator. The loader maps each one from its own moderation_mode / media_moderation_enabled with exactly
    the migration's expressions (else every imported creator would silently get the new-creator defaults)."""
    import importlib.util

    from etl.mapping import MODERATION_FROM_LEGACY
    from etl.rehearsal import Loader

    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0046_per_mode_moderation.py"
    spec = importlib.util.spec_from_file_location("_m0046_for_etl", path)
    m0046 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m0046)
    assert MODERATION_FROM_LEGACY == m0046.BACKFILL

    queries: list[str] = []
    loader = Loader(None, None)
    loader.fetch = lambda query, params=None: queries.append(query) or [(0,)] * query.startswith("SELECT count")
    loader.columns = lambda conn, table: ["id", *MODERATION_FROM_LEGACY]
    loader.copy = lambda table, columns, rows: 0
    loader.tgt = SimpleNamespace(cursor=lambda: contextlib.nullcontext(SimpleNamespace(execute=lambda *a: None)))
    loader.load_users_and_streamers()
    assert queries[-2] == 'SELECT "id", ' + ", ".join(MODERATION_FROM_LEGACY.values()) + " FROM public.streamers"
