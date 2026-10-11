"""Security audit F11: the forgot-password answer must not depend on whether the account exists.

The route only applies the rate limits (the same work for every email) and answers 202; the account lookup, the
reset token and the email run as a background task after the response is sent.
"""

import asyncio
import json

from tests.integration import DOMAIN, requires_db
from tests.integration import db as _db
from tests.integration import make_user as _user

pytestmark = requires_db

FORGOT = "/api/v1/auth/password/forgot"
GENERIC = {"message": "If an account exists for this email, a reset link has been sent."}


def _forgot(app, email: str):
    return app.post(FORGOT, json={"email": email})


def test_unknown_inactive_and_active_accounts_get_the_same_answer(app):
    _user("f11-active" + DOMAIN)
    _user("f11-inactive" + DOMAIN, is_active=False)
    sender = app.app_state.auth.sender
    answers = [_forgot(app, e + DOMAIN) for e in ("f11-active", "f11-inactive", "f11-ghost")]
    assert [(r.status_code, r.json()) for r in answers] == [(202, GENERIC)] * 3
    assert [e.to for e in sender.sent] == ["f11-active" + DOMAIN]  # only the active account gets a link


class GatedSender:
    """Blocks inside send() until released, so the test can look at the response while the email is 'in flight'."""

    def __init__(self) -> None:
        self.sent = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, email) -> None:
        self.entered.set()
        await self.release.wait()
        self.sent.append(email)


def test_the_response_is_sent_before_the_email_is_delivered(app):
    """Drives the ASGI app directly on the TestClient's event loop: the complete 202 body must have been sent while
    the email sender is still blocked, i.e. the response does not wait for delivery (no timing thresholds)."""
    _user("f11-slow" + DOMAIN)
    body = json.dumps({"email": "f11-slow" + DOMAIN}).encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "https",
        "path": FORGOT, "raw_path": FORGOT.encode(), "root_path": "", "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
        "client": ("testclient", 50000), "server": ("testserver", 443), "state": {},
    }

    async def scenario():
        sender = GatedSender()
        app.app_state.auth.sender = sender
        messages = []
        requests = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            if requests:
                return requests.pop()
            await asyncio.Event().wait()  # no disconnect while the request is being handled

        async def send(message):
            messages.append(message)

        task = asyncio.create_task(app.app(scope, receive, send))
        await asyncio.wait_for(sender.entered.wait(), 10)
        # The email is being sent right now and cannot finish; the response must already be complete.
        start = next(m for m in messages if m["type"] == "http.response.start")
        chunks = [m for m in messages if m["type"] == "http.response.body"]
        assert start["status"] == 202
        assert chunks and not chunks[-1].get("more_body", False)
        assert json.loads(b"".join(m.get("body", b"") for m in chunks)) == GENERIC
        assert not task.done() and sender.sent == []
        sender.release.set()
        await asyncio.wait_for(task, 10)
        return sender.sent

    sent = app.portal.call(scenario)
    assert [e.to for e in sent] == ["f11-slow" + DOMAIN]


def test_rate_limits_and_reset_links_still_work(app):
    _user("f11-limit" + DOMAIN)
    sender = app.app_state.auth.sender
    assert [_forgot(app, "f11-limit" + DOMAIN).status_code for _ in range(4)] == [202] * 4
    assert len(sender.sent) == 3  # per-email limit: 3 links per hour, the 4th request sends nothing
    token = sender.last_token()
    with _db() as conn:
        n = conn.execute("SELECT count(*) FROM public.password_reset_tokens t JOIN public.users u ON u.id = t.user_id "
                         "WHERE u.email = %s AND t.used_at IS NULL", ("f11-limit" + DOMAIN,)).fetchone()[0]
    assert n == 3
    assert app.post("/api/v1/auth/password/reset",
                    json={"token": token, "new_password": "F11-New-Pass-42"}).status_code == 200
    assert app.post("/api/v1/auth/password/reset",
                    json={"token": token, "new_password": "F11-Other-Pass-42"}).status_code == 400  # single use
    # per-IP limit: 10 per hour across emails (4 used above), then 429 for every email
    codes = [_forgot(app, f"f11-ip{i}" + DOMAIN).status_code for i in range(7)]
    assert codes == [202] * 6 + [429]
    assert len(sender.sent) == 3


class FailingSender:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, email) -> None:
        self.sent.append(email)
        raise RuntimeError("provider unreachable")


def test_an_email_failure_changes_nothing_for_the_caller_and_leaks_nothing(app):
    _user("f11-fail" + DOMAIN)
    sender = app.app_state.auth.sender = FailingSender()
    records = app.app_state.log_capture.records
    r = _forgot(app, "f11-fail" + DOMAIN)
    assert (r.status_code, r.json()) == (202, GENERIC)
    token = sender.sent[0].text.split("token=")[1].split()[0]
    failed = [rec for rec in records if "password_reset_email_failed" in rec]
    assert len(failed) == 1 and "RuntimeError" in failed[0] and "'levelname': 'ERROR'" in failed[0]
    assert not any("password_reset_requested" in rec for rec in records)
    assert all(token not in rec and "f11-fail" not in rec and "provider unreachable" not in rec for rec in records)
    assert _forgot(app, "f11-fail" + DOMAIN).status_code == 202  # the app keeps serving


def test_a_successful_request_logs_the_event_but_never_the_token(app):
    user_id = _user("f11-log" + DOMAIN)
    assert _forgot(app, "f11-log" + DOMAIN).status_code == 202
    token = app.app_state.auth.sender.last_token()
    records = app.app_state.log_capture.records
    assert any("password_reset_requested" in rec and user_id in rec for rec in records)
    assert all(token not in rec and "f11-log" not in rec for rec in records)
