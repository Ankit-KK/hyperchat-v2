"""Request-level auth: session cookie, CSRF + Origin checks, admin role."""

from dataclasses import dataclass

from fastapi import Request, Response

from app.auth.repository import Session, User
from app.auth.service import AuthError, AuthService
from app.auth.tokens import constant_time_equal
from app.stepup.grants import MfaRequired

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True)
class CurrentUser:
    user: User
    session: Session


def auth_service(request: Request) -> AuthService:
    service = getattr(request.app.state, "auth", None)
    if service is None:
        raise AuthError(503, "Authentication is not configured", "auth_not_configured")
    return service


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def optional_user(request: Request) -> CurrentUser | None:
    svc = auth_service(request)
    resolved = await svc.resolve(request.cookies.get(svc.settings.session_cookie_name))
    return CurrentUser(user=resolved[1], session=resolved[0]) if resolved else None


async def require_user(request: Request) -> CurrentUser:
    current = await optional_user(request)
    if current is None:
        raise AuthError(401, "Not signed in", "no_session")
    if request.method in UNSAFE_METHODS:
        enforce_csrf(request, auth_service(request), current.session)
    return current


async def require_admin_enrolling(request: Request) -> CurrentUser:
    """An admin session at any assurance level: only for the passkey endpoints (enrollment and ceremonies)."""
    current = await require_user(request)
    if current.user.role != "admin":
        raise AuthError(403, "Admin access required", "not_admin")
    return current


async def require_admin(request: Request) -> CurrentUser:
    """An admin session. While MFA enforcement is 'draft' (S2) that is all; once it is 'active' the session must also
    hold passkey assurance (aal2, migration 0025), or the answer is 403 mfa_required."""
    current = await require_admin_enrolling(request)
    state, aal = await auth_service(request).repo.stepup_session(current.session.id)
    if state == "active" and aal < 2:
        raise MfaRequired()
    return current


def enforce_csrf(request: Request, svc: AuthService, session: Session) -> None:
    """Cookie-authenticated state changes need an allowlisted Origin AND the session's CSRF token."""
    if not svc.origin_allowed(request.headers.get("origin")):
        raise AuthError(403, "Request origin not allowed", "bad_origin")
    presented = request.headers.get("x-csrf-token") or ""
    if not presented or not constant_time_equal(presented, svc.csrf_token(session)):
        raise AuthError(403, "Invalid CSRF token", "bad_csrf")


def set_session_cookie(response: Response, svc: AuthService, token: str) -> None:
    s = svc.settings
    response.set_cookie(
        s.session_cookie_name, token, max_age=s.session_absolute_days * 86400, path="/",
        secure=True, httponly=True, samesite=s.session_cookie_samesite,
    )


def clear_session_cookie(response: Response, svc: AuthService) -> None:
    s = svc.settings
    response.delete_cookie(s.session_cookie_name, path="/", secure=True, httponly=True,
                           samesite=s.session_cookie_samesite)
