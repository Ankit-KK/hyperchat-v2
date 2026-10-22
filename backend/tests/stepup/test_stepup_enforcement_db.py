"""S3 (migration 0027): the database itself requires the step-up grant on every sensitive administrator write, whatever
code issues it. Each test connects as the API's own database roles, i.e. as a compromised API would, with real grants
from real WebAuthn ceremonies (tests/stepup/test_stepup_db.py helpers). 'active' enforcement is set only in this
disposable test database, by flipping the row with its guard off, and always put back to 'draft'."""

import json
import secrets
import threading
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest

from app.stepup.canonical import canonical_json, sha256_hex
from tests.integration import DOMAIN, db, fixture_db, make_streamer, make_user, requires_db, update_streamer
from tests.stepup.test_stepup_db import (  # noqa: F401  (verifier: the fixture)
    CONSUME,
    REFUSED,
    enrolled,
    grant,
    guards_off,
    verifier,
)

pytestmark = requires_db
API_ROLES = ("hyperchat_app", "hyperchat_worker", "hyperchat_auth", "hyperchat_lifecycle", "hyperchat_stepup")
KEY = "INSERT INTO public.partner_api_keys (partner_slug, key_hash, key_prefix) VALUES (%s, %s, 'hc_x')"
OWNER = "UPDATE public.streamers SET user_id = %s WHERE id = %s"


def set_enforcement(state: str) -> None:
    with guards_off("stepup_enforcement") as conn:  # test-only: the operator's readiness rule is not the subject here
        conn.execute("UPDATE public.stepup_enforcement SET state = %s, changed_by = 'test'", (state,))


@pytest.fixture
def active(verifier):  # noqa: F811
    set_enforcement("active")
    try:
        yield verifier
    finally:
        set_enforcement("draft")


@pytest.fixture
def draft(verifier):  # noqa: F811
    set_enforcement("draft")
    return verifier


def approved(v, name: str, action: str, target: str, params: dict) -> tuple[dict, tuple]:
    """A grant from a real ceremony, and the stepup_consume arguments the API would pass (its own admin and session)."""
    uid, sid, auth = enrolled(v, name)
    g = grant(v, uid, sid, auth, {"action": action, "target": target, "params": params})
    return g, (g["grant_id"], uid, sid, g["action"], g["target"], g["params_hash"])


def grant_row(grant_id: str) -> tuple:
    with fixture_db() as conn:
        return conn.execute("SELECT consumed_at IS NOT NULL, claimed_anchor FROM public.stepup_grants WHERE id = %s",
                            (grant_id,)).fetchone()


def key_hash() -> str:
    return secrets.token_hex(32)


@contextmanager
def refused_tx(role: str):
    """A transaction on `role` that a step-up guard refuses (at the statement, or at commit for a creator insert)."""
    with pytest.raises(REFUSED), db(role) as conn, conn.transaction():
        yield conn


def signup(name: str) -> str:
    with db() as conn:
        return conn.execute("INSERT INTO public.user_signups (name, email, mobile_number) VALUES ('S', %s, "
                            "'+91 90000 00009') RETURNING id::text", (name + DOMAIN,)).fetchone()[0]


NEW_CREATOR = ("INSERT INTO public.streamers (streamer_slug, streamer_name, razorpay_account) "
               "VALUES (%s, %s, 'primary') RETURNING id::text")


# ── the parameter hash ──────────────────────────────────────────────────────────

def test_the_database_hashes_parameters_exactly_as_the_api_does():
    vectors = json.loads(Path(__file__).with_name("canonical_vectors.json").read_text(encoding="utf-8"))["vectors"]
    cases = [v["params"] for v in vectors] + [
        {"email": 'a"b\\c/\x7f é ü 😀   <&>', "n": 9007199254740991, "m": -3, "t": True, "f": False,
         "z": None, "deep": {"b": [1, {"a": "x"}, []], "a": {}}},
        {"z": 1, "a": 2, "_": 3, "0": 4, "aa": 5, "a_b": 6, "a0": 7},
        {"user_id": "3f2b9a10-0c1d-4e5f-9a8b-7c6d5e4f3a2b", "expected_owner_id": None},
    ]
    with fixture_db() as conn:  # owner: no API role may even call it
        for params in cases:
            assert conn.execute("SELECT public.stepup_params_hash(%s::jsonb)",
                                (json.dumps(params),)).fetchone()[0] == sha256_hex(canonical_json(params)), params


# ── draft: unchanged for callers without a grant, fail-closed for a presented one ──

def test_draft_keeps_the_legacy_path_but_a_presented_grant_must_match_its_write(draft):
    uid = make_user("s3-draft-target" + DOMAIN, role="user")
    with db("hyperchat_auth") as conn:  # no grant at all: today's behaviour
        conn.execute("UPDATE public.users SET role = 'streamer' WHERE id = %s", (uid,))
    with db() as conn:
        conn.execute(KEY, ("authtest-s3-draft", key_hash()))
    g, args = approved(draft, "s3-draft", "partner_key.create", "partner_key", {"partner_slug": "authtest-s3-a"})
    with refused_tx("hyperchat_app") as conn:  # the grant approves another slug: refused in draft too
        assert conn.execute(CONSUME, args).fetchone()[0] == "ok"
        conn.execute(KEY, ("authtest-s3-b", key_hash()))
    assert grant_row(g["grant_id"]) == (False, None)  # rolled back: not spent
    with db() as conn, conn.transaction():
        assert conn.execute(CONSUME, args).fetchone()[0] == "ok"
        conn.execute(KEY, ("authtest-s3-a", key_hash()))
    assert grant_row(g["grant_id"]) == (True, "partner_api_keys")


# ── active: every anchor needs its own grant ────────────────────────────────────

def test_a_compromised_api_role_cannot_perform_any_sensitive_write_without_a_grant(active):
    owner, other = make_user("s3-owner" + DOMAIN), make_user("s3-other" + DOMAIN, role="user")
    unclaimed, claimed = make_streamer("authtest-s3-unclaimed"), make_streamer("authtest-s3-claimed", owner)
    suspended = make_streamer("authtest-s3-suspended", owner)
    update_streamer(suspended, status="suspended")
    actor = make_user("s3-actor" + DOMAIN, role="admin")  # p_actor is only consistency (0023): the grant is not
    attempts = [
        ("hyperchat_app", OWNER, (other, unclaimed)),                              # creator.owner.assign
        ("hyperchat_app", OWNER, (other, claimed)),                                # creator.owner.transfer
        ("hyperchat_auth", OWNER, (other, unclaimed)),                             # (any API role)
        ("hyperchat_app", NEW_CREATOR, ("authtest-s3-new", "New")),                # creator.create (at commit)
        ("hyperchat_lifecycle", "SELECT public.kyc_change_creator_status(%s, 'reactivate', 'suspended', %s, NULL, "
                                "NULL)", (suspended, actor)),                       # creator.reactivate (definer)
        ("hyperchat_auth", "UPDATE public.users SET role = 'streamer' WHERE id = %s", (other,)),  # admin.user.change
        ("hyperchat_auth", "UPDATE public.users SET is_active = false WHERE id = %s", (other,)),
        ("hyperchat_app", "UPDATE public.users SET streamer_id = %s WHERE id = %s", (unclaimed, other)),
        ("hyperchat_auth", "UPDATE public.users SET email = %s WHERE id = %s", ("s3-new" + DOMAIN, other)),
        ("hyperchat_auth", "INSERT INTO public.user_invites (email, role, token_hash, expires_at) VALUES "
                           "(%s, 'admin', %s, now() + interval '1 day')", ("s3-inv" + DOMAIN, key_hash())),
        ("hyperchat_auth", "INSERT INTO public.user_invites (email, role, streamer_id, token_hash, expires_at) VALUES "
                           "(%s, 'streamer', %s, %s, now() + interval '1 day')",
         ("s3-inv2" + DOMAIN, unclaimed, key_hash())),                             # a creator link: admin.invite
        ("hyperchat_app", KEY, ("authtest-s3-key", key_hash())),                   # partner_key.create
        ("hyperchat_app", "UPDATE public.streamers SET user_id = NULL WHERE id = %s", (claimed,)),  # never
        ("hyperchat_app", "UPDATE public.streamers SET created_at = now() WHERE id = %s", (claimed,)),  # never
    ]
    for role, sql, args in attempts:
        with refused_tx(role) as conn:
            conn.execute(sql, args)
    with fixture_db() as conn:  # nothing happened
        assert conn.execute("SELECT user_id::text FROM public.streamers WHERE id = ANY(%s::uuid[]) ORDER BY "
                            "streamer_slug", ([unclaimed, claimed],)).fetchall() == [(owner,), (None,)]
        assert conn.execute("SELECT status FROM public.streamers WHERE id = %s", (suspended,)).fetchone()[0] == "suspended"
        assert conn.execute("SELECT role, is_active, email::text FROM public.users WHERE id = %s",
                            (other,)).fetchone() == ("user", True, "s3-other" + DOMAIN)
        assert conn.execute("SELECT count(*) FROM public.streamers WHERE streamer_slug = 'authtest-s3-new'").fetchone()[0] == 0
    # Ordinary, non-sensitive writes are untouched.
    with db() as conn:
        conn.execute("UPDATE public.streamers SET streamer_name = 'Renamed' WHERE id = %s", (claimed,))
        conn.execute("INSERT INTO public.user_invites (email, role, token_hash, expires_at) VALUES "
                     "(%s, 'user', %s, now() + interval '1 day')", ("s3-plain" + DOMAIN, key_hash()))
    with fixture_db() as conn:  # the operator (the migrator acting as owner) stays the trusted boundary
        conn.execute(OWNER, (other, unclaimed))


def test_a_grant_authorizes_exactly_its_own_write_once_in_its_own_transaction(active):
    g, args = approved(active, "s3-once", "partner_key.create", "partner_key", {"partner_slug": "authtest-s3-once"})
    with refused_tx("hyperchat_app") as conn:  # one grant, two anchor writes: the second needs its own grant
        assert conn.execute(CONSUME, args).fetchone()[0] == "ok"
        conn.execute(KEY, ("authtest-s3-once", key_hash()))
        conn.execute(KEY, ("authtest-s3-once", key_hash()))
    assert grant_row(g["grant_id"]) == (False, None)  # the whole transaction rolled back: the grant is not spent
    with db() as conn:  # consumed in one transaction (committed) ...
        assert conn.execute(CONSUME, args).fetchone()[0] == "ok"
    with refused_tx("hyperchat_app") as conn:  # ... authorizes nothing in a later one
        conn.execute(KEY, ("authtest-s3-once", key_hash()))
    assert grant_row(g["grant_id"]) == (True, None)


@pytest.mark.parametrize("wrong", ["action", "target", "params", "admin", "session"])
def test_a_grant_bound_to_anything_else_authorizes_nothing(active, wrong):
    sid = make_streamer("authtest-s3-wrong-" + wrong)
    uid = make_user(f"s3-wrong-{wrong}-u" + DOMAIN)
    with fixture_db() as conn:
        conn.execute("UPDATE public.streamers SET user_id = NULL WHERE id = %s", (sid,))
    action, target, params = "creator.owner.assign", f"creator:{sid}", {"user_id": uid}
    if wrong == "action":
        action = "creator.owner.transfer"
    elif wrong == "target":
        target = f"creator:{make_streamer('authtest-s3-wrong-other')}"
    elif wrong == "params":
        params = {"user_id": make_user("s3-wrong-params-v" + DOMAIN)}
    g, args = approved(active, f"s3-wrong-{wrong}", action, target, params)
    if wrong in ("admin", "session"):  # the API's own admin / session must be the grant's: stepup_consume refuses
        other_uid, other_sid, _ = enrolled(active, f"s3-wrong-{wrong}-2")
        args = (args[0], other_uid if wrong == "admin" else args[1], other_sid, *args[3:])
    with refused_tx("hyperchat_app") as conn:
        if wrong in ("admin", "session"):
            assert conn.execute(CONSUME, args).fetchone()[0] == "mismatch"
        else:
            assert conn.execute(CONSUME, args).fetchone()[0] == "ok"
        conn.execute(OWNER, (uid, sid))
    assert grant_row(g["grant_id"]) == (False, None)


def test_a_rolled_back_savepoint_returns_the_claim_and_a_rolled_back_consumption_returns_the_grant(active):
    g, args = approved(active, "s3-savepoint", "partner_key.create", "partner_key", {"partner_slug": "authtest-s3-sp"})
    with db() as conn, conn.transaction():
        assert conn.execute(CONSUME, args).fetchone()[0] == "ok"
        with pytest.raises(psycopg.errors.DivisionByZero), conn.transaction():  # the operation fails after its claim
            conn.execute(KEY, ("authtest-s3-sp", key_hash()))
            conn.execute("SELECT 1/0")
        conn.execute(KEY, ("authtest-s3-sp", key_hash()))  # the claim was rolled back with the savepoint
    assert grant_row(g["grant_id"]) == (True, "partner_api_keys")
    with fixture_db() as conn:
        assert conn.execute("SELECT count(*) FROM public.partner_api_keys WHERE partner_slug = 'authtest-s3-sp'"
                            ).fetchone()[0] == 1
    g2, args2 = approved(active, "s3-savepoint-2", "partner_key.create", "partner_key", {"partner_slug": "authtest-s3-sp2"})
    with refused_tx("hyperchat_app") as conn:
        with pytest.raises(psycopg.errors.DivisionByZero), conn.transaction():
            assert conn.execute(CONSUME, args2).fetchone()[0] == "ok"
            conn.execute("SELECT 1/0")
        conn.execute(KEY, ("authtest-s3-sp2", key_hash()))  # its consumption was rolled back too
    assert grant_row(g2["grant_id"]) == (False, None)


def test_concurrent_writes_with_one_grant_succeed_exactly_once(active):
    g, args = approved(active, "s3-race", "partner_key.create", "partner_key", {"partner_slug": "authtest-s3-race"})
    first = db()
    second_result: list = []

    def second():
        try:
            with db() as conn, conn.transaction():
                second_result.append(conn.execute(CONSUME, args).fetchone()[0])  # waits for the first
                conn.execute(KEY, ("authtest-s3-race", key_hash()))
        except psycopg.Error as exc:
            second_result.append(type(exc))

    with first:
        with first.transaction():
            assert first.execute(CONSUME, args).fetchone()[0] == "ok"
            first.execute(KEY, ("authtest-s3-race", key_hash()))
            t = threading.Thread(target=second)
            t.start()
            t.join(1)
            assert t.is_alive()  # blocked on the grant row
        t.join(20)
    assert second_result == ["consumed", REFUSED]
    with fixture_db() as conn:
        assert conn.execute("SELECT count(*) FROM public.partner_api_keys WHERE partner_slug = 'authtest-s3-race'"
                            ).fetchone()[0] == 1


# ── anchors and effects of the real operations ──────────────────────────────────

def test_a_reactivation_through_the_lifecycle_function_claims_its_grant(active):
    owner = make_user("s3-react-owner" + DOMAIN)
    sid = make_streamer("authtest-s3-react", owner)
    update_streamer(sid, status="suspended")
    g, args = approved(active, "s3-react", "creator.reactivate", f"creator:{sid}", {"expected_status": "suspended"})
    with db("hyperchat_lifecycle") as conn, conn.transaction():
        assert conn.execute(CONSUME, args).fetchone()[0] == "ok"
        assert conn.execute("SELECT public.kyc_change_creator_status(%s, 'reactivate', 'suspended', %s, NULL, NULL)",
                            (sid, args[1])).fetchone()[0] == "ok"
    assert grant_row(g["grant_id"]) == (True, "streamers.status")


def test_a_transfer_unlinks_only_the_previous_owner_once(active):
    old, new = make_user("s3-tr-old" + DOMAIN), make_user("s3-tr-new" + DOMAIN)
    sid = make_streamer("authtest-s3-transfer", old)
    staff = make_user("s3-tr-staff" + DOMAIN, streamer_id=sid)
    with fixture_db() as conn:
        conn.execute("UPDATE public.users SET streamer_id = %s WHERE id = %s", (sid, old))
    g, args = approved(active, "s3-transfer", "creator.owner.transfer", f"creator:{sid}",
                       {"user_id": new, "expected_owner_id": old})
    with refused_tx("hyperchat_app") as conn:  # the grant does not unlink anyone else (here: a staff account)
        assert conn.execute(CONSUME, args).fetchone()[0] == "ok"
        conn.execute(OWNER, (new, sid))
        conn.execute("UPDATE public.users SET streamer_id = NULL WHERE id = %s", (staff,))
    with db() as conn, conn.transaction():
        assert conn.execute(CONSUME, args).fetchone()[0] == "ok"
        conn.execute(OWNER, (new, sid))
        conn.execute("UPDATE public.users SET streamer_id = NULL WHERE id = %s", (old,))
    assert grant_row(g["grant_id"]) == (True, "streamers.user_id")
    with fixture_db() as conn:
        assert conn.execute("SELECT effect FROM public.stepup_grant_effects WHERE grant_id = %s",
                            (g["grant_id"],)).fetchall() == [("old_owner_unlinked",)]


def test_an_approval_claims_its_grant_and_records_the_creator_it_creates(active):
    params = {"slug": "authtest-s3-approved", "name": "Approved", "razorpay_account": "primary"}
    app = signup("s3-approve")
    g, args = approved(active, "s3-approve", "application.approve", f"application:{app}", params)
    old = make_streamer("authtest-s3-old-creator", name="Old")
    with fixture_db() as conn:
        slug, name, account = conn.execute("SELECT streamer_slug, streamer_name, razorpay_account FROM public.streamers "
                                           "WHERE id = %s", (old,)).fetchone()
    _, adopt = approved(active, "s3-adopt", "application.approve", f"application:{app}",
                        {"slug": slug, "name": name, "razorpay_account": account})
    with refused_tx("hyperchat_app") as conn:  # an approval never adopts an existing creator, even one it names
        assert conn.execute(CONSUME, adopt).fetchone()[0] == "ok"
        conn.execute("UPDATE public.user_signups SET review_status = 'approved', streamer_id = %s WHERE id = %s",
                     (old, app))
    with refused_tx("hyperchat_app") as conn:  # a creator without its approval: creator.create, not this grant
        assert conn.execute(CONSUME, args).fetchone()[0] == "ok"
        conn.execute(NEW_CREATOR, (params["slug"], params["name"]))
    with db() as conn, conn.transaction():
        assert conn.execute(CONSUME, args).fetchone()[0] == "ok"
        cid = conn.execute(NEW_CREATOR, (params["slug"], params["name"])).fetchone()[0]
        conn.execute("UPDATE public.user_signups SET review_status = 'approved', streamer_id = %s WHERE id = %s",
                     (cid, app))
    assert grant_row(g["grant_id"]) == (True, "user_signups.review_status")
    with fixture_db() as conn:
        assert conn.execute("SELECT effect FROM public.stepup_grant_effects WHERE grant_id = %s",
                            (g["grant_id"],)).fetchall() == [("creator_created",)]
    with refused_tx("hyperchat_app") as conn:  # final: its creator and address cannot be swapped afterwards
        conn.execute("UPDATE public.user_signups SET streamer_id = %s WHERE id = %s", (old, app))
    g2, args2 = approved(active, "s3-approve-inv", "application.invite", f"application:{app}", {})
    invite = ("INSERT INTO public.user_invites (email, role, streamer_id, token_hash, expires_at, signup_id) VALUES "
              "(%s, 'streamer', %s, %s, now() + interval '1 day', %s)")
    for email, creator in (("s3-someone-else" + DOMAIN, cid), ("s3-approve" + DOMAIN, old)):
        with refused_tx("hyperchat_app") as conn:  # its invitation: only the application's address and creator
            assert conn.execute(CONSUME, args2).fetchone()[0] == "ok"
            conn.execute(invite, (email, creator, key_hash(), app))
    with db() as conn, conn.transaction():
        assert conn.execute(CONSUME, args2).fetchone()[0] == "ok"
        conn.execute(invite, ("S3-Approve" + DOMAIN, cid, key_hash(), app))
    assert grant_row(g2["grant_id"]) == (True, "user_invites.insert")


def test_invite_acceptance_stays_internal_but_cannot_be_forged(active):
    sid = make_streamer("authtest-s3-accept")
    with fixture_db() as conn:  # an invitation issued earlier (its issue was the guarded anchor)
        open_, revoked = (conn.execute(
            "INSERT INTO public.user_invites (email, role, streamer_id, token_hash, expires_at, revoked_at) VALUES "
            "(%s, 'streamer', %s, %s, now() + interval '1 day', " + r + ") RETURNING id::text",
            (f"s3-acc-{n}" + DOMAIN, sid, key_hash())).fetchone()[0] for n, r in (("o", "NULL"), ("r", "now()")))
    accept = "UPDATE public.user_invites SET accepted_at = now() WHERE id = %s"
    insert = ("INSERT INTO public.users (email, role, streamer_id) VALUES (%s, 'streamer', %s) RETURNING id::text")
    with refused_tx("hyperchat_auth") as conn:  # a revoked invitation is never accepted
        conn.execute(accept, (revoked,))
    with refused_tx("hyperchat_auth") as conn:  # no creator-linked account without an invitation accepted now
        conn.execute(insert, ("s3-acc-x" + DOMAIN, sid))
    with refused_tx("hyperchat_auth") as conn:  # an invitation's terms never change
        conn.execute("UPDATE public.user_invites SET role = 'admin' WHERE id = %s", (open_,))
    make_user("s3-acc-admin" + DOMAIN, role="admin")  # an administrator who can sign in exists
    with refused_tx("hyperchat_auth") as conn:  # so no new admin row (bootstrap only)
        conn.execute("INSERT INTO public.users (email, role, password_hash) VALUES (%s, 'admin', 'x')",
                     ("s3-acc-boot" + DOMAIN,))
    with db("hyperchat_auth") as conn, conn.transaction():  # the genuine acceptance needs no grant
        conn.execute(accept, (open_,))
        uid = conn.execute(insert, ("s3-acc-o" + DOMAIN, sid)).fetchone()[0]
        conn.execute("UPDATE public.user_invites SET accepted_user_id = %s WHERE id = %s", (uid, open_))
    with refused_tx("hyperchat_auth") as conn:  # and happens once
        conn.execute("UPDATE public.user_invites SET accepted_at = now(), accepted_user_id = NULL WHERE id = %s",
                     (open_,))


# ── the guards' own surface ─────────────────────────────────────────────────────

def test_no_api_role_can_call_the_guards_and_every_guard_pins_its_search_path():
    with fixture_db() as conn:
        rows = conn.execute("""
            SELECT p.oid::regprocedure::text, p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner)
            FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace
              AND (p.proname LIKE 'stepup_guard_%' OR p.proname IN ('stepup_authorize', 'stepup_effect_or_legacy',
                                                                    'stepup_params_hash', 'stepup_canonical_json'))
              AND p.proname NOT IN ('stepup_guard_credentials', 'stepup_guard_challenges', 'stepup_guard_grants',
                                    'stepup_guard_effects', 'stepup_guard_assurance', 'stepup_guard_events',
                                    'stepup_guard_enforcement')
        """).fetchall()
        assert len(rows) == 10
        for fn, definer, config, owner in rows:
            assert owner == "hyperchat_owner" and any(c.startswith("search_path=pg_catalog, public") for c in config), fn
            for role in (*API_ROLES, "hyperchat_readonly", "public"):
                assert not conn.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, fn)).fetchone()[0], \
                    (role, fn)
        assert conn.execute("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'aac_stepup_guard_%' "
                            "AND tgenabled = 'O'").fetchone()[0] == 6
