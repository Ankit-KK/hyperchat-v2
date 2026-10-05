"""Phase 5 pure logic and HTTP plumbing (no database)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.dashboard import models
from app.dashboard import validation as v
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
