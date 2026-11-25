# Creator onboarding: automatic invitations (Stage 2, Phase A)

PDP v1.1 replaces "approve, then invite by hand" with one admin approval followed by an automatic invitation. Phase A
is code only: no migration, no job kind, no new table, role or grant (the PAY-15 soak pins the database head until
2026-12-01). Code: `backend/app/onboarding/auto_invite.py`, run by the worker (`python -m app.worker`) next to the job
slots and the realtime outbox publisher, on the worker's `hyperchat_app` connection.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `HYPERCHAT_AUTO_INVITE_ENABLED` | `false` | Run the loop in the worker. Off: nothing changes; admins invite by hand. |
| `HYPERCHAT_AUTO_INVITE_POLL_SECONDS` | `60` (10 to 3600) | Seconds between passes. |

The loop also needs `HYPERCHAT_EMAIL_BACKEND=resend` with the `onboarding` category active. Without them the worker
logs `automatic invitations not started` (`onboarding_email_inactive`) and does not run it, because the disabled
sender would report success without sending anything. Rollback: set the setting to `false` and restart the worker;
the manual route keeps working throughout.

## What a pass does

Each pass handles at most 10 applications, each in its own transaction. An application is due when all of these hold:

- it is `approved` and its creator is unclaimed (`streamers.user_id IS NULL`);
- it has no open, unexpired invitation;
- it was approved within the last 7 days, and no earlier than 7 days before this worker started. The start time is
  kept in memory only. Turning the setting on therefore never mass-invites old approvals, and a restart does not
  drop retries already in progress;
- no admin has invited it by hand (a manual invitation takes over for good), and no automatic attempt was skipped or
  paused;
- every earlier automatic attempt failed, there were fewer than 5, and the backoff since the last one has passed.

The application row is locked `FOR UPDATE SKIP LOCKED` inside the same transaction that issues the invitation
(`ApplicationRepository.issue_auto_invite`, which reuses the manual path's `issue_invite` body). The eligibility query
runs a second time under the lock, so two worker processes never invite the same application twice. The invitation
has no actor: `user_invites.invited_by` and `application_events.actor_user_id` are NULL, and the `invite` event's
note is `auto`. After commit the email is sent through the `onboarding` category with the same template and link as
the manual route.

## Outcomes

Every outcome is an actor-less `application_events` row. Rows hold codes only, never an address, a link or provider
text.

| Event | Meaning | Next |
|---|---|---|
| `invite`, note `auto` | an automatic attempt and its invitation | done when the email is accepted |
| `note`, `auto:failed:<code>` | the email failed (`transient`, `rejected`, `refused_auth`, `category_inactive`, `recipient_not_allowed`, `send_error`); the new link was revoked in the same transaction | retried with a fresh link after 60 s, 5 min, 30 min, then 2 h |
| `note`, `auto:skipped:<code>` | `account_exists`: the applicant already has an account (owner decision Q2: link it with `POST /api/v1/admin/creators/{id}/owner`), or a creator claimed meanwhile | never retried automatically |
| `note`, `auto:paused:<code>` | the database refused the invitation: `stepup_refused` (see below) or `guard_refused` | never retried automatically; an alert is raised |

The fifth failed attempt raises an `invite_failed` alert, and no sixth attempt follows (owner decision Q4). The owner's
schedule also lists a 12 h step, which only a sixth attempt would wait for. An invitation that expires after its 7
days is never re-sent automatically (Q4). In every stopped case the admin's manual resend remains available.

Alerts are recorded in `system_alerts` through `app.alerts` (`docs/alerting.md`): kind `invite_failed`, source
`auto_invite`, the reason code, and `signup_id` (plus `invite_id` after a failed email). They are delivered to the
operator channel like every other alert. Recording is best effort: an alert that cannot be written is logged and never
stops the loop.

## Admin view

`GET /api/v1/admin/applications/{id}` adds `invite_delivery`:

- `status` is the application's latest invitation: `pending`, `accepted`, `revoked` or `expired`. It is `null` when
  there is none, or when the daily cleanup deleted it.
- `auto_attempts` is the number of automatic attempts.
- `last_failure` is the code of the latest skipped, failed or paused outcome, else `null`.

## Related owner decisions (2026-11-25)

- **Q1:** the approve form's `razorpay_account` defaults to `primary`. An explicit value must still be a configured
  account. This reverses D12 for application approval only; `POST /api/v1/admin/creators` still requires the account
  by name. The approve form accepts only `primary`, the one logical account since D6 retired `secondary` (migration
  0018). A second account would need that field widened.
- **Q3:** the manual resend `POST /api/v1/admin/applications/{id}/invite` no longer asks for a passkey grant. A
  presented grant is still consumed. Its refusal rules are unchanged (`creator_claimed`, `account_exists`).

## Limits (Phase A)

- **Step-up.** The loop works only while step-up enforcement is `draft` (owner decision Q13). With `active`, migration
  0027's guard refuses an application invitation from a transaction that presented no grant. The loop records
  `auto:paused:stepup_refused`, raises an alert and leaves that application alone; it never crashes the worker (Q14).
  The manual resend then needs a grant as well: the database answers the route's attempt with 403
  `step_up_required`. Migration 0037 (Phase B, after the soak) adds the guard exception that lifts both limits. In
  `draft` the loop adds no new power: any `hyperchat_app` caller can already insert such an invitation.
- **Crash after commit.** An invitation committed but never sent, because the worker stopped between commit and send,
  stays open until it expires. It is not retried automatically, and the admin view shows it as `pending`.
- **Imports.** The worker now imports `app.admin.applications`, which pulls in `app.payments.figures` (read helpers
  for the admin donation list). The loop never calls it and never touches payment or accounting tables.

# Onboarding checklist (Stage 5, Phase A)

A read-only view of how far each creator is from activation, computed from existing data. Phase A adds no migration,
table, role, grant or job kind, and changes no lifecycle path. **Activation stays manual** (`POST
/api/v1/admin/creators/{id}/activate`) until Stage 5 Phase B (migration 0040, after the PAY-15 soak). Code:
`backend/app/onboarding/checklist.py`, shared by both routes. Each request uses one connection and one `READ ONLY`
transaction on the API's own role (`hyperchat_app`); it calls `kyc_activation_block` once per creator, which that role
may already execute (migration 0023).

## Routes

| Route | Who | Answer |
|---|---|---|
| `GET /api/v1/me/onboarding` | the signed-in owner or linked account of at least one creator (403 otherwise, 401 signed out) | `{creators: [...]}`, one entry per creator the user may manage |
| `GET /api/v1/admin/onboarding?status=pending\|active&blocked_by=<condition>&limit=50&offset=0` | admins | `{total, limit, offset, creators: [...]}`, newest first; `limit` at most 200 |

Both answer `Cache-Control: no-store`. Each creator entry holds:

- `streamer_id`, `slug`, `status` (`pending`, `active`, `suspended`, `rejected`) and `track` (`standard`, `legacy`);
- `qualifies_for_auto_activation`: the creator came from an application and is on the standard KYC track (owner
  decision Q12);
- `conditions`: one row per condition below, `{condition, state, detail, blocking}`;
- `ready_for_activation`: true only for a qualifying `pending` creator with no blocking row. The PAN row blocks
  while its policy is pending, so this is never true before Stage 3.

The admin view adds `signup_id` and `invite_delivery` (`{status, auto_attempts, last_failure}`, the same derivation as
`GET /api/v1/admin/applications/{id}`). `blocked_by` keeps only creators that condition blocks; an unknown code is
400. It answers ids and slugs only, never the applicant's name, email or phone, nor the creator's display name.

## Conditions

Every row is evaluated against the creator's **current owner** (`streamers.user_id`), never the viewer. The owner, a
linked account (full owner power) and an admin therefore see the same rows. States are `ok` or `set` (met), and
`missing`, `blocked` or `policy_pending` (blocking). `detail` is a code or `null`, never a value.

| `condition` | Met when | `detail` when not met |
|---|---|---|
| `application_approved` | the creator's application (`user_signups.streamer_id`) is `approved` | the review status, or `no_application` |
| `invitation_accepted` | an invitation of that application was accepted (accepted invitations are never purged) | the latest invitation's state, or `no_invitation` |
| `ownership_confirmed` | the user who accepted it is the current owner, and that account is active | `no_owner`, `owner_not_invitee` (an owner linked by an admin), `owner_inactive` |
| `kyc_approved` | the owner's Didit KYC is approved for this server's Didit environment (`user_kyc_approved`) | `no_owner`, `owner_kyc_required` |
| `pan_policy` | never in Phase A: state `policy_pending` until Stage 3 (migration 0038) records PAN evidence. PAN-only applies to automatic activation only (owner decision Q6) | `pan_evidence_not_recorded` |
| `profile_picture` | `brand_logo_url` is an https URL under the `creator-assets/` prefix: state `set`, detail `validation_not_recorded` (informational until Phase B records validation) | `legacy_path` for an `/assets/...` path or any other value; `null` when empty |
| `background` | the same rule on `background_url` | the same codes |
| `no_restriction` | no `kyc_restrictions` row with `lifted_at IS NULL` | `open_restriction` (state `blocked`) |
| `razorpay_account` | the creator's `razorpay_account` is one of this server's configured accounts, as admin activation checks it | `account_not_configured` |
| `lifecycle_eligible` | `kyc_activation_block(streamer_id)` is NULL | its code: `restricted`, `no_owner`, `kyc_required` (state `blocked`) |

Nothing in either route writes or activates. A creator `pending` with every row met except PAN still waits for an
admin's activation.
