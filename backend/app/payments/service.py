"""Payment use-cases: checkout, status polling, checkout confirmation and the Razorpay webhook.

Every path that can complete a donation (webhook, confirm, poll) ends in PaymentRepository.apply, the one
locked transaction that decides the transition, applies the auto-approve rule, credits the donor total
and (for a webhook) records the event id. A payment only completes a donation after it was fetched from
Razorpay server-side with the account the order was created on, and pays exactly that order's amount in
its currency. Nothing here talks to Pusher, TTS, OBS or the bots: later phases react to the stored state
(payment_status, moderation_status, audio_scheduled_at, mod_notified).

Logs carry reason codes, order ids and account names only: never a key, secret, signature, status token,
the webhook payload or anything about the payer.
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg

from app.core.errors import ApiError
from app.payments import rules
from app.payments.accounts import HEALTH_TTL_SECONDS, AccountHealth, PaymentAccounts
from app.payments.models import CheckoutRequest, ConfirmRequest
from app.payments.razorpay import (
    ORDER_ID,
    PAYMENT_ID,
    Payment,
    RazorpayAccount,
    RazorpayClient,
    RazorpayError,
    checkout_signature_matches,
    signature_matches,
)
from app.payments.repository import PaymentRepository, WebhookEvent
from app.public import pricing
from app.public.service import PublicService, donations_unavailable
from app.security.audit import security_event
from app.soundboard.library import HypersoundLibrary
from app.storage.r2 import ObjectStore, StorageError

log = logging.getLogger("hyperchat.payments")

ON_HOLD = "Payments for this creator are on hold"
UNAVAILABLE = "Payment provider unavailable. Please try again."
NOT_FOUND = "Order not found"
INELIGIBLE = "HC001"  # SQLSTATE of the donations insert trigger (migration 0020): creator not eligible
RATE_RULES = {  # per client IP: (bucket, limit, window seconds)
    "checkout": ("payment:checkout", 10, 60),
    "status": ("payment:status", 60, 60),
    "confirm": ("payment:confirm", 10, 60),
}
POLL_COOLDOWN_SECONDS = 5  # at most one Razorpay lookup per order per 5 s, whoever polls
HANDLED_EVENTS = ("payment.captured", "payment.failed")
WEBHOOK_MAX_BYTES = 64 * 1024
_EVENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _not_found() -> ApiError:
    return ApiError(404, NOT_FOUND, "order_not_found")


def _payment_entity(payload: dict) -> dict:
    """payload.payment.entity of a Razorpay payment event, or {}."""
    node = payload
    for key in ("payload", "payment", "entity"):
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


class PaymentService:
    def __init__(self, *, public: PublicService, repo: PaymentRepository, accounts: PaymentAccounts,
                 client: RazorpayClient, health: AccountHealth, library: HypersoundLibrary,
                 store: ObjectStore | None, limiter, now_ms=None) -> None:
        self.public, self.repo, self.accounts, self.client, self.health = public, repo, accounts, client, health
        self.library, self.store, self.limiter = library, store, limiter
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

    async def check_rate(self, action: str, client_ip: str | None) -> None:
        bucket, limit, window = RATE_RULES[action]
        if not await self.limiter.hit_rate_limit(f"ip:{client_ip}", bucket, limit, window):
            security_event("payment_rejected", reason=f"{action}_rate_limited", client_ip=client_ip)
            raise ApiError(429, "Too many requests. Please try again shortly.", "payment_rate_limited")

    # ── POST /api/v1/public/streamers/{slug}/donations ──────────────────────
    async def checkout(self, slug: str, body: CheckoutRequest) -> dict:
        creator = await self.public.donation_creator(slug)
        try:
            currency = pricing.normalize_currency(body.currency)
        except pricing.UnsupportedCurrency:
            raise rules.bad_request("Unsupported currency", "unsupported_currency") from None
        name = rules.donor_name(body.name)
        amount, amount_inr = rules.checked_amount(body.amount, currency)
        attached = [k for k, v in (("voice", body.voice_message_url), ("hypersound", body.hypersound_url),
                                   ("media", body.media_url)) if v]
        if len(attached) > 1:
            raise rules.bad_request("Only one attachment per donation", "too_many_attachments")
        kind = attached[0] if attached else "text"
        rules.check_minimum(creator, kind, amount, currency)
        message = rules.sanitize(body.message)
        limit = rules.message_char_limit(creator["message_char_tiers"], amount_inr)
        if limit is not None and message and len(message) > limit:
            raise rules.bad_request(f"Message exceeds {limit} character limit for this donation amount",
                                    "message_too_long")
        attachment = await self._attachment(creator, kind, body, amount_inr)

        settings = await self.repo.payment_settings(creator["id"])
        account = await self._checkout_account(creator, settings)
        order_id = rules.new_order_id(settings["order_prefix"], creator["streamer_slug"], self._now_ms())
        try:
            order = await self.client.create_order(account, amount=rules.subunits(amount), currency=currency,
                                                   receipt=rules.receipt(order_id))
        except RazorpayError as exc:
            log.warning("razorpay order failed", extra={"reason": str(exc), "account": account.name,
                                                        "streamer_slug": creator["streamer_slug"]})
            if exc.reason == "auth":
                self.health.mark(account.name, "invalid_key")
                await self.repo.record_health(creator["id"], "invalid_key")
                raise ApiError(503, ON_HOLD, "payments_on_hold") from None
            if exc.reason == "rejected":
                raise rules.bad_request("This amount or currency cannot be paid", "razorpay_rejected") from None
            raise ApiError(502, UNAVAILABLE, "razorpay_unavailable") from None
        token, token_hash = rules.new_status_token()
        try:
            await self.repo.create_donation({
                "streamer_id": creator["id"], "order_id": order_id, "provider_order_id": order.id,
                "payment_account": account.name, "donor_name": name, "amount": amount, "currency": currency,
                "amount_inr": amount_inr, "message": message, "voice_message_url": attachment.get("voice_message_url"),
                "hypersound_url": attachment.get("hypersound_url"), "media_url": attachment.get("media_url"),
                "media_type": attachment.get("media_type"), "status_token_hash": token_hash})
        except psycopg.Error as exc:
            if exc.sqlstate != INELIGIBLE:
                raise
            # Became ineligible after the check above (status change, owner transfer): the unpaid order expires.
            security_event("payment_rejected", reason="creator_ineligible", streamer_slug=creator["streamer_slug"])
            raise donations_unavailable() from None
        return {"order_id": order_id, "razorpay_order_id": order.id, "razorpay_key_id": account.key_id,
                "amount": float(amount), "amount_subunits": order.amount, "currency": currency,
                "status_token": token}

    async def _attachment(self, creator: dict, kind: str, body: CheckoutRequest, amount_inr: Decimal) -> dict:
        if kind == "text":
            return {}
        if kind == "hypersound":
            sounds = (await self.library.sounds()).sounds
            if body.hypersound_url not in {s.audio_url for s in sounds}:
                raise rules.bad_request(rules.ATTACHMENT_ERRORS["hypersound"], "hypersound_url_invalid")
            return {"hypersound_url": body.hypersound_url}
        if self.store is None:
            raise ApiError(503, "Uploads are not available", "uploads_not_configured")
        url = body.voice_message_url if kind == "voice" else body.media_url
        key = rules.upload_key(url, self.store.public_url(""), creator["streamer_slug"], kind)
        if kind == "media" and not creator["media_upload_enabled"]:
            raise ApiError(403, "Media uploads are not enabled for this creator", "media_disabled")
        try:
            head = await self.store.head_upload(key)
        except StorageError as exc:
            log.warning("attachment check failed", extra={"reason": str(exc)})
            raise ApiError(502, "Could not check the attachment. Please try again.", "storage_failed") from None
        if head is None:
            raise rules.bad_request("Attachment not found", f"{kind}_missing")
        if kind == "media":
            return {"media_url": url, "media_type": rules.media_type(key)}
        own, platform = await self.public.repo.voice_tiers(creator["id"])
        max_seconds = rules.voice_max_seconds(own, platform, amount_inr)
        duration = head.metadata.get("duration-ms", "")
        if not duration.isascii() or not duration.isdigit():
            raise rules.bad_request(rules.ATTACHMENT_ERRORS["voice"], "voice_duration_unknown")
        if int(duration) > max_seconds * 1000 + rules.VOICE_DURATION_TOLERANCE_MS:
            raise rules.bad_request(f"Voice message is too long for this amount. Maximum is {max_seconds:g} seconds.",
                                    "voice_too_long")
        return {"voice_message_url": url}

    async def _checkout_account(self, creator: dict, settings: dict) -> RazorpayAccount:
        """The creator's account if its credentials work (cached per account), else ON_HOLD (missing or refused
        keys) or UNAVAILABLE (Razorpay unreachable: transient, re-probed next time). The result is
        written to the creator's streamer_payment_health row when it changed or is older than the cache."""
        account = self.accounts.for_payments(settings["razorpay_account"])
        status = "missing_key" if account is None else await self.health.status(account)
        checked = settings["health_checked_at"]
        stale = checked is None or datetime.now(UTC) - checked > timedelta(seconds=HEALTH_TTL_SECONDS)
        if stale or settings["health_status"] != status or settings["health_on_hold"] != (status != "ok"):
            await self.repo.record_health(creator["id"], status)
        if status == "network_error":  # transient: the same answer as an unreachable order creation, not a hold
            security_event("payment_unavailable", reason=status, streamer_slug=creator["streamer_slug"])
            raise ApiError(502, UNAVAILABLE, "razorpay_unavailable")
        if status != "ok":
            security_event("payment_on_hold", reason=status, streamer_slug=creator["streamer_slug"])
            raise ApiError(503, ON_HOLD, "payments_on_hold")
        return account

    # ── status token ────────────────────────────────────────────────────────
    async def _donation(self, order_id: str, token: str | None, client_ip: str | None) -> dict:
        """The order's donation when the X-Status-Token matches. Unknown order, missing or wrong token and
        legacy rows without a token (D4) all get the same 404."""
        row = await self.repo.by_order_id(order_id) if rules.ORDER_REF.match(order_id or "") else None
        if row is None:
            raise _not_found()
        if not rules.status_token_matches(row["status_token_hash"], token):
            security_event("payment_status_rejected", reason="bad_status_token" if token else "no_status_token",
                           order_id=order_id, client_ip=client_ip)
            raise _not_found()
        return row

    @staticmethod
    def _status(row: dict, payment_status: str | None = None) -> dict:
        return {"order_id": row["order_id"], "payment_status": payment_status or row["payment_status"],
                "amount": float(row["amount"]), "currency": row["currency"]}

    # ── GET /api/v1/public/donations/{order_id}/status ──────────────────────
    async def status(self, order_id: str, token: str | None, client_ip: str | None) -> dict:
        row = await self._donation(order_id, token, client_ip)
        if row["payment_status"] == "success":
            return self._status(row)
        if not await self.limiter.hit_rate_limit(f"order:{order_id}", "payment:poll", 1, POLL_COOLDOWN_SECONDS):
            return self._status(row)  # someone looked at Razorpay for this order less than 5 s ago
        account = self.accounts.for_payments(row["account"])
        if account is None:
            return self._status(row)
        try:
            payments = await self.client.order_payments(account, row["provider_order_id"])
        except RazorpayError as exc:
            log.warning("razorpay poll failed", extra={"reason": str(exc), "account": account.name,
                                                       "order_id": order_id})
            return self._status(row)
        captured = [p for p in payments if p.status == "captured"]
        for payment in captured:
            if self._mismatch(row, payment) is None:
                return self._status(row, (await self.repo.apply(row["id"], "captured", payment.id)).payment_status)
        if captured:  # captured, but not exactly this order's amount/currency
            outcome = self._mismatch(row, captured[0])
            security_event("payment_mismatch", reason=outcome, order_id=order_id, account=account.name)
            return self._status(row, (await self.repo.apply(row["id"], outcome, None)).payment_status)
        return self._status(row)

    @staticmethod
    def _mismatch(row: dict, payment: Payment) -> str | None:
        return rules.payment_mismatch(payment, provider_order_id=row["provider_order_id"], amount=row["amount"],
                                      currency=row["currency"])

    # ── POST /api/v1/public/donations/{order_id}/confirm ────────────────────
    async def confirm(self, order_id: str, token: str | None, body: ConfirmRequest, client_ip: str | None) -> dict:
        row = await self._donation(order_id, token, client_ip)
        account = self.accounts.for_payments(row["account"])
        if account is None:
            raise ApiError(503, "Payment verification unavailable", "razorpay_not_configured")
        payment_id = body.razorpay_payment_id
        # S-1: the signature is checked in every state, an already-paid order included (signature first)
        if not PAYMENT_ID.match(payment_id) or not checkout_signature_matches(
                account.key_secret.get_secret_value(), row["provider_order_id"], payment_id, body.razorpay_signature):
            security_event("payment_confirm_rejected", reason="bad_signature", order_id=order_id, client_ip=client_ip)
            raise rules.bad_request("Invalid payment signature", "bad_signature")
        if row["payment_status"] == "success":
            return self._status(row)  # a valid repeat: no Razorpay call, no write
        try:
            payment = await self.client.fetch_payment(account, payment_id)
        except RazorpayError as exc:
            log.warning("razorpay fetch failed", extra={"reason": str(exc), "account": account.name,
                                                        "order_id": order_id})
            raise ApiError(502, "Could not verify the payment. Please try again.", "razorpay_unavailable") from None
        if payment.order_id == row["provider_order_id"] and payment.status in ("created", "authorized"):
            return self._status(row)  # not captured yet: the page keeps polling
        outcome = "failed" if payment.status == "failed" and payment.order_id == row["provider_order_id"] \
            else (self._mismatch(row, payment) or "captured")
        if outcome in rules.MISMATCHES:
            security_event("payment_mismatch", reason=outcome, order_id=order_id, account=account.name)
        applied = await self.repo.apply(row["id"], outcome, payment.id if outcome == "captured" else None)
        return self._status(row, applied.payment_status)

    # ── POST /api/v1/webhooks/razorpay ──────────────────────────────────────
    def _webhook_accounts(self, body: bytes, signature: str | None) -> list[str]:
        """Accounts whose webhook secret signed this body. Every secret is checked (no early exit)."""
        return [name for name, secret in self.accounts.webhook_secrets() if signature_matches(secret, body, signature)]

    async def webhook(self, body: bytes, signature: str | None, event_id: str | None,
                      received_at: datetime) -> dict:
        signed_by = self._webhook_accounts(body, signature)
        if not signed_by:
            security_event("webhook_rejected", reason="bad_signature" if signature else "no_signature")
            raise ApiError(401, "Invalid signature", "webhook_bad_signature")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Malformed payload", "webhook_malformed") from None
        if not isinstance(payload, dict):
            raise ApiError(400, "Malformed payload", "webhook_malformed")
        if not (isinstance(event_id, str) and _EVENT_ID.match(event_id)):
            event_id = "sha256:" + hashlib.sha256(body).hexdigest()  # same body, same id
        if await self.repo.event_seen(event_id):
            return {"status": "duplicate"}
        event_type = payload.get("event") if isinstance(payload.get("event"), str) else "unknown"
        entity = _payment_entity(payload)
        order_ref, payment_ref = entity.get("order_id"), entity.get("id")
        event = WebhookEvent(event_id, event_type[:64], signed_by[0],
                             order_ref if isinstance(order_ref, str) and ORDER_ID.match(order_ref) else None,
                             payment_ref if isinstance(payment_ref, str) and PAYMENT_ID.match(payment_ref) else None,
                             received_at)
        if event_type not in HANDLED_EVENTS:
            return await self._record(event, "ignored")
        if event.provider_order_id is None or event.provider_payment_id is None:
            return await self._record(event, "no_order")
        row = await self.repo.by_provider_order(event.provider_order_id)
        if row is None:
            return await self._record(event, "unknown_order")
        if row["account"] not in signed_by:  # signed only by another account's secret: never trusted here
            security_event("webhook_rejected", reason="account_mismatch", order_id=row["order_id"],
                           account=signed_by[0])
            return await self._record(event, "account_mismatch", row["id"])
        account_name = row["account"]
        event = replace(event, account=account_name)
        if event_type == "payment.failed":
            return self._result(await self.repo.apply(row["id"], "failed", None, event))
        account = self.accounts.for_payments(account_name)
        if account is None:
            log.warning("webhook cannot verify payment", extra={"reason": "razorpay_not_configured",
                                                                "account": account_name})
            raise ApiError(502, "Payment verification unavailable", "razorpay_not_configured")
        try:
            payment = await self.client.fetch_payment(account, event.provider_payment_id)
        except RazorpayError as exc:
            if exc.reason != "not_found":  # transient or our credentials: 502 so Razorpay redelivers
                log.warning("razorpay fetch failed", extra={"reason": str(exc), "account": account_name,
                                                            "order_id": row["order_id"]})
                raise ApiError(502, "Payment verification unavailable", "razorpay_unavailable") from None
            outcome = "payment_not_found"
        else:
            outcome = self._mismatch(row, payment) or "captured"
        if outcome in rules.MISMATCHES:
            security_event("payment_mismatch", reason=outcome, order_id=row["order_id"], account=account_name)
        return self._result(await self.repo.apply(row["id"], outcome,
                                                  event.provider_payment_id if outcome == "captured" else None, event))

    async def _record(self, event: WebhookEvent, outcome: str, donation_id: str | None = None) -> dict:
        return {"status": outcome if await self.repo.record_event(event, outcome, donation_id) else "duplicate"}

    @staticmethod
    def _result(applied) -> dict:
        return {"status": applied.result}
