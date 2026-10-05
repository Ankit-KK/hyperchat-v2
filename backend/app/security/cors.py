"""CORS middleware for the v2 frontend.

The origin is echoed only when allowlisted; otherwise the Allow-Origin header is omitted so
browsers reject the response. Preflight requests are answered directly with 200 "ok".
Browsers only ever send the CSRF header (cookie sessions) or an OBS overlay token; signed
server-to-server requests do not go through CORS.
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

STATIC_ALLOWED_ORIGINS = frozenset({
    "https://hyperchat.space",
    "https://www.hyperchat.space",
    # Local frontend development servers (Vite).
    "http://localhost:8080",
    "http://localhost:5173",
})

ALLOWED_HEADERS = ", ".join(["content-type", "x-csrf-token", "x-obs-token"])


def is_allowed_origin(origin: str) -> bool:
    return bool(origin) and origin in STATIC_ALLOWED_ORIGINS


def cors_headers(origin: str, extra_origins: tuple[str, ...] = ()) -> dict[str, str]:
    headers = {
        "Vary": "Origin",
        "Access-Control-Allow-Headers": ALLOWED_HEADERS,
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS",
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Max-Age": "86400",
    }
    if is_allowed_origin(origin) or origin in extra_origins:
        headers["Access-Control-Allow-Origin"] = origin
    return headers


class CorsMiddleware:
    def __init__(self, app: ASGIApp, extra_origins: tuple[str, ...] = ()) -> None:
        self.app = app
        self.extra_origins = tuple(o for o in extra_origins if o)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        origin = ""
        for name, value in scope.get("headers", []):
            if name == b"origin":
                origin = value.decode("latin-1")
                break
        extra = cors_headers(origin, self.extra_origins)

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
