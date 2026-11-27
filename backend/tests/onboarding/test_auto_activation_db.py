"""Migration 0042: automatic activation (app.onboarding.auto_activate) on the scratch database. The one rule
(onboarding_activation_block), the operator switch (off by default), the lifecycle-only transition and the step-up
guard exception bound to its success row. Synthetic rows only (the env fixture's cleanup removes them)."""

import asyncio
import json

import psycopg
import pytest

from app.cli import connection_factory
from app.core.config import Settings
from app.onboarding.auto_activate import AutoActivator
from tests.integration import DOMAIN, db, fixture_db, make_user, requires_db, update_streamer
from tests.kyc.test_enforcement_db import verify
from tests.onboarding.conftest import set_enforcement
from tests.onboarding.test_checklist_db import ADMIN, accept, boss, entry_of, hold, kyc, rows  # noqa: F401 (fixtures)

pytestmark = requires_db
REFUSED = psycopg.errors.InsufficientPrivilege
OPERATOR = "authtest-operator"
PAN = {"status": "Approved", "document_type": "Tax Card", "document_subtype": "TAX_CARD_STANDARD",
       "issuing_state": "IND", "verification_method": "document"}
PASSPORT = {**PAN, "document_type": "Passport", "document_subtype": "PASSPORT_STANDARD"}


def switch(on: bool) -> str:
    with fixture_db() as conn:  # the operator (migration role, acting as hyperchat_owner)
        return conn.execute("SELECT public.onboarding_set_auto_activation(%s, %s, 'AUTHTEST-APPROVAL')",
                            (on, OPERATOR)).fetchone()[0]


@pytest.fixture
def auto(app, kyc, boss):  # noqa: F811
    """The worker's loop as the worker builds it; the switch goes back off and step-up back to draft afterwards."""
    activator = AutoActivator(connection_factory(Settings()), connection_factory(Settings(), role="lifecycle"),
                              frozenset({"primary"}), poll_seconds=60)
    yield activator
    switch(False)
    set_enforcement("draft")


def evidence(session_id: str, *docs: dict) -> None:
    """The signed decision's document labels (as 0041's kyc_check_statuses records them): the hold trigger runs."""
    with fixture_db() as conn:
        conn.execute("UPDATE public.kyc_sessions SET check_statuses = coalesce(check_statuses, '{}'::jsonb) || "
                     "jsonb_build_object("
                     "'id_documents', %s::jsonb) WHERE session_id = %s", (json.dumps(list(docs)), session_id))


def images(cid: str) -> None:
    urls = {kind: f"https://cdn.example/creator-assets/{cid}/{kind}/{c * 64}.webp"
            for kind, c in (("profile", "b"), ("background", "a"))}
    update_streamer(cid, brand_logo_url=urls["profile"], background_url=urls["background"])
    with fixture_db() as conn:  # the pipeline's records (migration 0039)
        for kind, url in urls.items():
            conn.execute("INSERT INTO public.creator_image_uploads (streamer_id, kind, state, variants) VALUES "
                         "(%s, %s, 'ready', jsonb_build_array(jsonb_build_object('key', %s::text, 'url', %s::text)))",
                         (cid, kind, url.split("/", 3)[3], url))


def eligible(app, repo, approved, name: str, *docs: dict) -> tuple[str, str, str]:
    """An application-created creator with every condition met: (creator id, owner id, KYC session id)."""
    sid, cid, email = approved(name)
    _, uid = accept(app, repo, sid, email)
    session = verify(uid)
    evidence(session, *(docs or (PAN,)))
    images(cid)
    return cid, uid, session


def block(cid: str) -> str | None:
    with db() as conn:
        return conn.execute("SELECT public.onboarding_activation_block(%s)", (cid,)).fetchone()[0]


def status(cid: str) -> str:
    with db() as conn:
        return conn.execute("SELECT status FROM public.streamers WHERE id = %s", (cid,)).fetchone()[0]


def lifecycle_activate(cid: str, account: str | None = "primary") -> str:
    with db("hyperchat_lifecycle") as conn:
        return conn.execute("SELECT public.onboarding_auto_activate(%s, %s)", (cid, account)).fetchone()[0]


def activations(cid: str) -> list[tuple]:
    with db() as conn:
        return conn.execute("SELECT a.pan_status, a.payout_hold_id IS NOT NULL, e.action, e.actor_user_id, e.reason, "
                            "e.from_status, e.to_status FROM public.onboarding_activations a JOIN "
                            "public.creator_status_events e ON e.id = a.status_event_id WHERE a.streamer_id = %s",
                            (cid,)).fetchall()


def test_the_switch_starts_off_is_operator_only_and_every_change_is_audited(auto):
    with db() as conn:
        assert conn.execute("SELECT auto_activation FROM public.onboarding_settings").fetchone()[0] is False
    with fixture_db() as conn:
        assert conn.execute("SELECT public.onboarding_set_auto_activation(true, %s, ' ')", (OPERATOR,)
                            ).fetchone()[0] == "operator_and_approval_required"
        before = conn.execute("SELECT count(*) FROM public.onboarding_settings_events").fetchone()[0]
    assert switch(True) == "on" and switch(False) == "off"
    with fixture_db() as conn:
        assert conn.execute("SELECT count(*) FROM public.onboarding_settings_events").fetchone()[0] == before + 2
        assert conn.execute("SELECT auto_activation, changed_by, approval_reference FROM "
                            "public.onboarding_settings_events ORDER BY created_at DESC LIMIT 2").fetchall() == [
            (False, OPERATOR, "AUTHTEST-APPROVAL"), (True, OPERATOR, "AUTHTEST-APPROVAL")]
        with pytest.raises(psycopg.errors.CheckViolation):  # one row, never deleted (whoever asks)
            conn.execute("DELETE FROM public.onboarding_settings")
    for role in ("hyperchat_app", "hyperchat_worker", "hyperchat_auth", "hyperchat_lifecycle", "hyperchat_stepup"):
        with db(role) as conn:
            for sql in ("UPDATE public.onboarding_settings SET auto_activation = true, changed_by = 'x', "
                        "approval_reference = 'x'",
                        "SELECT public.onboarding_set_auto_activation(true, 'x', 'x')",
                        "INSERT INTO public.onboarding_settings_events (auto_activation, changed_by, "
                        "approval_reference) VALUES (true, 'x', 'x')"):
                with pytest.raises(REFUSED):
                    conn.execute(sql)


def test_an_eligible_creator_activates_only_while_the_switch_is_on(app, repo, approved, auto):
    cid, uid, session = eligible(app, repo, approved, "aa-pan")
    assert block(cid) is None
    assert entry_of(app.get(ADMIN), cid)["ready_for_activation"] is True  # the checklist agrees with the rule
    assert asyncio.run(auto.run_once()) == [] and status(cid) == "pending"  # off (the default): nothing happens
    assert lifecycle_activate(cid) == "switched_off"

    switch(True)
    assert (cid, "ok") in asyncio.run(auto.run_once())
    assert status(cid) == "active"
    # audited as an automatic activation: no actor, reason 'auto', the inputs on the success row
    assert activations(cid) == [("pan_verified", False, "activate", None, "auto", "pending", "active")]
    with db() as conn:
        assert conn.execute("SELECT user_id::text, kyc_session_id::text, razorpay_account FROM "
                            "public.onboarding_activations WHERE streamer_id = %s", (cid,)).fetchone() == \
            (uid, session, "primary")
    assert all(c != cid for c, _ in asyncio.run(auto.run_once()))  # never twice
    assert lifecycle_activate(cid) == "status_changed"
    view = app.get(ADMIN + "/auto-activation")
    assert view.status_code == 200 and view.headers["cache-control"] == "no-store", view.text
    body = view.json()
    assert body["enabled"] is True and body["changed_by"] == OPERATOR
    [mine] = [a for a in body["activations"] if a["streamer_id"] == cid]
    assert (mine["status"], mine["pan_status"], mine["payout_hold"]) == ("active", "pan_verified", False)


def test_a_non_pan_approval_activates_with_its_payout_hold_but_one_without_evidence_never(app, repo, approved, auto):
    switch(True)
    held, uid, _ = eligible(app, repo, approved, "aa-passport", PASSPORT)
    assert block(held) is None
    assert lifecycle_activate(held) == "ok"
    assert activations(held) == [("non_pan", True, "activate", None, "auto", "pending", "active")]
    with db() as conn:  # the hold stays open: payouts wait for a PAN re-verification or an administrator
        assert conn.execute("SELECT count(*) FROM public.kyc_payout_holds WHERE user_id = %s AND cleared_at IS NULL",
                            (uid,)).fetchone()[0] == 1

    # 0041 keeps one open hold per user: a later non-PAN re-approval (session B) adds no row, and the open hold of
    # session A still covers it
    again, owner2, first = eligible(app, repo, approved, "aa-reapproved", PASSPORT)
    with fixture_db() as conn:
        hold_a = conn.execute("SELECT id::text FROM public.kyc_payout_holds WHERE user_id = %s AND cleared_at IS NULL",
                              (owner2,)).fetchone()[0]
    from tests.kyc.test_kyc_pan_db import new_session
    from tests.kyc.test_enforcement_db import set_kyc
    second = new_session(owner2)
    set_kyc(second, "Approved")
    evidence(second, PASSPORT)
    with db() as conn:
        assert conn.execute("SELECT e.session_id::text FROM public.kyc_effective(%s, 'sandbox') e",
                            (owner2,)).fetchone()[0] == second != first
    assert block(again) is None and lifecycle_activate(again) == "ok"
    with db() as conn:
        assert conn.execute("SELECT payout_hold_id::text FROM public.onboarding_activations WHERE streamer_id = %s",
                            (again,)).fetchone()[0] == hold_a

    sid, old, email = approved("aa-noevidence")  # approved as before 0041: no documents recorded, so no hold
    _, owner_id = accept(app, repo, sid, email)
    verify(owner_id)
    images(old)
    assert block(old) == "pan_evidence_missing"
    assert rows(entry_of(app.get(ADMIN), old))["pan_policy"] == ("missing", "pan_evidence_not_recorded")
    assert lifecycle_activate(old) == "pan_evidence_missing" and status(old) == "pending"


def test_each_missing_condition_keeps_the_creator_pending_with_its_code(app, repo, approved, auto):
    switch(True)
    sid, cid, email = approved("aa-steps")
    assert block(cid) == "no_owner"
    _, uid = accept(app, repo, sid, email)
    assert block(cid) == "kyc_required"
    session = verify(uid)
    evidence(session, PAN)
    assert block(cid) == "profile_picture_missing"
    update_streamer(cid, brand_logo_url=f"https://cdn.example/creator-assets/{cid}/profile/{'c' * 64}.webp")
    assert block(cid) == "profile_picture_missing"  # a pointer without its ready record (0039) is not valid
    images(cid)
    assert block(cid) is None

    hold(cid, "aa-holder")  # Q15: an open restriction wins
    assert block(cid) == "restricted" and lifecycle_activate(cid) == "restricted"
    assert not [c for c, _ in asyncio.run(auto.run_once()) if c == cid]
    assert status(cid) == "pending" and activations(cid) == []


def test_only_application_created_pending_standard_creators_and_the_invitee_qualify(app, repo, approved, auto):
    switch(True)
    cid, uid, _ = eligible(app, repo, approved, "aa-owner")
    other = make_user("aa-other" + DOMAIN)
    evidence(verify(other), PAN)
    update_streamer(cid, user_id=other)  # an owner linked by hand (verified too) is not the invitee
    assert block(cid) == "owner_not_invitee" and lifecycle_activate(cid) == "owner_not_invitee"
    update_streamer(cid, user_id=uid)
    update_streamer(cid, kyc_track="legacy")
    assert block(cid) == "not_application_created"
    update_streamer(cid, kyc_track="standard")
    update_streamer(cid, status="suspended")  # a suspended (or active, rejected) creator never auto-activates
    assert block(cid) == "not_pending" and lifecycle_activate(cid) == "status_changed"
    update_streamer(cid, status="pending")
    assert lifecycle_activate(cid, "secondary") == "account_changed"  # compare-and-set on the configured account
    auto.accounts = frozenset({"secondary"})  # this server no longer configures the creator's account: not picked
    assert all(c != cid for c, _ in asyncio.run(auto.run_once())) and status(cid) == "pending"


def test_only_the_lifecycle_role_activates_and_only_through_its_function(app, repo, approved, auto):
    switch(True)
    cid, _, _ = eligible(app, repo, approved, "aa-roles")
    for role in ("hyperchat_app", "hyperchat_worker", "hyperchat_auth", "hyperchat_stepup", "hyperchat_readonly"):
        with db(role) as conn:
            with pytest.raises(REFUSED):
                conn.execute("SELECT public.onboarding_auto_activate(%s, 'primary')", (cid,))
    for role in ("hyperchat_app", "hyperchat_worker", "hyperchat_lifecycle"):
        with db(role) as conn:
            with pytest.raises(REFUSED):
                conn.execute("INSERT INTO public.onboarding_activations (streamer_id, status_event_id, user_id, "
                             "signup_id, invite_id, pan_status, profile_upload_id, background_upload_id, "
                             "razorpay_account) VALUES (%s, gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), "
                             "gen_random_uuid(), 'pan_verified', gen_random_uuid(), gen_random_uuid(), 'primary')",
                             (cid,))
    with db() as conn:
        with pytest.raises(REFUSED):
            conn.execute("DELETE FROM public.onboarding_activations")


def test_with_step_up_active_only_the_automatic_transition_skips_the_passkey(app, repo, approved, auto):
    """The exception is bound to onboarding_auto_activate's success row in the same transaction, not to the role or
    a setting: the admin function on the same lifecycle role, for the same eligible creator, still needs its grant."""
    manual, _, _ = eligible(app, repo, approved, "aa-manual")  # built in draft: its approval and invite need grants
    admin = make_user("aa-admin" + DOMAIN, role="admin")
    switch(True)
    set_enforcement("active")
    with db("hyperchat_lifecycle") as conn:
        with pytest.raises(REFUSED, match="step-up"):
            conn.execute("SELECT public.kyc_change_creator_status(%s, 'activate', 'pending', %s, NULL, 'primary')",
                         (manual, admin))
        with pytest.raises(REFUSED, match="step-up"):  # a custom setting opens nothing
            with conn.transaction():
                conn.execute("SELECT set_config('hyperchat.onboarding_activation', %s, true)", (manual,))
                conn.execute("SELECT public.kyc_change_creator_status(%s, 'activate', 'pending', %s, NULL, 'primary')",
                             (manual, admin))
    assert status(manual) == "pending"
    assert lifecycle_activate(manual) == "ok" and status(manual) == "active"  # the automatic path, no grant
