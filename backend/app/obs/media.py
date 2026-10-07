"""What a claimed donation plays (P10-D5, P10-D6).

Legacy order: the hypersound, else the voice message, else the generated TTS. The chosen URL is served (302 or
JSON) only when it is EXACTLY an object of this environment's public bucket under a known key pattern:

  hypersound  <base>/hypersounds/<one path segment>.mp3                (the platform library)
  voice       <base>/voice-messages/<this creator's slug>_<ms>_<hex>.webm   (Phase 6 upload of this creator)
  tts         <base>/tts-audio/<this creator's id>/<this donation's id>.mp3 (Phase 8), or <base>/system/silence.mp3

Anything else (legacy hosts, Supabase storage, another creator's object, query strings, fragments, encoded or
relative paths) is never served: the claim marks the item played and moves on. This also removes legacy's open
redirect (get-current-audio redirected to whatever URL was stored).

A hidden message (message_visible = false) is never spoken: generated TTS is replaced by the silence object.
Voice messages and hypersounds are the donor's own audio and play as they are.
"""

import re

from app.storage.r2 import SILENCE_KEY

_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
# One path segment: unreserved/sub-delim characters or percent-escapes, never an encoded "/", "\" or ".".
_HYPERSOUND = re.compile(r"hypersounds/(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%(?![2][fFeE]|5[cC])[0-9A-Fa-f]{2}){1,200}\.mp3")


def _key(url: str | None, base: str) -> str | None:
    prefix = base.rstrip("/") + "/"
    if not isinstance(url, str) or not url.startswith(prefix) or any(c in url for c in "?#\\") or ".." in url:
        return None
    return url[len(prefix):]


def _valid(kind: str, key: str | None, d: dict, slug: str) -> bool:
    if key is None:
        return False
    if kind == "hypersound":
        return bool(_HYPERSOUND.fullmatch(key))
    if kind == "voice":
        return bool(_SLUG.fullmatch(slug)) and bool(
            re.fullmatch(rf"voice-messages/{re.escape(slug)}_\d{{13}}_[0-9a-f]{{32}}\.webm", key))
    return key == SILENCE_KEY or key == f"tts-audio/{d['streamer_id']}/{d['id']}.mp3"


def choose(d: dict, base: str | None, slug: str) -> tuple[str, str] | None:
    """(kind, URL) to play, or None when the item must not be served. `d` needs id, streamer_id, hypersound_url,
    voice_message_url, tts_audio_url and message_visible."""
    if not base:
        return None
    if d.get("hypersound_url"):
        kind, url = "hypersound", d["hypersound_url"]
    elif d.get("voice_message_url"):
        kind, url = "voice", d["voice_message_url"]
    elif d.get("tts_audio_url"):
        kind, url = "tts", d["tts_audio_url"]
    else:
        return None
    key = _key(url, base)
    if not _valid(kind, key, d, slug):
        return None
    if kind == "tts" and key == SILENCE_KEY:
        kind = "silence"
    elif kind == "tts" and not d.get("message_visible", True):
        kind, url = "silence", f"{base.rstrip('/')}/{SILENCE_KEY}"  # a hidden message is never spoken
    return kind, url
