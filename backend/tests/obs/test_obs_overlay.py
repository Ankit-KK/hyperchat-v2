"""OBS-token data: GET /api/v1/obs/overlay-config and GET /api/v1/obs/leaderboard (formerly anonymous under
/api/v1/public/streamers/{slug}/...). The creator always comes from the validated token, never from the request."""

import secrets
from datetime import UTC, datetime, timedelta

import pytest

from app.security.crypto import sha256_hex
from tests.integration import db, make_streamer, requires_db, update_streamer

pytestmark = requires_db
CONFIG, BOARD = "/api/v1/obs/overlay-config", "/api/v1/obs/leaderboard"
OVERLAY_FIELDS = {"brand_color", "alert_box_scale", "alert_overlay_id", "leaderboard_overlay_id", "goal_overlay_id",
                  "overlay_adjust_mode", "leaderboard_widget_enabled", "leaderboard_bg_enabled"}
# Dashboard-only or donor-page state that must never come back from the OBS overlay routes either.
NEVER = ("goal_activated_at", "goal_name", "goal_target_amount", "moderation", "razorpay", "user_id", "streamer_id",
         "email", "token", "order", "latest", "tts", "media", "hyperemotes", "el_voice", "status", "pusher_group")


def token_for(sid: str, **cols) -> str:
    token = "obs_" + secrets.token_hex(32)
    cols = {"is_active": True, **cols}
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    with db() as conn:
        conn.execute(f"INSERT INTO public.obs_tokens (streamer_id, token_hash, {names}) VALUES (%s, %s, {marks})",
                     (sid, sha256_hex(token), *cols.values()))
    return token


def totals(sid: str, *rows) -> None:
    with db() as conn:
        for name, amount in rows:
            conn.execute("INSERT INTO public.streamer_donator_totals (streamer_id, donator_name, donator_name_lower, "
                         "total_amount, donation_count) VALUES (%s, %s, %s, %s, 1)", (sid, name, name.lower(), amount))


@pytest.mark.parametrize("path", [CONFIG, BOARD])
def test_a_valid_token_is_required(app, path):
    sid = make_streamer("authtest-ovl-auth")
    good = token_for(sid)
    revoked = token_for(sid, revoked_at=datetime.now(UTC) - timedelta(minutes=1))
    expired = token_for(sid, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    inactive = token_for(sid, is_active=False)
    assert app.get(path).status_code == 401
    for bad in ("obs_" + "0" * 64, "garbage", revoked, expired, inactive):
        r = app.get(path, headers={"x-obs-token": bad})
        assert r.status_code == 401 and set(r.json()) == {"error"}, bad[:8]
    assert app.get(path, headers={"x-obs-token": good, "origin": "https://evil.example"}).status_code == 403
    ok = app.get(path, headers={"x-obs-token": good})
    assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store" and "set-cookie" not in ok.headers
    assert app.get(path, params={"token": good}).status_code == 200  # the browser-source ?token= form
    assert app.post(path, headers={"x-obs-token": good}).status_code == 405


def test_overlay_config_is_exactly_the_settings_channel_state_of_the_tokens_creator(app):
    a = make_streamer("authtest-ovl-a")
    b = make_streamer("authtest-ovl-b")
    update_streamer(a, brand_color="#112233", alert_box_scale=1.25, alert_overlay_id=2, leaderboard_overlay_id=4,
                    goal_overlay_id=3, overlay_adjust_mode=True, leaderboard_widget_enabled=False,
                    leaderboard_bg_enabled=False, goal_name="Secret goal", goal_target_amount=5000,
                    goal_is_active=True, goal_activated_at=datetime(2026, 9, 1, tzinfo=UTC), el_voice_id="authtest-v")
    update_streamer(b, brand_color="#445566", alert_overlay_id=7)
    r = app.get(CONFIG, headers={"x-obs-token": token_for(a)})
    body = r.json()
    assert set(body) == OVERLAY_FIELDS
    assert body == {"brand_color": "#112233", "alert_box_scale": 1.25, "alert_overlay_id": 2,
                    "leaderboard_overlay_id": 4, "goal_overlay_id": 3, "overlay_adjust_mode": True,
                    "leaderboard_widget_enabled": False, "leaderboard_bg_enabled": False}
    for word in NEVER + ("Secret goal", "authtest-v", "2026-09-01"):
        assert word not in r.text, word
    # Creator A's token reads A whatever the request names; B's token reads B.
    assert app.get(CONFIG, headers={"x-obs-token": token_for(a)}, params={"streamer": "authtest-ovl-b",
                                                                         "slug": "authtest-ovl-b"}).json() == body
    assert app.get(CONFIG, headers={"x-obs-token": token_for(b)}).json()["alert_overlay_id"] == 7


def test_leaderboard_is_the_tokens_creator_only(app):
    on = make_streamer("authtest-lb-on")
    totals(on, ("Small", 100), ("Big Fan", 2500.5), ("Mid", 900))
    other = make_streamer("authtest-lb-other")
    totals(other, ("Someone Else", 99999))
    r = app.get(BOARD, headers={"x-obs-token": token_for(on)})
    assert r.json() == {"enabled": True, "top_donor": {"name": "Big Fan", "total_amount_inr": 2500.5}}
    assert "Someone Else" not in r.text and "Small" not in r.text and "latest" not in r.text
    off = make_streamer("authtest-lb-off")
    update_streamer(off, leaderboard_widget_enabled=False)
    totals(off, ("Hidden", 9999))
    assert app.get(BOARD, headers={"x-obs-token": token_for(off)}).json() == {"enabled": False, "top_donor": None}
    empty = make_streamer("authtest-lb-empty")
    totals(empty, ("Nobody", 0))
    assert app.get(BOARD, headers={"x-obs-token": token_for(empty)}).json() == {"enabled": True, "top_donor": None}
    tie = make_streamer("authtest-lb-tie")
    totals(tie, ("Zed", 500), ("Amy", 500))
    assert app.get(BOARD, headers={"x-obs-token": token_for(tie)}).json()["top_donor"]["name"] == "Amy"


def test_a_token_scoped_away_from_a_channel_cannot_read_its_state(app):
    sid = make_streamer("authtest-ovl-scope")
    audio_only = token_for(sid, pusher_channel_scope=["private-authtest-ovl-scope-audio"])
    assert app.get(CONFIG, headers={"x-obs-token": audio_only}).status_code == 403
    assert app.get(BOARD, headers={"x-obs-token": audio_only}).status_code == 403
    settings_only = token_for(sid, pusher_channel_scope=["private-authtest-ovl-scope-settings"])
    assert app.get(CONFIG, headers={"x-obs-token": settings_only}).status_code == 200
    assert app.get(BOARD, headers={"x-obs-token": settings_only}).status_code == 403


def test_reads_are_rate_limited_per_token(app):
    hdr = {"x-obs-token": token_for(make_streamer("authtest-ovl-rate"))}
    codes = [app.get(CONFIG if i % 2 else BOARD, headers=hdr).status_code for i in range(62)]
    assert codes[:60] == [200] * 60 and codes[60:] == [429, 429]
