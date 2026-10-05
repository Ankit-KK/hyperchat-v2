"""Transactional email (password reset, invites).

Backends: "disabled" (default: nothing is sent; only the template name is logged, never the link)
and "resend" (HTTPS API, the provider the old custom login already used). Templates are the
existing branded HTML files with their single {{ .ConfirmationURL }} placeholder.
"""

import html
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

log = logging.getLogger("hyperchat.email")
TEMPLATES = Path(__file__).parent / "templates"
SUBJECTS = {"reset-password": "Reset your HyperChat password", "invite": "You're invited to HyperChat"}


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


class EmailSender(Protocol):
    async def send(self, email: Email) -> None: ...


class DisabledSender:
    async def send(self, email: Email) -> None:
        log.warning("email suppressed (email_backend=disabled)", extra={"template": email.template})


class ResendSender:
    def __init__(self, api_key: str, sender: str, client: httpx.AsyncClient | None = None) -> None:
        self._key = api_key
        self._from = sender
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def send(self, email: Email) -> None:
        resp = await self._client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {self._key}"},
            json={"from": self._from, "to": [email.to], "subject": email.subject, "html": email.html,
                  "text": email.text},
        )
        if resp.status_code >= 300:
            log.error("email send failed", extra={"template": email.template, "status": resp.status_code})
            raise RuntimeError("email provider error")
        log.info("email sent", extra={"template": email.template})
