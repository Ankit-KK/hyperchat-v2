# Transactional email: multi-category architecture (proposal)

**Status: PROPOSAL for review. Nothing here is implemented.** No configuration, DNS or credential was changed, and no
email was sent. Implementing it needs separate approval. The limited delivery smoke test (#103) uses only the verified
onboarding domain and is separate from this proposal.

## 1. What exists today (audit of `main`)
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
| T2 | Admin invitation (`POST /auth/invites`; role user / streamer / admin) | `auth/service.py` `create_invite` | `invite` | `require_admin`; step-up grant for admin or creator-linked invitations (S3); the token is stored hashed | **Not caught.** A provider error propagates after the invitation row exists, so the admin gets a generic 500 (finding F1) |
| T3 | Application owner invitation (`POST /admin/applications/{id}/invite`) | `admin/applications.py` `invite` | `invite` | `require_admin`; step-up; only approved applications; a retry revokes the previous link | 502 `invite_delivery_failed` (exception type logged), and a retry issues a fresh link |

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
### 2.1 Categories, senders, routing
| Category | Domain (DEV) | Sender | Templates (today → later) |
|---|---|---|---|
| `onboarding` | `onboarding.quicktestspace.site` (**verified**) | `HyperChat <no-reply@onboarding.quicktestspace.site>` | `invite` (T2, T3) |
| `auth` | `auth.quicktestspace.site` (not yet verified) | `HyperChat Security <no-reply@auth.quicktestspace.site>` | `reset-password` (T1); later security alerts |
| `notifications` | `notifications.quicktestspace.site` | `HyperChat <no-reply@notifications.quicktestspace.site>` | none yet |
| `payments` | `payments.quicktestspace.site` | `HyperChat Payments <no-reply@payments.quicktestspace.site>` | none yet (needs a donor-email decision) |
| `support` | `support.quicktestspace.site` | `HyperChat Support <no-reply@support.quicktestspace.site>` | none yet (needs a support system) |

**Rules:**
- **A fixed, code-owned map from template to category** (`app/email/categories.py`). Callers name the template only, never a sender or category, so a reset can never be sent from onboarding.
- **Every template maps to exactly one category.** A template without a category is a startup error.
- **No fallback between categories.** If a category isn't active, its send is refused; it is never re-routed.

### 2.2 One central service
`app/email/service.py`: `EmailService.send(template, to, *, link=None, idempotency_key=None) -> Delivery`. It replaces `AuthService.sender` at the three call sites. Those sites keep their own authorization, rate limits, token handling and responses unchanged.

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
- **Verification is an operator step, never inside the API process.** A sending-only key can't read domain status. A root-only CLI, `app.cli email-domain-check --category X`, would use a full-access key from a root-only file outside `api.env` to confirm `verified` before the operator sets `VERIFIED_DOMAIN`. Resend also refuses an unverified domain at send time (403), which is classified as permanent.

### 2.4 Provider errors, retries, idempotency
- **Classification:**
  - 2xx: `sent`, with the provider message id recorded;
  - 401 / 403: `refused_auth` (wrong key, key revoked, or the domain isn't allowed for the key) — permanent, with a security event;
  - 400 / 422: `rejected` — permanent;
  - 429 / 5xx / network or timeout: `transient`.
- **T1 (reset):** at most 2 attempts inside the existing 15 s deadline and slot, with the same `Idempotency-Key` (derived from the reset token's hash). It still never raises to the caller.
- **T2 and T3 (invites):** no automatic retry, because the call is synchronous. Both answer 502 `invite_delivery_failed` (T2 gains this, fixing F1), and an admin retry issues a fresh link.
- **Tokens are never queued.** A raw link or token is never written to the jobs table or any log. Later non-secret notifications may use the existing job queue (`dedupe_key`, backoff) with the same idempotency rule.
- **Every outcome has a `Delivery` status.**

### 2.5 Delivery logging and privacy
- `security_event("email_delivery", category, template, status, reason, provider_message_id, recipient_hash)`.
- The recipient hash is a SHA-256 of the lower-cased address, the same as the rate-limit keys.
- Never logged: the address, link, token, key or rendered body.
- **Optional, P1, only if delivery records must be queried:** migration `email_deliveries` (id, category, template, recipient_hash, status, reason, provider_message_id, created_at). The runtime role could only insert; rows are removed after 90 days by `app.maintenance`. **P0 needs no migration.**

### 2.6 Security of the existing flows
- Reset keeps the generic 202, the rate limits, background dispatch, hashed tokens and active-account-only behaviour.
- Invitations keep `require_admin`, step-up for admin or creator-linked invitations, and the S3 database guards.
- Only the sender selection changes, plus F1's error handling.
- **Owner decision D1:** should admin-role invitations (T2 with `role=admin`) go from `onboarding` (proposed: it is an invitation) or from `auth`?

## 3. Priorities
| Priority | Work | Notes |
|---|---|---|
| **P0** | `EmailService` with the fixed template → category map; per-category config and startup validation; refusal of inactive categories; Resend client with classification and `Idempotency-Key`; delivery logging; T1–T3 moved onto it; **F1 fix** (T2 answers 502) | No migration. Activate `onboarding` first (verified), then `auth` only after `auth.quicktestspace.site` is verified |
| P0 (ops) | #103 opt-in delivery check from `no-reply@onboarding.quicktestspace.site`; later the same check per category | Each real send approved |
| P1 | `email-domain-check` CLI; optional `email_deliveries` table; security alerts (password changed / reset completed / admin email corrected) on `auth` | Alerts are new behaviour (owner approval of content and recipients) |
| P2 | Application decision and welcome emails (`onboarding`); notifications; payments (needs a donor-email and consent decision); support (needs a support system) | Each is new product behaviour |

## 4. Testing strategy
- **Routing:** every template maps to exactly one category; `reset-password` can never use the onboarding client or key; a payments or support template (when added) can never fall back. Wrong-category routing is refused even when another category is active.
- **Configuration matrix:** inactive category; missing key; missing or mismatched verified domain; `from` on another domain; production with a DEV domain and the reverse; a shared key for `auth` / `payments` — each refused at startup or before any HTTP call. The default config sends nothing.
- **Provider, through an `httpx.MockTransport`:** 2xx with the message id; 401 / 403 / 400 / 422 permanent; 429 / 5xx / timeout transient; the `Idempotency-Key` stays the same across T1's retry; T1 still never raises; T2 and T3 answer 502.
- **Flows (existing suites stay green):** the forgot-password timing and enumeration tests; invite step-up and S3 guards; application invite retry; a new test for F1.
- **Leaks:** `caplog` and response scans prove no key, address, link or token is printed or returned.
- **Smoke (opt-in, allowlisted, approved):** the #103 check, extended per active category.
- **Mutation runs** on the category map, startup validation and the inactive-category refusal.

## 5. Not in scope
DNS changes, Resend account settings, installing keys on DEV or production, real sends, and frontend work. Each needs separate approval.
