"""C1: provider-initiated dispute and refund evidence (migration 0029). Record only: nothing here changes a donation,
a donor total, a goal, the leaderboard or stats, starts a refund or calls Razorpay.

The webhook route has already checked the HMAC signature against every configured account, parsed the JSON and
normalised the event id before an event reaches `record`. Then:
  1. parse   the dispute / refund entity (ids, enums, amounts and times only; never the payload, the payer's
             contact details or card data). An unusable payment id or reference is `no_order`, as for payments.
  2. match   the donation whose completing payment this is (`payment`), else the donation of the payment's Razorpay
             order (`order`: another payment of that order, e.g. a duplicate capture), else none (`none`).
  3. account a matched donation's account must be one that signed the body, or the event is `account_mismatch`
             and no evidence row is written (another account's event is never stored against our donation).
  4. record  the webhook event and the evidence row in ONE transaction; a repeated event id records nothing.

The effective state per dispute / refund is computed by the payment_integrity_current view, never stored.
"""

import logging
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app.payments import rules
from app.payments.razorpay import ORDER_ID, PAYMENT_ID
from app.security.audit import security_event

log = logging.getLogger("hyperchat.payments")

# event -> the status the event implies (used when the entity's own status is missing or unknown)
DISPUTE_EVENTS = {"payment.dispute.created": "open", "payment.dispute.under_review": "under_review",
                  "payment.dispute.action_required": "action_required", "payment.dispute.won": "won",
                  "payment.dispute.lost": "lost", "payment.dispute.closed": "closed"}
REFUND_EVENTS = {"refund.created": "pending", "refund.processed": "processed", "refund.failed": "failed"}
INTEGRITY_EVENTS = frozenset(DISPUTE_EVENTS) | frozenset(REFUND_EVENTS)
DISPUTE_STATUSES = frozenset(DISPUTE_EVENTS.values())
REFUND_STATUSES = frozenset(REFUND_EVENTS.values())
PHASES = frozenset({"fraud", "retrieval", "chargeback", "pre_arbitration", "arbitration"})
_REF = {"dispute": re.compile(r"^disp_[A-Za-z0-9]{1,40}$"), "refund": re.compile(r"^rfnd_[A-Za-z0-9]{1,40}$")}
_REASON = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_EPOCH_MAX = 4_102_444_800  # 2100-01-01: anything later is not a real provider time


@dataclass(frozen=True)
class Evidence:
    """One parsed dispute / refund event. `complete` is False when a field we rely on is missing or unusable."""
    kind: str
    event_type: str
    provider_ref: str
    payment_id: str
    order_id: str | None
    phase: str | None
    status: str
    amount_paise: int | None
    currency: str | None
    amount_deducted_paise: int | None
    reason_code: str | None
    respond_by: datetime | None
    provider_event_at: datetime | None
    complete: bool


def _node(payload, *keys):
    for key in keys:
        payload = payload.get(key) if isinstance(payload, dict) else None
    return payload


def _paise(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _epoch(value) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < _EPOCH_MAX:
        return None
    return datetime.fromtimestamp(value, UTC)


def parse(event_type: str, payload: dict) -> Evidence | None:
    """The evidence of one integrity event, or None when its payment id or provider reference is unusable."""
    kind = "dispute" if event_type in DISPUTE_EVENTS else "refund" if event_type in REFUND_EVENTS else None
    if kind is None:
        return None
    entity = _node(payload, "payload", kind, "entity")
    if not isinstance(entity, dict):
        return None
    ref, payment_id = entity.get("id"), entity.get("payment_id")
    if not (isinstance(ref, str) and _REF[kind].match(ref) and isinstance(payment_id, str)
            and PAYMENT_ID.match(payment_id)):
        return None
    order_id = _node(payload, "payload", "payment", "entity", "order_id")
    order_id = order_id if isinstance(order_id, str) and ORDER_ID.match(order_id) else None
    implied = (DISPUTE_EVENTS if kind == "dispute" else REFUND_EVENTS)[event_type]
    allowed = DISPUTE_STATUSES if kind == "dispute" else REFUND_STATUSES
    status = entity.get("status") if entity.get("status") in allowed else implied
    phase = entity.get("phase") if kind == "dispute" and entity.get("phase") in PHASES else None
    currency = entity.get("currency") if isinstance(entity.get("currency"), str) and _CURRENCY.match(
        entity["currency"]) else None
    reason = entity.get("reason_code") if isinstance(entity.get("reason_code"), str) and _REASON.match(
        entity["reason_code"]) else None
    amount = _paise(entity.get("amount"))
    deducted = _paise(entity.get("amount_deducted")) if kind == "dispute" else None
    complete = (entity.get("status") in allowed and amount is not None and currency is not None
                and (kind == "refund" or phase is not None))
    return Evidence(kind, event_type, ref, payment_id, order_id, phase, status, amount, currency, deducted, reason,
                    _epoch(entity.get("respond_by")) if kind == "dispute" else None, _epoch(payload.get("created_at")),
                    complete)


def consistent(evidence: Evidence, donation: dict | None) -> bool:
    """Complete, and (for a matched donation) its currency, a paid donation and an amount within the payment."""
    if not evidence.complete:
        return False
    if donation is None:
        return True
    try:
        donation_paise = rules.subunits(Decimal(str(donation["amount"])))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return (donation["payment_status"] == "success" and evidence.currency == donation["currency"]
            and evidence.amount_paise <= donation_paise)


async def record(repo, event, evidence: Evidence | None, signed_by: list[str]) -> str:
    """Record one signed integrity event. Returns the outcome (or "duplicate")."""
    if evidence is None:
        return "no_order" if await repo.record_event(event, "no_order") else "duplicate"
    donation, match_type = await repo.integrity_target(evidence.payment_id, evidence.order_id)
    if donation is not None and donation["account"] not in signed_by:  # never another account's evidence
        security_event("webhook_rejected", reason="account_mismatch", order_id=donation["order_id"],
                       account=signed_by[0])
        return "account_mismatch" if await repo.record_event(event, "account_mismatch", donation["id"]) else "duplicate"
    account = donation["account"] if donation is not None else signed_by[0]
    ok = consistent(evidence, donation)
    outcome = "integrity_unknown_payment" if donation is None else "integrity_recorded" if ok else "integrity_mismatch"
    if not await repo.record_integrity(replace(event, account=account), outcome, evidence,
                                       donation["id"] if donation else None, match_type, ok):
        return "duplicate"
    # ids and codes only (security_event drops anything else): never names, amounts or the payload
    security_event("payment_integrity_mismatch" if outcome == "integrity_mismatch" else "payment_integrity_event",
                   reason=f"{evidence.event_type}:{match_type}", status=evidence.status,
                   order_id=donation["order_id"] if donation else None, account=account)
    return outcome
