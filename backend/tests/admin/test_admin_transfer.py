"""D13: admin creator ownership transfer (POST /api/v1/admin/creators/{id}/owner/transfer) through the real app, plus
the owner-specific Custom Soundboard terms (migration 0019). Scratch database, synthetic rows only."""

import asyncio
import json
import threading
import time

import pytest

from app.admin.repository import AdminRepository
from app.cli import connection_factory
from app.core.config import Settings
from app.soundboard import terms
from tests.integration import (
    DOMAIN,
    ORIGIN,
    accept_soundboard_terms,
    db,
    login,
    make_donation,
    make_streamer,
    make_user,
    new_browser,
    requires_db,
    signed_in,
)

pytestmark = requires_db
SLUG = "authtest-d13"


def _url(sid: str) -> str:
    return f"/api/v1/admin/creators/{sid}/owner/transfer"


def _admin(app) -> dict:
    return signed_in(app, "d13-boss" + DOMAIN, role="admin")


def _user_id(email: str) -> str:
    with db() as conn:
        return conn.execute("SELECT id::text FROM public.users WHERE email = %s", (email,)).fetchone()[0]


def _signed_in_user(app, name: str, **kwargs):
    browser = new_browser(app)
    headers = signed_in(browser, name + DOMAIN, **kwargs)
    return browser, headers, _user_id(name + DOMAIN)


def _creator_with_history(owner: str) -> str:
    """A creator owned (and linked both ways, as every migrated creator is) by `owner`, with donations, payment
    health, OBS tokens and a custom soundboard slot."""
    sid = make_streamer(SLUG, user_id=owner)
    with db() as conn:
        conn.execute("UPDATE public.users SET streamer_id = %s WHERE id = %s", (sid, owner))
        conn.execute("UPDATE public.streamers SET razorpay_account = 'primary', soundboard_enabled = true, "
                     "tts_enabled = false, brand_color = '#123456' WHERE id = %s", (sid,))
        conn.execute("INSERT INTO public.streamer_payment_health (streamer_id, status) VALUES (%s, 'ok')", (sid,))
        conn.execute("INSERT INTO public.obs_tokens (streamer_id, token_hash) VALUES (%s, 'd13-active-1'), "
                     "(%s, 'd13-active-2')", (sid, sid))
        conn.execute("INSERT INTO public.obs_tokens (streamer_id, token_hash, is_active, revoked_at) "
                     "VALUES (%s, 'd13-revoked', false, now())", (sid,))
        conn.execute("INSERT INTO public.streamer_soundboard_slots (streamer_slug, source, display_name, audio_key, "
                     "audio_url, audio_duration_s, image_key, image_url, image_type, is_visible, order_index) VALUES (%s, "
                     "'custom', 'D13', 'authtest-d13/a.mp3', 'https://x.invalid/a.mp3', 2.5, 'authtest-d13/i.png', "
                     "'https://x.invalid/i.png', 'png', true, 1)", (SLUG,))
    for amount in (100, 250):
        make_donation(sid, amount)
    with db() as conn:
        conn.execute("UPDATE public.donations SET payment_account = 'primary', provider_order_id = 'order_d13' || "
                     "amount::int WHERE streamer_id = %s", (sid,))
    return sid


def _snapshot(sid: str) -> dict:
    """Everything a transfer must leave alone, as JSON (the owner column and its timestamp excluded)."""
    q = {
        "creator": "SELECT to_jsonb(s) - 'user_id' - 'updated_at' FROM public.streamers s WHERE id = %(s)s",
        "donations": "SELECT jsonb_agg(to_jsonb(d) ORDER BY d.id) FROM public.donations d WHERE streamer_id = %(s)s",
        "totals": "SELECT jsonb_agg(to_jsonb(t) ORDER BY t.donator_name) FROM public.streamer_donator_totals t "
                  "WHERE streamer_id = %(s)s",
        "health": "SELECT to_jsonb(h) FROM public.streamer_payment_health h WHERE streamer_id = %(s)s",
        "obs": "SELECT jsonb_agg(to_jsonb(o) ORDER BY o.token_hash) FROM public.obs_tokens o WHERE streamer_id = %(s)s",
        "slots": "SELECT jsonb_agg(to_jsonb(x) ORDER BY x.id) FROM public.streamer_soundboard_slots x "
                 "WHERE streamer_slug = %(slug)s",
        "terms": "SELECT jsonb_agg(to_jsonb(t) ORDER BY t.id) FROM public.soundboard_tnc_acceptances t "
                 "WHERE streamer_id = %(s)s",
    }
    with db() as conn:
        return {k: json.dumps(conn.execute(sql, {"s": sid, "slug": SLUG}).fetchone()[0], sort_keys=True, default=str)
                for k, sql in q.items()}


def _account(user_id: str) -> tuple:
    with db() as conn:
        return conn.execute("SELECT email::text, role, is_active, streamer_id::text, password_hash, "
                            "(SELECT count(*) FROM public.user_identities WHERE user_id = u.id), "
                            "(SELECT count(*) FROM public.password_reset_tokens WHERE user_id = u.id AND used_at IS NULL) "
                            "FROM public.users u WHERE id = %s", (user_id,)).fetchone()


def _owner_of(sid: str) -> str | None:
    with db() as conn:
        return conn.execute("SELECT user_id::text FROM public.streamers WHERE id = %s", (sid,)).fetchone()[0]


def _access(client, slug: str = SLUG) -> int:
    return client.get(f"/api/v1/streamers/{slug}/access").status_code


def test_transfer_moves_the_creator_and_only_the_creator(app):
    headers = _admin(app)
    old, _, old_id = _signed_in_user(app, "d13-old")
    second_old_session = new_browser(app)
    assert login(second_old_session, "d13-old" + DOMAIN).status_code == 200
    other = make_streamer("authtest-d13-other")
    new, _, new_id = _signed_in_user(app, "d13-new", role="user", streamer_id=other)  # linked elsewhere: untouched
    sid = _creator_with_history(old_id)
    with db() as conn:  # the old owner's reset link and Google link stay theirs
        conn.execute("INSERT INTO public.password_reset_tokens (user_id, token_hash, expires_at) "
                     "VALUES (%s, 'd13-reset', now() + interval '30 minutes')", (old_id,))
        conn.execute("INSERT INTO public.user_identities (user_id, provider, provider_subject) "
                     "VALUES (%s, 'google', 'd13-sub')", (old_id,))
    before, old_before, new_before = _snapshot(sid), _account(old_id), _account(new_id)
    assert _access(old) == 200 and _access(new) == 403

    r = app.post(_url(sid), json={"user_id": new_id, "expected_owner_id": old_id}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {k: body[k] for k in ("changed", "previous_owner_id", "sessions_revoked", "old_owner_unlinked",
                                 "active_obs_tokens")} == {
        "changed": True, "previous_owner_id": old_id, "sessions_revoked": 2, "old_owner_unlinked": True,
        "active_obs_tokens": 2}
    assert body["creator"]["id"] == sid and body["creator"]["claimed"] is True
    assert "email" not in r.text and "@" not in r.text and "password" not in r.text
    assert _owner_of(sid) == new_id
    # The old owner: unlinked, signed out everywhere, nothing else changed; refused on the next request.
    assert _account(old_id) == (old_before[0], old_before[1], True, None, old_before[4], 1, 1)
    assert old.get("/api/v1/auth/session").status_code == 401
    assert second_old_session.get("/api/v1/auth/session").status_code == 401
    again = new_browser(app)
    assert login(again, "d13-old" + DOMAIN).status_code == 200 and _access(again) == 403
    # The new owner: access at once on the existing session; account untouched (role, link, everything).
    assert new.get("/api/v1/auth/session").status_code == 200 and _access(new) == 200
    assert _account(new_id) == new_before
    assert new.get("/api/v1/me/streamers").status_code == 200
    # Creator data, payments, media, OBS tokens and soundboard rows: identical.
    assert _snapshot(sid) == before


def test_an_old_owner_linked_elsewhere_keeps_that_link_but_loses_the_creator(app):
    headers = _admin(app)
    elsewhere = make_streamer("authtest-d13-else", user_id=make_user("d13-else-owner" + DOMAIN))  # claimed
    old, _, old_id = _signed_in_user(app, "d13-old2")
    sid = make_streamer(SLUG, user_id=old_id)
    with db() as conn:
        conn.execute("UPDATE public.users SET streamer_id = %s WHERE id = %s", (elsewhere, old_id))
    new_id = make_user("d13-new2" + DOMAIN)
    r = app.post(_url(sid), json={"user_id": new_id, "expected_owner_id": old_id}, headers=headers)
    assert r.status_code == 200 and r.json()["old_owner_unlinked"] is False and r.json()["active_obs_tokens"] == 0
    assert _account(old_id)[3] == elsewhere
    fresh = new_browser(app)
    login(fresh, "d13-old2" + DOMAIN)
    assert _access(fresh) == 403 and _access(fresh, "authtest-d13-else") == 200


def test_access_and_csrf(app):
    old_id, new_id = make_user("d13-o" + DOMAIN), make_user("d13-n" + DOMAIN)
    sid = make_streamer(SLUG, user_id=old_id)
    body = {"user_id": new_id, "expected_owner_id": old_id}
    assert new_browser(app).post(_url(sid), json=body, headers=ORIGIN).status_code == 401
    plain, plain_headers, _ = _signed_in_user(app, "d13-plain")
    assert plain.post(_url(sid), json=body, headers=plain_headers).status_code == 403
    headers = _admin(app)
    assert app.post(_url(sid), json=body, headers=ORIGIN).status_code == 403  # no CSRF token
    assert app.post(_url(sid), json=body, headers={**headers, "origin": "https://evil.invalid"}).status_code == 403
    assert _owner_of(sid) == old_id


def test_refusals_change_nothing(app):
    headers = _admin(app)
    old_id, new_id = make_user("d13-o" + DOMAIN), make_user("d13-n" + DOMAIN)
    inactive = make_user("d13-off" + DOMAIN, is_active=False)
    sid = _creator_with_history(old_id)
    unclaimed = make_streamer("authtest-d13-free")
    before = _snapshot(sid)
    missing = "00000000-0000-4000-8000-000000000000"
    cases = [
        (missing, {"user_id": new_id, "expected_owner_id": old_id}, 404, "Creator not found"),
        ("not-a-uuid", {"user_id": new_id, "expected_owner_id": old_id}, 404, "Creator not found"),
        (unclaimed, {"user_id": new_id, "expected_owner_id": old_id}, 409,
         "This creator has no owner; use POST /api/v1/admin/creators/{id}/owner"),
        (sid, {"user_id": new_id, "expected_owner_id": new_id}, 409,
         "The creator's current owner is not expected_owner_id"),
        (sid, {"user_id": missing, "expected_owner_id": old_id}, 400, "Unknown or inactive user"),
        (sid, {"user_id": inactive, "expected_owner_id": old_id}, 400, "Unknown or inactive user"),
        (sid, {"user_id": "nope", "expected_owner_id": old_id}, 400, "user_id and expected_owner_id must be user ids"),
    ]
    for creator, body, code, error in cases:
        r = app.post(_url(creator), json=body, headers=headers)
        assert (r.status_code, r.json()) == (code, {"error": error}), (creator, body)
    for body in ({"user_id": new_id}, {"user_id": new_id, "expected_owner_id": old_id, "role": "admin"},
                 {"user_id": 1, "expected_owner_id": old_id}):
        assert app.post(_url(sid), json=body, headers=headers).status_code == 422
    assert _owner_of(sid) == old_id and _owner_of(unclaimed) is None and _snapshot(sid) == before
    assert _account(old_id)[3] is not None  # still linked
    assert not any("creator_owner_transferred" in r for r in app.app_state.log_capture.records)


def test_transferring_to_the_current_owner_is_a_no_op(app):
    headers = _admin(app)
    owner, _, owner_id = _signed_in_user(app, "d13-same")
    sid = _creator_with_history(owner_id)
    before, account = _snapshot(sid), _account(owner_id)
    r = app.post(_url(sid), json={"user_id": owner_id, "expected_owner_id": owner_id}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert (body["changed"], body["sessions_revoked"], body["old_owner_unlinked"]) == (False, 0, False)
    assert _snapshot(sid) == before and _account(owner_id) == account and _access(owner) == 200
    assert not any("creator_owner_transferred" in r for r in app.app_state.log_capture.records)


def test_the_transfer_is_audited_with_ids_only(app):
    headers = _admin(app)
    old_id, new_id = make_user("d13-ao" + DOMAIN), make_user("d13-an" + DOMAIN)
    sid = make_streamer(SLUG, user_id=old_id)
    app.post(_url(sid), json={"user_id": new_id, "expected_owner_id": old_id}, headers=headers)
    events = [r for r in app.app_state.log_capture.records if "creator_owner_transferred" in r]
    assert len(events) == 1
    assert all(f"'{k}': '{v}'" in events[0] for k, v in
               (("previous_user_id", old_id), ("target_user_id", new_id), ("streamer_id", sid)))
    assert not [r for r in app.app_state.log_capture.records if DOMAIN in r]


# ── soundboard terms are owner-specific (migration 0019) ────────────────────────

def _terms(client) -> dict:
    return client.get(f"/api/v1/dashboard/streamers/{SLUG}/soundboard/terms").json()


def _usable_publicly(app) -> bool:
    return app.get(f"/api/v1/public/streamers/{SLUG}").json()["soundboard_enabled"]


def test_soundboard_needs_the_new_owners_own_acceptance(app):
    headers = _admin(app)
    old, _, old_id = _signed_in_user(app, "d13-sb-old")
    new, new_headers, new_id = _signed_in_user(app, "d13-sb-new")
    sid = make_streamer(SLUG, user_id=old_id)
    with db() as conn:
        conn.execute("UPDATE public.streamers SET soundboard_enabled = true WHERE id = %s", (sid,))
    accept_soundboard_terms(sid)  # the old owner's acceptance
    assert _usable_publicly(app) and _terms(old)["usable"] is True

    assert app.post(_url(sid), json={"user_id": new_id, "expected_owner_id": old_id}, headers=headers).json()["changed"]
    state = _terms(new)
    assert (state["eligible"], state["accepted"], state["usable"], state["can_accept"]) == (True, False, False, True)
    assert not _usable_publicly(app)
    with db() as conn:  # the old acceptance is history: kept, unrevoked
        assert conn.execute("SELECT user_id::text, revoked_at FROM public.soundboard_tnc_acceptances "
                            "WHERE streamer_id = %s", (sid,)).fetchall() == [(old_id, None)]
    r = new.post(f"/api/v1/dashboard/streamers/{SLUG}/soundboard/terms/accept", json={"version": terms.VERSION},
                 headers=new_headers)
    assert r.status_code == 200 and r.json()["usable"] is True and _usable_publicly(app)
    # Back to the previous owner: their own earlier acceptance applies again.
    r = app.post(_url(sid), json={"user_id": old_id, "expected_owner_id": new_id}, headers=headers)
    assert r.json()["changed"] and _usable_publicly(app)
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.soundboard_tnc_acceptances WHERE streamer_id = %s "
                            "AND terms_version = %s", (sid, terms.VERSION)).fetchone()[0] == 2


def test_an_acceptance_by_anyone_but_the_owner_never_counts(app):
    owner_id = make_user("d13-sb-owner" + DOMAIN)
    sid = make_streamer(SLUG, user_id=owner_id)
    with db() as conn:
        conn.execute("UPDATE public.streamers SET soundboard_enabled = true WHERE id = %s", (sid,))
    accept_soundboard_terms(sid, user_id=make_user("d13-sb-other" + DOMAIN))
    assert not _usable_publicly(app)
    accept_soundboard_terms(sid)
    assert _usable_publicly(app)


# ── concurrency (repository level) ──────────────────────────────────────────────

def _repo() -> AdminRepository:
    return AdminRepository(connection_factory(Settings()), auth_factory=connection_factory(Settings(), "auth"))


def test_two_competing_transfers_have_one_winner(env):
    old_id = make_user("d13-race-old" + DOMAIN)
    targets = [make_user(f"d13-race{i}" + DOMAIN) for i in range(3)]
    sid = make_streamer(SLUG, user_id=old_id)

    async def race():
        repo = _repo()
        return await asyncio.gather(*(repo.transfer_owner(sid, t, old_id) for t in targets))
    results = [r for r, _, _ in asyncio.run(race())]
    assert sorted(results) == ["ok", "owner_changed", "owner_changed"]
    assert _owner_of(sid) == targets[results.index("ok")]


def _in_thread(coro_factory) -> dict:
    out: dict = {}

    def run():
        out["result"] = asyncio.run(coro_factory())
    thread = threading.Thread(target=run)
    thread.start()
    out["thread"] = thread
    return out


def test_a_deactivation_committing_first_refuses_the_transfer(env):
    old_id, new_id = make_user("d13-deact-old" + DOMAIN), make_user("d13-deact-new" + DOMAIN)
    sid = make_streamer(SLUG, user_id=old_id)
    with db() as conn, conn.transaction():
        conn.execute("UPDATE public.users SET is_active = false WHERE id = %s", (new_id,))  # holds the target row
        pending = _in_thread(lambda: _repo().transfer_owner(sid, new_id, old_id))
        time.sleep(0.5)
        assert "result" not in pending  # waits for the deactivation
    pending["thread"].join(10)
    assert pending["result"][0] == "bad_user" and _owner_of(sid) == old_id


def test_a_streamer_link_change_in_flight_neither_deadlocks_nor_survives_the_transfer(env):
    """PATCH users.streamer_id locks the user row, then its foreign key key-share-locks the creator. The transfer
    locks the creator FOR NO KEY UPDATE first, then the user: compatible, so it simply waits for the PATCH (a FOR
    UPDATE on the creator would deadlock here)."""
    old_id, new_id = make_user("d13-link-old" + DOMAIN), make_user("d13-link-new" + DOMAIN)
    sid = make_streamer(SLUG, user_id=old_id)
    with db() as conn, conn.transaction():
        conn.execute("SELECT 1 FROM public.users WHERE id = %s FOR UPDATE", (old_id,))
        pending = _in_thread(lambda: _repo().transfer_owner(sid, new_id, old_id))
        time.sleep(0.5)  # the transfer now holds the creator and waits for the old owner's row
        conn.execute("SET LOCAL lock_timeout = '5s'")
        conn.execute("UPDATE public.users SET streamer_id = %s WHERE id = %s", (sid, old_id))  # FK: key share
        assert "result" not in pending
    pending["thread"].join(10)
    result, _, counts = pending["result"]
    assert result == "ok" and counts["old_owner_unlinked"] is True
    assert _owner_of(sid) == new_id and _account(old_id)[3] is None


@pytest.mark.parametrize("first", ["transfer", "deactivate"])
def test_transfer_and_deactivation_racing_stay_consistent(env, first):
    old_id, new_id = make_user("d13-rc-old" + DOMAIN), make_user("d13-rc-new" + DOMAIN)
    sid = make_streamer(SLUG, user_id=old_id)

    async def race():
        repo = _repo()
        jobs = [repo.transfer_owner(sid, new_id, old_id), repo.update_user(new_id, {"is_active": False})]
        return await asyncio.gather(*(jobs if first == "transfer" else reversed(jobs)))
    results = asyncio.run(race())
    transfer = next(r for r in results if isinstance(r[0], str))[0]
    assert transfer in ("ok", "bad_user")
    assert _owner_of(sid) == (new_id if transfer == "ok" else old_id)
