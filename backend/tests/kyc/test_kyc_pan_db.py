"""Migration 0041: PAN evidence from the signed Didit decision, the fail-closed PAN rule, the database-owned payout
hold for a non-PAN approval, and the PAN acknowledgement before KYC. The real app on the scratch database with a
fake Didit; synthetic rows only (the env fixture's cleanup removes holds and acknowledgements)."""

import json
import time

import psycopg
import pytest

from app.kyc.service import PAN_ACK_SHA256, PAN_ACK_VERSION
from tests.integration import DOMAIN, db, fixture_db, make_streamer, requires_db, signed_in
from tests.kyc.test_kyc_db import ME, acknowledge, hook, kyc, owner, start  # noqa: F401 (kyc: the fixture)

pytestmark = requires_db
REFUSED = psycopg.errors.InsufficientPrivilege
PAN = {"status": "Approved", "document_type": "Tax Card", "document_subtype": "TAX_CARD_STANDARD",
       "issuing_state": "IND", "verification_method": "document"}


def decision(sid: str, *docs: dict, liveness: bool = False) -> dict:
    items = [{**d, "node_id": f"id_verification_{n}", "document_number": "ABCDE1234F", "first_name": "Asha",
              "last_name": "Rao", "date_of_birth": "1990-02-03"} for n, d in enumerate(docs, 1)]
    out = {"session_id": sid, "status": "Approved", "id_verifications": items, "face_matches": [{"status": "Approved"}]}
    if liveness:
        out["liveness_checks"] = [{"status": "Approved"}]
    return out


def approve(app, uid: str, sid: str, *docs: dict, at: int | None = None, **kw) -> str:
    return hook(app, session_id=sid, vendor=uid, status="Approved", created_at=at or int(time.time()),
                decision=decision(sid, *docs, **kw)).json()["status"]


def pan_status(uid: str) -> str:
    with db() as conn:
        return conn.execute("SELECT public.kyc_pan_status(%s, 'sandbox')", (uid,)).fetchone()[0]


def holds(uid: str) -> list[tuple]:
    with fixture_db() as conn:
        return conn.execute("SELECT reason, session_id::text, cleared_reason, cleared_session_id::text FROM "
                            "public.kyc_payout_holds WHERE user_id = %s ORDER BY set_at, id", (uid,)).fetchall()


def new_session(uid: str) -> str:
    """A further session of the user (as kyc_create_session stores it), newer than the earlier ones."""
    from tests.kyc.test_kyc_db import WF
    with fixture_db() as conn:
        return conn.execute("INSERT INTO public.kyc_sessions (user_id, vendor_data, session_id, workflow_id, "
                            "environment, created_at) VALUES (%s, %s, gen_random_uuid(), %s, 'sandbox', "
                            "clock_timestamp()) RETURNING session_id::text", (uid, uid, WF)).fetchone()[0]


# ── evidence and the PAN rule ──────────────────────────────────────────────────────────────────────────────────

def test_a_signed_pan_approval_is_pan_verified_and_keeps_labels_only(kyc):
    headers, uid = owner(kyc, "kyc-pan-ok")
    sid = start(kyc, headers)["session_id"]
    assert approve(kyc, uid, sid, PAN) == "applied"  # no liveness_checks: the owner's workflows have none
    assert pan_status(uid) == "pan_verified" and holds(uid) == []
    with db() as conn:
        checks = conn.execute("SELECT check_statuses FROM public.kyc_sessions WHERE session_id = %s",
                              (sid,)).fetchone()[0]
    assert checks["id_documents"] == [PAN]  # the five labels, nothing else
    assert not any(secret in json.dumps(checks) for secret in ("ABCDE1234F", "Asha", "Rao", "1990-02-03"))
    me = kyc.get(ME).json()
    assert (me["pan_status"], me["payout_hold"]) == ("pan_verified", {"held": False, "reason": None, "since": None})


@pytest.mark.parametrize(("docs", "liveness"), [
    (({**PAN, "document_subtype": None},), False),                       # the subtype is missing: fail closed
    (({**PAN, "issuing_state": "USA"},), False),                         # not India
    (({**PAN, "document_subtype": "PASSPORT_STANDARD", "document_type": "Passport"},), True),
    (({**PAN, "verification_method": "id_lookup"},), False),            # a register lookup is not the PAN card
    ((PAN, {**PAN, "document_subtype": "VOTER_CARD"}), False),           # EVERY document must be PAN
    (({k: v for k, v in PAN.items() if k != "issuing_state"},), False),  # a field absent altogether
])
def test_anything_else_approved_activates_but_holds_payouts(kyc, docs, liveness):
    headers, uid = owner(kyc, f"kyc-pan-no{abs(hash(str(docs))) % 10_000}")
    sid = start(kyc, headers)["session_id"]
    assert approve(kyc, uid, sid, *docs, liveness=liveness) == "applied"
    with db() as conn:
        assert conn.execute("SELECT public.user_kyc_approved(%s, 'sandbox')", (uid,)).fetchone()[0]  # approved
    assert pan_status(uid) == "non_pan" and holds(uid) == [("non_pan_kyc", sid, None, None)]
    hold = kyc.get(ME).json()["payout_hold"]
    assert hold["held"] and hold["reason"] == "non_pan_kyc" and hold["since"]


def test_re_verifying_with_pan_clears_the_hold_and_losing_the_approval_never_does(kyc):
    headers, uid = owner(kyc, "kyc-pan-reverify")
    first = start(kyc, headers)["session_id"]
    approve(kyc, uid, first, {**PAN, "document_subtype": "PASSPORT_STANDARD"}, at=int(time.time()) - 60)
    assert holds(uid) == [("non_pan_kyc", first, None, None)]
    second = new_session(uid)
    assert pan_status(uid) == "not_approved" and holds(uid)[0][2] is None  # in progress: the hold stays open
    approve(kyc, uid, second, PAN)
    assert pan_status(uid) == "pan_verified" and holds(uid) == [("non_pan_kyc", first, "pan_verified", second)]


def test_a_policy_change_re_evaluates_every_approval(kyc):
    headers, uid = owner(kyc, "kyc-pan-policy")
    sid = start(kyc, headers)["session_id"]
    approve(kyc, uid, sid, PAN)
    with fixture_db() as conn:  # the operator withdraws the PAN row (statement trigger: every user re-synced)
        conn.execute("DELETE FROM public.kyc_document_policy")
    try:
        assert pan_status(uid) == "non_pan" and [h[0] for h in holds(uid)] == ["non_pan_kyc"]
    finally:
        with fixture_db() as conn:  # restore the seed exactly (0041's downgrade checks it)
            conn.execute("INSERT INTO public.kyc_document_policy (issuing_state, document_subtype) "
                         "VALUES ('IND', 'TAX_CARD_STANDARD')")
    assert pan_status(uid) == "pan_verified" and holds(uid)[-1][2] == "pan_verified"


def test_an_unsigned_refresh_never_changes_the_document_evidence(kyc):
    headers, uid = owner(kyc, "kyc-pan-refresh")
    sid = start(kyc, headers)["session_id"]
    approve(kyc, uid, sid, {**PAN, "document_subtype": "PASSPORT_STANDARD"})
    forged = '{"id_documents": [{"status": "Approved", "document_subtype": "TAX_CARD_STANDARD", ' \
             '"issuing_state": "IND", "verification_method": "document"}]}'
    with db() as conn:  # the API's role, as an admin refresh calls it
        conn.execute("SELECT public.kyc_apply_refresh(%s, 'In Review', %s::jsonb, clock_timestamp(), NULL)",
                     (sid, forged))
        docs = conn.execute("SELECT check_statuses -> 'id_documents' FROM public.kyc_sessions WHERE session_id = %s",
                            (sid,)).fetchone()[0]
    assert docs[0]["document_subtype"] == "PASSPORT_STANDARD"  # the signed evidence stays
    assert holds(uid)[0][2] is None  # and the hold stays open


# ── the payout hold belongs to the database ────────────────────────────────────────────────────────────────────

def test_no_api_role_writes_or_clears_a_hold_or_rewrites_an_acknowledgement(kyc):
    headers, uid = owner(kyc, "kyc-pan-boundary")
    sid = start(kyc, headers)["session_id"]
    approve(kyc, uid, sid, {**PAN, "issuing_state": "USA"})
    for role in ("hyperchat_app", "hyperchat_worker", "hyperchat_auth", "hyperchat_lifecycle"):
        for sql, args in (("UPDATE public.kyc_payout_holds SET cleared_at = now(), cleared_reason = 'pan_verified' "
                           "WHERE user_id = %s", (uid,)),
                          ("DELETE FROM public.kyc_payout_holds WHERE user_id = %s", (uid,)),
                          ("INSERT INTO public.kyc_document_policy (issuing_state, document_subtype) "
                           "VALUES ('USA', 'X')", ()),
                          ("SELECT public.kyc_sync_payout_holds()", ()),
                          ("DELETE FROM public.kyc_pan_acknowledgements WHERE user_id = %s", (uid,))):
            with pytest.raises(REFUSED), db(role) as conn:
                conn.execute(sql, args)
    with db() as conn:
        for sql in ("UPDATE public.kyc_pan_acknowledgements SET session_id = gen_random_uuid(), bound_at = now() "
                    "WHERE user_id = %s AND session_id IS NOT NULL",  # rebinding an acknowledgement
                    "UPDATE public.kyc_pan_acknowledgements SET acknowledged_at = now() - interval '1 day' "
                    "WHERE user_id = %s",
                    "INSERT INTO public.kyc_pan_acknowledgements (user_id, text_version, text_sha256, acknowledged_at) "
                    "VALUES (%s, 'v', repeat('0', 64), now() - interval '1 hour')"):
            with pytest.raises(REFUSED), conn.transaction():
                conn.execute(sql, (uid,))
    assert holds(uid)[0][2] is None


# ── the PAN acknowledgement ────────────────────────────────────────────────────────────────────────────────────

def test_a_start_needs_the_pan_acknowledgement_and_records_which_session_it_was_for(kyc):
    h = signed_in(kyc, "kyc-pan-ack" + DOMAIN)
    with db() as conn:
        uid = conn.execute("SELECT id::text FROM public.users WHERE email = %s", ("kyc-pan-ack" + DOMAIN,)).fetchone()[0]
    make_streamer("authtest-kyc-pan-ack", user_id=uid)
    assert kyc.get(ME).json()["pan_acknowledgement"]["current"] is False
    r = kyc.post(f"{ME}/session", headers=h)
    assert (r.status_code, kyc.fake.calls) == (409, 0)  # refused before Didit is called
    body = acknowledge(kyc, h)
    assert body["text"].startswith("Only use PAN card for verification") and body["text_version"] == PAN_ACK_VERSION
    sid = kyc.post(f"{ME}/session", headers=h).json()["session_id"]
    with db() as conn:
        rows = conn.execute("SELECT session_id::text, text_version, text_sha256, bound_at IS NOT NULL FROM "
                            "public.kyc_pan_acknowledgements WHERE user_id = %s", (uid,)).fetchall()
    assert rows == [(sid, PAN_ACK_VERSION, PAN_ACK_SHA256, True)]
    assert kyc.post(f"{ME}/session", headers=h).json()["session_id"] == sid  # a resume keeps its acknowledgement
    kyc.fake.finish(uid)  # Didit would now create a NEW session: it needs a fresh acknowledgement
    r = kyc.post(f"{ME}/session", headers=h)
    assert r.status_code == 409 and "url" not in r.json()
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.kyc_sessions WHERE user_id = %s", (uid,)).fetchone()[0] == 1
    acknowledge(kyc, h)
    assert kyc.post(f"{ME}/session", headers=h).json()["session_id"] != sid


def test_only_an_owner_acknowledges(kyc):
    h = signed_in(kyc, "kyc-pan-stranger" + DOMAIN)
    assert kyc.post(f"{ME}/pan-acknowledgement", headers=h).status_code == 403
