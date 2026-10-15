# Owner identity verification (Didit KYC)

This page covers KYC PR3, migration 0022: owner-approved manual creator approval with mandatory Didit KYC, decided 2026-10-15.

Every creator's **owner** verifies their identity with Didit. A verification belongs to the **user** (`users.id`, sent to Didit as `vendor_data`), never to a creator:
- a creator transfer never moves it;
- one verified person stays verified for every creator they own.

**What PR3 does not do:** it records verification only. It doesn't activate creators, doesn't change donation eligibility (`creator_donation_block`, migration 0020), and starts no rollout. That is PR4.

## Configuration (server-side only, `/etc/hyperchat/api.env`)
| Variable | What | Secret |
|---|---|---|
| `HYPERCHAT_DIDIT_API_KEY` | API key of this environment's Didit application (`x-api-key`) | **yes** |
| `HYPERCHAT_DIDIT_WEBHOOK_SECRET` | Webhook signing secret of that application | **yes** |
| `HYPERCHAT_DIDIT_WORKFLOW_ID` | The KYC workflow's UUID (the owner's "Free KYC" workflow: `6ff16570-fe12-4212-9647-b12b8898cc09`, **to be confirmed in the Didit console for each application**) | no |
| `HYPERCHAT_DIDIT_ENVIRONMENT` | `sandbox` on DEV, `live` in production; any other combination **refuses to start** | no |
| `HYPERCHAT_DIDIT_APPLICATION_ID` | optional; when set, webhooks from any other Didit application are refused | no |

- **When unset:** until the key, secret, workflow and environment are all set, KYC answers `503` and every other feature works unchanged.
- **Secrets:** never in the repository, logs, responses or the database.
- **No live calls:** tests use a fake Didit and never call Didit.

**Configuration checklist** (owner, per environment):
1. Create the Didit **application**: sandbox for DEV, live for production.
2. Confirm the workflow id exists in **that** application.
3. Set the five variables above. Keep the two secrets in the env file only (mode 600).
4. In the Didit console, add the webhook destination `https://<api host>/api/v1/webhooks/didit`, subscribed to **status.updated** (and **data.updated**).
5. Send "Try Webhook" deliveries and check them in the API journal (`kyc_webhook_*` events, codes only).
6. Set the callback page `<frontend>/kyc/return` (built by the server; the frontend must not trust its query string).

## API
| Endpoint | Who | What |
|---|---|---|
| `GET /api/v1/me/kyc` | signed-in user | `{approved, state, provider_status, updated_at, owns_creator, available, can_start}`. `state` is one of `none`, `in_progress`, `in_review`, `approved`, `declined`, `expired`, `not_completed` |
| `POST /api/v1/me/kyc/session` | the **owner** of at least one creator, including legacy ones (Origin + CSRF; 5 per hour) | Starts or resumes the session and returns `{session_id, status, url}`. `url` must be an https `*.didit.me` link. A linked moderator or a user without a creator gets 403; an already-verified user or one in review gets 409; a provider problem gets 503. |
| `POST /api/v1/webhooks/didit` | Didit | See "Webhooks" below |
| `GET /api/v1/admin/users/{id}/kyc` | admin | Sessions (status, workflow, environment, dates, check statuses) and the audit; no identity data |
| `POST /api/v1/admin/users/{id}/kyc/refresh` | admin (CSRF; 30 per hour) | Reconciles the newest session with Didit's decision endpoint (two attempts at most, no polling) |

**There is no endpoint that marks a user approved.**

## Webhooks: how a delivery is authenticated
These checks run in order; any failure stops processing.
1. **Format and size:** `Content-Type: application/json`, body at most 512 KB, a JSON object.
2. **Signature:** `X-Signature-V2` must equal the hex HMAC-SHA256, under the webhook secret, of the **canonical JSON of the whole body**. Canonical JSON means:
   - whole-valued floats become integers, recursively;
   - keys are sorted at every depth;
   - arrays keep their order;
   - separators are compact (`,` and `:`);
   - Unicode isn't escaped.

   The comparison is constant-time. `X-Signature` (raw bytes) and the deprecated `X-Signature-Simple` are **never** used.
3. **Freshness:** both timestamps must be within 300 s of now, in either direction:
   - the **signed** body `timestamp`;
   - the `X-Timestamp` header.

   Didit's page checks only the header, which isn't signed. The body timestamp is signed and refreshed on every retry, so it's the authority. The docs don't say the two are equal, so equality isn't required.
4. **Application:** `environment` must be this server's, and `application_id` must match when it's configured. Otherwise the answer is 403, which Didit doesn't retry.
5. **Replay and retries:** `event_id` is unique (`kyc_webhook_events`). Didit keeps it the same across retries, so a repeat answers `{"status": "duplicate"}` and changes nothing. A captured delivery replayed later than 5 minutes is refused at step 3.
6. **Identity:** the session must be one this server created, with the same `vendor_data` (the user) and the configured workflow. If not, the outcome is `identity_mismatch` and nothing is applied.
   - **Approved needs its decision:** it's accepted only with a `decision` object whose `session_id` and `status` match. Otherwise the outcome is `incomplete`.
   - **Unknown statuses** are never applied.
7. **Ordering:** the status is written by `kyc_record_provider_status`, which keeps the provider's order using the envelope `created_at` (the provider's update time):
   - an older event is `stale` and never downgrades a newer state;
   - a later `Declined` or `Kyc Expired` always replaces an approval.

**Early webhooks:** a signed event for a session not stored yet is kept as `unmatched`. It's applied when the session is stored, and only for the same user.

**Answers:** 200 for anything stored (Didit doesn't retry), 401 or 403 for authentication failures (not retried), and 5xx only for real server faults (retried).

## Status transitions
- **Statuses:** Didit's exact values are stored as they are: `Not Started`, `In Progress`, `Awaiting User`, `In Review`, `Approved`, `Declined`, `Resubmitted`, `Abandoned`, `Expired`, `Kyc Expired`.
- **The approval rule:** a user is approved (`public.user_kyc_approved(user, environment)`, or `KycService.kyc_approved`) only while one of their sessions in this environment is `Approved`.
  - A later `Declined` (revocation) or `Kyc Expired` removes the approval.
  - A sandbox approval never counts in production.
- **After a finished session:** once a session ends in `Declined`, `Expired`, `Abandoned` or `Kyc Expired`, the owner can start again, and Didit issues a new session. `Resubmitted` continues the same session.

## Privacy and storage
- **Stored:** session ids, workflow, environment, Didit status and time, and the **statuses** of the identity checks: `id_verifications`, `liveness_checks`, `face_matches`, `aml_screenings`.
- **Never stored or logged:** documents, selfies, biometrics, names, document numbers, birth dates, addresses, media URLs, the decision or the webhook body.
- **Retention:**
  - webhook receipts: deleted after 90 days by the daily cleanup;
  - sessions and the KYC audit: kept while the account exists (users with KYC history can't be deleted by the API's database role);
  - Didit's own retention is configured in the Didit console.
- **Database privileges:** the API's role cannot UPDATE or DELETE sessions or the audit. A status is written only by the `SECURITY DEFINER` function above, which also writes the audit row.

**Frontend (later):**
- **Consent:** before redirecting, tell the owner that Didit, an external provider, will process their identity document and a selfie on HyperChat's behalf. Link Didit's privacy notice and HyperChat's own, and get explicit consent.
- **Return page:** read the state from `GET /api/v1/me/kyc`, never from the return URL.

## Recovery
| Problem | What happens |
|---|---|
| Didit is down | The owner gets 503 and retries; no row is written. |
| Didit created the session but our write failed | The next start gets the same session back (Didit reuses an unfinished session for the same `vendor_data`) and stores it. |
| A webhook never arrived | `POST /api/v1/admin/users/{id}/kyc/refresh`. |
| Wrong environment or application deliveries | Refused with 403 and visible as `kyc_webhook_rejected` in the journal. |

## Testing with sandbox credentials (owner approval required)
DEV gets its own **sandbox** application and keys:
- Use Didit's sandbox scenarios and the console's "Try Webhook" (deliveries carry `X-Didit-Test-Webhook: true`).
- Try Webhook events don't match a stored session, so they're stored as `unmatched` and never applied.
- No live or chargeable verification is run without explicit approval.

## Still for PR4
- **Activation:** require `kyc_approved` of the current owner (activation is refused until then).
- **Enforcement:** the audited rollout, owner-bound expiring grants, restrictions and the startup guard.
- **Eligibility:** wire KYC into `creator_donation_block`.
- **Loss of verification:** handle a KYC loss after activation (immediate restriction).
