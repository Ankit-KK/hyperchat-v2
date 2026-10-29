# Payments API (Phase 7): Razorpay checkout, status, confirmation and webhooks

Replaces the legacy Edge Functions `create-razorpay-order-unified`, `check-payment-status-unified` and
`razorpay-webhook`. Payment completion only changes database state: alerts, TTS/music/SFX, Pusher events,
the OBS audio queue and moderator notifications are later phases that react to the stored state
(`payment_status`, `moderation_status`, `audio_scheduled_at`, `mod_notified`). Phase 9: completion also
writes the realtime outbox rows (`new-donation`, and goal/leaderboard when auto-approved) in the same
transaction; the worker publishes them ([`realtime.md`](realtime.md)).

Base: `https://api.quicktestspace.site` (development, Razorpay **test mode** only). Errors are
`{"error": "..."}`; 422 validation errors use FastAPI's `{"detail": [...]}`. Donor routes are public (no
cookies) and answer `Cache-Control: no-store`.

| Method | Path | Origin | Rate limit (per IP, 60 s) |
|---|---|---|---|
| POST | `/api/v1/public/streamers/{slug}/donations` | trusted, required | 10 (`payment:checkout`) |
| GET | `/api/v1/public/donations/{order_id}/status` | not required | 60 (`payment:status`) |
| POST | `/api/v1/public/donations/{order_id}/confirm` | trusted, required | 10 (`payment:confirm`) |
| POST | `/api/v1/webhooks/razorpay` | none (server to server) | none |

## 1. Checkout
Request (unknown fields are refused, e.g. `media_type`, `is_hyperemote`, `upload_token`):
```json
{"name": "Ravi", "amount": 100, "currency": "INR", "message": "optional",
 "voice_message_url": null, "hypersound_url": null, "media_url": null}
```
Checks, in order:
1. **Creator**: active creator (404 as the public API).
2. **Currency**: one of INR, USD, EUR, GBP, AED, AUD, CAD, PHP.
3. **Name**: required, no emoji (Unicode `Extended_Pictographic`). Sanitised as legacy did (HTML stripped, `javascript:`/`vbscript:`/`data:` removed, `& < > " '` entity-encoded) and cut to 100 characters.
4. **Amount**: positive, at most 2 decimals, at most ₹5,00,000 in INR at the fixed rates.
5. **Attachments**: at most one of voice / hypersound / media (D8).
6. **Minimum**: the one the pricing page shows (`GET …/{slug}/pricing`) for the donation's kind (text, voice, hypersound, media), in the donor's currency. The text floor is ₹40 (D3).
7. **Message**: sanitised like the name, cut to 500 characters. Its length must fit the creator's `message_char_tiers` tier for the amount in INR.
8. **Attachment URLs**:
   - **Voice and media**: exactly a URL issued by `docs/uploads-api.md` for **this creator** in this environment's bucket (`<public base>/voice-messages/<slug>_…webm`, `<public base>/donation-media/<slug>_….<ext>`).
     - Anything else is refused: other host, other creator, query, fragment, encoded or relative paths.
     - The object must exist (an R2 HEAD, metadata only).
     - **Voice** duration (stored at upload) must fit the voice tier for the amount.
     - **Media** needs `media_upload_enabled`; `media_type` is derived by the server from the key.
   - **Hypersound**: must be one of the current platform library sounds.
9. **Payment account**:
   - The creator's account (`streamers.razorpay_account`) must be configured.
   - Its credentials must be healthy: checked with Razorpay, cached 2 minutes per account. Network errors are not cached.
     Missing or refused keys answer 503 `Payments for this creator are on hold`; an unreachable Razorpay answers
     502 `Payment provider unavailable. Please try again.` (transient, D7), as a failed order creation does.
   - The result is written to `streamer_payment_health` when it changes, at most once per 2 minutes.
10. **Razorpay order**:
    - A Razorpay order is created for `amount × 100` subunits, receipt = our order id. It is never retried.
    - Then, in **one transaction**, the pending donation (payment/moderation `pending`, `payment_account`, SHA-256 of the status token) and its `order_lookup` row are written. A Razorpay failure writes nothing.

Response 201:
```json
{"order_id": "ankit_rp_1800000000000_k3j9x0aa", "razorpay_order_id": "order_…", "razorpay_key_id": "rzp_test_…",
 "amount": 100.0, "amount_subunits": 10000, "currency": "INR", "status_token": "<64 hex>"}
```
`razorpay_key_id` is the public key Razorpay Checkout needs. The **status token** is shown once. Only its hash is
stored, and there is no `upload_token` (D5: uploads use Phase 6 grants).

| Status | When |
|---|---|
| 400 | any validation above; `This amount or currency cannot be paid` (Razorpay refused the order) |
| 403 | untrusted/missing Origin; media for a creator without media uploads |
| 404 | unknown or inactive creator |
| 422 | malformed body, unknown fields |
| 429 | rate limit |
| 502 | Razorpay or R2 unavailable |
| 503 | `Payments for this creator are on hold` (no account, missing or refused credentials, Razorpay unreachable); `Donations are temporarily unavailable` (the creator may not accept new donations: migration 0020, e.g. an unverified new owner; never says why); uploads/library not configured |

## 2. Status: `GET /api/v1/public/donations/{order_id}/status`
Header `X-Status-Token: <token from checkout>`. The token is only read from this header, never from the URL.
It is compared in constant time against the stored hash and is never logged.
- **Same 404 for every refusal**: an unknown order, a missing or wrong token, and legacy rows without a token (D4) all get the same `{"error": "Order not found"}`.
- **Response**: `{"order_id", "payment_status": "pending" | "success" | "failed", "amount", "currency"}`.
- **Unpaid orders**: while not paid, the API asks Razorpay for the order's payments, at most once per 5 s per order (`rate_limits` row `order:<id>` / `payment:poll`), using the account the order was created on. A captured payment that pays exactly this order completes the donation (section 5).

## 3. Confirm: `POST /api/v1/public/donations/{order_id}/confirm`
Header `X-Status-Token`; body = Razorpay Checkout's handler result:
`{"razorpay_payment_id": "pay_…", "razorpay_signature": "<hex>"}`.
1. **Signature**: HMAC-SHA256(key secret, `"<razorpay order id>|<payment id>"`), compared with `hmac.compare_digest`. It is checked in **every** state, an already-paid order included (S-1). A mismatch gives 400 `Invalid payment signature` plus a `payment_confirm_rejected` security event, and nothing else happens. If the order's account has no key secret configured, the answer is 503 (also for a paid order: nothing can be verified). An **already-paid** order with a valid signature answers 200 with the stored status, with no Razorpay call and no write (a repeat, or a confirm after the webhook won).
2. **Fetch**: the payment is fetched from Razorpay server-side. `created`/`authorized` keep the order `pending` (the page keeps polling). `failed` fails the order. `captured` must pay exactly this order.
3. **Complete**: section 5. A Razorpay error gives 502, so the page can retry.

## 4. Webhook: `POST /api/v1/webhooks/razorpay`
Configure in the Razorpay dashboard (test mode for development):
- **URL**: the one above.
- **Events**: `payment.captured`, `payment.failed`; for integrity evidence (section 7) also `payment.dispute.created`,
  `.under_review`, `.action_required`, `.won`, `.lost`, `.closed`, `refund.created`, `refund.processed`, `refund.failed`
  (subscribing is an owner action in the Razorpay dashboard).
- **Secret**: `HYPERCHAT_RAZORPAY_<ACCOUNT>_WEBHOOK_SECRET`.

Processing:
1. **Body size**: the raw body is capped at 64 KiB (413) before anything else.
2. **Signature**: `X-Razorpay-Signature` is checked against every configured account's webhook secret (HMAC-SHA256 over the raw bytes, constant time). No match gives 401, and nothing is parsed or stored.
3. **Parse**: JSON is parsed only after a valid signature; malformed gives 400.
4. **Idempotency**: by `X-Razorpay-Event-Id` (or the SHA-256 of the body if absent). An already processed event gives 200 `{"status": "duplicate"}`.
5. **Dispute and refund events**: recorded as integrity evidence only (section 7).
6. **Other events**: `200 {"status": "ignored"}`, recorded.
7. **Payment events**:
   - **Lookup**: the donation is found via `order_lookup` (else a unique `donations.provider_order_id` match). Unknown orders give 200 `unknown_order`, recorded (D6).
   - **Account binding**: the order's account must be one whose secret signed the body; otherwise 200 `account_mismatch`, recorded, donation untouched.
   - **`payment.failed`**: fails a pending order (section 5).
   - **`payment.captured`**: the payment is fetched from Razorpay with that account's keys, and must be `captured`, belong to this order, and match the amount (exact subunits) and currency.
     - A verified mismatch, or a payment Razorpay does not know, fails the order and answers 200 with the outcome recorded (D6).
     - Network, credential or provider errors answer **502**, nothing recorded, so Razorpay redelivers.

`payment_webhook_events` stores no payload: only the event id, type, account, Razorpay order/payment ids, the
donation and the outcome:
- **Completed or not**: `completed`, `failed`, `already_paid`, `already_failed`, `ignored`.
- **Not ours or unusable**: `no_order`, `unknown_order`, `account_mismatch`.
- **Mismatches**: `amount_mismatch`, `currency_mismatch`, `payment_mismatch`, `payment_not_found`.
- **Integrity evidence (section 7)**: `integrity_recorded`, `integrity_unknown_payment`, `integrity_mismatch`.

Since migration 0029 the log is **append-only for every role**:
- `hyperchat_app` may SELECT and INSERT; `hyperchat_worker` has no access.
- A guard trigger refuses UPDATE and DELETE, and TRUNCATE (direct or cascaded) while the table holds rows.
- `donation_id` is `ON DELETE RESTRICT`: a donation with webhook evidence can't be deleted.

## 5. Completion: one transaction for webhook, confirm and polling
`PaymentRepository.apply` locks the donation row (`FOR UPDATE`), then:

| current \ outcome | captured | failed / verified mismatch |
|---|---|---|
| pending | **paid** (+ auto-approve, credit) | failed, moderation `rejected` |
| failed | **paid** (+ auto-approve, credit) | unchanged |
| success | unchanged (never failed again) | unchanged |

When a donation is paid:
- **Payment id**: `provider_payment_id` is stored (unique).
- **Auto-approve rule (D2, D7)**: HyperEmotes and HyperSounds are always auto-approved. Otherwise only creators in `auto_approve` mode auto-approve, and with media moderation on (default) a donation with **media or a voice message** waits for a moderator.
- **If auto-approved**:
  - `moderation_status = auto_approved`, `approved_by_label = system`;
  - `audio_scheduled_at = now + 10 s`;
  - the donor total is credited once (`streamer_donator_totals`, as dashboard approval does).
- **If held**: the donation stays `pending` for moderation; the total is credited when a moderator approves it (Phase 5).

For a webhook the event row is inserted in the same transaction. A concurrent delivery of the same event
waits on the lock and then rolls back as `duplicate`, and a racing confirm or poll sees `success`. A donation is
completed and credited at most once.

**Duplicate capture (G3):**
- When a webhook, confirm or poll brings a captured payment for an order that is **already paid with another payment**, the answer and the state are unchanged (`already_paid`, no credit).
- The case is logged as the security event `payment_duplicate_capture` (order id and account name only), for manual investigation: possibly a second charge.
- The stored payment id is read under the row lock, so two paths racing with the **same** payment never trigger it.
- Refunds are manual (no refund flow yet).

## 6. Reconciliation (Step G1): `python -m app.cli payment-reconcile`
**A manual operator command. No timer runs it** (owner decisions D-G1 to D-G7, 2026-10-28). It completes donations whose capture never arrived through the webhook, confirm or poll paths (lost webhook, donor closed the page). It is **report-only** unless `--apply` **and** `--confirm-database <name of the connected database>` are both given.

**What it selects:**
- **V2 donations only:** `payment_account` set (the checkout writes it) **and** no `legacy_source_table` (the import writes that, and never `payment_account`). Rows with neither or both markers are *ambiguous provenance*: never selected, counted as a finding.
- In state `pending` or `failed`.
- Created between 15 minutes and 7 days ago, at most 200 per run, oldest first. The window and the limit can only be narrowed.
- Selection uses `donations.provider_order_id`, so it survives the 30-day `order_lookup` prune.

**How it verifies each order:**
- With the order's **own** account keys, it calls `GET /orders/{id}/payments`, the call the status poll makes. Nothing from a client is used.
- **`verified`:** exactly one captured payment that pays exactly this order (order id, `captured`, exact subunit amount, currency). With `--apply`, the existing completion transaction (§5) applies it, giving `completed`, or `already_paid` if another path won.
- **`no_capture`:** none, `created`, `authorized`, `failed` or `refunded`. Nothing changes; an order is never failed or expired here.
- **`multiple`** (more than one capture) and **`mismatch`:** reported and logged (`payment_reconcile_exception`), never applied, never refunded.
- **`inconclusive`:** the account is not configured here. Reported.
- **Any Razorpay error** (auth, not found, 429, 5xx, timeout, malformed answer) **stops the run** (fail closed). Unexamined orders are counted as `stopped_before`.
- Calls are paced (2 per second). SIGINT and SIGTERM stop between orders, never inside a completion. A re-run is idempotent.

**The report:**
- JSON on stdout. With `--report-file PATH`, also a **new** file with mode 0600 (refused if the file exists).
- It holds the mode, window, outcome counts, and what was **not** examined: `too_recent`, `too_old`, `legacy_excluded`, `ambiguous_provenance`, `beyond_limit`, `stopped_before`. Per order: order id, state, outcome, Razorpay payment id, reason, number of attempts and result.
- No donor data, tokens, keys or signatures.
- Each applied completion is also logged as `payment_reconciled`.

**Exit codes:**
- `0`: clean.
- `1`: completed with findings (verified-but-not-applied, multiple, mismatch, inconclusive, ambiguous provenance).
- `2`: stopped (a Razorpay error or an interruption) or refused.

## 7. Dispute and provider-refund evidence (C1, migration 0029)
**Record only.** Provider-initiated disputes (chargebacks) and refunds made at Razorpay are stored as evidence.
- Nothing changes a donation, a donor total, a goal, the leaderboard or stats.
- There is no refund endpoint, no refund call and no Razorpay API call.
- A compensating accounting entry would be a separate, approved change (D-C1b).

**Handled events and payloads:**
- `payment.dispute.created`, `.under_review`, `.action_required`, `.won`, `.lost`, `.closed`, from `payload.dispute.entity`;
- `refund.created`, `.processed`, `.failed`, from `payload.refund.entity`;
- the order id comes from `payload.payment.entity`;
- `refund.speed_changed` stays `ignored`.

**Processing** (after the usual signature, parse and dedupe steps; `app/payments/integrity.py`):
1. **Parse.** Only ids, enums, amounts in paise, currency, reason code and times are kept. Never the payload, the payer's
   contact details or card data. An unusable payment id or reference is `no_order` and stores no evidence.
2. **Match.** `payment` means the donation completed by this payment (`donations.provider_payment_id`). `order` means the
   donation of this Razorpay order, but another payment (for example a duplicate capture). `none` means no donation was
   found. **Only `payment` matches may be attributed to a donation.**
3. **Account.** A matched donation's account (`COALESCE(order account, creator account)`) must have signed the body.
   Otherwise the event is recorded as `account_mismatch`, a security event is logged, and **no evidence row is
   written**. Unknown payments are recorded under the signing account.
4. **Consistency.** Same currency, an amount within the payment, a paid donation, and no essential field missing.
   Otherwise the outcome is `integrity_mismatch`; the evidence is still recorded, with `consistent = false`.
5. **Record.** The webhook event and the `payment_integrity_events` row are written in one transaction. A repeated
   event id gives `duplicate` and writes nothing. Distinct events about the same dispute are all kept.

**Current state** (`payment_integrity_current` view): computed, never stored. Per dispute or refund, the event with the
highest rank wins. The order is:
1. phase: fraud = retrieval < chargeback < pre_arbitration < arbitration;
2. status: open / pending < under_review / action_required / failed < won / lost / closed / processed;
3. then the provider's event time, receipt time, record time and id.

So a won chargeback that reopens in pre-arbitration is open again, and a late `created` never undoes a later state.
`conflicting` marks another event of the same rank with a different status (for example won vs lost).

**Report:** `python -m app.cli payment-integrity-report [--since YYYY-MM-DD]`.
- Read-only (`READ ONLY` session), no Razorpay call, JSON on stdout.
- Exit 0 when nothing needs attention, 1 when something does, 2 on a refused argument.
- Needing attention means open or lost disputes, refunds, mismatches, conflicts, or a non-`payment` match.
- No timer.

**Cutover / rehearsal ETL:** `database/etl/rehearsal.py` refuses to load, before changing anything, when either
evidence table holds a row.

## Configuration (`/etc/hyperchat/api.env`, names only; see `deploy/api.env.example`)
`HYPERCHAT_RAZORPAY_PRIMARY_{KEY_ID,KEY_SECRET,WEBHOOK_SECRET}`.
- **One account, `primary` (D6)**: test vs live is the environment's job. **DEV: Razorpay test-mode keys. Production: live keys.** The `secondary` account was retired (migration 0018); a leftover `HYPERCHAT_RAZORPAY_SECONDARY_*` variable configures nothing.
- **Account mapping**: `streamers.razorpay_account` is `primary` or NULL (no payment account: on hold). Migration 0010 filled it from the legacy secret-name columns; since 0018 both legacy pairs (`razorpay-keyid`/`razorpay-keysecret` and the `(test)` pair, which held test-mode keys) are `primary`, anything else is NULL. Legacy orders keep `donations.payment_account` NULL (legacy never recorded one; the creator's account applies); V2 orders record the account they were created on.
- **Mode guards (startup, both ways)**: the API refuses to start with an `rzp_live_` key id unless `HYPERCHAT_APP_ENV=production`, and with an `rzp_test_` key id when it is `production`. The error names the variable, never the value.
- **Startup log**: account names only.

## Payment health (D7: admin/manual check, no scheduled polling)
- **Runtime protection stays at checkout** (above): every checkout uses the account's cached probe, so broken keys put
  payments on hold before any order exists, and Razorpay is asked at most once per 2 minutes per API process
  (outages are re-probed).
- **Manual verification** for cutover, key rotation and debugging, through the same cached probe
  (`GET /v1/payments?count=1`: authenticated, read-only, no order, no payment; same in test and live mode):
  - `GET /api/v1/admin/payments/health` (admin session): `{"account": "primary", "configured": true, "mode": "test",
    "status": "ok", "checked_at": "<ISO time of that answer>"}`. `mode` comes from the key id prefix (`test`, `live`,
    `unknown`); `status` is `ok`, `invalid_key`, `missing_key` or `network_error`. Never a key id, secret or Razorpay
    response; non-admins get 403, anonymous 401. Within the 2-minute cache it answers without calling Razorpay.
  - `python -m app.cli --env-file <api.env> payment-health`: one probe, prints the same fields as `key=value`, exit 0
    only when `status=ok` (1 otherwise).
- **No cron or timer** polls Razorpay: nothing would act on the result. Reconsider when an alert channel exists.
- `streamer_payment_health` (per creator, legacy shape) is still written by checkout and is not read for decisions.

## Logging
Logs carry reason codes, our order ids, Razorpay order/payment ids and account names. They never carry key
secrets, webhook secrets, signatures, status tokens, the webhook payload or payer details (email, phone, VPA).
The access log records paths without query strings.
