"""Helpers for the database-backed integration tests (fixtures are in conftest.py).

Enabled when HYPERCHAT_TEST_AUTH_DB is "<dbname>:<port>" of an Alembic-migrated scratch database.
HYPERCHAT_TEST_PG_HOST selects the server: a Unix-socket directory with peer authentication (the VPS,
default /var/run/postgresql) or a TCP host (CI's PostgreSQL service container). The app and the
helpers connect as hyperchat_app, the DML-only runtime role.

Every synthetic row is recognizable and removed after each test: users/invites @authtest.invalid,
streamers (and their donations) and partners with slugs starting "authtest-", nonces starting
"authtest-", platform soundboard slots with audio_key starting "authtest-". Rows owned by a synthetic
creator (moderators, bans, donor totals, moderation actions, payment health) go with it (ON DELETE CASCADE),
order_lookup rows and transcripts with their donation; webhook events with event ids starting "authtest-";
jobs of synthetic donations (or with dedupe keys starting "authtest-"); terms acceptances go with their
synthetic user. TTS moderation audit rows of synthetic creators keep a NULL streamer (SET NULL) and are
removed by their donation ids. Realtime outbox rows go with their creator (ON DELETE CASCADE).
"""

import logging
import os
import secrets
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from app.auth.google import GoogleAuthError, GoogleIdentity
from app.auth.passwords import hash_password

TARGET = os.environ.get("HYPERCHAT_TEST_AUTH_DB", "")
PG_HOST = os.environ.get("HYPERCHAT_TEST_PG_HOST", "/var/run/postgresql")
requires_db = pytest.mark.skipif(not TARGET, reason="HYPERCHAT_TEST_AUTH_DB not set")

FRONT = "https://hyperchat.space"
ORIGIN = {"origin": FRONT}
DOMAIN = "@authtest.invalid"
SLUG_PREFIX = "authtest-"
PASSWORD = "Correct-Horse-9"
NEVER_TOUCH = ("hyperchat", "hyperchat_restore_test")


def target() -> tuple[str, str]:
    db, port = TARGET.split(":")
    return db, port


def db(role: str = "hyperchat_app") -> psycopg.Connection:
    name, port = target()
    return psycopg.connect(f"host={PG_HOST} port={port} dbname={name} user={role}", autocommit=True)


def current_database() -> str:
    with db() as conn:
        return conn.execute("SELECT current_database()").fetchone()[0]


def make_user(email: str, password: str | None = PASSWORD, role: str = "streamer", hashed: str | None = None,
              streamer_id: str | None = None, is_active: bool = True) -> str:
    h = hashed if hashed is not None else (hash_password(password) if password else None)
    with db() as conn:
        return conn.execute(
            "INSERT INTO public.users (email, password_hash, role, streamer_id, is_active) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id::text", (email, h, role, streamer_id, is_active)).fetchone()[0]


def make_streamer(slug: str, user_id: str | None = None, name: str = "Auth Test") -> str:
    assert slug.startswith(SLUG_PREFIX)
    with db() as conn:
        return conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, user_id) VALUES (%s, %s, %s) "
                            "RETURNING id::text", (slug, name, user_id)).fetchone()[0]


def update_streamer(streamer_id: str, **cols) -> None:
    sets = ", ".join(f"{k} = %({k})s" for k in cols)
    with db() as conn:
        conn.execute(f"UPDATE public.streamers SET {sets} WHERE id = %(id)s", {**cols, "id": streamer_id})


def owned_creator(client, slug: str, email: str = "owner" + DOMAIN, **cols) -> tuple[str, dict]:
    """A creator owned by a new user who is then signed in on `client`: (streamer id, CSRF headers)."""
    sid = make_streamer(slug, user_id=make_user(email))
    if cols:
        update_streamer(sid, **cols)
    resp = login(client, email)
    assert resp.status_code == 200, resp.text
    return sid, csrf_headers(resp)


def make_donation(streamer_id: str, amount_inr=100, *, payment: str = "success", moderation: str = "pending",
                  name: str = "Donor", message: str | None = None, at: datetime | None = None,
                  currency: str = "INR", amount=None, visible: bool = True) -> str:
    """Insert a synthetic donation (order_id "authtest-...") and return its id."""
    with db() as conn:
        return conn.execute(
            "INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, amount_inr, "
            "payment_status, moderation_status, message, message_visible, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id::text",
            (streamer_id, SLUG_PREFIX + secrets.token_hex(6), name, amount if amount is not None else amount_inr,
             currency, amount_inr, payment, moderation, message, visible, at or datetime.now(UTC))).fetchone()[0]


def creator_state(streamer_id: str) -> str:
    """Everything a dashboard write could touch for one creator, as comparable text."""
    with db() as conn:
        return conn.execute("""
            SELECT json_build_array(
              (SELECT row_to_json(s) FROM public.streamers s WHERE s.id = %(s)s),
              (SELECT json_agg(d ORDER BY d.id) FROM public.donations d WHERE d.streamer_id = %(s)s),
              (SELECT json_agg(m ORDER BY m.id) FROM public.streamers_moderators m WHERE m.streamer_id = %(s)s),
              (SELECT json_agg(b ORDER BY b.id) FROM public.banned_donors b WHERE b.streamer_id = %(s)s),
              (SELECT json_agg(t ORDER BY t.id) FROM public.streamer_donator_totals t WHERE t.streamer_id = %(s)s),
              (SELECT json_agg(a ORDER BY a.id) FROM public.moderation_actions a WHERE a.streamer_id = %(s)s)
            )::text""", {"s": streamer_id}).fetchone()[0]


def cleanup_synthetic_rows() -> None:
    with db() as conn:
        conn.execute("DELETE FROM public.user_invites WHERE email::text LIKE %s", ("%" + DOMAIN,))
        conn.execute("DELETE FROM public.tts_moderation_audit WHERE donation_id IN (SELECT d.id FROM public.donations d "
                     "JOIN public.streamers s ON s.id = d.streamer_id WHERE s.streamer_slug LIKE %s) "
                     "OR supporter_identity_hash LIKE %s", (SLUG_PREFIX + "%", SLUG_PREFIX + "%"))
        # Jobs reference donations by id only (no FK): the synthetic donations' jobs go first.
        conn.execute("DELETE FROM public.jobs WHERE dedupe_key IN (SELECT d.id::text FROM public.donations d "
                     "JOIN public.streamers s ON s.id = d.streamer_id WHERE s.streamer_slug LIKE %s) "
                     "OR dedupe_key LIKE %s", (SLUG_PREFIX + "%", SLUG_PREFIX + "%"))
        # donations.streamer_id is ON DELETE RESTRICT: synthetic donations go first.
        conn.execute("DELETE FROM public.donations WHERE streamer_id IN "
                     "(SELECT id FROM public.streamers WHERE streamer_slug LIKE %s)", (SLUG_PREFIX + "%",))
        conn.execute("DELETE FROM public.streamers WHERE streamer_slug LIKE %s", (SLUG_PREFIX + "%",))
        conn.execute("DELETE FROM public.platform_soundboard_slots WHERE audio_key LIKE %s", (SLUG_PREFIX + "%",))
        conn.execute("DELETE FROM public.users WHERE email::text LIKE %s", ("%" + DOMAIN,))  # cascades sessions
        conn.execute("DELETE FROM public.partner_api_keys WHERE partner_slug LIKE %s", (SLUG_PREFIX + "%",))
        conn.execute("DELETE FROM public.request_nonces WHERE nonce LIKE %s OR nonce LIKE %s",
                     (SLUG_PREFIX + "%", "upload:" + SLUG_PREFIX + "%"))
        conn.execute("DELETE FROM public.rate_limits WHERE endpoint LIKE 'auth:%' OR endpoint LIKE 'admin:%' "
                     "OR endpoint LIKE 'upload:%' OR endpoint LIKE 'soundboard:%' OR endpoint LIKE 'payment:%' "
                     "OR endpoint LIKE 'transcription:%' OR endpoint LIKE 'realtime:%' OR endpoint LIKE 'obs:%' "
                     "OR endpoint LIKE 'bot:%'")
        # Webhook events of synthetic deliveries (their donations are gone already: donation_id SET NULL).
        conn.execute("DELETE FROM public.payment_webhook_events WHERE event_id LIKE %s", (SLUG_PREFIX + "%",))


def login(client, email, password=PASSWORD, headers=ORIGIN):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password}, headers=headers)


def csrf_headers(resp) -> dict:
    return {**ORIGIN, "x-csrf-token": resp.json()["csrf_token"]}


def signed_in(client, email: str, **user_kwargs) -> dict:
    """Create a user, sign `client` in, return headers for state-changing requests."""
    make_user(email, **user_kwargs)
    resp = login(client, email)
    assert resp.status_code == 200, resp.text
    return csrf_headers(resp)


class CaptureSender:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, email) -> None:
        self.sent.append(email)

    def last_token(self) -> str:
        return parse_qs(urlparse(self.sent[-1].text.split("\n")[3]).query)["token"][0]


class FakeGoogle:
    """Stands in for GoogleOAuth: the id_token checks themselves are unit-tested separately."""

    def __init__(self) -> None:
        self.identity: GoogleIdentity | None = None
        self.seen = []

    def authorization_url(self, state, nonce, verifier) -> str:
        return "https://accounts.google.invalid/auth?state=" + state

    async def exchange(self, code, verifier, nonce) -> GoogleIdentity:
        self.seen.append((code, verifier, nonce))
        if self.identity is None:
            raise GoogleAuthError("invalid_id_token")
        return self.identity


class LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record) -> None:
        self.records.append(str(record.__dict__))


class Browser:
    """A second browser (own cookie jar) on the SAME TestClient: the async DB pool is bound to the
    started client's event loop, so a separately constructed TestClient cannot be used."""

    def __init__(self, client: TestClient) -> None:
        self._client = client
        self.cookies = httpx.Cookies()

    def request(self, method: str, url: str, **kwargs):
        saved = httpx.Cookies(self._client.cookies)
        self._client.cookies = self.cookies
        try:
            return self._client.request(method, url, **kwargs)
        finally:
            self.cookies = httpx.Cookies(self._client.cookies)
            self._client.cookies = saved

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def patch(self, url: str, **kwargs):
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs):
        return self.request("DELETE", url, **kwargs)


def new_browser(client) -> Browser:
    return Browser(client)
