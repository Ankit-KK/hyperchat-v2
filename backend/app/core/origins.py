"""Browser origins the API trusts (CORS with credentials, login/CSRF Origin check, OBS overlays).

The set is explicit: the frontend's origin plus HYPERCHAT_ALLOWED_ORIGINS (comma-separated exact
origins such as `http://localhost:5173`). No wildcards, paths or credentials. Plain http is only
accepted for loopback hosts, and never in production.
"""

from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})


class OriginError(ValueError):
    pass


def normalize_origin(value: str) -> str:
    raw = value.strip()
    if not raw or "*" in raw:
        raise OriginError(f"invalid origin {raw!r}: wildcards and empty values are not allowed")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise OriginError(f"invalid origin {raw!r}") from None
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise OriginError(f"invalid origin {raw!r}: expected scheme://host[:port]")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise OriginError(f"invalid origin {raw!r}: no path, query, fragment or credentials")
    if ":" in host:
        host = f"[{host}]"
    if parts.scheme == "http" and host not in LOOPBACK_HOSTS:
        raise OriginError(f"invalid origin {raw!r}: plain http is only allowed for localhost")
    default_port = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default_port else "")


def parse_origins(value: str | None) -> tuple[str, ...]:
    items = [v for v in (value or "").replace("\n", ",").split(",") if v.strip()]
    return tuple(dict.fromkeys(normalize_origin(v) for v in items))


def is_development_origin(origin: str) -> bool:
    """Plain http or a loopback host: acceptable in development only."""
    parts = urlsplit(origin)
    return parts.scheme == "http" or (parts.hostname or "") in {"localhost", "127.0.0.1", "::1"}
