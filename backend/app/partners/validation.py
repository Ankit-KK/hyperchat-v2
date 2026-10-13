"""Legacy partner contract (supabase/functions/partner-signup and partner-check-streamer, docs/PARTNER_SIGNUP_API.md),
kept field for field so ClipWave only changes the URL. Deliberate differences (docs/partner-api.md):
  * unknown body fields and non-string values are refused (legacy ignored them / read them as empty);
  * control characters are refused in every field;
  * a YouTube link must be http(s) with no user part; it is stored as sent, with https:// added when the scheme was left
    out (legacy re-serialised it through URL());
  * the creator check also tries the name with "-" kept, because V2 slugs may contain "-" (legacy turned "-" into "_").
"""

import re
from urllib.parse import urlsplit

from app.payments.rules import has_pictograph

SIGNUP_FIELDS = ("channel_name", "name", "mobile_number", "youtube_channel", "email", "instagram_handle")
EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}")
YOUTUBE_HOSTS = frozenset({"youtube.com", "m.youtube.com", "youtu.be"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def legacy_slug(text: str) -> str:
    """The legacy normalizeSlug: lowercase, spaces and "-" become "_", anything else but [a-z0-9_] dropped."""
    s = re.sub(r"[\s-]+", "_", text.strip().lower())
    s = re.sub(r"[^a-z0-9_]", "", s)
    return re.sub(r"_+", "_", s).strip("_")


def slug_candidates(text: str) -> list[str]:
    """Exact slugs a channel name may match: the legacy form, and the same with "-" kept (V2 slugs allow it)."""
    dashed = re.sub(r"_+", "_", re.sub(r"[^a-z0-9_-]", "", re.sub(r"\s+", "_", text.strip().lower()))).strip("_-")
    return list(dict.fromkeys(s for s in (legacy_slug(text), dashed) if s))


def _youtube(value: str) -> tuple[str | None, str | None]:
    url = value if value.lower().startswith(("http://", "https://")) else "https://" + value
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").removeprefix("www.")
    except ValueError:
        return None, "YouTube channel must be a valid URL."
    if parts.scheme.lower() not in ("http", "https") or "@" in parts.netloc or host not in YOUTUBE_HOSTS:
        return None, "YouTube channel must be a youtube.com or youtu.be link."
    if len(url) > 300:
        return None, "YouTube channel link is too long."
    return url, None


def signup(body: object) -> tuple[dict, dict]:
    """(field errors, clean values) for a partner signup body. Values are trimmed; the email is lowercased."""
    if not isinstance(body, dict):
        return {"body": "The body must be a JSON object."}, {}
    errors = {k: "Unknown field." for k in body if k not in SIGNUP_FIELDS}
    for k in SIGNUP_FIELDS:
        v = body.get(k)
        if v is not None and not isinstance(v, str):
            errors[k] = "Must be a string."
        elif isinstance(v, str) and _CONTROL.search(v):
            errors[k] = "Must not contain control characters."
    text = {k: body[k].strip() if isinstance(body.get(k), str) else "" for k in SIGNUP_FIELDS}
    channel = text["channel_name"] or text["name"]  # "name": the legacy deprecated alias
    if "channel_name" not in errors and "name" not in errors:
        if not 2 <= len(channel) <= 100:
            errors["channel_name"] = "Channel name must be between 2 and 100 characters."
        elif has_pictograph(channel):
            errors["channel_name"] = "Channel name cannot contain emojis."
    if "mobile_number" not in errors and not 10 <= len(re.sub(r"\D", "", text["mobile_number"])) <= 15:
        errors["mobile_number"] = "Mobile number must contain 10 to 15 digits."
    email = text["email"].lower()
    if "email" not in errors and (not EMAIL.fullmatch(email) or len(email) > 254):
        errors["email"] = "A valid email address is required."
    youtube = None
    if "youtube_channel" not in errors:
        youtube, problem = (_youtube(text["youtube_channel"]) if text["youtube_channel"]
                            else (None, "YouTube channel must be a valid URL."))
        if problem:
            errors["youtube_channel"] = problem
    if "instagram_handle" not in errors and len(text["instagram_handle"]) > 100:
        errors["instagram_handle"] = "Instagram handle is too long."
    if "mobile_number" not in errors and len(text["mobile_number"]) > 40:
        errors["mobile_number"] = "Mobile number is too long."
    return errors, {"channel_name": channel, "mobile_number": text["mobile_number"], "youtube_channel": youtube,
                    "email": email, "instagram_handle": text["instagram_handle"] or None}


def check(body: object) -> tuple[dict, list[str]]:
    """(field errors, slug candidates) for a creator check body: {"channel_name": "..."} only."""
    if not isinstance(body, dict):
        return {"body": "The body must be a JSON object."}, []
    errors = {k: "Unknown field." for k in body if k != "channel_name"}
    value = body.get("channel_name")
    name = value.strip() if isinstance(value, str) else ""
    if not name or len(name) > 100 or _CONTROL.search(name):
        errors["channel_name"] = "channel_name is required (max 100 characters)."
        return errors, []
    candidates = slug_candidates(name)
    if not candidates:
        errors["channel_name"] = "channel_name must contain letters or numbers."
    return errors, candidates
