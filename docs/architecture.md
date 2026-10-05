# HyperChat v2 architecture

HyperChat v2 is a new application, not a modification of the legacy one: a new React frontend,
a FastAPI backend and a self-hosted PostgreSQL database. **Supabase is not part of this
architecture** — no Supabase Auth, Edge Functions, PostgREST/RLS browser access, Realtime or
Storage, and no Supabase client anywhere.

```
                    ┌────────────────────┐
                    │  New React Frontend│
                    │  hyperchat.space   │
                    └─────────┬──────────┘
                              │ HTTPS/API
                              ▼
                    ┌────────────────────┐
                    │   FastAPI Backend  │
                    │ api.hyperchat.space│
                    └─────────┬──────────┘
                              │
                ┌─────────────┴─────────────┐
                ▼                           ▼
        ┌───────────────┐          ┌────────────────┐
        │ PostgreSQL    │          │ External       │
        │ Application DB│          │ Services       │
        └───────────────┘          │ Razorpay       │
                                   │ Pusher         │
                                   │ R2             │
                                   │ Resend         │
                                   │ Google OAuth   │
                                   └────────────────┘
```

## Components
| Component | Technology | Hosting | Repository path |
|---|---|---|---|
| Frontend | new React app | Vercel (`hyperchat.space`, `www.hyperchat.space`) | `frontend/` (not started) |
| API | FastAPI, Uvicorn, psycopg 3 | VPS: Nginx → 127.0.0.1:8000, systemd | `backend/` |
| Database | PostgreSQL 17, schema owned by Alembic | VPS, Unix socket only | `database/` |
| Worker | background jobs (TTS, notifications, cleanup) | VPS, systemd | later phase |
| Deployment | Nginx, systemd, PostgreSQL setup, backups | VPS | `deploy/` |

External services, all called **only by the API/worker**: Razorpay (payments + webhooks),
Pusher (realtime; private channels authorized by the API), Cloudflare R2 (media storage, short-lived
upload grants), Resend (transactional email), Google OAuth (sign-in), plus the TTS/voice providers,
Telegram and Discord bots where features need them.

## Trust boundaries
| Caller | How it authenticates |
|---|---|
| Browser (frontend) | HttpOnly `__Host-hc_session` cookie + `x-csrf-token` header on every state change; Origin allowlist |
| OBS overlays | long random OBS token, stored as SHA-256, scoped to the creator's Pusher channels |
| Internal services, bots, workers | HMAC-signed requests with single-use nonces, or a shared secret (fail closed) |
| Partners | `x-api-key`, stored as SHA-256, revocable |
| Webhooks (Razorpay, Telegram, Discord) | the provider's own signature/secret scheme, verified by the API |

## Authorization
- Admin: `users.role = 'admin'`, may act on every creator; managed through `/api/v1/admin`. The first
  admin is created on the server with `python -m app.cli bootstrap-admin`.
- Creator access: owner (`streamers.user_id`) or linked account (`users.streamer_id`); creators nobody
  has claimed are admin-only. Enforced by `require_streamer_access` on every creator-scoped route.
- Role, status or creator-link changes revoke the user's sessions immediately.
- Browser trust is an explicit origin list (frontend + `HYPERCHAT_ALLOWED_ORIGINS`), shared by CORS,
  the CSRF/Origin check and the OBS origin gate.
- Moderators (Telegram/Discord bot identities, `streamers_moderators`) are authorized with the bot
  features, not as web users.

## Public read API
Unauthenticated, read-only endpoints for donation pages, overlays and the landing page — see
[`public-api.md`](public-api.md). Only `active` creators, fixed response fields, Cache-Control,
Nginx per-IP rate limiting.

## Database access
- Runtime roles: `hyperchat_app` (API) and `hyperchat_worker`: DML only, no DDL, no access to
  `alembic_version`. `hyperchat_readonly` for reporting. All via Unix-socket peer authentication.
- Schema changes only through Alembic (`hyperchat_migrator` → `SET ROLE hyperchat_owner`).
- One normalized `donations` table replaces the 133 per-creator `<slug>_donations` tables.

## Environments
| | Development | Production |
|---|---|---|
| API | `https://api.quicktestspace.site` | `https://api.hyperchat.space` |
| Database | `hyperchat_dev` (cluster `15/restoretest`, 5433), rehearsal load of the legacy dump | `hyperchat` (cluster `17/hyperchat`, 5432) |
| Cookie SameSite | `lax` | `lax` (frontend and API are same-site) |
| Status | running (auth) | schema only; no traffic until cutover |

## Delivery plan
Phases 0–15 are defined in [`migration-audit.md`](migration-audit.md), section M.
Done: 0 (decisions), 1 (platform: clusters, roles, Alembic schema, ETL rehearsal, backups),
2 (FastAPI-managed authentication), 3 (authorization, admin tooling, database-backed security
primitives, explicit origins, cleanup, CI). Next: the feature APIs (4–12), the new frontend (13),
cutover rehearsal (14) and Supabase decommissioning (15).
