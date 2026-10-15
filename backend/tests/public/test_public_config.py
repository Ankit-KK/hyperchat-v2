"""D5: public contact details and the Discord invite (GET /api/v1/public/config), from HYPERCHAT_PUBLIC_* settings.
No database: the values are validated at startup and served as they are."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.public.models import PublicConfig, PublicContact

SECRET = "sentinel-secret-d5-0f3a9c"  # planted in every secret setting: must never reach the response
CONTACT = {"HYPERCHAT_PUBLIC_CONTACT_NAME": "  HyperChat Support  ",
           "HYPERCHAT_PUBLIC_CONTACT_EMAIL": " Support@Example.TEST ",
           "HYPERCHAT_PUBLIC_CONTACT_PHONES": " +91 98765  43210 , (022) 555-0100 ,, ",
           "HYPERCHAT_PUBLIC_DISCORD_URL": " https://discord.gg/AbC-123 "}


def settings(**kw) -> Settings:
    return Settings(db_enabled=False, **kw)


def test_values_are_trimmed_and_normalized():
    s = settings(public_contact_name="  HyperChat  ", public_contact_email=" Support@Example.TEST ",
                 public_contact_phones=" +91 98765  43210 ,(022) 555-0100,, ",
                 public_discord_url=" https://discord.com/invite/hyperchat ")
    assert s.public_contact_name == "HyperChat" and s.public_contact_email == "support@example.test"
    assert s.public_contact_phones == "+91 98765 43210,(022) 555-0100"
    assert s.public_discord_url == "https://discord.com/invite/hyperchat"


def test_unset_and_blank_values_are_empty():
    for s in (settings(), settings(public_contact_name="  ", public_contact_email=" ", public_contact_phones=" , ",
                                   public_discord_url="  ")):
        assert (s.public_contact_name, s.public_contact_email, s.public_contact_phones, s.public_discord_url) == (
            "", "", "", "")


@pytest.mark.parametrize("url", ["https://discord.gg/abc", "https://Discord.gg/abc", "https://www.discord.com/invite/x-1",
                                 "https://discord.gg/abc/"])
def test_discord_invites_accepted(url):
    assert settings(public_discord_url=url).public_discord_url.endswith(url.rstrip("/").rsplit("/", 1)[1])


@pytest.mark.parametrize("url", [
    "http://discord.gg/abc", "discord.gg/abc", "https://evil.example/abc", "https://discord.gg.evil.example/abc",
    "https://discord.gg/abc?token=x", "https://discord.gg/abc#x", "https://user:pw@discord.gg/abc",
    "https://discord.gg:8443/abc", "https://discord.com/channels/1/2", "https://discord.gg/", "https://discord.gg/a b",
    "https://discord.gg/abc/def", "javascript:alert(1)",
])
def test_bad_discord_urls_refuse_to_start(url):
    with pytest.raises(ValidationError, match="HYPERCHAT_PUBLIC_DISCORD_URL"):
        settings(public_discord_url=url)


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "a b@example.test", "a@@example.test", "a@exa\tmple.test",
                                   "<a@example.test>", "a@example.test,b@example.test", "x" * 65 + "@example.test"])
def test_bad_emails_refuse_to_start(email):
    with pytest.raises(ValidationError, match="HYPERCHAT_PUBLIC_CONTACT_EMAIL"):
        settings(public_contact_email=email)


@pytest.mark.parametrize("phones", ["12345", "+91 98765 43210 ext 5", "+91\x0098765", "1234567890123456",
                                    "+91-98765-43210;drop", "++919876543210", "+91 98765\u200b43210", "a" * 30,
                                    ",".join(["9876543210"] * 6)])
def test_bad_phones_refuse_to_start(phones):
    with pytest.raises(ValidationError, match="HYPERCHAT_PUBLIC_CONTACT_PHONES"):
        settings(public_contact_phones=phones)


@pytest.mark.parametrize("name", ["x" * 81, "Hyper\x00Chat", "Hyper\nChat", "Hyper\u202eChat"])
def test_bad_names_refuse_to_start(name):
    with pytest.raises(ValidationError, match="HYPERCHAT_PUBLIC_CONTACT_NAME"):
        settings(public_contact_name=name)


def test_invalid_value_is_not_echoed_in_the_startup_error():
    with pytest.raises(ValidationError) as exc:
        settings(public_discord_url="https://discord.gg/abc?token=leak-me")
    assert "leak-me" not in str(exc.value)


def test_response_models_are_the_public_allow_list():
    assert set(PublicConfig.model_fields) == {"contact"}
    assert set(PublicContact.model_fields) == {"name", "email", "phones", "discord_url"}
    assert PublicConfig.model_config.get("extra") == PublicContact.model_config.get("extra") == "forbid"


# ── the endpoint ────────────────────────────────────────────────────────────────

@pytest.fixture
def client_with(monkeypatch):
    def make(values: dict[str, str]):
        monkeypatch.setenv("HYPERCHAT_DB_ENABLED", "false")
        monkeypatch.setenv("HYPERCHAT_APP_ENV", "test")
        for name in ("AUTH_SECRET", "UPLOAD_GRANT_SECRET", "RESEND_API_KEY", "TELEGRAM_BOT_TOKEN",
                     "DISCORD_BOT_TOKEN", "R2_SECRET_ACCESS_KEY", "SARVAM_API_KEY", "GOOGLE_CLIENT_SECRET"):
            monkeypatch.setenv(f"HYPERCHAT_{name}", SECRET)
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        from app.main import create_app

        return TestClient(create_app())

    yield make
    get_settings.cache_clear()


def test_all_fields_configured(client_with):
    with client_with(CONTACT) as client:
        r = client.get("/api/v1/public/config")
    assert r.status_code == 200
    assert r.json() == {"contact": {"name": "HyperChat Support", "email": "support@example.test",
                                    "phones": ["+91 98765 43210", "(022) 555-0100"],
                                    "discord_url": "https://discord.gg/AbC-123"}}
    assert r.headers["cache-control"] == "public, max-age=300" and "set-cookie" not in r.headers
    assert SECRET not in r.text


def test_nothing_configured_is_null_and_empty(client_with):
    with client_with({}) as client:
        r = client.get("/api/v1/public/config")
    assert r.status_code == 200
    assert r.json() == {"contact": {"name": None, "email": None, "phones": [], "discord_url": None}}
    assert SECRET not in r.text


def test_anonymous_get_only(client_with):
    with client_with(CONTACT) as client:
        assert client.get("/api/v1/public/config", headers={"Origin": "https://evil.example"}).status_code == 200
        for method in ("post", "put", "delete"):
            assert getattr(client, method)("/api/v1/public/config").status_code == 405


def test_an_invalid_value_refuses_to_start(client_with):
    with pytest.raises(ValidationError):
        client_with({"HYPERCHAT_PUBLIC_DISCORD_URL": "https://discord.gg.evil.example/x"})


def test_there_is_no_contact_submission_route(client_with):
    """D5: the legacy contact form is retired; nothing accepts a contact message."""
    with client_with({}) as client:
        paths = set(client.app.openapi()["paths"])
        assert "/api/v1/public/config" in paths and "/api/v1/public/signups" in paths
        assert not [p for p in paths if "contact" in p.lower()]
