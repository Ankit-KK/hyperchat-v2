"""Pusher channel names (kept from legacy, D3).

  private-{slug}-dashboard     creator dashboard (session: owner / linked account / admin)
  private-{slug}-alerts        OBS alerts overlay (OBS token)        audio-now-playing, vaani-celebration (Phase 10)
  private-{slug}-audio         OBS audio player (OBS token)
  private-{slug}-settings      OBS overlays (OBS token)
  private-{slug}-leaderboard   OBS leaderboard widget (OBS token)
  {slug}-goal                  public goal bar: aggregates only

No presence or encrypted channels. Names are built from the creator's CURRENT slug when an event is
published (the outbox stores the creator id), and parsed with the suffix anchored at the end, so a slug that
itself contains "-alerts" can never be confused with another creator's channel.
"""

import re

PRIVATE_SUFFIXES = ("dashboard", "alerts", "audio", "settings", "leaderboard")
OUTBOX_CHANNELS = ("dashboard", "alerts", "audio", "settings", "leaderboard", "goal")
MAX_CHANNEL_LENGTH = 164  # Pusher's limit
_PRIVATE = re.compile(r"private-(?P<slug>[a-z0-9][a-z0-9_-]{0,63})-(?P<suffix>" + "|".join(PRIVATE_SUFFIXES) + r")")
_SOCKET_ID = re.compile(r"[0-9]{1,20}\.[0-9]{1,20}")


def channel_name(slug: str, channel: str) -> str:
    if channel == "goal":
        return f"{slug}-goal"
    if channel not in PRIVATE_SUFFIXES:
        raise ValueError(f"unknown channel {channel!r}")
    return f"private-{slug}-{channel}"


def parse_private(name: str) -> tuple[str, str] | None:
    """(slug, suffix) of a private channel we serve, else None (presence-, private-encrypted-, public,
    unknown suffixes and malformed names)."""
    if not isinstance(name, str) or len(name) > MAX_CHANNEL_LENGTH or name.startswith("private-encrypted-"):
        return None
    match = _PRIVATE.fullmatch(name)  # fullmatch: "$" alone would accept a trailing newline
    return (match["slug"], match["suffix"]) if match else None


def valid_socket_id(value: str) -> bool:
    return isinstance(value, str) and bool(_SOCKET_ID.fullmatch(value))
