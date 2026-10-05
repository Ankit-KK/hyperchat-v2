"""Browser-origin configuration and the production guards (no database)."""

import pytest

from app.core.config import Settings
from app.core.origins import OriginError, is_development_origin, normalize_origin, parse_origins


def settings(**kw) -> Settings:
    return Settings(db_enabled=False, **kw)


@pytest.mark.parametrize("value, expected", [
    ("https://hyperchat.space", "https://hyperchat.space"),
    ("https://HyperChat.Space/", "https://hyperchat.space"),
    ("https://preview.example.test:443", "https://preview.example.test"),
    ("https://preview.example.test:8443", "https://preview.example.test:8443"),
    ("http://localhost:5173", "http://localhost:5173"),
    ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
    ("http://[::1]:5173", "http://[::1]:5173"),
])
def test_normalize_origin(value, expected):
    assert normalize_origin(value) == expected


@pytest.mark.parametrize("value", [
    "", "*", "https://*.lovable.app", "hyperchat.space", "ftp://hyperchat.space", "https://hyperchat.space/app",
    "https://hyperchat.space?x=1", "https://user:pw@hyperchat.space", "http://hyperchat.space",
    "http://192.168.1.10:5173", "https://hyperchat.space:99999",
])
def test_invalid_origins_rejected(value):
    with pytest.raises(OriginError):
        normalize_origin(value)


def test_parse_origins_list():
    assert parse_origins(" http://localhost:5173 , https://preview.example.test,http://localhost:5173 ") == (
        "http://localhost:5173", "https://preview.example.test")
    assert parse_origins("") == () and parse_origins(None) == ()


def test_browser_origins_default_is_the_frontend_only():
    assert settings().browser_origins == frozenset({"https://hyperchat.space"})
    assert settings(frontend_url="https://hyperchat.space/app/").browser_origins == frozenset({"https://hyperchat.space"})


def test_browser_origins_add_configured_extras():
    s = settings(allowed_origins="http://localhost:5173,https://www.hyperchat.space")
    assert s.browser_origins == frozenset({"https://hyperchat.space", "http://localhost:5173",
                                           "https://www.hyperchat.space"})


def test_invalid_configuration_fails_at_startup():
    with pytest.raises(ValueError):
        settings(allowed_origins="https://*.example.test")


@pytest.mark.parametrize("kw", [
    {"allowed_origins": "http://localhost:5173"},
    {"allowed_origins": "https://localhost:8443"},
    {"session_cookie_samesite": "none"},
    {"frontend_url": "http://localhost:5173"},
])
def test_production_refuses_development_settings(kw):
    with pytest.raises(ValueError):
        settings(app_env="production", db_label="PRODUCTION", **kw)


def test_production_accepts_https_origins_and_lax():
    s = settings(app_env="production", db_label="PRODUCTION", allowed_origins="https://www.hyperchat.space")
    assert s.session_cookie_samesite == "lax" and "https://www.hyperchat.space" in s.browser_origins


def test_development_origin_classification():
    assert is_development_origin("http://localhost:5173") and is_development_origin("https://127.0.0.1:8443")
    assert not is_development_origin("https://hyperchat.space")
