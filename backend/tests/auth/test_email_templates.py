"""The rendered emails as a recipient sees them: every placeholder filled, the right wording for each flow, the link
escaped and in the right places, the plain-text fallback, and readable contrast (WCAG AA)."""

import re

import pytest

from app.auth.email import SUBJECTS, render
from app.core.config import Settings

URL = "https://app.example.invalid/invite?token=abc123&next=<x>"
SAFE = "https://app.example.invalid/invite?token=abc123&amp;next=&lt;x&gt;"
TO = "invitee@example.invalid"
SITE = "https://www.hyperchat.space"


@pytest.mark.parametrize("template", ["invite", "reset-password"])
def test_every_placeholder_is_filled_and_the_link_appears_only_escaped(template):
    email = render(template, TO, URL)
    for part in (email.html, email.text, email.subject):
        assert "{{" not in part and "}}" not in part
    assert email.html.count(SAFE) == 4  # Outlook button, button, fallback href, fallback text
    assert URL not in email.html  # never unescaped in HTML
    assert set(re.findall(r'href="([^"]+)"', email.html)) == {SAFE, SITE}
    assert f"<title>{SUBJECTS[template]}</title>" in email.html.replace("&#x27;", "'")
    assert email.subject == SUBJECTS[template]


@pytest.mark.parametrize("template", ["invite", "reset-password"])
def test_plain_text_fallback_carries_the_link_on_line_four(template):
    lines = render(template, TO, URL).text.split("\n")
    assert lines[0] == SUBJECTS[template] and lines[3] == URL


def test_the_invitation_names_the_invited_address_and_never_uses_signup_wording():
    email = render("invite", TO, URL)
    assert f"<strong>{TO}</strong>" in email.html and f"This invitation is for {TO}." in email.text
    assert "Accept invitation" in email.html and "set your password" in email.html
    for wrong in ("Confirm", "confirm", "sign up", "signup", "activate", "create a HyperChat account"):
        assert wrong not in email.html and wrong not in email.text


def test_the_recipient_address_is_html_escaped():
    email = render("invite", 'x"><script>alert(1)</script>@example.invalid', URL)
    assert "<script>" not in email.html and "&lt;script&gt;" in email.html


def test_the_reset_email_keeps_its_own_wording():
    email = render("reset-password", TO, "https://app.example.invalid/reset-password?token=abc123")
    assert "Reset Password" in email.html and "choose a new password" in email.text
    assert TO not in email.html  # the reset email does not echo the address
    for wrong in ("invite", "Invitation", "sign up", "activate"):
        assert wrong not in email.html


def test_the_stated_expiry_matches_the_default_settings():
    s = Settings(app_env="test")
    assert s.invite_ttl_days == 7 and s.reset_token_ttl_minutes == 60
    invite, reset = render("invite", TO, URL), render("reset-password", TO, URL)
    assert "expires in 7 days" in invite.html and "expires in 7 days" in invite.text
    assert "expires in 1 hour" in reset.html and "expires in 1 hour" in reset.text


def test_a_template_with_an_unfilled_placeholder_is_never_rendered(tmp_path, monkeypatch):
    (tmp_path / "invite.html").write_text("<p>{{ .Unknown }}</p>", encoding="utf-8")
    monkeypatch.setattr("app.auth.email.TEMPLATES", tmp_path)
    with pytest.raises(ValueError, match="unfilled placeholder"):
        render("invite", TO, URL)


def _luminance(hex_color):
    def channel(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("template", ["invite", "reset-password"])
def test_text_on_the_white_card_meets_wcag_aa(template):
    html = render(template, TO, URL).html
    card = html.split("<!-- Card -->", 1)[1].split("<!-- Footer -->", 1)[0]
    text_colors = set(re.findall(r"(?<!background-)color:(#[0-9A-Fa-f]{6})", card)) - {"#FFFFFF"}
    assert text_colors and all(_contrast(c, "#FFFFFF") >= 4.5 for c in text_colors), text_colors
    for bg in set(re.findall(r'(?:background-color:|fillcolor=")(#[0-9A-Fa-f]{6})', card)) - {"#FFFFFF"}:
        assert _contrast("#FFFFFF", bg) >= 4.5, bg  # white button label
