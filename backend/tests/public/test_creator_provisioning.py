"""CRE-9: creator pages are provisioned automatically. One shared set of public routes serves every creator from
the database; nothing per creator is generated or registered. A creator becomes public only when it is `active`
(admin activation after KYC), disappears on suspension exactly like an unknown slug on every public, checkout and
upload route, and never exposes private data. Real app, scratch database, fake Razorpay, in-memory R2."""

import json

import pytest

from app.admin.service import RESERVED_SLUGS
from app.storage.memory import InMemoryObjectStore
from tests.integration import (
    DOMAIN,
    ORIGIN,
    csrf_headers,
    db,
    login,
    make_streamer,
    make_user,
    new_browser,
    requires_db,
)
from tests.kyc.test_enforcement_db import enf, owner_user, standard_creator, status_change, verify  # noqa: F401
from tests.payments.test_payments_db import checkout, client, rz_env  # noqa: F401 (fixtures)
from tests.uploads.test_uploads_db import uploads_env  # noqa: F401 (fixture)

pytestmark = requires_db
P = "/api/v1/public/streamers"
C = "/api/v1/admin/creators"
NOT_FOUND = {"error": "Streamer not found"}
READS = ("", "/pricing", "/goal", "/soundboard")


@pytest.fixture
def world(uploads_env, client, enf):  # uploads_env first: its secret must exist before the app is built
    enf.app_state.uploads.store = InMemoryObjectStore()
    enf.grants = uploads_env
    return enf


def _unknown_404(app, slug: str) -> None:
    """Every public read, checkout and upload-grant answers exactly as for a slug that never existed."""
    for suffix in READS:
        r = app.get(f"{P}/{slug}{suffix}")
        assert (r.status_code, r.json()) == (404, NOT_FOUND), (slug, suffix)
        assert "cache-control" not in r.headers, (slug, suffix)  # a 404 is never cached
    r = app.post(f"{P}/{slug}/donations", json={"name": "Ravi", "amount": 100}, headers=ORIGIN)
    assert (r.status_code, r.json()) == (404, NOT_FOUND), slug
    r = app.post(f"{P}/{slug}/upload-grant", json={"kind": "voice"}, headers=ORIGIN)
    assert (r.status_code, r.json()) == (404, NOT_FOUND), slug


def _grant(app, slug: str) -> str:
    r = app.post(f"{P}/{slug}/upload-grant", json={"kind": "voice"}, headers=ORIGIN)
    assert r.status_code == 201, r.text
    app.grants.append(r.json()["grant"].split(".")[3])
    return r.json()["grant"]


def _public(app, slug: str) -> str:
    profile = app.get(f"{P}/{slug}")
    assert profile.status_code == 200 and profile.headers["cache-control"] == "public, max-age=60", profile.text
    assert app.get(f"{P}/{slug}/pricing").status_code == 200 and app.get(f"{P}/{slug}/goal").status_code == 200
    return profile.text


def _no_private_data(text: str, *secrets_: str) -> None:
    for value in (*secrets_, "razorpay", "primary", "kyc", "user_id", "status", "pending", "suspended", DOMAIN):
        assert value not in text, value


def test_admin_creation_kyc_and_activation_make_the_page_appear_with_no_per_creator_code(world):
    uid = owner_user("prov-new")
    r = world.admin.post(C, json={"slug": "AuthTest-Prov-New", "name": "Prov New", "razorpay_account": "primary"},
                         headers=world.boss)
    assert r.status_code == 201 and r.json()["creator"]["status"] == "pending", r.text
    sid, slug = r.json()["creator"]["id"], r.json()["creator"]["slug"]
    assert slug == "authtest-prov-new"  # stored lowercased: one canonical page per creator
    calls = len(world.fake.calls)
    _unknown_404(world, slug)  # pending, no owner
    assert world.admin.post(f"{C}/{sid}/owner", json={"user_id": uid}, headers=world.boss).status_code == 200
    _unknown_404(world, slug)  # pending, owner assigned: still invisible
    verify(uid)
    _unknown_404(world, slug)  # owner verified, creator not yet activated: still invisible
    assert len(world.fake.calls) == calls  # nothing reached Razorpay for an invisible creator

    assert status_change(world, sid, "activate", "pending").status_code == 200
    text = _public(world, slug)  # the next request serves it: no deploy, route or cache to refresh
    assert (json.loads(text)["name"], json.loads(text)["slug"]) == ("Prov New", slug)
    _no_private_data(text, sid, uid, "prov-new" + DOMAIN)
    for upper in ("AUTHTEST-PROV-NEW", "AuthTest-Prov-New"):  # any case reaches the same page
        assert world.get(f"{P}/{upper}").text == text
    order = checkout(world, slug)
    assert order["razorpay_order_id"] and len(world.fake.calls) > calls  # an active creator takes donations
    _grant(world, slug)


def test_suspension_hides_the_creator_everywhere_and_reactivation_restores_it(world):
    owner = make_user("prov-s-owner" + DOMAIN)
    sid = make_streamer("authtest-prov-s", user_id=owner, name="Prov S")
    make_streamer("authtest-prov-other", name="Prov Other")
    _public(world, "authtest-prov-s")
    stale = _grant(world, "authtest-prov-s")  # issued while active
    assert status_change(world, sid, "suspend", "active", "review").status_code == 200

    _unknown_404(world, "authtest-prov-s")
    unknown = world.get(f"{P}/authtest-prov-never-existed")
    assert world.get(f"{P}/authtest-prov-s").content == unknown.content  # byte-identical to an unknown slug
    r = world.post(f"{P}/authtest-prov-s/uploads/voice", content=b"x",
                   headers={**ORIGIN, "content-type": "audio/webm", "x-upload-grant": stale})
    assert (r.status_code, r.json()) == (404, NOT_FOUND)  # a grant issued before suspension is useless
    _no_private_data(unknown.text + world.get(f"{P}/authtest-prov-s").text, sid, owner, "review")
    _public(world, "authtest-prov-other")  # other creators are unaffected: one shared route set

    assert status_change(world, sid, "reactivate", "suspended").status_code == 200
    _public(world, "authtest-prov-s")
    _grant(world, "authtest-prov-s")


def test_many_creators_are_served_by_the_same_routes_from_their_own_rows(world):
    names = {f"authtest-prov-m{i}": f"Creator {i}" for i in range(4)}
    for slug, name in names.items():
        make_streamer(slug, name=name)
    for slug, name in names.items():
        profile = world.get(f"{P}/{slug}").json()
        assert (profile["slug"], profile["name"]) == (slug, name)
    route_paths = {getattr(route, "path", "") for route in world.app.routes}
    assert not any(slug in path for slug in names for path in route_paths)  # nothing registered per creator


@pytest.mark.parametrize("slug", sorted(RESERVED_SLUGS))
def test_every_reserved_site_path_is_refused_as_a_new_creator_slug(world, slug):
    for candidate in (slug, slug.upper()):
        r = world.admin.post(C, json={"slug": candidate, "name": "N", "razorpay_account": "primary"},
                             headers=world.boss)
        assert r.status_code == 400, (candidate, r.text)
        if len(slug) >= 3:  # shorter reserved paths ("me") already fail the 3-64 shape rule first
            assert r.json() == {"error": "This slug is reserved"}, (candidate, r.text)
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.streamers WHERE streamer_slug = %s", (slug,)).fetchone()[0] == 0


@pytest.mark.parametrize("slug", ["ab", "-authtest", "authtest.x", "authtest x", "authtest/x", "a" * 65,
                                  "authtest-ü", "authtest%00", ""])
def test_invalid_slugs_are_refused_at_creation_and_unknown_publicly(world, slug):
    r = world.admin.post(C, json={"slug": slug, "name": "N", "razorpay_account": "primary"}, headers=world.boss)
    assert r.status_code == (422 if len(slug) > 64 else 400), (slug, r.text)  # 422: the request schema's length cap
    if slug and "/" not in slug:
        assert (world.get(f"{P}/{slug}").status_code, world.get(f"{P}/{slug}").json()) == (404, NOT_FOUND)


def test_a_conflicting_slug_is_refused_in_any_case_and_the_first_creator_is_untouched(world):
    sid = make_streamer("authtest-prov-dup", name="First")
    for candidate in ("authtest-prov-dup", "AuthTest-Prov-Dup"):
        r = world.admin.post(C, json={"slug": candidate, "name": "Second", "razorpay_account": "primary"},
                             headers=world.boss)
        assert (r.status_code, r.json()) == (409, {"error": "That slug is already used"}), candidate
    assert world.get(f"{P}/authtest-prov-dup").json()["name"] == "First"
    with db() as conn:
        assert conn.execute("SELECT id::text FROM public.streamers WHERE lower(streamer_slug) = "
                            "'authtest-prov-dup'").fetchall() == [(sid,)]


def test_only_the_owner_linked_account_and_admin_manage_a_provisioned_creator(world):
    sid = make_streamer("authtest-prov-own", user_id=make_user("prov-own" + DOMAIN))
    make_user("prov-linked" + DOMAIN, streamer_id=sid)
    make_streamer("authtest-prov-own-b", user_id=make_user("prov-other-own" + DOMAIN))  # owns another creator
    make_user("prov-stranger" + DOMAIN)
    url = "/api/v1/dashboard/streamers/authtest-prov-own/settings"
    for email, allowed in (("prov-own", True), ("prov-linked", True), ("prov-other-own", False),
                           ("prov-stranger", False)):
        browser = new_browser(world)
        headers = csrf_headers(login(browser, email + DOMAIN))
        expected = 200 if allowed else 403
        assert browser.get(url).status_code == expected, email
        assert browser.patch(url, json={"goal_overlay_id": 2}, headers=headers).status_code == expected, email
        mine = browser.get("/api/v1/me/streamers").text
        assert (sid in mine) is allowed, email  # a creator is listed only for its owner and linked account
    assert world.admin.get(url).status_code == 200  # an admin manages every creator
    with db() as conn:
        assert conn.execute("SELECT goal_overlay_id FROM public.streamers WHERE id = %s", (sid,)).fetchone()[0] == 2
