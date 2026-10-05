"""OBS overlay tokens and Pusher channel scopes.

OBS browser sources authenticate with a long random token (x-obs-token header, ?token= or
body.obs_token). Only its SHA-256 is stored (obs_tokens.token_hash). OBS browser sources (CEF)
send no Origin header, so "no Origin" is accepted; an Origin that is present must be the frontend.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from app.security.crypto import sha256_hex

OBS_ORIGIN_HOSTS = frozenset({"hyperchat.space", "www.hyperchat.space"})
DEFAULT_CHANNELS = ("alerts", "settings", "audio", "leaderboard", "goal")


@dataclass(frozen=True)
class ObsTokenInfo:
    streamer_id: str
    streamer_slug: str
    streamer_name: str | None


class ObsTokenStore(Protocol):
    async def validate(self, token_hash: str) -> ObsTokenInfo | None:
        """Active, unrevoked, unexpired token -> its streamer."""

    async def channel_scope(self, token_hash: str, streamer_id: str) -> list[str] | None:
        """Explicit channel scope of the token, or None for the default scope."""


@dataclass(frozen=True)
class ObsAuth:
    ok: bool
    status: int
    token_info: ObsTokenInfo | None = None
    error: str | None = None


def get_obs_token(headers: Mapping[str, str], query: Mapping[str, str], body: Mapping | None = None) -> str:
    """Precedence: x-obs-token header, ?token=, body.obs_token."""
    return headers.get("x-obs-token") or query.get("token") or (body or {}).get("obs_token") or ""


def is_obs_origin_allowed(origin: str | None) -> bool:
    if not origin:
        return True
    try:
        parsed = urlparse(origin)
        port = parsed.port
    except ValueError:
        return False
    return parsed.scheme == "https" and port is None and (parsed.hostname or "") in OBS_ORIGIN_HOSTS


def default_scope_for_obs(slug: str) -> list[str]:
    return [f"private-{slug}-{suffix}" for suffix in DEFAULT_CHANNELS]


async def validate_obs_token(store: ObsTokenStore, token: str | None) -> ObsAuth:
    if not token:
        return ObsAuth(False, 401, error="missing_token")
    info = await store.validate(sha256_hex(token))
    if info is None:
        return ObsAuth(False, 401, error="invalid_token")
    return ObsAuth(True, 200, token_info=info)


async def obs_channel_allowed(store: ObsTokenStore, token: str, info: ObsTokenInfo, channel: str) -> bool:
    scope = await store.channel_scope(sha256_hex(token), info.streamer_id) or default_scope_for_obs(info.streamer_slug)
    return channel in scope
