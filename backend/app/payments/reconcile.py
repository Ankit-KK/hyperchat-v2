"""Stale-payment reconciliation (P5, Step G1): a MANUAL operator command, report-only unless --apply.

Selects V2 donations only (PaymentRepository.reconcile_candidates: created by the checkout API, never an imported
legacy row, never a row of ambiguous provenance) that are `pending` or `failed` and were created between
max_age and min_age ago, oldest first, at most `limit`. For each one it asks Razorpay with the order's OWN account
(GET /orders/{id}/payments, the call the status poll makes) and classifies the answer:

  verified      exactly one captured payment, and it pays exactly this order (order id, captured, exact subunit
                amount, currency: rules.payment_mismatch). Applied ONLY with --apply, through
                PaymentRepository.apply (one locked transaction, credit at most once) -> applied | already_paid
  no_capture    Razorpay answered and no payment is captured (none, created, authorized, failed): untouched
  multiple      more than one captured payment: reported for manual investigation, never applied
  mismatch      a captured payment that is not exactly this order's: reported, never applied
  inconclusive  the account is not configured here: reported, never applied
  error         Razorpay failed (auth, not found, 429/5xx, timeout, malformed answer): the run STOPS (fail closed);
                orders not yet examined stay untouched and are counted

Nothing is ever failed, expired, refunded or deleted here, and report mode never calls apply. Orders outside the
window, legacy rows, ambiguous rows and candidates beyond the limit are counted, never implied reconciled. The
report holds order ids, Razorpay payment ids, outcomes and counts only: no donor data, tokens, keys or signatures.
Exit codes: 0 clean; 1 completed with findings (verified-but-not-applied, multiple, mismatch, inconclusive,
ambiguous provenance); 2 stopped (Razorpay error, interrupted) or refused.
"""

import asyncio
import logging
import signal
from dataclasses import dataclass, field

from app.payments import rules
from app.payments.razorpay import RazorpayError
from app.security.audit import security_event

log = logging.getLogger("hyperchat.payments")

MIN_AGE_MINUTES, MAX_AGE_DAYS, LIMIT, RATE_PER_SECOND = 15, 7, 200, 2.0  # owner decision D-G1


@dataclass
class Report:
    mode: str
    window: dict
    not_examined: dict = field(default_factory=dict)
    orders: list[dict] = field(default_factory=list)
    stopped: str | None = None

    def counts(self) -> dict:
        out: dict[str, int] = {}
        for o in self.orders:
            out[o["outcome"]] = out.get(o["outcome"], 0) + 1
        return dict(sorted(out.items()))

    def as_dict(self) -> dict:
        return {"mode": self.mode, "window": self.window, "examined": len(self.orders), "outcomes": self.counts(),
                "not_examined": self.not_examined, "stopped": self.stopped, "orders": self.orders}

    def exit_code(self) -> int:
        if self.stopped:
            return 2
        findings = {"multiple", "mismatch", "inconclusive"} | ({"verified"} if self.mode == "report" else set())
        if any(o["outcome"] in findings for o in self.orders) or self.not_examined.get("ambiguous_provenance"):
            return 1
        return 0


def classify(row: dict, payments: list) -> tuple[str, str | None, str | None]:
    """(outcome, payment id, mismatch reason) for one order from Razorpay's own list of its payments."""
    captured = [p for p in payments if p.status == "captured"]
    if not captured:
        return "no_capture", None, None
    if len(captured) > 1:
        return "multiple", None, None
    payment = captured[0]
    reason = rules.payment_mismatch(payment, provider_order_id=row["provider_order_id"], amount=row["amount"],
                                    currency=row["currency"])
    return ("mismatch", payment.id, reason) if reason else ("verified", payment.id, None)


class Reconciler:
    def __init__(self, repo, accounts, client, *, sleep=asyncio.sleep) -> None:
        self.repo, self.accounts, self.client, self.sleep = repo, accounts, client, sleep
        self.stop_requested = False

    def request_stop(self, *_):  # SIGINT/SIGTERM: finish the current order, examine no further one
        self.stop_requested = True

    async def run(self, *, apply: bool, min_age_minutes: int = MIN_AGE_MINUTES, max_age_days: int = MAX_AGE_DAYS,
                  limit: int = LIMIT, rate_per_second: float = RATE_PER_SECOND) -> Report:
        report = Report("apply" if apply else "report",
                        {"min_age_minutes": min_age_minutes, "max_age_days": max_age_days, "limit": limit})
        rows, report.not_examined = await self.repo.reconcile_candidates(min_age_minutes * 60, max_age_days * 86400,
                                                                          limit)
        for index, row in enumerate(rows):
            if self.stop_requested:
                report.stopped = "interrupted"
                break
            if index:
                await self.sleep(1.0 / rate_per_second)
            entry = {"order_id": row["order_id"], "state": row["payment_status"]}
            account = self.accounts.for_payments(row["account"])
            if account is None or row["account"] != row["own_account"]:
                report.orders.append({**entry, "outcome": "inconclusive", "reason": "account_not_configured"})
                continue
            try:
                payments = await self.client.order_payments(account, row["provider_order_id"])
            except RazorpayError as exc:  # fail closed: no guess, no further Razorpay call this run
                report.orders.append({**entry, "outcome": "error", "reason": exc.reason})
                report.stopped = f"razorpay_{exc.reason}"
                break
            outcome, payment_id, reason = classify(row, payments)
            entry.update(outcome=outcome, payment_id=payment_id, reason=reason, attempts=len(payments))
            if outcome in ("multiple", "mismatch"):
                security_event("payment_reconcile_exception", reason=reason or outcome, order_id=row["order_id"],
                               account=row["account"])
            if outcome == "verified" and apply:
                applied = await self.repo.apply(row["id"], "captured", payment_id)
                entry["result"] = applied.result
                if applied.result == "already_paid" and applied.paid_with != payment_id:
                    security_event("payment_duplicate_capture", order_id=row["order_id"], account=row["account"])
                elif applied.result == "completed":
                    security_event("payment_reconciled", reason=row["payment_status"], order_id=row["order_id"],
                                   account=row["account"])
            report.orders.append({k: v for k, v in entry.items() if v is not None})
        remaining = len(rows) - len(report.orders)
        if remaining:
            report.not_examined["stopped_before"] = remaining
        return report


async def run_cli(repo, accounts, client, *, apply: bool, **options) -> Report:
    """The command's run with SIGINT/SIGTERM turned into a stop between orders (never inside apply)."""
    reconciler = Reconciler(repo, accounts, client)
    previous = {s: signal.signal(s, reconciler.request_stop) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        return await reconciler.run(apply=apply, **options)
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
