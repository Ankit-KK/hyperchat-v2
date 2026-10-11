"""Security audit F10 follow-up: invite acceptance racing a password reset, another acceptance, or a failure.

Contract: consuming the invite, creating (or giving a password to) the account, burning its reset links and inserting
its session are ONE transaction; the account row is locked from the upsert on, the lock every reset takes first:
  * acceptance first: a reset with a link from before the acceptance finds it burned (refused); a reset with a link
    requested afterwards revokes the invite's session;
  * reset first (password-less account): the account now has a password, so the invite is refused (409) and is not
    consumed;
  * two acceptances (same token, or two invites for one account): exactly one succeeds, the other changes nothing;
  * any failure: no consumed invite, no account or password, no session.
Deterministic: interleavings are forced with events or held row locks; nothing depends on timing. Synthetic
@authtest.invalid accounts; invites are created directly in the scratch database (no email, no real invite).
"""

import asyncio
import time

import psycopg
import pytest

from app.auth.passwords import verify_password
from app.auth.tokens import new_hex_token, sha256_hex
from tests.integration import DOMAIN, db, make_user, requires_db

pytestmark = requires_db
IP = "203.0.113.140"
WAIT = 10  # seconds: an upper bound for events and lock probes, never a sleep that decides an outcome
NEW = "Invite-Pass-42"


@pytest.fixture
def auth(env):
    from app.auth.repository import AuthRepository
    from app.auth.service import AuthService
    from app.cli import connection_factory
    from app.core.config import Settings

    settings = Settings()
    svc = AuthService.from_settings(settings, AuthRepository(connection_factory(settings)))
    svc.admin_id = make_user("inv-admin" + DOMAIN, role="admin")
    return svc


async def invite(auth, email: str) -> str:
    token = new_hex_token(32)
    await auth.repo.create_invite(email, "streamer", None, sha256_hex(token), auth.admin_id, 7)
    return token


async def accept(auth, token: str, password: str = NEW):
    return await auth.accept_invite(token, password, None, IP, None)


async def reset_link(auth, user_id: str) -> str:
    token = new_hex_token(32)
    await auth.repo.create_reset_token(user_id, sha256_hex(token), 60)
    return token


def user_state(email: str) -> tuple[str | None, str | None, int]:
    """(user id, password hash, live sessions) for the email; (None, None, 0) when no account exists."""
    with db() as conn:
        row = conn.execute("SELECT id::text, password_hash FROM public.users WHERE email = %s", (email,)).fetchone()
        if row is None:
            return None, None, 0
        live = conn.execute("SELECT count(*) FROM public.sessions WHERE user_id = %s AND revoked_at IS NULL",
                            (row[0],)).fetchone()[0]
    return row[0], row[1], live


def consumed(token: str) -> bool:
    with db() as conn:
        return conn.execute("SELECT accepted_at IS NOT NULL FROM public.user_invites WHERE token_hash = %s",
                            (sha256_hex(token),)).fetchone()[0]


def waiting_on_lock(fragment: str, count: int = 1) -> bool:
    """True once `count` backends whose query contains `fragment` wait on a lock (probe only; locks decide)."""
    deadline = time.monotonic() + WAIT
    with db() as probe:
        while time.monotonic() < deadline:
            if probe.execute("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query LIKE %s "
                             "AND pid <> pg_backend_pid()", (f"%{fragment}%",)).fetchone()[0] >= count:
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


# ── the proven race: a reset completing right after the acceptance ──────────


def test_a_reset_right_after_an_acceptance_revokes_the_invite_session(auth):
    """The acceptance has returned from the database (the old code committed here and created the session only
    afterwards); a reset requested now completes before the caller gets its session. The session must not survive."""
    email = "inv-race-a" + DOMAIN

    async def scenario():
        token = await invite(auth, email)
        paused, go = asyncio.Event(), asyncio.Event()
        real = auth.repo.accept_invite

        async def gated(*args):
            result = await real(*args)
            paused.set()
            await go.wait()
            return result
        auth.repo.accept_invite = gated
        accepting = asyncio.create_task(accept(auth, token))
        await asyncio.wait_for(paused.wait(), WAIT)
        user_id = user_state(email)[0]
        await auth.reset_password(await reset_link(auth, user_id), "Reset-Pass-42", IP)
        go.set()
        _, issued = await accepting
        return await auth.resolve(issued.token)
    assert asyncio.run(scenario()) is None
    _, password_hash, live = user_state(email)
    assert live == 0 and verify_password("Reset-Pass-42", password_hash)


def test_a_reset_after_a_completed_acceptance_revokes_its_session(auth):
    email = "inv-race-b" + DOMAIN

    async def scenario():
        user, issued = await accept(auth, await invite(auth, email))
        assert await auth.resolve(issued.token) is not None
        await auth.reset_password(await reset_link(auth, user.id), "Reset-Pass-42", IP)
        return await auth.resolve(issued.token)
    assert asyncio.run(scenario()) is None
    assert user_state(email)[2] == 0


# ── order: acceptance holds the account row ─────────────────────────────────


def test_a_reset_arriving_during_an_acceptance_waits_and_its_older_link_is_refused(auth):
    """A password-less (migrated) account with an open reset link accepts an invite. While the acceptance holds the
    account row (its link burn is blocked by a held link row), the reset arrives: it must wait for the row, then find
    its link burned. The invite's password and session stay."""
    from app.auth.service import AuthError

    email = "inv-race-c" + DOMAIN
    user_id = make_user(email, password=None)

    async def scenario():
        link = await reset_link(auth, user_id)
        token = await invite(auth, email)
        blocker = held([("SELECT 1 FROM public.password_reset_tokens WHERE user_id = %s FOR UPDATE", (user_id,))])
        try:
            accepting = asyncio.create_task(accept(auth, token))
            assert await asyncio.to_thread(waiting_on_lock, "UPDATE public.password_reset_tokens")
            reset = asyncio.create_task(auth.reset_password(link, "Reset-Pass-42", IP))
            assert await asyncio.to_thread(waiting_on_lock, "SELECT is_active FROM public.users WHERE id")
            await finish(blocker, commit=False)
        finally:
            await finish(blocker, commit=False)
        _, issued = await accepting
        with pytest.raises(AuthError) as refused:
            await reset
        return refused.value, await auth.resolve(issued.token)
    refused, session = asyncio.run(scenario())
    assert (refused.status, refused.reason) == (400, "bad_reset_token")
    assert session is not None
    _, password_hash, live = user_state(email)
    assert live == 1 and verify_password(NEW, password_hash)


# ── order: reset holds the account row ──────────────────────────────────────


def test_an_acceptance_waiting_on_an_open_reset_is_refused_and_not_consumed(auth):
    """A reset transaction holds a password-less account's row and sets its password. The acceptance must wait for
    the row; once the reset commits the account has a password, so the invite is refused (409) and stays unused."""
    from app.auth.passwords import hash_password
    from app.auth.service import AuthError

    email = "inv-race-d" + DOMAIN
    user_id = make_user(email, password=None)
    reset_hash = hash_password("Reset-Pass-42")

    async def scenario():
        token = await invite(auth, email)
        reset = held([("SELECT 1 FROM public.users WHERE id = %s FOR UPDATE", (user_id,)),
                      ("UPDATE public.users SET password_hash = %s WHERE id = %s", (reset_hash, user_id)),
                      ("UPDATE public.sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                       (user_id,))])
        try:
            accepting = asyncio.create_task(accept(auth, token))
            assert await asyncio.to_thread(waiting_on_lock, "INSERT INTO public.users")
            await finish(reset, commit=True)
        finally:
            await finish(reset, commit=False)
        with pytest.raises(AuthError) as refused:
            await accepting
        return token, refused.value
    token, refused = asyncio.run(scenario())
    assert (refused.status, refused.reason) == (409, "account_exists")
    assert not consumed(token)
    _, password_hash, live = user_state(email)
    assert live == 0 and password_hash == reset_hash


# ── concurrent acceptances ──────────────────────────────────────────────────


def test_the_same_invite_accepted_twice_at_once_succeeds_exactly_once(auth):
    from app.auth.service import AuthError

    email = "inv-race-e" + DOMAIN

    async def scenario():
        token = await invite(auth, email)
        lock = held([("SELECT 1 FROM public.user_invites WHERE token_hash = %s FOR UPDATE", (sha256_hex(token),))])
        try:
            first = asyncio.create_task(accept(auth, token, "First-Pass-42"))
            second = asyncio.create_task(accept(auth, token, "Second-Pass-42"))
            assert await asyncio.to_thread(waiting_on_lock, "UPDATE public.user_invites", 2)
            await finish(lock, commit=False)
        finally:
            await finish(lock, commit=False)
        return await asyncio.gather(first, second, return_exceptions=True)
    results = asyncio.run(scenario())
    ok = [r for r in results if not isinstance(r, BaseException)]
    failed = [r for r in results if isinstance(r, AuthError)]
    assert len(ok) == 1 and [(e.status, e.reason) for e in failed] == [(400, "bad_invite_token")]
    _, password_hash, live = user_state(email)
    assert live == 1 and verify_password("First-Pass-42" if ok[0] is results[0] else "Second-Pass-42", password_hash)


def test_two_invites_for_one_password_less_account_succeed_exactly_once(auth):
    from app.auth.service import AuthError

    email = "inv-race-f" + DOMAIN
    user_id = make_user(email, password=None)

    async def scenario():
        tokens = [await invite(auth, email), await invite(auth, email)]
        lock = held([("SELECT 1 FROM public.users WHERE id = %s FOR UPDATE", (user_id,))])
        try:
            tasks = [asyncio.create_task(accept(auth, t, f"Pass-{i}-Invite-42")) for i, t in enumerate(tokens)]
            assert await asyncio.to_thread(waiting_on_lock, "INSERT INTO public.users", 2)
            await finish(lock, commit=False)
        finally:
            await finish(lock, commit=False)
        return tokens, await asyncio.gather(*tasks, return_exceptions=True)
    tokens, results = asyncio.run(scenario())
    winners = [i for i, r in enumerate(results) if not isinstance(r, BaseException)]
    losers = [r for r in results if isinstance(r, AuthError)]
    assert len(winners) == 1 and [(e.status, e.reason) for e in losers] == [(409, "account_exists")]
    assert [consumed(t) for t in tokens] == [i in winners for i in range(2)]  # the refused invite stays unused
    _, password_hash, live = user_state(email)
    assert live == 1 and verify_password(f"Pass-{winners[0]}-Invite-42", password_hash)


# ── failure: nothing partially committed ────────────────────────────────────


@pytest.mark.parametrize("existing", [False, True], ids=["new-account", "password-less-account"])
def test_a_failing_session_insert_leaves_no_trace(auth, monkeypatch, existing):
    """If the session cannot be inserted, the invite stays unused and no account or password is created."""
    email = ("inv-race-g2" if existing else "inv-race-g1") + DOMAIN
    if existing:
        make_user(email, password=None)
    other = make_user("inv-race-g-other" + ("2" if existing else "1") + DOMAIN)

    async def scenario():
        token = await invite(auth, email)
        user = await auth.repo.user_by_id(other)
        collide = (await auth.issue_session(user, IP, None)).session.id  # an existing session id
        real = auth._new_session

        def colliding(*args):
            session_token, csrf, row = real(*args)
            return session_token, csrf, (collide, *row[1:])  # same primary key: the insert fails
        monkeypatch.setattr(auth, "_new_session", colliding)
        with pytest.raises(psycopg.errors.UniqueViolation):
            await accept(auth, token)
        return token
    token = asyncio.run(scenario())
    assert not consumed(token)
    user_id, password_hash, live = user_state(email)
    assert (user_id is not None) == existing and password_hash is None and live == 0


# ── deactivated accounts: refused under the row lock, nothing changed ───────


def account_snapshot(user_id: str) -> tuple:
    """(password_hash, is_active, open reset links, live sessions, password_changed_at, email_verified_at)."""
    with db() as conn:
        return conn.execute("""
            SELECT u.password_hash, u.is_active,
                   (SELECT count(*) FROM public.password_reset_tokens t WHERE t.user_id = u.id AND t.used_at IS NULL),
                   (SELECT count(*) FROM public.sessions s WHERE s.user_id = u.id AND s.revoked_at IS NULL),
                   u.password_changed_at, u.email_verified_at
            FROM public.users u WHERE u.id = %s""", (user_id,)).fetchone()


def test_an_invite_for_a_deactivated_password_less_account_is_refused_and_changes_nothing(auth):
    from app.auth.service import AuthError

    email = "inv-off-a" + DOMAIN
    user_id = make_user(email, password=None, is_active=False)

    async def scenario():
        await reset_link(auth, user_id)  # an open link must survive the refused acceptance untouched
        token = await invite(auth, email)
        before = account_snapshot(user_id)
        with pytest.raises(AuthError) as refused:
            await accept(auth, token)
        return token, before, refused.value
    token, before, refused = asyncio.run(scenario())
    assert (refused.status, refused.reason, refused.error) == (403, "account_disabled", "This account is disabled")
    assert not consumed(token)
    assert account_snapshot(user_id) == before == (None, False, 1, 0, None, None)


def test_an_invite_for_a_deactivated_account_with_a_password_still_answers_account_exists(auth):
    """Unchanged: an account that has a password answers 409 whether or not it is active."""
    from app.auth.service import AuthError

    email = "inv-off-b" + DOMAIN
    user_id = make_user(email, is_active=False)

    async def scenario():
        token = await invite(auth, email)
        with pytest.raises(AuthError) as refused:
            await accept(auth, token)
        return token, refused.value
    token, refused = asyncio.run(scenario())
    assert (refused.status, refused.reason) == (409, "account_exists")
    assert not consumed(token) and account_snapshot(user_id)[1] is False


def test_an_acceptance_waiting_on_an_open_deactivation_is_refused_and_changes_nothing(auth):
    """A deactivation transaction holds the password-less account's row (is_active false, links burned, sessions
    revoked; uncommitted). The acceptance must wait for the row; once the deactivation commits it is refused (403)
    and changes nothing: an earlier, unlocked status check would have let it through."""
    from app.auth.service import AuthError

    email = "inv-off-c" + DOMAIN
    user_id = make_user(email, password=None)

    async def scenario():
        token = await invite(auth, email)
        deactivate = held([
            ("SELECT 1 FROM public.users WHERE id = %s FOR UPDATE", (user_id,)),
            ("UPDATE public.users SET is_active = false WHERE id = %s", (user_id,)),
            ("UPDATE public.password_reset_tokens SET used_at = now() WHERE user_id = %s AND used_at IS NULL",
             (user_id,)),
            ("UPDATE public.sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user_id,)),
        ])
        try:
            accepting = asyncio.create_task(accept(auth, token))
            assert await asyncio.to_thread(waiting_on_lock, "INSERT INTO public.users")
            await finish(deactivate, commit=True)
        finally:
            await finish(deactivate, commit=False)
        with pytest.raises(AuthError) as refused:
            await accepting
        return token, refused.value
    token, refused = asyncio.run(scenario())
    assert (refused.status, refused.reason) == (403, "account_disabled")
    assert not consumed(token)
    assert account_snapshot(user_id)[:4] == (None, False, 0, 0)


def test_a_deactivation_arriving_during_an_acceptance_waits_then_revokes_its_session(auth):
    """The acceptance holds the account row (its link burn is blocked by a held link row) when an admin deactivates
    the account (real code path): the deactivation waits, then revokes the invite's session. Nothing survives."""
    from app.admin.repository import AdminRepository
    from app.cli import connection_factory
    from app.core.config import Settings

    email = "inv-off-d" + DOMAIN
    user_id = make_user(email, password=None)
    admin_repo = AdminRepository(connection_factory(Settings()))

    async def scenario():
        await reset_link(auth, user_id)
        token = await invite(auth, email)
        blocker = held([("SELECT 1 FROM public.password_reset_tokens WHERE user_id = %s FOR UPDATE", (user_id,))])
        try:
            accepting = asyncio.create_task(accept(auth, token))
            assert await asyncio.to_thread(waiting_on_lock, "UPDATE public.password_reset_tokens")
            deactivating = asyncio.create_task(admin_repo.update_user(user_id, {"is_active": False}))
            assert await asyncio.to_thread(waiting_on_lock, "AS can_sign_in FROM public.users u")
            await finish(blocker, commit=False)
        finally:
            await finish(blocker, commit=False)
        _, issued = await accepting
        _, changed, revoked = await deactivating
        return token, issued, changed, revoked, await auth.resolve(issued.token)
    token, issued, changed, revoked, session = asyncio.run(scenario())
    assert consumed(token) and changed == ["is_active"] and revoked == 1 and session is None
    password_hash, active, links, live = account_snapshot(user_id)[:4]
    assert verify_password(NEW, password_hash) and active is False and links == 0 and live == 0
