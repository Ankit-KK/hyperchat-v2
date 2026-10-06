"""CORS middleware for the v2 frontend.

Only the configured browser origins (Settings.browser_origins: the frontend's origin plus
HYPERCHAT_ALLOWED_ORIGINS) are echoed, with credentials; any other origin gets no Allow-Origin header
and the browser rejects the response. Preflight requests are answered directly with 200 "ok".
Browsers only ever send the CSRF header (cookie sessions), an OBS overlay token or a donor upload
grant; signed server-to-server requests do not go through CORS.
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

ALLOWED_HEADERS = ", ".join(["content-type", "x-csrf-token", "x-obs-token", "x-upload-grant"])


def cors_headers(origin: str, allowed_origins: frozenset[str]) -> dict[str, str]:
    headers = {
        "Vary": "Origin",
        "Access-Control-Allow-Headers": ALLOWED_HEADERS,
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS",
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Max-Age": "86400",
    }
    if origin and origin in allowed_origins:
        headers["Access-Control-Allow-Origin"] = origin
    return headers


class CorsMiddleware:
    def __init__(self, app: ASGIApp, allowed_origins: frozenset[str]) -> None:
        self.app = app
        self.allowed_origins = frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        origin = ""
        for name, value in scope.get("headers", []):
            if name == b"origin":
                origin = value.decode("latin-1")
                break
        extra = cors_headers(origin, self.allowed_origins)

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
