-- PAY-15 6b: Stage 6 plan §6 verification SQL, updated for head 0035 with the switch OFF (read-only).
-- Each line prints 'V<n> <result>'; common.sh compares the whole output with VERIFY_EXPECTED (exact match).
SELECT 'V1 head ' || string_agg(version_num, ',') FROM public.alembic_version;
-- V2 (totals unchanged) is the 'totals' line of prestate.sql, compared before / after.
SELECT 'V3 buckets credited beyond their total ' || count(*) FROM public.streamer_donator_totals t
  JOIN (SELECT streamer_id, lower(donor_name) n, sum(credited_inr) c FROM public.donations GROUP BY 1, 2) d
    ON d.streamer_id = t.streamer_id AND d.n = t.donator_name_lower WHERE d.c > t.total_amount;
SELECT 'V4 donations outside their credit bounds ' || count(*) FROM public.donations
 WHERE NOT (0 <= credited_inr AND credited_inr <= COALESCE(creditable_inr, amount_inr)
            AND COALESCE(creditable_inr, amount_inr) <= amount_inr);
SELECT 'V5 donations whose history differs from credited ' || count(*) FROM public.donations d
  LEFT JOIN (SELECT donation_id, sum(delta_inr) s FROM public.donation_credit_events GROUP BY 1) e ON e.donation_id = d.id
 WHERE COALESCE(e.s, 0) <> d.credited_inr;
SELECT 'V6 credit differs from the no-reversal target ' || count(*) FROM public.donations d
  LEFT JOIN public.donation_reversals r ON r.donation_id = d.id
 WHERE d.credited_inr <> CASE WHEN d.payment_status = 'success' AND d.moderation_status IN ('approved', 'auto_approved')
                              THEN COALESCE(d.creditable_inr, d.amount_inr) ELSE 0 END
   AND COALESCE(r.applied_reversed_paise, 0) = 0;
SELECT 'V7 reversals ' || (SELECT count(*) FROM public.donation_reversals)
    || ' non-backfill/writer history ' || (SELECT count(*) FROM public.donation_credit_events WHERE cause NOT IN ('backfill', 'writer'))
    || ' state ' || (SELECT state || '/' || dispute_mode FROM public.payment_accounting_state)
    || ' events ' || (SELECT count(*) FROM public.payment_accounting_events)
    || ' runs ' || (SELECT count(*) FROM public.payment_accounting_runs)
    || ' evaluations ' || (SELECT count(*) FROM public.donation_accounting_evaluations);
SELECT 'V8 triggers ' || string_agg(tgname, ' ' ORDER BY tgname) FROM pg_trigger WHERE NOT tgisinternal AND tgname IN (
  'donations_credit_writer', 'donations_credit_delete_guard', 'donations_credit_delete_guard_truncate',
  'donations_protect_history_columns', 'donation_credit_events_guard', 'payment_integrity_events_guard',
  'payment_webhook_events_guard', 'payment_accounting_state_guard', 'donation_accounting_evaluations_ack_guard');
SELECT 'V8 privileges ' || has_table_privilege('hyperchat_app', 'public.streamer_donator_totals', 'UPDATE')
    || ' ' || has_table_privilege('hyperchat_app', 'public.donation_reversals', 'INSERT')
    || ' ' || has_function_privilege('hyperchat_worker', 'public.donation_accounting_apply(uuid,bigint,text,text[],text[])', 'EXECUTE');
SELECT 'V9 refund snapshots ' || count(*) FROM public.payment_integrity_events WHERE payment_amount_refunded_paise IS NOT NULL;
SELECT 'V10 0035 hardening '
    || (to_regprocedure('public.payment_accounting_scope_covers(jsonb, timestamp with time zone, text, uuid)') IS NOT NULL)
    || ' ' || EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'donation_accounting_evaluations_ack_guard' AND tgenabled IN ('O', 'A'))
    || ' ' || (SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND convalidated
               AND conname IN ('donation_accounting_evaluations_ack_run', 'donation_credit_events_run_id'));
SELECT 'V11 backfill rows ' || count(*) || ' credited ' || coalesce(sum(delta_inr), 0)
  FROM public.donation_credit_events WHERE cause = 'backfill';
