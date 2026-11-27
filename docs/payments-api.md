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
- **Creator blocked after checkout (owner decision 2026-11-28, audit run-4 NV4-1):** the payment is **completed and
  credited**. That covers a creator suspended, restricted or otherwise blocked after the order was created, and a
  payment authorised late (slow UPI or bank). Creator eligibility (`creator_donation_block`) is checked only when
  checkout inserts the pending donation, and no completion path re-checks it. Razorpay captures late
  authorisations automatically, and refusing one would hold the donor's money with no donation. Tests:
  `test_checkout_uploads_and_inserts_follow_one_rule_and_existing_orders_finalize` (restriction) and
  `test_a_late_capture_for_a_creator_suspended_after_checkout_completes_and_credits` (suspension).
- **Auto-approve rule (D2, D7)**: HyperSounds are always auto-approved. HyperEmotes are discontinued (owner decision D16, 2026-11-13): no donation is auto-approved for being one, an imported `is_hyperemote` row included. Otherwise only creators in `auto_approve` mode auto-approve, and with media moderation on (default) a donation with **media or a voice message** waits for a moderator.
- **If auto-approved**:
  - `moderation_status = auto_approved`, `approved_by_label = system`;
  - `audio_scheduled_at = now + 10 s`;
  - the donor total is credited once, by the database credit writer in the same UPDATE (migration 0031, as for
    dashboard and bot approval; application code never writes `streamer_donator_totals`).
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
  Since migration 0030 neither marker can be forged by an API role: `legacy_source_table` (and `is_hyperemote`) are written only by the legacy import, so a runtime role cannot hide a V2 row from reconciliation by setting it.
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
- PAY-15 stage 5: each item carries its donation's `accounting` state (`applied_reversed_paise`, `hold`, `flags`;
  `null` when never evaluated) and `imported`; a held item needs attention. A top-level `accounting` section lists
  every `held` donation, the `review_both` (F-BOTH) items and `imported_with_evidence` (D8, report only), with
  `counts`, regardless of `--since`. Exit 1 also when any donation is held. Ids, codes and amounts only: no donor
  names, no payloads.
- PAY-15 stage 5b (D9, migration 0033): every evidence row stores `payment_amount_refunded_paise`, Razorpay's
  cumulative refunded amount from the signed payload's payment entity, only when that entity is the evidence's own
  payment (`id` = `payment_id`), its amount a positive integer, the refunded amount an integer 0..amount and its
  currency a supported one equal to the evidence currency; otherwise NULL (never 0). Rows recorded before 0033 are
  NULL. The `refund_snapshots` section compares, per provider payment, the latest snapshot S (by provider event time,
  then receipt / record time and id) with P / Q, the processed / pending amounts of that payment's current refund
  references: `missing_refund_evidence` (S > P + Q, a likely missed refund webhook: exit 1), `pending_refunds`
  (P < S <= P + Q), `snapshot_behind` (S < P); `consistent` and `no_snapshot` are counted. **Diagnostic only**: the
  snapshot is not in `payment_integrity_current` and no evaluation, reversal, hold, credit, total or figure reads it.
  Whether Razorpay counts a created (pending) refund, or a lost dispute, in `amount_refunded` is not yet confirmed.
- Exit 0 when nothing needs attention, 1 when something does, 2 on a refused argument.
- Needing attention means open or lost disputes, refunds, mismatches, conflicts, or a non-`payment` match.
- No timer.

**Cutover / rehearsal ETL:** `database/etl/rehearsal.py` refuses to load, before changing anything, unless the target is a
fresh database at head (no payment evidence, no other data; `docs/operations/legacy-cutover.md`).

**Reversal accounting (PAY-15 stage 3, migration 0032), OFF by default.** `HYPERCHAT_PAYMENT_ACCOUNTING_ENABLED`
defaults to `false`; since stage 6a-4 a true setting is accepted under the switch gate below. With it off, evidence
changes no money (as above). The code path it enables:
- After recording evidence for a donation, and after a capture (evidence can arrive first), the repository locks the
  donation (before the evidence insert, so concurrent deliveries cannot deadlock), reads its current evidence and
  evaluates it with `app/payments/accounting.py` (PAY15-D2 to D4, D8).
- It calls `public.donation_accounting_apply` (SECURITY DEFINER, `hyperchat_app` only). A clear result is accepted
  only when it equals max(processed refunds, lost deductions) over **all** counted evidence of the donation's
  completing payment, every counted reference named, and nothing that must be held (conflicting, a currency mismatch,
  a non-INR or deduction-less loss); a hold freezes the applied reversal. Imported donations are refused (D8).
- State: `donation_reversals` (runtime roles SELECT only). The donor total moves only through the credit writer:
  target = paid and approved ? ceiling - floor(ceiling x reversed / captured) : 0, ceiling =
  `COALESCE(creditable_inr, amount_inr)`.
- History: `donation_credit_events` rows `evaluation` / `hold` / `resolved` (reversal and hold before and after, the
  evidence references, no money of their own) next to the writer's row that moved the money (same `txid`). A replay
  writes nothing.
- Known PAY-13 semantics: a loss and a win in the **same phase** are `conflicting` in the current-state view, so the
  applied reversal stays frozen (held) until resolved; a win at a later phase restores it exactly.

**Accounting state (PAY-15 stage 6a-1, migration 0034), OFF.** The startup refusal above is unchanged; 0034 adds the
database switch the activation tools drive (6a-2 verify and 6a-3 operator CLI below; 6a-4 not built).
- `payment_accounting_state` (one row): `state` off / active / frozen (created **off**) and `dispute_mode` hold /
  apply (created **hold**). Only the owner changes it (`payment_accounting_set_state`, operator + reason 10-500
  characters); allowed: off→active, active→frozen, frozen→active, frozen→off (only once nothing is applied or held);
  the mode changes only while off or frozen. Every change appends a `payment_accounting_events` row (append-only for
  the owner too). Runtime roles read it only.
- Evaluation happens **only while active**: `donation_accounting_apply` returns `inactive` otherwise and changes
  nothing. Lock order L-1: every payment transaction that may evaluate (confirm, capture webhook, status poll,
  `payment-reconcile`, integrity evidence) takes the state row FOR SHARE first (the API through
  `payment_accounting_lock_state()`: FOR SHARE needs UPDATE privilege, which the API role has not), then the
  donation, the donor total, jobs, the outbox. A freeze takes the state row FOR UPDATE: it commits only after
  the evaluations holding the row (evaluations that start after its commit see `frozen`), waits at most **5 s**
  (S6A-D6, the function's own `lock_timeout`; then `55P03`, rolled back, no event) and can be delayed by a steady
  stream of evaluations. Moderation never takes it.
- Refunds-only (dispute mode `hold`, S6A-D3): a current attributed lost dispute is held as `H-DISPUTE-MODE` (the
  lowest-priority hold) and the database refuses any clear result while one exists. The hold freezes the applied
  reversal (S6A-D3a: a refund arriving after the loss is **not** applied; the difference stays visible as a held item
  in `payment-integrity-report`, never reconciled).
- `donation_accounting_evaluations` (S6A-D8, operational metadata, not history): per donation the evidence
  fingerprint (md5 over the payment id, the dispute mode and every current evidence row's reference, kind, match,
  payment, status, phase, amounts, currency and conflict flag), when and in which state it was evaluated, the outcome,
  and a keep acknowledgement. A no-op evaluation refreshes it but writes no history and moves no total.
- Owner-only operations, each needing an operator run (`payment_accounting_runs`, append-only) of its command:
  `payment_accounting_reconcile` (activation, state active), `payment_accounting_keep` (state active or frozen;
  releases **only** `H-DISPUTE-MODE`, never an integrity hold, money unchanged, acknowledged for exactly the current
  fingerprint: new or changed evidence or a mode change makes it hold again), `payment_accounting_apply_evaluation`
  (state active; a held donation, only the clear result the evidence supports), `payment_accounting_revert` (state
  frozen or off; reversal 0 and hold cleared through the one credit writer). History rows of causes `manual`,
  `activation` and `revert` must name their run (`run_id`, CHECK). An operation that moves a reversal inserts the
  realtime `goal-progress` and `leaderboard-updated` events in the same transaction, after the writer (as
  `emit_totals_changed`, coalesced while one is pending); keep and no-op calls emit nothing.
- Downgrade of 0034 refuses once the state, the mode, an event, a run, an evaluation or any 0034 history exists.

**Accounting verification (PAY-15 stage 6a-2), read-only.** `HYPERCHAT_READONLY_DSN=… python -m app.payments.operator
verify --max-seconds N [--creator ID | --donation ID] [--output FILE]` (`app/payments/verify.py`) checks migration
0034's data and changes nothing. The writing commands (`state`, `reconcile`, `resolve`, `revert`) are stage 6a-3
(below). The scope id and `--max-seconds` are validated **before any connection**: an empty, blank or
malformed id, or a missing, non-finite or non-positive budget, is refused (exit 2) without connecting.
- **Session and statement protocol (audit correction rev. 7 §2.7):** `hyperchat_readonly` only (session and current
  user), head `0034` or `0035` (0035's objects are I10 expectations at head 0035), PostgreSQL ≥ 15, one `REPEATABLE READ READ ONLY` transaction (its snapshot is the first data
  statement's). The connection issues **only**: `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY`; `SET LOCAL
  lock_timeout = '5s'`, `SET LOCAL row_security = off`, `SET LOCAL statement_timeout = '<ms>ms'`; `DECLARE vfy_<n>
  NO SCROLL CURSOR WITHOUT HOLD FOR <constant>` (with its parameters) / `FETCH FORWARD <n> FROM vfy_<n>` / `CLOSE`
  for **every** data query, whose text must be an exact approved module constant (`SELECT` / `WITH`), so no result,
  catalog queries included, is ever fetched whole; and `ROLLBACK`, always. Anything else is refused before it is
  sent (`verify.statement_allowed`).
- **Deadline and ceilings:** one monotonic deadline `max_seconds` from entry (no production default; the budget and
  the snapshot-lifetime limit are owner decisions). Each data statement and `FETCH` gets `min(120 s, remaining)`;
  below 50 ms remaining it is refused before it is sent. Ceilings (provisional values, owner item 7, printed in
  `runtime.provisional_ceilings`): **rows** fetched (each FETCH asks for at most one row beyond the ceiling);
  **bytes**, the retained size of every fetched value including nested arrays, JSON and strings (`sys.getsizeof` of
  each distinct object, counted row by row before the row is kept; an estimate of Python memory, not process RSS,
  and the driver still holds one FETCH batch before the check) **plus** the report's canonical size, streamed and
  checked before serialization; and **CPU work**, charged one unit per item **as each scan actually iterates**
  (rows retained, events, versions of one reference, observations and refunds of one snapshot, catalog rows and
  role edges, enumeration steps, findings, serialization chunks), so the clock is read at least every
  `CHECKPOINT_EVERY` units of real progress, even inside one hot collection; the single report sort is the one
  bounded atomic operation (charged before, checked before and after). Measuring retained bytes checks the clock
  without counting work. Exhausting any refuses the run. The report carries the actual `runtime.rows_loaded`,
  `bytes_loaded` and `work_units`; `work_units` excludes the report's own size check and serialization
  (`work_units_scope`), which are enforced but cannot be counted inside the report. The report is built,
  `ROLLBACK` releases the
  snapshot (the connection is closed if that fails), the release is confirmed (idle or closed), and only then is the
  deadline checked: **no success is returned after the deadline**, including one crossed during `ROLLBACK`. Cleanup
  (`CLOSE`, `ROLLBACK`, close) always runs whatever time remains. A measured bound, not a hard real-time guarantee.
- **Row security:** before any financial read, any row security or policy on a relation the verifier reads refuses
  the whole report (`row_security: <rel>`, no findings); a policy appearing later makes the filtered read error under
  `row_security = off` (`row_security: read_filtered`). Any SQL error, cancellation or timeout is **refused**.
- **Invariants:** I1 credit bounds; I2 credited = the writer's target; I3 / I3-O per-donation and orphan chains and
  I6 live-bucket continuity (Erratum 1: deleted-creator buckets excluded and counted); I4 reversal / hold chains; I5
  totals rows; I7 Σ credited ≤ total (the pre-writer baseline reported); I8 no accounting on imported or ambiguous
  donations; I9 identity; I10 the PostgreSQL security baseline (below) and the verifier's own copy of the
  current-evidence view; I11 bounds and vocabularies, refunds-only holds (R-DM);
  I12 agreement with the audited revert exception (a keep is never an exception on 0034: its acknowledgement is only
  checked for forgery, A1), I12-H every clear row checked against every recorded version of its evidence references
  (S2, below); I13 evidence
  processing; I14 one accounting row per (donation, transaction); I15 SQL figures = the Python reference; I16
  evidence attribution (`evidence_attribution`: `unattributed`, `wrong_donation`, `account_mismatch`,
  `ref_identity_conflict`; ownership is the completing payment plus its account, never the order id); I17 the
  provider refund snapshot against processed and pending refunds (`refund_snapshot_mismatch`: same currency only,
  never converted; the candidate set is the latest provider time plus every undated observation; any decrease across
  the dated chronology is `snapshot_regression`; unusable refund rows are reported even without a snapshot); I18 the
  accounting state event chain (structural; a defect suspends mode-dependent I12-H conclusions).
- **I10, the PostgreSQL security baseline (rev. 7 §5), classified in Python over captured catalog rows
  (`verify.i10_problems`):** application-role attributes; other superusers (`unexpected_superuser`) unless on the
  administrator allow-list; **default-deny** role membership (only `hyperchat_migrator → hyperchat_owner`, stock
  predefined edges and administrators' own memberships), with direct and transitive `admin_path`, `owner_path` and
  `predefined_role_path` evaluated per version (PG16+ set / inherit options; PG15 `rolinherit`); the database owner
  (`hyperchat_owner`); relation, function and schema ownership; effective table / column rights on the accounting
  and evidence tables, and the **minimum `donations` write check** (only `hyperchat_app` / `hyperchat_worker` may
  INSERT / UPDATE / DELETE; nobody else, PUBLIC included, has any table or column write right); signature-exact
  function EXECUTE against the migration-derived ACL baseline, with grant options; schema CREATE; PUBLIC ACL entries;
  exact trigger bindings (function, timing, events, WHEN, args, constraint, columns; any other trigger is
  `unexpected_trigger`); and every pinned function's identity (`overloaded`, return type, language, volatility,
  parallel, strictness, security, owner, `proconfig`, source pin); and, since A5b, the **explicit ACL entries of
  schema `public`, the scoped tables and their columns against the owner-approved 0034 baseline** (OD-5, version-keyed:
  PG17 adds the owner's MAINTAIN), with the OD-11 codes `schema_acl` / `schema_acl_missing`, `table_acl` /
  `table_acl_missing`, `column_acl` / `column_acl_missing` (an altered entry is the missing + unexpected pair; PUBLIC
  and grant-option entries keep their own codes). An unreadable ACL row refuses the report (`acl_unreadable`); an
  unpinned PostgreSQL major is refused before any statement, and the effective-rights question includes MAINTAIN on
  PG17. **The values are populated but NOT activated: `I10_BASELINE_CONFIRMED` is still False and every original
  unverified entry is still reported, so I10 reports `baseline_unverified` (structural) and no report is clean before
  A5c.**
- **Administrator allow-list (provisional location, owner and format: owner item 5):**
  `/etc/hyperchat/verify-administrators`, one role name per line, read by the CLI (outside the database and the
  invocation). Missing: no approved administrator. **POSIX only:** opened without following a symlink and validated
  and read through that one descriptor; it must be a regular file **owned by uid 0** (the provisional trusted
  owner), not group- / other-writable, under ancestor directories owned by root that are not group- / other-writable
  (unless sticky). Anything else (a symlink, another owner, a writable file or directory, a FIFO, malformed or
  non-ASCII content) refuses the run before any connection. **Windows:** an existing allow-list refuses (ownership
  and symlink guarantees are unavailable); a missing one approves nobody. Its SHA-256 is printed in `administrators`
  as evidence of which file was read, not of its authorization. An application role on the list is never trusted.
- **Operator runs (D-10, the plan rev. 3 §6 scope contract the 6a-3 CLI will write):** `{"kind": "donations",
  "donations": [...]}`, `{"kind": "creator", "creator": …}`, `{"kind": "all_v2"}` (reconcile only) or `{"kind":
  "recovery", "donations": [...], "verify_report_sha256": …}`, each with `expires_at` (RFC 3339, within 24 h of the
  run). An unused run without a scope is attention; a used one, a malformed scope, a missing run, a use outside the
  scope, after `expires_at` or under the wrong command is an audit violation and never an I12 exception.
- **0034 timing limits (never hidden):** event timestamps are transaction starts and a transition's commit is
  unbounded (L-2 / L-2b), so verify **never asserts a missed evaluation or an overdue 24 h reconciliation**: an
  uncovered origin while active is `in_flight` (< 10 min) or `boundary_uncertain`, a backlog past the nominal window
  `deadline_uncertain`, with the 500 ppm clock margin on the window (`pending` only when provably inside). A backlog
  item is reconciled only when its evaluation observed `active` under the state lock (`ev.state`), on the current
  fingerprint, and I12 agrees, never by `evaluated_at`. A missed evaluation later covered leaves no trace (L-1).
  Legacy I12-H ambiguity (`historical_ambiguous`) is permanent; a later row never supersedes it.
- **Historical dispute mode and evidence order (CR-2, owner decisions 2-4):** stamps are transaction starts and
  commits are unbounded, so neither a row's mode nor its evidence state is chosen by timestamp proximity. A clear
  row's possible modes are every active period's mode, minus `hold` when its refs name a dispute (the core refuses a
  clear result in hold while a lost dispute is current). Evidence commit order is unknowable on 0034 (random uuid
  ids, `recorded_at` a transaction start: Decision 5), so a row's refs may have had any of their recorded versions.
  `unsupported_amount` (structural) only when no version, in any possible mode, gives the stored amount; supported
  under some modes only: `historical_ambiguous` (`mode_order`); several amounts: `evidence_cut`; the refs determine
  the amount but no `recorded_at` prefix within ±5 min reproduces it: `evidence_order`; otherwise exact. Exact proves
  arithmetic consistency with the refs, **not** that the evidence state existed: some self-consistent forged
  historical attribution can pass I12-H, and I4 / I12 may catch it but do not guarantee it. A keep acknowledgement is
  **never proven** on 0034 (A1), and 0035 cannot prove it retroactively: one that any evidence state under any mode
  the state ever had could give is `historical_ambiguous` (`ack_mode_order`, no exception); none: `forged_ack`. The
  report's `limitations` (L-1, L-2, L-2b, D5-1 to D5-5) say the same. Enumeration is bounded (4 096 states, 12
  events per ref): over the bound, or if the fingerprint replica fails, the case is uncertain, never exact or
  forged. The A1 product is built ref by ref and stops expanding once it is provably over 4 096, so its reported
  `combinations` is the product reached at that point (exact up to the bound; a lower bound beyond it). Ambiguities carry `basis` (`mode` /
  `evidence`), are permanent and block `activation_verification_ready`. A mode change or capture whose stamp precedes
  the evaluation is an uncertain origin (`boundary_uncertain`), never `indeterminate`.
- **Report:** canonical JSON (ids, enums and amounts; donor buckets as creator id plus a name digest) with a SHA-256
  `digest` over everything else. Status = the worst severity of the closed class table (`verify.SEVERITY`); exit
  **0 clean, 1 violation, 2 refused, 3 attention** (D-7). A class outside the table refuses the report. Findings are
  de-duplicated by (invariant, class, entity, canonical detail) and sorted. Every `historical_ambiguous`,
  `evidence_attribution` and `refund_snapshot_mismatch` finding must match **exactly one** closed variant (rev. 7
  §3.2, `reason` the discriminator), or the whole report is refused. `write_blockers` lists each blocking finding
  (`s2_over_bound`, `chain_invalid`, `a1_over_bound`, `a1_replica_failed`, every I16 / I17 reason). Also reported:
  `limitations` (rev. 7 §9), `administrators` and `runtime.provisional_ceilings`.
- **Output (`--output FILE`, rev. 7 §2.7.4): never overwritten, never substituted.** POSIX with `/proc/self/fd`
  only: the directory is opened once (`O_DIRECTORY | O_NOFOLLOW`) and must be owned by root or the operator's uid
  and not group- / other-writable, with trusted ancestors; every later step is relative to that open directory. An
  exclusive temporary file (`O_EXCL | O_NOFOLLOW`, 0600), write, fsync; the temporary name must still identify the
  inode written; then the **open inode itself** is hard-linked to `FILE` (the commit point), so a pathname swap can
  never publish foreign content (a swap is refused as `substituted`). An existing `FILE` (or symlink), an untrusted
  directory or any failure before the link publishes nothing: stdout carries a refused report
  `output_not_published: <step>` (exit 2), and only a temporary name still bound to this run's inode is removed.
  A cleanup failure after the link leaves the report published (its own exit code) and names the leftover on
  stderr. **Windows (and POSIX without `/proc/self/fd`): `--output` refuses** (`unsupported_platform`); stdout
  still carries the report.
- **Gates:** `reconcile_precondition` (full scope, active, no violation, **no write blocker**),
  `recovery_precondition` (full scope, active, at most `overdue_reconciliation`, **no write blocker**),
  `activation_verification_ready` (full scope, active, status clean: the part verify can establish) and
  `activation_complete`, which is **always false** in a verify report: it also needs the 6a-3 second report-only
  reconcile to plan zero changes (plan §4.3), which verify never attests (`not_attested`). A scoped or refused run
  sets every gate false. Clean is necessary, never sufficient, and nothing here activates accounting.
- **Tests:** `tests/payments/test_verify_units.py` and `test_verify_rev7_units.py` (no database: variants, gates,
  I12-H, A1, I16-I18, captured-catalog I10, the statement protocol and deadline on a scripted connection, and
  publication in temporary directories) and `test_verify_db.py` (a fresh copy of the pristine
  template `hyperchat_verify_tmpl` per test, created by `setup-test-db.sh`; `HYPERCHAT_TEST_VERIFY_ADMIN` names the
  throwaway server's superuser connection).

**Accounting hardening (PAY-15, migration 0035).** Two fixes on 0034's operator surface (OD-35-S):
- **F-7:** foreign keys bind `donation_accounting_evaluations.ack_run` and `donation_credit_events.run_id` to
  `payment_accounting_runs` (the upgrade refuses dangling rows first), and a row trigger
  (`donation_accounting_evaluations_ack_guard`) refuses setting or changing a keep acknowledgement outside
  `payment_accounting_keep` (42501 "(F-7)"; clearing one is allowed).
- **F-11:** `payment_accounting_reconcile`, `_keep`, `_apply_evaluation` and `_revert` check, at use, that the run's
  scope covers the donation (`payment_accounting_scope_covers`, the rule-for-rule port of `verify.parse_scope` +
  `scope_covers`: exact keys, a duplicate-free uuid list, `expires_at` within 24 h of the run and not passed,
  `all_v2` = V2 donations for reconcile only). Anything else is refused (42501 "(F-11)"). `open_run` is unchanged, so
  an unscoped run can still be opened but never used; `donation_accounting_core` stays owner-internal.
- Downgrade restores 0034's function bodies exactly and drops the trigger, the two functions and the two keys (no
  data is lost, so it is not refused).

**Operator CLI (PAY-15 stage 6a-3, `app/payments/accounting_ops.py`), report-only by default.**
`HYPERCHAT_ACCOUNTING_OWNER_DSN='… user=hyperchat_migrator' python -m app.payments.operator {state [--to off|active|
frozen] [--mode hold|apply] | reconcile | resolve keep|apply | revert} [--donation UUID …] [--apply
--confirm-database DB --operator ID --reason TEXT]`. The connection sets role `hyperchat_owner`; every write goes
through 0034's owner functions.
- Arguments are validated **before connecting** (exit 2): `--apply` needs `--confirm-database` equal to the connected
  database, `--operator` matching `[a-z][a-z0-9._-]{1,62}` and a `--reason` of 10-500 characters; donation ids are
  uuids without repeats. Each command also checks the accounting state first (reconcile and resolve apply: active;
  resolve keep: active or frozen; revert: frozen or off) and opens no run otherwise.
- The plan is the application's own evaluator (`accounting.evaluate_in_mode` over `payment_integrity_current`); the
  database re-derives and refuses anything else. A writing run always carries a `donations` scope naming exactly the
  planned donations, expiring in 1 h (OD-6a3-S: never unscoped, creator or all_v2). One transaction per donation; the
  first refusal stops the run (earlier donations stay written). Reconcile re-plans after writing: the second report
  must plan zero.
- Output: one canonical JSON line. Exit codes (D-7): 0 nothing to do / all applied, 3 work planned (report), 1 a
  donation refused or a reconcile's second report not zero, 2 refused before anything was written.
- **Tests:** `tests/payments/test_operator_units.py` (no database) and `test_operator_db.py` (PostgreSQL 15 and 17).

**Switch gate (PAY-15 stage 6a-4, `app/payments/switch.py`).**
- Evaluation runs only when `HYPERCHAT_PAYMENT_ACCOUNTING_ENABLED` is true **and** the database state is `active`,
  read inside each evaluation transaction (`repository._settle_accounting` under `payment_accounting_lock_state`,
  L-1). `frozen` or `off` stops evaluation at once with no restart; applied reversals stay, and moderation keeps using
  the applied value (F12). Activation is the 6a-3 reconcile, whose second report plans zero (F13); revert restores
  exactly the history without evidence-based reversals, leaving other donations and earlier ledger rows (F14).
- **Startup precondition:** with the setting true, the API and `payment-reconcile` refuse to start (exit 2 for the
  CLI) unless the database carries migration 0035: `payment_accounting_scope_covers`, the enabled
  `donation_accounting_evaluations_ack_guard` trigger and both validated foreign keys. This is a catalog check:
  runtime roles cannot read `alembic_version` (0007). An unreachable database refuses too.
- **L4 deploy check** (read-only, before any code rollback): `HYPERCHAT_READONLY_DSN=… python -m
  app.payments.operator deploy-check --target-release <release> [--releases-root /opt/hyperchat/releases]`.
  `<release>` is the 12-hex release id exactly as the host names its directory (`0963113493c7`), or a 40-hex commit
  matching a directory named by the full commit or by its 12-hex prefix. A release carries the reversal-aware writer
  iff its tree has migration 0032 (`ACCOUNTING_WRITER_REVISION`). Exit 2 (refused) when the target lacks it while any
  `applied_reversed_paise > 0` (revert first, L3), and on a malformed target, no matching or an ambiguous (several
  matching) release tree, an unreadable releases root or a database it cannot read; exit 0 otherwise. Canonical JSON
  out. An operator guard against an accidental rollback, not a tamper control.
- Deployment of 6a-4 to DEV or production is a separate owner decision (stage 6b, D-19 attestation, G4).
- **Tests:** `tests/payments/test_switch_units.py` (no database) and `test_switch_db.py` (PostgreSQL 15 and 17).

**Live figures (PAY-15 stage 4).** Goal progress (public and realtime), dashboard revenue and the CSV export read the
applied reversal through `app/payments/figures.py`: per donation `reversed_inr` = the pro-rata share of the stored
`amount_inr`, rounded down to the paisa (exact integer division), and `net_inr = amount_inr − reversed_inr`; sums are
of per-donation values. The leaderboard reads donor totals, which the credit writer already reduces. With no
reversal (the switch off), every figure equals the gross figure exactly.

## Configuration (`/etc/hyperchat/api.env`, names only; see `deploy/api.env.example`)
`HYPERCHAT_RAZORPAY_PRIMARY_{KEY_ID,KEY_SECRET,WEBHOOK_SECRET}`. `HYPERCHAT_PAYMENT_ACCOUNTING_ENABLED` stays unset
or `false` until an owner-approved activation (stage 6a-4 switch gate: a true setting needs migration 0035).
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
