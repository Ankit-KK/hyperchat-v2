"""G1 creator settings (D-R1 GET, D-M1 PATCH) through the real app against a scratch database."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.cli import connection_factory
from app.core.config import Settings
from app.dashboard.repository import DashboardRepository
from app.dashboard.validation import WRITABLE_SETTINGS
from app.realtime import outbox
from tests.integration import (
    DOMAIN,
    ORIGIN,
    csrf_headers,
    db,
    login,
    make_donation,
    make_streamer,
    make_user,
    new_browser,
    owned_creator,
    requires_db,
)

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
    "goal_name": "  New mic  ", "goal_target_amount": 2500.5,
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
    assert body["changed"] == [*WRITABLE_SETTINGS, "goal_activated_at"]  # the activation was stamped (D-G)
    s = body["settings"]
    assert s["goal_name"] == "New mic" and s["tts_language_boost"] == "English" and s["el_voice_id"] == "Voice123456"
    assert _db(sid, "goal_activated_at IS NOT NULL") == (True,)
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
    app.patch(url, json={"el_voice_id": "Voice123456"}, headers=headers)
    r = app.patch(url, json={"goal_overlay_id": 3}, headers=headers)
    assert r.json()["changed"] == ["goal_overlay_id"]
    expected = {**DEFAULTS, "slug": "authtest-partial", "el_voice_id": "Voice123456", "goal_overlay_id": 3}
    assert r.json()["settings"] == expected  # everything not sent is unchanged
    assert app.patch(url, json={"el_voice_id": ""}, headers=headers).json()["changed"] == ["el_voice_id"]
    assert app.patch(url, json={"el_voice_id": None}, headers=headers).json()["changed"] == []
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
    ({"goal_activated_at": "2020-01-01T00:00:00Z"}, 422),  # server-owned (D-G), in any form
    ({"goal_activated_at": None}, 422),
    ({"goal_activated_at": "2020-01-01T00:00:00Z", "goal_is_active": True}, 422),
    ({"goal_is_active": True, "goal_activated_at": 0}, 422),
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


# ── D-G: goal_activated_at is server-owned ──────────────────────────────────────

GOAL = "/api/v1/public/streamers/{}/goal"


def _activated(sid: str) -> datetime | None:
    return _db(sid, "goal_activated_at")[0]


def _goal_events(sid: str) -> int:
    with db() as conn:
        return conn.execute("SELECT count(*) FROM public.realtime_outbox WHERE streamer_id = %s "
                            "AND event = 'goal-progress'", (sid,)).fetchone()[0]


def _db_now() -> datetime:
    with db() as conn:
        return conn.execute("SELECT now()").fetchone()[0]


def test_activation_is_stamped_by_the_server_and_progress_starts_there(app):
    sid, headers = owned_creator(app, "authtest-dg", goal_name="Mic", goal_target_amount=1000)
    make_donation(sid, 500, moderation="auto_approved", at=datetime.now(UTC) - timedelta(days=3))  # before
    before = _db_now()
    r = app.patch(f"{S}/authtest-dg/settings", json={"goal_is_active": True}, headers=headers)
    assert r.status_code == 200 and r.json()["changed"] == ["goal_is_active", "goal_activated_at"], r.text
    stamped = _activated(sid)
    assert stamped.tzinfo is not None and before <= stamped <= _db_now()
    assert datetime.fromisoformat(r.json()["settings"]["goal_activated_at"]) == stamped
    assert app.get(GOAL.format("authtest-dg")).json()["progress_inr"] == 0.0  # the old donation never counts
    make_donation(sid, 120, moderation="auto_approved")
    assert app.get(GOAL.format("authtest-dg")).json()["progress_inr"] == 120.0
    assert _goal_events(sid) == 1


def test_other_edits_and_repeated_activation_keep_the_stamp(app):
    stamped = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)
    sid, headers = owned_creator(app, "authtest-dg-keep", goal_is_active=True, goal_activated_at=stamped,
                                 goal_name="Mic", goal_target_amount=1000)
    url = f"{S}/authtest-dg-keep/settings"
    for body in ({"goal_name": "Chair"}, {"goal_target_amount": 5000}, {"brand_color": "#000000"},
                 {"goal_is_active": True}, {"goal_is_active": True, "goal_name": "Desk"}, {"tts_enabled": False}):
        r = app.patch(url, json=body, headers=headers)
        assert r.status_code == 200 and "goal_activated_at" not in r.json()["changed"], (body, r.text)
        assert _activated(sid) == stamped, body


def test_unrelated_edit_emits_no_goal_event(app):
    sid, headers = owned_creator(app, "authtest-dg-quiet", goal_is_active=True, goal_name="Mic",
                                 goal_target_amount=10, goal_activated_at=datetime(2026, 10, 2, tzinfo=UTC))
    app.patch(f"{S}/authtest-dg-quiet/settings", json={"brand_color": "#000000", "tts_enabled": False},
              headers=headers)
    assert _goal_events(sid) == 0


def test_deactivation_keeps_the_stamp_and_reactivation_restamps(app):
    old = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)
    sid, headers = owned_creator(app, "authtest-dg-cycle", goal_is_active=True, goal_activated_at=old,
                                 goal_name="Mic", goal_target_amount=1000)
    url = f"{S}/authtest-dg-cycle/settings"
    off = app.patch(url, json={"goal_is_active": False}, headers=headers)
    assert off.json()["changed"] == ["goal_is_active"] and _activated(sid) == old
    assert app.get(GOAL.format("authtest-dg-cycle")).json()["active"] is False
    on = app.patch(url, json={"goal_is_active": True}, headers=headers)
    assert on.json()["changed"] == ["goal_is_active", "goal_activated_at"] and _activated(sid) > old


def test_imported_active_goal_without_a_stamp_is_stamped_on_activation(app):
    sid, headers = owned_creator(app, "authtest-dg-import", goal_is_active=True, goal_activated_at=None,
                                 goal_name="Mic", goal_target_amount=1000)
    assert app.get(GOAL.format("authtest-dg-import")).json()["active"] is False  # unchanged legacy behaviour
    assert app.patch(f"{S}/authtest-dg-import/settings", json={"goal_name": "Chair"},
                     headers=headers).json()["changed"] == ["goal_name"]
    assert _activated(sid) is None  # only an activation stamps
    r = app.patch(f"{S}/authtest-dg-import/settings", json={"goal_is_active": True}, headers=headers)
    assert r.json()["changed"] == ["goal_activated_at"] and _activated(sid) is not None
    assert app.get(GOAL.format("authtest-dg-import")).json()["active"] is True


def test_a_null_goal_is_active_counts_as_inactive(app):
    sid, headers = owned_creator(app, "authtest-dg-null", goal_is_active=None, goal_activated_at=None)
    r = app.patch(f"{S}/authtest-dg-null/settings", json={"goal_is_active": True}, headers=headers)
    assert r.json()["changed"] == ["goal_is_active", "goal_activated_at"] and _activated(sid) is not None


def test_linked_account_gets_the_same_rule_and_strangers_change_nothing(app):
    sid = make_streamer("authtest-dg-auth", user_id=make_user("owner" + DOMAIN))
    make_user("linked" + DOMAIN, streamer_id=sid)
    make_user("stranger" + DOMAIN)
    url = f"{S}/authtest-dg-auth/settings"
    stranger = new_browser(app)
    s_headers = csrf_headers(login(stranger, "stranger" + DOMAIN))
    assert stranger.patch(url, json={"goal_is_active": True}, headers=s_headers).status_code == 403
    assert _db(sid, "COALESCE(goal_is_active, false)", "goal_activated_at") == (False, None)
    linked = new_browser(app)
    l_headers = csrf_headers(login(linked, "linked" + DOMAIN))
    assert linked.patch(url, json={"goal_is_active": True, "goal_activated_at": "2020-01-01T00:00:00Z"},
                        headers=l_headers).status_code == 422
    assert _activated(sid) is None
    assert linked.patch(url, json={"goal_is_active": True}, headers=l_headers).status_code == 200
    assert _activated(sid) is not None


def test_concurrent_activations_stamp_once(env, monkeypatch):
    sid = make_streamer("authtest-dg-race", user_id=make_user("owner" + DOMAIN))
    repo = DashboardRepository(connection_factory(Settings()))
    original = outbox.emit_settings_changed

    async def slow(*args, **kwargs):  # holds the row lock inside the transaction, so the calls overlap
        await original(*args, **kwargs)
        await asyncio.sleep(0.4)

    monkeypatch.setattr(outbox, "emit_settings_changed", slow)

    async def race():
        return await asyncio.gather(*(repo.update_settings(sid, {"goal_is_active": True}) for _ in range(2)))

    results = asyncio.run(race())
    assert sorted(changed for _, changed in results) == [[], ["goal_is_active", "goal_activated_at"]]
    assert results[0][0]["goal_activated_at"] == results[1][0]["goal_activated_at"] == _activated(sid)
