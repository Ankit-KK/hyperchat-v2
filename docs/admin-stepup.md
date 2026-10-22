# Administrator step-up authentication (passkeys): S1, S2 and S3

S1 is the foundation of administrator MFA (approved design: WebAuthn only, a separate verifier, operator-approved
passkeys, single-use action-bound grants). S2 connects the API to it (endpoints, action-bound grants on the sensitive
admin routes, no Google auto-linking for administrators). S3 (migration 0027) makes PostgreSQL itself require the
grant on every sensitive administrator write, whatever code issues it.

**Status:**
- **Code ready** (S3 merged): the guards exist and are tested; nothing changes for a caller without a grant while
  the state is `draft`.
- **Not deployed:** no environment has 0025, 0026 or 0027.
- **Not activated:** the enforcement state is `draft` everywhere. Activation is a separate, explicitly approved
  operator step (see "S3: activation prerequisites"); this document gives no command for it.

## The trust boundary

```
browser (frontend origin) ──▶ API  (OS user hyperchat; roles app / auth / lifecycle / worker)
   passkey signs SHA-256(canonical action)      │ Unix socket /run/hyperchat-stepup.sock
                                                │ (systemd: hyperchat-stepup:hyperchat 0660)
                                                ▼
                              step-up verifier  (OS user hyperchat-stepup; role hyperchat_stepup; no network)
                                                │ writes, only after verifying a signature itself:
                                                ▼ challenges · pending passkeys · grants · session assurance · events
                              PostgreSQL guards (migration 0025) repeat the checks for every role
```

- **A separate identity at every layer.**
  - The verifier runs as `hyperchat-stepup` from its own root-owned release tree (`/opt/hyperchat-stepup`).
  - Its environment file holds **no secrets**. It refuses to start if any other `HYPERCHAT_*` variable is present, so a copied `api.env` can't run it.
  - It has no network: `PrivateNetwork=yes`, AF_UNIX only.
  - Its database role is reachable only from its own OS user through `pg_ident`. No API user maps to it, not even root.
- **The socket.** systemd creates it as `hyperchat-stepup:hyperchat 0660`: the API's user can call it, and nobody else except root.
- **What the API can do.** It forwards ids and the browser's WebAuthn response. It **cannot** produce a grant, session assurance or an active passkey. Those exist only when the verifier verified a signature from an active passkey of that administrator, over the canonical action stored for that challenge.
- **What the database enforces itself (0025)**, whatever the caller:
  - a passkey is inserted `pending`; only the operator (`hyperchat_owner`) approves or revokes it
  - a passkey's identity never changes, and its sign count never decreases
  - a challenge is SHA-256 over its canonical action, and the database recomputes that hash
  - a challenge names its own administrator, session, action, target, parameter hash and expiry; it lasts at most 5 minutes and is used once
  - a grant needs an action challenge verified **in the same transaction**, with identical bindings and an active passkey; it lasts at most 2 minutes, is consumed once through `stepup_consume()`, and is claimed once, by one anchor write, in that same transaction (see "One grant, one action")
  - session assurance needs a session ceremony in the same transaction, for that administrator's own live session; it lasts at most 12 hours
  - the audit history is append-only for everyone, the owner included
  - enforcement can become `active` only through the operator, and only while every active administrator has at least two active passkeys
  - 0007's default privileges (runtime DML on every new table) are revoked on every step-up table

### What each compromised identity can and cannot do (tested in `tests/stepup/test_stepup_db.py`)

| Identity | Cannot | Still can |
|---|---|---|
| `hyperchat_app`, `hyperchat_auth`, `hyperchat_lifecycle`, `hyperchat_worker` (a SQL injection, a leaked connection, **or the whole API process**) | create, approve or alter passkeys (not even read their keys); forge a grant or session assurance; change the enforcement state; alter, delete or forge audit events; clear the verifier's rate limits; `SET ROLE` to the verifier or owner; call operator functions | read the enforcement state; `stepup_consume()` a grant it was **given** (only with exact bindings, once); ask the verifier for a ceremony (the administrator's authenticator must still sign it); deny service |
| `hyperchat_stepup` (the verifier process itself) | approve or revoke a passkey; insert one already active; use a pending or revoked passkey; change enforcement; alter or delete audit events; issue a grant without a challenge verified in the same transaction | issue grants and assurance for signatures it checks. **The verifier is trusted to verify signatures correctly.** That's why it is a separate, small, network-less service. |
| The operator (`hyperchat_owner` via the migrator, root on the server) | rewrite the audit history; activate enforcement while an administrator has fewer than two active passkeys | approve, revoke, recover |
| root on the server | — | everything (out of scope; host hardening) |

**Honest limits of S1.**
- The frontend origin is part of the trusted base. A script injected there could show a misleading action, so S2 and the frontend phase need a strict CSP and a separate admin origin.
- A compromised API process can still read whatever its roles read, act as any non-admin user, and use the third-party keys in `api.env`.
- Nothing is enforced until the operator activates it (S3 provides the database guards).

## One grant, one action (the interface S3 enforces)

A grant is consumed by the API and **claimed** by the database write that *is* the action. Both happen in the same
transaction, and both bind the administrator, the session, the action, the target and the exact parameter hash.

| Step | Who | Function | Rule |
|---|---|---|---|
| Consume | The API's transaction | `stepup_consume(grant, admin, session, action, target, params_hash)` | Exact bindings; once; unexpired; the administrator, passkey and session are still valid. |
| Claim | S3's guard on the action's **anchor** write (owner-only, SECURITY DEFINER) | `stepup_claim(admin, session, action, target, params_hash, anchor)` | Takes **one** grant consumed **in this transaction** with exactly these bindings that nothing has claimed yet. Otherwise it refuses. |
| Effect | S3's guards on the action's secondary writes | `stepup_effect(admin, session, action, target, params_hash, effect)` | Needs a grant **claimed** in this transaction with the same bindings. Records each named effect **once per grant** (`stepup_grant_effects`). |

**The policy.**
- **One approved logical action** has exactly one anchor write (e.g. the creator's status change for `creator.activate`). Its legitimate multi-table consequences are **effects** of that one claimed grant: the status event row, a revoked grace grant, an audit row. Each named effect happens once per grant, and only for the same administrator, session, action, target and parameters.
- **A second, independent sensitive action needs its own grant**:
  - the same action again (a second anchor write in the same transaction)
  - another action or another target
  - other parameters
  - a write attributed to another session

  In every case `stepup_claim` finds no unclaimed matching grant and refuses. Two approvals of the same action authorize exactly two anchor writes.
- **Anything that is itself sensitive is an anchor, never an effect.** S3 must classify every guarded write into exactly one of the two, and the S3 review checks that table.
- **Session attribution:** a grant from session A can never be claimed for a write attributed to session B. S3 must take the acting session from the authenticated request path, the same session `stepup_consume` was called for, never from a free parameter.
- **No banking grants:** a consumed grant can be claimed only in the transaction that consumed it, so nothing can be consumed now and used later. A rolled-back transaction rolls back its consumption, so the grant stays single-use.

The tests are in `tests/stepup/test_stepup_db.py` ("S3's interface").

## S2: API integration

### Endpoints (`app/routers/mfa.py`, under `/api/v1/auth/mfa`)
| Endpoint | What it does |
|---|---|
| `GET /status` | Enforcement state, this session's `aal`, `mfa_required`, verifier state, this admin's passkeys. |
| `GET /credentials` | This admin's passkeys: fingerprint, status, dates. Never key material; listed by the verifier, since the API's roles cannot read the table. |
| `POST /registration/options`, `POST /registration/verify` | A new passkey, stored **pending** until the operator approves it. |
| `POST /assertion/options` `{action, target, params}`, `POST /assertion/verify` `{challenge_id, credential}` | `session.aal2` gives this session aal2. Any other action gives a **one-time grant** for exactly that action, target and params (2 minutes), sent back as `X-Step-Up-Grant`. |

- **Who may call:** every endpoint needs an admin session cookie, and every POST also needs an allowlisted Origin and the session's CSRF token.
- **Identity:** the administrator and the session come **only** from the server-side session. Request bodies refuse extra fields, so a `user_id` or `session_id` in a body is a 422.
- **Rate limits:** per admin and per IP in the API (`auth:mfa*`), plus the verifier's own per-admin limits.
- **Errors** are `{"error", "reason", ...}` with reasons such as `mfa_required`, `step_up_required` (with the exact `step_up: {action, target, params}` to approve), `step_up_refused` (with `detail`), `stepup_unavailable` and `stepup_not_configured`.
- **Fail closed:** with no verifier (setting unset, socket down), no registration, assurance or grant is possible: those endpoints answer 503. Admin access is unchanged while enforcement is `draft`.

### Routes bound to actions (`app/stepup/actions.py`, `ROUTES`)
S3 changed the parameters: they are now **exactly what the database reads back from the row it writes**, normalized
as stored (UUIDs lowercase and hyphenated, addresses trimmed and lowercased, the creator fields as
`AdminService.creator_fields` stores them). The frontend never has to guess: the route's `403 step_up_required`
answer carries the exact `{action, target, params}` to approve.

| Route | Action | Target | Params (as the database derives them) |
|---|---|---|---|
| `POST /api/v1/admin/creators/{id}/activate` | `creator.activate` | `creator:{id}` | `{expected_status}` |
| `POST /api/v1/admin/creators/{id}/reactivate` | `creator.reactivate` | `creator:{id}` | `{expected_status}` |
| `POST /api/v1/admin/creators` | `creator.create` | `creator:new` | `{slug, name, razorpay_account}` |
| `POST /api/v1/admin/creators/{id}/owner` | `creator.owner.assign` | `creator:{id}` | `{user_id}` |
| `POST /api/v1/admin/creators/{id}/owner/transfer` | `creator.owner.transfer` | `creator:{id}` | `{user_id, expected_owner_id}` |
| `POST /api/v1/admin/applications/{id}/approve` | `application.approve` | `application:{id}` | `{slug, name, razorpay_account}` |
| `POST /api/v1/admin/applications/{id}/invite` | `application.invite` | `application:{id}` | `{}` (the application's own address and creator: the database checks both) |
| `POST /api/v1/auth/invites` **with `role: admin` or a `streamer_id`** | `admin.invite` | `invite` | `{email, role, streamer_id}` |
| `PATCH /api/v1/admin/users/{id}` | `admin.user.change` | `user:{id}` | the account's **resulting** `{role, is_active, streamer_id}` |
| `PUT /api/v1/admin/users/{id}/email` | `admin.user.email` | `user:{id}` | `{email}` |
| `POST /api/v1/admin/partner-keys` | `partner_key.create` | `partner_key` | `{partner_slug}` |

- **Unbound actions:** `mfa.credential.revoke`. Passkeys are revoked by the operator only.
- **Not step-up actions (aal2 only once enforcement is active):**
  - defensive actions: suspend, restrict, revoke grace, revoke sessions, revoke a partner key
  - reads
  - soundboard eligibility
  - ordinary invitations (no admin role, no creator link)
- **Not bound (cosmetic, set at creation):** a new creator's `brand_color` and `order_prefix`. A grant never
  authorizes "this action in general": its parameter hash binds the values above.

### Transaction boundaries
- **The consumption is inside the operation's transaction.** Each bound route's repository method calls `public.stepup_consume(grant, admin, session, action, target, params_hash)` **first inside the same transaction as the operation**, on the role that performs it:
  - `hyperchat_lifecycle` for activate and reactivate (with the `kyc_change_creator_status` call)
  - `hyperchat_auth` for user, email and admin-invite changes
  - `hyperchat_app` for creators, owners, applications and partner keys

  All three got EXECUTE in 0025, so S2 needs **no new database privileges**.
- **A refused grant** (`mismatch`, `consumed`, `expired`, `credential_revoked`, `not_admin`, `session_invalid`, `not_found`) raises and rolls the operation back.
- **A refused operation** (a stale compare-and-set, a claimed creator, an unknown user, nothing changed, an exception) rolls the **consumption** back too (`psycopg.Rollback`). A grant is spent only by the operation it approved, and stays usable for a retry within its 2 minutes.
- **Concurrency:** two requests with one grant serialize on the grant's row, and exactly one succeeds.

### Compatibility while `draft`
- **`draft`:** a bound route without `X-Step-Up-Grant` still works, so existing admins are not locked out before enrolling. With a grant it is consumed and must be valid (fail closed): since S3 the database also refuses the write when the presented grant does not match it.
- **`active`** (only by the operator, after its separate approval): `require_admin` needs aal2 (passkey endpoints excepted), a bound route without a grant answers `step_up_required`, and **the database refuses the write itself** without a matching grant (S3), whatever the caller.

### Google sign-in for administrators
- **Never by email:** a Google account whose (even verified) email matches an administrator **no longer links or signs in**, at three layers:
  - `AuthService.google_login` refuses: 403 "Administrators sign in with their password and passkey".
  - `AuthRepository.google_sign_in` re-checks under the user-row lock.
  - **Migration 0026** refuses every API role, `hyperchat_auth` included, a new identity for an administrator, or moving an identity onto one.
- **Unchanged:** an identity linked to an administrator before (or by the operator) keeps working, and only that exact Google subject does. Non-admin email linking is unchanged.
- **Self-service Google linking for administrators is permanently off.** That covers every lifecycle path:
  - **Email change (D11):** the admin's identities are removed and **not** re-linked by the new email.
  - **Admin invitation:** the new account starts without a link.
  - **Passkey recovery** (`stepup_recover_admin`): ends sessions and passkeys but **keeps** an existing link. Google alone never restores aal2.
  - **Deactivation and reactivation:** keep the link (no sign-in while deactivated).
  - **Promotion:** stays impossible (0024).
  - **Demotion:** turns the account into an ordinary user, with ordinary linking.
- **Operator-verified linking (migration 0026): the only way an administrator gets a Google link.**
  - **Why nothing from the API counts.** A first version recorded "link requests" through `hyperchat_auth` and showed the person a reference code. Review showed a compromised API or `hyperchat_auth` can fabricate such a request, for any administrator and an attacker's Google subject. A code shown by the API proves nothing about which Google account the person controls. That table and the codes were removed: **the API records and shows nothing about linking**, and no API role can link an administrator's identity (0026 guard) or call the link function (owner only).
  - **What the operator verifies, both required:**

    | Fact | How |
    |---|---|
    | **Administrator's identity** | Out of band, by the operator: in person, or a call to a number the operator already had. Never a channel the API supplied. The administrator also states which Google address they will use. `--verified-by` records how this was done. |
    | **Control of that exact Google account** | `python -m app.stepup.operator link-google --email ADMIN --expect-google-email GOOGLE --verified-by TEXT --operator NAME --confirm-database DB` starts Google's **device sign-in** for the **operator's own OAuth client** (`HYPERCHAT_OPERATOR_GOOGLE_CLIENT_ID` / `_SECRET`, a "TVs and limited input devices" client, kept outside `api.env`; the tool refuses the API's client id). It prints a Google URL and a code. **The administrator completes the sign-in on their own device while the operator is in contact.** The ID token comes **directly from Google to the tool over TLS**, never through the API. |

  - **What the tool checks on the ID token:**
    - its signature against Google's published keys
    - the issuer
    - the audience (the operator's client, so a token minted for the API's client is useless)
    - issued within 10 minutes
    - a verified email equal to `--expect-google-email`
  - **What gets linked:** the token's **subject**, which nobody types or relays.
  - **`admin_link_google_identity()` (owner only)** links only to an **active administrator** and requires the verification record. It never moves a Google account already linked to any account (`subject_in_use`). It is audited in `stepup_events` (`identity_linked`, with operator, `verified_by`, Google email and method).
  - **What the database cannot do:** it cannot check a Google signature. That is why the function trusts only the owner, i.e. the operator's tool on the server, with the same trust as passkey approval.
  - **Refused, linking nothing (tests):**
    - a wrong Google account (email), or a token for the API's client
    - a wrong issuer, an unverified email, a stale token
    - another signing key
    - the person declining, the code expiring
    - an empty `--verified-by`, the wrong database
    - a non-administrator target (refused before anyone is asked to sign in)
    - a Google account in use
  - **Deployment ([APPROVAL]):** create the operator's device-flow OAuth client in Google Cloud. Its id and secret live only in the operator's root-only environment on the server. The server must be able to reach `oauth2.googleapis.com` and `www.googleapis.com` while the operator runs the command.

### Deployment additions (each [APPROVAL])
- **`HYPERCHAT_STEPUP_SOCKET`** in `api.env` (commented in the templates; set it only once the verifier is installed).
- **Operator Google client** for `link-google` (only if administrators need Google sign-in): `HYPERCHAT_OPERATOR_GOOGLE_CLIENT_ID` / `_SECRET` in the operator's root-only environment, never in `api.env`.
- **Migration 0026** after 0025.
- **No new roles or grants.** The API's existing unit can connect to the socket: AF_UNIX is allowed, and a read-only `/run` does not block connecting to a socket.
- **PostgreSQL 15:** 0026 uses only a row trigger, `FOR SHARE` and `pg_catalog` functions (PostgreSQL 11+). The same postgres:15 CI job proposed for 0025 would cover it.

## S3: database enforcement (migration 0027)

### How a guard decides
- **Who is guarded.** A guard fires when the **login** role (`session_user`) is one the API connects as:
  `hyperchat_app`, `hyperchat_worker`, `hyperchat_auth`, `hyperchat_lifecycle` or `hyperchat_stepup`. Keying on the
  login role, not `current_user`, means a SECURITY DEFINER function those roles call (`kyc_change_creator_status`)
  is guarded too, and so are writes from foreign-key cascades and other triggers in their transactions.
- **Anchor** (`stepup_authorize(action, target, params, anchor)`): the target and the parameters are derived **from
  the row being written**, the hash is computed in SQL (`stepup_params_hash`: byte for byte the API's canonical JSON,
  tested against the shared vectors), and exactly one grant **consumed in this transaction** for exactly that action,
  target and hash is claimed (`stepup_claim`). The administrator and session are the grant's: `stepup_consume`
  accepted it only for the API's authenticated administrator and session, so nothing the caller passes to the guard
  names them.
  - none, enforcement `active`: refused;
  - none, `draft`, but this transaction consumed **any** grant, claimed by its own anchor or not: refused. A
    presented grant authorizes its own write and never lets a second sensitive write through the legacy path
    (review of PR #76; `test_a_claimed_grant_never_carries_a_second_sensitive_write`, draft and active);
  - none, `draft`, no grant presented at all: allowed (the legacy path). A consumption rolled back with its
    savepoint left no trace, so it counts as not presented.
- **Effect** (`stepup_effect_or_legacy(action, target, params, effect)`): needs the grant that action claimed in
  this transaction **for exactly these parameters**; each named effect once per grant (`stepup_effect`). Its legacy
  path has the same rule: only in `draft`, only when no grant was presented.
- **Fail closed.** No branch allows a write because data is missing: an unreadable enforcement state, a missing
  creator row or a malformed parameter all end in the refusing branches. A failed, refused or no-op operation rolls
  its transaction (or savepoint) back, and the consumption and claim with it: the grant is not spent.

### Inventory and guard matrix
Every write path to the tables below was traced (repositories, services, definer functions, triggers, cascades).

| Sensitive write | Path (role) | Anchor / effect | Action, target, params derived from the row |
|---|---|---|---|
| `streamers.status` pending → active | `kyc_change_creator_status` (definer, `hyperchat_lifecycle`) | anchor `streamers.status` | `creator.activate`, `creator:<id>`, `{expected_status: OLD.status}` |
| `streamers.status` suspended → active | same | anchor `streamers.status` | `creator.reactivate`, same |
| `streamers.user_id` NULL → u | `AdminRepository.assign_owner` (`app`) | anchor `streamers.user_id` | `creator.owner.assign`, `creator:<id>`, `{user_id}` |
| `streamers.user_id` u → v | `AdminRepository.transfer_owner` (`app`) | anchor `streamers.user_id` | `creator.owner.transfer`, `creator:<id>`, `{user_id, expected_owner_id: OLD.user_id}` |
| old owner's `users.streamer_id` → NULL | same transaction | effect `old_owner_unlinked` | only the account the claimed transfer names as previous owner |
| `INSERT streamers` | `AdminRepository.create_creator` (`app`) | anchor `streamers.insert` (checked at commit) | `creator.create`, `creator:new`, `{slug, name, razorpay_account}` |
| `INSERT streamers` for an approval | `ApplicationRepository.approve` (`app`) | effect `creator_created` (checked at commit) | of the approval that references it |
| `user_signups.review_status` → approved | same | anchor `user_signups.review_status` | `application.approve`, `application:<id>`, `{slug, name, razorpay_account}` of the referenced creator, which must have been created in this transaction and be unclaimed |
| `INSERT user_invites` with `signup_id` | `ApplicationRepository.issue_invite` (`app`) | anchor `user_invites.insert` | `application.invite`, `application:<signup>`, `{}`; the row must carry the approved application's own address and creator, role `streamer` |
| `INSERT user_invites` role admin or with `streamer_id` | `AuthRepository.create_invite` (`auth`) | anchor `user_invites.insert` | `admin.invite`, `invite`, `{email (lowercase), role, streamer_id}` |
| `users.role` / `is_active` / `streamer_id` | `AdminRepository.update_user` (`auth`); `transfer_owner` (`app`) | anchor `users.privileges` | `admin.user.change`, `user:<id>`, the resulting `{role, is_active, streamer_id}` |
| `users.email` | `AdminRepository.change_email` (`auth`) | anchor `users.email` | `admin.user.email`, `user:<id>`, `{email (lowercase)}` |
| `INSERT partner_api_keys` | `PartnerRepository.create_key` (`app`) | anchor `partner_api_keys` | `partner_key.create`, `partner_key`, `{partner_slug}` |

**Closed outright for every API role** (no grant makes them possible; `hyperchat_auth` included, which 0024 did not
cover):
- removing a creator's owner (`streamers.user_id` → NULL, also through the `ON DELETE SET NULL` cascade of deleting
  the owner's account);
- changing `streamers.created_at` (an approval relies on it);
- changing an approved application's status, address or creator, or inserting an application that is already
  approved or linked to a creator or invitation;
- changing an invitation in any way except accepting it (now, once, only while unrevoked and unexpired, with the
  accepting account, by `hyperchat_auth`, **with the invitation's own token**) or revoking it (now, once); inserting
  an invitation that is already accepted or revoked;
- changing a partner key except recording its use or revoking it (no new hash, partner, or un-revoking);
- inserting an account with role admin or a creator link, except through the two internal paths below.

**Internal paths (no administrator action; authorized by what they carry):**
- **Invitation acceptance** (`hyperchat_auth`, `AuthRepository.accept_invite`): the account an invitation accepted
  **in this transaction** creates or upgrades (same address, role and creator; never to admin by update), and the
  unclaimed creator an application invitation accepted in this transaction claims for that account. The
  invitation's issue was the guarded anchor; its token is the user's authorization, and the database checks it:
  `accept_invite` presents the raw token for its transaction only (`set_config('hyperchat.invite_token', …, true)`)
  and the guard accepts the invitation only when its SHA-256 equals `token_hash`. A database role alone (an
  injection, a leaked connection) never sees a token, so it cannot fabricate the acceptance these exceptions trust.
  The token is a bound parameter; keep PostgreSQL statement logging off (the default) so it never reaches server
  logs.
- **Bootstrap** (`hyperchat_auth`, `AdminRepository.bootstrap_admin`): an admin row only while no active
  administrator can sign in (as 0024).

**Not guarded, by design** (no new authority): suspension, KYC restrictions, grace revocation, session revocation,
partner-key revocation, rejecting or reopening an application, notes, `user_signups.invite_id` bookkeeping,
ordinary invitations, the creator dashboard's own settings (`WRITABLE_SETTINGS` excludes owner, status and
account columns), soundboard eligibility, the Razorpay account column (only `primary` is allowed by 0018 and the
lifecycle function compare-and-sets it at activation). Payouts and wallet are out of scope.

### Privileged exceptions (the trusted boundary)
- **The operator** (logged in as `hyperchat_migrator`, acting as `hyperchat_owner`): migrations, the legacy import,
  `app.stepup.operator`, `app.kyc.operator` and test fixtures. Not guarded: whoever holds it can already change the
  guards themselves. It must stay out of reach of every API process (S0's `pg_ident` and file-ownership checks).
- **Root on the server**: everything (host hardening).
- No other exception exists: there is no permissive fallback when the verifier, a grant or the enforcement state is
  unavailable.

### Role and privilege changes
- None granted. Every new function (`stepup_canonical_json`, `stepup_params_hash`, `stepup_authorize`,
  `stepup_effect_or_legacy`, the six guard functions) is owned by `hyperchat_owner`, SECURITY DEFINER where it
  must call the owner-only `stepup_claim`/`stepup_effect`, pins `search_path = pg_catalog, public, pg_temp`, and
  has EXECUTE revoked from PUBLIC and every runtime role (tested). 0027 neither grants nor restores any table or
  function privilege revoked by 0024–0026 (the migration test compares every runtime privilege before and after).
- Guard triggers are named `aac_…` so they run after 0024's `aaa_…` and 0026's `aab_…` guards.

### What a compromised API role can no longer do (tests: `tests/stepup/test_stepup_enforcement_db.py`)
With enforcement `active`, connecting as the API's own roles and issuing the SQL directly (no grant): every row of
the matrix above is refused, including through the lifecycle definer function. A grant from a real ceremony
authorizes exactly its own write, once, in its own transaction: not a second write, not another action, target,
parameter set, administrator or session, not in a later transaction, not twice under concurrency; a rolled-back
savepoint returns the claim and a rolled-back consumption returns the grant. `test_mfa_api.py` drives every route
end to end with real `active` enforcement and deliberately un-normalized input.

**Still possible for a compromised API process** (unchanged by S3): waiting for a genuine ceremony and then
performing exactly what it approved (the administrator saw and signed that action); everything in "What each
compromised identity can and cannot do" above; ordinary-user account takeover (stage 3); and, as before,
`kyc_change_creator_status`'s `p_actor` is recorded as given (the grant, not `p_actor`, is the authority).

### Limits
- A compromised API **process** (not just a database role) sees invitation tokens when it issues them and could
  accept those; every such invitation was issued through a guarded anchor an administrator approved.
- Guards cannot be switched off by the API roles: `session_replication_role` and `ALTER TABLE … DISABLE TRIGGER`
  need superuser or ownership; `SET CONSTRAINTS … IMMEDIATE` only runs the deferred creator check earlier.
- The first activation (`creator.activate`) is covered by the same guard branch as reactivation and by the
  DB tests, but the end-to-end API test drives reactivation (first activation needs a KYC-approved owner fixture).
- Verified on PostgreSQL 16 locally and 17 in CI. **PostgreSQL 15 (DEV) is not tested**: 0027 uses only PL/pgSQL
  row and deferred constraint triggers, `jsonb`, `to_json`, pgcrypto `digest` and `pg_current_xact_id_if_assigned`
  (PostgreSQL 13+); the postgres:15 CI job proposed for 0025 would cover it.

### S3: activation prerequisites (each separately approved; nothing here was run)
Activation stays the operator's (`stepup_enforcement` changes only as `hyperchat_owner`, and 0025 refuses it while
any active administrator has fewer than two active passkeys). Before it is even requested:
1. 0025, 0026 and 0027 deployed and the API on the matching release, on the target environment.
2. The verifier installed and healthy on its own OS user, socket and role; `check_isolation.sh` all `OK`; the API's
   OS user not mapped to `hyperchat_migrator`, `hyperchat_owner` or `hyperchat_stepup`.
3. Every active administrator has **at least two approved passkeys** (`readiness` reports ready).
4. Operator access tested: the migrator login from root works, `app.stepup.operator list/readiness` runs, and the
   recovery path (`recover`, re-registration, `approve`) has been rehearsed with a test administrator.
5. Rollback and lockout recovery written down and rehearsed: the operator sets the state back to `draft` (owner
   only; immediate, no deploy), and `downgrade 0026` removes the guards entirely if ever needed.
6. A full rehearsal on DEV first: activate, perform each sensitive action from the frontend with a passkey, confirm
   a write without a grant is refused, return to `draft`.
7. The frontend's ceremony UI exists (phase 13): without it no administrator can produce a grant.

## Canonical actions (`app/stepup/canonical.py`)

`challenge = SHA-256("hyperchat-stepup-v1\n" ‖ canonical JSON)`, where the canonical JSON object is:
```
{action, admin, expires_at, issued_at, nonce, params, params_hash, purpose, rp_id, session, target, v}
```
- **`session`** is SHA-256 of the session id; the raw id is never exposed.
- **`params_hash`** is SHA-256 of the canonical parameters. A grant carries `params_hash`, so the database can check that the **exact** parameters were approved.
- **Format rules** that keep the frontend byte-identical:
  - sorted ASCII keys, no whitespace
  - safe integers only, no floats
  - no control characters or unpaired surrogates
  - bounded sizes
- **Test vectors:** `backend/tests/stepup/canonical_vectors.json`. The frontend must reproduce them byte for byte.

## Operator tool (`python -m app.stepup.operator`, root on the server, migration role)

| Command | Effect |
|---|---|
| `list [--email]`, `readiness` | Read-only. |
| `approve --email --fingerprint --verified-by --operator --confirm-database` | Approves a pending passkey. Only after confirming **out of band** that the administrator sees exactly that fingerprint (100 bits, 5×4 base32) on the screen where they registered it. A fingerprint belonging to a different administrator, a non-pending passkey, or one older than 72 hours is refused. |
| `revoke ... --reason` | Revokes a lost or compromised passkey. It immediately invalidates its unconsumed grants and its session assurance. |
| `recover --email --reason` | All authenticators lost: revokes every passkey and ends every session. The administrator re-registers and the operator re-approves. |

Password reset, email change and Google sign-in never touch passkeys or give assurance.

## Deployment sequence (later, each step **[APPROVAL]**; nothing here was run)

Real state from the S0 audit (2026-10-17):
- **DEV:** PostgreSQL **15.19**, database at **0019**.
- **Production:** PostgreSQL **17.11**, database at **0009**.
- **Both:** the old `pg_hba` and `pg_ident` (no lifecycle, auth or stepup roles); none of the roles exist.

1. **PostgreSQL 15 verification first.** CI tests PostgreSQL 17 only. Add a CI job against the disposable `postgres:15` image (its use still needs approval) that runs the migration round trips (`database/tests`) and `tests/stepup`, `tests/auth` and `tests/kyc`. The features 0025 uses all exist in PostgreSQL 13+: `xid8`, `pg_current_xact_id[_if_assigned]`, identity columns, `to_jsonb`, pgcrypto `digest`, column privileges and statement triggers.
2. **Roles and authentication rules**, per cluster (DEV `15/restoretest`, then production `17/hyperchat`):
   - Re-run `roles.sql` (creates `hyperchat_lifecycle`, `hyperchat_auth`, `hyperchat_stepup`).
   - Re-run `setup_cluster.sh` (`sync_pg_hba.sh` puts the rule with all login roles above the generic rule, with a backup).
   - Add the `pg_ident` lines (API user → lifecycle and auth; `hyperchat[-prod]-stepup` → `hyperchat_stepup`) and reload.
   - Then `bash deploy/check_isolation.sh dev|prod` must pass its PostgreSQL section.
3. **Migrations in order.** DEV goes 0020 → 0025; production goes 0010 → 0025, after its own approvals for every intermediate phase. Set `HYPERCHAT_DB_LIFECYCLE_USER` and `HYPERCHAT_DB_AUTH_USER` in `api.env` **before** restarting onto 0023 and 0024. KYC enforcement stays `draft`; MFA enforcement stays `draft`.
4. **Verifier install:**
   - `useradd --system --no-create-home --shell /usr/sbin/nologin hyperchat-stepup` (no supplementary groups)
   - `/opt/hyperchat-stepup` and `/etc/hyperchat-stepup` as `root:hyperchat-stepup 750`
   - `stepup.env` from `deploy/stepup.env.example` (or `.production.example`) as `root:hyperchat-stepup 640`
   - a release built like the API's, owned by root
   - units from `deploy/systemd/hyperchat[-prod]-stepup.{socket,service}`; `systemctl enable --now` the **socket**
   - `check_isolation.sh` must print only `OK`
5. **S2 (API endpoints) and enrollment.** Each administrator registers two passkeys; the operator approves each after verifying its fingerprint; `readiness` must report ready.
6. **S3:** migration 0027 after 0026 (no new roles, grants or settings; inert for callers without a grant while
   `draft`). Activation is a separate approval: "S3: activation prerequisites".
