"""Payments (Phase 7). See docs/payments-api.md.

  POST /api/v1/public/streamers/{slug}/donations     checkout: Razorpay order + pending donation
  GET  /api/v1/public/donations/{order_id}/status    payment status (X-Status-Token header)
  POST /api/v1/public/donations/{order_id}/confirm   Razorpay Checkout handler result (X-Status-Token)
  POST /api/v1/webhooks/razorpay                     Razorpay webhooks (signature, no Origin)

Donor routes are public (no cookies) and rate limited per IP; the two POSTs also need a trusted Origin.
The status token is only ever read from the X-Status-Token header, never from the URL.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.auth.dependencies import client_ip
from app.core.errors import ApiError
from app.payments.models import CheckoutRequest, ConfirmRequest
from app.payments.service import WEBHOOK_MAX_BYTES, PaymentService
from app.security.audit import security_event

checkout_router = APIRouter(prefix="/api/v1/public/streamers", tags=["payments"])
donations_router = APIRouter(prefix="/api/v1/public/donations", tags=["payments"])
webhooks_router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])
NO_STORE = {"Cache-Control": "no-store"}  # status tokens and per-order state


def _service(request: Request) -> PaymentService:
    service = getattr(request.app.state, "payments", None)
    if service is None:
        raise ApiError(503, "Payments are not available", "payments_not_configured")
    return service


def _guard(action: str, *, origin: bool):
    async def dependency(request: Request) -> PaymentService:
        if origin:
            value = request.headers.get("origin")
            if not value or value not in request.app.state.settings.browser_origins:
                security_event("payment_rejected", reason="bad_origin", client_ip=client_ip(request))
                raise ApiError(403, "Request origin not allowed", "bad_origin")
        service = _service(request)
        await service.check_rate(action, client_ip(request))
        return service
    return dependency


@checkout_router.post("/{slug}/donations", status_code=201)
async def checkout(slug: str, body: CheckoutRequest,
                   service: PaymentService = Depends(_guard("checkout", origin=True))):
    return JSONResponse(await service.checkout(slug, body), status_code=201, headers=NO_STORE)


@donations_router.get("/{order_id}/status")
async def status(order_id: str, request: Request, service: PaymentService = Depends(_guard("status", origin=False))):
    result = await service.status(order_id, request.headers.get("x-status-token"), client_ip(request))
    return JSONResponse(result, headers=NO_STORE)


@donations_router.post("/{order_id}/confirm")
async def confirm(order_id: str, body: ConfirmRequest, request: Request,
                  service: PaymentService = Depends(_guard("confirm", origin=True))):
    result = await service.confirm(order_id, request.headers.get("x-status-token"), body, client_ip(request))
    return JSONResponse(result, headers=NO_STORE)


@webhooks_router.post("/razorpay")
async def razorpay_webhook(request: Request):
    """Server to server: no Origin, no cookies. The raw body is capped, then its signature checked, before
    it is parsed. 2xx = done (including unknown orders and verified mismatches); 502 = please redeliver."""
    received_at = datetime.now(UTC)
    service = _service(request)
    too_large = ApiError(413, "Payload too large", "webhook_too_large")
    length = request.headers.get("content-length")
    if length is not None and (not length.isascii() or not length.isdigit() or int(length) > WEBHOOK_MAX_BYTES):
        raise too_large
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > WEBHOOK_MAX_BYTES:
            raise too_large
    result = await service.webhook(bytes(body), request.headers.get("x-razorpay-signature"),
                                   request.headers.get("x-razorpay-event-id"), received_at)
    return JSONResponse(result, headers=NO_STORE)
