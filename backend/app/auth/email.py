"""Transactional email (password reset, invites).

Backends: "disabled" (default: nothing is sent; only the template name is logged, never the link)
and "resend" (HTTPS API, the provider the old custom login already used). Templates are the
existing branded HTML files with their single {{ .ConfirmationURL }} placeholder.

With "resend", every template belongs to exactly one category (TEMPLATE_CATEGORY, fixed in code). A category sends
only when activated, from its own sender with its own domain-restricted key; an inactive category is refused before
any HTTP call and never re-routed through another one (docs/email-architecture-proposal.md).
"""

import hashlib
import html
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from app.core.config import EMAIL_DEV_DOMAINS, Settings

log = logging.getLogger("hyperchat.email")
TEMPLATES = Path(__file__).parent / "templates"
SUBJECTS = {"reset-password": "Reset your HyperChat password", "invite": "You're invited to HyperChat"}
# Every invitation, an admin's included, is onboarding; a password reset is authentication (owner decision).
TEMPLATE_CATEGORY = {"reset-password": "authentication", "invite": "onboarding"}
assert TEMPLATE_CATEGORY.keys() == SUBJECTS.keys() and set(TEMPLATE_CATEGORY.values()) <= EMAIL_DEV_DOMAINS.keys()


@dataclass(frozen=True)
class Email:
    to: str
    template: str
    subject: str
    html: str
    text: str


def render(template: str, to: str, url: str) -> Email:
    body = (TEMPLATES / f"{template}.html").read_text(encoding="utf-8")
    body = body.replace("{{ .ConfirmationURL }}", html.escape(url, quote=True))
    text = f"{SUBJECTS[template]}\n\nOpen this link to continue:\n{url}\n\nIf you did not request this, ignore this email."
    return Email(to=to, template=template, subject=SUBJECTS[template], html=body, text=text)


def recipient_hash(address: str) -> str:
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()


class EmailCategoryInactive(RuntimeError):
    """The template's category is not activated: nothing was sent, and nothing is sent from another category."""


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


class EmailSender(Protocol):
    async def send(self, email: Email) -> None: ...


class DisabledSender:
    async def send(self, email: Email) -> None:
        log.warning("email suppressed (email_backend=disabled)", extra={"template": email.template})


class ResendSender:
    def __init__(self, api_key: str, sender: str, client: httpx.AsyncClient | None = None,
                 category: str | None = None) -> None:
        self._key = api_key
        self._from = sender
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._category = category

    async def send(self, email: Email) -> None:
        fields = {"category": self._category, "template": email.template, "recipient_hash": recipient_hash(email.to)}
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
        try:
            message_id = str(resp.json().get("id") or "")[:100]
        except ValueError:
            message_id = ""
        log.info("email sent", extra={**fields, "provider_message_id": message_id})


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


def sender_from_settings(settings: Settings, client: httpx.AsyncClient | None = None) -> EmailSender:
    if settings.email_backend != "resend":
        return DisabledSender()
    senders: dict[str, EmailSender] = {}
    for category in settings.email_categories:  # each checked at startup (Settings._validate_email_categories)
        sender, key, _ = settings.email_category(category)
        senders[category] = ResendSender(key, sender, client, category=category)
    return CategorySender(senders)
