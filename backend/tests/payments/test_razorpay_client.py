"""Razorpay client against httpx.MockTransport: requests, failure classification, no retries, no secrets in
errors. No network; the credentials are TEST-ONLY literals."""

import asyncio
import base64
import json

import httpx
import pytest
from pydantic import SecretStr

from app.payments.razorpay import RazorpayAccount, RazorpayClient, RazorpayError

KEY_ID, KEY_SECRET = "rzp_test_TESTONLYkey", "TEST-ONLY-key-secret"
ACCOUNT = RazorpayAccount("primary", KEY_ID, SecretStr(KEY_SECRET), None)
ORDER = {"id": "order_ABC123", "entity": "order", "amount": 4000, "currency": "INR", "status": "created",
         "receipt": "r"}
PAYMENT = {"id": "pay_XYZ789", "entity": "payment", "amount": 4000, "currency": "INR", "status": "captured",
           "order_id": "order_ABC123", "email": "payer@example.invalid", "contact": "+910000000000"}


class Recorder:
    def __init__(self, responder):
        self.responder, self.requests = responder, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)


def client_for(responder) -> tuple[RazorpayClient, Recorder]:
    rec = Recorder(responder)
    return RazorpayClient(transport=httpx.MockTransport(rec)), rec


def run(coro):
    return asyncio.run(coro)


def reason(coro) -> str:
    with pytest.raises(RazorpayError) as exc:
        run(coro)
    assert KEY_SECRET not in str(exc.value) and KEY_ID not in str(exc.value) and exc.value.__cause__ is None
    return exc.value.reason


def test_create_order_request_and_basic_auth():
    client, rec = client_for(lambda r: httpx.Response(200, json=ORDER))
    order = run(client.create_order(ACCOUNT, amount=4000, currency="INR", receipt="ankit_rp_1_abcdefgh"))
    assert (order.id, order.amount, order.currency) == ("order_ABC123", 4000, "INR")
    (req,) = rec.requests
    assert (req.method, str(req.url)) == ("POST", "https://api.razorpay.com/v1/orders")
    assert json.loads(req.content) == {"amount": 4000, "currency": "INR", "receipt": "ankit_rp_1_abcdefgh"}
    assert req.headers["authorization"] == "Basic " + base64.b64encode(f"{KEY_ID}:{KEY_SECRET}".encode()).decode()


@pytest.mark.parametrize("status", [500, 502, 503, 429])
def test_order_creation_is_never_retried(status):
    client, rec = client_for(lambda r: httpx.Response(status, json={"error": {"description": "x"}}))
    assert reason(client.create_order(ACCOUNT, amount=4000, currency="INR", receipt="r")) == "network"
    assert len(rec.requests) == 1


def test_order_must_be_exactly_the_one_requested():
    client, _ = client_for(lambda r: httpx.Response(200, json={**ORDER, "amount": 3999}))
    assert reason(client.create_order(ACCOUNT, amount=4000, currency="INR", receipt="r")) == "bad_response"
    client, _ = client_for(lambda r: httpx.Response(200, json={**ORDER, "currency": "USD"}))
    assert reason(client.create_order(ACCOUNT, amount=4000, currency="INR", receipt="r")) == "bad_response"


@pytest.mark.parametrize("status, expected", [(401, "auth"), (403, "auth"), (404, "not_found"), (400, "rejected"),
                                              (429, "network"), (500, "network"), (504, "network"),
                                              (302, "bad_response"), (418, "bad_response"), (204, "bad_response")])
def test_status_classification(status, expected):
    client, _ = client_for(lambda r: httpx.Response(status, headers={"location": "https://evil.invalid/"}))
    assert reason(client.fetch_payment(ACCOUNT, "pay_XYZ789")) == expected


@pytest.mark.parametrize("exc", [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.ConnectError("c"),
                                 httpx.RemoteProtocolError("p")])
def test_transport_failures_are_network(exc):
    def boom(request):
        raise exc
    client, _ = client_for(boom)
    assert reason(client.fetch_payment(ACCOUNT, "pay_XYZ789")) == "network"


@pytest.mark.parametrize("body", [b"not json", b"[]", b'"x"', json.dumps({**PAYMENT, "amount": "4000"}).encode(),
                                  json.dumps({**PAYMENT, "amount": True}).encode(),
                                  json.dumps({**PAYMENT, "id": "pay_../x"}).encode(),
                                  json.dumps({**PAYMENT, "currency": "inr"}).encode(),
                                  json.dumps({k: v for k, v in PAYMENT.items() if k != "status"}).encode()])
def test_bad_bodies(body):
    client, _ = client_for(lambda r: httpx.Response(200, content=body))
    assert reason(client.fetch_payment(ACCOUNT, "pay_XYZ789")) == "bad_response"


def test_fetch_payment_and_order_payments_and_fetch_order():
    def respond(request):
        path = request.url.path
        if path == "/v1/payments/pay_XYZ789":
            return httpx.Response(200, json=PAYMENT)
        if path == "/v1/orders/order_ABC123/payments":
            return httpx.Response(200, json={"entity": "collection", "count": 2,
                                             "items": [PAYMENT, {**PAYMENT, "id": "pay_FAIL1", "status": "failed"}]})
        if path == "/v1/orders/order_ABC123":
            return httpx.Response(200, json={**ORDER, "status": "paid"})
        return httpx.Response(404)
    client, rec = client_for(respond)
    p = run(client.fetch_payment(ACCOUNT, "pay_XYZ789"))
    assert (p.id, p.order_id, p.status, p.amount, p.currency) == ("pay_XYZ789", "order_ABC123", "captured", 4000, "INR")
    assert [x.status for x in run(client.order_payments(ACCOUNT, "order_ABC123"))] == ["captured", "failed"]
    assert run(client.fetch_order(ACCOUNT, "order_ABC123")).status == "paid"
    assert all(r.method == "GET" for r in rec.requests)


@pytest.mark.parametrize("bad", ["", "pay_", "pay_../../orders", "pay_x/y", "order_ABC", "pay_X?y=1", "PAY_ABC",
                                 "pay_X%2F..", None])
def test_payment_ids_are_checked_before_any_request(bad):
    client, rec = client_for(lambda r: httpx.Response(200, json=PAYMENT))
    assert reason(client.fetch_payment(ACCOUNT, bad)) == "not_found"
    assert rec.requests == []


@pytest.mark.parametrize("bad", ["", "order_", "order_../../payments", "order_x/y", "pay_ABC", "order_X#y",
                                 "ORDER_ABC", None])
def test_order_ids_are_checked_before_any_request(bad):
    client, rec = client_for(lambda r: httpx.Response(200, json=ORDER))
    assert reason(client.order_payments(ACCOUNT, bad)) == "not_found"
    assert reason(client.fetch_order(ACCOUNT, bad)) == "not_found"
    assert rec.requests == []


def test_order_payments_requires_items_list():
    client, _ = client_for(lambda r: httpx.Response(200, json={"entity": "collection"}))
    assert reason(client.order_payments(ACCOUNT, "order_ABC123")) == "bad_response"


@pytest.mark.parametrize("status, expected", [(200, "ok"), (401, "invalid_key"), (403, "invalid_key"),
                                              (500, "network_error"), (404, "network_error")])
def test_probe(status, expected):
    client, rec = client_for(lambda r: httpx.Response(status, json={"entity": "collection", "items": []}))
    assert run(client.probe(ACCOUNT)) == expected
    assert str(rec.requests[0].url) == "https://api.razorpay.com/v1/payments?count=1"


def test_probe_network_failure():
    def boom(request):
        raise httpx.ConnectError("down")
    client, _ = client_for(boom)
    assert run(client.probe(ACCOUNT)) == "network_error"


def test_timeout_is_ten_seconds():
    seen = []

    def respond(request):
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200, json=ORDER)
    client, _ = client_for(respond)
    run(client.fetch_order(ACCOUNT, "order_ABC123"))
    assert seen == [{"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0}]
