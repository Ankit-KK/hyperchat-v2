"""KYC PR4 (migration 0023) through the real app on the scratch database: the enforcement record (starts draft, only
moves forward, operator-only), the extended donation rule (restrictions, owner, KYC, finite owner-bound grace,
closure), admin activation / reactivation, ownership transfers and suspension revoking grace, checkout / uploads /
direct inserts following one rule, existing orders still finalizing, races, the runtime role's limits, the audit,
and the restore witness. Synthetic rows only; no Didit call (KYC states are recorded with the PR3 functions)."""

import json
import os
import threading
import time
import uuid
from unittest import mock

import psycopg
import pytest

from app.core.errors import ApiError
from app.kyc.operator import main as operator_main
from app.kyc.witness import KycWitness
from tests.integration import (
    DOMAIN,
    FIXTURE_ROLE,
    ORIGIN,
    PG_HOST,
    db,
    fixture_db,
    make_streamer,
    make_user,
    new_browser,
    requires_db,
    signed_in,
    target,
)
from tests.kyc.test_kyc_db import WF, configure, unconfigure
from tests.payments.test_payments_db import checkout, client, donation, rz_env, webhook  # noqa: F401 (fixtures)

pytestmark = requires_db
A = "/api/v1/admin"
P = "/api/v1/public/streamers"
UNAVAILABLE = {"error": "Donations are temporarily unavailable"}


def reset_enforcement() -> None:
    """Scratch database only: put the (forward-only) enforcement record back to draft between tests. The schema
    owner can disable a trigger, as it can do anything; the runtime roles cannot (tested below)."""
    with fixture_db() as conn:
        conn.execute("DELETE FROM public.kyc_enforcement_events WHERE streamer_id IS NULL")
        conn.execute("ALTER TABLE public.kyc_enforcement DISABLE TRIGGER kyc_enforcement_guard")
        conn.execute("UPDATE public.kyc_enforcement SET state = 'draft', generation = 0, policy = NULL, "
                     "activated_at = NULL, activated_by = NULL, activation_authorization = NULL, "
                     "activation_migration = NULL, closed_at = NULL, closed_by = NULL, closure_authorization = NULL")
        conn.execute("ALTER TABLE public.kyc_enforcement ENABLE TRIGGER kyc_enforcement_guard")


@pytest.fixture
def enf(app, tmp_path):
    """The app with Didit configured in the database and a fresh restore witness file."""
    reset_enforcement()
    configure()
    witness = KycWitness(str(tmp_path / "kyc.witness"))
    app.app_state.public.witness = app.app_state.kyc_enforcement.witness = witness
    app.witness = witness
    app.app_state.admin.payment_accounts = frozenset({"primary"})
    app.admin = new_browser(app)
    app.boss = signed_in(app.admin, "enf-boss" + DOMAIN, role="admin")
    yield app
    unconfigure()
    reset_enforcement()


def operator(app, *args: str) -> int:
    name, port = target()
    env = {"HYPERCHAT_MIGRATION_DSN": f"host={PG_HOST} port={port} dbname={name} user={FIXTURE_ROLE}",
           "HYPERCHAT_KYC_WITNESS_PATH": app.witness.path or ""}
    with mock.patch.dict(os.environ, env):
        return operator_main(list(args))


def confirm() -> tuple[str, str]:
    return "--confirm-database", target()[0]


def activate(app) -> None:
    assert operator(app, "activate", "--operator", "ops-test", "--authorization", "TEST-APPROVAL-1", *confirm()) == 0


def grant(app, sid: str, days: int = 30) -> int:
    return operator(app, "grant", "--creator", sid, "--days", str(days), "--reason", "rollout test",
                    "--operator", "ops-test", *confirm())


def owner_user(name: str) -> str:
    return make_user(name + DOMAIN)


def verify(uid: str, status: str = "Approved") -> str:
    """A Didit session of `uid` in `status`, recorded as the signed webhook path records it (migration 0022)."""
    with fixture_db() as conn:
        sid = conn.execute("INSERT INTO public.kyc_sessions (user_id, vendor_data, session_id, workflow_id, "
                           "environment) VALUES (%s, %s, gen_random_uuid(), %s, 'sandbox') RETURNING session_id::text",
                           (uid, uid, WF)).fetchone()[0]
        set_kyc(sid, status, conn)
    return sid


def set_kyc(session_id: str, status: str, conn=None) -> None:
    def run(c):
        c.execute("SELECT public.kyc_record_provider_status(%s, %s, clock_timestamp(), '{}'::jsonb, 'webhook', NULL)",
                  (session_id, status))
    if conn is not None:
        return run(conn)
    with fixture_db() as c:
        run(c)


def standard_creator(slug: str, uid: str | None, status: str = "pending") -> str:
    """A creator as the admin API / an approved application creates it (standard track, no baseline)."""
    with fixture_db() as conn:
        return conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, user_id, status, "
                            "razorpay_account) VALUES (%s, 'Std', %s, %s, 'primary') RETURNING id::text",
                            (slug, uid, status)).fetchone()[0]


def block(sid: str) -> str | None:
    with db() as conn:
        return conn.execute("SELECT public.creator_donation_block(%s)", (sid,)).fetchone()[0]


def insert_donation(sid: str, conn=None) -> None:
    """A donation insert by the runtime role: the trigger runs the rule for every one."""
    def run(c):
        c.execute("INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, amount_inr) "
                  "VALUES (%s, %s, 'D', 5, 'INR', 5)", (sid, "authtest-" + uuid.uuid4().hex[:12]))
    if conn is not None:
        return run(conn)
    with db() as c:
        run(c)


def refused(sid: str) -> bool:
    try:
        insert_donation(sid)
    except psycopg.Error as exc:
        assert exc.sqlstate == "HC001", exc
        return True
    return False


def status_change(app, sid: str, action: str, expected: str, reason: str | None = None):
    return app.admin.post(f"{A}/creators/{sid}/{action}", json={"expected_status": expected, "reason": reason},
                          headers=app.boss)


def state() -> tuple:
    with db() as conn:
        return conn.execute("SELECT state, generation FROM public.kyc_enforcement").fetchone()


def events(sid: str | None = None) -> list[str]:
    with db() as conn:
        return [r[0] for r in conn.execute("SELECT action FROM public.kyc_enforcement_events WHERE "
                                           "(%s::uuid IS NULL OR streamer_id = %s::uuid) ORDER BY created_at, id",
                                           (sid, sid))]


# ── before the rollout: nothing changes ─────────────────────────────────────────

def test_enforcement_starts_inactive_and_existing_creators_keep_their_pr1_eligibility(enf):
    assert state() == ("draft", 0) and events() == []
    a = owner_user("enf-legacy")
    legacy = make_streamer("authtest-enf-legacy", user_id=a)          # unverified owner, still the baseline
    unowned = make_streamer("authtest-enf-unowned")                  # an existing creator nobody claimed (PR1)
    moved = make_streamer("authtest-enf-moved", user_id=a)
    with fixture_db() as conn:
        conn.execute("UPDATE public.streamers SET user_id = %s WHERE id = %s", (owner_user("enf-new"), moved))
    std = standard_creator("authtest-enf-std", owner_user("enf-std"), status="active")
    assert (block(legacy), block(unowned), block(moved), block(std)) == (None, None, "kyc_required", "kyc_required")
    assert not refused(legacy) and refused(std)
    view = enf.admin.get(f"{A}/kyc/enforcement").json()
    assert view["enforcement"]["state"] == "draft" and view["witness"] == {"configured": True, "problem": None}
    assert view["new_donations_fail_closed"] is False
    preview = enf.admin.get(f"{A}/kyc/enforcement/preview").json()
    mine = {r["streamer_slug"]: r for r in preview["creators"] if r["streamer_slug"].startswith("authtest-enf-")}
    assert (mine["authtest-enf-legacy"]["block_now"], mine["authtest-enf-legacy"]["block_when_active"],
            mine["authtest-enf-legacy"]["block_when_closed"]) == (None, "kyc_required", "kyc_required")
    assert mine["authtest-enf-unowned"]["block_when_active"] == "no_owner"
    assert preview["summary"]["would_lose_new_donations_when_active"] >= 2
    assert state() == ("draft", 0)  # a preview changes nothing


# ── activation of new creators and the owner's KYC ──────────────────────────────

def test_only_a_verified_owner_is_activated_explicitly_and_a_kyc_loss_blocks_at_once(enf):
    uid = owner_user("enf-act")
    sid = standard_creator("authtest-enf-act", uid)
    r = status_change(enf, sid, "activate", "pending")
    assert (r.status_code, r.json()["error"]) == (409, "The creator's owner has no approved identity verification "
                                                      "(KYC)")
    session = verify(uid)
    with db() as conn:  # verification alone activates nothing
        assert conn.execute("SELECT status FROM public.streamers WHERE id = %s", (sid,)).fetchone()[0] == "pending"
    assert refused(sid)
    r = status_change(enf, sid, "activate", "pending")
    assert r.status_code == 200 and r.json()["donation_block"] is None
    assert not refused(sid)
    set_kyc(session, "Declined")  # revoked: refused at once, although the stored status still says active
    assert block(sid) == "kyc_required" and refused(sid)
    set_kyc(session, "Approved")  # a later re-approval of the same session
    assert block(sid) is None
    set_kyc(session, "Kyc Expired")
    assert block(sid) == "kyc_required"
    verify(uid)  # a new, approved session
    assert block(sid) is None
    configure(workflow=str(uuid.uuid4()))  # the operator moves to another Didit workflow: re-verification needed
    assert block(sid) == "kyc_required" and refused(sid)
    configure()  # back again: a new epoch, the old approval does not return
    assert block(sid) == "kyc_required"
    with db() as conn:
        assert conn.execute("SELECT status FROM public.streamers WHERE id = %s", (sid,)).fetchone()[0] == "active"


def test_pending_suspended_restricted_and_ownerless_creators_stay_blocked(enf):
    uid = owner_user("enf-blk")
    verify(uid)
    pending = standard_creator("authtest-enf-pend", uid)
    active = standard_creator("authtest-enf-ok", uid, status="active")
    ownerless = standard_creator("authtest-enf-none", None, status="active")
    assert (block(pending), block(active), block(ownerless)) == ("not_active", None, "no_owner")
    with fixture_db() as conn:
        conn.execute("UPDATE public.users SET is_active = false WHERE id = %s", (uid,))
    assert block(active) == "no_owner"  # an inactive owner is not a valid owner
    with fixture_db() as conn:
        conn.execute("UPDATE public.users SET is_active = true WHERE id = %s", (uid,))
    assert status_change(enf, active, "suspend", "active", "test").status_code == 200
    assert block(active) == "not_active" and refused(active)
    assert status_change(enf, active, "reactivate", "suspended").status_code == 200  # KYC still valid
    r = enf.admin.post(f"{A}/creators/{pending}/kyc/restrictions", json={"reason": "fraud_review"}, headers=enf.boss)
    assert r.status_code == 200 and r.json()["block_now"] == "not_active"
    r = status_change(enf, pending, "activate", "pending")
    assert (r.status_code, r.json()["error"]) == (409, "The creator has an open KYC restriction")


def test_restrictions_block_at_once_and_a_lift_needs_a_note_and_never_bypasses_kyc(enf):
    legacy = make_streamer("authtest-enf-rst", user_id=owner_user("enf-rst"))
    assert block(legacy) is None
    r = enf.admin.post(f"{A}/creators/{legacy}/kyc/restrictions", json={"reason": "owner_dispute", "note": "n"},
                       headers=enf.boss)
    assert r.status_code == 200 and r.json()["block_now"] == "restricted" and refused(legacy)
    rid = r.json()["restrictions"][0]["id"]
    assert enf.admin.post(f"{A}/creators/{legacy}/kyc/restrictions", json={"reason": "nope"},
                          headers=enf.boss).status_code == 400
    assert enf.admin.post(f"{A}/creators/{legacy}/kyc/restrictions", json={"reason": "admin_hold"}).status_code == 403
    assert enf.admin.post(f"{A}/creators/{legacy}/kyc/restrictions/{rid}/lift", json={"note": "  "},
                          headers=enf.boss).status_code == 400
    r = enf.admin.post(f"{A}/creators/{legacy}/kyc/restrictions/{rid}/lift", json={"note": "resolved"},
                       headers=enf.boss)
    assert r.status_code == 200 and r.json()["block_now"] is None  # draft: back to the PR1 baseline
    assert enf.admin.post(f"{A}/creators/{legacy}/kyc/restrictions/{rid}/lift", json={"note": "again"},
                          headers=enf.boss).status_code == 409
    activate(enf)
    r = enf.admin.post(f"{A}/creators/{legacy}/kyc/restrictions", json={"reason": "admin_hold"}, headers=enf.boss)
    rid2 = r.json()["restrictions"][-1]["id"]
    r = enf.admin.post(f"{A}/creators/{legacy}/kyc/restrictions/{rid2}/lift", json={"note": "lifted"},
                       headers=enf.boss)
    assert r.json()["block_now"] == "kyc_required"  # a lift never bypasses KYC
    assert events(legacy) == ["restriction_added", "restriction_lifted", "restriction_added", "restriction_lifted"]
    with open(enf.witness.path, encoding="utf-8") as f:
        assert [json.loads(line)["generation"] for line in f] == [1, 2, 3, 4, 5]


# ── grace and the rollout ───────────────────────────────────────────────────────

def test_grace_is_operator_only_finite_owner_bound_and_one_shot(enf):
    a = make_streamer("authtest-enf-ga", user_id=owner_user("enf-ga"))
    b = make_streamer("authtest-enf-gb", user_id=owner_user("enf-gb"))
    std = standard_creator("authtest-enf-gs", owner_user("enf-gs"), status="active")
    assert enf.admin.post(f"{A}/creators/{a}/kyc/grace", json={"days": 30}, headers=enf.boss).status_code in (404, 405)
    assert grant(enf, a, days=91) == 2  # the documented maximum is 90 days
    assert grant(enf, std) == 2         # standard creators have no exemption
    assert grant(enf, a) == 0
    assert grant(enf, a) == 2           # one grant per creator and owner, ever: no rolling extension
    assert operator(enf, "activate", "--operator", "ops", "--authorization", "X", "--confirm-database", "wrong") == 2
    assert state()[0] == "draft"        # nothing happens without naming this database
    activate(enf)
    assert (block(a), block(b), block(std)) == (None, "kyc_required", "kyc_required")
    assert not refused(a) and refused(b)
    c = make_streamer("authtest-enf-gc", user_id=owner_user("enf-gc"))
    assert grant(enf, c) == 0 and block(c) is None
    with fixture_db() as conn:  # the owner changes by any path (no API revocation): the grant is bound to its owner
        conn.execute("UPDATE public.streamers SET user_id = %s WHERE id = %s", (owner_user("enf-gc-new"), c))
    assert block(c) == "kyc_required"
    with fixture_db() as conn:  # time passes (scratch only: the grant is otherwise immutable)
        conn.execute("ALTER TABLE public.kyc_grace_grants DISABLE TRIGGER kyc_grace_grants_guard")
        conn.execute("UPDATE public.kyc_grace_grants SET granted_at = now() - interval '31 days', "
                     "expires_at = now() - interval '1 second' WHERE streamer_id = %s", (a,))
        conn.execute("ALTER TABLE public.kyc_grace_grants ENABLE TRIGGER kyc_grace_grants_guard")
    assert block(a) == "kyc_required" and refused(a)  # expired grace blocks donations
    with db() as conn:
        assert conn.execute("SELECT public.kyc_record_grace_expiries()").fetchone()[0] >= 1
        assert conn.execute("SELECT public.kyc_record_grace_expiries()").fetchone()[0] == 0
    assert events(a) == ["grace_granted", "grace_expired"]


def test_revocation_transfer_suspension_and_restriction_end_grace(enf):
    owners = {k: owner_user(f"enf-g{k}") for k in "rtsx"}
    creators = {k: make_streamer(f"authtest-enf-g{k}", user_id=owners[k]) for k in "rtsx"}
    for sid in creators.values():
        assert grant(enf, sid) == 0
    activate(enf)
    assert all(block(sid) is None for sid in creators.values())
    r = enf.admin.post(f"{A}/creators/{creators['r']}/kyc/grace/revoke", json={"reason": "owner asked"},
                       headers=enf.boss)
    assert r.json()["revoked"] == 1 and block(creators["r"]) == "kyc_required"
    newcomer = owner_user("enf-gt-new")
    r = enf.admin.post(f"{A}/creators/{creators['t']}/owner/transfer",
                       json={"user_id": newcomer, "expected_owner_id": owners["t"]}, headers=enf.boss)
    assert r.status_code == 200 and r.json()["donation_block"] == "kyc_required"  # the new owner has no grace
    verify(newcomer)
    assert block(creators["t"]) is None  # ... and needs their own verification
    assert status_change(enf, creators["s"], "suspend", "active", "check").status_code == 200
    r = status_change(enf, creators["s"], "reactivate", "suspended")
    assert (r.status_code, r.json()["error"]) == (409, "The creator's owner has no approved identity verification "
                                                      "(KYC)")  # the suspension ended its grace
    enf.admin.post(f"{A}/creators/{creators['x']}/kyc/restrictions", json={"reason": "kyc_lost"}, headers=enf.boss)
    lifted = enf.admin.get(f"{A}/creators/{creators['x']}/kyc").json()
    enf.admin.post(f"{A}/creators/{creators['x']}/kyc/restrictions/{lifted['restrictions'][0]['id']}/lift",
                   json={"note": "done"}, headers=enf.boss)
    assert block(creators["x"]) == "kyc_required"  # the restriction revoked the grace
    for k in "rtsx":
        assert "grace_revoked" in events(creators[k]), k
    with db() as conn:
        reasons = dict(conn.execute("SELECT streamer_id::text, revoke_reason FROM public.kyc_grace_grants "
                                    "WHERE streamer_id = ANY(%s::uuid[])", (list(creators.values()),)).fetchall())
    assert (reasons[creators["t"]], reasons[creators["s"]]) == ("owner_changed", "creator_suspended")
    assert reasons[creators["x"]] == "restricted: kyc_lost"


def test_closure_ends_grace_and_nothing_reopens(enf):
    graced = make_streamer("authtest-enf-cl", user_id=owner_user("enf-cl"))
    uid = owner_user("enf-cv")
    verified = make_streamer("authtest-enf-cv", user_id=uid)
    verify(uid)
    assert grant(enf, graced) == 0
    assert operator(enf, "close", "--operator", "ops", "--authorization", "X", *confirm()) == 2  # draft: not yet
    activate(enf)
    assert operator(enf, "close", "--operator", "ops-test", "--authorization", "TEST-CLOSE", *confirm()) == 0
    assert state()[0] == "closed"
    assert block(graced) == "kyc_required" and block(verified) is None
    assert grant(enf, verified) == 2  # no more grace after closure
    assert operator(enf, "activate", "--operator", "ops", "--authorization", "X", *confirm()) == 2
    with fixture_db() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):  # not even the schema owner
        conn.execute("UPDATE public.kyc_enforcement SET state = 'active', generation = generation + 1")
    with fixture_db() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("UPDATE public.kyc_enforcement SET activation_authorization = 'rewritten'")
    with db() as conn:
        record = conn.execute("SELECT activated_by, activation_authorization, activation_migration, policy, "
                              "closed_by, closure_authorization, grace_max_days FROM public.kyc_enforcement").fetchone()
    assert record == ("ops-test", "TEST-APPROVAL-1", "0023", "kyc-pr4-v1", "ops-test", "TEST-CLOSE", 90)
    assert events()[:1] == ["grace_granted"] and "enforcement_activated" in events() and "enforcement_closed" in events()


# ── payments: one rule for checkout, uploads and inserts; existing orders finalize ─

def test_checkout_uploads_and_inserts_follow_one_rule_and_existing_orders_finalize(client, enf):
    sid = make_streamer("authtest-enf-pay", user_id=owner_user("enf-pay"))
    out = checkout(client, "authtest-enf-pay")  # an order created while eligible
    r = enf.admin.post(f"{A}/creators/{sid}/kyc/restrictions", json={"reason": "fraud_review"}, headers=enf.boss)
    assert r.status_code == 200
    r = client.post(f"{P}/authtest-enf-pay/donations", json={"name": "Ravi", "amount": 100}, headers=ORIGIN)
    assert (r.status_code, r.json()) == (503, UNAVAILABLE)  # generic: never the reason
    assert "fraud" not in r.text and "restrict" not in r.text
    with pytest.raises(ApiError) as exc:  # the same gate the upload endpoints use
        client.portal.call(client.app_state.uploads.creators.active, "authtest-enf-pay")
    assert (exc.value.status, exc.value.error) == (503, UNAVAILABLE["error"])
    assert refused(sid)  # and the trigger, for any other insert path
    payment = client.fake.captured(out["razorpay_order_id"], 10000)
    assert webhook(client, "payment.captured", out, payment.id, event_id="authtest-enf-ev1").json() == {
        "status": "completed"}
    assert donation(out["order_id"])["payment_status"] == "success"  # the earlier order still completes


# ── races (lock order: creator row first) ───────────────────────────────────────

def _race(hold_sql: str, args: tuple, sid: str, conn_factory=fixture_db) -> dict:
    """Run `hold_sql` in an open transaction, start a donation insert, prove it waits, then commit."""
    result = {}

    def donate():
        try:
            insert_donation(sid)
            result["ok"] = True
        except psycopg.Error as exc:
            result["sqlstate"] = exc.sqlstate

    with conn_factory() as conn:
        with conn.transaction():
            conn.execute(hold_sql, args)
            th = threading.Thread(target=donate)
            th.start()
            time.sleep(0.5)
            result["waited"] = th.is_alive()
        th.join(20)
    return result


def test_a_donation_racing_a_suspension_or_an_owner_change_waits_and_is_refused(enf):
    uid = owner_user("enf-race")
    verify(uid)
    sid = standard_creator("authtest-enf-race", uid, status="active")
    r = _race("UPDATE public.streamers SET status = 'suspended' WHERE id = %s", (sid,), sid)
    assert r == {"waited": True, "sqlstate": "HC001"}
    with fixture_db() as conn:
        conn.execute("UPDATE public.streamers SET status = 'active' WHERE id = %s", (sid,))
    r = _race("UPDATE public.streamers SET user_id = %s WHERE id = %s", (owner_user("enf-race-b"), sid), sid)
    assert r == {"waited": True, "sqlstate": "HC001"}  # the new owner is unverified


def test_a_kyc_loss_refuses_every_donation_checked_after_it_commits(enf):
    uid = owner_user("enf-kycrace")
    session = verify(uid)
    sid = standard_creator("authtest-enf-kycrace", uid, status="active")
    r = _race("SELECT public.kyc_record_provider_status(%s, 'Declined', clock_timestamp(), NULL, 'webhook', NULL)",
              (session,), sid)
    assert r == {"waited": False, "ok": True}  # checked before the decline committed: ordered before it
    assert refused(sid)                        # every check after the commit refuses


def test_activation_racing_an_owner_change_checks_the_new_owner(enf):
    uid = owner_user("enf-actrace")
    verify(uid)
    sid = standard_creator("authtest-enf-actrace", uid)
    result = {}
    with fixture_db() as conn:
        with conn.transaction():
            conn.execute("UPDATE public.streamers SET user_id = %s WHERE id = %s", (owner_user("enf-actrace-b"), sid))
            th = threading.Thread(target=lambda: result.update(r=status_change(enf, sid, "activate", "pending")))
            th.start()
            time.sleep(0.5)
            assert th.is_alive()  # the activation waits for the creator row
        th.join(20)
    assert result["r"].status_code == 409 and result["r"].json()["error"].startswith("The creator's owner has no")


# ── the runtime role and the audit ──────────────────────────────────────────────

def test_the_runtime_role_cannot_forge_enforcement_grace_or_erase_history(enf):
    sid = make_streamer("authtest-enf-role", user_id=owner_user("enf-role"))
    assert grant(enf, sid) == 0
    with db() as conn:
        for sql, args in (
                ("SELECT public.kyc_enforcement_activate('me', 'me')", ()),
                ("SELECT public.kyc_enforcement_close('me', 'me')", ()),
                ("SELECT public.kyc_grant_grace(%s, 90, 'me', 'me')", (sid,)),
                ("SELECT public.kyc_acknowledge_restore('me', 999, 'me')", ()),
                ("SELECT public.kyc_donation_decision(%s, 'draft')", (sid,)),
                ("SELECT public.kyc_enforcement_log('grace_granted', NULL, NULL, NULL, NULL, 'me', 'me')", ()),
                ("UPDATE public.kyc_enforcement SET state = 'closed'", ()),
                ("UPDATE public.kyc_enforcement SET generation = 0", ()),
                ("INSERT INTO public.kyc_grace_grants (streamer_id, owner_id, expires_at, granted_by, reason) "
                 "SELECT %s, user_id, now() + interval '1 day', 'me', 'me' FROM public.streamers WHERE id = %s",
                 (sid, sid)),
                ("UPDATE public.kyc_grace_grants SET expires_at = expires_at + interval '1 day'", ()),
                ("UPDATE public.kyc_grace_grants SET revoked_at = NULL", ()),
                ("DELETE FROM public.kyc_grace_grants WHERE streamer_id = %s", (sid,)),
                ("INSERT INTO public.kyc_enforcement_events (action) VALUES ('enforcement_activated')", ()),
                ("DELETE FROM public.kyc_enforcement_events", ()),
                ("UPDATE public.kyc_restrictions SET lifted_at = now()", ()),
                ("DELETE FROM public.creator_status_events", ())):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql, args)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):  # deleting the creator cannot erase its history
            conn.execute("DELETE FROM public.streamers WHERE id = %s", (sid,))
    with fixture_db() as conn:  # the guards hold for the schema owner as well
        for sql in ("UPDATE public.kyc_grace_grants SET expires_at = expires_at + interval '1 day'",
                    "UPDATE public.kyc_enforcement_events SET detail = 'x'",
                    "DELETE FROM public.kyc_enforcement"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)
    assert state() == ("draft", 1) and events(sid) == ["grace_granted"]


# ── restore safety (the witness outside PostgreSQL) ─────────────────────────────

def test_a_database_behind_its_witness_refuses_new_donations_until_reconciled(client, enf):
    sid = make_streamer("authtest-enf-wit", user_id=owner_user("enf-wit"))
    assert grant(enf, sid) == 0  # generation 1, witnessed
    generation = state()[1]
    enf.witness.record("draft", generation + 5, action="later_change_lost_by_a_restore")  # what a restore looks like
    r = client.post(f"{P}/authtest-enf-wit/donations", json={"name": "Ravi", "amount": 100}, headers=ORIGIN)
    assert (r.status_code, r.json()) == (503, UNAVAILABLE)
    view = enf.admin.get(f"{A}/kyc/enforcement").json()
    assert view["witness"]["problem"] == "restore_suspected" and view["new_donations_fail_closed"] is True
    assert grant(enf, make_streamer("authtest-enf-wit2", user_id=owner_user("enf-wit2"))) == 2  # no change until then
    assert operator(enf, "acknowledge-restore", "--operator", "ops-test", "--detail", "re-applied lost revocations",
                    *confirm()) == 0
    assert state()[1] == generation + 6 and enf.admin.get(f"{A}/kyc/enforcement").json()["witness"]["problem"] is None
    assert checkout(client, "authtest-enf-wit")["order_id"]
    enf.witness.record("active", generation + 6)  # the witness saw activation; the database never did
    assert enf.admin.get(f"{A}/kyc/enforcement").json()["witness"]["problem"] == "restore_suspected"
    assert operator(enf, "acknowledge-restore", "--operator", "ops", "--detail", "x", *confirm()) == 2  # activate first
    assert "restore_acknowledged" in events()


def test_without_a_witness_only_the_untouched_database_is_accepted(client, enf):
    sid = make_streamer("authtest-enf-nowit", user_id=owner_user("enf-nowit"))
    no_witness = KycWitness(None)
    client.app_state.public.witness = client.app_state.kyc_enforcement.witness = no_witness
    assert checkout(client, "authtest-enf-nowit")["order_id"]  # draft, generation 0: the pre-rollout database
    r = enf.admin.post(f"{A}/creators/{sid}/kyc/restrictions", json={"reason": "admin_hold"}, headers=enf.boss)
    assert (r.status_code, r.json()["error"]) == (503, "KYC enforcement changes need the restore witness "
                                                       "(HYPERCHAT_KYC_WITNESS_PATH)")
    enf.witness = no_witness
    assert grant(enf, sid) == 2  # the operator tool refuses unwitnessed changes as well
    with fixture_db() as conn:  # a change made some other way (generation 1) without a witness
        conn.execute("SELECT public.kyc_grant_grace(%s, 10, 'ops', 'direct')", (sid,))
    r = client.post(f"{P}/authtest-enf-nowit/donations", json={"name": "Ravi", "amount": 100}, headers=ORIGIN)
    assert (r.status_code, r.json()) == (503, UNAVAILABLE)
