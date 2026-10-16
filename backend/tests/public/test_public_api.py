"""Phase 4 public read API end to end (real app, scratch database; see tests/integration.py).

Global tables (platform soundboard/voice tiers, metrics over all donations) are only touched under
strict_scratch_db, never on hyperchat_dev.
"""

import secrets
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.integration import (
    DOMAIN,
    FRONT,
    accept_soundboard_terms,
    db,
    fixture_db,
    make_streamer,
    make_user,
    requires_db,
    signed_in,
    update_streamer,
)

pytestmark = requires_db
P = "/api/v1/public/streamers"
NOT_FOUND = {"error": "Streamer not found"}
E1_FIELDS = {"slug", "name", "brand_color", "brand_logo_url", "background_url", "tts_enabled", "hyperemotes_enabled",
             "media_upload_enabled", "media_max_file_size_mb", "soundboard_enabled"}
GOAL_FIELDS = {"active", "name", "target_amount_inr", "progress_inr", "brand_color", "goal_overlay_id"}
INACTIVE_GOAL = {"active": False, "name": None, "target_amount_inr": None, "progress_inr": None, "brand_color": None, "goal_overlay_id": None}
# OBS-token or dashboard-session data: never in any anonymous response (key or value).
NOT_PUBLIC_KEYS = {"alert_box_scale", "alert_overlay_id", "leaderboard_overlay_id", "overlay_adjust_mode",
                   "leaderboard_bg_enabled", "leaderboard_widget_enabled", "goal_activated_at", "goal_is_active",
                   "top_donor", "total_amount_inr", "user_id", "streamer_id", "owner_id", "razorpay_account",
                   "razorpay_key_ref", "order_prefix", "pusher_group", "obs_token", "token_hash", "status",
                   "moderation_mode", "el_voice_id"}


def streamer(slug: str, user_id: str | None = None, terms: bool = True, **cols) -> str:
    """soundboard_enabled=True also records the owner's acceptance of the current terms (D3) unless terms=False."""
    sid = make_streamer(slug, user_id=user_id, name=cols.pop("name", "Auth Test"))
    if cols:
        update_streamer(sid, **cols)  # fixture setup, as the schema owner
    if cols.get("soundboard_enabled") and terms:
        accept_soundboard_terms(sid)
    return sid


def donation(sid: str, amount_inr, *, payment="success", moderation="auto_approved", at=None, currency="INR",
             amount=None, name="Donor", message=None) -> None:
    with fixture_db() as conn:
        conn.execute(
            "INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, amount_inr, "
            "payment_status, moderation_status, message, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (sid, "authtest-" + secrets.token_hex(6), name, amount if amount is not None else amount_inr, currency,
             amount_inr, payment, moderation, message, at or datetime.now(UTC)))


def totals(sid: str, *rows) -> None:
    with db() as conn:
        for name, amount in rows:
            conn.execute("INSERT INTO public.streamer_donator_totals (streamer_id, donator_name, donator_name_lower, "
                         "total_amount, donation_count) VALUES (%s, %s, %s, %s, 1)", (sid, name, name.lower(), amount))


# ── E1 profile ──────────────────────────────────────────────────────────────────

def test_profile_exact_fields_values_and_cache(app):
    streamer("authtest-prof", name="Prof Creator", brand_color="#112233", background_url="https://cdn.example.test/bg.png",
             media_max_file_size_mb=25, soundboard_enabled=True, leaderboard_widget_enabled=False)
    r = app.get(f"{P}/authtest-prof")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=60"
    body = r.json()
    assert set(body) == E1_FIELDS and "leaderboard_widget_enabled" not in body  # OBS-only (owner decision)
    assert body == {"slug": "authtest-prof", "name": "Prof Creator", "brand_color": "#112233", "brand_logo_url": None,
                    "background_url": "https://cdn.example.test/bg.png", "tts_enabled": True,
                    "hyperemotes_enabled": True, "media_upload_enabled": False, "media_max_file_size_mb": 25.0,
                    "soundboard_enabled": True}
    assert app.get(f"{P}/AUTHTEST-PROF").json()["slug"] == "authtest-prof"  # slugs are case-insensitive


def test_null_database_values_are_served_safely(app):
    streamer("authtest-nulls", brand_color=None, tts_enabled=None, hyperemotes_enabled=None, media_upload_enabled=None,
             media_max_file_size_mb=None, leaderboard_widget_enabled=None, alert_box_scale=None, goal_is_active=None)
    body = app.get(f"{P}/authtest-nulls").json()
    assert body["brand_color"] is None and body["tts_enabled"] is True and body["media_upload_enabled"] is False
    assert body["media_max_file_size_mb"] is None
    assert app.get(f"{P}/authtest-nulls/goal").json() == INACTIVE_GOAL


# ── 404 behaviour (D3) ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("suffix", ["", "/pricing", "/soundboard", "/goal"])
def test_unknown_inactive_and_malformed_slugs_are_identical_404s(app, suffix):
    streamer("authtest-pending", status="pending", soundboard_enabled=True)
    streamer("authtest-rejected", status="rejected", rejection_reason="nope", soundboard_enabled=True)
    for slug in ("authtest-nope", "authtest-pending", "authtest-rejected", "Bad%20Slug", "-x", "x" * 70, "a.b"):
        r = app.get(f"{P}/{slug}{suffix}")
        assert (r.status_code, r.json()) == (404, NOT_FOUND), (slug, suffix)
        assert "cache-control" not in r.headers


# ── E2 pricing (D4) ─────────────────────────────────────────────────────────────

def test_pricing_inr_default_creator_tiers_and_char_tiers(app):
    sid = streamer("authtest-price", min_tts_amount_inr=75, min_voice_amount_inr=150, media_min_amount=100,
                   message_char_tiers='[{"min_amount": 50, "max_chars": 100}, {"min_amount": 151, "max_chars": 200}]')
    with db() as conn:
        conn.execute("INSERT INTO public.streamer_voice_tiers (streamer_id, min_amount_inr, max_duration_seconds, "
                     "is_active) VALUES (%s, 0, 10, true), (%s, 200, 20, true), (%s, 500, 60, false)", (sid, sid, sid))
    r = app.get(f"{P}/authtest-price/pricing")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=60"
    assert r.json() == {"currency": "INR", "min_text": 40.0, "min_tts": 80.0, "min_voice": 150.0,
                        "min_hypersound": 30.0, "min_media": 100.0, "tts_enabled": True,
                        "message_char_tiers": [{"min_amount": 50.0, "max_chars": 100},
                                               {"min_amount": 160.0, "max_chars": 200}],
                        "voice_duration_tiers": [{"min_amount": 0.0, "max_duration_seconds": 10},
                                                 {"min_amount": 200.0, "max_duration_seconds": 20}]}


@pytest.mark.parametrize("currency", ["INR", "usd", "EUR", "GBP", "AED", "AUD", "CAD", "PHP"])
def test_pricing_supported_currencies(app, currency):
    streamer("authtest-cur")
    r = app.get(f"{P}/authtest-cur/pricing", params={"currency": currency})
    assert r.status_code == 200 and r.json()["currency"] == currency.upper()


@pytest.mark.parametrize("currency", ["JPY", "XXX", "US", "eur1", "1"])
def test_pricing_unsupported_currencies_rejected(app, currency):
    streamer("authtest-cur2")
    r = app.get(f"{P}/authtest-cur2/pricing", params={"currency": currency})
    assert (r.status_code, r.json()) == (400, {"error": "Unsupported currency"})
    assert app.get(f"{P}/authtest-cur2/pricing", params={"currency": "x" * 9}).status_code == 422


def test_pricing_platform_tiers_then_builtin_fallback(strict_scratch_db, app):
    streamer("authtest-fallback")
    fallback = app.get(f"{P}/authtest-fallback/pricing").json()["voice_duration_tiers"]
    with db() as conn:
        existing = conn.execute("SELECT count(*) FROM public.platform_voice_tiers WHERE is_active").fetchone()[0]
    if existing == 0:
        assert fallback == [{"min_amount": 0.0, "max_duration_seconds": 8},
                            {"min_amount": 150.0, "max_duration_seconds": 12},
                            {"min_amount": 300.0, "max_duration_seconds": 15}]
    with db() as conn:
        ids = [r[0] for r in conn.execute(
            "INSERT INTO public.platform_voice_tiers (min_amount_inr, max_duration_seconds) VALUES "
            "(987001, 31), (987002, 32) RETURNING id")]
    try:
        tiers = app.get(f"{P}/authtest-fallback/pricing").json()["voice_duration_tiers"]
        assert {"min_amount": 987010.0, "max_duration_seconds": 31} in tiers  # INR rounding to next 10
    finally:
        with db() as conn:
            conn.execute("DELETE FROM public.platform_voice_tiers WHERE id = ANY(%s)", (ids,))


# ── removed: E3 overlay config and E5 leaderboard are OBS-token data now (tests/obs/test_obs_overlay.py) ──

@pytest.mark.parametrize("suffix", ["/overlay-config", "/leaderboard"])
def test_overlay_config_and_leaderboard_are_not_anonymous_any_more(app, suffix):
    sid = streamer("authtest-gone", alert_overlay_id=2, alert_box_scale=1.25, overlay_adjust_mode=True)
    totals(sid, ("Big Fan", 2500.5))
    for headers in ({}, {"origin": FRONT}, {"cookie": "__Host-hc_session=garbage"}):
        r = app.get(f"{P}/authtest-gone{suffix}", headers=headers)
        assert r.status_code == 404, suffix
        assert "Big Fan" not in r.text and "alert" not in r.text and "1.25" not in r.text


# ── E4 soundboard ───────────────────────────────────────────────────────────────

def _slot(conn, slug, **cols):
    cols = {"source": "custom", "is_visible": True, **cols}
    keys = ", ".join(cols)
    vals = ", ".join(f"%({k})s" for k in cols)
    return conn.execute(f"INSERT INTO public.streamer_soundboard_slots (streamer_slug, {keys}) "
                        f"VALUES (%(slug)s, {vals}) RETURNING id::text", {**cols, "slug": slug}).fetchone()[0]


def _custom(conn, slug, n, visible=True, order=None):
    return _slot(conn, slug, display_name=f"Custom {n}", audio_key=f"authtest-a{n}",
                 audio_url=f"https://cdn.example.test/c{n}.mp3", audio_duration_s=2, image_key=f"authtest-i{n}",
                 image_url=f"https://cdn.example.test/c{n}.gif", image_type="gif", is_visible=visible,
                 order_index=order, swappable_index=n)


def test_soundboard_disabled_and_custom_sounds(app):
    streamer("authtest-sb-off")
    r = app.get(f"{P}/authtest-sb-off/soundboard")
    assert (r.status_code, r.json()) == (404, {"error": "Soundboard not enabled"})
    streamer("authtest-sb", soundboard_enabled=True)
    with db() as conn:
        a = _custom(conn, "authtest-sb", 1, order=4)
        _custom(conn, "authtest-sb", 2, visible=False)
        c = _custom(conn, "authtest-sb", 3, order=1)
    r = app.get(f"{P}/authtest-sb/soundboard")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=60"
    sounds = [s for s in r.json()["sounds"] if s["id"] in (a, c) or s["display_name"].startswith("Custom")]
    assert [s["id"] for s in sounds] == [c, a]
    assert sounds[0] == {"id": c, "display_name": "Custom 3", "audio_url": "https://cdn.example.test/c3.mp3",
                         "thumb_url": "https://cdn.example.test/c3.gif"}


def test_soundboard_platform_defaults_and_overrides(strict_scratch_db, app):
    streamer("authtest-sbp", soundboard_enabled=True)
    with db() as conn:
        p = [conn.execute(
            "INSERT INTO public.platform_soundboard_slots (display_name, audio_key, audio_url, audio_duration_s, "
            "image_key, image_url, image_type, default_order_index) VALUES (%s, %s, %s, 1, %s, %s, 'png', %s) "
            "RETURNING id::text", (f"Plat {i}", f"authtest-p{i}", f"https://cdn.example.test/p{i}.mp3",
                                   f"authtest-pi{i}", f"https://cdn.example.test/p{i}.png", 40 + i)).fetchone()[0]
            for i in range(3)]
        _slot(conn, "authtest-sbp", source="platform", platform_slot_id=p[1], is_visible=False)
        o = _slot(conn, "authtest-sbp", source="platform", platform_slot_id=p[2], is_visible=True, order_index=0,
                  display_name="Renamed")
    sounds = app.get(f"{P}/authtest-sbp/soundboard").json()["sounds"]
    mine = [s for s in sounds if s["audio_url"].startswith("https://cdn.example.test/p")]
    assert [(s["id"], s["display_name"]) for s in mine] == [(o, "Renamed"), (p[0], "Plat 0")]


# ── E6 goal (D4: amount_inr) ────────────────────────────────────────────────────

def test_goal_progress_boundaries(app):
    activated = datetime.now(UTC) - timedelta(days=2)
    sid = streamer("authtest-goal", goal_name="Chair", goal_target_amount=10000, goal_activated_at=activated,
                   goal_is_active=True, brand_color="#abcdef", goal_overlay_id=3, alert_overlay_id=5,
                   alert_box_scale=1.5, overlay_adjust_mode=True)
    other = streamer("authtest-goal-other")
    donation(sid, 100, at=activated)                                    # exactly at activation: counted
    donation(sid, 200, moderation="approved")                           # counted
    donation(sid, 890, currency="USD", amount=10)                       # counted as stored amount_inr
    donation(sid, 5000, at=activated - timedelta(seconds=1))            # before activation
    donation(sid, 7, payment="pending")
    donation(sid, 11, payment="failed")
    donation(sid, 13, moderation="rejected")
    donation(sid, 17, moderation="pending")
    donation(other, 999)                                                # another creator
    r = app.get(f"{P}/authtest-goal/goal")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=10"
    assert set(r.json()) == GOAL_FIELDS
    assert r.json() == {"active": True, "name": "Chair", "target_amount_inr": 10000.0, "progress_inr": 1190.0,
                        "brand_color": "#abcdef", "goal_overlay_id": 3}  # what the tokenless goal overlay renders
    assert "goal_activated_at" not in r.text and "alert" not in r.text and "1.5" not in r.text
    with db() as conn:
        conn.execute("UPDATE public.streamers SET goal_is_active = false WHERE id = %s", (sid,))
    stale = app.get(f"{P}/authtest-goal/goal")
    assert stale.json() == INACTIVE_GOAL and "Chair" not in stale.text and "10000" not in stale.text


def test_goal_active_without_activation_time_is_inactive(app):
    streamer("authtest-goal2", goal_is_active=True, goal_activated_at=None, goal_name="X")
    assert app.get(f"{P}/authtest-goal2/goal").json() == INACTIVE_GOAL


# ── E7 / E8 metrics (D7) ────────────────────────────────────────────────────────

def _metrics_client(env):
    from app.main import create_app

    return TestClient(create_app(), base_url="https://testserver")


def test_metrics_aggregates_corrected_24h_and_cache(strict_scratch_db, env):
    a = streamer("authtest-m-a")
    b = streamer("authtest-m-b")
    streamer("authtest-m-pending", status="pending")
    streamer("authtest-m-rejected", status="rejected")
    streamer("authtest-m-idle")
    old = datetime.now(UTC) - timedelta(hours=25)
    donation(a, 10)                                   # success + approved, recent
    donation(a, 20, moderation="approved", at=old)    # success + approved, older than 24 h
    donation(a, 30, moderation="pending")             # success, not approved
    donation(b, 40)                                   # success + approved, recent
    donation(b, 50, payment="pending")
    donation(b, 60, payment="failed")
    with db() as conn:
        other_streamers = conn.execute("SELECT count(*) FROM public.streamers WHERE status = 'active' "
                                       "AND streamer_slug NOT LIKE 'authtest-%'").fetchone()[0]
        other_donations = conn.execute("SELECT count(*) FROM public.donations d JOIN public.streamers s "
                                       "ON s.id = d.streamer_id WHERE s.streamer_slug NOT LIKE 'authtest-%'"
                                       ).fetchone()[0]
    assert (other_streamers, other_donations) == (0, 0), "metrics test needs an otherwise empty scratch database"
    with _metrics_client(env) as client:
        r = client.get("/api/v1/metrics/platform")
        assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=300"
        assert r.json() == {"total_interactions": 4, "total_alerts": 3, "active_streams": 2}
        s = client.get("/api/v1/metrics/showcase")
        assert s.status_code == 200 and s.headers["cache-control"] == "public, max-age=60"
        body = s.json()
        assert set(body) == {"streamers_live", "alerts_last_24h", "last_sync"}
        assert body["streamers_live"] == 3            # active only: a, b, idle
        assert body["alerts_last_24h"] == 2           # legacy bug ('completed') always gave 0
        assert datetime.strptime(body["last_sync"], "%Y-%m-%dT%H:%M:%SZ")
        donation(b, 70)                               # new data: served from the in-process cache until TTL
        assert client.get("/api/v1/metrics/platform").json()["total_interactions"] == 4
        assert client.get("/api/v1/metrics/showcase").json() == body
    with _metrics_client(env) as fresh:               # a new process sees the new row
        assert fresh.get("/api/v1/metrics/platform").json()["total_interactions"] == 5


# ── public = no authentication, no CSRF, CORS by allowlist ──────────────────────

ALL_PATHS = ["", "/pricing", "/soundboard", "/goal"]


def json_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in json_keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in json_keys(v)}
    return set()


def test_public_endpoints_need_no_auth_csrf_or_origin(app):
    streamer("authtest-open", soundboard_enabled=True)
    urls = [f"{P}/authtest-open{s}" for s in ALL_PATHS] + ["/api/v1/metrics/platform", "/api/v1/metrics/showcase"]
    for url in urls:
        r = app.get(url)                                                       # no cookie, no Origin (OBS)
        assert r.status_code == 200 and "set-cookie" not in r.headers, url
        assert "access-control-allow-origin" not in r.headers
        evil = app.get(url, headers={"origin": "https://evil.invalid"})
        assert evil.status_code == 200 and "access-control-allow-origin" not in evil.headers
        ok = app.get(url, headers={"origin": FRONT})
        assert ok.headers["access-control-allow-origin"] == FRONT
        assert app.get(url, headers={"cookie": "__Host-hc_session=garbage"}).status_code == 200
        assert app.post(url, headers={"origin": FRONT}).status_code == 405       # read-only
    headers = signed_in(app, "viewer" + DOMAIN)                                  # a signed-in user sees the same data
    assert app.get(f"{P}/authtest-open", headers=headers).json() == app.get(f"{P}/authtest-open").json()


# ── privacy: nothing private leaks from any endpoint ────────────────────────────

def test_no_private_data_in_any_public_response(strict_scratch_db, app):
    owner = make_user("owner-private" + DOMAIN)
    sid = streamer("authtest-priv", user_id=owner, soundboard_enabled=True, goal_is_active=True,
                   goal_activated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC), goal_name="G", goal_target_amount=1,
                   alert_overlay_id=2, leaderboard_overlay_id=6, alert_box_scale=1.37, overlay_adjust_mode=True,
                   leaderboard_bg_enabled=False,
                   order_prefix="authtestPRIVPFX", custom_page_path="/authtest-secret-page",
                   discord_moderation_channel_id="authtest-discord-777", razorpay_key_ref="authtest-rzp-ref",
                   el_voice_id="authtest-voice-id", rejection_reason="authtest-reason")
    with db() as conn:
        conn.execute("UPDATE public.users SET streamer_id = %s WHERE id = %s", (sid, owner))
        conn.execute("INSERT INTO public.obs_tokens (streamer_id, token_hash) VALUES (%s, 'authtest-obs-hash-xyz')",
                     (sid,))
        _custom(conn, "authtest-priv", 9, order=0)
    donation(sid, 777.77, name="Secret Donor", message="authtest private message")
    donation(sid, 100)  # so the goal aggregate (877.77) differs from any single donation amount
    totals(sid, ("Public Name", 1234))
    urls = [f"{P}/authtest-priv{s}" for s in ALL_PATHS] + ["/api/v1/metrics/platform", "/api/v1/metrics/showcase",
                                                          f"{P}/authtest-priv/pricing?currency=USD"]
    texts = []
    for url in urls:
        r = app.get(url)
        assert r.status_code == 200, url
        texts.append(r.text)
        leaked = json_keys(r.json()) & NOT_PUBLIC_KEYS
        assert not leaked, (url, leaked)
    blob = "\n".join(texts)
    for secret in (owner, sid, DOMAIN, "authtestPRIVPFX", "authtest-secret-page", "authtest-discord-777",
                   "authtest-rzp-ref", "authtest-voice-id", "authtest-reason", "authtest-obs-hash-xyz",
                   "authtest private message", "Secret Donor", "777.77", "authtest-", "razorpay", "email",
                   "password", "token", "order_", "Public Name", "2026-01-02", "1.37"):
        assert secret not in blob.replace("authtest-priv", "").replace("authtest-a9", "").replace("authtest-i9", ""), secret
    # The leaderboard (donor names and totals) is OBS-token data now: no anonymous route returns it.
