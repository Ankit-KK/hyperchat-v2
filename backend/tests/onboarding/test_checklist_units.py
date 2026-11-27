"""Units of the read-only onboarding checklist (app.onboarding.checklist, no database): the condition evaluator, the
image pointer rule and the shape of the facts query."""

import pytest

from app.onboarding import checklist
from app.onboarding.checklist import CONDITIONS, evaluate, image_state

ACCOUNTS = frozenset({"primary"})
LOGO = "https://cdn.example/creator-assets/0b5f/logo/ab12.webp"
BACKGROUND = "https://cdn.example/creator-assets/0b5f/background/cd34.webp"


def facts(**kw) -> dict:
    """A fresh application-created pending creator: approved, nothing else done."""
    return {"streamer_id": "s1", "slug": "authtest-x", "status": "pending", "kyc_track": "standard",
            "razorpay_account": "primary", "brand_logo_url": None, "background_url": None, "claimed": False,
            "signup_id": "a1", "review_status": "approved", "invite_status": None, "invite_accepted": False,
            "owner_is_invitee": False, "owner_active": False, "kyc_approved": False, "restricted": False,
            "lifecycle_block": "no_owner", **kw}


def done(**kw) -> dict:
    """Everything a person can do today is done."""
    return facts(**{"claimed": True, "invite_status": "accepted", "invite_accepted": True, "owner_is_invitee": True,
                    "owner_active": True, "kyc_approved": True, "lifecycle_block": None, "brand_logo_url": LOGO,
                    "background_url": BACKGROUND, "profile_ready_url": LOGO, "background_ready_url": BACKGROUND,
                    **kw})


def rows(entry: dict) -> dict:
    return {r["condition"]: (r["state"], r["detail"], r["blocking"]) for r in entry["conditions"]}


def test_every_condition_is_listed_once_in_order():
    assert [r["condition"] for r in evaluate(facts(), ACCOUNTS)["conditions"]] == list(CONDITIONS)
    assert len(CONDITIONS) == 10


def test_a_fresh_approval_is_missing_everything_a_person_has_not_done_yet():
    entry = evaluate(facts(), ACCOUNTS)
    assert rows(entry) == {
        "application_approved": ("ok", None, False),
        "invitation_accepted": ("missing", "no_invitation", True),
        "ownership_confirmed": ("missing", "no_owner", True),
        "kyc_approved": ("missing", "no_owner", True),
        "pan_policy": ("missing", "kyc_not_approved", False),  # 0041: informs, never blocks
        "profile_picture": ("missing", None, True),
        "background": ("missing", None, True),
        "no_restriction": ("ok", None, False),
        "razorpay_account": ("ok", None, False),
        "lifecycle_eligible": ("blocked", "no_owner", True)}
    assert entry["qualifies_for_auto_activation"] is True and entry["ready_for_activation"] is False


@pytest.mark.parametrize(("pan_status", "held", "covered", "row"), [
    ("pan_verified", False, True, ("ok", None, False)),
    ("non_pan", True, True, ("payout_hold", "non_pan_kyc", False)),  # owner 2026-11-27: activated, payouts held
    ("non_pan", False, True, ("ok", "hold_cleared", False)),  # an administrator cleared that approval's hold
    ("not_approved", False, False, ("missing", "kyc_not_approved", False)),
    (None, False, False, ("missing", "kyc_not_approved", False))])
def test_pan_only_reports_the_payout_hold_when_a_hold_covers_a_non_pan_approval(pan_status, held, covered, row):
    entry = evaluate(done(pan_status=pan_status, payout_held=held, pan_covered=covered), ACCOUNTS)
    assert rows(entry)["pan_policy"] == row
    assert not [c for c, (_, _, blocking) in rows(entry).items() if blocking]
    assert entry["ready_for_activation"] is True


def test_a_non_pan_approval_no_hold_covers_blocks_automatic_activation():
    """Approved before 0041 (no document evidence, so no automatic hold): onboarding_pan_covered is false and the
    database rule refuses it, so the checklist does too (an administrator may still activate it by hand)."""
    entry = evaluate(done(pan_status="non_pan", activation_block="pan_evidence_missing"), ACCOUNTS)
    assert rows(entry)["pan_policy"] == ("missing", "pan_evidence_not_recorded", True)
    assert entry["ready_for_activation"] is False and checklist.blocked_by(entry, "pan_policy")


def test_ready_needs_the_database_rule_too():
    assert evaluate(done(), ACCOUNTS)["ready_for_activation"] is True
    assert evaluate(done(activation_block="owner_not_invitee"), ACCOUNTS)["ready_for_activation"] is False


@pytest.mark.parametrize(("change", "condition", "expected"), [
    ({"restricted": True, "lifecycle_block": "restricted"}, "no_restriction", ("blocked", "open_restriction", True)),
    ({"razorpay_account": "secondary"}, "razorpay_account", ("missing", "account_not_configured", True)),
    ({"owner_is_invitee": False}, "ownership_confirmed", ("missing", "owner_not_invitee", True)),
    ({"owner_active": False}, "ownership_confirmed", ("missing", "owner_inactive", True)),
    ({"kyc_approved": False, "lifecycle_block": "kyc_required"}, "kyc_approved",
     ("missing", "owner_kyc_required", True)),
    ({"kyc_approved": False, "lifecycle_block": "kyc_required"}, "lifecycle_eligible",
     ("blocked", "kyc_required", True)),
    ({"brand_logo_url": "/assets/legacy/logo.png"}, "profile_picture", ("missing", "legacy_path", True)),
    ({"background_url": ""}, "background", ("missing", None, True)),
    ({}, "profile_picture", ("ok", None, False)),
    ({"profile_ready_url": None}, "profile_picture", ("set", "validation_not_recorded", True)),  # no record (0039)
    ({"background_ready_url": BACKGROUND + "?old"}, "background", ("set", "validation_not_recorded", True)),
    ({"review_status": "rejected"}, "application_approved", ("missing", "rejected", True)),
])
def test_one_fact_flips_its_own_row(change, condition, expected):
    assert rows(evaluate(done(**change), ACCOUNTS))[condition] == expected


def test_invitation_detail_is_the_delivery_state_and_claim_without_application_is_not_an_invitee():
    assert rows(evaluate(facts(invite_status="expired"), ACCOUNTS))["invitation_accepted"][1] == "expired"
    legacy = evaluate(facts(signup_id=None, review_status=None, kyc_track="legacy", claimed=True, status="active"),
                      ACCOUNTS)
    assert rows(legacy)["application_approved"] == ("missing", "no_application", True)
    assert rows(legacy)["ownership_confirmed"][1] == "owner_not_invitee"


@pytest.mark.parametrize(("change", "qualifies"), [
    ({}, True), ({"kyc_track": "legacy"}, False), ({"signup_id": None, "review_status": None}, False)])
def test_only_application_created_standard_track_creators_qualify(change, qualifies):
    assert evaluate(facts(**change), ACCOUNTS)["qualifies_for_auto_activation"] is qualifies  # Q12


@pytest.mark.parametrize(("value", "ready", "expected"), [
    (None, None, ("missing", None)), ("", None, ("missing", None)),
    ("/assets/streamers/abc/logo.png", None, ("missing", "legacy_path")),
    ("https://cdn.example/assets/abc.png", None, ("missing", "legacy_path")),
    ("http://cdn.example/creator-assets/x/logo/h.webp", None, ("missing", "legacy_path")),
    (LOGO, None, ("set", "validation_not_recorded")),
    (LOGO, BACKGROUND, ("set", "validation_not_recorded")),  # a ready row for another image
    (LOGO, LOGO, ("ok", None))])
def test_only_the_pointer_of_a_ready_upload_row_is_valid(value, ready, expected):
    assert image_state(value, ready) == expected


def test_blocked_by_matches_only_a_blocking_row():
    entry = evaluate(done(), ACCOUNTS)
    assert not checklist.blocked_by(entry, "pan_policy") and not checklist.blocked_by(entry, "kyc_approved")
    assert checklist.blocked_by(evaluate(done(kyc_approved=False), ACCOUNTS), "kyc_approved")


def test_the_facts_query_only_reads():
    sql = " ".join(checklist._FACTS.split()).upper()
    for verb in ("INSERT", "UPDATE", "DELETE", "KYC_CHANGE_CREATOR_STATUS", "ONBOARDING_AUTO_ACTIVATE", "FOR "):
        assert verb not in sql, verb
    assert "PUBLIC.KYC_ACTIVATION_BLOCK(S.ID)" in sql and "PUBLIC.ONBOARDING_ACTIVATION_BLOCK(S.ID)" in sql
    for field in ("A.NAME", "A.EMAIL", "A.MOBILE_NUMBER", "STREAMER_NAME"):  # ids and slugs only
        assert field not in sql, field
