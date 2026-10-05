"""Unit matrix for the creator access rule (no database)."""

import pytest

from app.auth.repository import User
from app.authz.access import StreamerRef, access_via, denial_reason, parse_streamer_ref

STREAMER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_STREAMER_ID = "22222222-2222-4222-8222-222222222222"
OWNER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_USER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def user(uid=OWNER_ID, role="streamer", streamer_id=None, active=True) -> User:
    return User(id=uid, email="u@example.invalid", username=None, role=role, streamer_id=streamer_id,
                is_active=active, password_hash=None, failed_login_attempts=0, locked_until=None)


CLAIMED = StreamerRef(STREAMER_ID, "creator-a", "Creator A", OWNER_ID)
UNCLAIMED = StreamerRef(STREAMER_ID, "creator-a", "Creator A", None)


@pytest.mark.parametrize("who, streamer, expected", [
    (user(role="admin", uid=OTHER_USER_ID), CLAIMED, "admin"),
    (user(role="admin", uid=OTHER_USER_ID), UNCLAIMED, "admin"),
    (user(), CLAIMED, "owner"),
    (user(uid=OTHER_USER_ID, streamer_id=STREAMER_ID), CLAIMED, "linked"),
    (user(uid=OWNER_ID, streamer_id=OTHER_STREAMER_ID), CLAIMED, "owner"),
    (user(uid=OTHER_USER_ID), CLAIMED, None),
    (user(uid=OTHER_USER_ID, streamer_id=OTHER_STREAMER_ID), CLAIMED, None),
    (user(uid=OTHER_USER_ID, streamer_id=STREAMER_ID), UNCLAIMED, None),  # unclaimed: admin-only
    (user(uid=OTHER_USER_ID, role="user"), CLAIMED, None),
    (user(active=False), CLAIMED, None),
    (user(role="admin", active=False), CLAIMED, None),
])
def test_access_matrix(who, streamer, expected):
    assert access_via(who, streamer) == expected


def test_denial_reasons():
    assert denial_reason(user(uid=OTHER_USER_ID), UNCLAIMED) == "unclaimed_streamer"
    assert denial_reason(user(uid=OTHER_USER_ID), CLAIMED) == "not_owner"
    assert denial_reason(user(active=False), CLAIMED) == "inactive_user"


@pytest.mark.parametrize("value, expected", [
    (STREAMER_ID, (STREAMER_ID, None)),
    (STREAMER_ID.upper(), (STREAMER_ID, None)),
    ("Creator-A", (None, "creator-a")),
    (" creator_a ", (None, "creator_a")),
    ("", None), ("../etc", None), ("a b", None), ("x" * 65, None), ("-leading", None), ("ünïcode", None),
])
def test_parse_streamer_ref(value, expected):
    ref = parse_streamer_ref(value)
    assert (None if ref is None else (ref.streamer_id, ref.slug)) == expected
