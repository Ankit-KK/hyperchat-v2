"""Didit v3 (https://docs.didit.me): session API client and webhook signature verification (KYC PR3).

Sessions: POST {base}/v3/session/ with x-api-key; body {workflow_id, vendor_data, callback}. Didit returns the
unfinished session of the same vendor_data instead of a new one, so a retry after a lost response (or a failed local
write) gets the same session back. Decision: GET {base}/v3/session/{id}/decision/.

Webhooks (docs.didit.me/integration/webhooks): X-Signature-V2 = hex HMAC-SHA256(secret, canonical JSON of the WHOLE
body), canonical = whole-valued floats as integers (recursively), keys sorted, arrays in order, compact separators,
Unicode kept (ensure_ascii=False). X-Signature (raw bytes) and the deprecated X-Signature-Simple (envelope only) are
never used. Freshness: the docs check the X-Timestamp header (abs(now - X-Timestamp) <= 300 s); that header is not
signed, so the signed body `timestamp` (refreshed on every retry) is the authority and must be fresh too; both are
required. Equality of the two is not documented, so it is not required (each must be within the window).
"""

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

FRESHNESS_SECONDS = 300
STATUSES = frozenset({"Not Started", "In Progress", "Awaiting User", "In Review", "Approved", "Declined", "Resubmitted",
                      "Abandoned", "Expired", "Kyc Expired"})
CHECKS = ("id_verifications", "liveness_checks", "face_matches", "aml_screenings")  # statuses only are kept
_HEX = re.compile(r"^[0-9a-f]{64}$")
_URL_HOST = re.compile(r"^([a-z0-9-]+\.)*didit\.me$")


class DiditError(Exception):
    """A provider failure, by reason code only (never a key, body or URL)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _shorten(value):
    if isinstance(value, dict):
        return {k: _shorten(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shorten(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def canonical(body: dict) -> bytes:
    return json.dumps(_shorten(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(body: dict, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical(body), hashlib.sha256).hexdigest()


def signature_valid(body: dict, presented: str | None, secret: str) -> bool:
    presented = (presented or "").strip().lower()
    return bool(_HEX.match(presented)) and hmac.compare_digest(presented, sign(body, secret))


def fresh(value, now: float) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and abs(now - value) <= FRESHNESS_SECONDS


def as_uuid(value) -> str | None:
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def check_statuses(decision) -> dict:
    """{check: [statuses]} for the four identity checks; never any field but `status`."""
    out = {}
    if isinstance(decision, dict):
        for name in CHECKS:
            items = decision.get(name)
            if isinstance(items, list):
                out[name] = [i["status"] for i in items
                             if isinstance(i, dict) and isinstance(i.get("status"), str) and len(i["status"]) <= 32]
    return out


def hosted_url_ok(url) -> bool:
    if not isinstance(url, str) or len(url) > 2048:
        return False
    parts = urlsplit(url)
    return (parts.scheme == "https" and bool(_URL_HOST.match(parts.hostname or "")) and parts.username is None
            and parts.password is None and parts.port in (None, 443))


@dataclass(frozen=True)
class CreatedSession:
    session_id: str
    url: str
    status: str
    workflow_version: int | None


class DiditClient:
    def __init__(self, *, base: str, api_key: str, workflow_id: str, timeout: float = 10.0) -> None:
        self._base, self._key, self.workflow_id = base.rstrip("/"), api_key, workflow_id
        self._client = httpx.AsyncClient(timeout=timeout)

    async def _call(self, method: str, path: str, **kw) -> dict:
        try:
            resp = await self._client.request(method, self._base + path, headers={"x-api-key": self._key}, **kw)
        except httpx.TimeoutException:
            raise DiditError("timeout") from None
        except httpx.HTTPError:
            raise DiditError("network") from None
        if resp.status_code in (401, 403):
            raise DiditError("credentials")
        if resp.status_code == 429:
            raise DiditError("rate_limited")
        if resp.status_code >= 400:
            raise DiditError(f"http_{resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise DiditError("bad_response") from None
        if not isinstance(data, dict):
            raise DiditError("bad_response")
        return data

    async def create_session(self, vendor_data: str, callback: str) -> CreatedSession:
        data = await self._call("POST", "/v3/session/", json={"workflow_id": self.workflow_id,
                                                               "vendor_data": vendor_data, "callback": callback})
        session_id, version = as_uuid(data.get("session_id")), data.get("workflow_version")
        if (session_id is None or data.get("vendor_data") != vendor_data or as_uuid(data.get("workflow_id")) !=
                self.workflow_id or data.get("status") not in STATUSES or not hosted_url_ok(data.get("url"))):
            raise DiditError("unexpected_response")
        return CreatedSession(session_id, data["url"], data["status"], version if isinstance(version, int) else None)

    async def decision(self, session_id: str) -> dict:
        data = await self._call("GET", f"/v3/session/{session_id}/decision/")
        if as_uuid(data.get("session_id")) != session_id or data.get("status") not in STATUSES:
            raise DiditError("unexpected_response")
        return data
