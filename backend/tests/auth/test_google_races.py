"""Security audit F10 follow-up: Google sign-in racing a password reset or a deactivation.

Contract: a Google sign-in's account check, identity link, sign-in record and session insert are ONE transaction under
the user-row lock, which every reset, password change and deactivation takes first, so the two are serialized:
  * sign-in first: the reset (or deactivation) that follows revokes its session;
  * reset first: the sign-in is a later, independent authentication (Google never depended on the password) and its
    session stays; every session that existed when the reset committed is revoked;
  * deactivation first: the sign-in is refused with the usual 403 and writes nothing (no session, no link, no record).
Deterministic: interleavings are forced with events or a held row lock; nothing depends on timing. Synthetic
@authtest.invalid accounts and the fake Google provider only.
"""

import asyncio
import time

import psycopg
import pytest

from app.auth.google import GoogleIdentity
from app.auth.tokens import new_hex_token, sha256_hex
from tests.integration import DOMAIN, FakeGoogle, db, make_user, requires_db

pytestmark = requires_db
IP = "203.0.113.130"
WAIT = 10  # seconds: an upper bound for events and lock probes, never a sleep that decides an outcome


@pytest.fixture
def auth(env):
    from app.auth.repository import AuthRepository
    from app.auth.service import AuthService
    from app.cli import connection_factory
    from app.core.config import Settings

    settings = Settings()
    svc = AuthService.from_settings(settings, AuthRepository(connection_factory(settings)))
    svc.google = FakeGoogle()
    return svc


def google_as(auth, email: str, subject: str) -> None:
    auth.google.identity = GoogleIdentity(subject, email, True)


async def sign_in(auth):
    return await auth.google_login("code", "verifier", "nonce", IP, None)


async def reset_link(auth, user_id: str) -> str:
    token = new_hex_token(32)
    await auth.repo.create_reset_token(user_id, sha256_hex(token), 60)
    return token


def state(user_id: str) -> tuple[int, int, object]:
    """(live sessions, Google identities, last_login_at) of the user."""
    with db() as conn:
        live = conn.execute("SELECT count(*) FROM public.sessions WHERE user_id = %s AND revoked_at IS NULL",
                            (user_id,)).fetchone()[0]
        links = conn.execute("SELECT count(*) FROM public.user_identities WHERE user_id = %s", (user_id,)).fetchone()[0]
        last = conn.execute("SELECT last_login_at FROM public.users WHERE id = %s", (user_id,)).fetchone()[0]
    return live, links, last


def pause_after_lookup(auth):
    """Pause a Google sign-in right after it found the account by email (its early is_active check is next, on the
    row as fetched) until `go` is set; `paused` is set when it gets there."""
    paused, go = asyncio.Event(), asyncio.Event()
    real = auth.repo.user_by_email

    async def gated(*args):
        user = await real(*args)
        paused.set()
        await go.wait()
        return user
    auth.repo.user_by_email = gated
    return paused, go


def waiting_on_lock(fragment: str) -> bool:
    """True once a backend whose query contains `fragment` waits on a lock (probe only; locks decide outcomes)."""
    deadline = time.monotonic() + WAIT
    with db() as probe:
        while time.monotonic() < deadline:
            if probe.execute("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query LIKE %s "
                             "AND pid <> pg_backend_pid()", (f"%{fragment}%",)).fetchone()[0]:
                return True
            time.sleep(0.01)
    return False


def held(sql_steps: list[tuple[str, tuple]]) -> psycopg.Connection:
    conn = db()
    conn.autocommit = False
    for sql, params in sql_steps:
        conn.execute(sql, params)
    return conn


async def finish(conn: psycopg.Connection, commit: bool) -> None:
    if not conn.closed:
        await asyncio.to_thread(conn.commit if commit else conn.rollback)
        conn.close()


# ── order: Google sign-in first ─────────────────────────────────────────────


def test_a_google_session_issued_before_a_reset_is_revoked_by_it(auth):
    email = "g-race-a" + DOMAIN
    user_id = make_user(email)
    google_as(auth, email, "g-sub-a")

    async def scenario():
        _, issued = await sign_in(auth)
        assert await auth.resolve(issued.token) is not None
        await auth.reset_password(await reset_link(auth, user_id), "Reset-Pass-42", IP)
        return await auth.resolve(issued.token)
    assert asyncio.run(scenario()) is None
    assert state(user_id)[0] == 0


def test_a_reset_arriving_while_google_holds_the_row_waits_then_revokes_the_new_session(auth):
    """The sign-in is inside its transaction (user row locked, identity insert blocked by a held conflicting row); a
    reset arriving now must wait for that lock, then revoke the session the sign-in committed."""
    email = "g-race-b" + DOMAIN
    user_id, other = make_user(email), make_user("g-race-b-other" + DOMAIN)
    google_as(auth, email, "g-sub-b")

    async def scenario():
        link = await reset_link(auth, user_id)
        # An open transaction holding the same Google subject: the sign-in's identity insert waits on it while the
        # sign-in already holds the user-row lock.
        blocker = held([("INSERT INTO public.user_identities (user_id, provider, provider_subject) "
                         "VALUES (%s, 'google', 'g-sub-b')", (other,))])
        try:
            google = asyncio.create_task(sign_in(auth))
            assert await asyncio.to_thread(waiting_on_lock, "INSERT INTO public.user_identities")
            reset = asyncio.create_task(auth.reset_password(link, "Reset-Pass-42", IP))
            assert await asyncio.to_thread(waiting_on_lock, "SELECT is_active FROM public.users WHERE id")
            await finish(blocker, commit=False)
        finally:
            await finish(blocker, commit=False)
        _, issued = await google
        await reset
        return await auth.resolve(issued.token)
    assert asyncio.run(scenario()) is None  # signed in first, so the reset revoked it
    live, links, _ = state(user_id)
    assert live == 0 and links == 1


# ── order: reset first ──────────────────────────────────────────────────────


def test_a_reset_completing_during_a_google_sign_in_revokes_older_sessions_only(auth):
    """The sign-in found the account, then a reset completes, then the sign-in finishes. The reset revoked every
    session that existed when it committed; the Google session is issued after it and stays (the contract)."""
    email = "g-race-c" + DOMAIN
    user_id = make_user(email)
    google_as(auth, email, "g-sub-c")

    async def scenario():
        _, earlier = await sign_in(auth)
        paused, go = pause_after_lookup(auth)
        auth.google.identity = GoogleIdentity("g-sub-c2", email, True)  # not linked yet: looked up by email
        google = asyncio.create_task(sign_in(auth))
        await asyncio.wait_for(paused.wait(), WAIT)
        await auth.reset_password(await reset_link(auth, user_id), "Reset-Pass-42", IP)
        go.set()
        _, later = await google
        return await auth.resolve(earlier.token), await auth.resolve(later.token)
    earlier, later = asyncio.run(scenario())
    assert earlier is None and later is not None
    assert state(user_id)[0] == 1


def test_a_google_sign_in_waits_for_an_open_reset_transaction_then_gets_a_later_session(auth):
    """A reset transaction holds the user row (password set, sessions revoked, uncommitted). The sign-in must wait
    for that lock; after the commit its session is issued after the reset and is not touched by it."""
    email = "g-race-d" + DOMAIN
    user_id = make_user(email)
    google_as(auth, email, "g-sub-d")

    async def scenario():
        _, earlier = await sign_in(auth)
        reset = held([("SELECT 1 FROM public.users WHERE id = %s FOR UPDATE", (user_id,)),
                      ("UPDATE public.users SET password_changed_at = now() WHERE id = %s", (user_id,)),
                      ("UPDATE public.sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                       (user_id,))])
        try:
            google = asyncio.create_task(sign_in(auth))
            assert await asyncio.to_thread(waiting_on_lock, "public.user")
            await finish(reset, commit=True)
        finally:
            await finish(reset, commit=False)
        _, later = await google
        return await auth.resolve(earlier.token), await auth.resolve(later.token)
    earlier, later = asyncio.run(scenario())
    assert earlier is None and later is not None


# ── deactivation (the case the old code got wrong) ──────────────────────────


def test_a_deactivation_completing_during_a_google_sign_in_leaves_nothing_behind(auth):
    """The sign-in passed its early is_active check, then an admin deactivates the account (real code path). The
    sign-in must be refused with the usual 403 and write nothing: no session, no identity link, no sign-in record."""
    from app.admin.repository import AdminRepository
    from app.auth.service import AuthError
    from app.cli import connection_factory
    from app.core.config import Settings

    email = "g-race-e" + DOMAIN
    user_id = make_user(email)
    google_as(auth, email, "g-sub-e")
    admin_repo = AdminRepository(connection_factory(Settings()))

    async def scenario():
        paused, go = pause_after_lookup(auth)
        google = asyncio.create_task(sign_in(auth))
        await asyncio.wait_for(paused.wait(), WAIT)  # found the account while it was still active
        await admin_repo.update_user(user_id, {"is_active": False})  # commits before the sign-in continues
        go.set()  # the early check sees the account as fetched (active); the locked check must catch it
        with pytest.raises(AuthError) as refused:
            await google
        return refused.value
    refused = asyncio.run(scenario())
    assert (refused.status, refused.reason) == (403, "account_disabled")
    assert state(user_id) == (0, 0, None)


def test_a_google_sign_in_waits_for_an_open_deactivation_then_is_refused_without_writes(auth):
    """A deactivation transaction holds the user row (is_active false, sessions revoked, uncommitted) after the
    sign-in's early check passed. Once it commits, the sign-in is refused and nothing was written."""
    from app.auth.service import AuthError

    email = "g-race-f" + DOMAIN
    user_id = make_user(email)
    google_as(auth, email, "g-sub-f")

    async def scenario():
        paused, go = pause_after_lookup(auth)
        google = asyncio.create_task(sign_in(auth))
        await asyncio.wait_for(paused.wait(), WAIT)  # the account was found active; the check runs next
        deactivate = held([("SELECT 1 FROM public.users WHERE id = %s FOR UPDATE", (user_id,)),
                           ("UPDATE public.users SET is_active = false WHERE id = %s", (user_id,)),
                           ("UPDATE public.sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                            (user_id,))])
        try:
            go.set()
            assert await asyncio.to_thread(waiting_on_lock, "public.user")
            await finish(deactivate, commit=True)
        finally:
            await finish(deactivate, commit=False)
        with pytest.raises(AuthError) as refused:
            await google
        return refused.value
    refused = asyncio.run(scenario())
    assert (refused.status, refused.reason) == (403, "account_disabled")
    assert state(user_id) == (0, 0, None)


# ── error path ──────────────────────────────────────────────────────────────


def test_a_failure_while_creating_the_session_rolls_back_the_link_and_the_record(auth, monkeypatch):
    """If the session insert fails, the identity link and the sign-in record must not be left behind."""
    email = "g-race-g" + DOMAIN
    user_id = make_user(email)
    google_as(auth, email, "g-sub-g")

    async def scenario():
        user = await auth.repo.user_by_email(email)
        existing = (await auth.issue_session(user, IP, None)).session  # an invite-style session to collide with
        real = auth._new_session

        def colliding(*args):
            token, csrf, row = real(*args)
            return token, csrf, (existing.id, *row[1:])  # same primary key: the insert fails
        monkeypatch.setattr(auth, "_new_session", colliding)
        with pytest.raises(psycopg.errors.UniqueViolation):
            await sign_in(auth)
    asyncio.run(scenario())
    assert state(user_id) == (1, 0, None)  # only the earlier session; no link, no sign-in recorded
