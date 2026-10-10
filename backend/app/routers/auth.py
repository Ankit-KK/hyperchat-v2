"""/api/v1/auth — FastAPI-managed authentication (replaces Supabase Auth)."""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.auth.dependencies import (
    CurrentUser,
    auth_service,
    clear_session_cookie,
    client_ip,
    optional_user,
    require_admin,
    require_user,
    set_session_cookie,
)
from app.auth.service import AuthError, public_user
from app.auth.tokens import new_token, sign_payload, verify_payload

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
OAUTH_COOKIE = "__Host-hc_oauth"


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=1024)


class EmailIn(BaseModel):
    email: str = Field(max_length=254)


class ResetIn(BaseModel):
    token: str = Field(max_length=256)
    new_password: str = Field(max_length=1024)


class ChangeIn(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


class InviteIn(BaseModel):
    email: str = Field(max_length=254)
    role: str = "streamer"
    streamer_id: str | None = None


class AcceptIn(BaseModel):
    token: str = Field(max_length=256)
    password: str = Field(max_length=1024)
    username: str | None = Field(default=None, max_length=50)


def _signed_in(response: Response, request: Request, user, issued) -> dict:
    set_session_cookie(response, auth_service(request), issued.token)
    return {"user": public_user(user), "csrf_token": issued.csrf_token}


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict:
    svc = auth_service(request)
    if not svc.origin_allowed(request.headers.get("origin")):  # login CSRF protection
        raise AuthError(403, "Request origin not allowed", "bad_origin")
    user, issued = await svc.login(body.email, body.password, client_ip(request), request.headers.get("user-agent"))
    return _signed_in(response, request, user, issued)


@router.get("/session")
async def session(request: Request, current: CurrentUser | None = Depends(optional_user)) -> dict:
    if current is None:
        raise AuthError(401, "Not signed in", "no_session")
    return {"user": public_user(current.user), "csrf_token": auth_service(request).csrf_token(current.session)}


@router.post("/logout")
async def logout(request: Request, response: Response, current: CurrentUser = Depends(require_user)) -> dict:
    svc = auth_service(request)
    await svc.logout(current.session)
    clear_session_cookie(response, svc)
    return {"success": True}


@router.post("/logout-all")
async def logout_all(request: Request, response: Response, current: CurrentUser = Depends(require_user)) -> dict:
    svc = auth_service(request)
    revoked = await svc.logout_all(current.user)
    clear_session_cookie(response, svc)
    return {"success": True, "sessions_revoked": revoked}


@router.post("/password/forgot", status_code=202)
async def forgot(body: EmailIn, request: Request) -> dict:
    await auth_service(request).request_reset(body.email, client_ip(request))
    return {"message": "If an account exists for this email, a reset link has been sent."}


@router.post("/password/reset")
async def reset(body: ResetIn, request: Request) -> dict:
    await auth_service(request).reset_password(body.token, body.new_password, client_ip(request))
    return {"message": "Password has been reset. You can now sign in with your new password."}


@router.post("/password/change")
async def change(body: ChangeIn, request: Request, current: CurrentUser = Depends(require_user)) -> dict:
    await auth_service(request).change_password(current.user, current.session, body.current_password,
                                                body.new_password, client_ip(request))
    return {"success": True}


@router.post("/invites", status_code=201)
async def create_invite(body: InviteIn, request: Request, current: CurrentUser = Depends(require_admin)) -> dict:
    return await auth_service(request).create_invite(current.user, body.email, body.role, body.streamer_id)


@router.post("/invites/accept")
async def accept_invite(body: AcceptIn, request: Request, response: Response) -> dict:
    svc = auth_service(request)
    if not svc.origin_allowed(request.headers.get("origin")):
        raise AuthError(403, "Request origin not allowed", "bad_origin")
    user, issued = await svc.accept_invite(body.token, body.password, body.username, client_ip(request),
                                           request.headers.get("user-agent"))
    return _signed_in(response, request, user, issued)


def _safe_redirect(path: str | None) -> str:
    return path if path and path.startswith("/") and not path.startswith("//") and "\\" not in path else "/dashboard"


@router.get("/google/start")
async def google_start(request: Request, redirect: str | None = None) -> RedirectResponse:
    svc = auth_service(request)
    if svc.google is None:
        raise AuthError(404, "Google sign-in is not enabled", "google_disabled")
    state, nonce, verifier = new_token(24), new_token(24), new_token(48)
    cookie = sign_payload(svc.secret, {"s": state, "n": nonce, "v": verifier, "r": _safe_redirect(redirect)}, 600)
    resp = RedirectResponse(svc.google.authorization_url(state, nonce, verifier), status_code=302)
    resp.set_cookie(OAUTH_COOKIE, cookie, max_age=600, path="/", secure=True, httponly=True, samesite="lax")
    return resp


@router.get("/google/callback")
async def google_callback(request: Request, code: str | None = None, state: str | None = None,
                          error: str | None = None) -> RedirectResponse:
    svc = auth_service(request)
    front = svc.settings.frontend_url.rstrip("/")
    payload = verify_payload(svc.secret, request.cookies.get(OAUTH_COOKIE))

    def fail(reason: str) -> RedirectResponse:
        resp = RedirectResponse(f"{front}/auth?error={quote(reason)}", status_code=302)
        resp.delete_cookie(OAUTH_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return resp

    if error or not code:
        return fail("google_cancelled")
    if payload is None or not state or payload.get("s") != state:
        return fail("invalid_state")
    try:
        user, issued = await svc.google_login(code, payload["v"], payload["n"], client_ip(request),
                                              request.headers.get("user-agent"))
    except AuthError as exc:
        return fail(exc.reason)
    resp = RedirectResponse(f"{front}{payload.get('r', '/dashboard')}", status_code=302)
    set_session_cookie(resp, svc, issued.token)
    resp.delete_cookie(OAUTH_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return resp

