"""Pure access decisions (no I/O), so every rule is unit-testable."""

import re
import uuid
from dataclasses import dataclass
from typing import Literal

from app.auth.repository import User

AccessVia = Literal["admin", "owner", "linked"]
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class StreamerRef:
    id: str
    slug: str
    name: str
    user_id: str | None


@dataclass(frozen=True)
class StreamerRefQuery:
    """A path parameter that is either a streamer id (UUID) or a slug."""
    streamer_id: str | None = None
    slug: str | None = None


def parse_streamer_ref(value: str) -> StreamerRefQuery | None:
    value = (value or "").strip()
    try:
        return StreamerRefQuery(streamer_id=str(uuid.UUID(value)))
    except ValueError:
        pass
    slug = value.lower()
    return StreamerRefQuery(slug=slug) if _SLUG.match(slug) else None


def access_via(user: User, streamer: StreamerRef) -> AccessVia | None:
    """How `user` may act on `streamer`, or None when access is denied."""
    if not user.is_active:
        return None
    if user.role == "admin":
        return "admin"
    if streamer.user_id is None:
        return None  # unclaimed creator: admin-only
    if streamer.user_id == user.id:
        return "owner"
    if user.streamer_id is not None and user.streamer_id == streamer.id:
        return "linked"
    return None


def denial_reason(user: User, streamer: StreamerRef) -> str:
    if not user.is_active:
        return "inactive_user"
    return "unclaimed_streamer" if streamer.user_id is None else "not_owner"
