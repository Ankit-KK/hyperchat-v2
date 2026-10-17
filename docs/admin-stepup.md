# Administrator step-up authentication (passkeys): S1 and S2

S1 is the foundation of administrator MFA (approved design: WebAuthn only, a separate verifier, operator-approved
passkeys, single-use action-bound grants). S2 connects the API to it (endpoints, action-bound grants on the sensitive
admin routes, no Google auto-linking for administrators).

**Still not enforced:**
- The enforcement state is `draft`.
- No database write *requires* a grant: that is S3.
- Nothing here is deployed.

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
- Nothing is enforced until S2 and S3.

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
| Route | Action | Target | Params (exactly as sent) |
|---|---|---|---|
| `POST /api/v1/admin/creators/{id}/activate` | `creator.activate` | `creator:{id}` | `{expected_status, reason}` |
| `POST /api/v1/admin/creators/{id}/reactivate` | `creator.reactivate` | `creator:{id}` | `{expected_status, reason}` |
| `POST /api/v1/admin/creators` | `creator.create` | `creator:new` | the full body |
| `POST /api/v1/admin/creators/{id}/owner` | `creator.owner.assign` | `creator:{id}` | `{user_id}` |
| `POST /api/v1/admin/creators/{id}/owner/transfer` | `creator.owner.transfer` | `creator:{id}` | `{user_id, expected_owner_id}` |
| `POST /api/v1/admin/applications/{id}/approve` | `application.approve` | `application:{id}` | the full body |
| `POST /api/v1/admin/applications/{id}/invite` | `application.invite` | `application:{id}` | `{}` |
| `POST /api/v1/auth/invites` **with `role: admin`** | `admin.invite` | `invite` | `{email, role, streamer_id}` |
| `PATCH /api/v1/admin/users/{id}` | `admin.user.change` | `user:{id}` | exactly the fields sent |
| `PUT /api/v1/admin/users/{id}/email` | `admin.user.email` | `user:{id}` | `{email}` |
| `POST /api/v1/admin/partner-keys` | `partner_key.create` | `partner_key` | `{partner_slug}` |

- **Unbound actions:** `mfa.credential.revoke`. Passkeys are revoked by the operator only.
- **Not step-up actions (aal2 only once enforcement is active):**
  - defensive actions: suspend, restrict, revoke grace, revoke sessions, revoke a partner key
  - reads
  - soundboard eligibility
  - ordinary (non-admin) invitations
- **Params come from the request body as the browser sent it, before the service normalizes anything.** The frontend therefore asks for a ceremony with the same body it will send. A grant never authorizes "this action in general": its parameter hash binds the exact values.

### Transaction boundaries
- **The consumption is inside the operation's transaction.** Each bound route's repository method calls `public.stepup_consume(grant, admin, session, action, target, params_hash)` **first inside the same transaction as the operation**, on the role that performs it:
  - `hyperchat_lifecycle` for activate and reactivate (with the `kyc_change_creator_status` call)
  - `hyperchat_auth` for user, email and admin-invite changes
  - `hyperchat_app` for creators, owners, applications and partner keys

  All three got EXECUTE in 0025, so S2 needs **no new database privileges**.
- **A refused grant** (`mismatch`, `consumed`, `expired`, `credential_revoked`, `not_admin`, `session_invalid`, `not_found`) raises and rolls the operation back.
- **A refused operation** (a stale compare-and-set, a claimed creator, an unknown user, nothing changed, an exception) rolls the **consumption** back too (`psycopg.Rollback`). A grant is spent only by the operation it approved, and stays usable for a retry within its 2 minutes.
- **Concurrency:** two requests with one grant serialize on the grant's row, and exactly one succeeds.

### Temporary compatibility (until S3; documented, no new privilege)
- **`draft`:** a bound route without `X-Step-Up-Grant` still works, so existing admins are not locked out before enrolling. With a grant it is consumed and must be valid (fail closed).
- **`active`** (only by the operator, after S3 and its approval): `require_admin` needs aal2 (passkey endpoints excepted), and a bound route without a grant answers `step_up_required`.
- **What this rests on:** both behaviours are API checks. A process that skips them is stopped only by S3's database guards (`stepup_claim` and `stepup_effect` on each anchor write). **S2 does not claim those guards are active.**

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
6. **S3** (database enforcement) and its activation, separately authorized.
