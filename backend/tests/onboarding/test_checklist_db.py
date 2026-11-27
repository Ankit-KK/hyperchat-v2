"""Onboarding Stage 5 Phase A: the read-only checklist (GET /api/v1/me/onboarding) and the admin progress view
(GET /api/v1/admin/onboarding) on the scratch database. Synthetic rows only; nothing here may activate."""

import asyncio
from types import SimpleNamespace

import pytest

from app.auth.tokens import new_hex_token, sha256_hex
from tests.integration import (
    DOMAIN,
    ORIGIN,
    PASSWORD,
    db,
    fixture_db,
    login,
    make_user,
    new_browser,
    requires_db,
    update_streamer,
)
from tests.kyc.test_enforcement_db import verify
from tests.kyc.test_kyc_db import configure, unconfigure

pytestmark = requires_db
ME = "/api/v1/me/onboarding"
ADMIN = "/api/v1/admin/onboarding"


@pytest.fixture
def kyc(app, monkeypatch):
    """Didit configured for 'sandbox' in the database (kyc_activation_block) and in this server (the KYC row)."""
    configure()
    monkeypatch.setattr(app.app_state, "kyc", SimpleNamespace(environment="sandbox"))
    yield
    unconfigure()


@pytest.fixture
def boss(app):
    from tests.admin.test_applications import _admin
    _admin(app, "chk-boss")  # the shared client as an admin; 'primary' is this server's configured account
    return app


def hold(cid: str, admin: str) -> None:
    """An open restriction, written as fixture setup. kyc_restrict_creator would also append a KYC enforcement event,
    which moves the enforcement chain past the restore witness and fails later donation suites closed."""
    with fixture_db() as conn:
        conn.execute("INSERT INTO public.kyc_restrictions (streamer_id, reason, created_by) VALUES (%s, 'admin_hold', %s)",
                     (cid, make_user(admin + DOMAIN, role="admin")))


def rows(entry: dict) -> dict:
    return {r["condition"]: (r["state"], r["detail"]) for r in entry["conditions"]}


def entry_of(resp, cid: str) -> dict:
    assert resp.status_code == 200, resp.text
    [entry] = [c for c in resp.json()["creators"] if c["streamer_id"] == cid]
    return entry


def accept(app, repo, sid: str, email: str):
    """The applicant accepts the application's invitation (the real route) and signs in."""
    admin = make_user(f"chk-inviter-{sid[:8]}" + DOMAIN, role="admin")
    token = new_hex_token(32)
    assert asyncio.run(repo.issue_invite(sid, admin, sha256_hex(token), 7))[0] == "ok"
    r = new_browser(app).post("/api/v1/auth/invites/accept", json={"token": token, "password": PASSWORD},
                              headers=ORIGIN)
    assert r.status_code == 200, r.text
    owner = new_browser(app)
    assert login(owner, email).status_code == 200
    with db() as conn:
        uid = conn.execute("SELECT id::text FROM public.users WHERE email = %s", (email,)).fetchone()[0]
    return owner, uid


def test_the_checklist_follows_each_step_and_never_activates(app, repo, approved, boss, kyc):
    sid, cid, email = approved("chk-steps")
    fresh = entry_of(boss.get(ADMIN), cid)
    assert rows(fresh) == {
        "application_approved": ("ok", None), "invitation_accepted": ("missing", "no_invitation"),
        "ownership_confirmed": ("missing", "no_owner"), "kyc_approved": ("missing", "no_owner"),
        "pan_policy": ("missing", "kyc_not_approved"), "profile_picture": ("missing", None),
        "background": ("missing", None), "no_restriction": ("ok", None), "razorpay_account": ("ok", None),
        "lifecycle_eligible": ("blocked", "no_owner")}
    assert (fresh["status"], fresh["track"], fresh["qualifies_for_auto_activation"]) == ("pending", "standard", True)

    owner, uid = accept(app, repo, sid, email)
    r = owner.get(ME)
    assert r.headers["cache-control"] == "no-store"
    claimed = entry_of(r, cid)
    assert rows(claimed)["invitation_accepted"] == ("ok", None)
    assert rows(claimed)["ownership_confirmed"] == ("ok", None)
    assert rows(claimed)["kyc_approved"] == ("missing", "owner_kyc_required")
    assert rows(claimed)["lifecycle_eligible"] == ("blocked", "kyc_required")

    verify(uid)  # the owner's Didit verification, recorded as the signed webhook records it
    update_streamer(cid, brand_logo_url="/assets/streamers/chk/logo.png",
                    background_url=f"https://cdn.example/creator-assets/{cid}/background/{'a' * 64}.webp")
    verified = entry_of(owner.get(ME), cid)
    assert rows(verified)["kyc_approved"] == ("ok", None) and rows(verified)["lifecycle_eligible"] == ("ok", None)
    assert rows(verified)["profile_picture"] == ("missing", "legacy_path")
    assert rows(verified)["background"] == ("set", "validation_not_recorded")

    logo = f"https://cdn.example/creator-assets/{cid}/logo/{'b' * 64}.webp"
    update_streamer(cid, brand_logo_url=logo)
    assert rows(entry_of(owner.get(ME), cid))["profile_picture"] == ("set", "validation_not_recorded")  # no record
    background = f"https://cdn.example/creator-assets/{cid}/background/{'a' * 64}.webp"
    with fixture_db() as conn:  # the pipeline's records (migration 0039), as the upload transaction writes them
        for kind, url in (("profile", logo), ("background", background)):
            conn.execute("INSERT INTO public.creator_image_uploads (streamer_id, kind, state, variants) VALUES "
                         "(%s, %s, 'ready', jsonb_build_array(jsonb_build_object('key', %s::text, 'url', %s::text)))",
                         (cid, kind, url.split("/", 3)[3], url))
    complete = entry_of(owner.get(ME), cid)
    assert rows(complete)["profile_picture"] == ("ok", None) and rows(complete)["background"] == ("ok", None)
    # recorded without a signed decision's documents (as before 0041): not PAN, no hold opened automatically, so
    # automatic activation refuses it (migration 0042: pan_evidence_missing) and only that row blocks
    assert [r["condition"] for r in complete["conditions"] if r["blocking"]] == ["pan_policy"]
    assert rows(complete)["pan_policy"] == ("missing", "pan_evidence_not_recorded")
    assert complete["ready_for_activation"] is False

    app.app_state.admin.payment_accounts = frozenset()  # this server no longer configures the account
    assert rows(entry_of(owner.get(ME), cid))["razorpay_account"] == ("missing", "account_not_configured")
    app.app_state.admin.payment_accounts = frozenset({"primary"})

    hold(cid, "chk-holder")
    held = entry_of(owner.get(ME), cid)
    assert rows(held)["no_restriction"] == ("blocked", "open_restriction")
    assert rows(held)["lifecycle_eligible"] == ("blocked", "restricted")
    assert rows(held)["pan_policy"] == ("missing", "pan_evidence_not_recorded")
    assert held["ready_for_activation"] is False
    with db() as conn:  # nothing here activates
        assert conn.execute("SELECT status FROM public.streamers WHERE id = %s", (cid,)).fetchone()[0] == "pending"


def test_who_may_read_a_checklist(app, approved, kyc):
    sid, cid, _ = approved("chk-access")
    owner_id = make_user("chk-access-owner" + DOMAIN)
    with db() as conn:
        conn.execute("UPDATE public.streamers SET user_id = %s WHERE id = %s", (owner_id, cid))
    owner = new_browser(app)
    assert login(owner, "chk-access-owner" + DOMAIN).status_code == 200
    mine = entry_of(owner.get(ME), cid)
    assert rows(mine)["ownership_confirmed"] == ("missing", "owner_not_invitee")  # linked by hand, not invited

    make_user("chk-access-linked" + DOMAIN, streamer_id=cid)  # linked accounts have full owner power
    linked = new_browser(app)
    assert login(linked, "chk-access-linked" + DOMAIN).status_code == 200
    assert entry_of(linked.get(ME), cid) == mine  # every row is the owner's, whoever reads it

    make_user("chk-access-stranger" + DOMAIN)
    stranger = new_browser(app)
    assert login(stranger, "chk-access-stranger" + DOMAIN).status_code == 200
    r = stranger.get(ME)
    assert r.status_code == 403 and "owner or linked account" in r.json()["error"]
    assert r.headers["cache-control"] == "no-store"
    assert new_browser(app).get(ME).status_code == 401


def test_the_admin_view_filters_pages_and_shows_no_applicant_data(app, approved, boss, kyc):
    sid, held, email = approved("chk-adm-held")
    _, other, _ = approved("chk-adm-other")
    hold(held, "chk-adm-holder")

    r = boss.get(ADMIN)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    entry = entry_of(r, held)
    assert entry["signup_id"] == sid
    assert entry["invite_delivery"] == {"status": None, "auto_attempts": 0, "last_failure": None}
    for secret in (email, "+91", "Auto"):  # the conftest applicant's address, phone and name
        assert secret not in r.text, secret

    only_held = boss.get(ADMIN, params={"blocked_by": "no_restriction"}).json()["creators"]
    ids = {c["streamer_id"] for c in only_held}
    assert held in ids and other not in ids
    assert other not in {c["streamer_id"] for c in boss.get(ADMIN, params={"status": "active"}).json()["creators"]}
    assert other in {c["streamer_id"] for c in boss.get(ADMIN, params={"status": "pending"}).json()["creators"]}

    first = boss.get(ADMIN, params={"limit": 1}).json()
    second = boss.get(ADMIN, params={"limit": 1, "offset": 1}).json()
    assert first["total"] == second["total"] >= 2 and len(first["creators"]) == len(second["creators"]) == 1
    assert first["creators"][0]["streamer_id"] != second["creators"][0]["streamer_id"]

    bad = boss.get(ADMIN, params={"blocked_by": "nope"})
    assert bad.status_code == 400 and "blocked_by must be one of" in bad.text
    assert boss.get(ADMIN, params={"limit": 201}).status_code == 422
    assert boss.get(ADMIN, params={"status": "rejected"}).status_code == 422

    assert new_browser(app).get(ADMIN).status_code == 401
    make_user("chk-adm-creator" + DOMAIN)
    creator = new_browser(app)
    assert login(creator, "chk-adm-creator" + DOMAIN).status_code == 200
    assert creator.get(ADMIN).status_code == 403
