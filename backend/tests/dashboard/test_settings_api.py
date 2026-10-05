"""G1 creator settings (D-R1 GET, D-M1 PATCH) through the real app against a scratch database."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.dashboard.validation import WRITABLE_SETTINGS
from tests.integration import DOMAIN, ORIGIN, db, owned_creator, requires_db

pytestmark = requires_db
S = "/api/v1/dashboard/streamers"

DEFAULTS = {  # a new creator row, as served (nullable booleans get their column default)
    "slug": "authtest-set", "name": "Auth Test", "status": "active", "moderation_mode": "auto_approve",
    "telegram_moderation_enabled": True, "discord_moderation_enabled": False, "media_moderation_enabled": True,
    "tts_enabled": True, "tts_language_boost": "Hindi", "el_voice_id": None, "hyperemotes_enabled": True,
    "hyperemotes_min_amount": 1.0, "media_upload_enabled": False, "media_min_amount": 100.0,
    "brand_color": "#3b82f6", "alert_box_scale": 1.0, "alert_overlay_id": 0, "leaderboard_overlay_id": 0,
    "goal_overlay_id": 0, "overlay_adjust_mode": False, "leaderboard_bg_enabled": True,
    "leaderboard_widget_enabled": True, "goal_name": None, "goal_target_amount": None, "goal_is_active": False,
    "goal_activated_at": None,
}
EVERYTHING = {
    "moderation_mode": "manual", "telegram_moderation_enabled": False, "discord_moderation_enabled": True,
    "tts_enabled": False, "hyperemotes_enabled": False, "alert_box_scale": 1.25, "goal_is_active": True,
    "goal_name": "  New mic  ", "goal_target_amount": 2500.5, "goal_activated_at": "2026-10-01T10:00:00+05:30",
    "media_upload_enabled": True, "media_moderation_enabled": False, "tts_language_boost": "english",
    "alert_overlay_id": 16, "leaderboard_overlay_id": 15, "overlay_adjust_mode": True, "leaderboard_bg_enabled": False,
    "goal_overlay_id": 6, "brand_color": "#ABCDEF", "leaderboard_widget_enabled": False, "el_voice_id": "Voice123456",
}


def _db(sid: str, *cols: str) -> tuple:
    with db() as conn:
        return conn.execute(f"SELECT {', '.join(cols)} FROM public.streamers WHERE id = %s", (sid,)).fetchone()


def test_read_settings_fields_defaults_and_refs(app):
    sid, _ = owned_creator(app, "authtest-set")
    r = app.get(f"{S}/authtest-set/settings")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == DEFAULTS
    assert app.get(f"{S}/{sid}/settings").json() == DEFAULTS            # by id
    assert app.get(f"{S}/AUTHTEST-SET/settings").json() == DEFAULTS     # slugs are case-insensitive


def test_read_settings_serves_stored_values_and_null_defaults(app):
    sid, _ = owned_creator(app, "authtest-set2", tts_enabled=None, hyperemotes_enabled=None, moderation_mode=None,
                           goal_is_active=None, tts_language_boost="hindi", hyperemotes_min_amount=50,
                           media_min_amount=20, status="pending", rejection_reason="authtest-hidden")
    body = app.get(f"{S}/authtest-set2/settings").json()
    assert body["tts_enabled"] is True and body["hyperemotes_enabled"] is True and body["goal_is_active"] is False
    assert body["moderation_mode"] == "auto_approve"
    assert body["tts_language_boost"] == "hindi"  # legacy lowercase row is served as stored (A2: no migration)
    assert (body["hyperemotes_min_amount"], body["media_min_amount"]) == (50.0, 20.0)
    assert body["status"] == "pending" and "rejection_reason" not in body and "authtest-hidden" not in str(body)


def test_patch_every_writable_field_then_idempotent(app):
    sid, headers = owned_creator(app, "authtest-patch")
    url = f"{S}/authtest-patch/settings"
    r = app.patch(url, json=EVERYTHING, headers=headers)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store", r.text
    body = r.json()
    assert body["changed"] == list(WRITABLE_SETTINGS)
    s = body["settings"]
    assert s["goal_name"] == "New mic" and s["tts_language_boost"] == "English" and s["el_voice_id"] == "Voice123456"
    assert datetime.fromisoformat(s["goal_activated_at"]) == datetime(2026, 10, 1, 4, 30, tzinfo=UTC)
    assert (s["alert_overlay_id"], s["leaderboard_overlay_id"], s["goal_overlay_id"]) == (16, 15, 6)
    assert s["alert_box_scale"] == 1.25 and s["goal_target_amount"] == 2500.5 and s["moderation_mode"] == "manual"
    assert app.get(url).json() == s
    assert _db(sid, "goal_name", "tts_language_boost", "alert_box_scale", "goal_target_amount") == (
        "New mic", "English", Decimal("1.25"), Decimal("2500.5"))

    stamp = _db(sid, "updated_at")
    again = app.patch(url, json=EVERYTHING, headers=headers)
    assert again.status_code == 200 and again.json() == {"settings": s, "changed": []}
    assert _db(sid, "updated_at") == stamp  # nothing differed: no UPDATE ran


def test_patch_is_partial_and_clears_nullable_fields(app):
    sid, headers = owned_creator(app, "authtest-partial")
    url = f"{S}/authtest-partial/settings"
    app.patch(url, json={"el_voice_id": "Voice123456", "goal_activated_at": "2026-10-01T00:00:00Z"}, headers=headers)
    r = app.patch(url, json={"goal_overlay_id": 3}, headers=headers)
    assert r.json()["changed"] == ["goal_overlay_id"]
    settings = r.json()["settings"]
    assert datetime.fromisoformat(settings.pop("goal_activated_at")) == datetime(2026, 10, 1, tzinfo=UTC)
    expected = {**DEFAULTS, "slug": "authtest-partial", "el_voice_id": "Voice123456", "goal_overlay_id": 3}
    del expected["goal_activated_at"]
    assert settings == expected  # everything not sent is unchanged
    assert app.patch(url, json={"el_voice_id": ""}, headers=headers).json()["changed"] == ["el_voice_id"]
    assert app.patch(url, json={"el_voice_id": None}, headers=headers).json()["changed"] == []
    assert app.patch(url, json={"goal_activated_at": None}, headers=headers).json()["changed"] == ["goal_activated_at"]
    assert _db(sid, "el_voice_id", "goal_activated_at") == (None, None)


def test_lowercase_language_is_canonicalised_only_when_saved(app):
    sid, headers = owned_creator(app, "authtest-lang", tts_language_boost="hindi")
    r = app.patch(f"{S}/authtest-lang/settings", json={"tts_language_boost": "HINDI"}, headers=headers)
    assert r.json()["changed"] == ["tts_language_boost"] and _db(sid, "tts_language_boost") == ("Hindi",)


@pytest.mark.parametrize("body,status", [
    ({}, 400),
    ({"leaderboard_overlay_id": 16}, 422),        # database trigger limit (A8)
    ({"alert_overlay_id": 17}, 422),
    ({"tts_enabled": "true"}, 422),
    ({"tts_enabled": None}, 422),
    ({"brand_color": "javascript:alert(1)"}, 422),
    ({"goal_target_amount": 0}, 422),
    ({"tts_language_boost": "Klingon"}, 422),
    ({"media_min_amount": 10}, 422),              # read-only (A1)
    ({"hyperemotes_min_amount": 10}, 422),
    ({"status": "active"}, 422),
    ({"user_id": "00000000-0000-4000-8000-000000000000"}, 422),
    ({"goal_overlay_id": 2, "unknown": 1}, 422),  # one bad field rejects the whole patch
])
def test_patch_rejections_change_nothing(app, body, status):
    sid, headers = owned_creator(app, "authtest-bad")
    before = _db(sid, "updated_at", "goal_overlay_id")
    r = app.patch(f"{S}/authtest-bad/settings", json=body, headers=headers)
    assert r.status_code == status and r.headers["cache-control"] == "no-store", r.text
    if status == 400:
        assert r.json() == {"error": "No settings to update"}
    assert _db(sid, "updated_at", "goal_overlay_id") == before


def test_patch_needs_csrf_and_an_allowed_origin(app):
    sid, headers = owned_creator(app, "authtest-csrf")
    url = f"{S}/authtest-csrf/settings"
    for bad in (ORIGIN, {**headers, "x-csrf-token": "wrong"}, {**headers, "origin": "https://evil.invalid"},
                {"x-csrf-token": headers["x-csrf-token"]}):
        r = app.patch(url, json={"goal_overlay_id": 4}, headers=bad)
        assert r.status_code == 403 and r.headers["cache-control"] == "no-store"
    assert _db(sid, "goal_overlay_id") == (0,)
    assert app.get(url).status_code == 200  # reads need no CSRF token


def test_changes_are_audited_without_secrets(app):
    sid, headers = owned_creator(app, "authtest-audit")
    app.patch(f"{S}/authtest-audit/settings", json={"brand_color": "#000000", "tts_enabled": False}, headers=headers)
    records = app.app_state.log_capture.records
    assert any("streamer_settings_updated" in r and sid in r and "tts_enabled,brand_color" in r for r in records)
    assert all(app.cookies.get("__Host-hc_session") not in r and DOMAIN not in r for r in records)
