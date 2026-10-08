"""Telegram Bot API and Discord API v10 clients, and Discord request verification.

Errors become app.jobs.retry Retryable / Permanent with short reason codes: never a response body (it can echo donor
text), never the bot token (it is part of every Telegram URL; httpx request logging is capped at WARNING by
app.core.logging and exceptions are reported by type only).

  Retryable  timeouts, network errors, 429 (with the platform's retry_after), 5xx
  Permanent  401/404 (bad token / unknown chat), 403 (bot blocked, cannot DM, missing access), other 4xx
"""

import json
import time

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.jobs.retry import Permanent, Retryable

TIMEOUT = httpx.Timeout(5.0)
DISCORD_API = "https://discord.com/api/v10"
DISCORD_MAX_SKEW_SECONDS = 300


def _retry_after(response: httpx.Response, body: dict | None, key: str) -> float | None:
    value = None
    if isinstance(body, dict):
        value = (body.get("parameters") or {}).get("retry_after") if key == "telegram" else body.get("retry_after")
    if value is None:
        value = response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _classify(platform: str, response: httpx.Response) -> None:
    status = response.status_code
    if 200 <= status < 300:
        return
    try:
        body = response.json()
    except ValueError:
        body = None
    if status == 429:
        raise Retryable(f"{platform}_rate_limited", _retry_after(response, body, platform))
    if status >= 500:
        raise Retryable(f"{platform}_server_error")
    if status in (401, 404):
        raise Permanent(f"{platform}_unauthorized" if status == 401 else f"{platform}_not_found")
    if status == 403:
        raise Permanent(f"{platform}_forbidden")
    raise Permanent(f"{platform}_rejected")


async def _send(platform: str, transport, method: str, url: str, **kwargs) -> httpx.Response:
    try:
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT, follow_redirects=False) as client:
            response = await client.request(method, url, **kwargs)
    except httpx.TimeoutException:
        raise Retryable(f"{platform}_timeout") from None
    except httpx.HTTPError:
        raise Retryable(f"{platform}_network") from None
    _classify(platform, response)
    return response


class TelegramClient:
    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._token, self._transport = token, transport

    def __repr__(self) -> str:  # never the token
        return "TelegramClient()"

    async def call(self, method: str, payload: dict) -> dict:
        response = await _send("telegram", self._transport, "POST",
                               f"https://api.telegram.org/bot{self._token}/{method}", json=payload)
        try:
            body = response.json()
        except ValueError:
            raise Retryable("telegram_malformed") from None
        if not isinstance(body, dict) or not body.get("ok"):
            raise Permanent("telegram_rejected")
        return body.get("result")

    async def send_message(self, chat_id: str, text: str, keyboard: list | None = None, *, html: bool = True) -> str:
        payload = {"chat_id": int(chat_id), "text": text, "disable_web_page_preview": True}
        if html:
            payload["parse_mode"] = "HTML"
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        result = await self.call("sendMessage", payload)
        return str(result.get("message_id", "")) if isinstance(result, dict) else ""

    async def edit_message_text(self, chat_id: str, message_id: int, text: str) -> None:
        """Plain text (no parse_mode): the original message text comes back from Telegram without its markup."""
        await self.call("editMessageText", {"chat_id": int(chat_id), "message_id": int(message_id), "text": text[:4096],
                                            "disable_web_page_preview": True})

    async def get_me(self) -> dict:
        return await self.call("getMe", {})

    async def set_webhook(self, url: str, secret: str) -> None:
        await self.call("setWebhook", {"url": url, "secret_token": secret, "drop_pending_updates": True,
                                       "allowed_updates": ["message", "callback_query"]})


class DiscordClient:
    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._token, self._transport = token, transport

    def __repr__(self) -> str:
        return "DiscordClient()"

    async def call(self, method: str, path: str, payload: dict | None = None) -> dict:
        response = await _send("discord", self._transport, method, f"{DISCORD_API}{path}", json=payload,
                               headers={"Authorization": f"Bot {self._token}", "User-Agent": "HyperChat (v2, 1.0)"})
        try:
            return response.json() if response.content else {}
        except ValueError:
            raise Retryable("discord_malformed") from None

    async def create_message(self, channel_id: str, payload: dict) -> str:
        return str((await self.call("POST", f"/channels/{int(channel_id)}/messages", payload)).get("id", ""))

    async def open_dm(self, user_id: str) -> str:
        return str((await self.call("POST", "/users/@me/channels", {"recipient_id": str(int(user_id))})).get("id", ""))

    async def get_application(self) -> dict:
        return await self.call("GET", "/applications/@me")

    async def set_interactions_endpoint(self, url: str) -> None:
        await self.call("PATCH", "/applications/@me", {"interactions_endpoint_url": url})


def verify_discord(public_key_hex: str, signature_hex: str | None, timestamp: str | None, body: bytes,
                   now: float | None = None) -> bool:
    """Discord's Ed25519 signature over timestamp + body, and a timestamp at most 5 minutes off (replays)."""
    if not signature_hex or not timestamp:
        return False
    try:
        if abs((now if now is not None else time.time()) - int(timestamp)) > DISCORD_MAX_SKEW_SECONDS:
            return False
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        key.verify(bytes.fromhex(signature_hex), timestamp.encode() + body)
        return True
    except (ValueError, InvalidSignature):
        return False


def parse_json(body: bytes) -> dict | None:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None
