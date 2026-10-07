"""FastAPI dependencies for non-browser callers.

  require_obs_token       OBS overlays: token (header x-obs-token or ?token=), frontend Origin or none
  require_partner_key     partners: x-api-key
  require_signed_request  internal services / workers: HMAC signature + single-use nonce
  require_cron_secret     scheduled jobs: x-cron-secret
Every rejection is a SecurityError -> {"error": ...} with a secret-free security event.
"""

from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.security.audit import security_event
from app.security.nonce_store import NonceStore
from app.security.obs_token import (
    ObsTokenInfo,
    ObsTokenStore,
    get_obs_token,
    is_obs_origin_allowed,
    own_origin,
    validate_obs_token,
)
from app.security.request_signing import verify_hmac_request
from app.security.shared_secret import PartnerKey, PartnerKeyStore, verify_partner_api_key, verify_shared_secret


class SecurityError(Exception):
    def __init__(self, status: int, error: str, reason: str) -> None:
        super().__init__(reason)
        self.status, self.error, self.reason = status, error, reason


@dataclass
class SecurityStores:
    nonces: NonceStore
    obs_tokens: ObsTokenStore
    partner_keys: PartnerKeyStore


@dataclass(frozen=True)
class ObsCaller:
    info: ObsTokenInfo
    token: str  # kept in memory only, for channel-scope checks; never logged


def install_security_error_handler(app: FastAPI) -> None:
    @app.exception_handler(SecurityError)
    async def _handle(request: Request, exc: SecurityError) -> JSONResponse:
        security_event("request_rejected", reason=exc.reason, status=exc.status, path=request.url.path,
                       method=request.method, client_ip=request.client.host if request.client else None)
        return JSONResponse({"error": exc.error}, status_code=exc.status)


def _stores(request: Request) -> SecurityStores:
    stores = getattr(request.app.state, "security", None)
    if stores is None:
        raise SecurityError(503, "Security layer not configured", "security_not_configured")
    return stores


def _secret(value) -> str | None:
    return value.get_secret_value() if value is not None else None


async def require_obs_token(request: Request) -> ObsCaller:
    if not is_obs_origin_allowed(request.headers.get("origin"), request.app.state.settings.browser_origins,
                                 own_origin(request.headers.get("host"))):
        raise SecurityError(403, "Request origin not allowed", "bad_origin")
    token = get_obs_token(request.headers, request.query_params)
    result = await validate_obs_token(_stores(request).obs_tokens, token)
    if not result.ok:
        raise SecurityError(401, "Invalid or missing OBS token", result.error or "invalid_token")
    return ObsCaller(info=result.token_info, token=token)


async def require_partner_key(request: Request) -> PartnerKey:
    result = await verify_partner_api_key(_stores(request).partner_keys, request.headers.get("x-api-key"))
    if not result.ok:
        raise SecurityError(401, result.error, "bad_partner_key")
    return result.partner


async def require_signed_request(request: Request) -> None:
    settings = request.app.state.settings
    keys = [k for k in (_secret(settings.internal_service_secret),
                        _secret(settings.internal_service_secret_previous)) if k]
    result = await verify_hmac_request(request.headers, await request.body(), keys, _stores(request).nonces)
    if not result.ok:
        raise SecurityError(result.status, result.error or "Invalid request signature", result.reason)


async def require_cron_secret(request: Request) -> None:
    if not verify_shared_secret(request.headers.get("x-cron-secret"), _secret(request.app.state.settings.cron_secret)):
        raise SecurityError(401, "Unauthorized", "bad_cron_secret")
