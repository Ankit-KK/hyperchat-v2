"""Test doubles for Phase 7: a scripted Razorpay client and payload/signature helpers. Every key, secret and
id here is a TEST-ONLY value; nothing talks to Razorpay."""

import asyncio
import hashlib
import hmac
import itertools
import json
import secrets

from app.payments.razorpay import Order, Payment, RazorpayError

PAYER_EMAIL = "payer.secret@example.invalid"  # payer details inside webhook payloads: must never be logged
PAYER_CONTACT = "+919999012345"


def razorpay_env() -> dict[str, str]:
    """Random TEST-ONLY Razorpay settings (env var name -> value) for both accounts."""
    values = {}
    for account in ("PRIMARY", "SECONDARY"):
        values[f"HYPERCHAT_RAZORPAY_{account}_KEY_ID"] = f"rzp_test_TESTONLY{account[0]}{secrets.token_hex(4)}"
        values[f"HYPERCHAT_RAZORPAY_{account}_KEY_SECRET"] = f"TEST-ONLY-ks-{secrets.token_hex(12)}"
        values[f"HYPERCHAT_RAZORPAY_{account}_WEBHOOK_SECRET"] = f"TEST-ONLY-wh-{secrets.token_hex(12)}"
    return values


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def webhook_body(event: str, order_id: str | None, payment_id: str | None, amount: int, currency: str = "INR",
                 status: str = "captured") -> bytes:
    entity = {"id": payment_id, "entity": "payment", "amount": amount, "currency": currency, "status": status,
              "order_id": order_id, "method": "upi", "vpa": "payer@okaxis", "email": PAYER_EMAIL,
              "contact": PAYER_CONTACT, "notes": [], "fee": 0}
    return json.dumps({"entity": "event", "account_id": "acc_TESTONLY", "event": event, "contains": ["payment"],
                       "payload": {"payment": {"entity": entity}}, "created_at": 1_800_000_000}).encode()


class FakeRazorpay:
    """Orders get ids order_TEST<n>; payments are scripted per id. `barrier` (an asyncio.Barrier created in the
    app's loop) makes concurrent fetches line up so their completions really race."""

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.orders: list[dict] = []
        self.payments: dict[str, Payment] = {}
        self.order_payment_lists: dict[str, list[Payment]] = {}
        self.fail: dict[str, str] = {}  # operation -> RazorpayError reason
        self.probe_result = "ok"
        self.calls: list[tuple] = []
        self.barrier: asyncio.Barrier | None = None

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail:
            raise RazorpayError(self.fail[op])

    async def create_order(self, account, *, amount, currency, receipt) -> Order:
        self.calls.append(("create_order", account.name, amount, currency, receipt))
        self._maybe_fail("create_order")
        order = Order(f"order_TEST{next(self._ids)}{secrets.token_hex(3)}", amount, currency, "created")
        self.orders.append({"id": order.id, "account": account.name, "amount": amount, "currency": currency,
                            "receipt": receipt})
        return order

    async def fetch_payment(self, account, payment_id) -> Payment:
        self.calls.append(("fetch_payment", account.name, payment_id))
        if self.barrier is not None:
            await self.barrier.wait()
        self._maybe_fail("fetch_payment")
        if payment_id not in self.payments:
            raise RazorpayError("not_found")
        return self.payments[payment_id]

    async def order_payments(self, account, order_id) -> list[Payment]:
        self.calls.append(("order_payments", account.name, order_id))
        self._maybe_fail("order_payments")
        return list(self.order_payment_lists.get(order_id, []))

    async def fetch_order(self, account, order_id) -> Order:
        raise AssertionError("not used by the service")

    async def probe(self, account) -> str:
        self.calls.append(("probe", account.name))
        return self.probe_result

    def count(self, op: str) -> int:
        return sum(1 for c in self.calls if c[0] == op)

    def captured(self, order_id: str, amount: int, currency: str = "INR", status: str = "captured",
                 payment_id: str | None = None) -> Payment:
        payment = Payment(payment_id or f"pay_TEST{secrets.token_hex(5)}", order_id, status, amount, currency)
        self.payments[payment.id] = payment
        self.order_payment_lists.setdefault(order_id, []).append(payment)
        return payment
