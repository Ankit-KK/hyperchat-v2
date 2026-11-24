-- PAY-15 6b: the pre-state record (read-only; one line per fact; identical before and after the migrations).
-- Stage 6 plan §5 6b-2 step 3 / §6 V2: the totals checksum, donation counts by status, evidence counts.
SELECT 'totals ' || count(*) || ' ' || coalesce(md5(string_agg(streamer_id::text || ':' || donator_name_lower || ':'
       || total_amount || ':' || donation_count, ',' ORDER BY streamer_id, donator_name_lower)), '-')
  FROM public.streamer_donator_totals;
SELECT 'donations ' || coalesce(string_agg(payment_status || '/' || coalesce(moderation_status, '-') || '=' || n, ' '
       ORDER BY payment_status, moderation_status), '-')
  FROM (SELECT payment_status, moderation_status, count(*) n FROM public.donations GROUP BY 1, 2) x;
SELECT 'donation rows ' || count(*) || ' ' || coalesce(md5(string_agg(id::text || '|' || payment_status || '|'
       || coalesce(moderation_status, '') || '|' || coalesce(provider_payment_id, '') || '|' || coalesce(amount_inr::text, ''),
       ',' ORDER BY id)), '-') FROM public.donations;
SELECT 'webhook events ' || count(*) || ' linked ' || count(donation_id) FROM public.payment_webhook_events;
-- Row hashes name their columns explicitly (the 0010 / 0029 columns): a later migration that ADDS a column (0033 adds
-- payment_integrity_events.payment_amount_refunded_paise) must not change them. Never hash whole rows (w::text).
SELECT 'webhook rows ' || coalesce(md5(string_agg(h, '' ORDER BY h)), '-') FROM (
  SELECT md5(ROW(w.id, w.provider, w.event_id, w.event_type, w.account, w.provider_order_id, w.provider_payment_id,
                 w.donation_id, w.outcome, w.received_at, w.processed_at)::text) AS h
  FROM public.payment_webhook_events w) x;
SELECT 'evidence ' || coalesce(string_agg(kind || '/' || status || '=' || n, ' ' ORDER BY kind, status), '-')
  FROM (SELECT kind, status, count(*) n FROM public.payment_integrity_events GROUP BY 1, 2) x;
SELECT 'evidence rows ' || coalesce(md5(string_agg(h, '' ORDER BY h)), '-') FROM (
  SELECT md5(ROW(e.id, e.webhook_event_id, e.donation_id, e.match_type, e.account, e.kind, e.event_type, e.provider_ref,
                 e.provider_payment_id, e.provider_order_id, e.phase, e.status, e.amount_paise, e.currency,
                 e.amount_deducted_paise, e.reason_code, e.respond_by, e.provider_event_at, e.consistent, e.received_at,
                 e.recorded_at)::text) AS h
  FROM public.payment_integrity_events e) x;
SELECT 'users ' || count(*) || ' streamers ' || (SELECT count(*) FROM public.streamers) FROM public.users;
SELECT 'jobs ' || coalesce(string_agg(status || '=' || n, ' ' ORDER BY status), '-')
  FROM (SELECT status, count(*) n FROM public.jobs GROUP BY 1) x;
