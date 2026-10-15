"""Phase 7 payments API through the real app (create_app, routers, CORS, error handlers) with in-process
fakes for the database-backed parts (creator rows behind the real PublicService, the payment repository,
the rate limiter), R2 (in-memory) and Razorpay (scripted). tests/payments/test_payments_db.py repeats the
flows on PostgreSQL, including the completion transaction, idempotency and concurrency."""

import hashlib
import logging
import re
from collections import Counter
from decimal import Decimal

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.payments.accounts import AccountHealth, PaymentAccounts
from app.payments.repository import Applied
from app.payments.service import PaymentService
from app.public.service import PublicService
from app.soundboard.library import HypersoundLibrary
from app.storage.memory import InMemoryObjectStore, StoredObject
from tests.payments.fakes import (
    OTHER_SECRET,
    PAYER_CONTACT,
    PAYER_EMAIL,
    FakeRazorpay,
    razorpay_env,
    sign,
    webhook_body,
)

P = "/api/v1/public/streamers"
D = "/api/v1/public/donations"
W = "/api/v1/webhooks/razorpay"
FRONT = "https://app.quicktestspace.site"
ORIGIN = {"origin": FRONT}
HEX = "b" * 32
BASE = "https://media.example.test"
VOICE = f"voice-messages/creator-a_1800000000000_{HEX}.webm"
MEDIA = f"donation-media/creator-a_1800000000000_{HEX}.gif"
LEAKS = re.compile(r"Traceback|File \"|site-packages|psycopg|SELECT |INSERT |/opt/|/etc/|TEST-ONLY|rzp_test_|"
                   r"x-amz|Signature=|payer\.secret|9999012345", re.I)


class FakePublicRepo:
    def __init__(self) -> None:
        base = dict(status="active", media_upload_enabled=True, media_max_file_size_mb=Decimal("10"),
                    min_text_amount_inr=None, min_tts_amount_inr=None, min_voice_amount_inr=Decimal("150"),
                    min_hypersound_amount_inr=None, media_min_amount=Decimal("100"),
                    message_char_tiers=[{"min_amount": 0, "max_chars": 50}, {"min_amount": 100, "max_chars": 120}])
        self.rows = {
            "creator-a": dict(base, id="id-a", streamer_slug="creator-a"),
            "no-media": dict(base, id="id-n", streamer_slug="no-media", media_upload_enabled=False),
            "no-account": dict(base, id="id-x", streamer_slug="no-account"),
            "pending-c": dict(base, id="id-p", streamer_slug="pending-c", status="pending"),
        }
        self.blocks: dict[str, str] = {}
        self.tiers = {"id-a": [(150, 10), (300, 20)]}

    async def donation_block(self, streamer_id):
        return self.blocks.get(streamer_id)  # the real rule: public.creator_donation_block (migration 0020)

    async def active_streamer(self, slug):
        row = self.rows.get(slug)
        return dict(row) if row and row["status"] == "active" else None

    async def voice_tiers(self, streamer_id):
        return self.tiers.get(streamer_id, []), []


class FakePaymentRepo:
    def __init__(self) -> None:
        self.settings = {sid: {"order_prefix": f"{sid}_rp_", "razorpay_account": account, "health_status": None,
                               "health_on_hold": None, "health_checked_at": None}
                         for sid, account in (("id-a", "primary"), ("id-n", "primary"), ("id-x", None))}
        self.health: list[tuple] = []
        self.donations: list[dict] = []
        self.rows: dict[str, dict] = {}
        self.applied: list[tuple] = []
        self.events: list[tuple] = []

    async def payment_settings(self, sid):
        return dict(self.settings[sid])

    async def record_health(self, sid, status):
        self.health.append((sid, status))

    async def create_donation(self, row):
        self.donations.append(row)
        self.rows[row["order_id"]] = {
            "id": f"don-{len(self.donations)}", "order_id": row["order_id"], "provider_order_id": row["provider_order_id"],
            "amount": row["amount"], "currency": row["currency"], "payment_status": "pending",
            "status_token_hash": row["status_token_hash"], "provider_payment_id": None,
            "account": row["payment_account"], "streamer_slug": "creator-a"}
        return self.rows[row["order_id"]]["id"]

    async def by_order_id(self, order_id):
        return self.rows.get(order_id)

    async def by_provider_order(self, razorpay_order_id):
        return next((r for r in self.rows.values() if r["provider_order_id"] == razorpay_order_id), None)

    async def event_seen(self, event_id):
        return any(e[0] == event_id for e in self.events)

    async def record_event(self, event, outcome, donation_id=None):
        self.events.append((event.event_id, outcome, donation_id))
        return True

    async def apply(self, donation_id, outcome, payment_id, event=None):
        self.applied.append((donation_id, outcome, payment_id, event.event_id if event else None))
        return Applied("completed" if outcome == "captured" else "failed",
                       "success" if outcome == "captured" else "failed")


class FakeLimiter:
    def __init__(self) -> None:
        self.counts: Counter = Counter()
        self.calls: list[tuple] = []

    async def hit_rate_limit(self, key, bucket, limit, window_seconds):
        self.calls.append((key, bucket, limit, window_seconds))
        self.counts[(key, bucket)] += 1
        return self.counts[(key, bucket)] <= limit


class World:
    def __init__(self) -> None:
        self.env = razorpay_env()
        self.settings = Settings(_env_file=None, **{k.removeprefix("HYPERCHAT_").lower(): v
                                                    for k, v in self.env.items()})
        self.public_repo = FakePublicRepo()
        self.repo = FakePaymentRepo()
        self.razorpay = FakeRazorpay()
        self.limiter = FakeLimiter()
        self.store = InMemoryObjectStore(BASE)
        self.store.seed_library("hypersounds/Air Horn.mp3")
        self.clock = [1_800_000_000_000]

    def service(self, *, store="default") -> PaymentService:
        store = self.store if store == "default" else store
        return PaymentService(public=PublicService(self.public_repo), repo=self.repo,
                              accounts=PaymentAccounts.from_settings(self.settings), client=self.razorpay,
                              health=AccountHealth(self.razorpay), library=HypersoundLibrary(store), store=store,
                              limiter=self.limiter, now_ms=lambda: self.clock[0])

    def secret(self, name: str) -> str:
        return self.env[f"HYPERCHAT_RAZORPAY_{name}"]


@pytest.fixture
def world():
    return World()


@pytest.fixture
def client(monkeypatch, world):
    monkeypatch.setenv("HYPERCHAT_DB_ENABLED", "false")
    monkeypatch.setenv("HYPERCHAT_APP_ENV", "test")
    monkeypatch.setenv("HYPERCHAT_FRONTEND_URL", FRONT)
    monkeypatch.setenv("HYPERCHAT_ALLOWED_ORIGINS", "http://localhost:5173")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    bodies: list[str] = []
    capture = LogLines()
    logging.getLogger().addHandler(capture)
    try:
        with TestClient(create_app(), base_url="https://testserver") as c:
            c.app.state.payments = world.service()
            c.event_hooks["response"].append(lambda r: bodies.append(r.read().decode("utf-8", "replace")))
            c.logs = capture
            yield c
    finally:
        logging.getLogger().removeHandler(capture)
        get_settings.cache_clear()
    leaked = [x[:200] for x in bodies if LEAKS.search(x) and "razorpay_key_id" not in x]
    assert not leaked, leaked  # no stack traces, SQL, secrets or payer details in any response
    for value in (*world.env.values(), PAYER_EMAIL, PAYER_CONTACT):
        assert value not in capture.text(), "a secret or payer detail reached the logs"


class LogLines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record) -> None:
        self.records.append(str(record.__dict__))

    def text(self) -> str:
        return "\n".join(self.records)


def body(**kw) -> dict:
    return {"name": "Ravi", "amount": 100, **kw}


def checkout(client, slug="creator-a", headers=ORIGIN, **kw):
    return client.post(f"{P}/{slug}/donations", json=body(**kw), headers=headers)


def seed_voice(world, key=VOICE, duration_ms="9000"):
    world.store.objects[key] = StoredObject(b"v", "audio/webm", "", {"duration-ms": duration_ms})


def seed_media(world, key):
    world.store.objects[key] = StoredObject(b"m", "image/gif" if key.endswith(".gif") else "image/png", "", {})


# ── checkout: request guards ────────────────────────────────────────────────

@pytest.mark.parametrize("origin", [None, "https://evil.invalid", "null", "https://app.quicktestspace.site.evil.invalid",
                                    "http://app.quicktestspace.site"])
def test_checkout_needs_a_trusted_origin(client, world, origin):
    r = checkout(client, headers={"origin": origin} if origin else {})
    assert (r.status_code, r.json()) == (403, {"error": "Request origin not allowed"})
    assert world.razorpay.calls == [] and world.repo.donations == []


def test_localhost_dev_origin_allowed_and_rate_limit_recorded(client, world):
    r = checkout(client, headers={"origin": "http://localhost:5173"})
    assert r.status_code == 201, r.text
    assert world.limiter.calls[0] == ("ip:testclient", "payment:checkout", 10, 60)


def test_checkout_rate_limited_per_ip(client, world):
    for _ in range(10):
        assert checkout(client).status_code == 201
    r = checkout(client)
    assert (r.status_code, r.json()) == (429, {"error": "Too many requests. Please try again shortly."})
    assert len(world.repo.donations) == 10


@pytest.mark.parametrize("extra", [{"media_type": "image"}, {"is_hyperemote": True}, {"upload_token": "x"},
                                   {"streamer_slug": "creator-a"}, {"moderation_status": "approved"}])
def test_unknown_fields_rejected(client, world, extra):
    r = checkout(client, **extra)
    assert r.status_code == 422 and world.razorpay.calls == []


@pytest.mark.parametrize("slug", ["pending-c", "nobody", "creator-a..", "creator a", "a" * 70])
def test_unknown_or_inactive_creator(client, slug):
    r = checkout(client, slug=slug)
    assert (r.status_code, r.json()) == (404, {"error": "Streamer not found"})


def test_a_creator_that_cannot_accept_donations_gets_no_order(client, world):
    """Migration 0020: public.creator_donation_block (e.g. an unverified new owner); a generic 503, never why."""
    world.public_repo.blocks["id-a"] = "kyc_required"
    r = checkout(client)
    assert (r.status_code, r.json()) == (503, {"error": "Donations are temporarily unavailable"})
    assert world.razorpay.calls == [] and world.repo.donations == []


def test_ineligible_at_insert_time_writes_no_donation(client, world):
    """Became ineligible between the check and the insert: the trigger's refusal is the same generic 503."""
    class Ineligible(psycopg.Error):
        sqlstate = "HC001"

    async def refuse(row):
        raise Ineligible("creator not eligible for new donations: kyc_required")

    world.repo.create_donation = refuse
    r = checkout(client)
    assert (r.status_code, r.json()) == (503, {"error": "Donations are temporarily unavailable"})
    assert len(world.razorpay.orders) == 1 and world.repo.donations == []  # the unpaid order simply expires


# ── checkout: validation ────────────────────────────────────────────────────

@pytest.mark.parametrize("kw, error", [
    ({"currency": "JPY"}, "Unsupported currency"), ({"currency": "usdt"}, "Unsupported currency"),
    ({"name": " "}, "Name is required"), ({"name": "Ravi \U0001F525"}, "Name cannot contain emojis"),
    ({"name": "<i></i>"}, "Invalid name provided"),
    ({"amount": 0}, "Valid amount is required"), ({"amount": -40}, "Valid amount is required"),
    ({"amount": "abc"}, None), ({"amount": 40.005}, "Amount can have at most 2 decimal places"),
    ({"amount": 0.1 + 0.2}, "Amount can have at most 2 decimal places"),
    ({"amount": 600000}, "Amount is too large"),
    ({"amount": 39}, "Text messages require minimum INR 40"),
    ({"amount": 0.49, "currency": "USD"}, "Text messages require minimum USD 0.5"),
    ({"amount": 50, "message": "x" * 51}, "Message exceeds 50 character limit for this donation amount"),
    ({"amount": 100, "message": "&" * 30}, "Message exceeds 120 character limit for this donation amount"),
    ({"voice_message_url": f"{BASE}/{VOICE}", "media_url": f"{BASE}/{MEDIA}"}, "Only one attachment per donation"),
    ({"hypersound_url": f"{BASE}/hypersounds/Air%20Horn.mp3", "media_url": f"{BASE}/{MEDIA}"},
     "Only one attachment per donation"),
])
def test_checkout_validation(client, world, kw, error):
    r = checkout(client, **kw)
    if error is None:
        assert r.status_code == 422
    else:
        assert (r.status_code, r.json()) == (400, {"error": error})
    assert world.razorpay.calls == [] and world.repo.donations == []


def test_message_sanitised_and_tier_counted_after_encoding(client, world):
    r = checkout(client, amount=100, message="<b>hi</b> & 'bye'")
    assert r.status_code == 201
    assert world.repo.donations[-1]["message"] == "hi &amp; &#x27;bye&#x27;"
    assert world.repo.donations[-1]["donor_name"] == "Ravi"


def test_usd_checkout_amounts(client, world):
    r = checkout(client, amount="1.5", currency="USD")
    assert r.status_code == 201
    row = world.repo.donations[-1]
    assert (row["amount"], row["currency"], row["amount_inr"]) == (Decimal("1.50"), "USD", Decimal("133.50"))
    assert world.razorpay.orders[-1]["amount"] == 150 and r.json()["amount_subunits"] == 150


# ── checkout: attachments ───────────────────────────────────────────────────

def test_hypersound_from_the_library(client, world):
    url = world.store.public_url("hypersounds/Air Horn.mp3")
    r = checkout(client, amount=30, hypersound_url=url)
    assert r.status_code == 201 and world.repo.donations[-1]["hypersound_url"] == url
    for bad in (url + "?x", url.replace("Air%20Horn", "Other"), "https://evil.invalid/hypersounds/Air%20Horn.mp3",
                url.replace("https", "http")):
        r = checkout(client, amount=30, hypersound_url=bad)
        assert (r.status_code, r.json()) == (400, {"error": "Invalid HyperSound"})


def test_hypersound_minimum(client):
    r = checkout(client, amount=29, hypersound_url=f"{BASE}/hypersounds/Air%20Horn.mp3")
    assert r.json() == {"error": "HyperSounds require minimum INR 30"}


def test_voice_checks_ownership_existence_and_duration_tier(client, world):
    seed_voice(world, duration_ms="9000")
    url = f"{BASE}/{VOICE}"
    r = checkout(client, amount=150, voice_message_url=url)
    assert r.status_code == 201 and world.repo.donations[-1]["voice_message_url"] == url
    seed_voice(world, duration_ms="12000")  # 12 s: the Rs 150 tier allows 10 s, the Rs 300 tier 20 s
    r = checkout(client, amount=150, voice_message_url=url)
    assert r.json() == {"error": "Voice message is too long for this amount. Maximum is 10 seconds."}
    assert checkout(client, amount=300, voice_message_url=url).status_code == 201
    seed_voice(world, duration_ms="")
    assert checkout(client, amount=300, voice_message_url=url).json() == {"error": "Invalid voice message"}
    missing = f"{BASE}/voice-messages/creator-a_1800000000001_{HEX}.webm"
    assert checkout(client, amount=300, voice_message_url=missing).json() == {"error": "Attachment not found"}
    other = f"{BASE}/voice-messages/no-media_1800000000000_{HEX}.webm"
    seed_voice(world, key=other.removeprefix(BASE + "/"))
    assert checkout(client, amount=300, voice_message_url=other).json() == {"error": "Invalid voice message"}


def test_voice_minimum(client, world):
    seed_voice(world)
    r = checkout(client, amount=149, voice_message_url=f"{BASE}/{VOICE}")
    assert r.json() == {"error": "Voice messages require minimum INR 150"}


def test_media_type_comes_from_the_server(client, world):
    seed_media(world, MEDIA)
    r = checkout(client, amount=100, media_url=f"{BASE}/{MEDIA}")
    assert r.status_code == 201 and world.repo.donations[-1]["media_type"] == "gif"
    png = MEDIA.replace(".gif", ".png")
    seed_media(world, png)
    assert checkout(client, amount=100, media_url=f"{BASE}/{png}").status_code == 201
    assert world.repo.donations[-1]["media_type"] == "image"


def test_media_needs_the_creator_switch(client, world):
    key = f"donation-media/no-media_1800000000000_{HEX}.png"
    seed_media(world, key)
    r = checkout(client, slug="no-media", amount=100, media_url=f"{BASE}/{key}")
    assert (r.status_code, r.json()) == (403, {"error": "Media uploads are not enabled for this creator"})


def test_storage_failures(client, world):
    seed_voice(world)
    world.store.fail_with, world.store.fail_ops = "r2_InternalError", {"head"}
    r = checkout(client, amount=150, voice_message_url=f"{BASE}/{VOICE}")
    assert (r.status_code, r.json()) == (502, {"error": "Could not check the attachment. Please try again."})
    client.app.state.payments = world.service(store=None)
    r = checkout(client, amount=150, voice_message_url=f"{BASE}/{VOICE}")
    assert (r.status_code, r.json()) == (503, {"error": "Uploads are not available"})
    r = checkout(client, amount=30, hypersound_url=f"{BASE}/hypersounds/Air%20Horn.mp3")
    assert (r.status_code, r.json()) == (503, {"error": "Sound library unavailable"})
    assert world.razorpay.calls == []


# ── checkout: accounts, health, Razorpay failures ───────────────────────────

def test_successful_checkout(client, world):
    r = checkout(client, amount="100.50", message="Hello")
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    out = r.json()
    assert set(out) == {"order_id", "razorpay_order_id", "razorpay_key_id", "amount", "amount_subunits", "currency",
                        "status_token"}  # no upload_token (D5), no donation id
    assert out["razorpay_key_id"] == world.env["HYPERCHAT_RAZORPAY_PRIMARY_KEY_ID"]
    assert (out["amount"], out["amount_subunits"], out["currency"]) == (100.5, 10050, "INR")
    assert re.fullmatch(r"id-a_rp_1800000000000_[a-z0-9]{8}", out["order_id"])
    assert re.fullmatch(r"[0-9a-f]{64}", out["status_token"])
    row = world.repo.donations[-1]
    assert row["status_token_hash"] == hashlib.sha256(out["status_token"].encode()).hexdigest()
    assert (row["payment_account"], row["provider_order_id"], row["amount_inr"]) == \
        ("primary", out["razorpay_order_id"], Decimal("100.50"))
    assert "upload_token_hash" not in row and row["media_type"] is None
    assert world.razorpay.calls[-1] == ("create_order", "primary", 10050, "INR", out["order_id"][:40])
    assert world.repo.health == [("id-a", "ok")]  # health row written once (it was missing)


def test_one_account_and_shared_probe_cache(client, world):
    assert checkout(client).json()["razorpay_key_id"] == world.env["HYPERCHAT_RAZORPAY_PRIMARY_KEY_ID"]
    assert checkout(client).status_code == 201 and checkout(client, slug="no-media").status_code == 201
    assert world.razorpay.count("probe") == 1  # once for the account, cached across creators
    assert [o["account"] for o in world.razorpay.orders] == ["primary", "primary", "primary"]


def test_creator_without_account_is_on_hold(client, world):
    r = checkout(client, slug="no-account")
    assert (r.status_code, r.json()) == (503, {"error": "Payments for this creator are on hold"})
    assert world.repo.health == [("id-x", "missing_key")] and world.razorpay.calls == []


def test_unconfigured_account_is_on_hold(client, world):
    world.settings = Settings(_env_file=None)
    client.app.state.payments = world.service()
    r = checkout(client)
    assert r.json() == {"error": "Payments for this creator are on hold"}
    assert world.repo.health == [("id-a", "missing_key")]


@pytest.mark.parametrize("probe, status, error", [
    ("invalid_key", 503, "Payments for this creator are on hold"),
    ("network_error", 502, "Payment provider unavailable. Please try again."),  # transient, not a hold (D7)
])
def test_failing_credentials_put_checkout_on_hold(client, world, probe, status, error):
    world.razorpay.probe_result = probe
    r = checkout(client)
    assert (r.status_code, r.json()) == (status, {"error": error})
    assert world.repo.health == [("id-a", probe)] and world.razorpay.count("create_order") == 0
    world.razorpay.probe_result = "ok"
    r = checkout(client)
    assert (r.status_code == 201) is (probe == "network_error")  # network errors are re-probed, bad keys cached


def test_health_row_only_written_when_it_changes_or_is_stale(client, world):
    from datetime import UTC, datetime, timedelta

    world.repo.settings["id-a"].update(health_status="ok", health_on_hold=False,
                                       health_checked_at=datetime.now(UTC) - timedelta(seconds=10))
    assert checkout(client).status_code == 201 and world.repo.health == []
    world.repo.settings["id-a"]["health_checked_at"] = datetime.now(UTC) - timedelta(minutes=5)
    assert checkout(client).status_code == 201 and world.repo.health == [("id-a", "ok")]


@pytest.mark.parametrize("failure, status, error", [
    ("network", 502, "Payment provider unavailable. Please try again."),
    ("bad_response", 502, "Payment provider unavailable. Please try again."),
    ("rejected", 400, "This amount or currency cannot be paid"),
    ("auth", 503, "Payments for this creator are on hold"),
])
def test_razorpay_order_failures_store_nothing(client, world, failure, status, error):
    world.razorpay.fail["create_order"] = failure
    r = checkout(client)
    assert (r.status_code, r.json()) == (status, {"error": error})
    assert world.repo.donations == []
    if failure == "auth":  # Razorpay refused the keys: on hold without another probe
        assert world.repo.health[-1] == ("id-a", "invalid_key")
        del world.razorpay.fail["create_order"]
        assert checkout(client).status_code == 503 and world.razorpay.count("create_order") == 1


# ── status / confirm: token handling ────────────────────────────────────────

def paid_order(client, world) -> dict:
    r = checkout(client)
    assert r.status_code == 201
    return r.json()


def test_status_token_rules(client, world):
    out = paid_order(client, world)
    url = f"{D}/{out['order_id']}/status"
    ok = client.get(url, headers={"x-status-token": out["status_token"]})
    assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store"
    assert ok.json() == {"order_id": out["order_id"], "payment_status": "pending", "amount": 100.0, "currency": "INR"}
    expected = (404, {"error": "Order not found"})
    token = out["status_token"]
    flipped = token[:-1] + ("1" if token[-1] == "0" else "0")  # always a different token
    for headers in ({}, {"x-status-token": flipped}, {"x-status-token": "x" * 64},
                    {"x-status-token": world.repo.donations[-1]["status_token_hash"]}):
        r = client.get(url, headers=headers)
        assert (r.status_code, r.json()) == expected
    r = client.get(f"{url}?status_token={out['status_token']}")  # never read from the URL
    assert (r.status_code, r.json()) == expected
    r = client.get(f"{D}/no_such_order/status", headers={"x-status-token": out["status_token"]})
    assert (r.status_code, r.json()) == expected
    world.repo.rows[out["order_id"]]["status_token_hash"] = None  # legacy row without a token (D4)
    assert (client.get(url, headers={"x-status-token": out["status_token"]}).status_code) == 404


def test_status_needs_no_origin_but_is_rate_limited(client, world):
    out = paid_order(client, world)
    for _ in range(60):
        client.get(f"{D}/{out['order_id']}/status", headers={"x-status-token": out["status_token"]})
    r = client.get(f"{D}/{out['order_id']}/status", headers={"x-status-token": out["status_token"]})
    assert r.status_code == 429


def test_poll_cooldown_limits_razorpay_lookups(client, world):
    out = paid_order(client, world)
    for _ in range(3):
        client.get(f"{D}/{out['order_id']}/status", headers={"x-status-token": out["status_token"]})
    assert world.razorpay.count("order_payments") == 1
    assert ("order:" + out["order_id"], "payment:poll", 1, 5) in world.limiter.calls


def test_poll_completes_through_the_shared_path(client, world):
    out = paid_order(client, world)
    payment = world.razorpay.captured(out["razorpay_order_id"], 10000)
    r = client.get(f"{D}/{out['order_id']}/status", headers={"x-status-token": out["status_token"]})
    assert r.json()["payment_status"] == "success"
    assert world.repo.applied == [("don-1", "captured", payment.id, None)]


def test_poll_mismatch_is_applied_as_failed(client, world):
    out = paid_order(client, world)
    world.razorpay.captured(out["razorpay_order_id"], 9999)
    r = client.get(f"{D}/{out['order_id']}/status", headers={"x-status-token": out["status_token"]})
    assert r.json()["payment_status"] == "failed" and world.repo.applied == [("don-1", "amount_mismatch", None, None)]


def test_confirm_signature_checked_before_anything_else(client, world):
    out = paid_order(client, world)
    url = f"{D}/{out['order_id']}/confirm"
    headers = {**ORIGIN, "x-status-token": out["status_token"]}
    payment = world.razorpay.captured(out["razorpay_order_id"], 10000)
    good = sign(world.secret("PRIMARY_KEY_SECRET"), f"{out['razorpay_order_id']}|{payment.id}".encode())
    flipped = good[:-1] + ("1" if good[-1] == "0" else "0")  # always a different signature
    for sig, pid in ((flipped, payment.id), ("zz" * 32, payment.id), (good, "pay_OTHER123"),
                     (sign(OTHER_SECRET, f"{out['razorpay_order_id']}|{payment.id}".encode()),
                      payment.id), (sign(world.secret("PRIMARY_WEBHOOK_SECRET"),
                                         f"{out['razorpay_order_id']}|{payment.id}".encode()), payment.id)):
        r = client.post(url, json={"razorpay_payment_id": pid, "razorpay_signature": sig}, headers=headers)
        assert (r.status_code, r.json()) == (400, {"error": "Invalid payment signature"})
    assert world.razorpay.count("fetch_payment") == 0 and world.repo.applied == []
    r = client.post(url, json={"razorpay_payment_id": payment.id, "razorpay_signature": good},
                    headers={"x-status-token": out["status_token"]})
    assert r.status_code == 403  # confirm needs the trusted Origin
    r = client.post(url, json={"razorpay_payment_id": payment.id, "razorpay_signature": good}, headers=ORIGIN)
    assert r.status_code == 404  # and the status token
    r = client.post(url, json={"razorpay_payment_id": payment.id, "razorpay_signature": good, "x": 1},
                    headers=headers)
    assert r.status_code == 422
    r = client.post(url, json={"razorpay_payment_id": payment.id, "razorpay_signature": good}, headers=headers)
    assert r.json()["payment_status"] == "success"
    assert world.repo.applied == [("don-1", "captured", payment.id, None)]


def test_confirm_authorized_payment_stays_pending(client, world):
    out = paid_order(client, world)
    payment = world.razorpay.captured(out["razorpay_order_id"], 10000, status="authorized")
    good = sign(world.secret("PRIMARY_KEY_SECRET"), f"{out['razorpay_order_id']}|{payment.id}".encode())
    r = client.post(f"{D}/{out['order_id']}/confirm", json={"razorpay_payment_id": payment.id,
                                                            "razorpay_signature": good},
                    headers={**ORIGIN, "x-status-token": out["status_token"]})
    assert r.json()["payment_status"] == "pending" and world.repo.applied == []


def test_confirm_provider_failure_is_502(client, world):
    out = paid_order(client, world)
    payment = world.razorpay.captured(out["razorpay_order_id"], 10000)
    world.razorpay.fail["fetch_payment"] = "network"
    good = sign(world.secret("PRIMARY_KEY_SECRET"), f"{out['razorpay_order_id']}|{payment.id}".encode())
    r = client.post(f"{D}/{out['order_id']}/confirm", json={"razorpay_payment_id": payment.id,
                                                            "razorpay_signature": good},
                    headers={**ORIGIN, "x-status-token": out["status_token"]})
    assert (r.status_code, r.json()) == (502, {"error": "Could not verify the payment. Please try again."})
    assert world.repo.applied == []


# ── webhook ─────────────────────────────────────────────────────────────────

def post_webhook(client, raw: bytes, secret: str | None, event_id="authtest-evt-1", **headers):
    h = {"content-type": "application/json", **headers}
    if secret is not None:
        h["x-razorpay-signature"] = sign(secret, raw)
    if event_id:
        h["x-razorpay-event-id"] = event_id
    return client.post(W, content=raw, headers=h)


def test_webhook_signature_required_and_checked_before_parsing(client, world):
    out = paid_order(client, world)
    raw = webhook_body("payment.captured", out["razorpay_order_id"], "pay_TESTabc", 10000)
    for secret in (None, "TEST-ONLY-not-ours", world.secret("PRIMARY_KEY_SECRET")):
        r = post_webhook(client, raw, secret)
        assert (r.status_code, r.json()) == (401, {"error": "Invalid signature"})
    tampered = raw.replace(b"10000", b"10001")
    r = client.post(W, content=tampered, headers={"x-razorpay-signature": sign(world.secret("PRIMARY_WEBHOOK_SECRET"),
                                                                                raw)})
    assert r.status_code == 401
    r = client.post(W, content=b"{not json", headers={"x-razorpay-signature": "0" * 64})
    assert r.status_code == 401  # unsigned garbage is never parsed
    assert world.repo.events == [] and world.repo.applied == [] and world.razorpay.count("fetch_payment") == 0


def test_webhook_size_cap_before_anything(client, world):
    raw = b"{" + b" " * (64 * 1024) + b"}"
    r = post_webhook(client, raw, world.secret("PRIMARY_WEBHOOK_SECRET"))
    assert (r.status_code, r.json()) == (413, {"error": "Payload too large"})
    r = client.post(W, content=b"{}", headers={"content-length": "999999999"})
    assert r.status_code == 413


def test_webhook_needs_no_origin_and_signed_garbage_is_400(client, world):
    secret = world.secret("PRIMARY_WEBHOOK_SECRET")
    r = post_webhook(client, b"[1, 2", secret, origin="https://evil.invalid")
    assert (r.status_code, r.json()) == (400, {"error": "Malformed payload"})
    r = post_webhook(client, b"[1, 2]", secret)
    assert r.status_code == 400 and world.repo.events == []


def test_webhook_other_events_are_acknowledged(client, world):
    raw = b'{"event": "refund.processed", "payload": {}}'
    r = post_webhook(client, raw, world.secret("PRIMARY_WEBHOOK_SECRET"))
    assert (r.status_code, r.json()) == (200, {"status": "ignored"})
    assert world.repo.events == [("authtest-evt-1", "ignored", None)]


def test_webhook_unknown_order_and_wrong_account(client, world):
    secret = world.secret("PRIMARY_WEBHOOK_SECRET")
    raw = webhook_body("payment.captured", "order_NOTOURS1", "pay_TESTabc", 10000)
    assert post_webhook(client, raw, secret).json() == {"status": "unknown_order"}
    out = paid_order(client, world)  # a primary-account order...
    raw = webhook_body("payment.captured", out["razorpay_order_id"], "pay_TESTabc", 10000)
    r = post_webhook(client, raw, OTHER_SECRET, event_id="authtest-evt-2")
    assert (r.status_code, r.json()) == (401, {"error": "Invalid signature"})  # ...signed by no account of ours
    assert world.repo.applied == [] and world.razorpay.count("fetch_payment") == 0


def test_webhook_captured_is_verified_server_side(client, world):
    out = paid_order(client, world)
    payment = world.razorpay.captured(out["razorpay_order_id"], 10000)
    raw = webhook_body("payment.captured", out["razorpay_order_id"], payment.id, 10000)
    r = post_webhook(client, raw, world.secret("PRIMARY_WEBHOOK_SECRET"))
    assert (r.status_code, r.json()) == (200, {"status": "completed"})
    assert world.razorpay.calls[-1] == ("fetch_payment", "primary", payment.id)
    assert world.repo.applied == [("don-1", "captured", payment.id, "authtest-evt-1")]


@pytest.mark.parametrize("reason", ["network", "auth", "bad_response"])
def test_webhook_provider_errors_are_502_for_redelivery(client, world, reason):
    out = paid_order(client, world)
    payment = world.razorpay.captured(out["razorpay_order_id"], 10000)
    world.razorpay.fail["fetch_payment"] = reason
    raw = webhook_body("payment.captured", out["razorpay_order_id"], payment.id, 10000)
    r = post_webhook(client, raw, world.secret("PRIMARY_WEBHOOK_SECRET"))
    assert (r.status_code, r.json()) == (502, {"error": "Payment verification unavailable"})
    assert world.repo.applied == [] and world.repo.events == []  # nothing recorded: the retry is processed


def test_webhook_payment_unknown_to_razorpay_is_a_mismatch(client, world):
    out = paid_order(client, world)
    raw = webhook_body("payment.captured", out["razorpay_order_id"], "pay_TESTnotthere", 10000)
    r = post_webhook(client, raw, world.secret("PRIMARY_WEBHOOK_SECRET"))
    assert r.json() == {"status": "failed"}
    assert world.repo.applied == [("don-1", "payment_not_found", None, "authtest-evt-1")]


def test_webhook_event_id_fallback_and_preflight(client, world):
    raw = b'{"event": "order.paid"}'
    post_webhook(client, raw, world.secret("PRIMARY_WEBHOOK_SECRET"), event_id=None)
    assert world.repo.events[-1][0] == "sha256:" + hashlib.sha256(raw).hexdigest()
    post_webhook(client, raw, world.secret("PRIMARY_WEBHOOK_SECRET"), event_id="../../etc")
    assert world.repo.events[-1][0].startswith("sha256:")
    r = client.options(f"{D}/x/status", headers={"origin": FRONT, "access-control-request-method": "GET",
                                                 "access-control-request-headers": "x-status-token"})
    assert r.status_code == 200 and "x-status-token" in r.headers["access-control-allow-headers"]
    assert r.headers["access-control-allow-origin"] == FRONT


def test_routes_unavailable_without_service(client):
    del client.app.state.payments
    assert checkout(client).status_code == 503
    assert client.post(W, content=b"{}").json() == {"error": "Payments are not available"}
