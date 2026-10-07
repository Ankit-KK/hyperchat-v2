"""Pusher apps, signing and the REST client.

Apps: one per streamers.pusher_group (HYPERCHAT_PUSHER_*_{1..4}); a creator whose group is not configured
uses group 1 (D6). Publishing and the config endpoint use the same resolver, so a client always connects to
the app that receives its events.

REST (https://pusher.com/docs/channels/library_auth_reference/rest-api/): POST /apps/{id}/events with
auth_key, auth_timestamp, auth_version=1.0, body_md5 and auth_signature = HMAC-SHA256(secret,
"POST\\n/apps/{id}/events\\n<sorted query>"). Private-channel auth: "<key>:" + HMAC-SHA256(secret,
"<socket_id>:<channel>").

The secret only ever signs; it is never logged. Response bodies are never logged or stored either: errors
become short reason codes (app.jobs.retry).
"""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from app.core.config import PUSHER_GROUPS, Settings
from app.jobs.retry import Permanent, Retryable

MAX_DATA_BYTES = 10_000  # Pusher's limit for one event's data
TIMEOUT = httpx.Timeout(5.0)


@dataclass(frozen=True)
class PusherApp:
    group: int
    app_id: str
    key: str
    secret: str = field(repr=False)
    cluster: str

    @property
    def host(self) -> str:
        return f"api-{self.cluster}.pusher.com"


class PusherApps:
    def __init__(self, apps: dict[int, PusherApp]) -> None:
        self._apps = apps

    @classmethod
    def from_settings(cls, settings: Settings) -> "PusherApps":
        apps = {}
        for group in PUSHER_GROUPS:
            values = settings.pusher_group(group)
            if values is not None:
                apps[group] = PusherApp(group, *values)
        return cls(apps)

    def resolve(self, group: int | None) -> PusherApp | None:
        """The creator's group app, else group 1, else None (realtime unavailable for this creator)."""
        return self._apps.get(group or 1) or self._apps.get(1)

    @property
    def groups(self) -> list[int]:
        return sorted(self._apps)


def _hmac(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def channel_auth(app: PusherApp, socket_id: str, channel: str) -> str:
    """The "auth" value of a private-channel authorization response."""
    return f"{app.key}:{_hmac(app.secret, f'{socket_id}:{channel}')}"


def signed_query(app: PusherApp, method: str, path: str, body: bytes, timestamp: int) -> dict[str, str]:
    params = {"auth_key": app.key, "auth_timestamp": str(timestamp), "auth_version": "1.0",
              "body_md5": hashlib.md5(body).hexdigest()}  # noqa: S324 - required by the Pusher protocol
    query = "&".join(f"{k}={params[k]}" for k in sorted(params))
    params["auth_signature"] = _hmac(app.secret, f"{method}\n{path}\n{query}")
    return params


def event_body(channel: str, event: str, data: dict) -> bytes:
    encoded = json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)
    if len(encoded.encode()) > MAX_DATA_BYTES:
        raise Permanent("payload_too_large")
    return json.dumps({"name": event, "channels": [channel], "data": encoded}, separators=(",", ":")).encode()


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers.get("retry-after", ""))
    except ValueError:
        return None


class PusherClient:
    """Publishes one event to one channel. Raises Retryable / Permanent with reason codes only."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, clock=time.time) -> None:
        self._transport, self._clock = transport, clock

    async def publish(self, app: PusherApp, channel: str, event: str, data: dict) -> None:
        body = event_body(channel, event, data)
        path = f"/apps/{app.app_id}/events"
        query = signed_query(app, "POST", path, body, int(self._clock()))
        url = f"https://{app.host}{path}?{urlencode(query)}"
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=TIMEOUT, follow_redirects=False) as client:
                response = await client.post(url, content=body, headers={"content-type": "application/json"})
        except httpx.TimeoutException:
            raise Retryable("pusher_timeout") from None
        except httpx.HTTPError:
            raise Retryable("pusher_network") from None
        status = response.status_code
        if 200 <= status < 300:
            return
        if status == 429:
            raise Retryable("pusher_rate_limited", _retry_after(response))
        if status >= 500:
            raise Retryable("pusher_server_error", _retry_after(response))
        if status == 401:
            raise Permanent("pusher_auth_failed")
        if status == 403:
            raise Permanent("pusher_forbidden")  # e.g. app disabled or over its message quota
        if status == 413:
            raise Permanent("payload_too_large")
        raise Permanent("pusher_rejected")
