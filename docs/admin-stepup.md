# Administrator step-up authentication (passkeys): S1

S1 is the foundation of administrator MFA (approved design: WebAuthn only, a separate verifier, operator-approved
passkeys, single-use action-bound grants). **Nothing is enforced yet**: the enforcement state is `draft`, no API
route calls the verifier (S2), and no database write requires a grant (S3). Nothing here is deployed.

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
  - a grant needs an action challenge verified **in the same transaction**, with identical bindings and an active passkey; it lasts at most 2 minutes and is consumed once, only through `stepup_consume()`
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
