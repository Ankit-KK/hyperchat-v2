"""Authorization, CSRF, caching and privacy across EVERY creator dashboard endpoint (scratch database).

ENDPOINTS lists each route once; the matrix tests below run all of them for every kind of caller.
"""

import itertools
import json
from datetime import UTC, datetime

from tests.integration import (
    DOMAIN,
    ORIGIN,
    csrf_headers,
    db,
    login,
    make_donation,
    make_streamer,
    make_user,
    new_browser,
    requires_db,
    update_streamer,
)

pytestmark = requires_db
S = "/api/v1/dashboard/streamers"
_n = itertools.count(1)

# (method, path below the creator, body factory or None). Path placeholders come from _fixtures().
ENDPOINTS = [
    ("GET", "/settings", None),
    ("PATCH", "/settings", lambda n: {"goal_overlay_id": n % 7}),
    ("GET", "/donations", None),
    ("GET", "/donations/stats", None),
    ("GET", "/donations/export.csv?from=2026-10-01&to=2026-10-02", None),
]
WRITES = [e for e in ENDPOINTS if e[0] != "GET"]
READS = [e for e in ENDPOINTS if e[0] == "GET"]
PLANTED = ("authtest-rzp-order", "authtest-status-hash", "authtest-upload-hash", "authtest_legacy_table")


def _fixtures(sid: str) -> dict:
    """Rows of this creator that the endpoints act on (with internal columns that must never be served)."""
    donation = make_donation(sid, 50, moderation="approved", at=datetime(2026, 10, 1, 12, tzinfo=UTC), name="Fan",
                             message="authtest message")
    with db() as conn:
        conn.execute("UPDATE public.donations SET provider_order_id = %s, status_token_hash = %s, "
                     "upload_token_hash = %s, legacy_source_table = %s WHERE id = %s", (*PLANTED, donation))
    return {"donation": donation}


def _call(browser, method: str, ref: str, path: str, body, ids: dict, headers: dict):
    kwargs = {"json": body(next(_n))} if body else {}
    if method != "GET":
        kwargs["headers"] = headers
    return browser.request(method, f"{S}/{ref}{path.format(**ids)}", **kwargs)


def state(sid: str) -> str:
    """Everything a dashboard write could touch for one creator, as comparable text."""
    with db() as conn:
        return conn.execute("""
            SELECT json_build_array(
              (SELECT row_to_json(s) FROM public.streamers s WHERE s.id = %(s)s),
              (SELECT json_agg(d ORDER BY d.id) FROM public.donations d WHERE d.streamer_id = %(s)s),
              (SELECT json_agg(m ORDER BY m.id) FROM public.streamers_moderators m WHERE m.streamer_id = %(s)s),
              (SELECT json_agg(b ORDER BY b.id) FROM public.banned_donors b WHERE b.streamer_id = %(s)s),
              (SELECT json_agg(t ORDER BY t.id) FROM public.streamer_donator_totals t WHERE t.streamer_id = %(s)s),
              (SELECT json_agg(a ORDER BY a.id) FROM public.moderation_actions a WHERE a.streamer_id = %(s)s)
            )::text""", {"s": sid}).fetchone()[0]


def _signed_in_browser(app, email: str) -> tuple:
    browser = new_browser(app)
    resp = login(browser, email)
    assert resp.status_code == 200, resp.text
    return browser, csrf_headers(resp)


def test_owner_linked_admin_allowed_stranger_forbidden_anonymous_unauthenticated(app):
    sid = make_streamer("authtest-matrix", user_id=make_user("owner" + DOMAIN))
    make_user("linked" + DOMAIN, streamer_id=sid)
    make_user("stranger" + DOMAIN)
    make_user("boss" + DOMAIN, role="admin")
    ids = _fixtures(sid)
    for who, allowed in (("owner", True), ("linked", True), ("boss", True), ("stranger", False)):
        browser, headers = _signed_in_browser(app, who + DOMAIN)
        for (method, path, body), ref in itertools.product(ENDPOINTS, ("authtest-matrix", sid)):
            r = _call(browser, method, ref, path, body, ids, headers)
            assert r.headers["cache-control"] == "no-store", (who, method, path)
            if allowed:
                assert r.status_code in (200, 201), (who, method, path, r.text)
            else:
                assert (r.status_code, r.json()) == (403, {"error": "Forbidden"}), (who, method, path)
    anonymous = new_browser(app)
    for method, path, body in ENDPOINTS:
        r = _call(anonymous, method, "authtest-matrix", path, body, ids, ORIGIN)
        assert (r.status_code, r.json()) == (401, {"error": "Not signed in"}), (method, path)
        assert r.headers["cache-control"] == "no-store"


def test_stranger_writes_change_nothing(app):
    sid = make_streamer("authtest-matrix2", user_id=make_user("owner" + DOMAIN))
    ids = _fixtures(sid)
    before = state(sid)
    make_user("stranger" + DOMAIN)
    browser, headers = _signed_in_browser(app, "stranger" + DOMAIN)
    for method, path, body in WRITES:
        assert _call(browser, method, sid, path, body, ids, headers).status_code == 403
    assert state(sid) == before


def test_unknown_and_malformed_creators_are_404(app):
    make_user("boss" + DOMAIN, role="admin")
    browser, headers = _signed_in_browser(app, "boss" + DOMAIN)
    for ref in ("authtest-nobody", "00000000-0000-4000-8000-000000000000", "Bad%20Slug", "a.b", "x" * 70):
        for method, path, body in ENDPOINTS:
            r = _call(browser, method, ref, path, body, {k: "00000000-0000-4000-8000-000000000000"
                                                          for k in ("donation", "moderator")}, headers)
            assert (r.status_code, r.json()) == (404, {"error": "Streamer not found"}), (ref, method, path)
            assert r.headers["cache-control"] == "no-store"


def test_unclaimed_creator_is_admin_only(app):
    sid = make_streamer("authtest-unclaimed")
    ids = _fixtures(sid)
    make_user("linked" + DOMAIN, streamer_id=sid)
    make_user("boss" + DOMAIN, role="admin")
    linked, linked_headers = _signed_in_browser(app, "linked" + DOMAIN)
    admin, admin_headers = _signed_in_browser(app, "boss" + DOMAIN)
    for method, path, body in ENDPOINTS:
        r = _call(linked, method, sid, path, body, ids, linked_headers)
        assert (r.status_code, r.json()) == (403, {"error": "This creator account is not claimed yet"})
        assert _call(admin, method, sid, path, body, ids, admin_headers).status_code in (200, 201), (method, path)


def test_pending_and_rejected_creators_keep_their_dashboard(app):
    for status in ("pending", "rejected"):
        email = f"owner-{status}" + DOMAIN
        sid = make_streamer(f"authtest-{status}", user_id=make_user(email))
        update_streamer(sid, status=status)
        ids = _fixtures(sid)
        browser, headers = _signed_in_browser(app, email)
        for method, path, body in ENDPOINTS:
            r = _call(browser, method, sid, path, body, ids, headers)
            assert r.status_code in (200, 201), (status, method, path, r.text)


def test_deactivated_owner_is_signed_out(app):
    owner = make_user("owner" + DOMAIN)
    sid = make_streamer("authtest-inactive", user_id=owner)
    ids = _fixtures(sid)
    browser, headers = _signed_in_browser(app, "owner" + DOMAIN)
    with db() as conn:
        conn.execute("UPDATE public.users SET is_active = false WHERE id = %s", (owner,))
    before = state(sid)
    for method, path, body in ENDPOINTS:
        assert _call(browser, method, sid, path, body, ids, headers).status_code == 401, (method, path)
    assert state(sid) == before


def test_every_write_needs_csrf_and_an_allowed_origin(app):
    sid = make_streamer("authtest-csrf-all", user_id=make_user("owner" + DOMAIN))
    ids = _fixtures(sid)
    browser, headers = _signed_in_browser(app, "owner" + DOMAIN)
    before = state(sid)
    for method, path, body in WRITES:
        for bad in (ORIGIN, {**headers, "x-csrf-token": "wrong"}, {**headers, "origin": "https://evil.invalid"},
                    {"x-csrf-token": headers["x-csrf-token"]}):
            r = _call(browser, method, sid, path, body, ids, bad)
            assert r.status_code == 403 and r.headers["cache-control"] == "no-store", (method, path, bad)
    assert state(sid) == before


FORBIDDEN_KEYS = {"id", "user_id", "streamer_id", "email", "order_id", "provider_order_id", "status_token_hash",
                  "upload_token_hash", "legacy_source_table", "mod_notified", "order_prefix", "pusher_group",
                  "razorpay_key_ref", "razorpay_secret_ref", "discord_moderation_channel_id", "api_enabled",
                  "rejection_reason", "custom_page_path", "password_hash", "token_hash", "csrf_token"}


def _keys(value) -> set:
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value)) if value else set()
    return set()


def test_no_private_data_in_any_read(app):
    owner = make_user("owner-private" + DOMAIN)
    sid = make_streamer("authtest-private", user_id=owner)
    update_streamer(sid, order_prefix="authtestPRIVPFX", custom_page_path="/authtest-secret-page",
                    discord_moderation_channel_id="authtest-discord-777", razorpay_key_ref="authtest-rzp-ref",
                    razorpay_secret_ref="authtest-rzp-secret", rejection_reason="authtest-reason", pusher_group=7)
    with db() as conn:
        conn.execute("UPDATE public.users SET streamer_id = %s WHERE id = %s", (sid, owner))
        conn.execute("INSERT INTO public.obs_tokens (streamer_id, token_hash) VALUES (%s, 'authtest-obs-hash')",
                     (sid,))
    ids = _fixtures(sid)
    browser, headers = _signed_in_browser(app, "owner-private" + DOMAIN)
    texts = []
    for method, path, body in READS:
        r = _call(browser, method, sid, path, body, ids, headers)
        assert r.status_code == 200, (path, r.text)
        texts.append(r.text)
        if r.headers["content-type"].startswith("application/json"):
            assert not _keys(r.json()) & FORBIDDEN_KEYS, (path, _keys(r.json()) & FORBIDDEN_KEYS)
    blob = "\n".join(texts)
    with db() as conn:
        order_id = conn.execute("SELECT order_id FROM public.donations WHERE id = %s", (ids["donation"],)).fetchone()[0]
    for secret in (owner, sid, DOMAIN, order_id, *PLANTED, "authtestPRIVPFX", "authtest-secret-page",
                   "authtest-discord-777", "authtest-rzp-ref", "authtest-rzp-secret", "authtest-reason",
                   "authtest-obs-hash", "pusher_group", "password", "csrf"):
        assert secret not in blob, secret
    assert json.loads(texts[0])["slug"] == "authtest-private"
    assert "Fan" in blob and ids["donation"] in blob  # the creator does see its own donations
