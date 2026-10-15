"""Admin user / invite management through the real app (scratch database, see tests/integration.py)."""

import asyncio

import pytest

from app.admin.repository import AdminRepository, LastAdminError
from app.cli import connection_factory
from app.core.config import Settings
from tests.integration import (
    DOMAIN,
    ORIGIN,
    db,
    login,
    make_streamer,
    make_user,
    new_browser,
    requires_db,
    signed_in,
)

pytestmark = requires_db
U = "/api/v1/admin/users"


def _admin(app) -> dict:
    return signed_in(app, "boss" + DOMAIN, role="admin")


def _id(email: str) -> str:
    with db() as conn:
        return conn.execute("SELECT id::text FROM public.users WHERE email = %s", (email,)).fetchone()[0]


def test_admin_endpoints_need_an_admin_session_and_csrf(app):
    target = make_user("target" + DOMAIN)
    anonymous = new_browser(app)
    assert anonymous.get(U).status_code == 401
    plain = new_browser(app)
    headers = signed_in(plain, "plain" + DOMAIN)
    assert plain.get(U).status_code == 403
    assert plain.patch(f"{U}/{target}", json={"role": "admin"}, headers=headers).status_code == 403
    assert plain.get("/api/v1/admin/invites").status_code == 403
    admin_headers = _admin(app)
    assert app.patch(f"{U}/{target}", json={"is_active": False}, headers=ORIGIN).status_code == 403  # no CSRF
    assert app.patch(f"{U}/{target}", json={"is_active": False},
                     headers={**admin_headers, "origin": "https://evil.invalid"}).status_code == 403
    with db() as conn:
        assert conn.execute("SELECT is_active FROM public.users WHERE id = %s", (target,)).fetchone()[0] is True


def test_list_users_filters_and_never_exposes_hashes(app):
    _admin(app)
    make_user("alice_list" + DOMAIN)
    make_user("bob_list" + DOMAIN, role="user", password=None)
    r = app.get(U, params={"q": "_list" + DOMAIN})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and [u["email"] for u in body["users"]] == ["alice_list" + DOMAIN, "bob_list" + DOMAIN]
    bob = body["users"][1]
    assert bob["has_password"] is False and bob["role"] == "user" and bob["is_active"] is True
    assert "password_hash" not in r.text and "$argon2" not in r.text
    assert app.get(U, params={"q": "list" + DOMAIN, "role": "user"}).json()["total"] == 1
    assert app.get(U, params={"q": "%"}).json()["total"] == 0  # LIKE wildcards are escaped
    assert app.get(U, params={"role": "superuser"}).status_code == 400
    assert app.get(U, params={"limit": 500}).status_code == 422


def test_role_change_revokes_target_sessions(app):
    headers = _admin(app)
    target_browser = new_browser(app)
    signed_in(target_browser, "promote" + DOMAIN)
    target = _id("promote" + DOMAIN)
    assert target_browser.get("/api/v1/auth/session").status_code == 200
    r = app.patch(f"{U}/{target}", json={"role": "admin"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == ["role"] and r.json()["sessions_revoked"] == 1 and r.json()["user"]["role"] == "admin"
    assert target_browser.get("/api/v1/auth/session").status_code == 401  # must sign in again
    again = login(target_browser, "promote" + DOMAIN)
    assert again.json()["user"]["role"] == "admin"
    same = app.patch(f"{U}/{target}", json={"role": "admin"}, headers=headers)
    assert same.json()["changed"] == [] and same.json()["sessions_revoked"] == 0


def test_deactivate_blocks_login_and_reactivate_restores_it(app):
    headers = _admin(app)
    browser = new_browser(app)
    signed_in(browser, "victim" + DOMAIN)
    victim = _id("victim" + DOMAIN)
    assert app.patch(f"{U}/{victim}", json={"is_active": False}, headers=headers).json()["sessions_revoked"] == 1
    assert browser.get("/api/v1/auth/session").status_code == 401
    assert login(new_browser(app), "victim" + DOMAIN).status_code == 401
    assert app.patch(f"{U}/{victim}", json={"is_active": True}, headers=headers).status_code == 200
    assert login(new_browser(app), "victim" + DOMAIN).status_code == 200


def test_admin_cannot_change_own_role_or_status(app):
    headers = _admin(app)
    me = _id("boss" + DOMAIN)
    for change in ({"role": "user"}, {"is_active": False}):
        r = app.patch(f"{U}/{me}", json=change, headers=headers)
        assert r.status_code == 403 and r.json() == {"error": "You cannot change your own role or status"}
    assert app.get("/api/v1/auth/session").status_code == 200


def test_streamer_link_unlink_and_validation(app):
    headers = _admin(app)
    sid = make_streamer("authtest-link")
    user = make_user("member" + DOMAIN)
    r = app.patch(f"{U}/{user}", json={"streamer_id": sid}, headers=headers)
    assert r.status_code == 200 and r.json()["user"]["streamer_id"] == sid
    r = app.patch(f"{U}/{user}", json={"streamer_id": None}, headers=headers)
    assert r.status_code == 200 and r.json()["user"]["streamer_id"] is None
    bad = [({"streamer_id": "00000000-0000-4000-8000-000000000000"}, 400), ({"streamer_id": "nope"}, 400),
           ({"role": "superuser"}, 422), ({"role": None}, 400), ({}, 400), ({"password_hash": "x"}, 422)]
    for body, status in bad:
        assert app.patch(f"{U}/{user}", json=body, headers=headers).status_code == status, body
    assert app.patch(f"{U}/not-a-uuid", json={"role": "user"}, headers=headers).status_code == 404
    assert app.patch(f"{U}/00000000-0000-4000-8000-000000000000", json={"role": "user"},
                     headers=headers).status_code == 404


def test_force_logout(app):
    headers = _admin(app)
    browser = new_browser(app)
    signed_in(browser, "kick" + DOMAIN)
    r = app.post(f"{U}/{_id('kick' + DOMAIN)}/sessions/revoke", headers=headers)
    assert r.status_code == 200 and r.json() == {"sessions_revoked": 1}
    assert browser.get("/api/v1/auth/session").status_code == 401


def test_invite_list_and_revoke(app):
    headers = _admin(app)
    assert app.post("/api/v1/auth/invites", json={"email": "invitee" + DOMAIN}, headers=headers).status_code == 201
    token = app.app_state.auth.sender.last_token()
    pending = app.get("/api/v1/admin/invites").json()["invites"]
    mine = [i for i in pending if i["email"] == "invitee" + DOMAIN]
    assert len(mine) == 1 and mine[0]["status"] == "pending" and "token" not in str(mine[0]).lower()
    invite_id = mine[0]["id"]
    assert app.post(f"/api/v1/admin/invites/{invite_id}/revoke", headers=headers).json() == {"success": True}
    again = app.post(f"/api/v1/admin/invites/{invite_id}/revoke", headers=headers)
    assert again.status_code == 409 and again.json() == {"error": "Invite is already revoked"}
    accept = new_browser(app).post("/api/v1/auth/invites/accept", headers=ORIGIN,
                                   json={"token": token, "password": "Invited-Pass-42"})
    assert accept.status_code == 400
    assert [i["status"] for i in app.get("/api/v1/admin/invites", params={"status": "all"}).json()["invites"]
            if i["id"] == invite_id] == ["revoked"]
    assert app.post("/api/v1/admin/invites/not-a-uuid/revoke", headers=headers).status_code == 404
    assert app.get("/api/v1/admin/invites", params={"status": "weird"}).status_code == 400


def test_changes_are_audited_without_secrets(app):
    headers = _admin(app)
    target = make_user("audited" + DOMAIN)
    app.patch(f"{U}/{target}", json={"role": "user"}, headers=headers)
    records = app.app_state.log_capture.records
    assert any("user_updated" in r and target in r for r in records)
    assert all(app.cookies.get("__Host-hc_session") not in r for r in records)


def test_last_sign_in_admin_cannot_be_removed_even_concurrently(strict_scratch_db, env):
    """The API forbids self-demotion; the repository additionally guarantees one sign-in admin remains."""
    only_admin = make_user("onlyadmin" + DOMAIN, role="admin")
    make_user("nopassadmin" + DOMAIN, role="admin", password=None)  # cannot sign in: does not count
    repo = AdminRepository(connection_factory(Settings()))
    with pytest.raises(LastAdminError):
        asyncio.run(repo.update_user(only_admin, {"role": "user"}))
    with pytest.raises(LastAdminError):
        asyncio.run(repo.update_user(only_admin, {"is_active": False}))
    second = make_user("secondadmin" + DOMAIN, role="admin")
    user, changed, _ = asyncio.run(repo.update_user(only_admin, {"role": "user"}))
    assert changed == ["role"] and user["role"] == "user"
    with pytest.raises(LastAdminError):
        asyncio.run(repo.update_user(second, {"is_active": False}))


# ── creator onboarding (CRE-2) ──────────────────────────────────────────────────

C = "/api/v1/admin/creators"


def _creator(slug: str) -> dict | None:
    with db() as conn:
        cur = conn.execute("""
            SELECT streamer_name, user_id, status, moderation_mode, tts_enabled, telegram_moderation_enabled,
                   discord_moderation_enabled, leaderboard_widget_enabled, hyperemotes_enabled, media_upload_enabled,
                   media_moderation_enabled, min_text_amount_inr::int, min_tts_amount_inr::int,
                   min_voice_amount_inr::int, min_hypersound_amount_inr::int, media_min_amount::int,
                   media_max_file_size_mb::int, message_char_tiers, goal_is_active, custom_page_path, razorpay_account,
                   pusher_group, order_prefix, brand_color
            FROM public.streamers WHERE streamer_slug = %s""", (slug,))
        row = cur.fetchone()
        return dict(zip([c.name for c in cur.description], row)) if row else None


def _onboarding(app, accounts=("primary",), groups=()):
    app.app_state.admin.payment_accounts = frozenset(accounts)
    app.app_state.admin.pusher_groups = tuple(groups)
    return _admin(app)


def test_admin_onboards_a_creator_with_the_legacy_launch_defaults(app):
    headers = _onboarding(app)
    r = app.post(C, json={"slug": " AuthTest-New_One ", "name": "  New   One ", "razorpay_account": "primary",
                          "brand_color": "#FF6B35", "order_prefix": "atn_rp_"}, headers=headers)
    assert r.status_code == 201, r.text
    created = r.json()["creator"]
    assert {k: created[k] for k in ("slug", "name", "status", "razorpay_account", "pusher_group", "order_prefix",
                                    "brand_color", "moderation_mode", "claimed")} == {
        "slug": "authtest-new_one", "name": "New One", "status": "pending", "razorpay_account": "primary",
        "pusher_group": 1, "order_prefix": "atn_rp_", "brand_color": "#ff6b35", "moderation_mode": "auto_approve",
        "claimed": False}
    assert _creator("authtest-new_one") == {
        "streamer_name": "New One", "user_id": None, "status": "pending", "moderation_mode": "auto_approve",
        "tts_enabled": True, "telegram_moderation_enabled": False, "discord_moderation_enabled": False,
        "leaderboard_widget_enabled": True, "hyperemotes_enabled": True, "media_upload_enabled": True,
        "media_moderation_enabled": False, "min_text_amount_inr": 40, "min_tts_amount_inr": 40,
        "min_voice_amount_inr": 150, "min_hypersound_amount_inr": 30, "media_min_amount": 100,
        "media_max_file_size_mb": 10,
        "message_char_tiers": [{"min_amount": 40, "max_chars": 200}, {"min_amount": 100, "max_chars": 300},
                               {"min_amount": 500, "max_chars": 500}],
        "goal_is_active": False, "custom_page_path": "/authtest-new_one", "razorpay_account": "primary",
        "pusher_group": 1, "order_prefix": "atn_rp_", "brand_color": "#ff6b35"}
    # pending (owner decision 2026-10-15): not public until an admin activates it; only admins manage it until linked
    assert app.get("/api/v1/public/streamers/authtest-new_one").status_code == 404
    assert app.get("/api/v1/dashboard/streamers/authtest-new_one/settings").status_code == 200  # the admin
    creator_user = new_browser(app)
    signed_in(creator_user, "creator" + DOMAIN)
    assert creator_user.get("/api/v1/dashboard/streamers/authtest-new_one/settings").status_code == 403
    owned = app.post(f"{C}/{created['id']}/owner", json={"user_id": _id("creator" + DOMAIN)}, headers=headers)
    assert owned.status_code == 200 and owned.json()["creator"]["claimed"] is True, owned.text
    assert creator_user.get("/api/v1/dashboard/streamers/authtest-new_one/settings").status_code == 200
    assert any("creator_created" in rec and created["id"] in rec for rec in app.app_state.log_capture.records)


def test_pusher_group_is_the_least_loaded_configured_group(app):
    headers = _onboarding(app, groups=(2, 3))
    with db() as conn:
        counts = dict(conn.execute("SELECT g, (SELECT count(*) FROM public.streamers WHERE pusher_group = g) "
                                   "FROM unnest(ARRAY[2, 3]) g").fetchall())
    expected = min((2, 3), key=lambda g: (counts[g], g))
    created = app.post(C, json={"slug": "authtest-grp", "name": "G", "razorpay_account": "primary"},
                       headers=headers).json()["creator"]
    assert created["pusher_group"] == expected and created["order_prefix"] is None


@pytest.mark.parametrize("body, code, reason", [
    ({"slug": "ab", "name": "N", "razorpay_account": "primary"}, 400, "bad_slug"),
    ({"slug": "authtest bad", "name": "N", "razorpay_account": "primary"}, 400, "bad_slug"),
    ({"slug": "-authtest", "name": "N", "razorpay_account": "primary"}, 400, "bad_slug"),
    ({"slug": "authtest.x", "name": "N", "razorpay_account": "primary"}, 400, "bad_slug"),
    ({"slug": "dashboard", "name": "N", "razorpay_account": "primary"}, 400, "reserved_slug"),
    ({"slug": "Privacy-Policy", "name": "N", "razorpay_account": "primary"}, 400, "reserved_slug"),
    ({"slug": "authtest-x", "name": "   ", "razorpay_account": "primary"}, 400, "bad_name"),
    ({"slug": "authtest-x", "name": "N", "razorpay_account": "secondary"}, 422, None),  # retired (D6)
    ({"slug": "authtest-x", "name": "N", "razorpay_account": "primary", "brand_color": "red"}, 400, "bad_brand_color"),
    ({"slug": "authtest-x", "name": "N", "razorpay_account": "primary", "order_prefix": "x_"}, 400,
     "bad_order_prefix"),
    ({"slug": "authtest-x", "name": "N"}, 422, None),                                  # no default account (D12)
    ({"slug": "authtest-x", "name": "N", "razorpay_account": "tertiary"}, 422, None),
    ({"slug": "authtest-x", "name": "N", "razorpay_account": "primary", "user_id": "x"}, 422, None),
    ({"slug": "authtest-x", "name": "N", "razorpay_account": "primary", "status": "pending"}, 422, None),
    ({"slug": 123, "name": "N", "razorpay_account": "primary"}, 422, None),
])
def test_onboarding_validation(app, body, code, reason):
    headers = _onboarding(app)
    r = app.post(C, json=body, headers=headers)
    assert r.status_code == code, r.text
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.streamers WHERE streamer_slug IN ('authtest-x', 'ab', "
                            "'dashboard', 'privacy-policy')").fetchone()[0] == 0


def test_onboarding_refuses_an_unconfigured_primary_account(app):
    """D12: the account is explicit and must be configured here; nothing falls back to another account."""
    headers = _onboarding(app, accounts=())
    r = app.post(C, json={"slug": "authtest-x", "name": "N", "razorpay_account": "primary"}, headers=headers)
    assert (r.status_code, r.json()) == (400, {"error": "That Razorpay account is not configured on this server"})
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.streamers "
                            "WHERE streamer_slug = 'authtest-x'").fetchone()[0] == 0


def test_onboarding_conflicts_and_access(app):
    headers = _onboarding(app)
    make_streamer("authtest-taken")
    taken = app.post(C, json={"slug": "AuthTest-Taken", "name": "N", "razorpay_account": "primary"}, headers=headers)
    assert (taken.status_code, taken.json()) == (409, {"error": "That slug is already used"})
    first = app.post(C, json={"slug": "authtest-p1", "name": "N", "razorpay_account": "primary",
                              "order_prefix": "pp_rp_"}, headers=headers)
    assert first.status_code == 201
    dup = app.post(C, json={"slug": "authtest-p2", "name": "N", "razorpay_account": "primary",
                            "order_prefix": "pp_rp_"}, headers=headers)
    assert (dup.status_code, dup.json()) == (409, {"error": "That order prefix is already used"})
    assert _creator("authtest-p2") is None
    body = {"slug": "authtest-noaccess", "name": "N", "razorpay_account": "primary"}
    assert app.post(C, json=body, headers=ORIGIN).status_code == 403  # no CSRF token
    plain = new_browser(app)
    plain_headers = signed_in(plain, "plain" + DOMAIN)
    assert plain.post(C, json=body, headers=plain_headers).status_code == 403  # not an admin
    assert new_browser(app).post(C, json=body, headers=ORIGIN).status_code == 401
    assert _creator("authtest-noaccess") is None


def test_concurrent_onboarding_of_one_slug_creates_one_creator(env):
    from app.admin.repository import CreatorConflict
    repo = AdminRepository(connection_factory(Settings()))

    async def create():
        try:
            return await repo.create_creator({"streamer_slug": "authtest-race-new", "streamer_name": "R",
                                              "razorpay_account": "primary", "custom_page_path": "/x"}, ())
        except CreatorConflict as exc:
            return exc.field

    async def race():
        return await asyncio.gather(*(create() for _ in range(4)))
    results = asyncio.run(race())
    assert sum(isinstance(r, dict) for r in results) == 1 and results.count("slug") == 3


def test_owner_assignment_only_for_unclaimed_creators_and_active_users(app):
    headers = _onboarding(app)
    created = app.post(C, json={"slug": "authtest-own", "name": "O", "razorpay_account": "primary"},
                       headers=headers).json()["creator"]
    other = make_streamer("authtest-own-other")
    url = f"{C}/{created['id']}/owner"
    make_user("inactive" + DOMAIN, is_active=False)
    for user_id in (_id("inactive" + DOMAIN), "00000000-0000-4000-8000-000000000000", "not-a-uuid"):
        r = app.post(url, json={"user_id": user_id}, headers=headers)
        assert (r.status_code, r.json()) == (400, {"error": "Unknown or inactive user"}), user_id
    for bad in ("00000000-0000-4000-8000-000000000000", "not-a-uuid"):
        assert app.post(f"{C}/{bad}/owner", json={"user_id": _id("boss" + DOMAIN)}, headers=headers).status_code == 404
    first = make_user("first" + DOMAIN)
    assert app.post(url, json={"user_id": first}, headers=headers).status_code == 200
    r = app.post(url, json={"user_id": make_user("second" + DOMAIN)}, headers=headers)  # never a silent transfer
    assert (r.status_code, r.json()) == (409, {"error": "This creator already has an owner"})
    plain = new_browser(app)
    plain_headers = signed_in(plain, "plain" + DOMAIN)
    r = plain.post(f"{C}/{other}/owner", json={"user_id": _id("plain" + DOMAIN)}, headers=plain_headers)
    assert r.status_code == 403  # not an admin
    assert app.post(f"{C}/{other}/owner", json={"user_id": first}, headers=ORIGIN).status_code == 403  # no CSRF
    with db() as conn:
        owners = dict(conn.execute("SELECT streamer_slug, user_id::text FROM public.streamers "
                                   "WHERE streamer_slug IN ('authtest-own', 'authtest-own-other')").fetchall())
    assert owners == {"authtest-own": first, "authtest-own-other": None}
    assert any("creator_owner_assigned" in rec for rec in app.app_state.log_capture.records)


def test_concurrent_owner_assignments_have_one_winner(env):
    sid = make_streamer("authtest-own-race")
    users = [make_user(f"racer{i}" + DOMAIN) for i in range(4)]
    repo = AdminRepository(connection_factory(Settings()))

    async def race():
        return await asyncio.gather(*(repo.assign_owner(sid, u) for u in users))
    results = [r for r, _ in asyncio.run(race())]
    assert sorted(results) == ["claimed", "claimed", "claimed", "ok"]
