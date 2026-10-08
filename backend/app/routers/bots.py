"""Moderation bot webhooks (Phase 11). See app.bots.

  POST /api/v1/webhooks/telegram   X-Telegram-Bot-Api-Secret-Token; updates (commands, button presses)
  POST /api/v1/webhooks/discord    Ed25519-signed interactions (PING, button presses)

Both answer 503 while bots are disabled or not configured (no webhook is registered then); an unauthenticated
request gets 401 and a security event (reason code only). Responses are never cached.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.security.audit import security_event

router = APIRouter(prefix="/api/v1/webhooks", tags=["bots"])
NO_STORE = {"Cache-Control": "no-store"}


def _reply(status: int, body: dict | None) -> Response:
    if body is None:
        return Response(status_code=status, headers=NO_STORE)
    return JSONResponse(body, status_code=status, headers=NO_STORE)


@router.post("/telegram")
async def telegram_webhook(request: Request):
    bot = getattr(request.app.state, "telegram_bot", None)
    if bot is None:
        return _reply(503, {"error": "Bots are not available"})
    if not bot.authentic(request.headers.get("x-telegram-bot-api-secret-token")):
        security_event("bot_webhook_rejected", reason="bad_telegram_secret", path=request.url.path)
        return _reply(401, {"error": "Unauthorized"})
    status, body = await bot.handle(await request.body())
    return _reply(status, body)


@router.post("/discord")
async def discord_webhook(request: Request):
    bot = getattr(request.app.state, "discord_bot", None)
    if bot is None:
        return _reply(503, {"error": "Bots are not available"})
    status, body = await bot.handle(await request.body(), request.headers.get("x-signature-ed25519"),
                                    request.headers.get("x-signature-timestamp"))
    if status == 401:
        security_event("bot_webhook_rejected", reason="bad_discord_signature", path=request.url.path)
        return _reply(401, {"error": "Invalid request signature"})
    return _reply(status, body)
