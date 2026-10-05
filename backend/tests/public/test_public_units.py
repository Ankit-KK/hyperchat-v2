"""Phase 4 pure logic: pricing rules, soundboard merge, fixed response field sets, metrics cache, Nginx limits."""

import asyncio
import re
from decimal import Decimal
from pathlib import Path

import pytest

from app.public import models, pricing, soundboard
from app.public.service import TTLCache

REPO = Path(__file__).resolve().parents[3]

# ── pricing ─────────────────────────────────────────────────────────────────────

STREAMER = {"min_text_amount_inr": None, "min_tts_amount_inr": Decimal("75"), "min_voice_amount_inr": Decimal("150"),
            "min_hypersound_amount_inr": Decimal("10"), "media_min_amount": Decimal("100")}


@pytest.mark.parametrize("currency", sorted(pricing.EXCHANGE_RATES_TO_INR))
def test_all_eight_legacy_currencies_supported(currency):
    assert pricing.normalize_currency(currency.lower()) == currency


@pytest.mark.parametrize("bad", ["JPY", "XXX", "US", "usd ", "", "€"])
def test_unsupported_currencies_rejected(bad):
    if bad.strip().upper() in pricing.EXCHANGE_RATES_TO_INR:
        assert pricing.normalize_currency(bad)
    elif bad == "":
        assert pricing.normalize_currency(bad) == "INR"  # absent/empty -> default INR
    else:
        with pytest.raises(pricing.UnsupportedCurrency):
            pricing.normalize_currency(bad)


def test_legacy_table_is_unchanged():
    assert pricing.EXCHANGE_RATES_TO_INR == {"INR": 1, "USD": 89, "EUR": 94, "GBP": 113, "AED": 24, "AUD": 57,
                                             "CAD": 64, "PHP": 1.5}


def test_minimums_floors_and_inr_rounding():
    assert pricing.minimums(STREAMER, "INR") == {
        "min_text": 20.0,        # floor 20, creator unset
        "min_tts": 80.0,         # max(40, 75) = 75 -> next 10
        "min_voice": 150.0,      # floor 0, creator 150
        "min_hypersound": 30.0,  # floor 30 beats creator 10
        "min_media": 100.0,
    }


@pytest.mark.parametrize("currency, expected_text, expected_tts", [
    ("USD", 0.5, 1.0),   # 20/89=0.22 -> 0.5 ; 75/89=0.84 -> 1.0
    ("AED", 1.0, 4.0),   # 20/24=0.83 -> 1 ; 75/24=3.13 -> 4
    ("GBP", 0.5, 1.0),
    ("PHP", 13.5, 50.0), # 20/1.5=13.33 -> 13.5 ; 75/1.5=50 -> 50.0
])
def test_conversion_rounding_matches_legacy(currency, expected_text, expected_tts):
    m = pricing.minimums(STREAMER, currency)
    assert (m["min_text"], m["min_tts"]) == (expected_text, expected_tts)


def test_message_char_tiers_conversion_and_malformed_entries():
    raw = [{"min_amount": 50, "max_chars": 100}, {"min_amount": 151, "max_chars": 200},
           {"min_amount": "x", "max_chars": 5}, {"min_amount": True, "max_chars": 5}, "junk", {"max_chars": 9}]
    assert pricing.message_char_tiers(raw, "INR") == [{"min_amount": 50.0, "max_chars": 100},
                                                     {"min_amount": 160.0, "max_chars": 200}]
    assert pricing.message_char_tiers(None, "INR") is None
    assert pricing.message_char_tiers({"not": "a list"}, "INR") is None


def test_voice_tier_fallback_order():
    own, platform = [(0, 10), (200, 20)], [(0, 8), (150, 12)]
    assert pricing.voice_tiers(own, platform, "INR")[1] == {"min_amount": 200.0, "max_duration_seconds": 20}
    assert pricing.voice_tiers([], platform, "INR")[1] == {"min_amount": 150.0, "max_duration_seconds": 12}
    assert pricing.voice_tiers([], [], "INR") == [{"min_amount": 0.0, "max_duration_seconds": 8},
                                                {"min_amount": 150.0, "max_duration_seconds": 12},
                                                {"min_amount": 300.0, "max_duration_seconds": 15}]


# ── soundboard merge ────────────────────────────────────────────────────────────

def _p(pid, order, url="https://cdn.example.test/p.mp3"):
    return {"id": pid, "display_name": f"P{pid}", "audio_url": url, "image_url": None, "default_order_index": order}


def _s(sid, platform=None, visible=True, order=None, url=None, name=None, image=None):
    return {"id": sid, "platform_slot_id": platform, "is_visible": visible, "order_index": order,
            "display_name": name, "audio_url": url, "image_url": image}


def test_soundboard_merge_rules():
    platform = [_p("p1", 0), _p("p2", 1), _p("p3", 2), _p("p4", 3, url="")]
    own = [
        _s("o1", platform="p1", visible=True, order=5, name="Renamed", image="https://cdn.example.test/i.gif"),
        _s("o2", platform="p2", visible=False),                        # hides platform p2
        _s("c1", visible=True, order=1, url="https://cdn.example.test/c1.mp3", name="Custom"),
        _s("c2", visible=False, order=0, url="https://cdn.example.test/c2.mp3", name="Hidden custom"),
        _s("c3", visible=True, order=None, url=None, name="No audio"),
    ]
    sounds = soundboard.merge(platform, own)
    assert [s["id"] for s in sounds] == ["c1", "p3", "o1"]  # orders 1, 2, 5; p2 hidden, p4/c3 no audio
    assert sounds[2] == {"id": "o1", "display_name": "Renamed", "audio_url": "https://cdn.example.test/p.mp3",
                         "thumb_url": "https://cdn.example.test/i.gif"}
    assert soundboard.merge([], []) == []


# ── fixed response field sets (privacy guard) ──────────────────────────────────

EXPECTED_FIELDS = {
    models.PublicStreamer: {"slug", "name", "brand_color", "brand_logo_url", "background_url", "tts_enabled",
                            "hyperemotes_enabled", "media_upload_enabled", "media_max_file_size_mb",
                            "soundboard_enabled", "leaderboard_widget_enabled"},
    models.Pricing: {"currency", "min_text", "min_tts", "min_voice", "min_hypersound", "min_media", "tts_enabled",
                     "message_char_tiers", "voice_duration_tiers"},
    models.OverlayConfig: {"slug", "name", "brand_color", "brand_logo_url", "alert_box_scale", "alert_overlay_id",
                           "leaderboard_overlay_id", "goal_overlay_id", "overlay_adjust_mode",
                           "leaderboard_widget_enabled", "leaderboard_bg_enabled", "hyperemotes_enabled",
                           "media_upload_enabled", "tts_enabled", "goal_name", "goal_target_amount",
                           "goal_activated_at", "goal_is_active"},
    models.Sound: {"id", "display_name", "audio_url", "thumb_url"},
    models.Soundboard: {"sounds"},
    models.TopDonor: {"name", "total_amount_inr"},
    models.Leaderboard: {"top_donor"},
    models.Goal: {"active", "name", "target_amount_inr", "progress_inr"},
    models.PlatformMetrics: {"total_interactions", "total_alerts", "active_streams"},
    models.ShowcaseMetrics: {"streamers_live", "alerts_last_24h", "last_sync"},
    models.AmountTier: {"min_amount", "max_chars"},
    models.VoiceTier: {"min_amount", "max_duration_seconds"},
}
FORBIDDEN_NAME = re.compile(r"email|user_id|password|token|secret|key_ref|order|provider|message$|custom_page|"
                            r"pusher|razorpay|moderation|discord|telegram|rejection|api_enabled|^streamer_id$")


@pytest.mark.parametrize("model, fields", EXPECTED_FIELDS.items(), ids=lambda m: getattr(m, "__name__", ""))
def test_response_models_have_exact_fields_and_no_private_names(model, fields):
    assert set(model.model_fields) == fields
    assert not [f for f in fields if FORBIDDEN_NAME.search(f)]
    assert all(c in "abcdefghijklmnopqrstuvwxyz_0123456789" for f in fields for c in f)  # snake_case
    assert model.model_config.get("extra") == "forbid"  # unknown fields can never be added to a response


# ── metrics cache ───────────────────────────────────────────────────────────────

def test_ttl_cache_hits_within_ttl_and_reloads_after():
    now = [100.0]
    cache, calls = TTLCache(clock=lambda: now[0]), []

    async def loader():
        calls.append(1)
        return len(calls)

    async def run():
        assert await cache.get("k", 60, loader) == 1
        assert await cache.get("k", 60, loader) == 1
        now[0] += 61
        assert await cache.get("k", 60, loader) == 2
        results = await asyncio.gather(*[cache.get("j", 60, loader) for _ in range(5)])
        assert results == [3] * 5  # concurrent misses load once
    asyncio.run(run())
    assert len(calls) == 3


# ── Nginx rate limit (configuration; behaviour: tests/e2e/nginx_ratelimit_check.sh) ─

def test_nginx_public_rate_limit_configuration():
    zone = (REPO / "deploy/nginx/snippets/hyperchat-public-limit-zone.conf").read_text()
    limit = (REPO / "deploy/nginx/snippets/hyperchat-public-limit.conf").read_text()
    site = (REPO / "deploy/nginx/api.quicktestspace.site.conf").read_text()
    assert "limit_req_zone $binary_remote_addr zone=hyperchat_public:10m rate=10r/s;" in zone
    assert "limit_req zone=hyperchat_public burst=20 nodelay;" in limit and "limit_req_status 429;" in limit
    assert "include /etc/nginx/snippets/hyperchat-public-limit-zone.conf;" in site
    block = site.split("location ~ ^/api/v1/(public|metrics)/ {", 1)[1].split("}", 1)[0]
    assert "include /etc/nginx/snippets/hyperchat-public-limit.conf;" in block
    assert "proxy_pass http://127.0.0.1:8000;" in block and "X-Forwarded-For" in block
    assert "return 429 '{\"error\":\"Too many requests\"}';" in site
    other = site.split("location / {")[-1]
    assert "limit_req" not in other  # login, admin, health etc. are not rate limited by this zone
