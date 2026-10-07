"""Phase 9 units: channel names, Pusher signing (Pusher's documented examples), the REST client's error
classification (mocked transport: no real Pusher, no credentials) and the settings."""

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.jobs.retry import Permanent, Retryable
from app.realtime.channels import channel_name, parse_private, valid_socket_id
from app.realtime.pusher import MAX_DATA_BYTES, PusherApp, PusherApps, PusherClient, channel_auth, event_body, signed_query

# Pusher's worked examples (https://pusher.com/docs/channels/library_auth_reference/): public test values.
DOC_APP = PusherApp(1, "3", "278d425bdf160c739803", "7ad3773142a6692b25b8", "mt1")
SECRET = "test-only-secret-0123456789"
APP = PusherApp(1, "123456", "testkey0123", SECRET, "ap2")


# ── channels ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, parsed", [
    ("private-ankit-dashboard", ("ankit", "dashboard")),
    ("private-ankit-alerts", ("ankit", "alerts")),
    ("private-chiaa_gaming-leaderboard", ("chiaa_gaming", "leaderboard")),
    ("private-a-b-c-settings", ("a-b-c", "settings")),
    ("private-foo-alerts-dashboard", ("foo-alerts", "dashboard")),  # suffix anchored at the end
    ("private-ankit-audio", ("ankit", "audio")),
])
def test_private_channels_parse(name, parsed):
    assert parse_private(name) == parsed


@pytest.mark.parametrize("name", [
    "ankit-goal", "private-ankit-goal", "presence-ankit-dashboard", "private-encrypted-ankit-dashboard",
    "private-Ankit-dashboard", "private--dashboard", "private-ankit-dashboard ", "private-ankit-other",
    "private-ankit-dashboard\n", "private-" + "a" * 65 + "-alerts", "private-an%kit-alerts", "", None, 5,
    "private-ankit-dashboard,private-x-dashboard", "private-../x-alerts",
])
def test_other_channels_are_refused(name):
    assert parse_private(name) is None


def test_channel_names():
    assert channel_name("ankit", "dashboard") == "private-ankit-dashboard"
    assert channel_name("ankit", "goal") == "ankit-goal"  # public: aggregates only
    with pytest.raises(ValueError):
        channel_name("ankit", "presence")


@pytest.mark.parametrize("value, ok", [("1234.1234", True), ("123456789.987654321", True), ("1234", False),
                                       ("1234.", False), ("a.b", False), ("1.2.3", False), (" 1.2", False),
                                       ("1" * 21 + ".1", False), (None, False)])
def test_socket_ids(value, ok):
    assert valid_socket_id(value) is ok


# ── signing ─────────────────────────────────────────────────────────────────

def test_rest_signature_matches_pushers_documented_example():
    body = b'{"name":"foo","channels":["project-3"],"data":"{\\"some\\":\\"data\\"}"}'
    q = signed_query(DOC_APP, "POST", "/apps/3/events", body, 1353088179)
    assert q["body_md5"] == "ec365a775a4cd0599faeb73354201b6f"
    assert q["auth_signature"] == "da454824c97ba181a32ccc17a72625ba02771f50b50e1e7430e47a1f3f457e6c"
    assert q["auth_key"] == DOC_APP.key and q["auth_version"] == "1.0"


def test_channel_auth_matches_pushers_documented_example():
    assert channel_auth(DOC_APP, "1234.1234", "private-foobar") == \
        "278d425bdf160c739803:58df8b0c36d6982b82c3ecf6b4662e34fe8c25bba48f5369f135bf843651c3a4"


def test_signature_is_bound_to_socket_and_channel():
    a = channel_auth(APP, "1.1", "private-a-alerts")
    assert a != channel_auth(APP, "1.2", "private-a-alerts") != channel_auth(APP, "1.1", "private-b-alerts")
    assert SECRET not in a and SECRET not in repr(APP)


def test_event_body_and_size_limit():
    body = json.loads(event_body("private-a-dashboard", "new-donation", {"x": "ü"}))
    assert body == {"name": "new-donation", "channels": ["private-a-dashboard"], "data": '{"x":"ü"}'}
    with pytest.raises(Permanent) as exc:
        event_body("c", "e", {"x": "a" * MAX_DATA_BYTES})
    assert exc.value.reason == "payload_too_large"


# ── the REST client (mocked transport) ──────────────────────────────────────

def publish(handler):
    client = PusherClient(httpx.MockTransport(handler), clock=lambda: 1_700_000_000)
    return asyncio.run(client.publish(APP, "private-a-dashboard", "new-donation", {"event_id": "e1", "v": 1}))


def test_publish_sends_a_signed_request():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"], seen["body"], seen["ct"] = request.url, request.content, request.headers["content-type"]
        return httpx.Response(200, json={})
    publish(handler)
    url = urlsplit(str(seen["url"]))
    assert url.scheme == "https" and url.hostname == "api-ap2.pusher.com" and url.path == "/apps/123456/events"
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q == signed_query(APP, "POST", "/apps/123456/events", seen["body"], 1_700_000_000)
    assert SECRET not in str(seen["url"]) and SECRET.encode() not in seen["body"] and seen["ct"] == "application/json"
    assert json.loads(json.loads(seen["body"])["data"]) == {"event_id": "e1", "v": 1}


@pytest.mark.parametrize("status, headers, kind, reason, retry_after", [
    (429, {"retry-after": "7"}, Retryable, "pusher_rate_limited", 7.0),
    (429, {}, Retryable, "pusher_rate_limited", None),
    (500, {}, Retryable, "pusher_server_error", None),
    (503, {"retry-after": "x"}, Retryable, "pusher_server_error", None),
    (400, {}, Permanent, "pusher_rejected", None),
    (401, {}, Permanent, "pusher_auth_failed", None),
    (403, {}, Permanent, "pusher_forbidden", None),
    (404, {}, Permanent, "pusher_rejected", None),
    (413, {}, Permanent, "payload_too_large", None),
])
def test_http_errors_are_classified(status, headers, kind, reason, retry_after):
    with pytest.raises(kind) as exc:
        publish(lambda request: httpx.Response(status, headers=headers, text="secret-bearing provider body"))
    assert exc.value.reason == reason and "provider body" not in str(exc.value)
    if kind is Retryable:
        assert exc.value.retry_after == retry_after


@pytest.mark.parametrize("error, reason", [(httpx.ReadTimeout("t"), "pusher_timeout"),
                                           (httpx.ConnectTimeout("t"), "pusher_timeout"),
                                           (httpx.ConnectError("c"), "pusher_network")])
def test_network_errors_are_retryable(error, reason):
    def handler(request):
        raise error
    with pytest.raises(Retryable) as exc:
        publish(handler)
    assert exc.value.reason == reason


def test_redirects_are_not_followed():
    with pytest.raises(Permanent):
        publish(lambda request: httpx.Response(302, headers={"location": "https://evil.example/"}))


# ── settings and apps ───────────────────────────────────────────────────────

def settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def group(n, app_id="123", key="abcDEF123", secret="s3cr3t-value", cluster="ap2"):
    return {f"pusher_app_id_{n}": app_id, f"pusher_key_{n}": key, f"pusher_secret_{n}": SecretStr(secret),
            f"pusher_cluster_{n}": cluster}


def test_realtime_is_off_by_default_and_no_group_is_configured():
    s = settings()
    assert s.realtime_enabled is False and s.pusher_groups_configured == [] and s.pusher_config_problems() == []
    assert PusherApps.from_settings(s).resolve(1) is None


@pytest.mark.parametrize("override", [{"cluster": "ap2.evil.com"}, {"cluster": "AP2"}, {"cluster": ""},
                                      {"app_id": "12a"}, {"key": "has space"}, {"secret": " "}])
def test_invalid_groups_are_unconfigured_and_reported_by_name(override):
    s = settings(**group(2, **override))
    assert s.pusher_group(2) is None and s.pusher_config_problems() == ["HYPERCHAT_PUSHER_*_2"]
    assert "s3cr3t" not in str(s.pusher_config_problems())


def test_unconfigured_groups_use_group_one():
    apps = PusherApps.from_settings(settings(**group(1, app_id="1"), **group(3, app_id="3")))
    assert apps.groups == [1, 3]
    assert apps.resolve(3).app_id == "3" and apps.resolve(2).app_id == "1" and apps.resolve(7).app_id == "1"
    assert apps.resolve(None).app_id == "1"
    only_two = PusherApps.from_settings(settings(**group(2, app_id="2")))
    assert only_two.resolve(2).app_id == "2" and only_two.resolve(1) is None and only_two.resolve(4) is None


def test_publish_rate_bounds():
    with pytest.raises(ValueError):
        settings(realtime_publish_rate=0)
    assert settings(realtime_publish_rate=10).realtime_publish_rate == 10
