"""Google OAuth 2.0 / OpenID Connect (authorization code + PKCE + state + nonce).

Sign-in only: accounts are invite-only, so a Google identity is linked to an EXISTING user, first by
Google subject (identities imported from Supabase Auth at cutover), then by verified email (never for an
administrator: S2, migration 0026).

The device authorization flow (RFC 8628) is used ONLY by the operator tool (app.stepup.operator link-google), with the
operator's own OAuth client, to verify independently of the API which Google account an administrator controls.
"""

import asyncio

import base64
import hashlib
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEVICE_TOKEN_MAX_AGE = 600  # seconds: a device-flow ID token must have been issued during this ceremony
ISSUERS = ("https://accounts.google.com", "accounts.google.com")


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str
    email_verified: bool


class GoogleAuthError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class GoogleOAuth:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str,
                 client: httpx.AsyncClient | None = None, jwks_cache_seconds: int = 3600) -> None:
        self.client_id = client_id
        self._secret = client_secret
        self.redirect_uri = redirect_uri
        self._http = client or httpx.AsyncClient(timeout=10.0)
        self._jwks: dict | None = None
        self._jwks_at = 0.0
        self._jwks_ttl = jwks_cache_seconds

    def authorization_url(self, state: str, nonce: str, code_verifier: str) -> str:
        return AUTH_URL + "?" + urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri, "response_type": "code",
            "scope": "openid email profile", "state": state, "nonce": nonce,
            "code_challenge": pkce_challenge(code_verifier), "code_challenge_method": "S256",
            "prompt": "select_account",
        })

    async def exchange(self, code: str, code_verifier: str, nonce: str) -> GoogleIdentity:
        try:
            resp = await self._http.post(TOKEN_URL, data={
                "code": code, "client_id": self.client_id, "client_secret": self._secret,
                "redirect_uri": self.redirect_uri, "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            })
        except httpx.HTTPError:
            raise GoogleAuthError("token_endpoint_unreachable") from None
        if resp.status_code != 200:
            raise GoogleAuthError(f"token_endpoint_status_{resp.status_code}")
        id_token = (resp.json() or {}).get("id_token")
        if not id_token:
            raise GoogleAuthError("no_id_token")
        return await self.verify_id_token(id_token, nonce)

    async def verify_id_token(self, id_token: str, nonce: str) -> GoogleIdentity:
        return await self._verify(id_token, nonce=nonce)

    # ── device authorization flow (operator tool only) ─────────────────────────────────────────────────────
    async def device_start(self) -> dict:
        """{device_code, user_code, verification_url, interval, expires_in} from Google."""
        try:
            resp = await self._http.post(DEVICE_CODE_URL, data={"client_id": self.client_id, "scope": "openid email"})
        except httpx.HTTPError:
            raise GoogleAuthError("device_endpoint_unreachable") from None
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code != 200 or not body.get("device_code") or not body.get("user_code"):
            raise GoogleAuthError(f"device_endpoint_status_{resp.status_code}")
        return {"device_code": body["device_code"], "user_code": body["user_code"],
                "verification_url": body.get("verification_url") or body.get("verification_uri"),
                "interval": int(body.get("interval", 5)), "expires_in": int(body.get("expires_in", 600))}

    async def device_poll(self, device_code: str, interval: int, expires_in: int, sleep=asyncio.sleep,
                          clock=time.monotonic) -> GoogleIdentity:
        """Poll Google's token endpoint until the person completes the sign-in on their own device; the ID token
        comes straight from Google over TLS and is still fully verified (signature, issuer, THIS client as audience,
        expiry, freshness, verified email)."""
        deadline = clock() + expires_in
        while clock() < deadline:
            await sleep(interval)
            try:
                resp = await self._http.post(TOKEN_URL, data={
                    "client_id": self.client_id, "client_secret": self._secret, "device_code": device_code,
                    "grant_type": DEVICE_GRANT})
            except httpx.HTTPError:
                raise GoogleAuthError("token_endpoint_unreachable") from None
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code == 200:
                if not body.get("id_token"):
                    raise GoogleAuthError("no_id_token")
                return await self._verify(body["id_token"], nonce=None, max_age=DEVICE_TOKEN_MAX_AGE)
            error = body.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            raise GoogleAuthError(error or f"token_endpoint_status_{resp.status_code}")
        raise GoogleAuthError("device_flow_expired")

    async def _verify(self, id_token: str, *, nonce: str | None, max_age: int | None = None) -> GoogleIdentity:
        try:
            kid = jwt.get_unverified_header(id_token).get("kid")
        except jwt.PyJWTError:
            raise GoogleAuthError("malformed_id_token") from None
        key = await self._key(kid)
        try:
            claims = jwt.decode(id_token, key, algorithms=["RS256"], audience=self.client_id,
                                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.ExpiredSignatureError:
            raise GoogleAuthError("id_token_expired") from None
        except jwt.PyJWTError:
            raise GoogleAuthError("invalid_id_token") from None
        if claims.get("iss") not in ISSUERS:
            raise GoogleAuthError("bad_issuer")
        if nonce is not None and claims.get("nonce") != nonce:
            raise GoogleAuthError("nonce_mismatch")
        if max_age is not None and claims["iat"] < time.time() - max_age:
            raise GoogleAuthError("id_token_too_old")
        email = claims.get("email")
        if not email or claims.get("email_verified") is not True:
            raise GoogleAuthError("email_not_verified")
        return GoogleIdentity(subject=claims["sub"], email=email, email_verified=True)

    async def _key(self, kid: str | None):
        for force in (False, True):
            if force or self._jwks is None or time.monotonic() - self._jwks_at > self._jwks_ttl:
                try:
                    resp = await self._http.get(JWKS_URL)
                    resp.raise_for_status()
                    self._jwks, self._jwks_at = resp.json(), time.monotonic()
                except (httpx.HTTPError, ValueError):
                    if self._jwks is None:
                        raise GoogleAuthError("jwks_unavailable") from None
            for k in jwt.PyJWKSet.from_dict(self._jwks).keys:
                if k.key_id == kid:
                    return k.key
        raise GoogleAuthError("unknown_signing_key")
