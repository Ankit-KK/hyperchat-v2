"""Alert message format, the channel setting and the worker's delivery wiring (no database)."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError

from app.alerts import discord_message
from app.core.config import Settings
from app.worker import build_notifier

CHANNEL = "123456789012345678"


def test_message_carries_codes_and_ids_and_never_pings():
    aid = str(uuid.uuid4())
    msg = discord_message({"id": aid, "kind": "job_failed", "source": "worker", "severity": "error",
                           "code": "provider_auth", "created_at": datetime(2026, 11, 25, 10, tzinfo=UTC),
                           "context": {"job_id": "j1", "delivery_attempts": 2, "delivery_error": "discord_timeout"}},
                          "development")
    assert msg["allowed_mentions"] == {"parse": []}
    assert msg["content"] == (f"HyperChat development alert: job_failed (worker, error) code=provider_auth job_id=j1 "
                              f"at=2026-11-25T10:00:00+00:00 id={aid}")


def test_channel_setting_is_validated_and_unset_by_default(monkeypatch):
    for key in ("HYPERCHAT_ALERT_DISCORD_CHANNEL_ID", "HYPERCHAT_ALERT_POLL_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=None)
    assert s.alert_discord_channel_id is None and s.alert_poll_seconds == 30
    assert Settings(_env_file=None, alert_discord_channel_id=f" {CHANNEL} ").alert_discord_channel_id == CHANNEL
    assert Settings(_env_file=None, alert_discord_channel_id=" ").alert_discord_channel_id is None
    for bad in ("12", "#ops", "1234567890123456789012"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, alert_discord_channel_id=bad)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, alert_poll_seconds=1)


def settings(channel=CHANNEL, token="TEST-ONLY-token", recipients=frozenset({CHANNEL})):
    return SimpleNamespace(alert_discord_channel_id=channel, discord_bot_token=SecretStr(token) if token else None,
                           bot_recipients=recipients, app_env="development", alert_poll_seconds=30.0)


@pytest.mark.parametrize("cfg, enabled", [
    (settings(), True),
    (settings(recipients=None), True),                # production: no allow-list
    (settings(channel=None), False),                  # default: record only
    (settings(token=None), False),                    # no bot token
    (settings(recipients=frozenset()), False),        # outside production a channel must be allow-listed (D13)
])
def test_delivery_needs_a_channel_a_token_and_the_allow_list(cfg, enabled):
    assert build_notifier(cfg, connection_factory=None).enabled is enabled
