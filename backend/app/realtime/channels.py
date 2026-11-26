"""Pusher channel names.

  private-{slug}-dashboard              creator dashboard (session: owner / linked account / admin)
  private-{slug}-alerts-t{tag}          OBS alerts overlay (OBS token)   audio-now-playing, vaani-celebration (Phase 10)
  private-{slug}-audio-t{tag}           OBS audio player (OBS token)
  private-{slug}-settings-t{tag}        OBS overlays (OBS token)
  private-{slug}-leaderboard-t{tag}     OBS leaderboard widget (OBS token)
  {slug}-goal                           public goal bar: aggregates only

Overlay channels are per OBS token (security audit NV-4, 2026-11-26): {tag} is token_tag(the token's SHA-256), so
every token has channels of its own. Pusher signs a subscription once and can never drop it, so revoking a token
works by no longer publishing to its channels: the publisher sends each overlay event to the tags of the creator's
ACTIVE tokens only, and an overlay that is still connected with a revoked token receives nothing more. A client
learns its names from GET /api/v1/obs/session; the untagged legacy names (D3) are refused for OBS tokens and never
published. (Legacy overlays never used these names: they talk to the legacy Supabase functions.)

No presence or encrypted channels. Names are built from the creator's CURRENT slug when an event is
published (the outbox stores the creator id), and parsed with the suffix anchored at the end, so a slug that
itself contains "-alerts" can never be confused with another creator's channel.
"""

import hashlib
import re

PRIVATE_SUFFIXES = ("dashboard", "alerts", "audio", "settings", "leaderboard")
OBS_SUFFIXES = ("alerts", "audio", "settings", "leaderboard")
OUTBOX_CHANNELS = ("dashboard", "alerts", "audio", "settings", "leaderboard", "goal")
MAX_CHANNEL_LENGTH = 164  # Pusher's limit
_PRIVATE = re.compile(r"private-(?P<slug>[a-z0-9][a-z0-9_-]{0,63})-(?P<suffix>" + "|".join(PRIVATE_SUFFIXES)
                      + r")(?:-t(?P<tag>[0-9a-f]{16}))?")
_SOCKET_ID = re.compile(r"[0-9]{1,20}\.[0-9]{1,20}")
_TAG_DOMAIN = b"hyperchat-obs-channel-v1\n"


def channel_name(slug: str, channel: str) -> str:
    """The untagged name: the dashboard and goal channels as published, and the base an OBS scope is checked on."""
    if channel == "goal":
        return f"{slug}-goal"
    if channel not in PRIVATE_SUFFIXES:
        raise ValueError(f"unknown channel {channel!r}")
    return f"private-{slug}-{channel}"


def token_tag(token_hash: str) -> str:
    """16 hex characters naming one OBS token's channels; derived from its stored SHA-256, never from the token."""
    return hashlib.sha256(_TAG_DOMAIN + token_hash.encode()).hexdigest()[:16]


def obs_channel_name(slug: str, suffix: str, tag: str) -> str:
    if suffix not in OBS_SUFFIXES:
        raise ValueError(f"not an overlay channel {suffix!r}")
    return f"private-{slug}-{suffix}-t{tag}"


def parse_private(name: str) -> tuple[str, str, str | None] | None:
    """(slug, suffix, tag or None) of a private channel we serve, else None (presence-, private-encrypted-, public,
    unknown suffixes and malformed names). Whether a tag is required or refused is the authorizer's rule."""
    if not isinstance(name, str) or len(name) > MAX_CHANNEL_LENGTH or name.startswith("private-encrypted-"):
        return None
    match = _PRIVATE.fullmatch(name)  # fullmatch: "$" alone would accept a trailing newline
    return (match["slug"], match["suffix"], match["tag"]) if match else None


def valid_socket_id(value: str) -> bool:
    return isinstance(value, str) and bool(_SOCKET_ID.fullmatch(value))
