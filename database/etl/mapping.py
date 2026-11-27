"""Pure transformation rules (unit-tested; no database access)."""

import re
from urllib.parse import urlsplit

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
DONATION_TABLE_SUFFIX = "_donations"
# Legacy streamers."razorpay-keyid"/"razorpay-keysecret" held the NAMES of secrets; only these two pairs exist.
# Both are the one V2 account (D6, migration 0018): the "(test)" pair held Razorpay TEST-MODE keys, which is an
# environment concern in V2 (DEV = test keys, production = live keys), not a second merchant account. Any other
# pair has no payment account (checkout on hold). Orders never get an account from this: legacy recorded none.
RAZORPAY_ACCOUNTS = {
    ("razorpay-keyid", "razorpay-keysecret"): "primary",
    ("razorpay-keyid(test)", "razorpay-keysecret(test)"): "primary",
}


# Creator image columns rewritten from a legacy public media base to the production one (owner decision D9:
# production serves media from a configured custom domain, not r2.dev). Fresh start (owner, 2026-11-27): only each
# creator's profile picture and background are carried over (docs/operations/legacy-asset-migration-plan.md);
# donation media and soundboard files are not, so no other table is rewritten. Only the scheme+host(+path) prefix
# changes; the object key is kept byte for byte.
MEDIA_URL_COLUMNS = {
    "streamers": ("brand_logo_url", "background_url"),
}

# Fresh start: legacy donation media, voice messages and donor links are history only. These donation columns are
# loaded as NULL, so no V2 path (OBS queue, replay, dashboard, donor status / upload) can serve, play or accept a
# legacy URL or token. The reconciliation counts what the source held and asserts the target holds none.
DONATION_CLEARED_COLUMNS = ("voice_message_url", "hypersound_url", "media_url", "media_type", "tts_audio_url",
                            "status_token_hash", "upload_token_hash")

# Columns the V2 schema added to straight-copied tables that legacy never had: left to their defaults (or, in
# LEGACY_VALUES, set to the value the migration gave the rows that existed then). Explicit: any OTHER target column
# missing in the source still fails the load. A new migration that adds a column to one of these tables must add it
# here (test_etl_mapping pins the list against the migrations).
V2_ONLY_COLUMNS = {
    "banned_donors": ("unbanned_at", "unbanned_by_label", "unbanned_by_user_id"),  # 0016
    "user_signups": ("review_status", "reviewed_by", "reviewed_at", "rejection_reason", "streamer_id",
                     "invite_id"),  # 0021
}
# 0021 gave the signups that existed then 'legacy_unreviewed' and then changed the column default to 'submitted':
# imported rows keep the legacy meaning (never offered for approval as if new).
LEGACY_VALUES = {
    "user_signups": {"review_status": "legacy_unreviewed"},
}


def media_base(value: str) -> str:
    """A media base URL as the ETL accepts it: https, a host, an optional path, no query or fragment; returned
    without a trailing slash. Anything else raises ValueError."""
    parts = urlsplit(value or "")
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment or "@" in parts.netloc:
        raise ValueError("a media base must be an https URL without credentials, query or fragment")
    return value.rstrip("/")


def rewrite_media_url(url: str | None, legacy_bases: tuple[str, ...], target_base: str | None) -> str | None:
    """`url` with a legacy media base prefix replaced by `target_base` (the key after it unchanged). NULL, a URL on
    the target base already, and any other URL (another host, a Supabase Storage URL, an external link) are
    returned unchanged. Deterministic and idempotent. Prefixes are compared exactly, including the '/' after them,
    so https://legacy.example does not match https://legacy.example.evil."""
    if url is None or not target_base:
        return url
    for base in legacy_bases:
        prefix = base + "/"
        if url.startswith(prefix) and len(url) > len(prefix):
            return target_base + "/" + url[len(prefix):]
    return url


def split_approved_by(value: str | None, moderator_ids: set[str]) -> tuple[str | None, str | None]:
    """approved_by held either a streamers_moderators.id (UUID) or a free-text label."""
    if value is None or value == "":
        return None, None
    text = str(value)
    if UUID_RE.match(text) and text.lower() in moderator_ids:
        return text.lower(), None
    return None, text


def normalize_currency(value: str | None) -> str:
    """NULL takes the column default (INR); anything else must already be a 3-letter code."""
    code = ("INR" if value is None else value).strip().upper()
    if not CURRENCY_RE.match(code):
        raise ValueError(f"unsupported currency code: {code!r}")
    return code


def user_role(source_role: str | None, email: str, admin_emails: set[str]) -> str:
    """admin_emails is folded into users.role (case-insensitive match)."""
    if email.lower() in admin_emails:
        return "admin"
    return source_role or "user"


def razorpay_account(key_ref: str | None, secret_ref: str | None) -> str | None:
    return RAZORPAY_ACCOUNTS.get((key_ref, secret_ref))


def slug_from_table(table_name: str) -> str:
    if not table_name.endswith(DONATION_TABLE_SUFFIX):
        raise ValueError(f"not a donation table: {table_name}")
    return table_name[: -len(DONATION_TABLE_SUFFIX)]
