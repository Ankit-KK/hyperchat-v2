"""Partner API (D4: ClipWave), replacing the legacy partner-signup / partner-check-streamer Edge Functions:

  POST /api/v1/partner/signups          record a creator signup for the calling partner
  POST /api/v1/partner/streamers/check  is this channel name already a HyperChat creator? (boolean only)

Server-to-server: x-api-key only (require_partner_key; no cookies, CSRF or CORS). The partner is ALWAYS the key's
partner_slug, never a body field. Order as legacy: key, per-partner rate limit, body size, JSON, fields. Responses keep
the legacy shapes ({"error": "Validation failed", "fields": {...}} is a 400, not FastAPI's 422) so ClipWave only
changes the URL. Admin key management (legacy generate-partner-key) is at /api/v1/admin/partner-keys.
"""

import json
import logging
import re
import secrets
import uuid

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.auth.dependencies import CurrentUser, client_ip, require_admin
from app.core.errors import ApiError
from app.partners import validation
from app.partners.repository import PartnerRepository
from app.security.audit import security_event
from app.security.crypto import sha256_hex
from app.security.dependencies import PartnerKey, require_partner_key
from app.stepup.actions import StepUp, step_up

router = APIRouter(prefix="/api/v1/partner", tags=["partner"])
admin_router = APIRouter(prefix="/api/v1/admin/partner-keys", tags=["admin"])

SIGNUP_RATE = ("partner:signup", 60, 60)   # legacy: 60 / minute per partner
CHECK_RATE = ("partner:check", 30, 60)     # legacy: 30 / minute per partner (no bulk slug scanning)
SIGNUP_MAX_BYTES, CHECK_MAX_BYTES = 10240, 2048
PARTNER_SLUG = re.compile(r"[a-z0-9_-]{2,50}")
UNAVAILABLE = ApiError(503, "Partner API unavailable. Please try again later.", "partner_unavailable")
EXISTS_MESSAGE = {True: "This creator is already registered on HyperChat.",
                  False: "This creator is not registered on HyperChat yet."}


def _reply(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _repo(request: Request) -> PartnerRepository:
    repo = getattr(request.app.state, "partners", None)
    if repo is None:
        raise UNAVAILABLE
    return repo


async def _guarded_body(request: Request, partner: PartnerKey, rate: tuple, max_bytes: int):
    """Per-partner rate limit, then the size-limited JSON body; a JSONResponse when refused."""
    try:
        allowed = await request.app.state.auth.repo.hit_rate_limit(f"partner:{partner.partner_slug}", *rate)
    except psycopg.Error:
        raise UNAVAILABLE from None
    if not allowed:
        security_event("partner_rejected", reason="rate_limited", partner_slug=partner.partner_slug)
        return _reply({"error": "Too many requests. Please try again later."}, 429)
    raw = await request.body()
    if len(raw) > max_bytes:
        return _reply({"error": "Request too large"}, 413)
    try:
        return json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError):
        return _reply({"error": "Invalid JSON body"}, 400)


@router.post("/signups")
async def partner_signup(request: Request, partner: PartnerKey = Depends(require_partner_key)):
    body = await _guarded_body(request, partner, SIGNUP_RATE, SIGNUP_MAX_BYTES)
    if isinstance(body, JSONResponse):
        return body
    errors, value = validation.signup(body)
    if errors:
        return _reply({"error": "Validation failed", "fields": errors}, 400)
    repo = _repo(request)
    ua = (request.headers.get("user-agent") or "")[:300] or None
    try:
        candidates = validation.slug_candidates(value["channel_name"])
        exists = bool(candidates) and await repo.creator_exists(candidates)
        signup_id = await repo.add_signup(partner.partner_slug, value, client_ip(request), ua)
    except psycopg.Error:
        security_event("partner_signup_failed", reason="database_error", partner_slug=partner.partner_slug)
        raise UNAVAILABLE from None
    if signup_id is None:  # this partner already sent this email: success, do not retry (legacy)
        return _reply({"success": True, "duplicate": True, "already_on_hyperchat": exists})
    security_event("partner_signup_recorded", level=logging.INFO, partner_slug=partner.partner_slug)
    return _reply({"success": True, "id": signup_id, "already_on_hyperchat": exists}, 201)


@router.post("/streamers/check")
async def partner_check_streamer(request: Request, partner: PartnerKey = Depends(require_partner_key)):
    body = await _guarded_body(request, partner, CHECK_RATE, CHECK_MAX_BYTES)
    if isinstance(body, JSONResponse):
        return body
    errors, candidates = validation.check(body)
    if errors:
        return _reply({"error": "Validation failed", "fields": errors}, 400)
    try:
        exists = await _repo(request).creator_exists(candidates)
    except psycopg.Error:
        raise UNAVAILABLE from None
    return _reply({"success": True, "exists": exists, "message": EXISTS_MESSAGE[exists]})


# ── admin key management (legacy generate-partner-key) ─────────────────────────

class KeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    partner_slug: StrictStr = Field(max_length=50)


def _key(row: dict) -> dict:
    return {"id": row["id"], "partner_slug": row["partner_slug"], "key_prefix": row["key_prefix"],
            "revoked": row["revoked"], "last_used_at": row["last_used_at"] and row["last_used_at"].isoformat(),
            "created_at": row["created_at"].isoformat()}


@admin_router.get("")
async def list_partner_keys(request: Request, current: CurrentUser = Depends(require_admin)) -> dict:
    """Never the key or its hash: id, partner, display prefix, state, last use."""
    return {"keys": [_key(r) for r in await _repo(request).list_keys()]}


@admin_router.post("", status_code=201)
async def create_partner_key(body: KeyCreate, request: Request, current: CurrentUser = Depends(require_admin),
                             stepup: StepUp = Depends(step_up("partner_key.create"))):
    """A new key for a partner: hc_<partner_slug>_<64 hex>, returned ONCE; only its SHA-256 is stored. Existing keys
    of the partner stay valid (rotate = create, switch the partner over, then revoke the old one)."""
    slug = body.partner_slug.strip().lower()
    if not PARTNER_SLUG.fullmatch(slug):
        raise ApiError(400, "partner_slug must be 2-50 characters: lowercase letters, digits, - or _",
                       "bad_partner_slug")
    grant = stepup.bind("partner_key", body.model_dump())
    api_key = f"hc_{slug}_{secrets.token_hex(32)}"
    row = await _repo(request).create_key(slug, sha256_hex(api_key), api_key[:12], grant)
    security_event("partner_key_created", level=logging.INFO, user_id=current.user.id, partner_slug=slug)
    return _reply({"api_key": api_key, "key": _key(row)}, 201)  # no-store: the only time the key is ever shown


@admin_router.post("/{key_id}/revoke")
async def revoke_partner_key(key_id: str, request: Request, current: CurrentUser = Depends(require_admin)) -> dict:
    try:
        key_id = str(uuid.UUID(key_id))
    except ValueError:
        raise ApiError(404, "Key not found", "partner_key_not_found") from None
    result, row = await _repo(request).revoke_key(key_id)
    if result == "not_found":
        raise ApiError(404, "Key not found", "partner_key_not_found")
    if result == "revoked":
        security_event("partner_key_revoked", user_id=current.user.id, partner_slug=row["partner_slug"])
    return {"key": _key(row), "changed": result == "revoked"}
