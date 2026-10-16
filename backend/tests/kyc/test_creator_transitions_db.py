"""KYC PR4 review (PR #71, second round): eligibility is necessary but not sufficient. A creator's status changes only
through public.kyc_change_creator_status (validation + transition + audit in one transaction), never by a raw
application-role write. Real PostgreSQL, synthetic rows only."""

import psycopg
import pytest

from tests.integration import db, make_streamer, make_user, requires_db
from tests.kyc.test_enforcement_db import (  # noqa: F401 (fixtures)
    block,
    enf,
    owner_user,
    standard_creator,
    status_change,
    verify,
)

pytestmark = requires_db


def status_of(sid: str) -> str:
    with db() as conn:
        return conn.execute("SELECT status FROM public.streamers WHERE id = %s", (sid,)).fetchone()[0]


def audit(sid: str) -> list[str]:
    with db() as conn:
        return [r[0] for r in conn.execute("SELECT action FROM public.creator_status_events WHERE streamer_id = %s "
                                           "ORDER BY created_at, id", (sid,))]


def raw(sql: str, args: tuple) -> None:
    """The application's database credentials, bypassing the admin API."""
    with db() as conn:
        conn.execute(sql, args)


def test_a_suspended_legacy_creator_is_not_reactivated_by_a_raw_update(enf):
    sid = make_streamer("authtest-tr-legacy", user_id=owner_user("tr-legacy"))  # draft, baseline, unverified
    assert status_change(enf, sid, "suspend", "active", "check").status_code == 200
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        raw("UPDATE public.streamers SET status = 'active' WHERE id = %s", (sid,))
    assert status_of(sid) == "suspended" and block(sid) == "not_active" and audit(sid) == ["suspend"]
    assert status_change(enf, sid, "reactivate", "suspended").status_code == 200  # the admin path still works
    assert block(sid) is None and audit(sid) == ["suspend", "reactivate"]


def test_a_pending_verified_creator_is_not_activated_by_a_raw_update(enf):
    uid = owner_user("tr-std")
    verify(uid)
    sid = standard_creator("authtest-tr-std", uid)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        raw("UPDATE public.streamers SET status = 'active' WHERE id = %s", (sid,))
    assert status_of(sid) == "pending" and audit(sid) == []
    r = status_change(enf, sid, "activate", "pending")
    assert r.status_code == 200 and block(sid) is None and audit(sid) == ["activate"]


def test_raw_inserts_and_other_status_writes_are_refused(enf):
    uid = owner_user("tr-ins")
    verify(uid)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # a new creator born active
        raw("INSERT INTO public.streamers (streamer_slug, streamer_name, user_id, status, razorpay_account) "
            "VALUES ('authtest-tr-ins', 'X', %s, 'active', 'primary')", (uid,))
    raw("INSERT INTO public.streamers (streamer_slug, streamer_name, user_id) VALUES ('authtest-tr-ok', 'X', %s)",
        (uid,))  # pending, as the admin API creates it
    sid = make_streamer("authtest-tr-sus", user_id=owner_user("tr-sus"))
    for status in ("suspended", "rejected", "pending"):  # no unaudited status change at all
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            raw("UPDATE public.streamers SET status = %s WHERE id = %s", (status, sid))
    assert status_of(sid) == "active" and audit(sid) == []


def test_the_transition_function_validates_and_audits_whoever_calls_it(enf):
    """The application role may call it (it is the admin API's path), but only for a real active admin, and it
    always applies every check and writes the audit row in the same transaction."""
    uid = owner_user("tr-fn")
    sid = standard_creator("authtest-tr-fn", uid)
    nobody = make_user("tr-nobody@authtest.invalid")
    admin = make_user("tr-admin@authtest.invalid", role="admin")
    call = "SELECT public.kyc_change_creator_status(%s, 'activate', 'pending', %s, NULL, ARRAY['primary'])"
    with db() as conn:
        assert conn.execute(call, (sid, nobody)).fetchone()[0] == "not_authorized"
        assert conn.execute(call, (sid, admin)).fetchone()[0] == "kyc_required"  # eligibility first
        verify(uid)
        assert conn.execute("SELECT public.kyc_change_creator_status(%s, 'activate', 'pending', %s, NULL, "
                            "ARRAY['secondary'])", (sid, admin)).fetchone()[0] == "account_not_configured"
        assert conn.execute("SELECT public.kyc_change_creator_status(%s, 'activate', 'active', %s, NULL, "
                            "ARRAY['primary'])", (sid, admin)).fetchone()[0] == "status_changed"  # compare-and-set
        assert conn.execute(call, (sid, admin)).fetchone()[0] == "ok"
    assert status_of(sid) == "active" and audit(sid) == ["activate"]
    with db() as conn:
        actor = conn.execute("SELECT actor_user_id::text FROM public.creator_status_events WHERE streamer_id = %s",
                             (sid,)).fetchone()[0]
    assert actor == admin
