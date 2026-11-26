"""Migration 0038 (onboarding Stage 2 Phase B, PDP v1.1 Q3/Q4/Q13/Q14): an approved application's invitation without a
passkey grant, only through onboarding_issue_invite on hyperchat_lifecycle, in BOTH step-up states. Every other way
of writing an application invitation keeps 0027's rule. Synthetic rows only (DOMAIN addresses, authtest- slugs)."""

import psycopg
import psycopg.rows

from tests.integration import DOMAIN, db, fixture_db, make_user, requires_db
from tests.stepup.test_stepup_db import CONSUME, verifier  # noqa: F401  (verifier: the fixture)
from tests.stepup.test_stepup_enforcement_db import (  # noqa: F401  (active, mode: fixtures)
    API_ROLES,
    NEW_CREATOR,
    active,
    approved,
    key_hash,
    mode,
    refused_tx,
    signup,
)

pytestmark = requires_db
LIFECYCLE = "hyperchat_lifecycle"
ISSUE = "SELECT * FROM public.onboarding_issue_invite(%s, %s, %s, %s, %s)"
FUNCTION = "public.onboarding_issue_invite(uuid, text, integer, uuid, boolean)"
INSERT = ("INSERT INTO public.user_invites (email, role, streamer_id, token_hash, expires_at, signup_id) VALUES "
          "(%s, 'streamer', %s, %s, now() + interval '1 day', %s)")
ANNOUNCE = "SELECT set_config('hyperchat.onboarding_invite', %s, true)"


def application(local: str, approve: bool = True) -> tuple[str, str | None]:
    """(signup id, creator id): an application, approved by the operator (no guard) with a new pending creator."""
    sid, cid = signup(local), None
    if approve:
        with fixture_db() as conn:
            cid = conn.execute(NEW_CREATOR, (f"authtest-{local.lower()}", "Inv")).fetchone()[0]
            conn.execute("UPDATE public.user_signups SET review_status = 'approved', streamer_id = %s WHERE id = %s",
                         (cid, sid))
    return sid, cid


def issue(sid: str, *, actor: str | None = None, automatic: bool = True, token: str | None = None,
          ttl: int = 7) -> dict:
    with db(LIFECYCLE) as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        return cur.execute(ISSUE, (sid, token or key_hash(), ttl, actor, automatic)).fetchone()


def invites(sid: str) -> list[tuple]:
    with fixture_db() as conn:
        return conn.execute("SELECT id::text, email::text, role, streamer_id::text, invited_by::text, "
                            "revoked_at IS NOT NULL, expires_at <= now() + interval '7 days' "
                            "FROM public.user_invites WHERE signup_id = %s ORDER BY created_at, id", (sid,)).fetchall()


def events(sid: str) -> list[tuple]:
    with fixture_db() as conn:
        return conn.execute("SELECT action, actor_user_id::text, note FROM public.application_events "
                            "WHERE signup_id = %s ORDER BY created_at, id", (sid,)).fetchall()


# ── the lifecycle function: works in draft and active alike (Q14) ──────────────────────────────────────────

def test_the_function_issues_and_resends_an_approved_applications_invitation_in_both_states(mode):
    sid, cid = application("S3-Inv-Ok")  # mixed case: the invitation carries the application's address exactly
    admin = make_user("s3-inv-ok-boss" + DOMAIN, role="admin")
    first = issue(sid, ttl=30)  # automatic; a longer configured TTL is capped at 7 days (Q4)
    assert (first["o_result"], first["o_email"], first["o_replaced"]) == ("ok", "S3-Inv-Ok" + DOMAIN, 0)
    assert invites(sid) == [(str(first["o_invite_id"]), "S3-Inv-Ok" + DOMAIN, "streamer", cid, None, False, True)]
    assert events(sid) == [("invite", None, "auto")]
    with fixture_db() as conn:
        assert conn.execute("SELECT invite_id::text FROM public.user_signups WHERE id = %s",
                            (sid,)).fetchone()[0] == str(first["o_invite_id"])
    resend = issue(sid, actor=admin, automatic=False)  # Q3: the administrator's resend, no grant
    assert (resend["o_result"], resend["o_replaced"]) == ("ok", 1)
    rows = invites(sid)
    assert [r[5] for r in rows] == [True, False] and rows[1][4] == admin  # the old link revoked, the new one theirs
    assert events(sid)[-1] == ("invite", admin, None)
    assert issue(sid)["o_result"] == "not_due"  # an administrator took over: never automatic again


def test_refusals_change_nothing(mode):
    admin, user = make_user("s3-inv-ref-boss" + DOMAIN, role="admin"), make_user("s3-inv-ref-user" + DOMAIN)
    unapproved, _ = application("s3-inv-unapproved", approve=False)
    assert issue(unapproved)["o_result"] == "not_approved"
    assert issue(unapproved, actor=admin, automatic=False)["o_result"] == "not_approved"
    sid, cid = application("s3-inv-ref")
    assert issue("3f2b9a10-0c1d-4e5f-9a8b-7c6d5e4f3a2b")["o_result"] == "not_found"
    assert issue(sid, actor=admin)["o_result"] == "not_authorized"  # an automatic call names no actor
    for actor in (None, user):  # a manual call names an active administrator
        assert issue(sid, actor=actor, automatic=False)["o_result"] == "not_authorized"
    assert issue(sid, token="not-a-sha256")["o_result"] == "bad_request"
    assert invites(sid) == [] and invites(unapproved) == [] and events(sid) == []
    assert issue(sid, actor=admin, automatic=False)["o_result"] == "ok"
    with fixture_db() as conn:  # the creator got an owner after the invitation was sent
        conn.execute("UPDATE public.streamers SET user_id = %s WHERE id = %s", (user, cid))
    assert issue(sid, actor=admin, automatic=False)["o_result"] == "creator_claimed"
    assert [r[5] for r in invites(sid)] == [False]  # nothing revoked: the refusal is atomic
    existing, _ = application("s3-inv-existing")
    make_user("s3-inv-existing" + DOMAIN)  # Q2: an existing account takes the manual owner-assign path
    assert issue(existing)["o_result"] == issue(existing, actor=admin, automatic=False)["o_result"] == "account_exists"
    assert invites(existing) == []


def test_automatic_invitations_follow_q4(mode):
    """No second automatic link while one is unrevoked and unaccepted (open, or expired after delivery: no automatic
    resend after the 7-day expiry); a failed send revokes it, and then a retry may follow; 5 attempts at most."""
    sid, _ = application("s3-inv-q4")
    assert issue(sid)["o_result"] == "ok"
    assert issue(sid)["o_result"] == "not_due"  # open
    with fixture_db() as conn:
        conn.execute("UPDATE public.user_invites SET expires_at = now() - interval '1 minute' WHERE signup_id = %s",
                     (sid,))
    assert issue(sid)["o_result"] == "not_due"  # delivered and expired: the administrator resends (Q4)
    for _ in range(4):
        with fixture_db() as conn:  # the send failed: the worker revoked the link (app.onboarding.auto_invite)
            conn.execute("UPDATE public.user_invites SET revoked_at = now() WHERE signup_id = %s "
                         "AND revoked_at IS NULL", (sid,))
        assert issue(sid)["o_result"] == "ok"
    with fixture_db() as conn:
        conn.execute("UPDATE public.user_invites SET revoked_at = now() WHERE signup_id = %s AND revoked_at IS NULL",
                     (sid,))
    assert issue(sid)["o_result"] == "attempts_exhausted"
    assert [e[2] for e in events(sid)].count("auto") == 5


def test_a_transaction_that_presented_a_grant_cannot_also_issue_one_without_it(mode):
    """One grant, one action: a consumed grant is claimed by the granted path (0027's anchor), never ignored here."""
    sid, _ = application("s3-inv-grant-app")
    _, args = approved(mode, "s3-inv-grant", "application.invite", f"application:{sid}", {})
    with db(LIFECYCLE) as conn, conn.transaction():
        assert conn.execute(CONSUME, args).fetchone()[0] == "ok"
        assert conn.execute(ISSUE, (sid, key_hash(), 7, None, True)).fetchone()[0] == "grant_presented"
    assert invites(sid) == []


# ── what stays closed: every other writer of an application invitation ──────────────────────────────────────

def test_no_other_writer_gets_the_exception_with_enforcement_active(active):
    """Q13 accepts the lifecycle role (the API or worker process); a bare hyperchat_app (e.g. SQL injection) gains
    nothing: a hand-written application invitation with the right address, creator and even the announced token
    hash is still refused without a grant. Different address, unapproved application and non-application invites
    are refused as before."""
    sid, cid = application("S3-Inv-Closed")
    for role in API_ROLES:
        token = key_hash()
        with refused_tx(role) as conn:  # the lifecycle role has no INSERT at all: permission denied, also 42501
            conn.execute(ANNOUNCE, (token,))
            conn.execute(INSERT, ("S3-Inv-Closed" + DOMAIN, cid, token, sid))
    token = key_hash()
    with refused_tx("hyperchat_app") as conn:  # another case of the same address: not the frozen one either
        conn.execute(ANNOUNCE, (token,))
        conn.execute(INSERT, ("s3-inv-closed" + DOMAIN, cid, token, sid))
    with refused_tx("hyperchat_app") as conn:  # another address
        conn.execute(ANNOUNCE, (token,))
        conn.execute(INSERT, ("s3-inv-attacker" + DOMAIN, cid, token, sid))
    unapproved, _ = application("s3-inv-closed-unapproved", approve=False)
    with refused_tx("hyperchat_app") as conn:  # an unapproved application
        conn.execute(ANNOUNCE, (token,))
        conn.execute(INSERT, ("s3-inv-closed-unapproved" + DOMAIN, cid, token, unapproved))
    with refused_tx("hyperchat_app") as conn:  # a non-application creator invitation: admin.invite, as before
        conn.execute(ANNOUNCE, (token,))
        conn.execute("INSERT INTO public.user_invites (email, role, streamer_id, token_hash, expires_at) VALUES "
                     "(%s, 'streamer', %s, %s, now() + interval '1 day')", ("s3-inv-plain" + DOMAIN, cid, token))
    assert invites(sid) == [] and invites(unapproved) == []
    assert issue(sid)["o_result"] == "ok"  # the one path that is open


def test_only_the_lifecycle_role_may_call_the_function_and_it_has_no_table_access():
    with fixture_db() as conn:
        definer, config, owner = conn.execute(
            "SELECT prosecdef, proconfig, pg_get_userbyid(proowner) FROM pg_proc WHERE oid = %s::regprocedure",
            (FUNCTION,)).fetchone()
        assert definer and owner == "hyperchat_owner" and "search_path=pg_catalog, public, pg_temp" in config
        for role in (*API_ROLES, "hyperchat_readonly", "public"):
            assert conn.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, FUNCTION)).fetchone()[0] \
                == (role == LIFECYCLE), role
        assert not any(conn.execute("SELECT has_table_privilege(%s, 'public.user_invites', %s)",
                                    (LIFECYCLE, p)).fetchone()[0] for p in ("INSERT", "UPDATE", "DELETE", "SELECT"))
