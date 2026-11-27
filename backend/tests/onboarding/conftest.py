"""Fixtures for the automatic invitation loop's database tests (synthetic rows only, removed by the env fixture)."""

import asyncio
import itertools

import pytest

from app.admin.applications import ApplicationRepository
from app.alerts import Alerts
from app.auth.email import EmailDeliveryError
from app.cli import connection_factory
from app.core.config import Settings
from app.onboarding.auto_invite import AutoInviter
from tests.integration import DOMAIN, FRONT, db, fixture_db, make_user

_n = itertools.count()


class FakeSender:
    """Records every email; the first `fail` sends raise a transient provider error (or always, with fail=-1)."""

    def __init__(self, fail: int = 0) -> None:
        self.sent, self.attempts, self.fail = [], 0, fail

    async def send(self, email) -> None:
        self.attempts += 1
        if self.fail < 0 or self.attempts <= self.fail:
            raise EmailDeliveryError("transient")
        self.sent.append(email)

    async def aclose(self) -> None:
        pass


def set_enforcement(state: str) -> None:
    """Test-only: flip step-up enforcement in this disposable database (as tests/stepup do), guard off."""
    with fixture_db() as conn, conn.transaction():
        conn.execute("ALTER TABLE public.stepup_enforcement DISABLE TRIGGER USER")
        conn.execute("UPDATE public.stepup_enforcement SET state = %s, changed_by = 'test'", (state,))
        conn.execute("ALTER TABLE public.stepup_enforcement ENABLE TRIGGER USER")


def age_events(signup_id: str, by: str = "13 hours") -> None:
    """Move an application's history into the past, so the backoff has elapsed (no sleeping)."""
    with fixture_db() as conn:
        conn.execute(f"UPDATE public.application_events SET created_at = created_at - interval '{by}' "
                     "WHERE signup_id = %s", (signup_id,))


def events(signup_id: str) -> list[tuple]:
    with db() as conn:
        return conn.execute("SELECT action, actor_user_id, note FROM public.application_events WHERE signup_id = %s "
                            "ORDER BY created_at, id", (signup_id,)).fetchall()


def invites(signup_id: str) -> list[tuple]:
    with db() as conn:
        return conn.execute("SELECT id::text, token_hash, invited_by, revoked_at IS NOT NULL, email::text "
                            "FROM public.user_invites WHERE signup_id = %s ORDER BY created_at, id",
                            (signup_id,)).fetchall()


@pytest.fixture
def repo(env) -> ApplicationRepository:
    return ApplicationRepository(connection_factory(Settings()), connection_factory(Settings(), role="lifecycle"))


@pytest.fixture
def approved(repo):
    """approved(local) -> (signup id, creator id, email): an application approved by an admin, creator pending."""
    admin = make_user(f"auto-boss{next(_n)}" + DOMAIN, role="admin")

    def make(local: str) -> tuple[str, str, str]:
        email = local + DOMAIN
        with db() as conn:
            sid = conn.execute("INSERT INTO public.user_signups (name, email, mobile_number) VALUES "
                               "('Auto', %s, '+91 90000 00000') RETURNING id::text", (email,)).fetchone()[0]
        slug = f"authtest-{local}"
        fields = {"streamer_slug": slug, "streamer_name": "Auto", "razorpay_account": "primary",
                  "custom_page_path": f"/{slug}"}
        result, creator = asyncio.run(repo.approve(sid, admin, fields, ()))
        assert result == "ok"
        return sid, creator["id"], email
    return make


def alerts(signup_id: str) -> list[tuple]:
    """The invite_failed alerts recorded for an application: (source, code, context)."""
    with db() as conn:
        return conn.execute("SELECT source, message, context FROM public.system_alerts WHERE context->>'kind' = "
                            "'invite_failed' AND context->>'signup_id' = %s ORDER BY created_at",
                            (signup_id,)).fetchall()


@pytest.fixture
def inviter(repo, strict_scratch_db):
    """Alert rows carry no synthetic marker: the table is emptied around the test, as tests/alerts does."""
    with db() as conn:
        conn.execute("DELETE FROM public.system_alerts")
    store = Alerts(connection_factory(Settings()))

    def make(sender, **kw) -> AutoInviter:
        return AutoInviter(repo, sender, store, frontend_url=FRONT, ttl_days=7, poll_seconds=60, **kw)
    yield make
    with db() as conn:
        conn.execute("DELETE FROM public.system_alerts")
