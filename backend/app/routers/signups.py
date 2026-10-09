"""POST /api/v1/public/signups: register interest in becoming a creator (legacy SignupDialog).

Public: no cookies, but the request must come from a trusted frontend Origin and is rate limited per IP.
A repeated email gets the same answer as a new one, so the endpoint cannot be used to find out who signed up
(legacy said "This email is already registered"; deliberate difference, see docs/backend-completion-checklist.md).
"""

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.auth.dependencies import client_ip
from app.core.errors import ApiError
from app.public.signups import SignupRepository, SignupRequest
from app.security.audit import security_event

router = APIRouter(prefix="/api/v1/public", tags=["public"])
RATE = ("signup:create", 5, 3600)  # bucket, requests, seconds per IP
UNAVAILABLE = ApiError(503, "Signups are unavailable right now. Please try again later.", "signups_unavailable")


async def _guard(request: Request) -> SignupRepository:
    """Trusted Origin, then the per-IP rate limit; runs before the body is validated."""
    ip = client_ip(request)
    origin = request.headers.get("origin")
    if not origin or origin not in request.app.state.settings.browser_origins:
        security_event("signup_rejected", reason="bad_origin", client_ip=ip)
        raise ApiError(403, "Request origin not allowed", "bad_origin")
    repo = getattr(request.app.state, "signups", None)
    if repo is None:
        raise UNAVAILABLE
    try:
        allowed = await request.app.state.auth.repo.hit_rate_limit(f"ip:{ip}", *RATE)
    except psycopg.Error:
        security_event("signup_failed", reason="database_error", client_ip=ip)
        raise UNAVAILABLE from None
    if not allowed:
        security_event("signup_rejected", reason="rate_limited", client_ip=ip)
        raise ApiError(429, "Too many signups. Please try again later.", "rate_limited")
    return repo


@router.post("/signups", status_code=201)
async def create_signup(body: SignupRequest, request: Request, repo: SignupRepository = Depends(_guard)):
    try:
        await repo.add(body)  # new or already registered: the same answer (no enumeration)
    except psycopg.Error:
        security_event("signup_failed", reason="database_error", client_ip=client_ip(request))
        raise UNAVAILABLE from None
    return JSONResponse({"status": "received"}, status_code=201, headers={"Cache-Control": "no-store"})
