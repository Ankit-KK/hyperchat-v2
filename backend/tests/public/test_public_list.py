"""E10 (review gap G5, owner decision R7): GET /api/v1/public/streamers lists active creators for the marketing site's
creator list and the sitemap. Real app, scratch database (tests/integration.py), except the pure cursor tests.

The scratch database holds other tests' active creators, so every database assertion is scoped with a `q` prefix
unique to this run."""

import secrets

import pytest

from app.core.errors import ApiError
from app.public.service import decode_slug_cursor, encode_slug_cursor
from tests.integration import make_streamer, requires_db, update_streamer

P = "/api/v1/public/streamers"
FIELDS = {"slug", "name", "brand_logo_url", "background_url", "brand_color"}


# ── cursor (no database) ───────────────────────────────────────────────────────

@pytest.mark.parametrize("slug", ["a", "authtest-x_y", "Legacy.Slug", "a" * 64])
def test_the_cursor_round_trips_any_stored_slug(slug):
    assert decode_slug_cursor(encode_slug_cursor(slug)) == slug


@pytest.mark.parametrize("bad", ["", "%%%", "!!", "a" * 101, encode_slug_cursor("a" * 65), "_-8"])
def test_a_bad_cursor_is_a_400(bad):
    with pytest.raises(ApiError) as err:
        decode_slug_cursor(bad)
    assert (err.value.status, err.value.reason) == (400, "bad_cursor")


# ── end to end ─────────────────────────────────────────────────────────────────

@pytest.fixture
def world(app):
    """Seven active creators, plus one each pending, suspended and rejected, under a prefix unique to this run."""
    prefix = f"authtest-list{secrets.token_hex(3)}-"
    active = {f"{prefix}{c}": f"List {c.upper()}" for c in ("a", "b", "c", "d", "e", "f", "g")}
    for slug, name in active.items():
        make_streamer(slug, name=name)
    update_streamer(make_streamer(f"{prefix}a_x", name="Under Score"), brand_color="#112233",
                    brand_logo_url="https://cdn.example.test/l.png", background_url="https://cdn.example.test/b.png",
                    tts_enabled=False)
    active[f"{prefix}a_x"] = "Under Score"
    for status in ("pending", "suspended", "rejected"):
        update_streamer(make_streamer(f"{prefix}{status}", name=f"Hidden {status}"), status=status)
    app.prefix, app.active = prefix, active
    return app


def _all(app, **params) -> list[dict]:
    r = app.get(P, params={"q": app.prefix, "limit": 200, **params})
    assert r.status_code == 200, r.text
    assert r.json()["has_more"] is False and r.json()["next_cursor"] is None
    return r.json()["creators"]


@requires_db
def test_only_active_creators_with_exactly_the_five_card_fields(world):
    creators = _all(world)
    assert {c["slug"] for c in creators} == set(world.active)  # pending, suspended, rejected absent
    assert all(set(c) == FIELDS for c in creators)
    styled = next(c for c in creators if c["slug"] == f"{world.prefix}a_x")
    assert styled == {"slug": f"{world.prefix}a_x", "name": "Under Score", "brand_color": "#112233",
                      "brand_logo_url": "https://cdn.example.test/l.png",
                      "background_url": "https://cdn.example.test/b.png"}


@requires_db
def test_cursor_paging_is_stable_complete_and_disjoint(world):
    expected = [c["slug"] for c in _all(world)]  # the database's own order
    seen, cursor, pages = [], None, 0
    while True:
        params = {"q": world.prefix, "limit": 3, **({"cursor": cursor} if cursor else {})}
        body = world.get(P, params=params).json()
        assert len(body["creators"]) <= 3 and body["has_more"] == (body["next_cursor"] is not None)
        seen += [c["slug"] for c in body["creators"]]
        pages += 1
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]
    assert seen == expected and len(set(seen)) == len(seen) == 8 and pages == 3
    again = world.get(P, params={"q": world.prefix, "limit": 3}).json()
    assert [c["slug"] for c in again["creators"]] == expected[:3]  # same first page every time


@requires_db
def test_limit_bounds_and_bad_cursor(world):
    assert len(world.get(P, params={"q": world.prefix, "limit": 1}).json()["creators"]) == 1
    for bad in (0, 201, -1, "x"):
        assert world.get(P, params={"limit": bad}).status_code == 422, bad
    r = world.get(P, params={"cursor": "%%%"})
    assert (r.status_code, r.json()) == (400, {"error": "Invalid cursor"})
    assert "cache-control" not in r.headers  # an error is never cached
    assert world.get(P, params={"q": "x" * 65}).status_code == 422
    assert world.get(P).status_code == 200  # default limit 50
    assert len(world.get(P).json()["creators"]) <= 50


@requires_db
def test_q_is_a_case_insensitive_literal_prefix_of_slug_or_name(world):
    assert [c["slug"] for c in _all(world, q=f"{world.prefix}A_")] == [f"{world.prefix}a_x"]  # '_' is no wildcard
    assert world.get(P, params={"q": f"{world.prefix}%"}).json()["creators"] == []  # nor is '%'
    by_name = world.get(P, params={"q": "under scor", "limit": 200}).json()["creators"]
    assert f"{world.prefix}a_x" in {c["slug"] for c in by_name}
    assert world.get(P, params={"q": f"{world.prefix}pending"}).json()["creators"] == []


@requires_db
def test_cache_header_and_no_cookie_needed_or_set(world):
    world.cookies.clear()  # no session, no CSRF cookie, no Origin header
    assert not world.cookies
    r = world.get(P, params={"q": world.prefix})
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=60"
    assert "set-cookie" not in r.headers
    assert {c["slug"] for c in r.json()["creators"]} == set(world.active)
