"""Razorpay REST client (Orders and Payments API) and signature checks.

Credentials are only handed to httpx as basic auth: they are never logged, never part of an exception or a
repr. Every call has a 10 second timeout and nothing is retried here: a retried order creation could open a
second order, and callers of the reads decide themselves (a webhook answers 502 so Razorpay redelivers).
Failures raise RazorpayError whose str() is one secret-free reason:

    auth          401/403: the key id/secret were refused
    not_found     404: no such order/payment on this account
    rejected      400: Razorpay refused the request (e.g. an amount it does not accept)
    network       timeout, connection error, 429 or 5xx: worth retrying later
    bad_response  any other status, or a body that is not the expected JSON

Ids are checked against Razorpay's formats before they become part of a URL path.
"""

import hashlib
import hmac
import re
from dataclasses import dataclass

import httpx
from pydantic import SecretStr

API_BASE = "https://api.razorpay.com/v1"
TIMEOUT_SECONDS = 10.0
ORDER_ID = re.compile(r"^order_[A-Za-z0-9]{1,40}$")
PAYMENT_ID = re.compile(r"^pay_[A-Za-z0-9]{1,40}$")
CURRENCY = re.compile(r"^[A-Z]{3}$")
_HEX_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class RazorpayError(Exception):
    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason, self.status = reason, status


@dataclass(frozen=True, repr=False)
class RazorpayAccount:
    """One configured account. key_id is public (Razorpay Checkout needs it); the secrets are not."""
    name: str
    key_id: str
    key_secret: SecretStr
    webhook_secret: SecretStr | None

    def __repr__(self) -> str:
        return f"RazorpayAccount({self.name}, <redacted>)"


@dataclass(frozen=True)
class Order:
    id: str
    amount: int  # subunits (paise / cents)
    currency: str
    status: str


@dataclass(frozen=True)
class Payment:
    id: str
    order_id: str | None
    status: str  # created | authorized | captured | refunded | failed
    amount: int  # subunits
    currency: str


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_order(data) -> Order:
    if not isinstance(data, dict):
        raise RazorpayError("bad_response")
    oid, amount, currency, status = data.get("id"), _int(data.get("amount")), data.get("currency"), data.get("status")
    if (not isinstance(oid, str) or not ORDER_ID.match(oid) or amount is None or not isinstance(currency, str)
            or not CURRENCY.match(currency) or not isinstance(status, str)):
        raise RazorpayError("bad_response")
    return Order(oid, amount, currency, status)


def parse_payment(data) -> Payment:
    if not isinstance(data, dict):
        raise RazorpayError("bad_response")
    pid, order_id = data.get("id"), data.get("order_id")
    amount, currency, status = _int(data.get("amount")), data.get("currency"), data.get("status")
    if (not isinstance(pid, str) or not PAYMENT_ID.match(pid) or not (order_id is None or isinstance(order_id, str))
            or amount is None or not isinstance(currency, str) or not CURRENCY.match(currency)
            or not isinstance(status, str)):
        raise RazorpayError("bad_response")
    return Payment(pid, order_id, status, amount, currency)


class RazorpayClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, base_url: str = API_BASE,
                 timeout: float = TIMEOUT_SECONDS) -> None:
        self._transport, self._base_url, self._timeout = transport, base_url, timeout

    async def _request(self, account: RazorpayAccount, method: str, path: str, *, json: dict | None = None,
                       params: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(base_url=self._base_url, transport=self._transport, timeout=self._timeout,
                                         auth=(account.key_id, account.key_secret.get_secret_value()),
                                         follow_redirects=False) as client:
                response = await client.request(method, path, json=json, params=params)
        except httpx.HTTPError:
            raise RazorpayError("network") from None
        status = response.status_code
        if status in (401, 403):
            raise RazorpayError("auth", status)
        if status == 404:
            raise RazorpayError("not_found", status)
        if status == 400:
            raise RazorpayError("rejected", status)
        if status == 429 or status >= 500:
            raise RazorpayError("network", status)
        if not 200 <= status < 300:
            raise RazorpayError("bad_response", status)
        try:
            data = response.json()
        except ValueError:
            raise RazorpayError("bad_response", status) from None
        if not isinstance(data, dict):
            raise RazorpayError("bad_response", status)
        return data

    async def create_order(self, account: RazorpayAccount, *, amount: int, currency: str, receipt: str) -> Order:
        """POST /orders. Never retried. The returned order must be exactly the one requested."""
        order = parse_order(await self._request(account, "POST", "/orders",
                                                json={"amount": amount, "currency": currency, "receipt": receipt}))
        if order.amount != amount or order.currency != currency:
            raise RazorpayError("bad_response")
        return order

    async def fetch_order(self, account: RazorpayAccount, order_id: str) -> Order:
        if not ORDER_ID.match(order_id or ""):
            raise RazorpayError("not_found")
        return parse_order(await self._request(account, "GET", f"/orders/{order_id}"))

    async def order_payments(self, account: RazorpayAccount, order_id: str) -> list[Payment]:
        if not ORDER_ID.match(order_id or ""):
            raise RazorpayError("not_found")
        items = (await self._request(account, "GET", f"/orders/{order_id}/payments")).get("items")
        if not isinstance(items, list):
            raise RazorpayError("bad_response")
        return [parse_payment(item) for item in items]

    async def fetch_payment(self, account: RazorpayAccount, payment_id: str) -> Payment:
        if not PAYMENT_ID.match(payment_id or ""):
            raise RazorpayError("not_found")
        return parse_payment(await self._request(account, "GET", f"/payments/{payment_id}"))

    async def probe(self, account: RazorpayAccount) -> str:
        """Credential health (legacy probeRazorpayCredentials): "ok", "invalid_key" or "network_error"."""
        try:
            await self._request(account, "GET", "/payments", params={"count": 1})
        except RazorpayError as exc:
            return "invalid_key" if exc.reason == "auth" else "network_error"
        return "ok"


def signature_matches(secret: str, message: bytes, signature: str | None) -> bool:
    """HMAC-SHA256(secret, message) as hex, compared in constant time. Malformed signatures never match."""
    if not secret or not isinstance(signature, str) or not _HEX_SHA256.match(signature):
        return False
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


def checkout_signature_matches(key_secret: str, order_id: str, payment_id: str, signature: str | None) -> bool:
    """Razorpay Checkout's razorpay_signature: HMAC-SHA256(key secret, "<order id>|<payment id>")."""
    return signature_matches(key_secret, f"{order_id}|{payment_id}".encode(), signature)
