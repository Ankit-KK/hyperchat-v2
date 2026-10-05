"""G4 moderators: list (D-R6), add (D-M3), remove (D-M4) against a scratch database."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.cli import connection_factory
from app.core.config import Settings
from app.dashboard.repository import DashboardRepository, DuplicateModerator
from tests.integration import DOMAIN, creator_state, db, make_streamer, make_user, owned_creator, requires_db

pytestmark = requires_db
S = "/api/v1/dashboard/streamers"
FIELDS = {"moderator_id", "mod_name", "telegram_user_id", "discord_user_id", "role", "can_approve", "can_reject",
          "can_hide_message", "can_ban", "can_replay", "is_active", "created_at", "last_action_at"}


def _insert(sid: str, name: str, telegram=None, discord=None, active=True, at=None) -> str:
    """A moderator row written directly, as legacy data may look (no validation)."""
    with db() as conn:
        return conn.execute("INSERT INTO public.streamers_moderators (streamer_id, mod_name, telegram_user_id, "
                            "discord_user_id, is_active, created_at) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id::text",
                            (sid, name, telegram, discord, active, at or datetime.now(UTC))).fetchone()[0]


def _rows(sid: str) -> list[tuple]:
    with db() as conn:
        return conn.execute("SELECT mod_name, telegram_user_id, discord_user_id, is_active FROM "
                            "public.streamers_moderators WHERE streamer_id = %s ORDER BY created_at", (sid,)).fetchall()


def test_list_includes_removed_moderators_newest_first(app):
    sid, _ = owned_creator(app, "authtest-mods")
    other = make_streamer("authtest-mods-other")
    t = datetime(2026, 9, 1, tzinfo=UTC)
    old = _insert(sid, "Old", telegram="111", active=False, at=t)
    new = _insert(sid, "New", discord="user#0001", at=t + timedelta(days=1))   # legacy non-numeric id: served as is
    _insert(other, "Not mine", telegram="222")
    with db() as conn:
        conn.execute("UPDATE public.streamers_moderators SET role = NULL, can_ban = NULL WHERE id = %s", (new,))
    r = app.get(f"{S}/authtest-mods/moderators")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    mods = r.json()["moderators"]
    assert set(r.json()) == {"moderators"} and [m["moderator_id"] for m in mods] == [new, old]
    assert all(set(m) == FIELDS for m in mods)
    assert {k: mods[0][k] for k in FIELDS - {"created_at"}} == {
        "moderator_id": new, "mod_name": "New", "telegram_user_id": None, "discord_user_id": "user#0001",
        "role": "moderator", "can_approve": True, "can_reject": True, "can_hide_message": True, "can_ban": False,
        "can_replay": True, "is_active": True, "last_action_at": None}
    assert mods[1]["is_active"] is False
    assert app.get(f"{S}/authtest-mods-other/moderators").status_code == 403


def test_add_with_telegram_discord_or_both(app):
    sid, headers = owned_creator(app, "authtest-add")
    url = f"{S}/authtest-add/moderators"
    for body, expected in (
            ({"mod_name": "  Tele  ", "telegram_user_id": " 123456789 "}, ("Tele", "123456789", None)),
            ({"mod_name": "Disc", "discord_user_id": "123456789012345678", "telegram_user_id": ""},
             ("Disc", None, "123456789012345678")),
            ({"mod_name": "Both", "telegram_user_id": "@legacy_handle", "discord_user_id": "name#1234"},
             ("Both", "@legacy_handle", "name#1234"))):
        r = app.post(url, json=body, headers=headers)
        assert r.status_code == 201 and r.headers["cache-control"] == "no-store", r.text
        m = r.json()
        assert set(m) == FIELDS and (m["mod_name"], m["telegram_user_id"], m["discord_user_id"]) == expected
        assert (m["role"], m["is_active"], m["can_ban"], m["can_approve"]) == ("moderator", True, False, True)
    assert _rows(sid) == [("Tele", "123456789", None, True), ("Disc", None, "123456789012345678", True),
                          ("Both", "@legacy_handle", "name#1234", True)]
    assert len(app.get(url).json()["moderators"]) == 3


@pytest.mark.parametrize("body", [
    {"mod_name": "No ids"}, {"mod_name": "Blank ids", "telegram_user_id": " ", "discord_user_id": ""},
    {"mod_name": "", "telegram_user_id": "1"}, {"mod_name": "x" * 101, "telegram_user_id": "1"},
    {"mod_name": "Long", "telegram_user_id": "1" * 65}, {"mod_name": "Num", "telegram_user_id": 123},
    {"mod_name": "Perm", "telegram_user_id": "1", "can_ban": True}, {"mod_name": "Role", "telegram_user_id": "1",
                                                                     "role": "owner"},
])
def test_add_rejects_invalid_input(app, body):
    sid, headers = owned_creator(app, "authtest-add-bad")
    r = app.post(f"{S}/authtest-add-bad/moderators", json=body, headers=headers)
    assert r.status_code == 422 and r.headers["cache-control"] == "no-store"
    assert _rows(sid) == []


def test_duplicate_active_telegram_and_discord_ids_are_409(app):
    sid, headers = owned_creator(app, "authtest-dup")
    other = make_streamer("authtest-dup-other")
    _insert(other, "Elsewhere", telegram="555", discord="777")  # another creator may use the same ids
    url = f"{S}/authtest-dup/moderators"
    first = app.post(url, json={"mod_name": "A", "telegram_user_id": "555", "discord_user_id": "777"}, headers=headers)
    assert first.status_code == 201
    for body, platform in (({"mod_name": "B", "telegram_user_id": " 555 "}, "Telegram"),
                           ({"mod_name": "C", "discord_user_id": "777"}, "Discord"),
                           ({"mod_name": "D", "telegram_user_id": "556", "discord_user_id": "777"}, "Discord")):
        r = app.post(url, json=body, headers=headers)
        assert (r.status_code, r.json()) == (409, {"error": f"An active moderator with this {platform} ID already exists"})
        assert r.headers["cache-control"] == "no-store"
    assert len(_rows(sid)) == 1
    removed = app.delete(f"{url}/{first.json()['moderator_id']}", headers=headers)
    assert removed.json()["result"] == "removed"
    again = app.post(url, json={"mod_name": "A again", "telegram_user_id": "555", "discord_user_id": "777"},
                     headers=headers)
    assert again.status_code == 201  # only ACTIVE moderators count as duplicates


def test_existing_duplicate_and_nonconforming_rows_stay_untouched(app):
    sid, headers = owned_creator(app, "authtest-legacy-mods")
    a = _insert(sid, "Legacy 1", discord="12345678901234567890")
    _insert(sid, "Legacy 2", discord="12345678901234567890")   # the duplicate that exists in dev data
    _insert(sid, "Legacy 3", telegram="not-a-number")
    before = _rows(sid)
    mods = app.get(f"{S}/authtest-legacy-mods/moderators").json()["moderators"]
    assert {m["mod_name"] for m in mods} == {"Legacy 1", "Legacy 2", "Legacy 3"}
    r = app.post(f"{S}/authtest-legacy-mods/moderators", json={"mod_name": "New", "discord_user_id": "12345678901234567890"},
                 headers=headers)
    assert r.status_code == 409 and _rows(sid) == before
    assert app.delete(f"{S}/authtest-legacy-mods/moderators/{a}", headers=headers).json()["result"] == "removed"


def test_remove_is_soft_idempotent_and_scoped(app):
    sid, headers = owned_creator(app, "authtest-remove")
    other = make_streamer("authtest-remove-other", user_id=make_user("other" + DOMAIN))
    mine = _insert(sid, "Mine", telegram="1")
    theirs = _insert(other, "Theirs", telegram="2")
    url = f"{S}/authtest-remove/moderators"
    r = app.delete(f"{url}/{mine}", headers=headers)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["result"] == "removed" and r.json()["moderator"]["is_active"] is False
    assert set(r.json()) == {"result", "moderator"} and set(r.json()["moderator"]) == FIELDS
    again = app.delete(f"{url}/{mine}", headers=headers)
    assert again.json()["result"] == "unchanged" and again.json()["moderator"]["is_active"] is False
    assert _rows(sid) == [("Mine", "1", None, False)]  # soft removal: the row stays
    before = creator_state(other)
    for bad in (theirs, "00000000-0000-4000-8000-000000000000", "not-a-uuid"):
        resp = app.delete(f"{url}/{bad}", headers=headers)
        assert (resp.status_code, resp.json()) == (404, {"error": "Moderator not found"}), bad
    assert creator_state(other) == before


def test_changes_are_audited(app):
    sid, headers = owned_creator(app, "authtest-mod-audit")
    added = app.post(f"{S}/authtest-mod-audit/moderators", json={"mod_name": "M", "telegram_user_id": "42"},
                     headers=headers).json()
    app.delete(f"{S}/authtest-mod-audit/moderators/{added['moderator_id']}", headers=headers)
    records = app.app_state.log_capture.records
    assert any("moderator_added" in r and sid in r for r in records)
    assert any("moderator_removed" in r and sid in r for r in records)


def test_concurrent_adds_of_one_discord_id_insert_once(env):
    sid = make_streamer("authtest-mod-race")
    repo = DashboardRepository(connection_factory(Settings()))
    original = repo._insert_moderator

    async def slow_insert(*args, **kwargs):  # hold the transaction open after the insert
        row = await original(*args, **kwargs)
        await asyncio.sleep(0.4)
        return row

    repo._insert_moderator = slow_insert

    async def add(name):
        try:
            return await repo.add_moderator(sid, name, None, "999999999999999999")
        except DuplicateModerator as exc:
            return exc.kind

    async def race():
        return await asyncio.gather(add("One"), add("Two"))

    results = asyncio.run(race())
    assert sum(isinstance(r, dict) for r in results) == 1 and "discord" in results
    assert len(_rows(sid)) == 1
