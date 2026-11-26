"""T-3 (Phase 2 audit §3.3, SEC-C): owner decision 2026-11-25, a linked account (users.streamer_id) keeps FULL owner
power. Pinned on the writes the audit named: settings PATCH (moderation off included), moderators add/remove, OBS
token create/delete, and the CSV export. A signed-in stranger is 403 on each and changes nothing."""

from datetime import UTC, datetime

import pytest

from tests.integration import (
    DOMAIN,
    creator_state,
    csrf_headers,
    db,
    login,
    make_donation,
    make_streamer,
    make_user,
    new_browser,
    requires_db,
)

pytestmark = requires_db
S = "/api/v1/dashboard/streamers"
EXPORT = "/donations/export.csv?from=2026-10-01&to=2026-10-02"


def _browser(app, email: str):
    b = new_browser(app)
    r = login(b, email)
    assert r.status_code == 200, r.text
    return b, csrf_headers(r)


def _tokens(sid: str) -> int:
    with db() as conn:
        return conn.execute("SELECT count(*) FROM public.obs_tokens WHERE streamer_id = %s AND revoked_at IS NULL",
                            (sid,)).fetchone()[0]


@pytest.mark.parametrize("who", ["owner", "linked"])
def test_a_linked_account_has_the_same_power_as_the_owner(app, who):
    slug = f"authtest-t3-{who}"
    sid = make_streamer(slug, user_id=make_user(f"t3-{who}-owner" + DOMAIN))
    make_user(f"t3-{who}-linked" + DOMAIN, streamer_id=sid)
    make_donation(sid, 50, moderation="approved", at=datetime(2026, 10, 1, 12, tzinfo=UTC), name="T3 Fan",
                  message="t3 message")
    app.app_state.obs.public_base = "https://media.example.test"  # no R2 in the scratch app
    b, h = _browser(app, f"t3-{who}-{who}" + DOMAIN)  # t3-owner-owner or t3-linked-linked
    base = f"{S}/{slug}"

    # A new creator auto-approves with media moderation on: both values here differ from it.
    r = b.patch(f"{base}/settings", json={"moderation_mode": "manual", "media_moderation_enabled": False}, headers=h)
    assert r.status_code == 200 and {"moderation_mode", "media_moderation_enabled"} <= set(r.json()["changed"]), r.text
    with db() as conn:
        assert conn.execute("SELECT moderation_mode, media_moderation_enabled FROM public.streamers WHERE id = %s",
                            (sid,)).fetchone() == ("manual", False)

    r = b.post(f"{base}/moderators", json={"mod_name": "T3 Mod", "telegram_user_id": "987654321"}, headers=h)
    assert r.status_code == 201, r.text
    r = b.delete(f"{base}/moderators/{r.json()['moderator_id']}", headers=h)
    assert (r.status_code, r.json()["result"]) == (200, "removed"), r.text

    before = _tokens(sid)
    r = b.post(f"{base}/obs-tokens", headers=h)
    assert r.status_code == 201, r.text
    assert _tokens(sid) == before + 1
    r = b.delete(f"{base}/obs-tokens/{r.json()['token']['id']}", headers=h)
    assert (r.status_code, r.json()["token"]["state"]) == (200, "revoked"), r.text

    r = b.get(f"{base}{EXPORT}")
    assert r.status_code == 200 and "T3 Fan" in r.text and "t3 message" in r.text


def test_a_stranger_is_403_on_each_and_changes_nothing(app):
    slug = "authtest-t3-stranger"
    sid = make_streamer(slug, user_id=make_user("t3-s-owner" + DOMAIN))
    make_donation(sid, 50, moderation="approved", at=datetime(2026, 10, 1, 12, tzinfo=UTC), name="T3 Fan")
    with db() as conn:
        moderator = conn.execute("INSERT INTO public.streamers_moderators (streamer_id, mod_name, telegram_user_id) "
                                 "VALUES (%s, 'Fixture mod', '12345') RETURNING id::text", (sid,)).fetchone()[0]
    app.app_state.obs.public_base = "https://media.example.test"
    owner, owner_h = _browser(app, "t3-s-owner" + DOMAIN)
    token = owner.post(f"{S}/{slug}/obs-tokens", headers=owner_h).json()["token"]["id"]
    make_user("t3-stranger" + DOMAIN)
    b, h = _browser(app, "t3-stranger" + DOMAIN)
    before, tokens = creator_state(sid), _tokens(sid)
    base = f"{S}/{slug}"
    calls = [
        ("PATCH", "/settings", {"moderation_mode": "manual", "media_moderation_enabled": False}),
        ("POST", "/moderators", {"mod_name": "Evil", "telegram_user_id": "111111111"}),
        ("DELETE", f"/moderators/{moderator}", None),
        ("POST", "/obs-tokens", None),
        ("DELETE", f"/obs-tokens/{token}", None),
        ("GET", EXPORT, None),
    ]
    for method, path, body in calls:
        kwargs = {"json": body} if body is not None else {}
        if method != "GET":
            kwargs["headers"] = h
        r = b.request(method, f"{base}{path}", **kwargs)
        assert (r.status_code, r.json()) == (403, {"error": "Forbidden"}), (method, path, r.text)
    assert creator_state(sid) == before and _tokens(sid) == tokens
