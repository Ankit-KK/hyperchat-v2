"""Decision rules of the opt-in integration checks (smoke_integrations.py), importable without side effects so that
backend/tests/test_smoke_integrations.py can test them.

Every check is OFF unless explicitly enabled, and refuses rather than guesses:
  * Razorpay: HYPERCHAT_SMOKE_RAZORPAY=1 runs the D7 credential probe (GET /payments?count=1, no order, no payment);
    HYPERCHAT_SMOKE_RAZORPAY_ORDER=1 additionally creates ONE test-mode order. Only rzp_test_ keys, never in production.
  * Email: HYPERCHAT_SMOKE_EMAIL_TO=<address> sends ONE message, only when the same address is listed in
    HYPERCHAT_SMOKE_EMAIL_ALLOW (exact addresses, comma-separated), never to a reserved or synthetic domain, only with
    the resend backend configured, never in production.
"""

import re
from collections.abc import Mapping

RESERVED_DOMAINS = ("invalid", "test", "example", "localhost", "local")      # RFC 2606 / 6761 top-level names
RESERVED_HOSTS = ("example.com", "example.net", "example.org")
ADDRESS = re.compile(r"[a-z0-9._%+-]+@([a-z0-9-]+\.)+[a-z]{2,}")


def key_mode(key_id: str) -> str:
    """Razorpay key mode from the key id prefix (the id itself is never shown); as app.payments.accounts.key_mode."""
    return "test" if key_id.startswith("rzp_test_") else "live" if key_id.startswith("rzp_live_") else "unknown"


def razorpay_plan(env: Mapping[str, str], app_env: str, key_id: str) -> tuple[str, str]:
    """("skip" | "run" | "refuse", reason)."""
    if env.get("HYPERCHAT_SMOKE_RAZORPAY") != "1":
        if env.get("HYPERCHAT_SMOKE_RAZORPAY_ORDER") == "1":
            return "refuse", "HYPERCHAT_SMOKE_RAZORPAY_ORDER needs HYPERCHAT_SMOKE_RAZORPAY=1"
        return "skip", "opt-in: HYPERCHAT_SMOKE_RAZORPAY=1 not set"
    if app_env == "production":
        return "refuse", "never in production"
    if not key_id:
        return "refuse", "no Razorpay key configured"
    mode = key_mode(key_id)
    if mode != "test":
        return "refuse", f"only rzp_test_ keys may be used here (found a {mode} key)"
    return "run", "test mode"


def order_wanted(env: Mapping[str, str]) -> bool:
    return env.get("HYPERCHAT_SMOKE_RAZORPAY_ORDER") == "1"


def email_plan(env: Mapping[str, str], app_env: str, backend: str, key_set: bool) -> tuple[str, str, str]:
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
        return "refuse", "not a plain email address", ""
    domain = to.rsplit("@", 1)[1]
    if domain.rsplit(".", 1)[-1] in RESERVED_DOMAINS or domain in RESERVED_HOSTS or domain.endswith(".invalid"):
        return "refuse", "a reserved or synthetic domain cannot receive mail", ""
    if app_env == "production":
        return "refuse", "never in production", ""
    if backend != "resend" or not key_set:
        return "refuse", "the email backend is not resend with a key (HYPERCHAT_EMAIL_BACKEND / HYPERCHAT_RESEND_API_KEY)", ""
    return "run", "allowlisted recipient", to


def mask(address: str) -> str:
    """a***@example.com: enough to recognise the recipient in the evidence, not to harvest it."""
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def leaks(text: str, secrets: list[str]) -> list[int]:
    """Indexes of the given secret values (non-empty, 8+ characters) that appear in the text."""
    return [i for i, s in enumerate(secrets) if s and len(s) >= 8 and s in text]
