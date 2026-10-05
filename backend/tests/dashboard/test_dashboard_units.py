"""Phase 5 pure logic and HTTP plumbing (no database)."""

import base64
import csv
import io
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.auth.repository import User
from app.core.errors import ApiError
from app.dashboard import csv_export, models
from app.dashboard import validation as v
from app.dashboard.moderation import ACTIONS, STATUSES, NotPaid, Step, actor_label, transition
from app.dashboard.repository import SETTINGS_SQL_TYPES
from app.dashboard.validation import parse_uuid
from app.routers.dashboard import is_private_path


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_DB_ENABLED", "false")
    monkeypatch.setenv("HYPERCHAT_APP_ENV", "test")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app(), base_url="https://testserver") as c:
        yield c
    get_settings.cache_clear()


# ── Cache-Control: no-store, scoped to dashboard + terms ────────────────────────

PRIVATE = ["/api/v1/dashboard/streamers/x/settings", "/api/v1/dashboard/streamers/x/donations/export.csv",
           "/api/v1/dashboard/anything", "/api/v1/me/terms", "/api/v1/me/terms/accept"]
NOT_PRIVATE = ["/health", "/api/v1/me/streamers", "/api/v1/me/termsx", "/api/v1/auth/session",
               "/api/v1/public/streamers/x", "/api/v1/metrics/platform", "/api/v1/streamers/x/access",
               "/api/v1/dashboardx", "/api/v1/admin/users"]


def test_private_path_scope():
    assert all(is_private_path(p) for p in PRIVATE)
    assert not any(is_private_path(p) for p in NOT_PRIVATE)


def test_no_store_on_dashboard_and_terms_only(client):
    for path in PRIVATE:
        for method in ("GET", "POST", "PATCH", "DELETE"):
            r = client.request(method, path)
            assert r.headers.get("cache-control") == "no-store", (method, path, r.status_code)
    for path in NOT_PRIVATE:
        r = client.get(path)
        assert r.headers.get("cache-control") != "no-store", (path, r.status_code)


# ── ids ─────────────────────────────────────────────────────────────────────────

def test_parse_uuid():
    assert parse_uuid("6F9619FF-8B86-D011-B42D-00C04FC964FF") == "6f9619ff-8b86-d011-b42d-00c04fc964ff"
    for bad in ("", "nope", "123", None, "6f9619ff-8b86-d011-b42d-00c04fc964f", "' OR 1=1 --"):
        assert parse_uuid(bad) is None


# ── G1 settings: field sets ─────────────────────────────────────────────────────

def test_writable_settings_are_the_legacy_allowlist_minus_the_read_only_amounts():
    legacy = {"moderation_mode", "telegram_moderation_enabled", "discord_moderation_enabled", "tts_enabled",
              "hyperemotes_enabled", "hyperemotes_min_amount", "alert_box_scale", "goal_is_active", "goal_name",
              "goal_target_amount", "goal_activated_at", "media_upload_enabled", "media_moderation_enabled",
              "media_min_amount", "tts_language_boost", "alert_overlay_id", "leaderboard_overlay_id",
              "overlay_adjust_mode", "leaderboard_bg_enabled", "goal_overlay_id", "brand_color",
              "leaderboard_widget_enabled", "el_voice_id"}  # update-streamer-settings/index.ts:132
    assert len(legacy) == 23 and len(v.WRITABLE_SETTINGS) == 21
    assert set(v.WRITABLE_SETTINGS) == legacy - set(v.READ_ONLY_SETTINGS)
    assert set(models.SettingsPatch.model_fields) == set(v.WRITABLE_SETTINGS)
    assert tuple(SETTINGS_SQL_TYPES) == v.WRITABLE_SETTINGS
    shown = set(models.DashboardSettings.model_fields)
    assert set(v.WRITABLE_SETTINGS) | set(v.READ_ONLY_SETTINGS) | {"slug", "name", "status"} == shown
    for private in ("id", "user_id", "rejection_reason", "order_prefix", "pusher_group", "razorpay_key_ref",
                    "razorpay_secret_ref", "discord_moderation_channel_id", "api_enabled", "custom_page_path"):
        assert private not in shown


# ── G1 settings: validation rules (same Python-mode validation FastAPI applies to the body) ──

def _patch(body: dict) -> dict:
    return models.SettingsPatch.model_validate(body).changes()


VALID = [
    ({"moderation_mode": "manual"}, "manual"),
    ({"moderation_mode": "auto_approve"}, "auto_approve"),
    ({"tts_enabled": False}, False),
    ({"leaderboard_widget_enabled": True}, True),
    ({"alert_box_scale": 0.75}, 0.75),
    ({"alert_box_scale": 1}, 1.0),
    ({"alert_box_scale": 1.5}, 1.5),
    ({"goal_name": "  New mic  "}, "New mic"),
    ({"goal_name": "x" * 120}, "x" * 120),
    ({"goal_target_amount": 1}, 1),
    ({"goal_target_amount": 0.01}, 0.01),
    ({"goal_target_amount": 100_000_000}, 100_000_000),
    ({"goal_activated_at": None}, None),
    ({"tts_language_boost": "hindi"}, "Hindi"),
    ({"tts_language_boost": "VIETNAMESE"}, "Vietnamese"),
    ({"tts_language_boost": "Portuguese"}, "Portuguese"),
    ({"alert_overlay_id": 0}, 0),
    ({"alert_overlay_id": 16}, 16),
    ({"leaderboard_overlay_id": 15}, 15),
    ({"goal_overlay_id": 6}, 6),
    ({"brand_color": "#3b82f6"}, "#3b82f6"),
    ({"brand_color": "#ABCDEF"}, "#ABCDEF"),
    ({"el_voice_id": "Voice12345"}, "Voice12345"),
    ({"el_voice_id": "a" * 64}, "a" * 64),
    ({"el_voice_id": ""}, None),
    ({"el_voice_id": None}, None),
]


@pytest.mark.parametrize("body,expected", VALID)
def test_valid_settings(body, expected):
    assert _patch(body) == {next(iter(body)): expected}


def test_goal_activated_at_needs_a_timezone():
    assert _patch({"goal_activated_at": "2026-10-01T10:00:00+05:30"}) == {
        "goal_activated_at": datetime(2026, 10, 1, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))}
    assert _patch({"goal_activated_at": "2026-10-01T04:30:00Z"})["goal_activated_at"] == datetime(
        2026, 10, 1, 4, 30, tzinfo=UTC)


INVALID = [
    {"moderation_mode": "MANUAL"}, {"moderation_mode": "auto"}, {"moderation_mode": None},
    {"tts_enabled": "true"}, {"tts_enabled": 1}, {"tts_enabled": 0}, {"tts_enabled": None},
    {"goal_is_active": "yes"}, {"overlay_adjust_mode": None}, {"leaderboard_bg_enabled": "false"},
    {"alert_box_scale": 2}, {"alert_box_scale": 0.5}, {"alert_box_scale": "1.0"}, {"alert_box_scale": True},
    {"alert_box_scale": None}, {"alert_box_scale": float("nan")},
    {"goal_name": ""}, {"goal_name": "   "}, {"goal_name": "x" * 121}, {"goal_name": 5}, {"goal_name": None},
    {"goal_target_amount": 0}, {"goal_target_amount": -5}, {"goal_target_amount": 100_000_000.01},
    {"goal_target_amount": "100"}, {"goal_target_amount": True}, {"goal_target_amount": float("nan")},
    {"goal_target_amount": float("inf")}, {"goal_target_amount": None},
    {"goal_activated_at": "2026-10-01"}, {"goal_activated_at": "2026-10-01T10:00:00"},
    {"goal_activated_at": "yesterday"}, {"goal_activated_at": 1700000000}, {"goal_activated_at": ""},
    {"tts_language_boost": "Klingon"}, {"tts_language_boost": ""}, {"tts_language_boost": "Hindi "},
    {"tts_language_boost": None}, {"tts_language_boost": 5},
    {"alert_overlay_id": -1}, {"alert_overlay_id": 17}, {"alert_overlay_id": "3"}, {"alert_overlay_id": 3.0},
    {"alert_overlay_id": True}, {"alert_overlay_id": None},
    {"leaderboard_overlay_id": 16}, {"goal_overlay_id": 7}, {"goal_overlay_id": -1},
    {"brand_color": "3b82f6"}, {"brand_color": "#3b82f"}, {"brand_color": "#3b82f6\n"}, {"brand_color": "red"},
    {"brand_color": "#3b82f6ff"}, {"brand_color": None},
    {"el_voice_id": "short"}, {"el_voice_id": "has space 123"}, {"el_voice_id": "a" * 65},
    {"el_voice_id": "abc-def-ghi-jk"}, {"el_voice_id": 1234567890},
    {"hyperemotes_min_amount": 5}, {"media_min_amount": 5},  # read-only in Phase 5 (A1)
    {"status": "active"}, {"slug": "x"}, {"user_id": "x"}, {"rejection_reason": "x"}, {"pusher_group": 2},
]


@pytest.mark.parametrize("body", INVALID, ids=lambda b: repr(b)[:60])
def test_invalid_settings(body):
    with pytest.raises(ValidationError):
        models.SettingsPatch.model_validate(body)


def test_patch_reports_only_sent_fields_in_a_fixed_order():
    body = {"el_voice_id": None, "brand_color": "#000000", "moderation_mode": "manual"}
    assert list(_patch(body)) == ["moderation_mode", "brand_color", "el_voice_id"]
    assert _patch({}) == {}


# ── G2 keyset cursor ────────────────────────────────────────────────────────────

DID = "6f9619ff-8b86-d011-b42d-00c04fc964ff"
AT = datetime(2026, 10, 5, 18, 29, 59, 999999, tzinfo=UTC)


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def test_cursor_round_trip_keeps_microseconds_and_offset():
    cursor = v.encode_cursor(AT, DID)
    assert v.decode_cursor(cursor) == (AT, DID) and v.cursor_position(cursor) == (AT, DID)
    ist = AT.astimezone(v.IST)
    assert v.decode_cursor(v.encode_cursor(ist, DID)) == (ist, DID) and v.cursor_position(None) is None
    assert "=" not in cursor and "|" not in cursor


@pytest.mark.parametrize("bad", [
    "", "x", "!!!!", "€uro", "a" * 201, _b64("no-separator"), _b64(f"{AT.isoformat()}|not-a-uuid"),
    _b64(f"2026-10-05T18:29:59|{DID}"),            # no timezone
    _b64(f"yesterday|{DID}"), _b64(f"{AT.isoformat()}|{DID.upper()}"), _b64(f"|{DID}"),
    _b64(f"{AT.isoformat()}|{DID}|extra"),
])
def test_tampered_cursors_are_rejected(bad):
    with pytest.raises(ValueError):
        v.decode_cursor(bad)
    with pytest.raises(ApiError) as err:
        v.cursor_position(bad)
    assert (err.value.status, err.value.error) == (400, "Invalid cursor")


def test_keyset_page_uses_the_extra_row_only_for_has_more():
    rows = [{"created_at": AT - timedelta(seconds=i), "donation_id": DID} for i in range(3)]
    items, cursor, more = v.keyset_page(rows, 2)
    assert len(items) == 2 and more and v.decode_cursor(cursor) == (rows[1]["created_at"], DID)
    assert v.keyset_page(rows, 3) == (rows, None, False)
    assert v.keyset_page([], 5) == ([], None, False)


# ── G2 IST boundaries (UTC+05:30: an IST day starts at 18:30:00 UTC the previous day) ──

def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_ist_day_boundary_at_1830_utc():
    assert v.ist_day_bounds(_utc(2026, 10, 5, 18, 29, 59, 999999)) == (_utc(2026, 10, 4, 18, 30), _utc(2026, 10, 5, 18, 30))
    assert v.ist_day_bounds(_utc(2026, 10, 5, 18, 30)) == (_utc(2026, 10, 5, 18, 30), _utc(2026, 10, 6, 18, 30))
    assert v.ist_day_bounds(_utc(2026, 10, 5, 0, 0)) == (_utc(2026, 10, 4, 18, 30), _utc(2026, 10, 5, 18, 30))


def test_ist_month_and_year_rollover():
    assert v.ist_month_bounds(_utc(2026, 9, 30, 18, 29, 59)) == (_utc(2026, 8, 31, 18, 30), _utc(2026, 9, 30, 18, 30))
    assert v.ist_month_bounds(_utc(2026, 9, 30, 18, 30)) == (_utc(2026, 9, 30, 18, 30), _utc(2026, 10, 31, 18, 30))
    assert v.ist_month_bounds(_utc(2026, 12, 31, 18, 30)) == (_utc(2026, 12, 31, 18, 30), _utc(2027, 1, 31, 18, 30))
    assert v.ist_month_bounds(_utc(2028, 2, 10)) == (_utc(2028, 1, 31, 18, 30), _utc(2028, 2, 29, 18, 30))
    assert v.ist_midnight(date(2026, 10, 6)).isoformat() == "2026-10-06T00:00:00+05:30"
    assert v.TIMEZONE_NAME == "Asia/Kolkata"


# ── G2 export range ─────────────────────────────────────────────────────────────

def test_export_days_valid_ranges():
    assert v.export_days("2026-10-01", "2026-10-01") == (date(2026, 10, 1), date(2026, 10, 1))
    assert v.export_days("2024-01-01", "2024-12-31")[1] == date(2024, 12, 31)   # 366 days (leap year)
    assert v.export_days("2025-01-01", "2026-01-01")[1] == date(2026, 1, 1)     # 366 days


@pytest.mark.parametrize("start,end,message", [
    (None, "2026-10-01", "from is required (YYYY-MM-DD)"),
    ("2026-10-01", None, "to is required (YYYY-MM-DD)"),
    ("", "2026-10-01", "from is required (YYYY-MM-DD)"),
    ("2026-1-01", "2026-10-01", "from must be a date in YYYY-MM-DD format"),
    ("20261001", "2026-10-01", "from must be a date in YYYY-MM-DD format"),
    ("2026-10-01T00:00", "2026-10-02", "from must be a date in YYYY-MM-DD format"),
    ("2026-02-30", "2026-03-01", "from must be a date in YYYY-MM-DD format"),
    ("2026-10-01", "2026-10-01\n", "to must be a date in YYYY-MM-DD format"),
    ("2026-10-02", "2026-10-01", "to must not be before from"),
    ("2025-01-01", "2026-01-02", "The date range is limited to 366 days"),       # 367 days
])
def test_export_days_rejections(start, end, message):
    with pytest.raises(ValueError) as err:
        v.export_days(start, end)
    assert str(err.value) == message


# ── G2 CSV encoding ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("=HYPERLINK(\"http://x\")", "'=HYPERLINK(\"http://x\")"), ("+91 boss", "'+91 boss"), ("-1", "'-1"),
    ("@SUM(A1)", "'@SUM(A1)"), ("\tTab", "'\tTab"), ("\rCR", "'\rCR"), ("Ravi", "Ravi"), ("a=b", "a=b"),
    ("", ""), (" =x", " =x"),
])
def test_formula_cells_are_neutralised(text, expected):
    assert csv_export.neutralize(text) == expected


def test_csv_encoding_columns_values_and_quoting():
    rows = [{"created_at": _utc(2026, 10, 5, 18, 30, 15, 123456), "donor_name": 'Quote "me", =now',
             "amount": Decimal("10.50"), "currency": "USD", "amount_inr": Decimal("934.50"), "order_id": "secret"},
            {"created_at": _utc(2026, 10, 5, 18, 29, 59), "donor_name": "=cmd|' /C calc'!A0", "amount": Decimal("1E+2"),
             "currency": "INR", "amount_inr": Decimal("100"), "order_id": "secret"}]
    text = "".join(csv_export.encode(rows))
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed == [["created_at", "donor_name", "amount", "currency", "amount_inr"],
                      ["2026-10-06T00:00:15+05:30", 'Quote "me", =now', "10.50", "USD", "934.50"],
                      ["2026-10-05T23:59:59+05:30", "'=cmd|' /C calc'!A0", "100", "INR", "100"]]
    assert "secret" not in text and text.endswith("\r\n")


def test_csv_streams_in_chunks():
    row = {"created_at": _utc(2026, 1, 1), "donor_name": "D", "amount": Decimal(1), "currency": "INR",
           "amount_inr": Decimal(1)}
    chunks = list(csv_export.encode([row] * 1201))
    assert len(chunks) == 3 and sum(c.count("\r\n") for c in chunks) == 1202
    assert list(csv_export.encode([])) == ["created_at,donor_name,amount,currency,amount_inr\r\n"]


# ── G3 moderation state machine (approved plan section 9) ───────────────────────

A, U = "applied", "unchanged"
# (status, action) -> expected step for a paid donation whose message is visible and donor not banned
MATRIX = {
    ("pending", "approve"): Step(A, "approved", True, approve=True, totals_delta=1),
    ("pending", "reject"): Step(A, "rejected", False),
    ("pending", "hide_message"): Step(A, "pending", False),
    ("pending", "unhide_message"): Step(U, "pending", True),
    ("pending", "ban_donor"): Step(A, "rejected", False, ban=True),
    ("approved", "approve"): Step(U, "approved", True),
    ("approved", "reject"): Step(A, "rejected", False, totals_delta=-1),
    ("approved", "hide_message"): Step(A, "approved", False),
    ("approved", "unhide_message"): Step(U, "approved", True),
    ("approved", "ban_donor"): Step(A, "rejected", False, totals_delta=-1, ban=True),
    ("auto_approved", "approve"): Step(U, "auto_approved", True),                      # A5
    ("auto_approved", "reject"): Step(A, "rejected", False, totals_delta=-1),         # legacy forgot this debit
    ("auto_approved", "hide_message"): Step(A, "auto_approved", False),
    ("auto_approved", "unhide_message"): Step(U, "auto_approved", True),
    ("auto_approved", "ban_donor"): Step(A, "rejected", False, totals_delta=-1, ban=True),
    ("rejected", "approve"): Step(A, "approved", True, approve=True, totals_delta=1),
    ("rejected", "reject"): Step(U, "rejected", True),
    ("rejected", "hide_message"): Step(A, "rejected", False),
    ("rejected", "unhide_message"): Step(U, "rejected", True),
    ("rejected", "ban_donor"): Step(A, "rejected", True, ban=True),
}


def _step(status, action, *, visible=True, banned=False, payment="success") -> Step:
    return transition(action, payment_status=payment, moderation_status=status, message_visible=visible,
                      donor_banned=banned)


def test_matrix_covers_every_status_and_action():
    assert set(MATRIX) == {(s, a) for s in STATUSES for a in ACTIONS} and len(MATRIX) == 20
    assert "unban_donor" not in ACTIONS and "replay" not in ACTIONS  # A3, Phase 10


@pytest.mark.parametrize("status,action", sorted(MATRIX))
def test_transition_matrix(status, action):
    assert _step(status, action) == MATRIX[(status, action)]


def test_hidden_messages_and_existing_bans():
    assert _step("pending", "hide_message", visible=False) == Step(U, "pending", False)
    assert _step("approved", "unhide_message", visible=False) == Step(A, "approved", True)
    assert _step("rejected", "approve", visible=False) == Step(A, "approved", False, approve=True, totals_delta=1)
    assert _step("pending", "ban_donor", banned=True) == Step(A, "rejected", False)          # no second ban
    assert _step("approved", "ban_donor", banned=True) == Step(A, "rejected", False, totals_delta=-1)
    assert _step("rejected", "ban_donor", banned=True) == Step(U, "rejected", True)


@pytest.mark.parametrize("payment", ["pending", "failed"])
@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("status", STATUSES)
def test_unpaid_donations_cannot_be_moderated(payment, action, status):
    with pytest.raises(NotPaid):
        _step(status, action, payment=payment)


def test_unknown_actions_and_statuses_are_errors():
    for action in ("unban_donor", "replay", "delete", ""):
        with pytest.raises(ValueError):
            _step("pending", action)
    with pytest.raises(ValueError):
        _step("hidden", "approve")


def _user(username, email="creator@example.test") -> User:
    return User(id="u1", email=email, username=username, role="streamer", streamer_id=None, is_active=True,
                password_hash=None, failed_login_attempts=0, locked_until=None)


def test_actor_label_is_the_username_or_dashboard_never_the_email():
    assert actor_label(_user("CreatorOne")) == "CreatorOne"
    assert actor_label(_user("  Spaced  ")) == "Spaced"
    for empty in (None, "", "   "):
        assert actor_label(_user(empty)) == "Dashboard"


@pytest.mark.parametrize("body", [{"action": "unban_donor"}, {"action": "replay"}, {"action": "APPROVE"}, {},
                                  {"action": "ban_donor", "reason": "x" * 501}, {"action": "ban_donor", "reason": 5},
                                  {"action": "approve", "streamer_id": "x"}, {"action": "approve", "moderator_id": "x"}])
def test_invalid_moderation_requests(body):
    with pytest.raises(ValidationError):
        models.ModerationRequest.model_validate(body)


def test_valid_moderation_requests():
    assert models.ModerationRequest.model_validate({"action": "ban_donor", "reason": "x" * 500}).reason == "x" * 500
    assert models.ModerationRequest.model_validate({"action": "approve"}).reason is None


# ── G4 moderators: compatibility-preserving rules (A7) ──────────────────────────

def _moderator(body: dict) -> tuple:
    m = models.ModeratorCreate.model_validate(body)
    return m.mod_name, m.telegram_user_id, m.discord_user_id


@pytest.mark.parametrize("body,expected", [
    ({"mod_name": "  Mod  ", "telegram_user_id": " 123456789 "}, ("Mod", "123456789", None)),
    ({"mod_name": "Mod", "discord_user_id": "123456789012345678"}, ("Mod", None, "123456789012345678")),
    ({"mod_name": "Mod", "telegram_user_id": "1", "discord_user_id": "2"}, ("Mod", "1", "2")),
    ({"mod_name": "Mod", "telegram_user_id": "", "discord_user_id": "2"}, ("Mod", None, "2")),     # "" = none
    ({"mod_name": "Mod", "telegram_user_id": None, "discord_user_id": " 2 "}, ("Mod", None, "2")),
    # no format is enforced: the legacy product never required one (dev data has such ids)
    ({"mod_name": "Mod", "telegram_user_id": "@handle_name"}, ("Mod", "@handle_name", None)),
    ({"mod_name": "Mod", "telegram_user_id": "-1001234567890"}, ("Mod", "-1001234567890", None)),
    ({"mod_name": "Mod", "discord_user_id": "someone#0001"}, ("Mod", None, "someone#0001")),
    ({"mod_name": "x" * 100, "telegram_user_id": "1" * 64}, ("x" * 100, "1" * 64, None)),
])
def test_valid_moderators(body, expected):
    assert _moderator(body) == expected


@pytest.mark.parametrize("body", [
    {"mod_name": "Mod"}, {"mod_name": "Mod", "telegram_user_id": "", "discord_user_id": "  "},
    {"mod_name": "Mod", "telegram_user_id": None, "discord_user_id": None},
    {"mod_name": "", "telegram_user_id": "1"}, {"mod_name": "   ", "telegram_user_id": "1"},
    {"mod_name": "x" * 101, "telegram_user_id": "1"}, {"mod_name": "Mod", "telegram_user_id": "1" * 65},
    {"mod_name": "Mod", "discord_user_id": "2" * 65}, {"mod_name": None, "telegram_user_id": "1"},
    {"mod_name": "Mod", "telegram_user_id": 123456789}, {"telegram_user_id": "1"},
    {"mod_name": "Mod", "telegram_user_id": "1", "can_ban": True},          # permissions are D-M7 (deferred)
    {"mod_name": "Mod", "telegram_user_id": "1", "role": "owner"},
    {"mod_name": "Mod", "telegram_user_id": "1", "streamer_id": "x"},
])
def test_invalid_moderators(body):
    with pytest.raises(ValidationError):
        models.ModeratorCreate.model_validate(body)


# ── G5 terms ────────────────────────────────────────────────────────────────────

def test_terms_version_is_the_legacy_server_constant():
    assert v.TERMS_VERSION == "2026-07-03"  # legacy src/config/terms.ts CURRENT_TERMS_VERSION
    assert models.TermsAccept.model_validate({"version": "2026-07-03"}).version == "2026-07-03"
    for body in ({}, {"version": None}, {"version": 1}, {"version": "x" * 41}, {"version": "v", "is_admin": True}):
        with pytest.raises(ValidationError):
            models.TermsAccept.model_validate(body)
