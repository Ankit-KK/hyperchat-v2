# Admin authentication boundary (migration 0024)

**Requirement:** the ordinary `hyperchat_app` database credentials alone must not let anyone become an administrator, forge an administrator's identity, or authorize privileged lifecycle changes.

## Assessment (before this change)
`hyperchat_app` is the API's general database role. It wrote every authentication table freely: `users`, `sessions`, `user_identities`, `user_invites` and `password_reset_tokens`.

**CSRF was no barrier.** The CSRF token is HMAC(server secret, session id), but `GET /api/v1/auth/session` returns it for any valid session cookie. So a forged session row was a complete session.

**Write paths that could establish or change admin authority, all as `hyperchat_app`:**

| Path | Code |
|---|---|
| Password sign-in: rehash and counters on the user row, then a new session | `AuthRepository.password_sign_in` |
| Google sign-in: identity link or update, user row, new session | `AuthRepository.google_sign_in` |
| Password change | `AuthRepository.change_password` |
| Password reset: token insert, then redemption (token, user row, sessions) | `AuthRepository.create_reset_token` / `reset_password` |
| Invites (any role, admin included) and their acceptance (user upsert, session) | `create_invite` / `accept_invite` |
| Admin user management: role, activity, creator link; email correction (also unlinks identities) | `AdminRepository.update_user` / `change_email` |
| First admin | `AdminRepository.bootstrap_admin` (CLI) |
| Session sliding and revocation, owner-transfer unlink, daily cleanup | the remaining writes, which are not authority-granting |

**Reproduced on a disposable PostgreSQL 17** (main `a070d2f`). Each attack used only `hyperchat_app` SQL, followed by legitimate API calls:

| # | Attack | Result |
|---|---|---|
| 1 | Overwrite an admin's password hash, then sign in | 200, and the admin API returned 200 |
| 2 | Insert an admin session row with a chosen token | the cookie worked, `/auth/session` returned the CSRF token, and the admin API returned 200 |
| 3 | `UPDATE users SET role = 'admin'` on an ordinary user | that user had the admin API |
| 4a | Insert a reset token for an admin, then redeem it | the admin password was replaced, and sign-in returned 200 |
| 4b | Insert an invite with role `admin`, then accept it | a new admin account |
| 4c | Bind the attacker's Google subject to an admin | Google sign-in signed in as that admin |
| 5 | Through path 1, `POST /admin/creators/{id}/activate` | the creator became active: the `hyperchat_lifecycle` boundary of PR4 was reached through a hijacked admin |

A sixth path was found during the assessment: **credentials prepared on an ordinary account before its promotion.** The app role can write a non-admin's password, identities or reset links, and a later promotion would hand them admin authority.

## Architecture selected
**A dedicated authentication role plus database-enforced guards on admin-capable rows.**

- **`hyperchat_auth`** (`deploy/postgres/roles.sql`) is used only by the authentication code path on its own short-lived connections:
  - sign-in, session issuance, password change and reset, invites, Google linking, admin user management, and the bootstrap CLI;
  - settings: `HYPERCHAT_DB_AUTH_USER`, plus the `pg_ident` maps for the API user and root.
- **Its privileges** are exactly those tables:
  - SELECT, INSERT and UPDATE on the five auth tables;
  - DELETE on `user_identities`;
  - SELECT on `user_signups` and `streamers`, plus UPDATE on `streamers.user_id` (an application invite claims its creator).
- **Guard triggers** (migration 0024, firing first: `aaa_…`) refuse these writes when they come from **`hyperchat_app` or `hyperchat_worker`**:

| Table | Refused for the runtime roles |
|---|---|
| `users` | inserting an admin; making a row admin; any update of an admin row (except unlinking its creator); deleting an admin |
| `sessions` | inserting a session for an admin, under a share lock on the user row taken *before* the role is read, so a concurrent promotion is waited for. For every session: changing its user, token or CSRF hash, absolute expiry or creation time; reviving a revoked one; extending one that already expired. Sliding a live session within its absolute expiry stays allowed, because the API does it on every request. |
| `user_identities` | linking to an admin; changing any identity's owner, provider or subject; any update of an admin's identity |
| `password_reset_tokens` | creating one for an admin; anything but burning one (`used_at` NULL → now) |
| `user_invites` | role `admin`; anything but accepting or revoking, once |

**Policy changes that close the prepared-credential path:**
- **No promotion.** An existing account can't be made an admin: the API answers 409, "Send an admin invitation instead". The `users` guard refuses an UPDATE that makes a row admin for **every** role the API connects as, `hyperchat_auth` included. Demotion and deactivation still work.
- **Insert-only admin creation.** Admins come only from an **admin invitation** or the **bootstrap CLI**, and both are a plain `INSERT ... ON CONFLICT (email) DO NOTHING`, with no `DO UPDATE` fallback. An account with the address, even one another transaction inserts while they run, refuses the whole transaction: the invitation is not consumed (409), and bootstrap exits with "already exists". An application (creator-owner) invitation is insert-only the same way.
- **Ordinary invitations never touch an admin row.** A user or creator-staff invitation may still give a password to an existing password-less, active account, but never to an admin (409).

**What stays with `hyperchat_app`:** everything about ordinary users and creators. That covers reads, rate limits, session sliding and revocation, logout, the owner-transfer unlink and cleanup deletes.

**Why this design:**
- It's the smallest change that makes `hyperchat_app` unable to create or alter admin authority.
- It enforces that in PostgreSQL, so it holds for every path, including SQL injection anywhere in the API and any future code using the general role.
- It keeps the existing transactions and lock order: users → reset tokens / identities → sessions, with invite → creator → users for acceptance.
- A caller-supplied admin id or session variable is never trusted. The guard keys on the database role that executes the statement (`current_user`), which the caller cannot choose.

## What each threat model gets
| Threat | Protected? |
|---|---|
| **SQL injection** in any `hyperchat_app` query, a leaked `hyperchat_app` connection, or a component holding only `hyperchat_app` | **Yes, for administrators.** It can't become, impersonate or extend an admin, so it can't reach admin APIs or the lifecycle transition. |
| **SQL injection inside the authentication code** (`hyperchat_auth` queries) | That code is small and fully parameterized, but `hyperchat_auth` could write admin rows. It is a much smaller surface, not zero. |
| **Compromise of the API process or its OS user** (`hyperchat` / `hyperchat-prod`) | **No.** Peer authentication maps the same OS user to `hyperchat_app`, `hyperchat_auth` and `hyperchat_lifecycle`, and the process holds the session secret. An extra PostgreSQL role does not protect against a process that is legitimately allowed to open all of them. |
| **Ordinary (non-admin) accounts against `hyperchat_app`** | **Not in scope.** The app role can still change a creator's or user's credentials and sessions. Admin promotion is closed, so this can't escalate to admin. |

## Remaining limitations and the next stage
**Stage 2, required before production launch: an admin step-up factor verified outside the API process.**
- **Option A:** TOTP or WebAuthn secrets stored in a table only a separate service, or `hyperchat_auth` functions with the secret never in the API's memory, can verify.
- **Option B:** admin sign-in through an external identity provider (Google Workspace / OIDC with MFA) whose assertion the API verifies with a key the database role can't forge.

Either one is what closes a compromised-process threat for admin actions. Stage 1 (this PR) closes the database-only threat, which is the one from the PR #71 review.

**Stage 3, optional:** move ordinary user and creator credential writes behind `hyperchat_auth` too, so `hyperchat_app` can't take over creator accounts either.

## Server setup (before deploying migration 0024)
1. Re-run `deploy/postgres/roles.sql`, which creates `hyperchat_auth`.
2. Grant it `CONNECT` on the database (`setup_cluster.sh` does this for new clusters).
3. Add the `pg_ident` lines (`hyperchat` / `hyperchat-prod` and `root` → `hyperchat_auth`), then reload.
   The cluster's HyperChat `pg_hba.conf` line must list `hyperchat_auth` too (fixed in the template by S0;
   `deploy/check_isolation.sh` reports it, `deploy/README.md` "Isolation audit" applies it).
4. Set `HYPERCHAT_DB_AUTH_USER=hyperchat_auth` in api.env.
   - **Without it:** non-admin sign-in still works, but **administrators cannot sign in**. The API logs an error at startup.
5. Then migrate (the migration grants to `hyperchat_auth`, so it fails safely if the role is missing).

**PostgreSQL 15 (DEV):** the features used (row triggers, `to_jsonb(record)`, `FOR SHARE` in a sub-select, column grants) all exist in PostgreSQL 11+, but this is **not yet tested on 15**. The disposable `postgres:15` image still needs approval.
