"""Pure transformation rules (unit-tested; no database access)."""

import hashlib
import re

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
DONATION_TABLE_SUFFIX = "_donations"
# Legacy streamers."razorpay-keyid"/"razorpay-keysecret" held the NAMES of secrets; only these two pairs exist.
# Same mapping as migration 0010; any other pair has no payment account (checkout on hold).
RAZORPAY_ACCOUNTS = {
    ("razorpay-keyid", "razorpay-keysecret"): "primary",
    ("razorpay-keyid(test)", "razorpay-keysecret(test)"): "secondary",
}


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


def obs_token_hash(token: str) -> str:
    """OBS tokens are stored as SHA-256 hex; existing OBS URLs keep working by hashing the presented token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
