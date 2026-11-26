"""CORS middleware for the v2 frontend.

Origins come in tiers (PDP v2.0 G1, docs/admin-auth-boundary.md "Origin tiers"):
- credentialed (Allow-Origin echoed with Allow-Credentials): the session and admin tiers on ordinary paths, and
  only the admin tier on the admin paths (/api/v1/admin/*, /api/v1/auth/mfa/*);
- public (Allow-Origin echoed WITHOUT credentials): the donor and overlay tiers, on ordinary paths only;
- any other origin gets no Allow-Origin header and the browser rejects the response.
With no tier configured every tier is Settings.browser_origins (the frontend's origin plus HYPERCHAT_ALLOWED_ORIGINS),
so every trusted origin is credentialed everywhere, exactly as before the tiers. Preflight requests are answered
directly with 200 "ok". Browsers only ever send the CSRF header (cookie sessions), an OBS overlay token, a donor
upload grant or a donation status token; signed server-to-server requests do not go through CORS.
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

ALLOWED_HEADERS = ", ".join(["content-type", "x-csrf-token", "x-obs-token", "x-upload-grant", "x-status-token"])
ADMIN_PATHS = ("/api/v1/admin", "/api/v1/auth/mfa")


def is_admin_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in ADMIN_PATHS)


def cors_headers(origin: str, allowed_origins: frozenset[str],
                 public_origins: frozenset[str] = frozenset()) -> dict[str, str]:
    headers = {
        "Vary": "Origin",
        "Access-Control-Allow-Headers": ALLOWED_HEADERS,
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS",
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Max-Age": "86400",
    }
    if origin and origin in allowed_origins:
        headers["Access-Control-Allow-Origin"] = origin
    elif origin and origin in public_origins:
        headers["Access-Control-Allow-Origin"] = origin
        del headers["Access-Control-Allow-Credentials"]  # the browser then sends and exposes no cookie
    return headers


class CorsMiddleware:
    def __init__(self, app: ASGIApp, allowed_origins: frozenset[str], admin_origins: frozenset[str] | None = None,
                 public_origins: frozenset[str] = frozenset()) -> None:
        self.app = app
        self.allowed_origins = frozenset(allowed_origins)
        self.admin_origins = self.allowed_origins if admin_origins is None else frozenset(admin_origins)
        self.public_origins = frozenset(public_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        origin = ""
        for name, value in scope.get("headers", []):
            if name == b"origin":
                origin = value.decode("latin-1")
                break
        if is_admin_path(scope.get("path", "")):
            extra = cors_headers(origin, self.admin_origins)
        else:
            extra = cors_headers(origin, self.allowed_origins, self.public_origins)

        if scope["method"] == "OPTIONS":
            body = b"ok"
            headers = [(k.lower().encode(), v.encode("latin-1")) for k, v in extra.items()]
            headers += [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]
            await send({"type": "http.response.start", "status": 200, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for k, v in extra.items():
                    headers[k] = v
            await send(message)

        await self.app(scope, receive, send_with_cors)
