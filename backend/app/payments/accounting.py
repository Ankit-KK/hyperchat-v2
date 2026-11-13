"""PAY-15 (D17): the read-only reversal evaluator. Pure: no I/O, no writes, no money moved.

Given one donation and the CURRENT state of its dispute / refund evidence (rows of the payment_integrity_current view,
migration 0029), decide how much of its captured payment is reversed, in the payment's smallest unit, or that the
evidence is ambiguous and must be held. Later PAY-15 stages apply the result; this module only computes it.

Rules (design: PAY-15 accounting plan sections 14.4 and 15, decisions PAY15-D2 to D4 and D8):
  * Attribution is checked here, from the trusted ids every evidence row carries (0029: provider_payment_id NOT NULL,
    donation_id), not from the stored match_type: a row counts only when its donation_id is this donation AND its
    provider_payment_id is the donation's completing payment (donations.provider_payment_id). match_type was decided
    when the evidence arrived, so evidence that arrived before the capture is stored as 'order' and still counts once
    the capture is known; evidence of another payment of the order (a duplicate capture) never counts. A row for
    another donation, or claiming match_type 'payment' for another payment, is a caller or data error: held.
  * A counted row needs a valid provider reference for its kind (disp_… / rfnd_…, as 0029's CHECK); rows are keyed by
    (kind, provider_ref).
  * Imported donations (legacy_source_table set) are never reversed automatically (D8): reported only.
  * An unpaid donation has nothing to reverse yet (evidence may arrive before the capture; it is evaluated again).
  * R = sum of refunds whose current status is 'processed'; L = sum of amount_deducted over disputes whose current
    status is 'lost'. open / under_review / action_required / won / closed and pending / failed refunds count 0.
  * Only R, or only L: that amount. Both: max(R, L), never R + L (an accepted dispute may also refund the customer),
    flagged F-BOTH.
  * Holds (no clear amount; the caller keeps the reversal it already applied): H-CONFLICT (a conflicting dispute,
    two different current rows for one (kind, reference), a row that fails attribution, a missing or invalid
    reference, a kind / status / phase or a counted amount that cannot be read), H-CURRENCY (a counted refund or dispute in another currency, any counted dispute on a non-INR donation, a
    donation currency outside the supported eight), H-NO-DEDUCTION (a lost dispute without a deducted amount),
    H-OVER-REFUND (R or L above the captured amount). With several, the first in that order is reported.
The evaluation depends only on the set of current rows: their order and exact repeats change nothing.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from app.payments import integrity, rules
from app.public import pricing

HOLDS = ("H-CONFLICT", "H-CURRENCY", "H-NO-DEDUCTION", "H-OVER-REFUND")  # reporting priority
PHASES = integrity.PHASES


@dataclass(frozen=True)
class Evaluation:
    reversed_paise: int | None   # None while held: no clear amount (the applied reversal stays frozen)
    hold: str | None             # one of HOLDS, or None
    flags: frozenset[str]        # F-BOTH, F-OPEN, REPORT-IMPORTED


CLEAR_NOTHING = Evaluation(0, None, frozenset())


def captured_paise(donation: Mapping) -> int:
    return rules.subunits(Decimal(str(donation["amount"])))


def reversed_inr(donation: Mapping, reversed_paise: int) -> Decimal:
    """D4: the INR value of a reversal, pro rata of the donation's stored amount_inr, rounded down to the paisa and
    capped at amount_inr. Computed on the cumulative reversal, never per event."""
    amount_inr_paise, captured = rules.subunits(Decimal(str(donation["amount_inr"]))), captured_paise(donation)
    if captured <= 0:
        return Decimal("0.00")
    paise = min(amount_inr_paise, amount_inr_paise * min(max(reversed_paise, 0), captured) // captured)
    return Decimal(paise) / 100


def _amount(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _same(a, b) -> bool:
    return a is not None and b is not None and str(a) == str(b)


def evaluate(donation: Mapping, evidence: Iterable[Mapping]) -> Evaluation:
    """The reversal of `donation` from its current evidence rows (any order, repeats allowed)."""
    rows = list(evidence)
    if donation.get("legacy_source_table") is not None:
        return Evaluation(0, None, frozenset({"REPORT-IMPORTED"} if rows else ()))
    if donation.get("payment_status") != "success" or not rows:
        return CLEAR_NOTHING

    holds, flags = set(), set()
    payment = donation.get("provider_payment_id")
    by_ref: dict[tuple, Mapping] = {}
    for row in rows:
        if payment is None or not _same(row.get("donation_id"), donation.get("id")):
            holds.add("H-CONFLICT")  # not this donation's evidence: the caller must never pass it
            continue
        if not _same(row.get("provider_payment_id"), payment):
            if row.get("match_type") == "payment":
                holds.add("H-CONFLICT")  # claims the completing payment but names another one
            continue  # another payment of the order (e.g. a duplicate capture): never this donation's money
        kind, ref = row.get("kind"), row.get("provider_ref")
        pattern = integrity._REF.get(kind) if isinstance(kind, str) else None
        if pattern is None or not isinstance(ref, str) or not pattern.match(ref):
            holds.add("H-CONFLICT")  # unknown kind, or a missing / empty / wrong-kind reference
            continue
        key, seen = (kind, ref), by_ref.get((kind, ref))
        if seen is not None and tuple(sorted(seen.items())) != tuple(sorted(row.items())):
            holds.add("H-CONFLICT")  # two different "current" states for one dispute / refund
        by_ref.setdefault(key, row)

    currency = donation.get("currency")
    if currency not in pricing.EXCHANGE_RATES_TO_INR:
        holds.add("H-CURRENCY")
    captured = captured_paise(donation)
    refunds = disputes = 0
    for row in by_ref.values():
        kind, status = row.get("kind"), row.get("status")
        if kind == "refund":
            if status not in integrity.REFUND_STATUSES:
                holds.add("H-CONFLICT")
            elif status == "processed":
                amount = _amount(row.get("amount_paise"))
                if amount is None:
                    holds.add("H-CONFLICT")
                else:
                    refunds += amount
                if row.get("currency") != currency:
                    holds.add("H-CURRENCY")
        else:  # dispute (the reference check admitted only the two kinds)
            if status not in integrity.DISPUTE_STATUSES or row.get("phase") not in PHASES or row.get("conflicting"):
                holds.add("H-CONFLICT")
            elif status in ("open", "under_review", "action_required"):
                flags.add("F-OPEN")
            elif status == "lost":
                deducted = _amount(row.get("amount_deducted_paise"))
                if not deducted:
                    holds.add("H-NO-DEDUCTION")
                else:
                    disputes += deducted
                if currency != "INR" or row.get("currency") != currency:
                    holds.add("H-CURRENCY")  # D4: non-INR disputes are deducted at another day's rate
    if refunds > captured or disputes > captured:
        holds.add("H-OVER-REFUND")
    if refunds and disputes:
        flags.add("F-BOTH")
    hold = next((h for h in HOLDS if h in holds), None)
    if hold is not None:
        return Evaluation(None, hold, frozenset(flags))
    return Evaluation(max(refunds, disputes), None, frozenset(flags))
