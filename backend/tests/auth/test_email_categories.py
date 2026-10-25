"""Transactional email by category (P0 of docs/email-architecture-proposal.md). No HTTP leaves the process: Resend
is an httpx.MockTransport, and every key below is a synthetic test value."""

import json
import logging

import httpx
import pytest
from pydantic import ValidationError

from app.auth.email import (
    TEMPLATE_CATEGORY,
    CategorySender,
    DisabledSender,
    Email,
    EmailCategoryInactive,
    EmailDeliveryError,
    recipient_hash,
    render,
    sender_from_settings,
)
from app.core.config import EMAIL_DEV_DOMAINS, Settings

pytestmark = pytest.mark.anyio

ONB_KEY, AUTH_KEY = "re_TEST_onboarding_key_1", "re_TEST_authentication_key_2"
TO, LINK = "invitee@example.invalid", "https://front.invalid/invite?token=SECRET-TOKEN-123"
ONBOARDING = {"email_onboarding_from": "HyperChat <no-reply@onboarding.quicktestspace.site>",
              "email_onboarding_resend_api_key": ONB_KEY,
              "email_onboarding_verified_domain": "onboarding.quicktestspace.site"}
AUTHENTICATION = {"email_authentication_from": "HyperChat Security <no-reply@auth.quicktestspace.site>",
                  "email_authentication_resend_api_key": AUTH_KEY,
                  "email_authentication_verified_domain": "auth.quicktestspace.site"}


def settings(active="", app_env="test", **fields) -> Settings:
    return Settings(app_env=app_env, email_backend="resend", email_active_categories=active, **fields)


class Resend:
    def __init__(self, status=200, body=None, error=None):
        self.seen, self.status, self.body, self.error = [], status, body or {"id": "msg_test_1"}, error

    def handler(self, request):
        self.seen.append(request)
        if self.error:
            raise self.error
        return httpx.Response(self.status, json=self.body)

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def sent(request):
    return request.headers["authorization"], json.loads(request.content)["from"]


# ── routing ─────────────────────────────────────────────────────────────────────

def test_every_template_has_exactly_one_known_category():
    assert TEMPLATE_CATEGORY == {"reset-password": "authentication", "invite": "onboarding"}
    assert set(TEMPLATE_CATEGORY.values()) <= set(EMAIL_DEV_DOMAINS)


def test_the_default_configuration_sends_nothing():
    assert isinstance(sender_from_settings(Settings(app_env="test")), DisabledSender)
    assert Settings(app_env="test").email_categories == ()


async def test_inactive_category_is_refused_before_any_call_and_never_rerouted():
    resend = Resend()
    sender = sender_from_settings(settings("onboarding", **ONBOARDING), resend.client())
    with pytest.raises(EmailCategoryInactive):  # authentication is not active: the onboarding key is never used
        await sender.send(render("reset-password", TO, LINK))
    assert resend.seen == []
    await sender.send(render("invite", TO, LINK))
    assert [sent(r) for r in resend.seen] == [(f"Bearer {ONB_KEY}", ONBOARDING["email_onboarding_from"])]


async def test_resend_with_no_active_category_refuses_every_template():
    resend = Resend()
    sender = sender_from_settings(settings(**ONBOARDING), resend.client())  # configured but not activated
    for template in TEMPLATE_CATEGORY:
        with pytest.raises(EmailCategoryInactive):
            await sender.send(render(template, TO, LINK))
    assert resend.seen == []


async def test_each_template_uses_only_its_own_categorys_key_and_sender():
    resend = Resend()
    sender = sender_from_settings(settings("onboarding, authentication", **ONBOARDING, **AUTHENTICATION),
                                  resend.client())
    await sender.send(render("reset-password", TO, LINK))
    await sender.send(render("invite", TO, LINK))
    assert [sent(r) for r in resend.seen] == [
        (f"Bearer {AUTH_KEY}", AUTHENTICATION["email_authentication_from"]),
        (f"Bearer {ONB_KEY}", ONBOARDING["email_onboarding_from"])]


async def test_a_template_without_a_category_is_never_sent():
    with pytest.raises(KeyError):
        await CategorySender({"onboarding": DisabledSender()}).send(Email(TO, "unknown", "s", "h", "t"))


# ── startup validation ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(("active", "fields", "refusal"), [
    ("marketing", {}, "unknown HYPERCHAT_EMAIL_ACTIVE_CATEGORIES entries: ['marketing']"),
    ("onboarding", {**ONBOARDING, "email_onboarding_resend_api_key": None}, "ONBOARDING_RESEND_API_KEY is missing"),
    ("onboarding", {**ONBOARDING, "email_onboarding_resend_api_key": "sk_live_not_resend"},
     "ONBOARDING_RESEND_API_KEY is missing or not a Resend key"),
    ("onboarding", {**ONBOARDING, "email_onboarding_verified_domain": ""}, "ONBOARDING_VERIFIED_DOMAIN must be"),
    ("onboarding", {**ONBOARDING, "email_onboarding_verified_domain": "auth.quicktestspace.site"},
     "ONBOARDING_VERIFIED_DOMAIN must be onboarding.quicktestspace.site"),
    ("onboarding", {**ONBOARDING, "email_onboarding_from": "HyperChat <no-reply@auth.quicktestspace.site>"},
     "ONBOARDING_FROM must be an address on onboarding.quicktestspace.site"),
    ("onboarding", {**ONBOARDING, "email_onboarding_from": "no-reply@evil.onboarding.quicktestspace.site.invalid"},
     "ONBOARDING_FROM must be"),
    ("onboarding", {**ONBOARDING, "email_onboarding_from": "@onboarding.quicktestspace.site"}, "ONBOARDING_FROM"),
    ("authentication", {**AUTHENTICATION, "email_authentication_from": ONBOARDING["email_onboarding_from"]},
     "AUTHENTICATION_FROM must be an address on auth.quicktestspace.site"),
    ("onboarding,authentication", {**ONBOARDING, **AUTHENTICATION, "email_authentication_resend_api_key": ONB_KEY},
     "AUTHENTICATION_RESEND_API_KEY is the same key as HYPERCHAT_EMAIL_ONBOARDING_RESEND_API_KEY"),
])
def test_misconfigured_categories_refuse_to_start_without_echoing_values(active, fields, refusal):
    with pytest.raises(ValidationError, match="Refusing to start") as exc:
        settings(active, **fields)
    text = str(exc.value)
    assert refusal in text
    assert ONB_KEY not in text and AUTH_KEY not in text and "sk_live_not_resend" not in text


def test_no_category_can_be_activated_in_production_yet():
    with pytest.raises(ValidationError, match="no email category is approved for production"):
        settings("onboarding", app_env="production", **ONBOARDING)


def test_a_valid_category_is_accepted_and_others_stay_inactive():
    s = settings("Onboarding", **ONBOARDING, **AUTHENTICATION)  # authentication configured, not activated
    assert s.email_categories == ("onboarding",)
    assert s.email_category("onboarding")[2] == "onboarding.quicktestspace.site"


# ── provider results, idempotency, leaks ────────────────────────────────────────

@pytest.mark.parametrize(("status", "reason"), [(401, "refused_auth"), (403, "refused_auth"), (400, "rejected"),
                                                (422, "rejected"), (429, "transient"), (500, "transient"),
                                                (503, "transient")])
async def test_provider_errors_are_classified(status, reason):
    sender = sender_from_settings(settings("onboarding", **ONBOARDING), Resend(status).client())
    with pytest.raises(EmailDeliveryError) as exc:
        await sender.send(render("invite", TO, LINK))
    assert exc.value.reason == reason


async def test_network_errors_are_transient():
    resend = Resend(error=httpx.ConnectTimeout("timed out"))
    sender = sender_from_settings(settings("onboarding", **ONBOARDING), resend.client())
    with pytest.raises(EmailDeliveryError) as exc:
        await sender.send(render("invite", TO, LINK))
    assert exc.value.reason == "transient"


async def test_the_same_message_keeps_its_idempotency_key_and_another_link_gets_a_new_one():
    resend = Resend()
    sender = sender_from_settings(settings("onboarding", **ONBOARDING), resend.client())
    await sender.send(render("invite", TO, LINK))
    await sender.send(render("invite", TO, LINK))
    await sender.send(render("invite", TO, LINK + "x"))
    keys = [r.headers["idempotency-key"] for r in resend.seen]
    assert keys[0] == keys[1] != keys[2] and len(keys[0]) == 64 and "SECRET-TOKEN" not in keys[0]


@pytest.mark.parametrize("status", [200, 401, 500])
async def test_nothing_secret_is_logged_or_raised(caplog, status):
    caplog.set_level(logging.DEBUG)
    sender = sender_from_settings(settings("onboarding", **ONBOARDING), Resend(status).client())
    try:
        await sender.send(render("invite", TO, LINK))
        raised = ""
    except EmailDeliveryError as exc:
        raised = str(exc)
    logged = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert recipient_hash(TO) in logged and "onboarding" in logged
    for secret in (ONB_KEY, TO, "SECRET-TOKEN-123", LINK):
        assert secret not in logged and secret not in raised
    if status == 200:
        assert "msg_test_1" in logged
