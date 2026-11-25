"""Units of the automatic invitation loop (no database): the backoff schedule, the selection SQL's shape, the
failure codes and when the worker starts the loop at all."""

import pytest

from app.auth.email import EmailCategoryInactive, EmailDeliveryError, EmailRecipientNotAllowed
from app.core.config import Settings
from app.onboarding.auto_invite import DELAYS, MAX_ATTEMPTS, PICK, failure_code, retry_delay
from app.worker import build_auto_inviter


def test_backoff_schedule_is_the_owners():
    assert MAX_ATTEMPTS == 5  # Q4
    assert [retry_delay(n) for n in range(1, MAX_ATTEMPTS + 1)] == [60, 300, 1800, 7200, 43200]
    assert DELAYS == (60, 300, 1800, 7200, 43200)


def test_selection_locks_one_application_and_skips_rows_another_worker_holds():
    sql = " ".join(PICK.split())
    assert sql.endswith("LIMIT 1 FOR UPDATE OF a SKIP LOCKED")
    for rule in ("a.review_status = 'approved'", "s.user_id IS NULL", "i.expires_at > now()",
                 "e.note IS DISTINCT FROM 'auto'", "'auto:skipped:%%'", "'auto:paused:%%'", "'auto:failed:%%'",
                 "< %(max_attempts)s", "(%(delays)s::int[])[count(*)::int]", "a.reviewed_at >= %(since)s",
                 "a.reviewed_at >= now() - make_interval(days => %(window_days)s)"):
        assert rule in sql, rule
    assert "jobs" not in sql and "INSERT" not in sql and "UPDATE" not in sql.replace("FOR UPDATE", "")


@pytest.mark.parametrize(("exc", "code"), [
    (EmailDeliveryError("transient"), "transient"), (EmailDeliveryError("refused_auth"), "refused_auth"),
    (EmailCategoryInactive("onboarding"), "category_inactive"), (EmailRecipientNotAllowed(), "recipient_not_allowed"),
    (RuntimeError("provider said something with an address@example.com"), "send_error")])
def test_failure_codes_never_carry_provider_text(exc, code):
    assert failure_code(exc) == code


def _settings(monkeypatch, **env) -> Settings:
    for key in ("HYPERCHAT_AUTO_INVITE_ENABLED", "HYPERCHAT_EMAIL_BACKEND", "HYPERCHAT_EMAIL_ACTIVE_CATEGORIES"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_the_loop_is_off_by_default_and_never_starts_without_onboarding_email(monkeypatch):
    assert Settings(_env_file=None).auto_invite_enabled is False
    assert Settings(_env_file=None).auto_invite_poll_seconds == 60
    assert build_auto_inviter(_settings(monkeypatch), None) is None
    off = _settings(monkeypatch, HYPERCHAT_AUTO_INVITE_ENABLED="true", HYPERCHAT_EMAIL_BACKEND="disabled")
    assert build_auto_inviter(off, None) is None  # the disabled sender would "succeed" without sending


@pytest.mark.parametrize("value", ["0", "9", "3601"])
def test_the_poll_interval_is_bounded(monkeypatch, value):
    monkeypatch.setenv("HYPERCHAT_AUTO_INVITE_POLL_SECONDS", value)
    with pytest.raises(ValueError):
        Settings(_env_file=None)
