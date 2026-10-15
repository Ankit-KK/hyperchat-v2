"""Phase 7 payments on PostgreSQL: the real app, repository and completion transaction against a migrated
scratch database (see tests/integration.py). Razorpay is the scripted FakeRazorpay and R2 the in-memory
store; every Razorpay key/secret is a random TEST-ONLY value per test.

Covers: checkout rows (donation + order_lookup + token hash, health), rollback on Razorpay failure, the
webhook/confirm/poll completion paths, every state transition, idempotency (event ids, duplicate payments),
real concurrency (webhook vs confirm, duplicate deliveries) and log safety."""

import asyncio
import hashlib

import httpx
import psycopg
import pytest

from app.cli import connection_factory
from app.core.config import Settings
from app.maintenance import cleanup
from app.payments.accounts import AccountHealth
from app.soundboard.library import HypersoundLibrary
from app.storage.memory import InMemoryObjectStore, StoredObject
from tests.integration import (
    ORIGIN,
    creator_state,
    db,
    make_donation,
    make_streamer,
    make_user,
    requires_db,
    update_streamer,
)
from tests.payments.fakes import (
    OTHER_SECRET,
    PAYER_CONTACT,
    PAYER_EMAIL,
    FakeRazorpay,
    razorpay_env,
    sign,
    webhook_body,
)

pytestmark = requires_db
P = "/api/v1/public/streamers"
D = "/api/v1/public/donations"
W = "/api/v1/webhooks/razorpay"
HEX = "c" * 32


@pytest.fixture
def rz_env(monkeypatch):
    values = razorpay_env()
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def wire(state, fake: FakeRazorpay, store: InMemoryObjectStore) -> None:
    payments = state.payments
    payments.client, payments.health = fake, AccountHealth(fake)
    payments.store, payments.library = store, HypersoundLibrary(store)


@pytest.fixture
def client(rz_env, app):
    fake, store = FakeRazorpay(), InMemoryObjectStore()
    wire(app.app_state, fake, store)
    app.fake, app.store, app.env, app.tokens = fake, store, rz_env, []
    yield app
    # Everything the server logged. The test client's own httpx request lines are skipped: they echo URLs the
    # TEST chose (e.g. a token deliberately put in a query string to prove the server ignores it).
    logs = "\n".join(r for r in app.app_state.log_capture.records
                     if not ("'name': 'httpx'" in r and "https://testserver/" in r))
    for value in (*rz_env.values(), *app.tokens, PAYER_EMAIL, PAYER_CONTACT):
        assert value not in logs, "a secret, status token or payer detail reached the logs"


def secret(client_or_env, name: str) -> str:
    env = client_or_env if isinstance(client_or_env, dict) else client_or_env.env
    return env[f"HYPERCHAT_RAZORPAY_{name}"]


def checkout(client, slug: str, **kw) -> dict:
    r = client.post(f"{P}/{slug}/donations", json={"name": "Ravi", "amount": 100, **kw}, headers=ORIGIN)
    assert r.status_code == 201, r.text
    client.tokens.append(r.json()["status_token"])
    return r.json()


def webhook(client, event: str, out: dict, payment_id: str, amount: int = 10000, *, event_id: str,
            account: str = "PRIMARY", currency: str = "INR"):
    raw = webhook_body(event, out["razorpay_order_id"], payment_id, amount, currency,
                       "captured" if event == "payment.captured" else "failed")
    return client.post(W, content=raw, headers={"x-razorpay-signature": sign(secret(client, f"{account}_WEBHOOK_SECRET"),
                                                                             raw),
                                                "x-razorpay-event-id": event_id, "content-type": "application/json"})


def donation(order_id: str) -> dict:
    with db() as conn:
        cur = conn.execute("""
            SELECT d.payment_status, d.moderation_status, d.provider_payment_id, d.payment_account,
                   d.approved_by_label, d.approved_at IS NOT NULL AS approved,
                   EXTRACT(EPOCH FROM d.audio_scheduled_at - d.approved_at) AS audio_delay,
                   d.status_token_hash, d.upload_token_hash, d.amount, d.amount_inr, d.currency::text AS currency,
                   d.donor_name, d.message, d.media_type, d.provider_order_id, d.id::text AS id,
                   (SELECT count(*) FROM public.order_lookup o WHERE o.donation_id = d.id
                      AND o.razorpay_order_id = d.provider_order_id AND o.order_id = d.order_id) AS lookups
            FROM public.donations d WHERE d.order_id = %s""", (order_id,))
        row = cur.fetchone()
        return dict(zip([c.name for c in cur.description], row)) if row else None


def totals(streamer_id: str) -> list[tuple]:
    with db() as conn:
        return conn.execute("SELECT donator_name, total_amount, donation_count FROM public.streamer_donator_totals "
                            "WHERE streamer_id = %s", (streamer_id,)).fetchall()


def events(prefix: str = "authtest-") -> list[tuple]:
    with db() as conn:
        return conn.execute("SELECT event_id, event_type, account, outcome, provider_order_id, provider_payment_id, "
                            "donation_id IS NOT NULL FROM public.payment_webhook_events WHERE event_id LIKE %s "
                            "ORDER BY processed_at, event_id", (prefix + "%",)).fetchall()


# ── checkout ────────────────────────────────────────────────────────────────

def test_checkout_writes_donation_order_lookup_token_hash_and_health(client):
    sid = make_streamer("authtest-pay-co")
    out = checkout(client, "authtest-pay-co", amount="100.50", message="<b>Hi</b>")
    row = donation(out["order_id"])
    assert (row["payment_status"], row["moderation_status"], row["payment_account"]) == ("pending", "pending",
                                                                                          "primary")
    assert row["status_token_hash"] == hashlib.sha256(out["status_token"].encode()).hexdigest()
    assert row["upload_token_hash"] is None and row["provider_payment_id"] is None  # D5: no order-bound token
    assert (str(row["amount"]), str(row["amount_inr"]), row["currency"]) == ("100.50", "100.50", "INR")
    assert (row["donor_name"], row["message"], row["provider_order_id"]) == ("Ravi", "Hi", out["razorpay_order_id"])
    assert row["lookups"] == 1 and out["order_id"].startswith("authtest-pay-co_")
    with db() as conn:
        health = conn.execute("SELECT status, on_hold, error_message FROM public.streamer_payment_health "
                              "WHERE streamer_id = %s", (sid,)).fetchone()
        rate = conn.execute("SELECT request_count FROM public.rate_limits WHERE ip_address = 'ip:testclient' "
                            "AND endpoint = 'payment:checkout'").fetchone()
    assert health == ("ok", False, None) and rate == (1,)


def test_the_trigger_refuses_a_creator_that_became_ineligible_after_the_check(client):
    """Migration 0020: the API check passed (stubbed here), but the creator's owner had changed: the donations insert
    trigger refuses under the creator row lock. No donation, no order_lookup row; a generic 503."""
    sid = make_streamer("authtest-pay-race", user_id=make_user("pay-race-a@authtest.invalid"))
    update_streamer(sid, user_id=make_user("pay-race-b@authtest.invalid"))  # an unverified new owner

    async def stale_check(streamer_id):
        return None

    client.app_state.payments.public.repo.donation_block = stale_check
    r = client.post(f"{P}/authtest-pay-race/donations", json={"name": "Ravi", "amount": 100}, headers=ORIGIN)
    assert (r.status_code, r.json()) == (503, {"error": "Donations are temporarily unavailable"})
    assert len(client.fake.orders) == 1  # the order was created; it is never paid and expires
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.donations WHERE streamer_id = %s", (sid,)).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM public.order_lookup WHERE razorpay_order_id = %s",
                            (client.fake.orders[0]["id"],)).fetchone() == (0,)


def test_razorpay_order_failure_writes_no_donation(client):
    sid = make_streamer("authtest-pay-fail")
    client.fake.fail["create_order"] = "network"
    r = client.post(f"{P}/authtest-pay-fail/donations", json={"name": "Ravi", "amount": 100}, headers=ORIGIN)
    assert (r.status_code, r.json()) == (502, {"error": "Payment provider unavailable. Please try again."})
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.donations WHERE streamer_id = %s", (sid,)).fetchone() == (0,)


def test_creator_without_account_is_on_hold_and_recorded(client):
    sid = make_streamer("authtest-pay-hold")
    update_streamer(sid, razorpay_account=None)
    r = client.post(f"{P}/authtest-pay-hold/donations", json={"name": "Ravi", "amount": 100}, headers=ORIGIN)
    assert (r.status_code, r.json()) == (503, {"error": "Payments for this creator are on hold"})
    with db() as conn:
        assert conn.execute("SELECT status, on_hold, error_message FROM public.streamer_payment_health WHERE "
                            "streamer_id = %s", (sid,)).fetchone() == (
            "missing_key", True, "razorpay credentials are not configured")
    assert client.fake.calls == []


def test_secondary_is_retired_and_foreign_webhooks_are_refused(client):
    """D6: one account. The database refuses 'secondary'; a webhook signed by any other secret is refused and the
    order stays pending; the order records the primary account it was created on."""
    sid = make_streamer("authtest-pay-sec")
    with pytest.raises(psycopg.errors.CheckViolation):
        update_streamer(sid, razorpay_account="secondary")
    out = checkout(client, "authtest-pay-sec")
    assert out["razorpay_key_id"] == secret(client, "PRIMARY_KEY_ID")
    payment = client.fake.captured(out["razorpay_order_id"], 10000)
    raw = webhook_body("payment.captured", out["razorpay_order_id"], payment.id, 10000, "INR", "captured")
    r = client.post(W, content=raw, headers={"x-razorpay-signature": sign(OTHER_SECRET, raw),
                                             "x-razorpay-event-id": "authtest-sec-1",
                                             "content-type": "application/json"})
    assert r.status_code == 401 and donation(out["order_id"])["payment_status"] == "pending"
    assert webhook(client, "payment.captured", out, payment.id, event_id="authtest-sec-2").json() == {
        "status": "completed"}
    assert client.fake.calls[-1] == ("fetch_payment", "primary", payment.id)
    assert donation(out["order_id"])["payment_account"] == "primary"
    assert [e[2:4] for e in events("authtest-sec-")] == [("primary", "completed")]


# ── webhook transitions and idempotency ─────────────────────────────────────

def test_captured_webhook_completes_once_and_is_idempotent(client):
    sid = make_streamer("authtest-pay-wh")
    out = checkout(client, "authtest-pay-wh")
    payment = client.fake.captured(out["razorpay_order_id"], 10000)
    r = webhook(client, "payment.captured", out, payment.id, event_id="authtest-wh-1")
    assert (r.status_code, r.json()) == (200, {"status": "completed"})
    row = donation(out["order_id"])
    assert (row["payment_status"], row["moderation_status"], row["provider_payment_id"]) == (
        "success", "auto_approved", payment.id)
    assert (row["approved"], row["approved_by_label"], float(row["audio_delay"])) == (True, "system", 10.0)
    assert totals(sid) == [("Ravi", 100, 1)]
    fetches = client.fake.count("fetch_payment")
    r = webhook(client, "payment.captured", out, payment.id, event_id="authtest-wh-1")  # redelivery
    assert r.json() == {"status": "duplicate"} and client.fake.count("fetch_payment") == fetches
    r = webhook(client, "payment.captured", out, payment.id, event_id="authtest-wh-2")  # same payment, new event
    assert r.json() == {"status": "already_paid"}
    assert totals(sid) == [("Ravi", 100, 1)]  # credited exactly once
    assert events("authtest-wh-") == [
        ("authtest-wh-1", "payment.captured", "primary", "completed", out["razorpay_order_id"], payment.id, True),
        ("authtest-wh-2", "payment.captured", "primary", "already_paid", out["razorpay_order_id"], payment.id, True)]


def test_webhook_still_completes_after_the_order_lookup_row_was_cleaned_up(client):
    """maintenance deletes order_lookup rows after 30 days (legacy cleanup-order-lookup); a late webhook for such an
    order still finds its donation through donations.provider_order_id."""
    make_streamer("authtest-pay-oldlookup")
    out = checkout(client, "authtest-pay-oldlookup")
    with db() as conn:
        conn.execute("UPDATE public.order_lookup SET created_at = now() - interval '31 days' "
                     "WHERE razorpay_order_id = %s", (out["razorpay_order_id"],))
    assert asyncio.run(cleanup(connection_factory(Settings()), dry_run=False))["order_lookup"] >= 1
    assert donation(out["order_id"])["lookups"] == 0
    payment = client.fake.captured(out["razorpay_order_id"], 10000)
    r = webhook(client, "payment.captured", out, payment.id, event_id="authtest-wh-oldlookup")
    assert (r.status_code, r.json()) == (200, {"status": "completed"})
    assert donation(out["order_id"])["payment_status"] == "success"


def test_failed_then_captured_becomes_paid(client):
    sid = make_streamer("authtest-pay-fc")
    out = checkout(client, "authtest-pay-fc")
    r = webhook(client, "payment.failed", out, "pay_TESTfirst", event_id="authtest-fc-1")
    assert r.json() == {"status": "failed"}
    assert donation(out["order_id"])["payment_status"] == "failed"
    assert donation(out["order_id"])["moderation_status"] == "rejected"
    r = webhook(client, "payment.failed", out, "pay_TESTfirst", event_id="authtest-fc-2")
    assert r.json() == {"status": "already_failed"}
    retry = client.fake.captured(out["razorpay_order_id"], 10000)  # the donor retried and paid
    r = webhook(client, "payment.captured", out, retry.id, event_id="authtest-fc-3")
    assert r.json() == {"status": "completed"}
    row = donation(out["order_id"])
    assert (row["payment_status"], row["moderation_status"], row["provider_payment_id"]) == (
        "success", "auto_approved", retry.id)
    assert totals(sid) == [("Ravi", 100, 1)]


def test_captured_then_failed_stays_paid(client):
    sid = make_streamer("authtest-pay-cf")
    out = checkout(client, "authtest-pay-cf")
    payment = client.fake.captured(out["razorpay_order_id"], 10000)
    assert webhook(client, "payment.captured", out, payment.id, event_id="authtest-cf-1").json() == {
        "status": "completed"}
    before = creator_state(sid)
    r = webhook(client, "payment.failed", out, "pay_TESTlate", event_id="authtest-cf-2")
    assert r.json() == {"status": "already_paid"} and creator_state(sid) == before


@pytest.mark.parametrize("amount, currency, outcome", [(9999, "INR", "amount_mismatch"),
                                                       (10001, "INR", "amount_mismatch"),
                                                       (10000, "USD", "currency_mismatch")])
def test_verified_mismatch_fails_the_donation_and_answers_200(client, amount, currency, outcome):
    sid = make_streamer("authtest-pay-mm")
    out = checkout(client, "authtest-pay-mm")
    payment = client.fake.captured(out["razorpay_order_id"], amount, currency)
    r = webhook(client, "payment.captured", out, payment.id, amount, event_id="authtest-mm-1", currency=currency)
    assert (r.status_code, r.json()) == (200, {"status": "failed"})
    row = donation(out["order_id"])
    assert (row["payment_status"], row["moderation_status"], row["provider_payment_id"]) == (
        "failed", "rejected", None)
    assert totals(sid) == [] and events("authtest-mm-")[0][3] == outcome


def test_unknown_order_answers_200_and_is_recorded(client):
    raw = webhook_body("payment.captured", "order_TESTnotours", "pay_TESTx", 10000)
    r = client.post(W, content=raw, headers={"x-razorpay-signature": sign(secret(client, "PRIMARY_WEBHOOK_SECRET"), raw),
                                             "x-razorpay-event-id": "authtest-unk-1"})
    assert (r.status_code, r.json()) == (200, {"status": "unknown_order"})
    assert events("authtest-unk-") == [("authtest-unk-1", "payment.captured", "primary", "unknown_order",
                                        "order_TESTnotours", "pay_TESTx", False)]


def test_bad_signature_changes_nothing(client):
    sid = make_streamer("authtest-pay-sig")
    out = checkout(client, "authtest-pay-sig")
    payment = client.fake.captured(out["razorpay_order_id"], 10000)
    before = creator_state(sid)
    raw = webhook_body("payment.captured", out["razorpay_order_id"], payment.id, 10000)
    for sig in ("0" * 64, sign("TEST-ONLY-wrong", raw), sign(secret(client, "PRIMARY_KEY_SECRET"), raw), None):
        headers = {"x-razorpay-event-id": "authtest-sig-1", **({"x-razorpay-signature": sig} if sig else {})}
        assert client.post(W, content=raw, headers=headers).status_code == 401
    tampered = raw.replace(b'"amount": 10000', b'"amount": 1')
    assert client.post(W, content=tampered, headers={
        "x-razorpay-signature": sign(secret(client, "PRIMARY_WEBHOOK_SECRET"), raw)}).status_code == 401
    assert creator_state(sid) == before and events("authtest-sig-") == []
    assert client.fake.count("fetch_payment") == 0


def test_provider_error_is_502_and_the_redelivery_completes(client):
    sid = make_streamer("authtest-pay-502")
    out = checkout(client, "authtest-pay-502")
    payment = client.fake.captured(out["razorpay_order_id"], 10000)
    client.fake.fail["fetch_payment"] = "network"
    r = webhook(client, "payment.captured", out, payment.id, event_id="authtest-502-1")
    assert r.status_code == 502 and events("authtest-502-") == []
    assert donation(out["order_id"])["payment_status"] == "pending"
    del client.fake.fail["fetch_payment"]
    r = webhook(client, "payment.captured", out, payment.id, event_id="authtest-502-1")  # Razorpay retries
    assert r.json() == {"status": "completed"} and totals(sid) == [("Ravi", 100, 1)]


def test_apply_on_a_vanished_donation_records_unknown_order(client):
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime

    import psycopg

    from app.payments.repository import PaymentRepository, WebhookEvent
    from tests.integration import PG_HOST, target

    @asynccontextmanager
    async def connection():
        name, port = target()
        conn = await psycopg.AsyncConnection.connect(f"host={PG_HOST} port={port} dbname={name} user=hyperchat_app")
        try:
            yield conn
        finally:
            await conn.close()

    event = WebhookEvent("authtest-gone-1", "payment.captured", "primary", "order_TESTgone", "pay_TESTgone",
                         datetime.now(UTC))
    applied = asyncio.run(PaymentRepository(connection).apply("00000000-0000-4000-8000-000000000000", "captured",
                                                              "pay_TESTgone", event))
    assert (applied.result, applied.payment_status) == ("unknown_order", None)
    assert events("authtest-gone-") == [("authtest-gone-1", "payment.captured", "primary", "unknown_order",
                                         "order_TESTgone", "pay_TESTgone", False)]


# ── confirm and polling ─────────────────────────────────────────────────────

def confirm(client, out, payment_id, signature=None):
    good = sign(secret(client, "PRIMARY_KEY_SECRET"), f"{out['razorpay_order_id']}|{payment_id}".encode())
    return client.post(f"{D}/{out['order_id']}/confirm",
                       json={"razorpay_payment_id": payment_id, "razorpay_signature": signature or good},
                       headers={**ORIGIN, "x-status-token": out["status_token"]})


def test_confirm_completes_the_donation(client):
    sid = make_streamer("authtest-pay-cfm")
    out = checkout(client, "authtest-pay-cfm")
    payment = client.fake.captured(out["razorpay_order_id"], 10000)
    r = confirm(client, out, payment.id, signature="1" * 64)
    assert (r.status_code, r.json()) == (400, {"error": "Invalid payment signature"})
    assert donation(out["order_id"])["payment_status"] == "pending"
    r = confirm(client, out, payment.id)
    assert r.json() == {"order_id": out["order_id"], "payment_status": "success", "amount": 100.0, "currency": "INR"}
    assert donation(out["order_id"])["provider_payment_id"] == payment.id and totals(sid) == [("Ravi", 100, 1)]
    fetches = client.fake.count("fetch_payment")
    assert confirm(client, out, payment.id).json()["payment_status"] == "success"
    assert client.fake.count("fetch_payment") == fetches  # already paid: no Razorpay call
    r = webhook(client, "payment.captured", out, payment.id, event_id="authtest-cfm-1")
    assert r.json() == {"status": "already_paid"} and totals(sid) == [("Ravi", 100, 1)]


def test_polling_completes_the_donation_with_a_cooldown(client):
    sid = make_streamer("authtest-pay-poll")
    out = checkout(client, "authtest-pay-poll")
    url, headers = f"{D}/{out['order_id']}/status", {"x-status-token": out["status_token"]}
    assert client.get(url, headers=headers).json()["payment_status"] == "pending"
    client.fake.captured(out["razorpay_order_id"], 10000)
    assert client.get(url, headers=headers).json()["payment_status"] == "pending"  # within the 5 s cooldown
    assert client.fake.count("order_payments") == 1
    with db() as conn:
        conn.execute("UPDATE public.rate_limits SET window_start = now() - interval '6 seconds' "
                     "WHERE ip_address = %s AND endpoint = 'payment:poll'", (f"order:{out['order_id']}",))
    assert client.get(url, headers=headers).json()["payment_status"] == "success"
    assert totals(sid) == [("Ravi", 100, 1)] and donation(out["order_id"])["moderation_status"] == "auto_approved"


def test_status_tokens_on_real_rows(client):
    sid = make_streamer("authtest-pay-tok")
    out = checkout(client, "authtest-pay-tok")
    url = f"{D}/{out['order_id']}/status"
    for headers in ({}, {"x-status-token": "0" * 64}):
        assert client.get(url, headers=headers).status_code == 404
    assert client.get(f"{url}?status_token={out['status_token']}").status_code == 404
    legacy = make_donation(sid, payment="pending")  # a legacy row: no status_token_hash (D4)
    with db() as conn:
        order_id = conn.execute("SELECT order_id FROM public.donations WHERE id = %s", (legacy,)).fetchone()[0]
    assert client.get(f"{D}/{order_id}/status", headers={"x-status-token": out["status_token"]}).status_code == 404


# ── the auto-approve rule on real creator settings ──────────────────────────

def upload(client, slug: str, kind: str) -> str:
    if kind == "voice":
        key = f"voice-messages/{slug}_1800000000000_{HEX}.webm"
        client.store.objects[key] = StoredObject(b"v", "audio/webm", "", {"duration-ms": "5000"})
    else:
        key = f"donation-media/{slug}_1800000000000_{HEX}.png"
        client.store.objects[key] = StoredObject(b"m", "image/png", "", {})
    return client.store.public_url(key)


@pytest.mark.parametrize("cols, attach, status, credited", [
    ({"moderation_mode": "manual"}, None, "pending", False),
    ({}, "voice", "pending", False),                                  # D2: voice held when media moderation is on
    ({}, "media", "pending", False),
    ({"media_moderation_enabled": False}, "voice", "auto_approved", True),
    ({"moderation_mode": "manual"}, "hypersound", "auto_approved", True),
])
def test_auto_approve_rule_on_completion(client, cols, attach, status, credited):
    slug = "authtest-pay-rule"
    sid = make_streamer(slug)
    update_streamer(sid, media_upload_enabled=True, **cols)
    kw = {"amount": 200}
    if attach in ("voice", "media"):
        kw[f"{attach}_message_url" if attach == "voice" else "media_url"] = upload(client, slug, attach)
    elif attach == "hypersound":
        client.store.seed_library("hypersounds/Horn.mp3")
        kw["hypersound_url"] = client.store.public_url("hypersounds/Horn.mp3")
    out = checkout(client, slug, **kw)
    payment = client.fake.captured(out["razorpay_order_id"], 20000)
    assert webhook(client, "payment.captured", out, payment.id, 20000, event_id="authtest-rule-1").json() == {
        "status": "completed"}
    row = donation(out["order_id"])
    assert (row["payment_status"], row["moderation_status"]) == ("success", status)
    assert (row["audio_delay"] is not None, row["approved"]) == (credited, credited)
    assert totals(sid) == ([("Ravi", 200, 1)] if credited else [])
    if attach == "media":
        assert row["media_type"] == "image"


# ── concurrency: one completion, one credit ─────────────────────────────────

def race(slug: str, rz: dict, build_requests):
    """Run the real app in this test's own event loop (its own pool) and send requests concurrently.
    The fake's barrier holds every payment fetch until all requests have fetched, so they reach the
    completion transaction together."""
    async def scenario():
        from app.main import create_app

        application = create_app()
        async with application.router.lifespan_context(application):
            fake, store = FakeRazorpay(), InMemoryObjectStore()
            wire(application.state, fake, store)
            transport = httpx.ASGITransport(app=application, client=("203.0.113.9", 4000))
            async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
                r = await c.post(f"{P}/{slug}/donations", json={"name": "Ravi", "amount": 100}, headers=ORIGIN)
                assert r.status_code == 201, r.text
                out = r.json()
                payment = fake.captured(out["razorpay_order_id"], 10000)
                requests = build_requests(out, payment)
                fake.barrier = asyncio.Barrier(len(requests))
                responses = await asyncio.gather(*(c.request(m, u, **kw) for m, u, kw in requests))
                return out, payment, responses, fake
    return asyncio.run(scenario())


def signed(rz: dict, raw: bytes, event_id: str) -> dict:
    return {"content": raw, "headers": {"x-razorpay-signature": sign(rz["HYPERCHAT_RAZORPAY_PRIMARY_WEBHOOK_SECRET"],
                                                                     raw), "x-razorpay-event-id": event_id}}


def test_concurrent_webhook_and_confirm_complete_exactly_once(rz_env, env):
    sid = make_streamer("authtest-pay-race")

    def requests(out, payment):
        raw = webhook_body("payment.captured", out["razorpay_order_id"], payment.id, 10000)
        good = sign(rz_env["HYPERCHAT_RAZORPAY_PRIMARY_KEY_SECRET"], f"{out['razorpay_order_id']}|{payment.id}".encode())
        return [("POST", W, signed(rz_env, raw, "authtest-race-1")),
                ("POST", f"{D}/{out['order_id']}/confirm",
                 {"json": {"razorpay_payment_id": payment.id, "razorpay_signature": good},
                  "headers": {**ORIGIN, "x-status-token": out["status_token"]}})]
    out, payment, (wh, cf), fake = race("authtest-pay-race", rz_env, requests)
    assert fake.count("fetch_payment") == 2  # both verified with Razorpay, then raced on the row lock
    assert wh.status_code == 200 and cf.status_code == 200
    assert cf.json()["payment_status"] == "success" and wh.json()["status"] in ("completed", "already_paid")
    assert totals(sid) == [("Ravi", 100, 1)]
    row = donation(out["order_id"])
    assert (row["payment_status"], row["provider_payment_id"]) == ("success", payment.id)
    assert [e[3] for e in events("authtest-race-")] == [wh.json()["status"]]


def test_concurrent_duplicate_deliveries_process_once(rz_env, env):
    sid = make_streamer("authtest-pay-dup")

    def requests(out, payment):
        raw = webhook_body("payment.captured", out["razorpay_order_id"], payment.id, 10000)
        return [("POST", W, signed(rz_env, raw, "authtest-dup-1")) for _ in range(3)]
    out, payment, responses, fake = race("authtest-pay-dup", rz_env, requests)
    assert fake.count("fetch_payment") == 3  # all three passed the seen-check before any committed
    assert sorted(r.json()["status"] for r in responses) == ["completed", "duplicate", "duplicate"]
    assert totals(sid) == [("Ravi", 100, 1)]
    assert [e[3] for e in events("authtest-dup-")] == ["completed"]
