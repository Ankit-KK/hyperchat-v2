"""Security audit F10: password reset and password change racing each other. Deterministic: every interleaving is
forced with events (or a held row lock); no outcome depends on timing. Controlled @authtest.invalid accounts only."""

import asyncio
import threading
import time

import pytest

from app.auth.passwords import hash_password, verify_password
from app.auth.tokens import new_hex_token, sha256_hex
from tests.integration import DOMAIN, PASSWORD, db, make_user, requires_db

pytestmark = requires_db
IP = "203.0.113.120"
WAIT = 10  # seconds: an upper bound for the events, never a sleep


def stored_hash(email: str) -> str:
    with db() as conn:
        return conn.execute("SELECT password_hash FROM public.users WHERE email = %s", (email,)).fetchone()[0]


@pytest.fixture
def auth(env, monkeypatch):
    """The real AuthService on the scratch database, with hash_password gated: hashing `gated[0]` signals `reached`
    and waits for `release`, so a test can run another operation exactly between a hash and its database write."""
    import app.auth.service as service
    from app.auth.repository import AuthRepository
    from app.cli import connection_factory
    from app.core.config import Settings

    reached, release, gated = threading.Event(), threading.Event(), [None]
    real_hash = service.hash_password

    def gate(password):
        if password == gated[0]:
            reached.set()
            assert release.wait(WAIT), "the test never released the gated hash"
        return real_hash(password)
    monkeypatch.setattr(service, "hash_password", gate)
    settings = Settings()
    svc = service.AuthService.from_settings(settings, AuthRepository(connection_factory(settings)))
    svc.gate = (reached, release, gated)
    return svc


async def reset_link(auth, user_id: str) -> str:
    token = new_hex_token(32)
    await auth.repo.create_reset_token(user_id, sha256_hex(token), 60)
    return token


def test_a_reset_never_overwrites_a_password_change_that_finished_first(auth):
    """Reset hashed its new password; before it writes, the owner's change completes (and burns the link).
    The reset must be refused, and the owner's password must stay."""
    from app.auth.service import AuthError

    email = "race-a" + DOMAIN
    user_id = make_user(email)
    reached, release, gated = auth.gate
    gated[0] = "Reset-Pass-42"

    async def scenario():
        user = await auth.repo.user_by_email(email)
        session = (await auth.issue_session(user, IP, None)).session
        link = await reset_link(auth, user_id)
        reset = asyncio.create_task(auth.reset_password(link, "Reset-Pass-42", IP))
        assert await asyncio.to_thread(reached.wait, WAIT)  # the reset is between its hash and its write
        await auth.change_password(user, session, PASSWORD, "Owner-Pass-42", IP)  # completes first
        release.set()
        with pytest.raises(AuthError) as refused:
            await reset
        return refused.value
    refused = asyncio.run(scenario())
    assert (refused.status, refused.reason) == (400, "bad_reset_token")
    final = stored_hash(email)
    assert verify_password("Owner-Pass-42", final) and not verify_password("Reset-Pass-42", final)


def test_a_change_never_overwrites_a_reset_that_finished_first(auth):
    """The owner's change verified the current password and is hashing the new one; before it writes, a reset
    completes. The change must be refused (its "current" password is no longer current), the reset's password stays."""
    from app.auth.service import AuthError

    email = "race-b" + DOMAIN
    user_id = make_user(email)
    reached, release, gated = auth.gate
    gated[0] = "Owner-Pass-42"

    async def scenario():
        user = await auth.repo.user_by_email(email)
        session = (await auth.issue_session(user, IP, None)).session
        link = await reset_link(auth, user_id)
        change = asyncio.create_task(auth.change_password(user, session, PASSWORD, "Owner-Pass-42", IP))
        assert await asyncio.to_thread(reached.wait, WAIT)  # the change verified the old password, is hashing
        await auth.reset_password(link, "Reset-Pass-42", IP)  # completes first
        release.set()
        with pytest.raises(AuthError) as refused:
            await change
        return refused.value
    refused = asyncio.run(scenario())
    assert (refused.status, refused.reason) == (400, "bad_current_password")
    final = stored_hash(email)
    assert verify_password("Reset-Pass-42", final) and not verify_password("Owner-Pass-42", final)


def test_a_reset_waits_for_an_open_password_change_transaction_then_is_refused(auth):
    """A password change transaction holds the user's row (same SQL and lock order as change_password). A reset
    arriving now must block on that row lock; once the change commits, the reset finds its link burned."""
    from app.auth.service import AuthError

    email = "race-c" + DOMAIN
    user_id = make_user(email)
    owner_hash = hash_password("Owner-Pass-42")

    async def scenario():
        link = await reset_link(auth, user_id)
        change = db()  # one open transaction, like AuthRepository.change_password before its commit
        change.autocommit = False
        change.execute("UPDATE public.users SET password_hash = %s, password_changed_at = now() WHERE id = %s",
                       (owner_hash, user_id))
        change.execute("UPDATE public.password_reset_tokens SET used_at = now() WHERE user_id = %s AND used_at IS NULL",
                       (user_id,))
        reset = asyncio.create_task(auth.reset_password(link, "Reset-Pass-42", IP))

        def reset_is_waiting_for_the_lock() -> bool:
            deadline = time.monotonic() + WAIT
            with db() as probe:
                while time.monotonic() < deadline:
                    if probe.execute("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                                     "AND query LIKE '%%FROM public.users WHERE id = %%FOR UPDATE%%'").fetchone()[0]:
                        return True
                    time.sleep(0.01)  # poll interval of the probe only; the outcome is decided by the lock
            return False
        try:
            assert await asyncio.to_thread(reset_is_waiting_for_the_lock)
            await asyncio.to_thread(change.commit)
        finally:
            if not change.closed:
                await asyncio.to_thread(change.rollback)  # no-op after a commit; frees the locks on failure
                change.close()
        with pytest.raises(AuthError) as refused:
            await reset
        return refused.value
    refused = asyncio.run(scenario())
    assert (refused.status, refused.reason) == (400, "bad_reset_token")
    assert stored_hash(email) == owner_hash


def test_a_sign_in_rehash_never_writes_an_old_password_back_over_a_reset(auth):
    """A legacy bcrypt account signs in with its (old) password; while the sign-in re-hashes it to Argon2id, a
    reset completes. The sign-in must not write the old password back, and the old password must not sign in."""
    import bcrypt

    from app.auth.service import AuthError

    email = "race-d" + DOMAIN
    user_id = make_user(email, hashed=bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode())
    reached, release, gated = auth.gate
    gated[0] = PASSWORD  # the rehash of the old password

    async def scenario():
        link = await reset_link(auth, user_id)
        sign_in = asyncio.create_task(auth.login(email, PASSWORD, IP, None))
        assert await asyncio.to_thread(reached.wait, WAIT)  # verified the old password, now re-hashing it
        await auth.reset_password(link, "Reset-Pass-42", IP)  # completes first
        release.set()
        with pytest.raises(AuthError) as refused:
            await sign_in
        return refused.value
    refused = asyncio.run(scenario())
    assert refused.status == 401
    final = stored_hash(email)
    assert verify_password("Reset-Pass-42", final) and not verify_password(PASSWORD, final)


# ── a password sign-in's session versus a reset / change that completes around it ──────────────────────────────

def pause_after_sign_in(auth):
    """Pause a password sign-in right after its critical section (the account/IP rate-limit cleanup is its next
    step, in this code and in the previous one) until `go` is set; `paused` is set when it gets there."""
    paused, go = asyncio.Event(), asyncio.Event()
    real = auth.repo.clear_rate_limits

    async def gated(*args):
        paused.set()
        await go.wait()
        return await real(*args)
    auth.repo.clear_rate_limits = gated
    return paused, go


def live_sessions(user_id: str) -> int:
    with db() as conn:
        return conn.execute("SELECT count(*) FROM public.sessions WHERE user_id = %s AND revoked_at IS NULL",
                            (user_id,)).fetchone()[0]


def test_a_sign_in_session_never_survives_a_reset_that_completes_around_it(auth):
    """The sign-in verified the old password and got past its critical section; a reset completes before the
    sign-in finishes. Whatever the order, no session authenticated with the old password may stay valid."""
    from app.auth.service import AuthError

    email = "race-e" + DOMAIN
    user_id = make_user(email)

    async def scenario():
        link = await reset_link(auth, user_id)
        paused, go = pause_after_sign_in(auth)
        sign_in = asyncio.create_task(auth.login(email, PASSWORD, IP, None))
        await asyncio.wait_for(paused.wait(), WAIT)
        await auth.reset_password(link, "Reset-Pass-42", IP)  # completes while the sign-in is still running
        go.set()
        try:
            _, issued = await sign_in
        except AuthError:  # a refused sign-in is a valid outcome too
            return None
        return await auth.resolve(issued.token)
    assert asyncio.run(scenario()) is None  # the session (if one was issued) is no longer valid
    assert live_sessions(user_id) == 0
    assert verify_password("Reset-Pass-42", stored_hash(email))


def test_a_sign_in_session_never_survives_a_password_change_that_completes_around_it(auth):
    """The same against a password change made from the owner's existing session: the change keeps the owner's
    session and must leave no other session alive, including one the old-password sign-in created."""
    from app.auth.service import AuthError

    email = "race-f" + DOMAIN
    user_id = make_user(email)

    async def scenario():
        user = await auth.repo.user_by_email(email)
        owner = (await auth.issue_session(user, IP, None)).session
        paused, go = pause_after_sign_in(auth)
        sign_in = asyncio.create_task(auth.login(email, PASSWORD, IP, None))
        await asyncio.wait_for(paused.wait(), WAIT)
        await auth.change_password(user, owner, PASSWORD, "Owner-Pass-42", IP)
        go.set()
        try:
            _, issued = await sign_in
        except AuthError:  # a refused sign-in is a valid outcome too
            return owner.id, None
        return owner.id, await auth.resolve(issued.token)
    owner_id, stale = asyncio.run(scenario())
    assert stale is None
    with db() as conn:
        alive = [r[0] for r in conn.execute("SELECT id::text FROM public.sessions WHERE user_id = %s AND revoked_at IS NULL",
                                            (user_id,))]
    assert alive == [owner_id]  # only the session that made the change
    assert verify_password("Owner-Pass-42", stored_hash(email))


def test_a_sign_in_waits_for_an_open_reset_transaction_then_gets_no_session(auth):
    """A reset transaction holds the user's row (password updated, sessions revoked, not committed yet). A sign-in
    with the old password must block on that row lock and, once the reset commits, be refused without a session."""
    from app.auth.service import AuthError

    email = "race-g" + DOMAIN
    user_id = make_user(email)
    reset_hash = hash_password("Reset-Pass-42")

    async def scenario():
        reset = db()
        reset.autocommit = False
        reset.execute("SELECT 1 FROM public.users WHERE id = %s FOR UPDATE", (user_id,))
        reset.execute("UPDATE public.users SET password_hash = %s WHERE id = %s", (reset_hash, user_id))
        reset.execute("UPDATE public.sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                      (user_id,))
        sign_in = asyncio.create_task(auth.login(email, PASSWORD, IP, None))

        def sign_in_is_waiting_for_the_lock() -> bool:
            deadline = time.monotonic() + WAIT
            with db() as probe:
                while time.monotonic() < deadline:
                    if probe.execute("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                                     "AND query LIKE '%%password_hash, is_active FROM public.users%%'").fetchone()[0]:
                        return True
                    time.sleep(0.01)  # poll interval of the probe only; the outcome is decided by the lock
            return False
        try:
            assert await asyncio.to_thread(sign_in_is_waiting_for_the_lock)
            await asyncio.to_thread(reset.commit)
        finally:
            if not reset.closed:
                await asyncio.to_thread(reset.rollback)
                reset.close()
        with pytest.raises(AuthError) as refused:
            await sign_in
        return refused.value
    refused = asyncio.run(scenario())
    assert refused.status == 401
    assert live_sessions(user_id) == 0
