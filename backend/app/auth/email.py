"""Transactional email (password reset, invites).

Backends: "disabled" (default: nothing is sent; only the template name is logged, never the link)
and "resend" (HTTPS API, the provider the old custom login already used). Templates are the
existing branded HTML files with their single {{ .ConfirmationURL }} placeholder.

With "resend", every template belongs to exactly one category (TEMPLATE_CATEGORY, fixed in code). A category sends
only when activated, from its own sender with its own domain-restricted key; an inactive category is refused before
any HTTP call and never re-routed through another one (docs/email-architecture-proposal.md). Outside production,
ResendSender also refuses, before any HTTP call, every recipient that is not on HYPERCHAT_EMAIL_RECIPIENT_ALLOWLIST.
"""

import hashlib
import html
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from app.core.config import EMAIL_ADDRESS, EMAIL_DEV_DOMAINS, Settings

log = logging.getLogger("hyperchat.email")
TEMPLATES = Path(__file__).parent / "templates"
SUBJECTS = {"reset-password": "Reset your HyperChat password", "invite": "You're invited to HyperChat"}
# Every invitation, an admin's included, is onboarding; a password reset is authentication (owner decision).
TEMPLATE_CATEGORY = {"reset-password": "authentication", "invite": "onboarding"}
assert TEMPLATE_CATEGORY.keys() == SUBJECTS.keys() and set(TEMPLATE_CATEGORY.values()) <= EMAIL_DEV_DOMAINS.keys()
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")  # Resend ids are UUIDs; anything else is not logged


@dataclass(frozen=True)
class Email:
    to: str
    template: str
    subject: str
    html: str
    text: str


# Plain-text fallback: (what the link does, what to know). The link stays on line 4 (tests and operators read it there).
# The durations match the defaults reset_token_ttl_minutes=60 and invite_ttl_days=7 (pinned by tests).
TEXTS = {
    "invite": ("Open this link to accept the invitation and set your password:",
               "This invitation is for {to}. The link works once and expires in 7 days. If you weren't expecting "
               "it, ignore this email: nothing changes unless the link is used."),
    "reset-password": ("Open this link to choose a new password:",
                       "The link expires in 1 hour. If you didn't request a password reset, ignore this email: "
                       "your password won't change."),
}


def render(template: str, to: str, url: str) -> Email:
    body = (TEMPLATES / f"{template}.html").read_text(encoding="utf-8")
    body = body.replace("{{ .ConfirmationURL }}", html.escape(url, quote=True))
    body = body.replace("{{ .Email }}", html.escape(to, quote=True))
    if "{{" in body:  # a placeholder render() does not fill would reach the recipient verbatim
        raise ValueError(f"unfilled placeholder in the {template} template")
    action, note = TEXTS[template]
    text = f"{SUBJECTS[template]}\n\n{action}\n{url}\n\n{note.format(to=to)}"
    return Email(to=to, template=template, subject=SUBJECTS[template], html=body, text=text)


class EmailCategoryInactive(RuntimeError):
    """The template's category is not activated: nothing was sent, and nothing is sent from another category."""


class EmailRecipientNotAllowed(RuntimeError):
    """Outside production, the recipient is not on the allowlist: nothing was sent."""


def recipient_allowed(to: object, allowed: frozenset[str] | None) -> bool:
    """Production (None): any recipient. Elsewhere: exactly one plain address that is on the (lower-cased) allowlist."""
    if allowed is None:
        return True
    return isinstance(to, str) and EMAIL_ADDRESS.fullmatch(to) is not None and to.lower() in allowed


class EmailDeliveryError(RuntimeError):
    """The provider did not accept the message. reason: refused_auth | rejected | transient."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"email provider error ({reason})")
        self.reason = reason


def _classify(status: int) -> str:
    if status in (401, 403):  # wrong or revoked key, or a domain the key may not send from: permanent
        return "refused_auth"
    if status == 429 or status >= 500:
        return "transient"
    return "rejected"


def _message_id(resp: httpx.Response) -> str:
    """The provider's message id from a 2xx body, or "" when it is missing, malformed or not a plain id. Never raises:
    the message was accepted, and unreadable metadata must not turn that into a failure (an invitation's link would
    then be revoked)."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - any unreadable body
        return ""
    value = body.get("id") if isinstance(body, dict) else None
    return value if isinstance(value, str) and _MESSAGE_ID.fullmatch(value) else ""


class EmailSender(Protocol):
    async def send(self, email: Email) -> None: ...


class DisabledSender:
    async def send(self, email: Email) -> None:
        log.warning("email suppressed (email_backend=disabled)", extra={"template": email.template})

    async def aclose(self) -> None:
        pass


class ResendSender:
    def __init__(self, api_key: str, sender: str, client: httpx.AsyncClient | None = None,
                 category: str | None = None, *, allowed_recipients: frozenset[str] | None) -> None:
        # Required: None (production, no restriction) or the allowlist; Settings.email_allowed_recipients picks it.
        self._allowed = allowed_recipients
        self._key = api_key
        self._from = sender
        self._owns_client = client is None  # an injected client belongs to the caller, who closes it
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._category = category

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send(self, email: Email) -> None:
        # No recipient identifier in P0: an unkeyed hash of an address is reversible by guessing (owner review).
        fields = {"category": self._category, "template": email.template}
        if not recipient_allowed(email.to, self._allowed):  # before any HTTP: the provider never sees the address
            log.warning("email refused: recipient not on the allowlist", extra=fields)
            raise EmailRecipientNotAllowed()
        # The same message (same link) always carries the same key, so a repeated send is not delivered twice.
        idempotency = hashlib.sha256(f"{email.template}\0{email.to}\0{email.text}".encode()).hexdigest()
        try:
            resp = await self._client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {self._key}", "Idempotency-Key": idempotency},
                json={"from": self._from, "to": [email.to], "subject": email.subject, "html": email.html,
                      "text": email.text},
            )
        except httpx.HTTPError as exc:
            log.error("email send failed", extra={**fields, "reason": "transient", "error": type(exc).__name__})
            raise EmailDeliveryError("transient") from None
        if resp.status_code >= 300:
            reason = _classify(resp.status_code)
            log.error("email send failed", extra={**fields, "status": resp.status_code, "reason": reason})
            raise EmailDeliveryError(reason)
        log.info("email sent", extra={**fields, "status": resp.status_code, "provider_message_id": _message_id(resp)})


class CategorySender:
    """Routes each email by its template's category to that category's own sender; refuses inactive categories."""

    def __init__(self, senders: dict[str, EmailSender]) -> None:
        self._senders = senders

    async def send(self, email: Email) -> None:
        category = TEMPLATE_CATEGORY[email.template]
        sender = self._senders.get(category)
        if sender is None:
            log.warning("email refused: category inactive", extra={"category": category, "template": email.template})
            raise EmailCategoryInactive(category)
        await sender.send(email)

    async def aclose(self) -> None:
        for sender in self._senders.values():
            close = getattr(sender, "aclose", None)
            if close is not None:
                await close()


def sender_from_settings(settings: Settings, client: httpx.AsyncClient | None = None) -> EmailSender:
    if settings.email_backend != "resend":
        return DisabledSender()
    senders: dict[str, EmailSender] = {}
    allowed = settings.email_allowed_recipients
    for category in settings.email_categories:  # each checked at startup (Settings._validate_email_categories)
        sender, key, _ = settings.email_category(category)
        senders[category] = ResendSender(key, sender, client, category=category, allowed_recipients=allowed)
    if senders and allowed == frozenset():
        log.warning("email recipient allowlist is empty: every email is refused (HYPERCHAT_EMAIL_RECIPIENT_ALLOWLIST)")
    return CategorySender(senders)
