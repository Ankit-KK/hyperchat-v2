"""Decision rules of the opt-in integration checks (smoke_integrations.py), importable without side effects so that
backend/tests/test_smoke_integrations.py can test them.

Every check is OFF unless explicitly enabled, and refuses rather than guesses:
  * Settings: only the approved DEV file /etc/hyperchat/api.env, by its canonical path (no symlink or alias on the way),
    a regular file not writable by group or others; no HYPERCHAT_* application setting in the process environment
    (it would override the file); only the known HYPERCHAT_SMOKE_* variables; the settings must say development, and a
    configured Razorpay key must be a test key.
  * Razorpay: HYPERCHAT_SMOKE_RAZORPAY=1 runs the D7 credential probe (GET /payments?count=1, no order, no payment);
    HYPERCHAT_SMOKE_RAZORPAY_ORDER=1 additionally creates ONE test-mode order. Only rzp_test_ keys.
  * Email: HYPERCHAT_SMOKE_EMAIL_TO=<address> sends ONE message, only when the same address is listed in
    HYPERCHAT_SMOKE_EMAIL_ALLOW (exact addresses, comma-separated), never to a reserved or synthetic domain, only with
    the resend backend configured and the approved DEV sender no-reply@mail.quicktestspace.site.
"""

import os
import re
import stat
from collections.abc import Mapping
from email.utils import parseaddr

DEV_ENV_FILE = "/etc/hyperchat/api.env"
APPROVED_SENDER = "no-reply@mail.quicktestspace.site"
KNOWN_SMOKE_VARS = ("HYPERCHAT_SMOKE_ENV_FILE", "HYPERCHAT_SMOKE_RAZORPAY", "HYPERCHAT_SMOKE_RAZORPAY_ORDER",
                    "HYPERCHAT_SMOKE_EMAIL_TO", "HYPERCHAT_SMOKE_EMAIL_ALLOW")
FLAGS = ("HYPERCHAT_SMOKE_RAZORPAY", "HYPERCHAT_SMOKE_RAZORPAY_ORDER")
RESERVED_DOMAINS = ("invalid", "test", "example", "localhost", "local")      # RFC 2606 / 6761 top-level names
RESERVED_HOSTS = ("example.com", "example.net", "example.org")
ADDRESS = re.compile(r"[a-z0-9._%+-]+@([a-z0-9-]+\.)+[a-z]{2,}")


def key_mode(key_id: str) -> str:
    """Razorpay key mode from the key id prefix (the id itself is never shown); as app.payments.accounts.key_mode."""
    return "test" if key_id.startswith("rzp_test_") else "live" if key_id.startswith("rzp_live_") else "unknown"


def any_enabled(env: Mapping[str, str]) -> bool:
    return any(env.get(k) for k in (*FLAGS, "HYPERCHAT_SMOKE_EMAIL_TO"))


def environment_refusal(env: Mapping[str, str]) -> str | None:
    """Variable NAMES only in the reason, never values."""
    overrides = sorted(k for k in env if k.startswith("HYPERCHAT_") and not k.startswith("HYPERCHAT_SMOKE_"))
    if overrides:
        return f"application settings in the process environment would override the settings file: {', '.join(overrides)}"
    unknown = sorted(k for k in env if k.startswith("HYPERCHAT_SMOKE_") and k not in KNOWN_SMOKE_VARS)
    if unknown:
        return f"unexpected variables for these checks: {', '.join(unknown)}"
    for k in FLAGS:
        if env.get(k, "") not in ("", "1"):
            return f"{k} must be 1 or unset"
    return None


def env_file_refusal(env: Mapping[str, str], approved: str = DEV_ENV_FILE) -> str | None:
    path = env.get("HYPERCHAT_SMOKE_ENV_FILE", approved)
    if path != approved:
        return f"HYPERCHAT_SMOKE_ENV_FILE may only be the approved DEV settings file {approved}"
    if os.path.realpath(path) != path:
        return "the settings file path is not canonical (a symlink or an alias on the way)"
    try:
        st = os.lstat(path)
    except OSError:
        return "the settings file does not exist or cannot be read"
    if not stat.S_ISREG(st.st_mode):
        return "the settings file is not a regular file"
    if st.st_mode & 0o022:
        return "the settings file is writable by group or others"
    return None


def settings_refusal(app_env: str, key_id: str) -> str | None:
    if app_env != "development":
        return f"the settings are not development ({app_env})"
    if key_id and key_mode(key_id) != "test":
        return f"a {key_mode(key_id)} Razorpay key is configured: these checks never run next to it"
    return None


def razorpay_plan(env: Mapping[str, str], app_env: str, key_id: str) -> tuple[str, str]:
    """("skip" | "run" | "refuse", reason)."""
    if env.get("HYPERCHAT_SMOKE_RAZORPAY") != "1":
        if env.get("HYPERCHAT_SMOKE_RAZORPAY_ORDER") == "1":
            return "refuse", "HYPERCHAT_SMOKE_RAZORPAY_ORDER needs HYPERCHAT_SMOKE_RAZORPAY=1"
        return "skip", "opt-in: HYPERCHAT_SMOKE_RAZORPAY=1 not set"
    if app_env != "development":
        return "refuse", "only with development settings"
    if not key_id:
        return "refuse", "no Razorpay key configured"
    mode = key_mode(key_id)
    if mode != "test":
        return "refuse", f"only rzp_test_ keys may be used here (found a {mode} key)"
    return "run", "test mode"


def order_wanted(env: Mapping[str, str]) -> bool:
    return env.get("HYPERCHAT_SMOKE_RAZORPAY_ORDER") == "1"


def sender_address(email_from: str) -> str:
    return parseaddr(email_from or "")[1].strip().lower()


def email_plan(env: Mapping[str, str], app_env: str, backend: str, key_set: bool,
               email_from: str = "") -> tuple[str, str, str]:
    """("skip" | "run" | "refuse", reason, recipient)."""
    to = env.get("HYPERCHAT_SMOKE_EMAIL_TO", "").strip().lower()
    if not to:
        return "skip", "opt-in: HYPERCHAT_SMOKE_EMAIL_TO not set", ""
    allow = {a.strip().lower() for a in env.get("HYPERCHAT_SMOKE_EMAIL_ALLOW", "").split(",") if a.strip()}
    if not allow:
        return "refuse", "HYPERCHAT_SMOKE_EMAIL_ALLOW (exact addresses) is required", ""
    if any("*" in a or "@" not in a or a.startswith("@") for a in allow):
        return "refuse", "HYPERCHAT_SMOKE_EMAIL_ALLOW takes exact addresses only (no wildcards or domains)", ""
    if to not in allow:
        return "refuse", "the recipient is not on HYPERCHAT_SMOKE_EMAIL_ALLOW", ""
    if not ADDRESS.fullmatch(to):
        return "refuse", "not a single plain email address", ""
    domain = to.rsplit("@", 1)[1]
    if domain.rsplit(".", 1)[-1] in RESERVED_DOMAINS or domain in RESERVED_HOSTS or domain.endswith(".invalid"):
        return "refuse", "a reserved or synthetic domain cannot receive mail", ""
    if app_env != "development":
        return "refuse", "only with development settings", ""
    if backend != "resend" or not key_set:
        return "refuse", "the email backend is not resend with a key (HYPERCHAT_EMAIL_BACKEND / HYPERCHAT_RESEND_API_KEY)", ""
    if sender_address(email_from) != APPROVED_SENDER:
        return "refuse", f"HYPERCHAT_EMAIL_FROM is not the approved DEV sender {APPROVED_SENDER}", ""
    return "run", "allowlisted recipient", to


def mask(address: str) -> str:
    """a***@example.com: enough to recognise the recipient in the evidence, not to harvest it."""
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def leaks(text: str, secrets: list[str]) -> list[int]:
    """Indexes of the given secret values (non-empty, 8+ characters) that appear in the text, ignoring case."""
    low = text.lower()
    return [i for i, s in enumerate(secrets) if s and len(s) >= 8 and s.lower() in low]
