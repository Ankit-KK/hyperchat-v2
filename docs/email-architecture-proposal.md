# Transactional email: multi-category architecture (proposal)

**Status (2026-10-25):**
- **All five DEV sending domains are verified in Resend.** Each has its own domain-restricted sending key.
- **No key is installed on DEV or production, and no category is active.**
- **P0 is implemented for review** (see section 6). Merging it, installing keys (`docs/operations/email-category-keys.md`) and activating categories each need separate approval.
- The opt-in delivery check (#103) still uses only the onboarding sender.

## 1. What existed before P0 (audit of `main`, 2026-10-25)
**Delivery path.** One global sender:
- **`HYPERCHAT_EMAIL_BACKEND`**: `disabled` (default) or `resend`.
- **`HYPERCHAT_RESEND_API_KEY`**: one key.
- **`HYPERCHAT_EMAIL_FROM`**: one sender; its default is the production domain, `HyperChat <no-reply@hyperchat.space>`.
- **`AuthService.from_settings`** builds `DisabledSender` or `ResendSender` (`backend/app/auth/email.py`):
  - `DisabledSender` logs the template name only.
  - `ResendSender` POSTs to `https://api.resend.com/emails`. A status ≥ 300 raises a generic `RuntimeError`, and the status is logged. There is no retry, no idempotency key, and the provider message id is not recorded.
- **On DEV and production today:** `disabled`, no key.

**Templates** (`backend/app/auth/templates/`): `invite.html` and `reset-password.html`. Each holds one `{{ .ConfirmationURL }}` placeholder.

**Every email trigger in the backend.** There are exactly three call sites of `sender.send`:

| # | Trigger | Code | Template | Authorization / protections today | Failure handling today |
|---|---|---|---|---|---|
| T1 | Password reset (`POST /auth/password/forgot`) | `auth/service.py` `request_reset` → `send_reset_link` | `reset-password` | Generic 202 for every address (no enumeration); IP and per-email rate limits; the work runs after the response in a bounded background slot with a 15 s deadline; the token is stored hashed; only active accounts | Logged as `password_reset_email_failed` (exception type only); the caller still sees 202 |
| T2 | Admin invitation (`POST /auth/invites`; role user / streamer / admin) | `auth/service.py` `create_invite` | `invite` | `require_admin`; step-up grant for admin or creator-linked invitations (S3); the token is stored hashed | **Fixed (F1, #105):** 502 "Invitation delivery could not be confirmed"; the link is revoked and never resent, and a retry is a new invitation. See *Operator: unrevoked invitations* below |
| T3 | Application owner invitation (`POST /admin/applications/{id}/invite`) | `admin/applications.py` `invite` | `invite` | `require_admin`; step-up; only approved applications; a retry revokes the previous link | 502 `invite_delivery_failed` (exception type logged), and a retry issues a fresh link |

**Operator: unrevoked invitations (check before enabling real email delivery).**
- **When it applies.** After an unconfirmed invitation send, the API tries to revoke the link and records `invite_withdrawn`. If the revocation did not happen, it records `invite_withdraw_failed` instead:
  - at ERROR level, on the `hyperchat.security` logger;
  - as JSON on stdout, which goes to the systemd journal;
  - with `reason` set to `not_open` or the database error class, plus `invite_id` and the admin's `user_id`;
  - never the address or the token.
- **What it means.** With a database error, that link stays valid until it expires (`HYPERCHAT_INVITE_TTL_DAYS`, 7 by default). `not_open` means the invitation had already been accepted or revoked, so nothing needs revoking.
- **Find these events:**
  ```bash
  journalctl -u hyperchat-api --since today | grep '"invite_withdraw_failed"'
  ```
- **Revoke one that is still open.** Use the API's own `hyperchat_auth` role, which the guards allow to make exactly this change, and the environment's socket, port and database. On DEV that is port `5433` and database `hyperchat_dev`:
  ```bash
  sudo -u hyperchat psql -X -h /var/run/postgresql -p <port> -U hyperchat_auth -d <database> -c "UPDATE public.user_invites SET revoked_at = now() WHERE id = '<invite_id>' AND accepted_at IS NULL AND revoked_at IS NULL"
  ```
  `UPDATE 1` means it was revoked. `UPDATE 0` means it was already accepted or revoked.
- **Duplicates.** Even an unrevoked link can create at most one account for that address: a second acceptance is refused.

**Not implemented today.** Each of these is new product behaviour; none should be described as working:
- welcome emails;
- application approved / rejected notifications (approve and reject send nothing);
- creator onboarding sequences;
- security alerts: password changed, reset completed, new sign-in, passkey or role changes, admin email correction (D11 sends nothing);
- account recovery beyond the reset link;
- email-change confirmation;
- account-update or announcement emails;
- payment emails. **The backend collects no donor email address;** the legacy `sapphire.email` column was dropped in migration 0004;
- support tickets. There is no support system; D5 only publishes a contact address.

**Categories that exist today:** AUTHENTICATION (T1) and ONBOARDING (T2, T3). NOTIFICATIONS, PAYMENTS and SUPPORT have no trigger.

## 2. Proposed design
**This section is the design as proposed.** Section 6 is the authoritative list of what #106 implements. Anything marked **[P1, not implemented]** or **[not implemented]** does not exist in the code.

### 2.1 Categories, senders, routing
| Category | Domain (DEV) | Sender | Templates (today → later) |
|---|---|---|---|
| `onboarding` | `onboarding.quicktestspace.site` (verified) | `HyperChat <no-reply@onboarding.quicktestspace.site>` | `invite` (T2, T3) |
| `authentication` | `auth.quicktestspace.site` (verified) | `HyperChat Security <no-reply@auth.quicktestspace.site>` | `reset-password` (T1); later security alerts |
| `notifications` | `notifications.quicktestspace.site` (verified) | `HyperChat <no-reply@notifications.quicktestspace.site>` | none yet |
| `payments` | `payments.quicktestspace.site` (verified) | `HyperChat Payments <no-reply@payments.quicktestspace.site>` | none yet (needs a donor-email decision) |
| `support` | `support.quicktestspace.site` (verified) | `HyperChat Support <no-reply@support.quicktestspace.site>` | none yet (needs a support system) |

"Verified" means verified in Resend. **None is active:** every category is off until it is listed in `HYPERCHAT_EMAIL_ACTIVE_CATEGORIES`.

**Rules:**
- **A fixed, code-owned map from template to category** (implemented as `TEMPLATE_CATEGORY` in `backend/app/auth/email.py`). Callers name the template only, never a sender or category, so a reset can never be sent from onboarding.
- **Every template maps to exactly one category.** A template without a category is a startup error.
- **No fallback between categories.** If a category isn't active, its send is refused; it is never re-routed.

### 2.2 One central service
**[not implemented in this form]** `app/email/service.py`: `EmailService.send(template, to, *, link=None, idempotency_key=None) -> Delivery`. P0 implements the routing as `CategorySender` behind the existing `sender.send(Email)` interface instead, and returns no `Delivery` object (section 6). As proposed, it replaces `AuthService.sender` at the three call sites. Those sites keep their own authorization, rate limits, token handling and responses unchanged.

The service:
1. looks up the category and checks it is **active** (below);
2. renders the template, reusing today's files plus a small shared layout later;
3. sends with the category's **own Resend client** (its key and its `from`), with an `Idempotency-Key` header;
4. classifies the result;
5. logs it;
6. returns a `Delivery` (status, provider message id, reason code).

### 2.3 Configuration: off by default, fail closed
- **`HYPERCHAT_EMAIL_BACKEND=disabled|resend`** stays the global switch. With `disabled`, behaviour stays as today (dev convenience; nothing is sent).
- **`HYPERCHAT_EMAIL_ACTIVE_CATEGORIES=`** (empty by default) lists the categories explicitly activated, for example `onboarding`.
- **`HYPERCHAT_EMAIL_RECIPIENT_ALLOWLIST=`** (DEV and test only, implemented in A1) lists the only addresses any category may send to:
  - comma-separated plain addresses, matched exactly after lower-casing; no wildcards, and a plus-address is a different address;
  - enforced in `ResendSender` before any request to Resend, for every category and for the opt-in delivery check;
  - empty or missing: every email is refused (the API still starts and logs one warning);
  - malformed: the API refuses to start, naming entry positions only, never addresses;
  - production: never restricted, and the API refuses to start if the variable is set.
- **Per category `X`:**
  - `HYPERCHAT_EMAIL_X_FROM`;
  - `HYPERCHAT_EMAIL_X_RESEND_API_KEY`: a **domain-restricted sending key** for that category's domain; recommended one key per domain;
  - `HYPERCHAT_EMAIL_X_VERIFIED_DOMAIN`: set by the operator **only after** confirming in Resend that the domain is verified.
- **Startup validation** refuses to start unless every *active* category has:
  - a `from` address whose domain equals exactly its `VERIFIED_DOMAIN`, and that domain equal to the category's expected domain;
  - a key;
  - for `auth` and `payments`, a key that isn't the same value as another category's (shared keys are allowed only for onboarding, notifications and support, and only explicitly);
  - in production, no `quicktestspace.site` domain; on DEV and test, nothing else.
- **Refused before any HTTP call** (`EmailCategoryInactive`): a send in an inactive category, or with a missing key or domain.
- **The startup log** names active categories and their domains, never keys.
- **Verification is an operator step, never inside the API process.** The CLI described here is **[P1, not implemented]**; today the operator checks the domain in the Resend dashboard. A sending-only key can't read domain status. A root-only CLI, `app.cli email-domain-check --category X`, would use a full-access key from a root-only file outside `api.env` to confirm `verified` before the operator sets `VERIFIED_DOMAIN`. Resend also refuses an unverified domain at send time (403), which is classified as permanent.

### 2.4 Provider errors, retries, idempotency
- **Classification:**
  - 2xx: `sent`, with the provider message id recorded;
  - 401 / 403: `refused_auth` (wrong key, key revoked, or the domain isn't allowed for the key) — permanent, with a security event;
  - 400 / 422: `rejected` — permanent;
  - 429 / 5xx / network or timeout: `transient`.
- **T1 (reset):** **[P1, not implemented]** at most 2 attempts inside the existing 15 s deadline and slot, with the same `Idempotency-Key` (derived from the reset token's hash). It still never raises to the caller.
- **T2 and T3 (invites):** no automatic retry, because the call is synchronous. Both answer 502 (T2 since F1, #105: "Invitation delivery could not be confirmed", the link revoked and never resent), and an admin retry issues a fresh link.
- **Tokens are never queued.** A raw link or token is never written to the jobs table or any log. Later non-secret notifications may use the existing job queue (`dedupe_key`, backoff) with the same idempotency rule.
- **Every outcome has a `Delivery` status.** **[not implemented]** P0 raises `EmailCategoryInactive` or `EmailDeliveryError(reason)` instead.

### 2.5 Delivery logging and privacy
- **As proposed:** `security_event("email_delivery", category, template, status, reason, provider_message_id, recipient_hash)`.
- **As implemented in P0:** `hyperchat.email` logs `email sent` / `email send failed` / `email refused: category inactive`, with the category, template, status, reason and a sanitized provider message id (a plain id, else empty).
  - **No recipient identifier at all** (owner review): an unkeyed hash of an address can be reversed by guessing.
  - A keyed (HMAC) identifier needs a secret-management decision first.
  - **[not implemented]**
- Never logged: the address, link, token, key or rendered body.
- **Optional, **[P1, not implemented]**, only if delivery records must be queried:** migration `email_deliveries` (id, category, template, recipient_hash, status, reason, provider_message_id, created_at). The runtime role could only insert; rows are removed after 90 days by `app.maintenance`. **P0 needs no migration.**

### 2.6 Security of the existing flows
- Reset keeps the generic 202, the rate limits, background dispatch, hashed tokens and active-account-only behaviour.
- Invitations keep `require_admin`, step-up for admin or creator-linked invitations, and the S3 database guards.
- Only the sender selection changes, plus F1's error handling.
- **Owner decision D1 (answered):** every invitation goes from `onboarding`, admin-role invitations included. Password resets and future security events go from `authentication`.

## 3. Priorities
| Priority | Work | Notes |
|---|---|---|
| **P0** | `EmailService` with the fixed template → category map; per-category config and startup validation; refusal of inactive categories; Resend client with classification and `Idempotency-Key`; delivery logging; T1–T3 moved onto it; **F1 fix** (T2 answers 502). What was actually built is listed in section 6 | No migration. Activate `onboarding` first, then `authentication` (both domains verified; each activation approved separately) |
| P0 (ops) | #103 opt-in delivery check from `no-reply@onboarding.quicktestspace.site`; later the same check per category | Each real send approved |
| P1 | `email-domain-check` CLI; optional `email_deliveries` table; security alerts (password changed / reset completed / admin email corrected) on `auth` | Alerts are new behaviour (owner approval of content and recipients) |
| P2 | Application decision and welcome emails (`onboarding`); notifications; payments (needs a donor-email and consent decision); support (needs a support system) | Each is new product behaviour |

## 4. Testing strategy
- **Routing:** every template maps to exactly one category; `reset-password` can never use the onboarding client or key; a payments or support template (when added) can never fall back. Wrong-category routing is refused even when another category is active.
- **Configuration matrix:** inactive category; missing key; missing or mismatched verified domain; `from` on another domain; production with a DEV domain and the reverse; a shared key for `auth` / `payments` — each refused at startup or before any HTTP call. The default config sends nothing.
- **Provider, through an `httpx.MockTransport`:** 2xx with the message id; 401 / 403 / 400 / 422 permanent; 429 / 5xx / timeout transient; the `Idempotency-Key` stays the same for a repeated message (T1's retry is **[P1, not implemented]**); T1 still never raises; T2 and T3 answer 502.
- **Flows (existing suites stay green):** the forgot-password timing and enumeration tests; invite step-up and S3 guards; application invite retry; a new test for F1.
- **Leaks:** `caplog` and response scans prove no key, address, link or token is printed or returned.
- **Smoke (opt-in, allowlisted, approved):** the #103 check, extended per active category.
- **Mutation runs** on the category map, startup validation and the inactive-category refusal.

## 5. Not in scope
DNS changes, Resend account settings, installing keys on DEV or production, real sends, and frontend work. Each needs separate approval.

## 6. Implementation status (P0, #106, review-only)
**Implemented and tested in #106:**
- **Five categories,** each with its own `FROM`, `RESEND_API_KEY` and `VERIFIED_DOMAIN`. All are off by default (`HYPERCHAT_EMAIL_ACTIVE_CATEGORIES` empty).
- **Fixed routing:** `invite` → onboarding and `reset-password` → authentication. No cross-category fallback.
- **Startup validation:**
  - the domain and sender belong to the category;
  - the key is a Resend key, and is not shared with another category;
  - no category may be active in production.
- **Provider errors are classified;** an `Idempotency-Key` is sent per message.
  - **A 2xx is always success,** even when its body cannot be read (the message id is then empty).
  - **Logs** carry no recipient identifier, address, link, token, key or body.
- **Each owned HTTP client is closed** when the API stops. An injected client is left to its owner.

**Not implemented (P1/P2; do not rely on these):**
- the bounded retry for password resets (T1);
- the `email-domain-check` CLI;
- persistent delivery records (`email_deliveries`);
- the `EmailService` / `Delivery` interface;
- a keyed recipient identifier in logs;
- any new email trigger (security alerts, notifications, payments, support).

**Differences from section 2:**
- **Location.** The code lives in `backend/app/auth/email.py`; there is no new `app/email/` package.
  - `TEMPLATE_CATEGORY` (the fixed map) and `CategorySender` keep the `sender.send(Email)` interface, so the three call sites, their authorization, rate limits, tokens and responses are untouched.
  - `sender_from_settings` builds one `ResendSender` per active category.
- **Settings** follow the Razorpay per-account pattern in `app/core/config.py`, with startup validation in `Settings._validate_email_categories`.
- **Stricter than section 2.3:**
  - every active category needs its **own** key; no shared keys at all;
  - a key must look like a Resend key (`re_`);
  - **no category may be active in production** until production sending domains are decided (owner decision D2).
- **The legacy `HYPERCHAT_RESEND_API_KEY` / `HYPERCHAT_EMAIL_FROM`** are no longer used by the API. Only the opt-in #103 check reads them.
- **F1** was fixed separately in #105 (merged). With P0, an inactive onboarding category also answers that 502 and revokes the link.
