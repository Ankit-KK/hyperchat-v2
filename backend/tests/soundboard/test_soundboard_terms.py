"""Custom Soundboard terms gate (D3) end to end on PostgreSQL: eligibility (admin) and the owner's acceptance of the
current terms version are separate controls, and both are needed on every Custom Soundboard route and on the public
board. Scratch database only (platform soundboard slots are global)."""

import pytest

from app.soundboard import terms
from tests.integration import (
    ORIGIN,
    accept_soundboard_terms,
    db,
    make_streamer,
    new_browser,
    requires_db,
    update_streamer,
)
from tests.soundboard.test_soundboard_api import ROUTES, S, board, browser, call, creator, platform, sb  # noqa: F401

pytestmark = requires_db
P = "/api/v1/public/streamers"
OLD = "soundboard-2026-07-14"
TERMS_REQUIRED = (403, {"error": "Accept the current Custom Soundboard terms first"})
DISABLED = (403, {"error": "Soundboard not enabled"})


def status(client, slug: str) -> dict:
    r = client.get(f"{S}/{slug}/soundboard/terms")
    assert r.status_code == 200, r.text
    return r.json()


def accept(client, slug: str, headers, version: str = terms.VERSION):
    return client.post(f"{S}/{slug}/soundboard/terms/accept", headers=headers, json={"version": version})


def acceptances(sid: str) -> list[tuple]:
    with db() as conn:
        return conn.execute("SELECT terms_version, user_id::text, accepted_at, revoked_at IS NOT NULL "
                            "FROM public.soundboard_tnc_acceptances WHERE streamer_id = %s ORDER BY terms_version",
                            (sid,)).fetchall()


def owner_of(sid: str) -> str:
    with db() as conn:
        return conn.execute("SELECT user_id::text FROM public.streamers WHERE id = %s", (sid,)).fetchone()[0]


def put(client, url: str, headers, json):
    return client.request("PUT", url, headers=headers, json=json)


def eligible(sid: str, on: bool = True) -> None:
    update_streamer(sid, soundboard_enabled=on)


# ── status ──────────────────────────────────────────────────────────────────────

def test_status_for_every_state(sb):
    sid, _ = creator(sb, "authtest-sbt-st", enabled=False)
    base = {"current_version": "soundboard-2026-10-13", "accepted_at": None}
    assert status(sb, "authtest-sbt-st") == {**base, "eligible": False, "accepted": False, "revoked": False,
                                             "usable": False, "can_accept": True}
    eligible(sid)
    assert status(sb, "authtest-sbt-st") == {**base, "eligible": True, "accepted": False, "revoked": False,
                                             "usable": False, "can_accept": True}
    accept_soundboard_terms(sid, version=OLD)  # an older version never counts
    assert status(sb, "authtest-sbt-st")["accepted"] is False
    accept_soundboard_terms(sid)
    now = status(sb, "authtest-sbt-st")
    assert (now["accepted"], now["revoked"], now["usable"], now["can_accept"]) == (True, False, True, True)
    assert now["accepted_at"]
    with db() as conn:
        conn.execute("UPDATE public.soundboard_tnc_acceptances SET revoked_at = now() WHERE streamer_id = %s "
                     "AND terms_version = %s", (sid, terms.VERSION))
    revoked = status(sb, "authtest-sbt-st")
    assert (revoked["accepted"], revoked["revoked"], revoked["usable"], revoked["can_accept"]) == (True, True, False,
                                                                                                    False)


def test_status_is_readable_by_linked_and_admin_but_they_cannot_accept(sb):
    sid, _ = creator(sb, "authtest-sbt-view")
    linked, _, _ = browser(sb, streamer_id=sid)
    admin, _, _ = browser(sb, role="admin")
    for client in (linked, admin):
        s = status(client, "authtest-sbt-view")
        assert (s["usable"], s["can_accept"]) == (True, False)
    stranger, _, _ = browser(sb)
    assert stranger.get(f"{S}/authtest-sbt-view/soundboard/terms").status_code == 403
    assert new_browser(sb).get(f"{S}/authtest-sbt-view/soundboard/terms").status_code == 401


# ── acceptance ──────────────────────────────────────────────────────────────────

def test_owner_accepts_once_and_repeats_are_idempotent(sb):
    sid, h = creator(sb, "authtest-sbt-acc", enabled=False)
    eligible(sid)
    accept_soundboard_terms(sid, version=OLD)
    r = accept(sb, "authtest-sbt-acc", h)
    assert r.status_code == 200 and r.json()["usable"] is True
    first = acceptances(sid)
    assert [(v, u, rev) for v, u, _, rev in first] == [(OLD, owner_of(sid), False),
                                                        (terms.VERSION, owner_of(sid), False)]
    for _ in range(2):
        r = accept(sb, "authtest-sbt-acc", h)
        assert r.status_code == 200 and r.json()["accepted"] is True
    assert acceptances(sid) == first  # no new row, accepted_at kept, the old version's row untouched
    with db() as conn:
        email, ua = conn.execute("SELECT user_email, user_agent FROM public.soundboard_tnc_acceptances "
                                 "WHERE streamer_id = %s AND terms_version = %s", (sid, terms.VERSION)).fetchone()
    assert email.endswith("@authtest.invalid") and ua  # auditable: who and from what client


def test_accepting_does_not_grant_eligibility(sb):
    sid, h = creator(sb, "authtest-sbt-noel", enabled=False)
    r = accept(sb, "authtest-sbt-noel", h)
    assert r.status_code == 200
    assert (r.json()["accepted"], r.json()["eligible"], r.json()["usable"]) == (True, False, False)
    assert (call(sb, "GET", "authtest-sbt-noel", "", None).status_code,
            call(sb, "GET", "authtest-sbt-noel", "", None).json()) == DISABLED


def test_only_the_owner_can_accept(sb):
    sid, h = creator(sb, "authtest-sbt-own", enabled=False)
    eligible(sid)
    linked, lh, _ = browser(sb, streamer_id=sid)
    admin, ah, _ = browser(sb, role="admin")
    for client, headers in ((linked, lh), (admin, ah)):
        r = accept(client, "authtest-sbt-own", headers)
        assert (r.status_code, r.json()) == (403, {"error": "Only the creator's owner can accept these terms"})
    stranger, sh, _ = browser(sb)
    assert (accept(stranger, "authtest-sbt-own", sh).json()) == {"error": "Forbidden"}
    assert accept(new_browser(sb), "authtest-sbt-own", ORIGIN).status_code == 401
    assert accept(sb, "authtest-sbt-own", ORIGIN).status_code == 403          # no CSRF token
    assert acceptances(sid) == []


def test_an_admin_who_owns_the_creator_accepts_as_owner(sb):
    admin, ah, admin_id = browser(sb, role="admin")
    sid = make_streamer("authtest-sbt-adm", user_id=admin_id)
    assert accept(admin, "authtest-sbt-adm", ah).status_code == 200
    assert [u for _, u, _, _ in acceptances(sid)] == [admin_id]


@pytest.mark.parametrize("version", [OLD, "soundboard-2026-10-14", "", " soundboard-2026-10-13", "SOUNDBOARD-2026-10-13"])
def test_only_the_current_version_is_accepted(sb, version):
    sid, h = creator(sb, "authtest-sbt-ver", enabled=False)
    r = accept(sb, "authtest-sbt-ver", h, version)
    assert (r.status_code, r.json()) == (409, {"error": "Terms version is not current"})
    assert acceptances(sid) == []
    r = sb.post(f"{S}/authtest-sbt-ver/soundboard/terms/accept", headers=h, json={})
    assert r.status_code == 422


def test_a_revoked_acceptance_cannot_be_renewed_by_the_owner(sb):
    sid, h = creator(sb, "authtest-sbt-rev", enabled=False)
    eligible(sid)
    accept_soundboard_terms(sid, revoked=True)
    r = accept(sb, "authtest-sbt-rev", h)
    assert (r.status_code, r.json()) == (409, {"error": "This acceptance was revoked by HyperChat"})
    assert [rev for _, _, _, rev in acceptances(sid)] == [True]


# ── eligibility (admin) ─────────────────────────────────────────────────────────

def test_admin_grants_and_revokes_eligibility_separately_from_acceptance(sb):
    sid, h = creator(sb, "authtest-sbt-elig", enabled=False)
    admin, ah, _ = browser(sb, role="admin")
    url = f"/api/v1/admin/creators/{sid}/soundboard"
    r = put(admin, url, headers=ah, json={"enabled": True})
    assert r.json() == {"creator_id": sid, "soundboard_enabled": True, "changed": True}
    assert put(admin, url, headers=ah, json={"enabled": True}).json()["changed"] is False  # idempotent
    assert acceptances(sid) == []                       # granting never accepts for the owner
    assert status(sb, "authtest-sbt-elig")["usable"] is False
    assert accept(sb, "authtest-sbt-elig", h).status_code == 200
    assert call(sb, "GET", "authtest-sbt-elig", "", None).status_code == 200
    r = put(admin, url, headers=ah, json={"enabled": False})
    assert r.json() == {"creator_id": sid, "soundboard_enabled": False, "changed": True}
    assert len(acceptances(sid)) == 1                   # revoking keeps the acceptance history
    assert (call(sb, "GET", "authtest-sbt-elig", "", None).status_code,
            call(sb, "GET", "authtest-sbt-elig", "", None).json()) == DISABLED
    assert put(admin, url, headers=ah, json={"enabled": True}).status_code == 200
    assert call(sb, "GET", "authtest-sbt-elig", "", None).status_code == 200  # re-granted: acceptance still counts


def test_eligibility_endpoint_is_admin_only_with_csrf_and_strict_input(sb):
    sid, h = creator(sb, "authtest-sbt-elig2", enabled=False)
    url = f"/api/v1/admin/creators/{sid}/soundboard"
    assert put(sb, url, headers=h, json={"enabled": True}).status_code == 403          # the owner is not an admin
    linked, lh, _ = browser(sb, streamer_id=sid)
    assert put(linked, url, headers=lh, json={"enabled": True}).status_code == 403
    assert put(new_browser(sb), url, headers=ORIGIN, json={"enabled": True}).status_code == 401
    admin, ah, _ = browser(sb, role="admin")
    assert put(admin, url, headers=ORIGIN, json={"enabled": True}).status_code == 403  # no CSRF token
    for body in ({"enabled": "true"}, {"enabled": 1}, {}, {"enabled": True, "accept": True}):
        assert put(admin, url, headers=ah, json=body).status_code == 422, body
    assert put(admin, "/api/v1/admin/creators/00000000-0000-4000-8000-000000000000/soundboard", headers=ah,
                     json={"enabled": True}).status_code == 404
    assert put(admin, "/api/v1/admin/creators/not-a-uuid/soundboard", headers=ah,
                     json={"enabled": True}).status_code == 404
    with db() as conn:
        assert conn.execute("SELECT soundboard_enabled FROM public.streamers WHERE id = %s", (sid,)).fetchone()[0] \
            is False


# ── the gate on every Custom Soundboard route ───────────────────────────────────

@pytest.mark.parametrize("setup", ["none", "old", "revoked"])
def test_every_route_needs_the_current_unrevoked_acceptance(sb, setup):
    platform(2)
    sid, h = creator(sb, f"authtest-sbt-g{setup}", enabled=False)
    eligible(sid)
    if setup == "old":
        accept_soundboard_terms(sid, version=OLD)
    elif setup == "revoked":
        accept_soundboard_terms(sid, revoked=True)
    for method, path, body in ROUTES:
        r = call(sb, method, f"authtest-sbt-g{setup}", path, body, h)
        assert (r.status_code, r.json()) == TERMS_REQUIRED, (method, path)


def test_routes_refuse_an_ineligible_creator_even_with_acceptance(sb):
    sid, h = creator(sb, "authtest-sbt-inel", enabled=False)
    accept_soundboard_terms(sid)
    for method, path, body in ROUTES:
        r = call(sb, method, "authtest-sbt-inel", path, body, h)
        assert (r.status_code, r.json()) == DISABLED, (method, path)


def test_routes_work_once_eligible_and_accepted(sb):
    platform(2)
    sid, h = creator(sb, "authtest-sbt-ok", enabled=False)
    eligible(sid)
    assert accept(sb, "authtest-sbt-ok", h).status_code == 200
    assert board(sb, "authtest-sbt-ok")["active"]
    assert call(sb, "POST", "authtest-sbt-ok", "/restore-defaults", None, h).status_code == 200
    r = call(sb, "POST", "authtest-sbt-ok", "/uploads", {"kind": "audio", "content_type": "audio/mpeg",
                                                          "content_length": 10}, h)
    assert r.status_code == 201


# ── public board and profile ────────────────────────────────────────────────────

def test_public_board_follows_the_gate(sb):
    sid = make_streamer("authtest-sbt-pub")
    profile = f"{P}/authtest-sbt-pub"

    def public() -> tuple[int, bool]:
        return sb.get(profile + "/soundboard").status_code, sb.get(profile).json()["soundboard_enabled"]

    assert public() == (404, False)                    # not eligible
    eligible(sid)
    assert public() == (404, False)                    # eligible, nothing accepted
    accept_soundboard_terms(sid, version=OLD)
    assert public() == (404, False)                    # only an old version
    accept_soundboard_terms(sid)
    assert public() == (200, True)                     # eligible + current acceptance
    with db() as conn:
        conn.execute("UPDATE public.soundboard_tnc_acceptances SET revoked_at = now() WHERE streamer_id = %s "
                     "AND terms_version = %s", (sid, terms.VERSION))
    assert public() == (404, False)                    # acceptance revoked
    with db() as conn:
        conn.execute("UPDATE public.soundboard_tnc_acceptances SET revoked_at = NULL WHERE streamer_id = %s", (sid,))
    assert public() == (200, True)
    eligible(sid, False)
    assert public() == (404, False)                    # eligibility revoked
    assert sb.get(profile).json()["slug"] == "authtest-sbt-pub"  # the rest of the profile is unaffected
    assert sb.get(profile + "/soundboard").json() == {"error": "Soundboard not enabled"}  # no terms state leaked


# ── regressions ─────────────────────────────────────────────────────────────────

def test_platform_terms_and_other_dashboard_routes_are_unaffected(sb):
    # Donor uploads (tests/uploads) share no code with the soundboard gate; that suite covers them unchanged.
    sid, h = creator(sb, "authtest-sbt-reg", enabled=False)
    assert accept(sb, "authtest-sbt-reg", h).status_code == 200
    assert sb.get("/api/v1/me/terms").json()["accepted"] is False  # soundboard terms are not the platform terms
    assert sb.get(f"{S}/authtest-sbt-reg/settings").status_code == 200
    assert sb.get(f"{S}/authtest-sbt-reg/donations").status_code == 200
