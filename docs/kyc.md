# Owner identity verification (Didit KYC)

This page covers KYC PR3, migration 0022: owner-approved manual creator approval with mandatory Didit KYC, decided 2026-10-15.

Every creator's **owner** verifies their identity with Didit. A verification belongs to the **user** (`users.id`, sent to Didit as `vendor_data`), never to a creator:
- a creator transfer never moves it;
- one verified person stays verified for every creator they own.

**What PR3 does not do:** it records verification only. It doesn't activate creators, doesn't change donation eligibility (`creator_donation_block`, migration 0020), and starts no rollout. That is PR4.

## Trust boundary
**The API's database role (`hyperchat_app`) can never make anyone approved.**
- **Read-only tables:** it only reads `kyc_sessions`, `kyc_webhook_events` and `kyc_events`.
- **Unreadable secret:** it cannot read `kyc_provider_config`, which holds the webhook secret.
- **Writes through functions only:** every write is a `SECURITY DEFINER` function, owned by `hyperchat_owner` (migration 0022).

| Function | What it does |
|---|---|
| `kyc_receive_webhook(canonical_body, signature, x_timestamp)` | Verifies the Didit signature **in the database** and applies only what the signed body says. **The only path to Approved.** |
| `kyc_create_session(user, session, workflow, version, environment)` | Stores a session: always `Not Started`, and only for this database's configured workflow and environment. Applies early webhooks. |
| `kyc_apply_refresh(session, status, checks, asked_at, actor)` | Records an admin refresh. Didit's decision endpoint isn't signed, so this **never approves**. |
| `kyc_purge_webhook_receipts(dry_run)` | The 90-day cleanup. |

**What this protects against:**
- A SQL injection or a leaked API database password can't approve anyone.
- Neither can a compromised API process: it has no webhook secret.

**Known limit:** a compromised API process still holds the Didit **API key**. It can record non-approving states (for example `Declined`) through the refresh path, but it can never approve.

## Configuration
**API settings** (`/etc/hyperchat/api.env`):

| Variable | What | Secret |
|---|---|---|
| `HYPERCHAT_DIDIT_API_KEY` | The API key of this environment's Didit application (`x-api-key`), used to start sessions and to refresh | **yes** |
| `HYPERCHAT_DIDIT_WORKFLOW_ID` | The KYC workflow's UUID. The owner's "Free KYC" workflow is `6ff16570-fe12-4212-9647-b12b8898cc09`, **to be confirmed in the Didit console for each application**. It must equal the database's. | no |
| `HYPERCHAT_DIDIT_ENVIRONMENT` | `sandbox` on DEV, `live` in production. Any other combination **refuses to start**. | no |

**Database** (`public.kyc_provider_config`, one row). The operator sets it with the migration role:
```
HYPERCHAT_MIGRATION_DSN=... python -m etl.kyc_provider_config --environment sandbox \
    --workflow-id <uuid> [--application-id <uuid>]
```
- **Run from:** `database/`.
- **The secret:** prompted without echo, or one line of stdin with `--secret-stdin`. It is never taken from argv and never written to `api.env`.
- **`--application-id`:** optional. When set, webhooks from any other Didit application are refused.

**Rules:**
- **When unconfigured:** until the API settings are set, starting a verification answers `503`; until the database row exists, webhooks answer `503`. Every other feature works unchanged.
- **Secrets:** never in the repository, logs or responses.
- **No live calls:** tests use a fake Didit and never call Didit.

**Configuration checklist** (owner, per environment):
1. Create the Didit **application**: sandbox for DEV, live for production.
2. Confirm the workflow id exists in **that** application.
3. Set the three API variables above, with the env file at mode 600.
4. Store the webhook secret with `etl/kyc_provider_config.py`.
5. In the Didit console, add the webhook destination `https://<api host>/api/v1/webhooks/didit`, subscribed to **status.updated** (and **data.updated**).
6. Send "Try Webhook" deliveries and check them in the API journal (`kyc_webhook_*` events, codes only).
7. Set up the callback page `<frontend>/kyc/return`. The server builds the URL; the frontend must not trust its query string.

## API
| Endpoint | Who | What |
|---|---|---|
| `GET /api/v1/me/kyc` | signed-in user | `{approved, state, provider_status, updated_at, owns_creator, available, can_start}`. `state` is one of `none`, `in_progress`, `in_review`, `approved`, `declined`, `expired`, `not_completed`, `reverification_required` (the newest session is `Approved` but the approval rule doesn't accept it). `approved` is always `public.user_kyc_approved`. |
| `POST /api/v1/me/kyc/session` | the **owner** of at least one creator, including legacy ones (Origin + CSRF; 5 per hour) | Starts or resumes the session and returns `{session_id, status, url}`. `url` must be an https `*.didit.me` link. A linked moderator or a user without a creator gets 403; an already-verified user or one in review gets 409; a provider problem gets 503. |
| `POST /api/v1/webhooks/didit` | Didit | See "Webhooks" below |
| `GET /api/v1/admin/users/{id}/kyc` | admin | Sessions (status, workflow, environment, dates, check statuses) and the audit; no identity data |
| `POST /api/v1/admin/users/{id}/kyc/refresh` | admin (CSRF; 30 per hour) | Reconciles the newest session with Didit's decision endpoint (two attempts at most, no polling). Answers the admin view plus `refresh`: `applied`, `unchanged`, `stale`, or `approval_needs_webhook`. **It never approves** (the response isn't signed). |

**There is no endpoint that marks a user approved.**

## Webhooks: how a delivery is authenticated
**Split of work:**
- **The API:** parses the body and computes its canonical JSON (what Didit signed).
- **The database:** `kyc_receive_webhook` does everything else, with a secret the API never has.

These checks run in order; any failure stops processing.
1. **Format and size:** `Content-Type: application/json`, body at most 512 KB, a JSON object.
2. **Signature:** `X-Signature-V2` must equal the hex HMAC-SHA256, under the webhook secret, of the **canonical JSON of the whole body**. Canonical JSON means:
   - whole-valued floats become integers, recursively;
   - keys are sorted at every depth;
   - arrays keep their order;
   - separators are compact (`,` and `:`);
   - Unicode isn't escaped.

   The comparison is between SHA-256 digests of the two hex strings, so its timing reveals nothing about the expected value. HMAC is RFC 2104, built on the built-in `sha256()` and tested against Python's `hmac`. `X-Signature` (raw bytes) and the deprecated `X-Signature-Simple` are **never** used.
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

**Early webhooks:**
- **Kept:** a signed event for a session not stored yet is kept as `unmatched`, with its status, provider time and check statuses.
- **Applied:** when the session is stored, the event is applied (`matched_later`), but only for the same user, workflow and environment. Any other user's event becomes `identity_mismatch`.
- **No race:** storing a session and receiving a webhook for the same Didit session take one advisory lock. In READ COMMITTED (enforced), whichever runs second sees what the first committed. So **no verified event stays `unmatched` once its session is committed**. Both orders are tested with a deliberately held transaction.

**Admin refresh ordering:**
- **Dated at the question:** a refresh is dated when Didit was **asked**, not when the answer is written. That time is the database clock before the call, and it must be within the last 2 minutes.
- **Stale answers lose:** a slow or delayed refresh is `stale` against any newer webhook.
- **Never approves:** an `Approved` answer is never recorded, so a stale refresh can't restore an approval. A valid later signed re-approval still applies.
- **Clock skew:** provider times come from Didit's clock and refresh times from ours; NTP keeps the difference to seconds.

**Answers:** 200 for anything stored (Didit doesn't retry), 401 or 403 for authentication failures (not retried), and 5xx only for real server faults (retried).

## Status transitions
- **Statuses:** Didit's exact values are stored as they are: `Not Started`, `In Progress`, `Awaiting User`, `In Review`, `Approved`, `Declined`, `Resubmitted`, `Abandoned`, `Expired`, `Kyc Expired`.
- **The approval rule** (`public.kyc_effective(user, environment)` returns `(approved, current session)`; `user_kyc_approved`, the API's `approved` and its displayed state all use it) is fail-closed.
  - **Current session:** the user's **newest** session (by creation order) for this database's configured environment **and workflow**, within the current **workflow epoch**. A session belongs to the epoch if it was created, or updated by Didit, at or after the moment the workflow was configured.
  - **Approved:** only while the current session is `Approved` **and** its approval is strictly later than the **veto** of every session of the user in this environment.
  - **The veto (`kyc_sessions.adverse_at`):** the latest provider time of any `Declined` or `Kyc Expired` ever recorded for that session.
    - It counts even when it arrived stale or came from an admin refresh.
    - It is never lowered: a later `Resubmitted`, `In Progress` or `In Review` doesn't erase it.
    - It lives on the session, so the 90-day receipt purge doesn't touch it.
    - On equal times, the veto wins.
- **Consequences:**
  - **Newer attempts and revocations win:** an older approval never outlives a newer attempt, decline or expiry, including a revocation later followed by a non-approving status.
  - **Restoring approval:** only a later genuine **signed** approval restores it. A refresh never approves.
  - **Same-session revocation:** still removes the approval.
  - **Environments stay separate:** a sandbox approval never counts in production.
- **States:**
  - `reverification_required`: the current session is `Approved` but vetoed, or the user has only sessions of an obsolete workflow or epoch. They can start again.
  - **Obsolete sessions don't block:** an obsolete-workflow session (`In Review` included) never blocks a new verification.
- **Workflow changes (fail-closed):**
  - **New epoch:** changing the configured workflow or environment starts a new epoch. A trigger on `kyc_provider_config` enforces this: a secret rotation keeps the epoch, and the epoch can't be set by hand.
  - **Restoring an earlier workflow does NOT restore** its earlier approvals. While another workflow was configured, that workflow's webhooks were refused, so a revocation or expiry Didit sent in that period was never recorded. Reviving the old approvals could therefore resurrect a verification Didit had already withdrawn.
  - **Re-verification:** after any switch, the owner needs a new signed approval. That can be a new session, or a Didit update of an unfinished old session dated after the switch.
- **After a finished session:** once a session ends in `Declined`, `Expired`, `Abandoned` or `Kyc Expired`, the owner can start again, and Didit issues a new session. `Resubmitted` continues the same session.

## Privacy and storage
- **Stored:** session ids, workflow, environment, Didit status and time, and the **statuses** of the identity checks: `id_verifications`, `liveness_checks`, `face_matches`, `aml_screenings`.
- **Never stored or logged:** documents, selfies, biometrics, names, document numbers, birth dates, addresses, media URLs, the decision or the webhook body.
- **Retention:**
  - webhook receipts: deleted after 90 days by the daily cleanup;
  - sessions and the KYC audit: kept while the account exists (users with KYC history can't be deleted by the API's database role);
  - Didit's own retention is configured in the Didit console.
- **Database privileges:**
  - The API's role only **reads** sessions, receipts and the audit. All writes, audit rows included, come from the functions in "Trust boundary".
  - No runtime role can read `kyc_provider_config`.

**Frontend (later):**
- **Consent:** before redirecting, tell the owner that Didit, an external provider, will process their identity document and a selfie on HyperChat's behalf. Link Didit's privacy notice and HyperChat's own, and get explicit consent.
- **Return page:** read the state from `GET /api/v1/me/kyc`, never from the return URL.

## Recovery
| Problem | What happens |
|---|---|
| Didit is down | The owner gets 503 and retries; no row is written. |
| Didit created the session but our write failed | The next start gets the same session back (Didit reuses an unfinished session for the same `vendor_data`) and stores it. |
| A webhook never arrived | Didit retries failed deliveries (5xx and 404). `POST /api/v1/admin/users/{id}/kyc/refresh` records any non-approving state. An approval needs Didit's signed webhook: a resend from the Didit console, if it offers one, or the owner verifies again. |
| Wrong environment or application deliveries | Refused with 403 and visible as `kyc_webhook_rejected` in the journal. |

## Testing with sandbox credentials (owner approval required)
DEV gets its own **sandbox** application and keys:
- Use Didit's sandbox scenarios and the console's "Try Webhook" (deliveries carry `X-Didit-Test-Webhook: true`).
- Try Webhook events don't match a stored session, so they're stored as `unmatched` and never applied.
- No live or chargeable verification is run without explicit approval.

## Enforcement (PR4)
`docs/kyc-rollout.md` covers how verification is enforced (migration 0023) and the rollout runbook.
- **Activation:** an admin activates a creator only when its current owner's KYC is approved.
- **New donations:** the donation rule requires the owner's current KYC. A Didit decline, expiry, revocation or workflow change refuses new donations at once, even while the creator's status still says `active`.
- **Legacy creators:** they keep their pre-rollout eligibility until an operator activates enforcement. After that they need KYC, or a finite grace grant bound to their owner.
