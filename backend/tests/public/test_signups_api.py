"""POST /api/v1/public/signups: creator signup interest (legacy SignupDialog -> public.user_signups)."""

from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.integration import DOMAIN, FRONT, ORIGIN, db, requires_db
from tests.uploads.test_uploads_api import FakeLimiter

S = "/api/v1/public/signups"
VALID = {"name": "  Asha  Rao ", "email": " Asha" + DOMAIN.upper() + " ", "mobile_number": "+91 (98765) 43210",
         "youtube_channel": " https://youtube.com/@asha ", "instagram_handle": ""}


def _row(email: str) -> dict | None:
    with db() as conn:
        cur = conn.execute("SELECT name, email, mobile_number, youtube_channel, instagram_handle "
                           "FROM public.user_signups WHERE lower(email) = lower(%s)", (email,))
        row = cur.fetchone()
        return dict(zip([c.name for c in cur.description], row)) if row else None


class FakeRepo:
    def __init__(self, fail=False) -> None:
        self.rows, self.fail = [], fail

    async def add(self, signup):
        if self.fail:
            raise psycopg.OperationalError("TEST-ONLY connection refused")
        self.rows.append(signup.model_dump())
        return True


@pytest.fixture
def offline(monkeypatch):
    """The app without a database: fake storage and rate limiter."""
    monkeypatch.setenv("HYPERCHAT_DB_ENABLED", "false")
    monkeypatch.setenv("HYPERCHAT_APP_ENV", "test")
    monkeypatch.setenv("HYPERCHAT_FRONTEND_URL", FRONT)
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app(), base_url="https://testserver") as c:
        c.app.state.signups, c.app.state.auth = FakeRepo(), SimpleNamespace(repo=FakeLimiter())
        yield c
    get_settings.cache_clear()


# ── without a database ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("headers", [{}, {"origin": "https://evil.invalid"}, {"origin": "null"}])
def test_untrusted_origin_is_refused_before_anything_else(offline, headers):
    r = offline.post(S, json={"junk": True}, headers=headers)  # refused before the body is validated
    assert (r.status_code, r.json()) == (403, {"error": "Request origin not allowed"})
    assert offline.app.state.signups.rows == [] and offline.app.state.auth.repo.calls == []


@pytest.mark.parametrize("change", [
    {"name": ""}, {"name": "   "}, {"name": "x" * 101}, {"name": "a\x00b"}, {"name": 5},
    {"email": "a@b"}, {"email": "no-at-sign.example"}, {"email": "a@b.c"}, {"email": "a b@example.com"},
    {"email": "x" * 250 + "@ex.com"}, {"email": None},
    {"mobile_number": "1234567"}, {"mobile_number": "1" * 21}, {"mobile_number": "98765abc43"},
    {"mobile_number": 9876543210},
    {"youtube_channel": "y" * 501}, {"instagram_handle": "i" * 101}, {"instagram_handle": "a\nb"},
    {"role": "admin"},
])
def test_invalid_input_is_refused(offline, change):
    r = offline.post(S, json={**VALID, **change}, headers=ORIGIN)
    assert r.status_code == 422, r.text
    assert offline.app.state.signups.rows == []


def test_missing_fields_are_refused(offline):
    for field in ("name", "email", "mobile_number"):
        body = {k: v for k, v in VALID.items() if k != field}
        assert offline.post(S, json=body, headers=ORIGIN).status_code == 422
    assert offline.app.state.signups.rows == []


def test_values_are_normalized(offline):
    r = offline.post(S, json=VALID, headers=ORIGIN)
    assert (r.status_code, r.json(), r.headers["cache-control"]) == (201, {"status": "received"}, "no-store")
    assert offline.app.state.signups.rows == [{
        "name": "Asha  Rao", "email": "asha" + DOMAIN, "mobile_number": "+91 (98765) 43210",
        "youtube_channel": "https://youtube.com/@asha", "instagram_handle": None}]


def test_rate_limit_is_per_ip(offline):
    limiter = offline.app.state.auth.repo
    codes = [offline.post(S, json=VALID, headers=ORIGIN).status_code for _ in range(6)]
    assert codes == [201] * 5 + [429]
    assert set(limiter.calls) == {("ip:testclient", "signup:create", 5, 3600)}
    assert len(offline.app.state.signups.rows) == 5


def test_database_failure_is_a_clean_503(offline):
    offline.app.state.signups = FakeRepo(fail=True)
    r = offline.post(S, json=VALID, headers=ORIGIN)
    assert (r.status_code, r.json()) == (503, {"error": "Signups are unavailable right now. Please try again later."})
    assert "TEST-ONLY" not in r.text

    async def broken(*args):
        raise psycopg.OperationalError("TEST-ONLY pool timeout")
    offline.app.state.signups = FakeRepo()
    offline.app.state.auth.repo.hit_rate_limit = broken
    assert offline.post(S, json=VALID, headers=ORIGIN).status_code == 503
    assert offline.app.state.signups.rows == []


def test_not_configured_is_503(offline):
    offline.app.state.signups = None
    assert offline.post(S, json=VALID, headers=ORIGIN).status_code == 503


# ── PostgreSQL ──────────────────────────────────────────────────────────────────

@requires_db
def test_signup_is_stored(app):
    r = app.post(S, json=VALID, headers=ORIGIN)
    assert (r.status_code, r.json()) == (201, {"status": "received"})
    assert _row("asha" + DOMAIN) == {"name": "Asha  Rao", "email": "asha" + DOMAIN,
                                     "mobile_number": "+91 (98765) 43210",
                                     "youtube_channel": "https://youtube.com/@asha", "instagram_handle": None}


@requires_db
def test_duplicates_get_the_same_answer_and_store_nothing(app):
    with db() as conn:  # a legacy row with mixed case
        conn.execute("INSERT INTO public.user_signups (name, email, mobile_number) VALUES ('Old', %s, '12345678')",
                     ("Old.Signup" + DOMAIN,))
    legacy_dup = app.post(S, json={**VALID, "email": "old.signup" + DOMAIN}, headers=ORIGIN)
    new = app.post(S, json=VALID, headers=ORIGIN)
    repeat = app.post(S, json={**VALID, "name": "Other", "email": "ASHA" + DOMAIN}, headers=ORIGIN)
    assert [(r.status_code, r.json()) for r in (legacy_dup, new, repeat)] == [(201, {"status": "received"})] * 3
    with db() as conn:
        rows = conn.execute("SELECT name FROM public.user_signups WHERE lower(email) IN (%s, %s) ORDER BY name",
                            ("old.signup" + DOMAIN, "asha" + DOMAIN)).fetchall()
    assert rows == [("Asha  Rao",), ("Old",)]  # the first submission of each email is kept, nothing overwritten


@requires_db
def test_rate_limit_and_origin_with_the_real_limiter(app):
    assert app.post(S, json=VALID, headers={"origin": "https://evil.invalid"}).status_code == 403
    codes = [app.post(S, json={**VALID, "email": f"rl{i}" + DOMAIN}, headers=ORIGIN).status_code for i in range(6)]
    assert codes == [201] * 5 + [429]
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.user_signups WHERE email LIKE %s",
                            ("rl%" + DOMAIN,)).fetchone()[0] == 5
    assert any("signup_rejected" in r and "rate_limited" in r for r in app.app_state.log_capture.records)
    assert not any("rl0" in r for r in app.app_state.log_capture.records)  # no email in the logs
