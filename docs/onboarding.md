# Creator onboarding: automatic invitations (Stage 2)

PDP v1.1 replaces "approve, then invite by hand" with one admin approval followed by an automatic invitation. Phase A
(#150) is the worker loop: `backend/app/onboarding/auto_invite.py`, run by the worker (`python -m app.worker`) next
to the job slots and the realtime outbox publisher. Phase B is migration 0038 (the invite guard exception, below): the
loop picks due applications on the worker's `hyperchat_app` connection and issues each invitation through
`onboarding_issue_invite` on a `hyperchat_lifecycle` connection, so it works with step-up enforcement `draft` or
`active` (owner decision Q14).

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `HYPERCHAT_AUTO_INVITE_ENABLED` | `false` | Run the loop in the worker. Off: nothing changes; admins invite by hand. |
| `HYPERCHAT_AUTO_INVITE_POLL_SECONDS` | `60` (10 to 3600) | Seconds between passes. |

The loop also needs `HYPERCHAT_EMAIL_BACKEND=resend` with the `onboarding` category active. Without them the worker
logs `automatic invitations not started` (`onboarding_email_inactive`) and does not run it, because the disabled
sender would report success without sending anything. It also needs `HYPERCHAT_DB_LIFECYCLE_USER` (the API's own
setting, read from the same env file); without it the reason is `lifecycle_role_unset`. Rollback: set the setting to `false` and restart the worker;
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

The pick only reads. `onboarding_issue_invite` then locks the application and re-checks it: an automatic call is
refused as `not_due` while an earlier invitation of the application is unrevoked and unaccepted, so two worker
processes never invite the same application twice (the loser moves on silently). The invitation has no actor: `user_invites.invited_by` and `application_events.actor_user_id` are NULL, and the `invite` event's
note is `auto`. After commit the email is sent through the `onboarding` category with the same template and link as
the manual route.

## Outcomes

Every outcome is an actor-less `application_events` row. Rows hold codes only, never an address, a link or provider
text.

| Event | Meaning | Next |
|---|---|---|
| `invite`, note `auto` | an automatic attempt and its invitation | done when the email is accepted |
| `note`, `auto:failed:<code>` | the email failed (`transient`, `rejected`, `refused_auth`, `category_inactive`, `recipient_not_allowed`, `send_error`); the new link was revoked in the same transaction | retried with a fresh link after 60 s, 5 min, 30 min, then 2 h |
| `note`, `auto:skipped:<code>` | `account_exists`: the applicant already has an account (owner decision Q2: link it with `POST /api/v1/admin/creators/{id}/owner`), a creator claimed meanwhile, or `attempts_exhausted` (the database's own 5-attempt cap) | never retried automatically |
| `note`, `auto:paused:<code>` | a database guard refused the invitation (`stepup_refused` or `guard_refused`; not expected since migration 0038) | never retried automatically; an alert is raised |

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
- **Q3:** the manual resend `POST /api/v1/admin/applications/{id}/invite` asks for no passkey grant, in either
  step-up state: without one it calls `onboarding_issue_invite` (manual mode, the administrator as actor) on the
  API's `hyperchat_lifecycle` connection (503 when `HYPERCHAT_DB_LIFECYCLE_USER` is unset). A presented grant is still
  consumed and claimed by 0027's anchor on the `hyperchat_app` path. Its refusal rules are unchanged
  (`creator_claimed`, `account_exists`).

## The invite guard exception (migration 0038)

0027's `stepup_guard_user_invites` refuses an application invitation without a passkey grant while enforcement is
`active`. 0038 opens exactly one way around it:

- **`onboarding_issue_invite(signup, token_hash, ttl_days, actor, automatic)`**: SECURITY DEFINER, EXECUTE for
  `hyperchat_lifecycle` only. That role has no table access of its own, so it can write `user_invites` only through a
  definer function. Lock order: application, its open invitations, its creator (as `ApplicationRepository._issue`
  and `accept_invite`). It refuses (nothing written) unless the application is `approved`, its creator is unclaimed and
  no account has its address. It takes the address and the creator from the locked application, never from the
  caller, and caps the expiry at 7 days. Automatic calls (actor NULL) are also refused while an earlier invitation is
  unrevoked and unaccepted (open, or expired after delivery), when an earlier automatic attempt has no
  `auto:failed:` note (Q4's "no automatic resend after the expiry", which therefore holds even after the 30-day
  cleanup deleted the expired row: the events are append-only), after any manual invitation (`not_due`), and after 5
  automatic ones (`attempts_exhausted`, Q4). Manual calls name an active
  administrator. A transaction that consumed a step-up grant is refused (`grant_presented`): one grant, one action.
- **The guard** needs no grant only when `session_user` is `hyperchat_lifecycle`, the row's token hash is the one the
  function announced for this transaction (`hyperchat.onboarding_invite`), the address is exactly the approved
  application's, and the transaction consumed no grant. Every other writer keeps 0027's rule.
- **What stays closed.** `hyperchat_app`, `hyperchat_auth`, `hyperchat_worker` and `hyperchat_stepup` cannot insert an
  application invitation without a grant under `active`, whatever they set: a SQL injection on the API's general
  role still gains nothing. Different addresses, unapproved applications and non-application invitations are
  refused as before.
- **What Q13 accepted.** Whoever can open `hyperchat_lifecycle` (the API or worker process, or its OS user) can
  issue a known link for an approved, unclaimed application to the applicant's own address. The attempt cap and the
  alerts are the mitigation.
- **Tests:** `backend/tests/stepup/test_onboarding_invite_db.py` (both states),
  `backend/tests/onboarding/test_auto_invite_db.py` (the loop and the manual resend in both states),
  `database/tests/test_migrations.py::test_0038_round_trip_changes_only_the_invite_guard_and_its_function_never_data`.
- **DEV deploy.** Expected alembic line: `Running upgrade 0037 -> 0038, Onboarding Stage 2 Phase B (PDP v1.1, ...`.
  Pre-check that the worker's env file sets `HYPERCHAT_DB_LIFECYCLE_USER` and that the cluster's ident map lets the
  worker's OS user open `hyperchat_lifecycle` (as for the API). Rollback: `alembic downgrade 0037` (drops the
  function, restores 0027's guard, keeps every row), with the previous release.

## Limits
- **Crash after commit.** An invitation committed but never sent, because the worker stopped between commit and send,
  stays open until it expires. It is not retried automatically, and the admin view shows it as `pending`.
- **Imports.** The worker now imports `app.admin.applications`, which pulls in `app.payments.figures` (read helpers
  for the admin donation list). The loop never calls it and never touches payment or accounting tables.

# Onboarding checklist (Stage 5, Phase A)

A read-only view of how far each creator is from activation, computed from existing data. Phase A adds no migration,
table, role, grant or job kind, and changes no lifecycle path. Activation is manual (`POST
/api/v1/admin/creators/{id}/activate`) or, since Stage 5 Phase B, automatic while the operator switch is on (migration
0042, "Automatic activation" below; the switch starts off). Code:
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
- `ready_for_activation`: true only for a qualifying `pending` creator with no blocking row **and** for which the
  database's own rule (`onboarding_activation_block`, migration 0042) answers NULL, so the checklist never promises an
  activation the worker would refuse.

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
| `pan_policy` | owner decision 2026-11-27: any approved KYC activates. State `ok` when the current approval is PAN-verified (migration 0041, docs/kyc.md "PAN evidence"), or `ok` / `hold_cleared` when an administrator cleared its non-PAN hold; `payout_hold` (not blocking) when a hold covers a non-PAN approval | `non_pan_kyc` (payouts held until the creator re-verifies with PAN); `pan_evidence_not_recorded` (**blocking**: a non-PAN approval no hold covers, recorded before 0041 without document evidence; automatic activation refuses it, an admin may activate by hand); `kyc_not_approved` (not blocking here: `kyc_approved` blocks) |
| `profile_picture` | `brand_logo_url` is exactly the primary URL of the creator's `ready` `creator_image_uploads` row (migration 0039: validity is the row, not the prefix) | `validation_not_recorded` (state `set`, blocking) for a `creator-assets/` URL without that record: a Phase A upload or a legacy copy, so the creator uploads again; `legacy_path` for an `/assets/...` path or any other value; `null` when empty |
| `background` | the same rule on `background_url` | the same codes |
| `no_restriction` | no `kyc_restrictions` row with `lifted_at IS NULL` | `open_restriction` (state `blocked`) |
| `razorpay_account` | the creator's `razorpay_account` is one of this server's configured accounts, as admin activation checks it | `account_not_configured` |
| `lifecycle_eligible` | `kyc_activation_block(streamer_id)` is NULL | its code: `restricted`, `no_owner`, `kyc_required` (state `blocked`) |

Nothing in either route writes or activates.

# Automatic activation (Stage 5, Phase B: migration 0042)

PDP v1.1: once every condition holds, the creator becomes `active` with no second admin step, and its public page is
served at once. **The operator switch starts OFF** and stays off until the owner confirms the Didit console checks
D1-D3. Manual activation is unchanged.

## The rule

`onboarding_activation_block(streamer_id)` answers NULL or the first failing code. The checklist, the worker and the
transition all use it:

| Code | Means |
|---|---|
| `not_found` / `not_pending` | no such creator, or it is not `pending` (an active, suspended or rejected creator never auto-activates) |
| `not_application_created` | Q12: no approved application created it, or it is not on the standard KYC track (admin-created and legacy creators stay manual) |
| `restricted` / `no_owner` / `kyc_required` | `kyc_activation_block` (migration 0023): an open restriction wins (Q15); an active owner; the owner's approved Didit KYC (any approval) |
| `owner_not_invitee` | the current owner is not the user who accepted the application's invitation (an owner linked by hand stays manual) |
| `pan_evidence_missing` | `onboarding_pan_covered`: the approval is PAN-verified, or the user has an open payout hold (0041 keeps one per user, whichever non-PAN approval opened it), or an administrator cleared this approval's hold. An approval recorded before 0041 has none of these (no document evidence, no automatic hold): an admin activates it by hand, or the operator runs the 0041 backfill (owner decision) |
| `profile_picture_missing` / `background_missing` | the pointer is not exactly the primary url of the creator's `ready` `creator_image_uploads` row (0039) |

The Razorpay account is the one condition outside SQL: the configured list lives in this server's settings. The worker
picks only creators whose account is configured, and the transition compares and sets it against the locked row, as
admin activation does.

## The switch (owner decision Q11)

`onboarding_settings` is one row, `auto_activation` false. Only the operator changes it, with an approval reference:

```sql
SELECT public.onboarding_set_auto_activation(true, '<operator>', '<owner approval reference>');
SELECT public.onboarding_set_auto_activation(false, '<operator>', '<reference>');  -- rollback: back to manual only
```

A trigger writes one append-only `onboarding_settings_events` row for every change, whoever issues it. No runtime role
writes either table. `GET /api/v1/admin/onboarding/auto-activation` (admins, read-only) shows the switch and the
automatic activations. Changing the switch from the admin API is not offered: it replaces the passkey for every
activation, so it would need its own step-up action (an owner decision; "admin API later").

## The transition

`onboarding_auto_activate(streamer_id, expected_account)` is SECURITY DEFINER, EXECUTE for `hyperchat_lifecycle` only.
- **Refuses first:** `switched_off`; `grant_presented` when the transaction consumed a step-up grant (one grant, one
  action: the granted path stays `kyc_change_creator_status`).
- **Under the lock:** it locks only the creator (`FOR NO KEY UPDATE`, as a restriction and the donations trigger do),
  then compare-and-sets `pending` (`status_changed`) and the account (`account_changed`), then re-evaluates the whole
  rule, so a restriction added meanwhile wins.
- **Writes, in one transaction:** the `creator_status_events` row (`activate`, no actor, reason `auto`), the
  `onboarding_activations` row (owner, application, invitation, KYC session, PAN status, the covering hold, both image
  records, the account) and the status. One row per creator ever (a creator leaves `pending` once).

**Step-up, explicitly.** 0027's `stepup_guard_streamers` anchors pending to active on `creator.activate`. 0042 re-creates
it from 0027's text with one change: no grant is needed when, and only when, `session_user` is `hyperchat_lifecycle`,
an `onboarding_activations` row for this creator was written in this transaction, and the transaction consumed no
grant. The marker is a row only `onboarding_auto_activate` can write, never a setting: `hyperchat_lifecycle` can also
run `kyc_change_creator_status` and set any custom setting, and neither opens the exception. Manual activation and
reactivation keep their grants under `active`. It works with step-up `draft` or `active`.

## The worker loop

`app.onboarding.auto_activate`, started by the worker whenever `hyperchat_lifecycle` is configured. Every
`HYPERCHAT_AUTO_ACTIVATION_POLL_SECONDS` (60) it reads the switch; while it is on it picks up to 10 creators the rule
accepts (read-only, `hyperchat_app`), and activates each on `hyperchat_lifecycle`. No job kind, no attempt rows: a
refusal changes nothing, and the creator is picked again only while the rule still says yes. A database guard refusal
(never expected) pauses that creator until the worker restarts and is logged. Each activation logs the
`creator_status_changed` security event (ids and codes only). No activation email yet.

## Rollback

`SELECT public.onboarding_set_auto_activation(false, ...)`: manual activation remains. The downgrade refuses while the
switch is on or any activation or switch change exists (its audit is never dropped).
