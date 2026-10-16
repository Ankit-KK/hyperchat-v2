"""Security audit F2: layered sign-in throttling replaces the account-wide lockout (app.auth.service.LOGIN_LIMITS).

Requests go through Uvicorn's real ProxyHeadersMiddleware (trusting 127.0.0.1, as the systemd units do) with this
TestClient as the local Nginx, so each request's client address comes from X-Forwarded-For exactly as Nginx appends
it ("<anything the client sent>, <real address>"). Controlled @authtest.invalid accounts only.
"""

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.auth.service import GENERIC_LOGIN_ERROR, LOGIN_LIMITS
from tests.integration import (
    DOMAIN,
    ORIGIN,
    PASSWORD,
    CaptureSender,
    FakeGoogle,
    LogCapture,
    db,
    make_user,
    requires_db,
)

pytestmark = requires_db
LIMIT = {bucket: (limit, window) for _, bucket, limit, window in LOGIN_LIMITS}
GENERIC = (401, {"error": GENERIC_LOGIN_ERROR})


@pytest.fixture
def nginx(env):
    """The app as `uvicorn --proxy-headers --forwarded-allow-ips 127.0.0.1` serves it; the client is Nginx."""
    from app.main import create_app

    application = create_app()
    capture = LogCapture()
    logging.getLogger().addHandler(capture)
    with TestClient(ProxyHeadersMiddleware(application, trusted_hosts="127.0.0.1"), base_url="https://testserver",
                    client=("127.0.0.1", 50000)) as client:
        application.state.auth.sender, application.state.auth.google = CaptureSender(), FakeGoogle()
        client.records = capture.records
        yield client
    logging.getLogger().removeHandler(capture)


def attempt(client, email, password, ip, forged=None):
    """One sign-in from client address `ip`; `forged` = an X-Forwarded-For value the client itself sent."""
    xff = f"{forged}, {ip}" if forged else ip
    client.cookies.clear()  # every attempt from a fresh "browser": sessions / cookies never matter
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password},
                    headers={**ORIGIN, "X-Forwarded-For": xff})
    return r.status_code, r.json()


def age(key_like: str, bucket: str, interval: str) -> None:
    with db() as conn:
        conn.execute(f"UPDATE public.rate_limits SET window_start = window_start - interval '{interval}' "
                     "WHERE ip_address LIKE %s AND endpoint = %s", (key_like, bucket))


def test_the_limits_are_the_documented_policy():
    assert LIMIT == {"auth:login": (20, 900), "auth:login-day": (100, 86400),
                     "auth:login-account": (5, 900), "auth:login-account-day": (15, 86400)}


def test_failures_from_one_ip_are_throttled_across_accounts(nginx):
    make_user("ipvictim" + DOMAIN)
    ip = "203.0.113.10"
    assert [attempt(nginx, f"spray{i}" + DOMAIN, "guess", ip) for i in range(20)] == [GENERIC] * 20
    assert attempt(nginx, "ipvictim" + DOMAIN, PASSWORD, ip) == GENERIC  # even the right password: throttled
    assert attempt(nginx, "ipvictim" + DOMAIN, PASSWORD, "203.0.113.11")[0] == 200  # another IP is unaffected
    assert any("login_rate_limited" in r and "auth:login" in r for r in nginx.records)


def test_failures_for_one_account_at_one_ip_are_throttled_but_never_lock_the_account(nginx):
    """The F2 vulnerability: 5 failures used to lock the account for everyone, and one more failure per 15 minutes
    kept it locked indefinitely. Now they only throttle that account at that IP."""
    make_user("target" + DOMAIN)
    attacker, owner = "198.51.100.66", "203.0.113.20"
    assert [attempt(nginx, "target" + DOMAIN, "guess", attacker) for _ in range(5)] == [GENERIC] * 5
    assert attempt(nginx, "target" + DOMAIN, PASSWORD, attacker) == GENERIC  # throttled at the attacker's IP
    for _ in range(3):  # the attacker keeps going, window after window
        age("acct:%:ip:" + attacker, "auth:login-account", "16 minutes")
        attempt(nginx, "target" + DOMAIN, "guess", attacker)
        assert attempt(nginx, "target" + DOMAIN, PASSWORD, owner)[0] == 200  # the owner always gets in
    with db() as conn:  # nothing is stored on the account itself
        assert conn.execute("SELECT failed_login_attempts, locked_until FROM public.users WHERE email = %s",
                            ("target" + DOMAIN,)).fetchone() == (0, None)


def test_email_is_normalized_for_the_account_ip_limit(nginx):
    make_user("norm" + DOMAIN)
    ip = "203.0.113.30"
    variants = ["norm" + DOMAIN, " NORM" + DOMAIN.upper(), "Norm" + DOMAIN + " ", "nOrM" + DOMAIN, "norm" + DOMAIN]
    assert [attempt(nginx, v, "guess", ip) for v in variants] == [GENERIC] * 5
    assert attempt(nginx, "NORM" + DOMAIN, PASSWORD, ip) == GENERIC  # one budget for every spelling


def test_spoofed_forwarded_for_does_not_bypass_the_ip_limit(nginx):
    """The client sends a different X-Forwarded-For each time; Nginx appends its real address, which is what counts."""
    make_user("spoofed" + DOMAIN)
    real = "203.0.113.40"
    results = [attempt(nginx, f"spoof{i}" + DOMAIN, "guess", real, forged=f"10.0.0.{i}, 127.0.0.1") for i in range(20)]
    assert results == [GENERIC] * 20
    assert attempt(nginx, "spoofed" + DOMAIN, PASSWORD, real, forged="192.0.2.1") == GENERIC
    with db() as conn:
        keys = {row[0] for row in conn.execute("SELECT ip_address FROM public.rate_limits WHERE endpoint = 'auth:login'")}
    assert f"ip:{real}" in keys and not any(k.startswith(("ip:10.0.0.", "ip:127.", "ip:192.0.2.")) for k in keys)


def test_untrusted_peer_cannot_choose_its_address(env):
    """A connection that is not the trusted proxy: its X-Forwarded-For is ignored, the peer address counts."""
    from app.main import create_app

    with TestClient(ProxyHeadersMiddleware(create_app(), trusted_hosts="127.0.0.1"), base_url="https://testserver",
                    client=("198.51.100.200", 50000)) as outsider:
        codes = [outsider.post("/api/v1/auth/login", json={"email": f"o{i}" + DOMAIN, "password": "x"},
                               headers={**ORIGIN, "X-Forwarded-For": f"10.1.0.{i}"}).status_code for i in range(21)]
    assert codes == [401] * 21
    with db() as conn:
        n = conn.execute("SELECT request_count FROM public.rate_limits WHERE ip_address = 'ip:198.51.100.200' "
                         "AND endpoint = 'auth:login'").fetchone()[0]
    assert n == 21


def test_every_failure_looks_the_same(nginx, monkeypatch):
    """Unknown account, wrong password, inactive account and throttled: same status, same body, one password
    verification each (the same hashing work), no cookie."""
    import app.auth.service as service

    calls = []
    real_verify = service.verify_password

    def counting(password, stored_hash):
        calls.append(stored_hash is not None)
        return real_verify(password, stored_hash)
    monkeypatch.setattr(service, "verify_password", counting)
    make_user("known" + DOMAIN)
    make_user("inactive" + DOMAIN, is_active=False)
    ip = "203.0.113.50"
    outcomes = [attempt(nginx, "nobody" + DOMAIN, "guess", ip), attempt(nginx, "known" + DOMAIN, "guess", ip),
                attempt(nginx, "inactive" + DOMAIN, PASSWORD, ip)]
    for _ in range(5):
        attempt(nginx, "known" + DOMAIN, "guess", "203.0.113.51")
    calls.clear()
    outcomes.append(attempt(nginx, "known" + DOMAIN, PASSWORD, "203.0.113.51"))  # throttled
    assert outcomes == [GENERIC] * 4 and len(calls) == 1
    nginx.cookies.clear()
    r = nginx.post("/api/v1/auth/login", json={"email": "nobody" + DOMAIN, "password": "x"},
                   headers={**ORIGIN, "X-Forwarded-For": ip})
    assert "set-cookie" not in r.headers


def test_sign_in_works_again_after_the_window_and_progressive_daily_limit(nginx):
    make_user("patient" + DOMAIN)
    ip = "203.0.113.60"
    for _ in range(3):  # 3 short windows of 5 failures = the daily allowance of 15
        assert [attempt(nginx, "patient" + DOMAIN, "guess", ip) for _ in range(5)] == [GENERIC] * 5
        age("acct:%:ip:" + ip, "auth:login-account", "16 minutes")
    assert attempt(nginx, "patient" + DOMAIN, PASSWORD, ip) == GENERIC  # the 24 h limit still holds
    age("acct:%:ip:" + ip, "auth:login-account-day", "25 hours")
    age("acct:%:ip:" + ip, "auth:login-account", "16 minutes")
    status, body = attempt(nginx, "patient" + DOMAIN, PASSWORD, ip)
    assert status == 200 and body["user"]["email"] == "patient" + DOMAIN


def test_a_successful_sign_in_clears_that_account_ip_budget(nginx):
    make_user("typo" + DOMAIN)
    ip = "203.0.113.70"
    assert [attempt(nginx, "typo" + DOMAIN, "guess", ip) for _ in range(4)] == [GENERIC] * 4
    assert attempt(nginx, "typo" + DOMAIN, PASSWORD, ip)[0] == 200
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.rate_limits WHERE ip_address LIKE %s",
                            ("acct:%:ip:" + ip,)).fetchone()[0] == 0
    assert [attempt(nginx, "typo" + DOMAIN, "guess", ip) for _ in range(4)] == [GENERIC] * 4  # a fresh budget
    assert attempt(nginx, "typo" + DOMAIN, PASSWORD, ip)[0] == 200


def test_concurrent_guesses_cannot_exceed_the_allowance(env, monkeypatch):
    """12 simultaneous wrong guesses for one account at one IP, each on its own database connection: exactly 5
    reach the password check (the atomic counters are taken before it); all 12 get the generic answer."""
    import app.auth.service as service
    from app.auth.repository import AuthRepository
    from app.cli import connection_factory
    from app.core.config import Settings

    real_checks = []
    real_verify = service.verify_password

    def counting(password, stored_hash):
        if stored_hash is not None:
            real_checks.append(1)
        return real_verify(password, stored_hash)
    monkeypatch.setattr(service, "verify_password", counting)
    make_user("racer2" + DOMAIN)
    settings = Settings()
    auth = service.AuthService.from_settings(settings, AuthRepository(connection_factory(settings), connection_factory(settings, "auth")))

    async def race():
        async def guess(i):
            try:
                await auth.login("racer2" + DOMAIN, f"wrong-{i}", "203.0.113.80", None)
            except service.AuthError as exc:
                return exc.status, exc.error
        return await asyncio.gather(*(guess(i) for i in range(12)))
    assert asyncio.run(race()) == [(401, GENERIC_LOGIN_ERROR)] * 12
    assert len(real_checks) == 5
    with db() as conn:
        counts = dict(conn.execute("SELECT endpoint, request_count FROM public.rate_limits WHERE ip_address LIKE %s",
                                   ("acct:%:ip:203.0.113.80",)).fetchall())
    assert counts == {"auth:login-account": 12, "auth:login-account-day": 12}  # none lost to the race


def test_rate_limit_rows_are_bounded_by_the_daily_cleanup(nginx, strict_scratch_db):
    from app.cli import connection_factory
    from app.core.config import Settings
    from app.maintenance import cleanup

    ip = "203.0.113.90"
    for i in range(3):
        attempt(nginx, f"bounded{i}" + DOMAIN, "guess", ip)
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.rate_limits WHERE ip_address = %s OR ip_address LIKE %s",
                            (f"ip:{ip}", f"acct:%:ip:{ip}")).fetchone()[0] == 8  # 2 IP + 2 x 3 account/IP rows
    for bucket in LIMIT:
        age(f"%{ip}", bucket, "25 hours")
    counts = asyncio.run(cleanup(connection_factory(Settings()), dry_run=False))
    assert counts["rate_limits"] >= 8
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.rate_limits WHERE ip_address LIKE %s",
                            (f"%{ip}",)).fetchone()[0] == 0


def test_security_logs_hold_no_password_email_or_token(nginx):
    make_user("logged" + DOMAIN)
    ip = "203.0.113.100"
    attempt(nginx, "logged" + DOMAIN, "Not-The-Password-77", ip)
    for _ in range(5):
        attempt(nginx, "logged" + DOMAIN, "Not-The-Password-77", "203.0.113.101")
    attempt(nginx, "logged" + DOMAIN, PASSWORD, "203.0.113.101")  # throttled
    status, body = attempt(nginx, "logged" + DOMAIN, PASSWORD, ip)
    assert status == 200
    text = "\n".join(nginx.records)
    for secret in (PASSWORD, "Not-The-Password-77", body["csrf_token"], "logged" + DOMAIN):
        assert secret not in text
    assert "login_rejected" in text and "login_rate_limited" in text and "login_succeeded" in text


# ── security audit F9: no hashing once the IP is over its limit; hashing off the event loop ─────────────────────

def counting_verify(monkeypatch):
    """Record every password verification (None = the dummy hash), still running the real one."""
    import app.auth.service as service

    calls = []
    real = service.verify_password

    def counting(password, stored_hash):
        calls.append(stored_hash)
        return real(password, stored_hash)
    monkeypatch.setattr(service, "verify_password", counting)
    return calls


def test_ip_throttled_attempts_never_hash(nginx, monkeypatch):
    calls = counting_verify(monkeypatch)
    ip = "203.0.113.110"
    results = [attempt(nginx, f"flood{i}" + DOMAIN, "guess", ip) for i in range(50)]
    assert results == [GENERIC] * 50  # the same answer before and after the limit
    assert len(calls) == 20  # only the 20 attempts within the IP limit were hashed
    with db() as conn:  # IP-throttled attempts do not touch the account buckets either
        rows = conn.execute("SELECT count(*) FROM public.rate_limits WHERE ip_address LIKE %s",
                            ("acct:%:ip:" + ip,)).fetchone()[0]
        ip_count = conn.execute("SELECT request_count FROM public.rate_limits WHERE ip_address = %s "
                                "AND endpoint = 'auth:login'", (f"ip:{ip}",)).fetchone()[0]
    assert rows == 2 * 20 and ip_count == 50  # every attempt is still counted against the IP


def test_account_throttled_attempts_still_cost_one_hash(nginx, monkeypatch):
    """Within the IP limit, account-specific throttling must stay indistinguishable from a real attempt."""
    make_user("costly" + DOMAIN)
    calls = counting_verify(monkeypatch)
    ip = "203.0.113.111"
    assert [attempt(nginx, "costly" + DOMAIN, "guess", ip) for _ in range(5)] == [GENERIC] * 5
    assert all(h is not None for h in calls) and len(calls) == 5
    calls.clear()
    assert [attempt(nginx, "costly" + DOMAIN, PASSWORD, ip) for _ in range(3)] == [GENERIC] * 3  # throttled
    assert calls == [None, None, None]  # one dummy verification each


def test_concurrent_attempts_cannot_get_past_the_ip_limit(env, monkeypatch):
    """30 simultaneous attempts from one IP (different accounts) through the app's own connection pool (10
    connections; the role allows 20): the atomic IP counter lets exactly 20 reach a password check."""
    import app.auth.service as service
    from app.auth.repository import AuthRepository
    from app.core.config import Settings
    from app.db.pool import Database

    calls = counting_verify(monkeypatch)
    settings = Settings(db_pool_min=10, db_pool_max=10)

    async def race():
        db_pool = Database(settings)
        await db_pool.open()
        auth = service.AuthService.from_settings(settings, AuthRepository(db_pool.connection))

        async def one(i):
            try:
                await auth.login(f"crowd{i}" + DOMAIN, "guess", "203.0.113.112", None)
            except service.AuthError as exc:
                return exc.status, exc.error
        try:
            return await asyncio.gather(*(one(i) for i in range(30)))
        finally:
            await db_pool.close()
    assert asyncio.run(race()) == [(401, GENERIC_LOGIN_ERROR)] * 30
    assert len(calls) == 20
    with db() as conn:
        assert conn.execute("SELECT request_count FROM public.rate_limits WHERE ip_address = 'ip:203.0.113.112' "
                            "AND endpoint = 'auth:login'").fetchone()[0] == 30  # none lost to the race


def test_the_event_loop_keeps_running_while_a_password_is_hashed(env, monkeypatch):
    """A sign-in whose hashing takes 0.6 s must not freeze the worker: a ticker on the same event loop keeps
    ticking (with hashing on the loop, it would stall for the whole 0.6 s)."""
    import time

    import app.auth.service as service
    from app.auth.repository import AuthRepository
    from app.cli import connection_factory
    from app.core.config import Settings

    real = service.verify_password

    def slow(password, stored_hash):
        time.sleep(0.6)
        return real(password, stored_hash)
    monkeypatch.setattr(service, "verify_password", slow)
    make_user("patient2" + DOMAIN)
    settings = Settings()
    auth = service.AuthService.from_settings(settings, AuthRepository(connection_factory(settings), connection_factory(settings, "auth")))

    async def scenario():
        gaps, done = [], asyncio.Event()

        async def tick():
            loop = asyncio.get_running_loop()
            last = loop.time()
            while not done.is_set():
                await asyncio.sleep(0.02)
                now = loop.time()
                gaps.append(now - last)
                last = now
        ticker = asyncio.create_task(tick())
        start = time.perf_counter()
        try:
            user, _ = await auth.login("patient2" + DOMAIN, PASSWORD, "203.0.113.113", None)
        finally:
            done.set()
            await ticker
        return user.email, time.perf_counter() - start, max(gaps)
    email, elapsed, worst_gap = asyncio.run(scenario())
    assert email == "patient2" + DOMAIN and elapsed >= 0.6  # the slow check really ran, and the sign-in succeeded
    assert worst_gap < 0.3, f"event loop stalled for {worst_gap:.2f} s"
