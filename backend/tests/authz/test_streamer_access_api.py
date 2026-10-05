"""Creator access through the real app against a scratch database (see tests/integration.py)."""

from tests.integration import DOMAIN, db, login, make_streamer, make_user, new_browser, requires_db, signed_in

pytestmark = requires_db


def _access(client, ref):
    return client.get(f"/api/v1/streamers/{ref}/access")


def test_owner_linked_admin_and_others(app):
    owner = make_user("owner" + DOMAIN)
    sid = make_streamer("authtest-alpha", user_id=owner)
    make_user("linked" + DOMAIN, streamer_id=sid)
    make_user("stranger" + DOMAIN)
    make_user("boss" + DOMAIN, role="admin")

    expectations = {"owner": (200, "owner"), "linked": (200, "linked"), "boss": (200, "admin"),
                    "stranger": (403, None)}
    for name, (status, via) in expectations.items():
        browser = new_browser(app)
        assert login(browser, name + DOMAIN).status_code == 200
        for ref in ("authtest-alpha", "AUTHTEST-ALPHA", sid):
            r = _access(browser, ref)
            assert r.status_code == status, (name, ref, r.text)
            if via:
                assert r.json() == {"streamer": {"id": sid, "slug": "authtest-alpha", "name": "Auth Test"},
                                    "access": via}
            else:
                assert r.json() == {"error": "Forbidden"}


def test_unclaimed_creator_is_admin_only_even_for_linked_account(app):
    sid = make_streamer("authtest-unclaimed")
    make_user("linked" + DOMAIN, streamer_id=sid)
    browser = new_browser(app)
    login(browser, "linked" + DOMAIN)
    r = _access(browser, "authtest-unclaimed")
    assert r.status_code == 403 and r.json() == {"error": "This creator account is not claimed yet"}
    admin = new_browser(app)
    signed_in(admin, "boss" + DOMAIN, role="admin")
    assert _access(admin, "authtest-unclaimed").json()["access"] == "admin"


def test_unknown_malformed_and_anonymous(app):
    signed_in(app, "someone" + DOMAIN)
    assert _access(app, "authtest-does-not-exist").status_code == 404
    assert _access(app, "00000000-0000-4000-8000-000000000000").status_code == 404
    assert _access(app, "bad%20slug").status_code == 404
    anonymous = new_browser(app)
    assert _access(anonymous, "authtest-anything").status_code == 401


def test_access_is_lost_when_ownership_or_account_changes(app):
    owner = make_user("owner" + DOMAIN)
    sid = make_streamer("authtest-beta", user_id=owner)
    login(app, "owner" + DOMAIN)
    assert _access(app, sid).status_code == 200
    with db() as conn:
        conn.execute("UPDATE public.streamers SET user_id = NULL WHERE id = %s", (sid,))
    assert _access(app, sid).status_code == 403
    with db() as conn:
        conn.execute("UPDATE public.streamers SET user_id = %s WHERE id = %s", (owner, sid))
        conn.execute("UPDATE public.users SET is_active = false WHERE id = %s", (owner,))
    assert _access(app, sid).status_code == 401  # deactivated: the session no longer resolves


def test_my_streamers_lists_owned_and_linked_only(app):
    owner = make_user("owner" + DOMAIN)
    a = make_streamer("authtest-a", user_id=owner, name="A")
    make_streamer("authtest-b", user_id=owner, name="B")
    other = make_user("other" + DOMAIN)
    c = make_streamer("authtest-c", user_id=other, name="C")
    make_streamer("authtest-unowned", name="D")
    make_user("member" + DOMAIN, streamer_id=c)

    login(app, "owner" + DOMAIN)
    r = app.get("/api/v1/me/streamers").json()
    assert r["is_admin"] is False and [s["slug"] for s in r["streamers"]] == ["authtest-a", "authtest-b"]
    assert r["streamers"][0] == {"id": a, "slug": "authtest-a", "name": "A"}

    member = new_browser(app)
    login(member, "member" + DOMAIN)
    assert [s["slug"] for s in member.get("/api/v1/me/streamers").json()["streamers"]] == ["authtest-c"]

    admin = new_browser(app)
    signed_in(admin, "boss" + DOMAIN, role="admin")
    assert admin.get("/api/v1/me/streamers").json() == {"is_admin": True, "streamers": []}
    assert new_browser(app).get("/api/v1/me/streamers").status_code == 401


def test_denials_are_logged_without_secrets(app):
    owner = make_user("owner" + DOMAIN)
    make_streamer("authtest-gamma", user_id=owner)
    signed_in(app, "stranger" + DOMAIN)
    assert _access(app, "authtest-gamma").status_code == 403
    records = app.app_state.log_capture.records
    assert any("streamer_access_denied" in r and "not_owner" in r for r in records)
    token = app.cookies.get("__Host-hc_session")
    assert all(token not in r for r in records)
