"""A1: the DEV/test recipient allowlist (HYPERCHAT_EMAIL_RECIPIENT_ALLOWLIST). Outside production every category sends
only to listed addresses, refused before any HTTP call; production is unrestricted and may not set it. No HTTP leaves
the process: Resend is an httpx.MockTransport, and every key and address is a synthetic test value."""

import logging

import httpx
import pytest
from pydantic import ValidationError

from app.auth.email import (
    TEMPLATE_CATEGORY,
    CategorySender,
    Email,
    EmailCategoryInactive,
    EmailRecipientNotAllowed,
    ResendSender,
    recipient_allowed,
    render,
    sender_from_settings,
)
from app.core.config import EMAIL_DEV_DOMAINS, Settings

pytestmark = pytest.mark.anyio

ALLOWED, OTHER = "tester+c1@example.invalid", "stranger@example.invalid"
LINK = "https://front.invalid/invite?token=SECRET-TOKEN-123"
CATEGORIES = {c: {f"email_{c}_from": f"HyperChat <no-reply@{d}>", f"email_{c}_resend_api_key": f"re_TEST_{c}_key",
                  f"email_{c}_verified_domain": d} for c, d in EMAIL_DEV_DOMAINS.items()}
ALL_FIELDS = {k: v for fields in CATEGORIES.values() for k, v in fields.items()}


def settings(allowlist=ALLOWED, active=",".join(EMAIL_DEV_DOMAINS), app_env="test") -> Settings:
    return Settings(app_env=app_env, email_backend="resend", email_active_categories=active,
                    email_recipient_allowlist=allowlist, **ALL_FIELDS)


class Resend:
    def __init__(self):
        self.seen = []

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: self.seen.append(request) or httpx.Response(200, json={"id": "msg_test_1"})))


def message(to, category):
    """An email of `category` (the two real templates, or a synthetic one for the categories without a template)."""
    template = next((t for t, c in TEMPLATE_CATEGORY.items() if c == category), None)
    if template:
        return render(template, to, LINK)
    return Email(to=to, template=f"synthetic-{category}", subject="s", html=f"<p>{LINK}</p>", text=LINK)


# ── configuration ───────────────────────────────────────────────────────────────

def test_entries_are_normalized_and_matched_exactly():
    s = settings(" Tester+C1@Example.INVALID , second@example.invalid ")
    assert s.email_allowed_recipients == frozenset({ALLOWED, "second@example.invalid"})


@pytest.mark.parametrize("allowlist", ["", "   "])
def test_a_missing_or_empty_allowlist_allows_nothing(allowlist):
    assert settings(allowlist).email_allowed_recipients == frozenset()
    assert Settings(app_env="development").email_allowed_recipients == frozenset()  # the default


@pytest.mark.parametrize("allowlist, positions", [
    ("*@example.invalid", [1]), ("tester@*.invalid", [1]), ("tester@", [1]), ("@example.invalid", [1]),
    ("tester@localhost", [1]), ("tester", [1]), (f"{ALLOWED},", [2]), (f",{ALLOWED}", [1]),
    (f"{ALLOWED},,{OTHER}", [2]), (f"Tester <{ALLOWED}>", [1]), (f"{ALLOWED};{OTHER}", [1]),
    (f"{ALLOWED} {OTHER}", [1]), ("tést@example.invalid", [1]), ("tester@exаmple.invalid", [1]),  # Cyrillic a
    (f"{OTHER},a b@example.invalid", [2]), ("tester@example.invalid.", [1]), ("tester@@example.invalid", [1]),
])
def test_a_malformed_allowlist_refuses_to_start_without_echoing_addresses(allowlist, positions):
    with pytest.raises(ValidationError) as err:
        settings(allowlist)
    assert f"HYPERCHAT_EMAIL_RECIPIENT_ALLOWLIST entries {positions}" in str(err.value)
    assert "example.invalid" not in str(err.value) and "re_TEST_" not in str(err.value)


def test_production_may_not_set_an_allowlist_and_is_never_restricted():
    with pytest.raises(ValidationError, match="must be empty in production"):
        Settings(app_env="production", db_label="PRODUCTION", email_recipient_allowlist=ALLOWED)
    assert Settings(app_env="production", db_label="PRODUCTION").email_allowed_recipients is None  # no restriction


# ── the guard ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("category", sorted(EMAIL_DEV_DOMAINS))
async def test_every_category_sends_to_an_allowed_recipient(category):
    resend = Resend()
    sender = ResendSender(f"re_TEST_{category}_key", "x@y.invalid", resend.client(), category=category,
                          allowed_recipients=settings().email_allowed_recipients)
    await sender.send(message(ALLOWED, category))
    assert len(resend.seen) == 1


@pytest.mark.parametrize("category", sorted(EMAIL_DEV_DOMAINS))
async def test_every_category_refuses_others_before_any_call(category, caplog):
    resend = Resend()
    sender = ResendSender(f"re_TEST_{category}_key", "x@y.invalid", resend.client(), category=category,
                          allowed_recipients=settings().email_allowed_recipients)
    with caplog.at_level(logging.DEBUG), pytest.raises(EmailRecipientNotAllowed):
        await sender.send(message(OTHER, category))
    assert resend.seen == []
    records = [r for r in caplog.records if r.name == "hyperchat.email"]  # not asyncio's own debug lines
    assert [r.getMessage() for r in records] == ["email refused: recipient not on the allowlist"]
    assert records[0].category == category


@pytest.mark.parametrize("template", sorted(TEMPLATE_CATEGORY))
async def test_the_api_sender_enforces_it_for_both_templates(template):
    resend = Resend()
    sender = sender_from_settings(settings(), resend.client())
    await sender.send(render(template, ALLOWED, LINK))
    with pytest.raises(EmailRecipientNotAllowed):
        await sender.send(render(template, OTHER, LINK))
    assert len(resend.seen) == 1


async def test_an_empty_allowlist_refuses_everything_and_says_so_once(caplog):
    resend = Resend()
    with caplog.at_level(logging.WARNING):
        sender = sender_from_settings(settings(""), resend.client())
    assert "allowlist is empty: every email is refused" in caplog.text
    for template in TEMPLATE_CATEGORY:
        with pytest.raises(EmailRecipientNotAllowed):
            await sender.send(render(template, ALLOWED, LINK))
    assert resend.seen == []


async def test_an_inactive_category_is_still_refused_first():
    resend = Resend()
    sender = sender_from_settings(settings(active="onboarding"), resend.client())
    with pytest.raises(EmailCategoryInactive):
        await sender.send(render("reset-password", ALLOWED, LINK))
    assert resend.seen == []


@pytest.mark.parametrize("to", [
    "tester+other@example.invalid",          # another plus-address is another address
    "tester@example.invalid",                # the base address of a listed plus-address
    "tester+c1@sub.example.invalid", "tester+c1@example.invalid.evil.invalid", "tester+c1@example.invalid.",
    " tester+c1@example.invalid", "tester+c1@example.invalid ", "tester+c1@example.invalid\n",
    "tester+c1@example.invalid\r\nBcc: stranger@example.invalid", f"{ALLOWED},{OTHER}", f"{ALLOWED};{OTHER}",
    f"Tester <{ALLOWED}>", f"<{ALLOWED}>", f'"{ALLOWED}"', "tester+c1@exаmple.invalid",  # Cyrillic a
    "tester+c1@example.invalid\x00", "", None, [ALLOWED], (ALLOWED,),
])
async def test_bypass_attempts_are_refused_before_any_call(to):
    resend = Resend()
    sender = ResendSender("re_TEST_onboarding_key", "x@y.invalid", resend.client(), category="onboarding",
                          allowed_recipients=frozenset({ALLOWED}))
    with pytest.raises(EmailRecipientNotAllowed):
        await sender.send(Email(to=to, template="invite", subject="s", html="h", text=LINK))
    assert resend.seen == []


@pytest.mark.parametrize("to", ["Kim@example.invalid", "kim@example.İnvalid"])  # Kelvin sign, dotted I
async def test_unicode_that_lower_cases_onto_an_allowed_address_is_refused(to):
    resend = Resend()
    sender = ResendSender("re_TEST_onboarding_key", "x@y.invalid", resend.client(), category="onboarding",
                          allowed_recipients=frozenset({"kim@example.invalid"}))
    assert "Kim@example.invalid".lower() == "kim@example.invalid"  # the trap this guards against
    with pytest.raises(EmailRecipientNotAllowed):
        await sender.send(Email(to=to, template="invite", subject="s", html="h", text=LINK))
    assert resend.seen == []


async def test_case_differences_are_the_same_address():
    assert recipient_allowed("Tester+C1@EXAMPLE.invalid", frozenset({ALLOWED}))


def test_the_allowlist_is_a_required_argument():
    with pytest.raises(TypeError):
        ResendSender("re_TEST_onboarding_key", "x@y.invalid")  # type: ignore[call-arg]


async def test_production_none_keeps_sending_to_anyone():
    resend = Resend()
    sender = ResendSender("re_TEST_onboarding_key", "x@y.invalid", resend.client(), allowed_recipients=None)
    await sender.send(render("invite", OTHER, LINK))
    assert len(resend.seen) == 1


async def test_a_refusal_logs_no_address_key_link_or_content(caplog):
    resend = Resend()
    sender = sender_from_settings(settings(), resend.client())
    with caplog.at_level(logging.DEBUG), pytest.raises(EmailRecipientNotAllowed) as err:
        await sender.send(render("invite", OTHER, LINK))
    dumped = caplog.text + "".join(str(r.__dict__) for r in caplog.records) + str(err.value) + repr(err.value)
    for value in (OTHER, "stranger", "SECRET-TOKEN-123", "re_TEST_", "You're invited", "<p"):
        assert value not in dumped, value


async def test_a_category_sender_cannot_be_built_around_the_guard():
    """Every ResendSender the API builds carries the settings' allowlist (no path constructs one without it)."""
    sender = sender_from_settings(settings(), Resend().client())
    assert isinstance(sender, CategorySender)
    assert {c: s._allowed for c, s in sender._senders.items()} == {
        c: frozenset({ALLOWED}) for c in EMAIL_DEV_DOMAINS}
