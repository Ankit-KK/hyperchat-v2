# HyperChat v2 frontend (not started)

A completely new React application, built from scratch against the FastAPI API. No code from the
legacy (Supabase-based) frontend is copied here; the legacy app is used only as a reference for
features and behaviour.

## Principles
- Talks **only** to the HyperChat API (`https://api.hyperchat.space`, development:
  `https://api.quicktestspace.site`). No Supabase client, no direct database access, no RLS.
- Authentication: the API's HttpOnly session cookie. Requests are sent with `credentials: "include"`;
  every POST/PUT/PATCH/DELETE carries the `x-csrf-token` header obtained from
  `POST /api/v1/auth/login` or `GET /api/v1/auth/session`. Tokens are never stored in
  `localStorage`.
- Realtime: Pusher, with private channels authorized by the API.
- Uploads: directly to Cloudflare R2 with short-lived URLs/grants issued by the API.
- Payments: Razorpay checkout; orders are created and verified by the API.
- Hosting: Vercel, at `https://hyperchat.space` / `https://www.hyperchat.space`.

## Status
Scaffolding only. Stack, routing, design system and API client are decided in the frontend phase.
