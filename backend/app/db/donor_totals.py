"""streamer_donator_totals writes shared by dashboard moderation (Phase 5) and payment completion (Phase 7).

Credit (upsert) or debit (floored at 0) the donor's total, as legacy increment/decrement_donator_total did,
keyed by (streamer_id, lower(donor name)). Always called inside the caller's transaction, with the
donation row locked, so one donation is credited at most once.
"""


async def change_donor_total(cur, streamer_id: str, donor_name: str, amount_inr, delta: int) -> None:
    if delta > 0:
        await cur.execute("""
            INSERT INTO public.streamer_donator_totals
                (streamer_id, donator_name, donator_name_lower, total_amount, donation_count)
            VALUES (%(sid)s, %(name)s, lower(%(name)s), %(amount)s, 1)
            ON CONFLICT (streamer_id, donator_name_lower) DO UPDATE SET
                total_amount = public.streamer_donator_totals.total_amount + EXCLUDED.total_amount,
                donation_count = public.streamer_donator_totals.donation_count + 1,
                donator_name = EXCLUDED.donator_name, updated_at = now()
        """, {"sid": streamer_id, "name": donor_name, "amount": amount_inr})
    else:
        await cur.execute("""
            UPDATE public.streamer_donator_totals SET total_amount = GREATEST(0, total_amount - %(amount)s),
                   donation_count = GREATEST(0, donation_count - 1), updated_at = now()
            WHERE streamer_id = %(sid)s AND donator_name_lower = lower(%(name)s)
        """, {"sid": streamer_id, "name": donor_name, "amount": amount_inr})
