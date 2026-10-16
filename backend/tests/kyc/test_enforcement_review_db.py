"""KYC PR4 review regressions (PR #71): a restored snapshot cannot be "caught up" past its witness by ordinary
changes, the API's database role cannot remove security holds by itself, and a revoked or expired verification never
falls back to legacy grace. Real PostgreSQL, synthetic rows only."""

import json
import threading
import time

import psycopg
import pytest

from tests.integration import ORIGIN, db, fixture_db, make_streamer, requires_db
from tests.kyc.test_enforcement_db import (  # noqa: F401 (fixtures)
    A,
    P,
    UNAVAILABLE,
    activate,
    block,
    client,
    confirm,
    enf,
    events,
    grant,
    insert_donation,
    operator,
    owner_user,
    refused,
    rz_env,
    set_kyc,
    standard_creator,
    state,
    status_change,
    verify,
)

pytestmark = requires_db


def simulate_restore(to_generation: int, grant_id: str) -> None:
    """What restoring an older snapshot does: the enforcement history after `to_generation` is gone and the grant that
    was revoked later is unrevoked again. Scratch only (the schema owner disables the guards)."""
    with fixture_db() as conn:
        for table, trigger in (("kyc_enforcement", "kyc_enforcement_guard"),
                               ("kyc_grace_grants", "kyc_grace_grants_guard")):
            conn.execute(f"ALTER TABLE public.{table} DISABLE TRIGGER {trigger}")
        conn.execute("UPDATE public.kyc_grace_grants SET revoked_at = NULL, revoked_by = NULL, revoke_reason = NULL "
                     "WHERE id = %s", (grant_id,))
        conn.execute("DELETE FROM public.kyc_enforcement_events WHERE generation > %s", (to_generation,))
        conn.execute("UPDATE public.kyc_enforcement SET generation = %s", (to_generation,))
        if conn.execute("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'kyc_enforcement' "
                        "AND column_name = 'chain')").fetchone()[0]:  # the history head goes back with the snapshot
            conn.execute("UPDATE public.kyc_enforcement SET chain = (SELECT chain FROM public.kyc_enforcement_events "
                         "WHERE generation = %s)", (to_generation,))
        for table, trigger in (("kyc_enforcement", "kyc_enforcement_guard"),
                               ("kyc_grace_grants", "kyc_grace_grants_guard")):
            conn.execute(f"ALTER TABLE public.{table} ENABLE TRIGGER {trigger}")


def witness_problem(app) -> str | None:
    return app.admin.get(f"{A}/kyc/enforcement").json()["witness"]["problem"]


# ── 1. a restore mismatch is never cleared by advancing the generation ───────────

def test_a_restore_cannot_be_caught_up_by_ordinary_changes(client, enf):
    a = make_streamer("authtest-rv-a", user_id=owner_user("rv-a"))
    b = make_streamer("authtest-rv-b", user_id=owner_user("rv-b"))
    assert grant(enf, a) == 0
    activate(enf)
    with db() as conn:
        grant_id = conn.execute("SELECT id::text FROM public.kyc_grace_grants WHERE streamer_id = %s", (a,)).fetchone()[0]
    restored_to = state()[1]
    r = enf.admin.post(f"{A}/creators/{a}/kyc/grace/revoke", json={"reason": "fraud signal"}, headers=enf.boss)
    assert r.json()["revoked"] == 1 and block(a) == "kyc_required"
    simulate_restore(restored_to, grant_id)  # the revocation is lost; the witness is one change ahead
    assert block(a) is None  # inside PostgreSQL the restored grace is valid again ...
    r = client.post(f"{P}/authtest-rv-a/donations", json={"name": "Ravi", "amount": 100}, headers=ORIGIN)
    assert (r.status_code, r.json()) == (503, UNAVAILABLE)  # ... but the API fails closed
    assert witness_problem(enf) == "restore_suspected"
    # An unrelated admin change advances the generation to the witness's: the mismatch must NOT clear.
    r = enf.admin.post(f"{A}/creators/{b}/kyc/restrictions", json={"reason": "admin_hold"}, headers=enf.boss)
    assert r.status_code == 200 and witness_problem(enf) == "restore_suspected"
    with db() as conn:  # nor can the API's database role catch up directly, however far it goes
        for _ in range(3):
            conn.execute("SELECT public.kyc_restrict_creator(%s, 'admin_hold', 'catch-up', NULL)", (b,))
    assert state()[1] > restored_to + 3 and witness_problem(enf) == "restore_suspected"
    r = client.post(f"{P}/authtest-rv-a/donations", json={"name": "Ravi", "amount": 100}, headers=ORIGIN)
    assert (r.status_code, r.json()) == (503, UNAVAILABLE)
    assert grant(enf, make_streamer("authtest-rv-c", user_id=owner_user("rv-c"))) == 2  # no operator change either
    # Reconciliation: re-apply the lost revocation (fail-closed changes stay allowed), then the operator's audited
    # acknowledgement re-anchors the witness.
    r = enf.admin.post(f"{A}/creators/{a}/kyc/grace/revoke", json={"reason": "re-applied after restore"},
                       headers=enf.boss)
    assert r.json()["revoked"] == 1 and witness_problem(enf) == "restore_suspected"
    assert operator(enf, "acknowledge-restore", "--operator", "ops-test", "--detail", "revocation re-applied",
                    *confirm()) == 0
    assert witness_problem(enf) is None and block(a) == "kyc_required"
    assert "restore_acknowledged" in events()


def test_a_witness_that_lags_a_commit_is_caught_up_but_never_re_anchored(enf):
    """The commit-to-file window: a change committed without a witness line (a crash, or a direct database-role call)
    is a consistent extension of the witnessed history, accepted; the next witnessed change records it."""
    a = make_streamer("authtest-rv-lag", user_id=owner_user("rv-lag"))
    enf.admin.post(f"{A}/creators/{a}/kyc/restrictions", json={"reason": "admin_hold"}, headers=enf.boss)
    with db() as conn:  # direct call: committed, never witnessed
        conn.execute("SELECT public.kyc_restrict_creator(%s, 'admin_hold', NULL, NULL)", (a,))
    assert witness_problem(enf) is None  # the database is ahead of the witness on the same history
    enf.admin.post(f"{A}/creators/{a}/kyc/restrictions", json={"reason": "kyc_lost"}, headers=enf.boss)
    with open(enf.witness.path, encoding="utf-8") as f:
        assert [json.loads(line)["generation"] for line in f] == [1, 3]  # the second line covers both changes


# ── 2. the API's database role cannot remove security holds ─────────────────────

def test_the_runtime_role_cannot_lift_a_restriction_or_bypass_activation_checks(enf):
    sid = make_streamer("authtest-rv-hold", user_id=owner_user("rv-hold"))
    r = enf.admin.post(f"{A}/creators/{sid}/kyc/restrictions", json={"reason": "fraud_review"}, headers=enf.boss)
    rid = r.json()["restrictions"][0]["id"]
    with db() as conn:  # the application's credentials, bypassing the HTTP route
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT public.kyc_lift_restriction(%s, %s, 'trust me', NULL)", (sid, rid))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE public.kyc_restrictions SET lifted_at = now(), lift_note = 'x' WHERE id = %s", (rid,))
    assert block(sid) == "restricted"
    assert enf.admin.post(f"{A}/creators/{sid}/kyc/restrictions/{rid}/lift", json={"note": "n"},
                          headers=enf.boss).status_code in (404, 405)  # no API lift any more
    # Suspended + restricted, then a direct status write: refused (the trigger re-checks the rule).
    assert status_change(enf, sid, "suspend", "active", "hold").status_code == 200
    with db() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("UPDATE public.streamers SET status = 'active' WHERE id = %s", (sid,))
    pending = standard_creator("authtest-rv-pend", owner_user("rv-pend"))  # unverified owner
    with db() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("UPDATE public.streamers SET status = 'active' WHERE id = %s", (pending,))
    # The operator's lift: explicit, audited, and it never bypasses KYC.
    assert operator(enf, "lift-restriction", "--creator", sid, "--restriction", rid, "--note", "resolved",
                    "--operator", "ops-test", *confirm()) == 0
    assert operator(enf, "lift-restriction", "--creator", sid, "--restriction", rid, "--note", "again",
                    "--operator", "ops-test", *confirm()) == 2
    assert block(sid) == "not_active" and events(sid)[-1] == "restriction_lifted"
    assert status_change(enf, sid, "reactivate", "suspended").status_code == 200  # draft: PR1 baseline again
    assert block(sid) is None


# ── 3. a revoked or expired verification never falls back to grace ──────────────

def test_a_revoked_or_expired_verification_never_falls_back_to_grace(enf):
    u, v, w, x = (owner_user(f"rv-g{k}") for k in "uvwx")
    graced = {k: make_streamer(f"authtest-rv-g{k}", user_id=uid) for k, uid in zip("uvwx", (u, v, w, x))}
    for k in "uvw":
        assert grant(enf, graced[k]) == 0
    activate(enf)
    assert block(graced["u"]) is None                 # grace
    session = verify(u)
    assert block(graced["u"]) is None                 # KYC
    set_kyc(session, "Declined")
    assert block(graced["u"]) == "kyc_required" and refused(graced["u"])  # not grace again
    set_kyc(session, "Resubmitted")
    assert block(graced["u"]) == "kyc_required"
    set_kyc(session, "Approved")                      # a later signed re-approval restores it (KYC, not grace)
    assert block(graced["u"]) is None
    set_kyc(session, "Kyc Expired")
    assert block(graced["u"]) == "kyc_required"
    verify(v, "Declined")                             # a failed first attempt ends grace too
    assert block(graced["v"]) == "kyc_required"
    verify(w, "In Progress")                          # still undecided: grace continues
    assert block(graced["w"]) is None
    verify(x, "Declined")
    assert grant(enf, graced["x"]) == 2               # no grace for an owner with a KYC decision


def test_a_kyc_loss_racing_a_graced_donation_never_reopens_grace(enf):
    uid = owner_user("rv-race")
    sid = make_streamer("authtest-rv-race", user_id=uid)
    assert grant(enf, sid) == 0
    activate(enf)
    session = verify(uid)
    result = {}
    with fixture_db() as conn:
        with conn.transaction():
            set_kyc(session, "Declined", conn)
            th = threading.Thread(target=lambda: result.update(ok=not refused(sid)))
            th.start()
            time.sleep(0.5)
        th.join(20)
    assert result["ok"] is True       # checked before the decline committed: ordered before it
    assert refused(sid)               # after it: refused, the grant notwithstanding
    insert = None
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.kyc_grace_grants WHERE streamer_id = %s AND revoked_at IS NULL",
                            (sid,)).fetchone()[0] == 1 and insert is None  # the grant exists; it no longer applies
