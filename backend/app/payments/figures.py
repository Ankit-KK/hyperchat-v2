"""PAY-15 (D17) stage 4: the one reversal-adjusted INR figure per donation (decisions PAY15-D1, D4, D5; S4-D1).

Every live money figure that reflects refunds and lost disputes (goal progress, dashboard revenue, the CSV export)
uses these SQL fragments, over the donation alias `d`:

  reversed_inr = div(amount_inr_paise * min(applied, captured), captured) / 100      (0 when nothing is applied)
  net_inr      = amount_inr - reversed_inr

  applied  = donation_reversals.applied_reversed_paise (migration 0032; the frozen value while held), 0 without a row
             (always 0 for imported and ambiguous rows, and while HYPERCHAT_PAYMENT_ACCOUNTING_ENABLED is off)
  captured = amount * 100 (every supported currency has 2 decimals: rules.subunits)

Exactly app.payments.accounting.reversed_inr(): pro rata of the STORED checkout-time amount_inr (never re-converted),
rounded down to the paisa per donation BEFORE any sum, capped at amount_inr. All numeric: div() is PostgreSQL's
exact integer division (a plain numeric `/` rounds to a limited number of digits and could floor one paisa high).
A zero or missing amount, or no reversal, gives 0. Leaderboards keep using streamer_donator_totals (the credit
writer applies the same reversal to each donation's credit ceiling). Counts never change (D1).
"""

REVERSAL_JOIN = "LEFT JOIN public.donation_reversals dr ON dr.donation_id = d.id"

_CAPTURED = "(d.amount * 100)"
# numeric(12,2) as amount_inr: the value is a whole number of paise, so the cast only fixes the scale (never rounds)
REVERSED_INR = (f"(CASE WHEN COALESCE(dr.applied_reversed_paise, 0) <= 0 OR COALESCE(d.amount, 0) <= 0 "
                f"THEN 0 ELSE div(d.amount_inr * 100 * LEAST(dr.applied_reversed_paise::numeric, "
                f"{_CAPTURED}), {_CAPTURED}) / 100 END)::numeric(12,2)")
NET_INR = f"(d.amount_inr - {REVERSED_INR})"

# PAY-15 stage 5 (D6, D2; S5-D1, S5-D2): the creator's coarse per-donation flag, from the accounting state only (never
# the evidence tables). A hold wins over an applied reversal, which wins over an open dispute; no hold code, F-BOTH
# or provider detail is ever exposed. NULL without a reversal row (always while the accounting switch is off).
REVERSAL_FLAG = ("(CASE WHEN dr.hold IS NOT NULL THEN 'under_review' "
                 "WHEN COALESCE(dr.applied_reversed_paise, 0) > 0 THEN 'reversed' "
                 "WHEN 'F-OPEN' = ANY (COALESCE(dr.flags, '{}'::text[])) THEN 'dispute_open' END)")

# Goal progress (D5: the active goal window only; ended goals are not rewritten): paid and approved donations since
# the goal was activated. One query for the public page and the realtime / OBS goal-progress event.
GOAL_PROGRESS = (f"SELECT COALESCE(sum({NET_INR}), 0) AS total FROM public.donations d {REVERSAL_JOIN} "
                 "WHERE d.streamer_id = %s AND d.payment_status = 'success' "
                 "AND d.moderation_status IN ('approved', 'auto_approved') AND d.created_at >= %s")
