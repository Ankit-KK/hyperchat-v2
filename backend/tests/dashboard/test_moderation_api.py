"""G3 moderation: queue (D-R5) and the transactional state machine (D-M2) against a scratch database."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.cli import connection_factory
from app.core.config import Settings
from app.dashboard.repository import DashboardRepository
from tests.integration import (
    fixture_db,
    DOMAIN,
    creator_state,
    db,
    make_donation,
    make_streamer,
    make_user,
    owned_creator,
    requires_db,
    signed_in,
)

pytestmark = requires_db
S = "/api/v1/dashboard/streamers"
QUEUE_FIELDS = {"donation_id", "donor_name", "amount", "currency", "amount_inr", "message", "message_visible",
                "is_hyperemote", "voice_message_url", "tts_audio_url", "hypersound_url", "media_url", "media_type",
                "moderation_status", "created_at", "approved_at", "approved_by_label", "is_donor_banned"}


def _moderate(client, ref, donation_id, action, headers, **extra):
    return client.post(f"{S}/{ref}/donations/{donation_id}/moderation", json={"action": action, **extra},
                       headers=headers)


def _donation(did: str) -> dict:
    with db() as conn:
        row = conn.execute("SELECT moderation_status, message_visible, approved_at, audio_scheduled_at, "
                           "approved_by_label, approved_by_moderator_id FROM public.donations WHERE id = %s",
                           (did,)).fetchone()
    return dict(zip(("status", "visible", "approved_at", "audio_scheduled_at", "label", "moderator"), row))


def _totals(sid: str, name: str = "Fan") -> tuple | None:
    with db() as conn:
        return conn.execute("SELECT total_amount, donation_count FROM public.streamer_donator_totals "
                            "WHERE streamer_id = %s AND donator_name_lower = lower(%s)", (sid, name)).fetchone()


def _seed_totals(sid: str, name: str, total, count: int) -> None:
    with db() as conn:
        conn.execute("INSERT INTO public.streamer_donator_totals (streamer_id, donator_name, donator_name_lower, "
                     "total_amount, donation_count) VALUES (%s, %s, lower(%s), %s, %s)", (sid, name, name, total, count))


def _actions(sid: str) -> list[dict]:
    with db() as conn:
        cur = conn.execute("SELECT donation_id::text, action_type, action_source, previous_status, new_status, notes, "
                           "moderator_id, moderator_telegram_id, moderator_name, actor_user_id::text "
                           "FROM public.moderation_actions WHERE streamer_id = %s ORDER BY created_at, id", (sid,))
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _bans(sid: str) -> list[tuple]:
    with db() as conn:
        return conn.execute("SELECT donor_name, banned_by_moderator_id, banned_by_name, reason, is_active "
                            "FROM public.banned_donors WHERE streamer_id = %s ORDER BY created_at", (sid,)).fetchall()


def _user_id(email: str) -> str:
    with db() as conn:
        return conn.execute("SELECT id::text FROM public.users WHERE email = %s", (email,)).fetchone()[0]


# ── the full matrix through the API (expectations written out independently of the code) ──

A, U = "applied", "unchanged"
#  (status, action): (result, new status, message visible after, totals delta, ban added)
EXPECTED = {
    ("pending", "approve"): (A, "approved", True, +1, False),
    ("pending", "reject"): (A, "rejected", False, 0, False),
    ("pending", "hide_message"): (A, "pending", False, 0, False),
    ("pending", "unhide_message"): (U, "pending", True, 0, False),
    ("pending", "ban_donor"): (A, "rejected", False, 0, True),
    ("approved", "approve"): (U, "approved", True, 0, False),
    ("approved", "reject"): (A, "rejected", False, -1, False),
    ("approved", "hide_message"): (A, "approved", False, 0, False),
    ("approved", "unhide_message"): (U, "approved", True, 0, False),
    ("approved", "ban_donor"): (A, "rejected", False, -1, True),
    ("auto_approved", "approve"): (U, "auto_approved", True, 0, False),
    ("auto_approved", "reject"): (A, "rejected", False, -1, False),
    ("auto_approved", "hide_message"): (A, "auto_approved", False, 0, False),
    ("auto_approved", "unhide_message"): (U, "auto_approved", True, 0, False),
    ("auto_approved", "ban_donor"): (A, "rejected", False, -1, True),
    ("rejected", "approve"): (A, "approved", True, +1, False),
    ("rejected", "reject"): (U, "rejected", True, 0, False),
    ("rejected", "hide_message"): (A, "rejected", False, 0, False),
    ("rejected", "unhide_message"): (U, "rejected", True, 0, False),
    ("rejected", "ban_donor"): (A, "rejected", True, 0, True),
}


@pytest.mark.parametrize("status,action", sorted(EXPECTED))
def test_state_machine_through_the_api(app, status, action):
    sid, headers = owned_creator(app, "authtest-mod")
    owner = _user_id("owner" + DOMAIN)
    did = make_donation(sid, 250, moderation=status, name="Fan", message="hello")
    _seed_totals(sid, "Fan", 1000, 5)  # what the donor's other (and this, if credited) donations add up to
    result, new_status, visible, delta, ban = EXPECTED[(status, action)]

    r = _moderate(app, "authtest-mod", did, action, headers)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store", r.text
    body = r.json()
    assert (body["result"], body["action"], body["previous_status"], body["new_status"]) == (
        result, action, status, new_status)
    assert set(body["donation"]) == QUEUE_FIELDS and body["donation"]["donation_id"] == did
    assert (body["donation"]["moderation_status"], body["donation"]["message_visible"]) == (new_status, visible)
    assert body["donation"]["is_donor_banned"] is ban

    row = _donation(did)
    assert (row["status"], row["visible"]) == (new_status, visible)
    assert _totals(sid) == (Decimal(1000 + delta * 250), 5 + delta)
    assert len(_bans(sid)) == (1 if ban else 0)
    actions = _actions(sid)
    if result == U:
        assert actions == []  # an unchanged action writes nothing, not even an audit row
    else:
        assert actions == [{"donation_id": did, "action_type": action, "action_source": "dashboard",
                            "previous_status": status, "new_status": new_status, "notes": None, "moderator_id": None,
                            "moderator_telegram_id": None, "moderator_name": "Dashboard", "actor_user_id": owner}]


def test_approve_stamps_the_session_user_and_clears_a_bot_approver(app):
    sid, headers = owned_creator(app, "authtest-approve")
    owner = _user_id("owner" + DOMAIN)
    with db() as conn:
        conn.execute("UPDATE public.users SET username = 'CreatorOne' WHERE id = %s", (owner,))
        mod = conn.execute("INSERT INTO public.streamers_moderators (streamer_id, mod_name, telegram_user_id) "
                           "VALUES (%s, 'Bot mod', '111') RETURNING id", (sid,)).fetchone()[0]
    did = make_donation(sid, 99, moderation="rejected", name="Fan")
    with db() as conn:
        conn.execute("UPDATE public.donations SET approved_by_moderator_id = %s WHERE id = %s", (mod, did))
    before = datetime.now(UTC) - timedelta(seconds=5)
    body = _moderate(app, "authtest-approve", did, "approve", headers).json()
    row = _donation(did)
    assert row["label"] == "CreatorOne" and row["moderator"] is None
    assert row["approved_at"] >= before and row["audio_scheduled_at"] == row["approved_at"]
    assert body["donation"]["approved_by_label"] == "CreatorOne" and body["donation"]["approved_at"]
    assert _actions(sid)[0]["moderator_name"] == "CreatorOne" and _actions(sid)[0]["actor_user_id"] == owner
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.moderation_actions WHERE moderator_name LIKE %s",
                            ("%" + DOMAIN,)).fetchone()[0] == 0  # never an email (A6)


def test_admin_actor_is_recorded_and_survives_user_deletion(app):
    sid = make_streamer("authtest-admin-mod", user_id=make_user("owner" + DOMAIN))
    did = make_donation(sid, 10, name="Fan")
    headers = signed_in(app, "boss" + DOMAIN, role="admin")
    admin = _user_id("boss" + DOMAIN)
    assert _moderate(app, sid, did, "approve", headers).json()["result"] == "applied"
    assert _actions(sid)[0]["actor_user_id"] == admin and _donation(did)["label"] == "Dashboard"
    with fixture_db() as conn:  # an admin row is not deletable by the runtime role (migration 0024)
        conn.execute("DELETE FROM public.users WHERE id = %s", (admin,))
    assert _actions(sid)[0]["actor_user_id"] is None and len(_actions(sid)) == 1  # ON DELETE SET NULL


def test_repeated_actions_are_unchanged_and_write_nothing(app):
    sid, headers = owned_creator(app, "authtest-repeat")
    approve_me = make_donation(sid, 100, name="Fan")
    ban_me = make_donation(sid, 50, name="Spammer")
    for action, did in (("approve", approve_me), ("hide_message", approve_me), ("ban_donor", ban_me)):
        assert _moderate(app, "authtest-repeat", did, action, headers).json()["result"] == "applied"
        snapshot = creator_state(sid)
        again = _moderate(app, "authtest-repeat", did, action, headers).json()
        assert again["result"] == "unchanged" and again["previous_status"] == again["new_status"]
        assert creator_state(sid) == snapshot
    assert _totals(sid) == (Decimal("100.00"), 1) and len(_bans(sid)) == 1 and len(_actions(sid)) == 3


@pytest.mark.parametrize("payment", ["pending", "failed"])
def test_unpaid_donations_are_409_for_every_action(app, payment):
    sid, headers = owned_creator(app, "authtest-unpaid")
    did = make_donation(sid, 100, payment=payment, moderation="pending", name="Fan")
    before = creator_state(sid)
    for action in ("approve", "reject", "hide_message", "unhide_message", "ban_donor"):
        r = _moderate(app, "authtest-unpaid", did, action, headers)
        assert (r.status_code, r.json()) == (409, {"error": "Donation is not paid"}), action
        assert r.headers["cache-control"] == "no-store"
    assert creator_state(sid) == before


def test_other_creators_and_unknown_donations_are_404(app):
    sid, headers = owned_creator(app, "authtest-mine")
    other = make_streamer("authtest-theirs", user_id=make_user("other" + DOMAIN))
    theirs = make_donation(other, 500, name="Fan")
    _seed_totals(other, "Fan", 500, 1)
    before = creator_state(other)
    for did in (theirs, "00000000-0000-4000-8000-000000000000", "not-a-uuid", "1"):
        for action in ("approve", "reject", "ban_donor", "hide_message"):
            r = _moderate(app, "authtest-mine", did, action, headers)
            assert (r.status_code, r.json()) == (404, {"error": "Donation not found"}), (did, action)
    assert creator_state(other) == before and _actions(sid) == [] and _bans(sid) == []
    assert _moderate(app, other, theirs, "approve", headers).status_code == 403  # their path: not my creator


def test_ban_reason_is_stored_exact_names_only(app):
    sid, headers = owned_creator(app, "authtest-ban")
    spam = make_donation(sid, 10, name="Spam_1", message="buy now")
    lookalike = make_donation(sid, 10, name="SpamX1")
    same_name = make_donation(sid, 10, name="SPAM_1")
    r = _moderate(app, "authtest-ban", spam, "ban_donor", headers, reason="  advertising  ")
    assert r.json()["result"] == "applied" and r.json()["donation"]["is_donor_banned"] is True
    assert _bans(sid) == [("Spam_1", None, "Dashboard", "advertising", True)]
    assert _actions(sid)[0]["notes"] == "advertising"
    queue = {d["donation_id"]: d["is_donor_banned"] for d in
             app.get(f"{S}/authtest-ban/moderation/queue", params={"status": "all"}).json()["donations"]}
    assert queue[same_name] is True and queue[lookalike] is False  # exact lower(name), no wildcards
    r = _moderate(app, "authtest-ban", same_name, "ban_donor", headers, reason="again")
    assert r.json()["result"] == "applied" and len(_bans(sid)) == 1  # rejected now, but no second ban
    _moderate(app, "authtest-ban", lookalike, "reject", headers, reason="ignored for reject")
    assert _actions(sid)[-1]["notes"] is None
    r = _moderate(app, "authtest-ban", spam, "ban_donor", headers, reason="x" * 501)
    assert r.status_code == 422


@pytest.mark.parametrize("action", ["unban_donor", "replay", "delete"])
def test_actions_outside_phase_5_are_rejected(app, action):
    sid, headers = owned_creator(app, "authtest-noaction")
    did = make_donation(sid, 10)
    r = _moderate(app, "authtest-noaction", did, action, headers)
    assert r.status_code == 422 and r.headers["cache-control"] == "no-store"


def test_totals_match_the_approved_donations_after_mixed_sequences(app):
    sid, headers = owned_creator(app, "authtest-totals")
    auto = make_donation(sid, 100, moderation="auto_approved", name="Fan")
    approved = make_donation(sid, 400, moderation="approved", name="fan")
    _seed_totals(sid, "Fan", 500, 2)  # credited when they were paid / approved
    p1 = make_donation(sid, 200, name="FAN")
    p2 = make_donation(sid, 300, name="Fan")
    for did, action in ((p1, "approve"), (auto, "reject"), (auto, "approve"), (approved, "ban_donor"), (p2, "approve"),
                        (p2, "reject"), (p2, "approve"), (p1, "hide_message"), (p1, "approve"), (auto, "approve")):
        assert _moderate(app, "authtest-totals", did, action, headers).status_code == 200
    with db() as conn:
        expected = conn.execute("SELECT sum(amount_inr), count(*) FROM public.donations WHERE streamer_id = %s "
                                "AND payment_status = 'success' AND moderation_status IN ('approved', 'auto_approved') "
                                "AND lower(donor_name) = 'fan'", (sid,)).fetchone()
    assert _totals(sid) == expected == (Decimal("600.00"), 3)


def test_debits_never_go_below_zero(app):
    sid, headers = owned_creator(app, "authtest-floor")
    did = make_donation(sid, 250, moderation="approved", name="Fan")
    _seed_totals(sid, "Fan", 100, 0)  # e.g. drifted legacy totals
    _moderate(app, "authtest-floor", did, "reject", headers)
    assert _totals(sid) == (Decimal("0.00"), 0)
    lonely = make_donation(sid, 30, moderation="approved", name="Nobody")  # no totals row at all
    assert _moderate(app, "authtest-floor", lonely, "reject", headers).json()["result"] == "applied"
    assert _totals(sid, "Nobody") is None


def test_a_failure_mid_transaction_rolls_everything_back(app):
    sid, headers = owned_creator(app, "authtest-rollback")
    did = make_donation(sid, 250, moderation="approved", name="Fan")
    _seed_totals(sid, "Fan", 250, 1)
    before = creator_state(sid)

    async def boom(*args, **kwargs):  # after the donation, totals and ban were written
        raise RuntimeError("injected failure")

    app.app_state.dashboard.repo._record_action = boom
    with pytest.raises(RuntimeError):
        _moderate(app, "authtest-rollback", did, "ban_donor", headers, reason="x")
    assert creator_state(sid) == before
    del app.app_state.dashboard.repo._record_action
    assert _moderate(app, "authtest-rollback", did, "ban_donor", headers).json()["result"] == "applied"
    assert creator_state(sid) != before


def _repo_with_pause(method: str) -> DashboardRepository:
    """A repository whose `method` waits 0.4 s inside the transaction, so concurrent calls overlap."""
    repo = DashboardRepository(connection_factory(Settings()))
    original = getattr(repo, method)

    async def slow(*args, **kwargs):
        await original(*args, **kwargs)
        await asyncio.sleep(0.4)

    setattr(repo, method, slow)
    return repo


def test_concurrent_approvals_credit_once(env):
    sid = make_streamer("authtest-race", user_id=make_user("owner" + DOMAIN))
    did = make_donation(sid, 100, name="Fan")
    repo = _repo_with_pause("_write_donation")

    async def race():
        return await asyncio.gather(*(repo.moderate(sid, did, "approve", actor_user_id=None, actor_label="Race",
                                                    notes=None) for _ in range(2)))

    results = asyncio.run(race())
    assert sorted(step.result for _, step, _ in results) == ["applied", "unchanged"]
    assert _totals(sid) == (Decimal("100.00"), 1) and len(_actions(sid)) == 1


def test_concurrent_bans_of_one_donor_add_one_ban(env):
    sid = make_streamer("authtest-race-ban", user_id=make_user("owner" + DOMAIN))
    first, second = make_donation(sid, 10, name="Spammer"), make_donation(sid, 10, name="SPAMMER")
    repo = _repo_with_pause("_insert_ban")

    async def race():
        return await asyncio.gather(*(repo.moderate(sid, did, "ban_donor", actor_user_id=None, actor_label="Race",
                                                    notes=None) for did in (first, second)))

    results = asyncio.run(race())
    assert [step.result for _, step, _ in results] == ["applied", "applied"]  # both donations rejected
    assert len(_bans(sid)) == 1 and {_donation(d)["status"] for d in (first, second)} == {"rejected"}


# ── D-R5 queue ──────────────────────────────────────────────────────────────────

def test_queue_filters_order_pending_count_and_fields(app):
    sid, _ = owned_creator(app, "authtest-queue")
    other = make_streamer("authtest-queue-other")
    t = datetime(2026, 10, 1, 12, tzinfo=UTC)
    p1 = make_donation(sid, 1, moderation="pending", at=t)
    a1 = make_donation(sid, 2, moderation="approved", at=t + timedelta(minutes=1))
    p2 = make_donation(sid, 3, moderation="pending", at=t + timedelta(minutes=2), visible=False, message="hidden")
    aa = make_donation(sid, 4, moderation="auto_approved", at=t + timedelta(minutes=3))
    r1 = make_donation(sid, 5, moderation="rejected", at=t + timedelta(minutes=4))
    make_donation(sid, 6, payment="pending", moderation="pending", at=t + timedelta(minutes=5))
    make_donation(other, 7, moderation="pending", at=t + timedelta(minutes=6))

    def ids(**params):
        r = app.get(f"{S}/authtest-queue/moderation/queue", params=params)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        assert r.json()["pending_count"] == 2
        return [d["donation_id"] for d in r.json()["donations"]]

    assert ids() == [p2, p1]                                  # default: pending
    assert ids(status="approved") == [aa, a1]                 # approved + auto_approved
    assert ids(status="rejected") == [r1]
    assert ids(status="all") == [r1, aa, p2, a1, p1]          # paid only
    body = app.get(f"{S}/authtest-queue/moderation/queue").json()
    assert set(body) == {"donations", "pending_count", "next_cursor", "has_more"}
    assert all(set(d) == QUEUE_FIELDS for d in body["donations"])
    assert body["donations"][0]["message"] == "hidden" and body["donations"][0]["message_visible"] is False

    first = app.get(f"{S}/authtest-queue/moderation/queue", params={"status": "all", "limit": 2}).json()
    assert [d["donation_id"] for d in first["donations"]] == [r1, aa] and first["has_more"] is True
    rest = app.get(f"{S}/authtest-queue/moderation/queue",
                   params={"status": "all", "limit": 2, "cursor": first["next_cursor"]}).json()
    assert [d["donation_id"] for d in rest["donations"]] == [p2, a1]


@pytest.mark.parametrize("params,status", [({"status": "auto_approved"}, 422), ({"status": "bogus"}, 422),
                                           ({"limit": 0}, 422), ({"limit": 101}, 422), ({"cursor": "x"}, 400)])
def test_queue_parameter_validation(app, params, status):
    owned_creator(app, "authtest-queue-bad")
    r = app.get(f"{S}/authtest-queue-bad/moderation/queue", params=params)
    assert r.status_code == status and r.headers["cache-control"] == "no-store"


def test_queue_ban_flags_ignore_inactive_and_other_creators_bans(app):
    sid, _ = owned_creator(app, "authtest-flags")
    other = make_streamer("authtest-flags-other")
    banned, old, elsewhere = (make_donation(sid, 1, name=n) for n in ("Troll", "Old", "Elsewhere"))
    with db() as conn:
        conn.execute("INSERT INTO public.banned_donors (streamer_id, donor_name, is_active) VALUES "
                     "(%s, 'TROLL', true), (%s, 'old', false), (%s, 'Elsewhere', true)", (sid, sid, other))
    flags = {d["donation_id"]: d["is_donor_banned"] for d in
             app.get(f"{S}/authtest-flags/moderation/queue").json()["donations"]}
    assert flags == {banned: True, old: False, elsewhere: False}


# ── D-R8 ban list and D-M6 unban by id ──────────────────────────────────────────

BAN_FIELDS = {"ban_id", "donor_name", "banned_by_name", "reason", "is_active", "created_at", "unbanned_at",
              "unbanned_by_label"}


def _insert_ban(sid: str, name: str, *, active: bool = True, at: str = "now()") -> str:
    with db() as conn:
        return conn.execute(f"INSERT INTO public.banned_donors (streamer_id, donor_name, banned_by_name, reason, "
                            f"is_active, created_at) VALUES (%s, %s, 'Old Mod', 'spam', %s, {at}) RETURNING id::text",
                            (sid, name, active)).fetchone()[0]


def _ban_rows(sid: str) -> list[tuple]:
    with db() as conn:
        return conn.execute("SELECT donor_name, is_active, unbanned_at IS NOT NULL, unbanned_by_label, "
                            "unbanned_by_user_id::text FROM public.banned_donors WHERE streamer_id = %s "
                            "ORDER BY created_at, id", (sid,)).fetchall()


def _unban(client, ref, ban_id, headers):
    return client.post(f"{S}/{ref}/moderation/banned-donors/{ban_id}/unban", headers=headers)


def test_banned_list_is_this_creators_active_bans_newest_first(app):
    sid, headers = owned_creator(app, "authtest-banlist")
    other = make_streamer("authtest-banlist-other", user_id=make_user("other" + DOMAIN))
    _insert_ban(sid, "Older", at="now() - interval '2 days'")
    _insert_ban(sid, "Lifted", active=False)
    _insert_ban(other, "Elsewhere")
    spam = make_donation(sid, 10, name="Spammer")
    assert _moderate(app, "authtest-banlist", spam, "ban_donor", headers, reason="ads").json()["result"] == "applied"
    r = app.get(f"{S}/authtest-banlist/moderation/banned-donors")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert (body["total"], body["truncated"]) == (2, False)
    assert [(b["donor_name"], b["reason"], b["banned_by_name"], b["is_active"]) for b in body["banned_donors"]] == [
        ("Spammer", "ads", "Dashboard", True), ("Older", "spam", "Old Mod", True)]
    assert all(set(b) == BAN_FIELDS for b in body["banned_donors"])


def test_unban_lifts_every_active_ban_of_the_name_records_who_and_is_idempotent(app):
    sid, headers = owned_creator(app, "authtest-unban")
    owner_id = _user_id("owner" + DOMAIN)
    first = _insert_ban(sid, "Troll", at="now() - interval '1 day'")
    _insert_ban(sid, "TROLL")          # a legacy duplicate of the same donor (case-insensitive)
    _insert_ban(sid, "Someone Else")   # untouched
    did = make_donation(sid, 10, name="troll")
    queue = lambda: {d["donation_id"]: d["is_donor_banned"] for d in  # noqa: E731
                     app.get(f"{S}/authtest-unban/moderation/queue", params={"status": "all"}).json()["donations"]}
    assert queue()[did] is True
    r = _unban(app, "authtest-unban", first, headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["result"], body["bans_lifted"]) == ("applied", 2)
    assert (body["ban"]["ban_id"], body["ban"]["is_active"], body["ban"]["unbanned_by_label"]) == (first, False,
                                                                                                    "Dashboard")
    assert body["ban"]["unbanned_at"] is not None
    assert _ban_rows(sid) == [("Troll", False, True, "Dashboard", owner_id), ("TROLL", False, True, "Dashboard", owner_id),
                              ("Someone Else", True, False, None, None)]
    assert queue()[did] is False
    again = _unban(app, "authtest-unban", first, headers).json()  # already lifted: nothing changes
    assert (again["result"], again["bans_lifted"]) == ("unchanged", 0)
    # the donor can be banned again afterwards (a new, active ban row)
    assert _moderate(app, "authtest-unban", did, "ban_donor", headers).json()["result"] == "applied"
    listed = app.get(f"{S}/authtest-unban/moderation/banned-donors").json()["banned_donors"]
    assert [b["donor_name"] for b in listed] == ["troll", "Someone Else"]
    # a STALE unban (the old, already lifted id: a retry or an old page) must not lift the newer ban
    stale = _unban(app, "authtest-unban", first, headers).json()
    assert (stale["result"], stale["bans_lifted"], stale["ban"]["ban_id"]) == ("unchanged", 0, first)
    assert [b["donor_name"] for b in app.get(f"{S}/authtest-unban/moderation/banned-donors").json()["banned_donors"]] \
        == ["troll", "Someone Else"] and queue()[did] is True
    newer = listed[0]["ban_id"]  # lifting the newer ban by its own id works
    assert _unban(app, "authtest-unban", newer, headers).json()["bans_lifted"] == 1 and queue()[did] is False


def test_unban_is_scoped_to_the_creator_and_needs_a_session_and_csrf(app):
    sid, headers = owned_creator(app, "authtest-unban-mine")
    other = make_streamer("authtest-unban-theirs", user_id=make_user("other" + DOMAIN))
    theirs = _insert_ban(other, "Theirs")
    mine = _insert_ban(sid, "Mine")
    for ban_id in (theirs, "00000000-0000-4000-8000-000000000000", "not-a-uuid"):
        r = _unban(app, "authtest-unban-mine", ban_id, headers)
        assert (r.status_code, r.json()) == (404, {"error": "Ban not found"}), ban_id
    assert _unban(app, "authtest-unban-theirs", theirs, headers).status_code == 403  # their creator: not mine
    assert app.get(f"{S}/authtest-unban-theirs/moderation/banned-donors").status_code == 403
    assert app.post(f"{S}/authtest-unban-mine/moderation/banned-donors/{mine}/unban",
                    headers={"origin": headers["origin"]}).status_code == 403  # no CSRF token
    app.cookies.clear()
    assert app.get(f"{S}/authtest-unban-mine/moderation/banned-donors").status_code == 401
    assert _unban(app, "authtest-unban-mine", mine, headers).status_code == 401
    assert _ban_rows(other) == [("Theirs", True, False, None, None)] and _ban_rows(sid)[0][1] is True


def test_concurrent_unbans_lift_once_and_a_racing_ban_is_never_lost(env):
    sid = make_streamer("authtest-race-unban", user_id=make_user("owner" + DOMAIN))
    ban = _insert_ban(sid, "Spammer")
    repo = DashboardRepository(connection_factory(Settings()))

    async def unbans():
        return await asyncio.gather(*(repo.unban(sid, ban, actor_user_id=None, actor_label="Race") for _ in range(4)))
    assert sorted(lifted for lifted, _ in asyncio.run(unbans())) == [0, 0, 0, 1]

    # unbans of DIFFERENT ban ids of one donor name (legacy duplicates) at once: no deadlock, both lifted exactly once
    dup_a, dup_b = _insert_ban(sid, "Dupe"), _insert_ban(sid, "DUPE")

    async def dupes():
        return await asyncio.gather(*(repo.unban(sid, b, actor_user_id=None, actor_label="Race")
                                      for b in (dup_a, dup_b, dup_a, dup_b)))
    assert sorted(lifted for lifted, _ in asyncio.run(dupes())) == [0, 0, 0, 2]
    assert [r[1] for r in _ban_rows(sid) if r[0].lower() == "dupe"] == [False, False]

    # ban (on a donation) racing an unban of the same donor: they serialise on the per-donor lock, so the outcome
    # is one of the two serial orders, never a lost or duplicated ban.
    again = _insert_ban(sid, "Spammer")
    did = make_donation(sid, 10, name="spammer")

    async def race():
        return await asyncio.gather(
            repo.moderate(sid, did, "ban_donor", actor_user_id=None, actor_label="Race", notes=None),
            repo.unban(sid, again, actor_user_id=None, actor_label="Race"))
    asyncio.run(race())
    active = [r for r in _ban_rows(sid) if r[1]]
    assert len(active) <= 1 and _donation(did)["status"] == "rejected"
