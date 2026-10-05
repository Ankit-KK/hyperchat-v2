# HyperChat v2 API (FastAPI)

The only backend of HyperChat v2: every client (web frontend, OBS overlays, bots, partners)
talks to this API, and only this API talks to PostgreSQL and the external services.

## Layout
```
app/main.py            app factory, lifespan, request logging, CORS
app/core/config.py     HYPERCHAT_* environment settings
app/core/logging.py    JSON-lines logging (no query strings, headers or bodies)
app/db/pool.py         psycopg 3 async pool + ping
app/auth/              FastAPI-managed authentication (sessions, CSRF, passwords, invites, Google)
app/security/          server-to-server building blocks (HMAC signing, nonces, upload grants,
                       OBS tokens, shared secrets, partner keys, CORS, security logging)
app/routers/           GET /health, /api/v1/auth/*
tests/                 unit tests + end-to-end auth tests (scratch database)
```
The database schema and migrations live in `../database`, deployment files in `../deploy`.

## Configuration
All settings are `HYPERCHAT_*` environment variables; template: `../deploy/api.env.example`.
On the VPS they live in `/etc/hyperchat/api.env` (root:hyperchat, 0640), never in Git.
`HYPERCHAT_DB_LABEL` is reported in logs and `/health`; the app refuses to start with
`HYPERCHAT_APP_ENV=production` if the label contains `TEST` or `DEV`.

## Authentication (`/api/v1/auth`)
- Passwords: Argon2id. bcrypt hashes imported from the legacy system at cutover verify and are
  upgraded to Argon2id on the next login.
- Browser sessions: opaque token in the `__Host-hc_session` cookie (Secure, HttpOnly, SameSite=Lax);
  only its SHA-256 is stored. 7-day sliding idle expiry, 30-day absolute expiry.
- CSRF: every cookie-authenticated POST/PUT/PATCH/DELETE needs an allowlisted `Origin` and the
  `x-csrf-token` header (returned by `/login` and `GET /session`). Login itself checks `Origin`.
- 5 failed logins lock the account for 15 minutes; IP rate limits in `rate_limits`.
- Password reset: single-use 1-hour token, all sessions revoked. Change password revokes other sessions.
- Onboarding is invite-only (`POST /invites`, admin; `POST /invites/accept`).
- Google: authorization code + PKCE + state + nonce; links an existing account by Google subject,
  then by verified email. Unknown Google accounts are rejected.

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/auth/login` | email + password -> session cookie + CSRF token |
| `GET /api/v1/auth/session` | current user + CSRF token |
| `POST /api/v1/auth/logout`, `/logout-all` | revoke this / every session |
| `POST /api/v1/auth/password/forgot`, `/reset`, `/change` | password reset and change |
| `POST /api/v1/auth/invites`, `/invites/accept` | admin invite, accept invite |
| `GET /api/v1/auth/google/start`, `/google/callback` | Google sign-in |

## Tests
```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```
`tests/auth/test_auth_flow.py` runs only with `HYPERCHAT_TEST_AUTH_DB=<scratch_db>:<port>` (an
Alembic-migrated scratch database; `hyperchat_dev` additionally needs `HYPERCHAT_TEST_AUTH_ALLOW_DEV=1`).
It never runs against `hyperchat` (production) or `hyperchat_restore_test`.
