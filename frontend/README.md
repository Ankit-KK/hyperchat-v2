# HyperChat v2 frontend (not started)

A completely new React application, built from scratch against the FastAPI API. No code from the
legacy (Supabase-based) frontend is copied here; the legacy app is used only as a reference for
features and behaviour.

## Environments
**V2 is not production-ready.** The frontend is built, integrated and tested against the
**development API only**:

| | Development (all current work) | Production (final cutover phase only) |
|---|---|---|
| Frontend | `https://app.quicktestspace.site` (Vercel), `http://localhost:5173` | `https://hyperchat.space` |
| API | `https://api.quicktestspace.site` | `https://api.hyperchat.space` |

- The API base URL comes from build configuration (e.g. `VITE_API_BASE_URL`); the default and every
  development / preview build use `https://api.quicktestspace.site`.
- `https://api.hyperchat.space` is set **only** in the production Vercel environment, at the final
  cutover. No development or preview build may reference it (to be enforced by a CI check).
- Nothing is deployed to `hyperchat.space` until the cutover: it still serves the legacy app.

## Principles
- Talks **only** to the HyperChat API. No Supabase client, no direct database access, no RLS.
- Authentication: the API's HttpOnly session cookie. Requests are sent with `credentials: "include"`;
  every POST/PUT/PATCH/DELETE carries the `x-csrf-token` header obtained from
  `POST /api/v1/auth/login` or `GET /api/v1/auth/session`. Tokens are never stored in
  `localStorage`.
- The API only accepts its configured frontend origins (development: `https://app.quicktestspace.site`
  and `http://localhost:5173`).
- Realtime: Pusher, with private channels authorized by the API.
- Uploads: directly to Cloudflare R2 with short-lived URLs/grants issued by the API.
- Payments: Razorpay checkout; orders are created and verified by the API.
- Contact: no contact form (retired, D5). Show the contact name, email, phone numbers and Discord invite from
  `GET /api/v1/public/config` (each may be `null` / empty: hide what is missing), plus the signup interest form
  (`POST /api/v1/public/signups`). See `docs/public-api.md`.
- Hosting: Vercel.

## Status
Scaffolding only. Stack, routing, design system and API client are decided in the frontend phase.
