"""Migration 0024 follow-up: an admin invitation and bootstrap-admin are insert-only. An account with the address that
another transaction inserts while they run (it commits after their existence check could have looked) refuses them
atomically: nothing is consumed, promoted or given a password. If that insert rolls back, they create the new admin.
Deterministic: the competing insert is held open in its own transaction; lock probes only observe."""

import asyncio

import pytest

from app.auth.tokens import new_hex_token, sha256_hex
from tests.auth.test_invite_races import IP, auth, consumed, finish, held, user_state, waiting_on_lock  # noqa: F401
from tests.integration import DOMAIN, db, requires_db

pytestmark = requires_db
PREPARED = "INSERT INTO public.users (email, role) VALUES (%s, 'user')"  # password-less, as hyperchat_app may


async def admin_invite(auth, email: str) -> str:
    token = new_hex_token(32)
    await auth.repo.create_invite(email, "admin", None, sha256_hex(token), auth.admin_id, 7)
    return token


def account(email: str) -> tuple | None:
    with db() as conn:
        return conn.execute("SELECT role, password_hash FROM public.users WHERE email = %s", (email,)).fetchone()


@pytest.mark.parametrize("competitor_commits", [True, False])
def test_an_admin_invite_racing_an_insert_of_its_address(auth, competitor_commits):
    from app.auth.service import AuthError

    email = f"ins-race-{int(competitor_commits)}" + DOMAIN

    async def scenario():
        token = await admin_invite(auth, email)
        competitor = held([(PREPARED, (email,))])
        try:
            accepting = asyncio.create_task(auth.accept_invite(token, "Admin-Pass-42", None, IP, None))
            assert await asyncio.to_thread(waiting_on_lock, "INSERT INTO public.users")
            await finish(competitor, commit=competitor_commits)
        finally:
            await finish(competitor, commit=False)
        try:
            return token, await accepting
        except AuthError as exc:
            return token, exc

    token, outcome = asyncio.run(scenario())
    if competitor_commits:
        assert isinstance(outcome, AuthError) and (outcome.status, outcome.reason) == (409, "account_exists")
        assert not consumed(token)  # still usable once the address is free again
        assert account(email) == ("user", None) and user_state(email)[2] == 0
    else:
        user, _ = outcome
        assert user.role == "admin" and consumed(token)
        assert account(email)[0] == "admin"


@pytest.mark.parametrize("competitor_commits", [True, False])
def test_bootstrap_racing_an_insert_of_its_address(env, competitor_commits):
    from app.admin.repository import AdminRepository, BootstrapRefused
    from app.auth.passwords import hash_password
    from app.cli import connection_factory
    from app.core.config import Settings
    from tests.auth.test_admin_auth_boundary_db import no_sign_in_admins

    no_sign_in_admins()
    settings = Settings()
    repo = AdminRepository(connection_factory(settings), auth_factory=connection_factory(settings, "auth"))
    email = f"boot-race-{int(competitor_commits)}" + DOMAIN
    password_hash = hash_password("Bootstrap-Pass-42")

    async def scenario():
        competitor = held([(PREPARED, (email,))])
        try:
            booting = asyncio.create_task(repo.bootstrap_admin(email, password_hash))
            assert await asyncio.to_thread(waiting_on_lock, "INSERT INTO public.users")
            await finish(competitor, commit=competitor_commits)
        finally:
            await finish(competitor, commit=False)
        try:
            return await booting
        except BootstrapRefused as exc:
            return exc

    outcome = asyncio.run(scenario())
    if competitor_commits:
        assert isinstance(outcome, BootstrapRefused) and outcome.reason == "account_exists"
        assert account(email) == ("user", None)
    else:
        assert account(email)[0] == "admin"
        no_sign_in_admins()  # leave no admin behind for the other bootstrap tests
