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

## Creator dashboard API
Cookie-authenticated endpoints for creators (settings, donations, moderation, moderators, terms) —
see [`dashboard-api.md`](dashboard-api.md). Every route is creator-scoped through
`require_streamer_access`, every change needs CSRF + Origin, responses are `no-store`. Moderation
is a transactional state machine that changes database state only; alerts, TTS, bots and replay
react to that state in later phases.

## Database access
- Runtime roles: `hyperchat_app` (API) and `hyperchat_worker`: DML only, no DDL, no access to
  `alembic_version`. `hyperchat_readonly` for reporting. All via Unix-socket peer authentication.
- Schema changes only through Alembic (`hyperchat_migrator` → `SET ROLE hyperchat_owner`).
- One normalized `donations` table replaces the 133 per-creator `<slug>_donations` tables.

## Environments
| | Development (active) | Production (reserved) |
|---|---|---|
| Frontend | `https://app.quicktestspace.site`, `http://localhost:5173` | `https://hyperchat.space` (legacy site until cutover) |
| API | `https://api.quicktestspace.site` (127.0.0.1:8000) | `https://api.hyperchat.space` (127.0.0.1:8001) |
| Database | `hyperchat_dev` (cluster `15/restoretest`, 5433), rehearsal load of the legacy dump | `hyperchat` (cluster `17/hyperchat`, 5432) |
| Trusted browser origins | `https://app.quicktestspace.site`, `http://localhost:5173` | `https://hyperchat.space` |
| Cookie SameSite | `none` (localhost development is cross-site) | `lax` (frontend and API are same-site) |
| Status | all development, previews and testing | deployed and idle; used only from the final cutover phase |

All V2 work targets the development environment. The production API and database were provisioned
early and stay isolated (own OS user, port, env file, database mapping and rate-limit zone); no
frontend, preview or test uses them before the final cutover phase.

## Delivery plan
Phases 0–15 are defined in [`migration-audit.md`](migration-audit.md), section M.
Done: 0 (decisions), 1 (platform: clusters, roles, Alembic schema, ETL rehearsal, backups),
2 (FastAPI-managed authentication), 3 (authorization, admin tooling, database-backed security
primitives, explicit origins, cleanup, CI), 4 (public read API), 5 (creator dashboard API,
migration 0009), 6 (uploads, hypersounds, soundboard on R2), 7 (payments, 0010), 8 (jobs and worker, 0011),
9 (realtime, 0012), 10 (OBS audio, 0013), 11 (moderation bots, 0014–0015; bots disabled on DEV) — all running
on the development API (migration 0015). In progress: closing the gaps of the full backend audit
([`backend-completion-checklist.md`](backend-completion-checklist.md); groups 1–4 merged (migration 0016), security
audit fixes and the D1 password-import tooling merged (`main` at migration 0017), none of it deployed yet; the importer has not been run against
real data), which covers the parts of phase 12 that need no owner decision. Next: the new frontend (13), then integration,
security and regression testing. Only then: production deployment and data migration (14) and the cutover
and Supabase decommissioning (15).
