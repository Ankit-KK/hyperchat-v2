"""PAY-14 creator pricing (D-M3b): the validation matrix (pure, no database) and the settings PATCH / public pricing /
public pricing behaviour against a scratch database."""

import pytest

from app.core.errors import ApiError
from app.dashboard import models
from app.dashboard import validation as v
from tests.integration import (
    DOMAIN,
    ORIGIN,
    csrf_headers,
    db,
    fixture_db,
    login,
    make_streamer,
    make_user,
    new_browser,
    owned_creator,
    requires_db,
)

S = "/api/v1/dashboard/streamers"
P = "/api/v1/public/streamers"


def _char(*pairs):
    return [{"min_amount": a, "max_chars": c} for a, c in pairs]


def _voice(*pairs):
    return [{"min_amount": a, "max_duration_seconds": s} for a, s in pairs]


# ── the validation matrix: (body, error or None when accepted). Every row is also in the PR description. ──────────
MATRIX = [
    # per-type minimums: platform floor (text 40, voice 0, media 0; D-M3a not approved) .. 10,000
    ({"message_min_amount": 40}, None),
    ({"message_min_amount": 10_000}, None),
    ({"message_min_amount": 39.99}, "message_min_amount must be 40-10000"),
    ({"message_min_amount": 10_000.01}, "message_min_amount must be 40-10000"),
    ({"voice_min_amount": 0}, None),
    ({"voice_min_amount": 150}, None),
    ({"voice_min_amount": -1}, "voice_min_amount must be 0-10000"),
    ({"voice_min_amount": 10_001}, "voice_min_amount must be 0-10000"),
    ({"media_min_amount": 0}, None),
    ({"media_min_amount": 10_000}, None),
    ({"media_min_amount": -0.01}, "media_min_amount must be 0-10000"),
    ({"media_min_amount": 10_000.5}, "media_min_amount must be 0-10000"),
    # message character tiers: at most 5, max_chars 50-500, min_amount 0-10,000, both strictly increasing
    ({"message_char_tiers": []}, None),
    ({"message_char_tiers": _char((0, 50))}, None),
    ({"message_char_tiers": _char((0, 50), (100, 100), (200, 200), (300, 300), (10_000, 500))}, None),
    ({"message_char_tiers": _char((0, 50), (1, 51), (2, 52), (3, 53), (4, 54), (5, 55))},
     "message_char_tiers allows at most 5 tiers"),
    ({"message_char_tiers": _char((0, 49))}, "message_char_tiers max_chars must be 50-500 characters"),
    ({"message_char_tiers": _char((0, 501))}, "message_char_tiers max_chars must be 50-500 characters"),
    ({"message_char_tiers": _char((-1, 100))}, "message_char_tiers min_amount must be 0-10000"),
    ({"message_char_tiers": _char((10_001, 100))}, "message_char_tiers min_amount must be 0-10000"),
    ({"message_char_tiers": _char((100, 100), (100, 200))},
     "message_char_tiers tiers must have strictly increasing min_amount and max_chars"),
    ({"message_char_tiers": _char((100, 200), (50, 300))},
     "message_char_tiers tiers must have strictly increasing min_amount and max_chars"),
    ({"message_char_tiers": _char((100, 200), (200, 200))},
     "message_char_tiers tiers must have strictly increasing min_amount and max_chars"),
    ({"message_char_tiers": _char((100, 300), (200, 200))},
     "message_char_tiers tiers must have strictly increasing min_amount and max_chars"),
    # voice tiers: at most 5, 5-30 seconds, min_amount 0-10,000, both strictly increasing
    ({"voice_tiers": []}, None),
    ({"voice_tiers": _voice((0, 5))}, None),
    ({"voice_tiers": _voice((0, 5), (100, 10), (200, 15), (300, 20), (10_000, 30))}, None),
    ({"voice_tiers": _voice((0, 5), (1, 6), (2, 7), (3, 8), (4, 9), (5, 10))}, "voice_tiers allows at most 5 tiers"),
    ({"voice_tiers": _voice((0, 4))}, "voice_tiers max_duration_seconds must be 5-30 seconds"),
    ({"voice_tiers": _voice((0, 31))}, "voice_tiers max_duration_seconds must be 5-30 seconds"),
    ({"voice_tiers": _voice((10_001, 10))}, "voice_tiers min_amount must be 0-10000"),
    ({"voice_tiers": _voice((150, 10), (150, 20))},
     "voice_tiers tiers must have strictly increasing min_amount and max_duration_seconds"),
    ({"voice_tiers": _voice((150, 20), (300, 10))},
     "voice_tiers tiers must have strictly increasing min_amount and max_duration_seconds"),
    # several fields: the first broken rule (fixed field order) is reported
    ({"media_min_amount": 50, "message_min_amount": 10}, "message_min_amount must be 40-10000"),
]


@pytest.mark.parametrize("body,error", MATRIX, ids=lambda x: repr(x)[:70])
def test_validation_matrix(body, error):
    changes = models.SettingsPatch.model_validate(body).changes()
    if error is None:
        v.pricing_rules(changes)
        return
    with pytest.raises(ApiError) as exc:
        v.pricing_rules(changes)
    assert (exc.value.status, exc.value.error, exc.value.reason) == (400, error, "invalid_pricing")


def test_unrelated_settings_pass_the_pricing_rules():
    v.pricing_rules({"tts_enabled": False, "goal_name": "Mic"})


# ── through the API (scratch database) ──────────────────────────────────────────

def _cols(sid: str) -> tuple:
    with db() as conn:
        return conn.execute("SELECT min_text_amount_inr, min_voice_amount_inr, media_min_amount, message_char_tiers, "
                            "updated_at FROM public.streamers WHERE id = %s", (sid,)).fetchone()


def _tiers(sid: str) -> list[tuple]:
    with db() as conn:
        return conn.execute("SELECT min_amount_inr, max_duration_seconds, is_active FROM public.streamer_voice_tiers "
                            "WHERE streamer_id = %s ORDER BY min_amount_inr", (sid,)).fetchall()


PRICES = {"message_min_amount": 100, "voice_min_amount": 200, "media_min_amount": 300,
          "message_char_tiers": _char((100, 150), (500, 400)),
          "voice_tiers": _voice((200, 10), (1000, 30))}


@requires_db
def test_creator_sets_minimums_and_tiers_and_public_pricing_and_get_follow(app):
    sid, headers = owned_creator(app, "authtest-price14")
    url = f"{S}/authtest-price14/settings"
    r = app.patch(url, json=PRICES, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == ["message_min_amount", "voice_min_amount", "media_min_amount", "message_char_tiers",
                                   "voice_tiers"]
    s = r.json()["settings"]
    assert (s["message_min_amount"], s["voice_min_amount"], s["media_min_amount"]) == (100.0, 200.0, 300.0)
    assert s["message_char_tiers"] == PRICES["message_char_tiers"] and s["voice_tiers"] == PRICES["voice_tiers"]
    assert app.get(url).json() == s
    assert _tiers(sid) == [(200, 10, True), (1000, 30, True)]
    pricing = app.get(f"{P}/authtest-price14/pricing").json()
    assert (pricing["min_text"], pricing["min_voice"], pricing["min_media"]) == (100.0, 200.0, 300.0)
    assert pricing["message_char_tiers"] == [{"min_amount": 100.0, "max_chars": 150},
                                             {"min_amount": 500.0, "max_chars": 400}]
    assert pricing["voice_duration_tiers"] == [{"min_amount": 200.0, "max_duration_seconds": 10},
                                               {"min_amount": 1000.0, "max_duration_seconds": 30}]
    stamp = _cols(sid)[-1]
    again = app.patch(url, json=PRICES, headers=headers)
    assert again.json() == {"settings": s, "changed": []} and _cols(sid)[-1] == stamp  # nothing differed


@requires_db
def test_floors_and_limits_answer_400_naming_the_field_and_change_nothing(app):
    sid, headers = owned_creator(app, "authtest-floor14")
    url = f"{S}/authtest-floor14/settings"
    before, tiers = _cols(sid), _tiers(sid)
    for body, field in (({"message_min_amount": 39}, "message_min_amount"),
                        ({"voice_min_amount": 10_001}, "voice_min_amount"),
                        ({"media_min_amount": -1}, "media_min_amount"),
                        ({"message_char_tiers": _char((0, 40))}, "message_char_tiers"),
                        ({"voice_tiers": _voice((0, 60))}, "voice_tiers"),
                        ({"voice_tiers": _voice((0, 10)), "message_min_amount": 20}, "message_min_amount")):
        r = app.patch(url, json=body, headers=headers)
        assert r.status_code == 400 and r.json()["error"].startswith(field), (body, r.text)
    assert _cols(sid) == before and _tiers(sid) == tiers


@requires_db
def test_voice_tiers_replace_the_whole_set_atomically(app):
    sid, headers = owned_creator(app, "authtest-vt14")
    with fixture_db() as conn:  # legacy rows: an inactive one and one out of today's range
        conn.execute("INSERT INTO public.streamer_voice_tiers (streamer_id, min_amount_inr, max_duration_seconds, "
                     "is_active) VALUES (%s, 0, 8, true), (%s, 150, 60, true), (%s, 500, 90, false)", (sid, sid, sid))
    url = f"{S}/authtest-vt14/settings"
    r = app.patch(url, json={"voice_tiers": _voice((0, 8), (150, 20))}, headers=headers)
    assert r.json()["changed"] == ["voice_tiers"] and _tiers(sid) == [(0, 8, True), (150, 20, True)]
    # an equal active set still replaces nothing; a different one replaces every row
    assert app.patch(url, json={"voice_tiers": _voice((0, 8), (150, 20))}, headers=headers).json()["changed"] == []
    assert app.patch(url, json={"voice_tiers": _voice((50, 5))}, headers=headers).json()["changed"] == ["voice_tiers"]
    assert _tiers(sid) == [(50, 5, True)]
    # [] clears them: the platform tiers (or the built-in fallback) apply again
    assert app.patch(url, json={"voice_tiers": []}, headers=headers).json()["settings"]["voice_tiers"] == []
    assert _tiers(sid) == []
    with db() as conn:
        platform = conn.execute("SELECT count(*) FROM public.platform_voice_tiers WHERE is_active").fetchone()[0]
    if platform == 0:
        assert app.get(f"{P}/authtest-vt14/pricing").json()["voice_duration_tiers"][0]["max_duration_seconds"] == 8


@requires_db
def test_existing_creators_are_unchanged_when_they_set_nothing(app):
    sid, headers = owned_creator(app, "authtest-keep14", min_voice_amount_inr=150,
                                 message_char_tiers='[{"min_amount": 40, "max_chars": 200}]')
    with fixture_db() as conn:
        conn.execute("INSERT INTO public.streamer_voice_tiers (streamer_id, min_amount_inr, max_duration_seconds) "
                     "VALUES (%s, 150, 10)", (sid,))
    pricing, cols, tiers = app.get(f"{P}/authtest-keep14/pricing").json(), _cols(sid)[:4], _tiers(sid)
    r = app.patch(f"{S}/authtest-keep14/settings", json={"brand_color": "#000000", "tts_enabled": False},
                  headers=headers)
    assert r.json()["changed"] == ["tts_enabled", "brand_color"]
    assert _cols(sid)[:4] == cols and _tiers(sid) == tiers
    assert {k: val for k, val in app.get(f"{P}/authtest-keep14/pricing").json().items() if k != "tts_enabled"} == {
        k: val for k, val in pricing.items() if k != "tts_enabled"}


@requires_db
def test_linked_account_may_set_prices_strangers_and_anonymous_may_not(app):
    sid = make_streamer("authtest-auth14", user_id=make_user("owner" + DOMAIN))
    make_user("linked" + DOMAIN, streamer_id=sid)
    make_user("stranger" + DOMAIN)
    url, body = f"{S}/authtest-auth14/settings", {"message_min_amount": 500}
    assert new_browser(app).patch(url, json=body, headers=ORIGIN).status_code == 401
    stranger = new_browser(app)
    assert stranger.patch(url, json=body, headers=csrf_headers(login(stranger, "stranger" + DOMAIN))).status_code == 403
    assert _cols(sid)[0] is None
    linked = new_browser(app)
    r = linked.patch(url, json=body, headers=csrf_headers(login(linked, "linked" + DOMAIN)))
    assert r.status_code == 200 and r.json()["changed"] == ["message_min_amount"]
    assert _cols(sid)[0] == 500
