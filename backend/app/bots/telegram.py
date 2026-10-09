"""The Telegram webhook (POST /api/v1/webhooks/telegram), Phase 11.

Authentication (D3): X-Telegram-Bot-Api-Secret-Token must equal HYPERCHAT_TELEGRAM_WEBHOOK_SECRET (constant time;
set with setWebhook secret_token by the CLI). Each update_id is processed once (request_nonces "tg:<update_id>").

Button presses (D4, D9): callback_data "m:<16-character id>" -> the stored row decides the action, donation and
creator (never the client); the presser must be the recipient the buttons were sent to, an active moderator of that
creator, Telegram moderation must be on and the moderator's role / permissions must allow the action. The action
runs through the shared moderation core (DashboardRepository.moderate with a BotActor: no HTTP hop) and the answer
reports the real result. Legacy callback formats are refused.

Commands (D10, private chats only, 20 per minute per user): /start, /pending, /recent, /status, /settings across
every creator the user moderates; /stats shows the pending count and the user's own action count, never revenue.

Outside production nothing is sent to a chat that is not allow-listed (D13), and only allow-listed creators
(HYPERCHAT_BOT_CREATOR_ALLOWLIST) are listed by commands or moderated by buttons. Logs: update kind and reason codes;
user ids only as short hashes; never update bodies, donor text, the token or the secret.
"""

import hmac
import logging
import re
from datetime import UTC, datetime, timedelta

from app.bots import format as fmt
from app.bots.clients import TelegramClient, parse_json
from app.bots.notify import allowed, creator_allowed
from app.bots.permissions import BOT_ACTIONS, can
from app.bots.repository import MAX_ASSIGNMENTS, BotRepository
from app.dashboard.moderation import NotPaid
from app.dashboard.repository import BotActor, DashboardRepository
from app.jobs.retry import JobError
from app.security.crypto import sha256_hex
from app.security.nonce_store import NonceReplayError, NonceStore, NonceStoreError

log = logging.getLogger("hyperchat.bots")
MAX_BODY = 256 * 1024
_CALLBACK = re.compile(r"m:([A-Za-z0-9]{16})")
RATE_COMMAND = ("bot:telegram:command", 20, 60)
RATE_CALLBACK = ("bot:telegram:callback", 60, 60)
HELP = ("🎮 <b>HyperChat Moderation Bot</b>\n\nReal-time donation moderation &amp; notifications.\n\n<b>Commands:</b>\n"
        "/pending - View pending donations\n/recent - View recent donations\n/stats - Pending and your actions\n"
        "/status - Check your moderator status\n/settings - View moderation settings")


def user_hash(user_id: str) -> str:
    return sha256_hex(f"telegram:{user_id}")[:12]


class TelegramBot:
    def __init__(self, *, secret: str, client: TelegramClient, repo: BotRepository, moderation: DashboardRepository,
                 nonces: NonceStore, limiter, recipients: frozenset[str] | None, frontend_url: str,
                 public_base: str | None, creators: frozenset[str] | None = None) -> None:
        self.secret, self.client, self.repo, self.moderation = secret, client, repo, moderation
        self.nonces, self.limiter, self.recipients, self.creators = nonces, limiter, recipients, creators
        self.frontend_url, self.public_base = frontend_url, public_base

    def authentic(self, presented: str | None) -> bool:
        return bool(presented) and hmac.compare_digest(presented.encode(), self.secret.encode())

    async def handle(self, body: bytes) -> tuple[int, dict | None]:
        """(HTTP status, JSON body). A JSON body may carry one Bot API method call (answerCallbackQuery)."""
        if len(body) > MAX_BODY:
            return 413, None
        update = parse_json(body)
        if update is None or not isinstance(update.get("update_id"), int):
            return 400, None
        try:
            await self.nonces.insert(f"tg:{update['update_id']}", None, datetime.now(UTC) + timedelta(days=2))
        except NonceReplayError:
            log.info("telegram update", extra={"kind": "duplicate"})
            return 200, None
        except NonceStoreError:
            return 503, None  # Telegram retries; nothing was processed
        if isinstance(update.get("callback_query"), dict):
            return 200, await self._callback(update["callback_query"])
        if isinstance(update.get("message"), dict):
            await self._message(update["message"])
            return 200, None
        log.info("telegram update", extra={"kind": "ignored"})
        return 200, None

    # ── button presses ──────────────────────────────────────────────────────
    @staticmethod
    def _answer(query_id: str, text: str) -> dict:
        return {"method": "answerCallbackQuery", "callback_query_id": str(query_id)[:64], "text": text[:200]}

    async def _callback(self, query: dict) -> dict | None:
        query_id = query.get("id")
        user_id = str((query.get("from") or {}).get("id", ""))
        message = query.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if not query_id or not user_id:
            return None
        if not await self.limiter.hit_rate_limit(f"tg:{user_hash(user_id)}", *RATE_CALLBACK):
            return self._answer(query_id, "⏳ Too many actions, try again in a minute")
        reason, answer = await self._decide(query, user_id, chat_id)
        log.info("telegram callback", extra={"kind": "callback", "reason": reason, "user": user_hash(user_id)})
        return self._answer(query_id, answer)

    async def _decide(self, query: dict, user_id: str, chat_id: str) -> tuple[str, str]:
        match = _CALLBACK.fullmatch(str(query.get("data") or ""))
        row = await self.repo.callback("telegram", match.group(1)) if match else None
        if row is None:
            return "unknown_callback", "❌ Invalid or expired action"
        if row["expired"]:
            return "expired", "❌ This action has expired. Please use the dashboard."
        if row["recipient_id"] != user_id or chat_id != user_id:
            return "not_recipient", "❌ This button was not sent to you"
        moderator = await self.repo.moderator_for("telegram", row["streamer_id"], user_id)
        if moderator is None:
            return "not_moderator", "❌ You are not a registered moderator for this streamer"
        if not creator_allowed(self.creators, moderator["streamer_slug"]):
            return "creator_not_allowlisted", "❌ Invalid or expired action"
        if not moderator["telegram_enabled"]:
            return "telegram_disabled", "❌ Telegram moderation is disabled"
        action = row["action_type"]
        if action not in BOT_ACTIONS or not can(moderator, action):
            return "not_permitted", "❌ No permission for this action"
        try:
            outcome = await self.moderation.moderate(
                row["streamer_id"], row["donation_id"], action, actor_user_id=None, actor_label=moderator["mod_name"],
                notes="Banned via Telegram" if action == "ban_donor" else None,
                bot=BotActor("telegram", moderator["id"], user_id))
        except NotPaid:
            return "not_paid", "❌ This donation is not paid"
        if outcome is None:
            return "donation_missing", "❌ Donation not found"
        _, step, _ = outcome
        line = fmt.result_line(action, step.result == "applied", moderator["mod_name"], step.new_status)
        await self._edit(query, line)
        return step.result, line

    async def _edit(self, query: dict, line: str) -> None:
        """Best effort: the clicked message gets the result and loses its buttons (plain text, no markup)."""
        message = query.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if not message.get("message_id") or not allowed(self.recipients, chat_id):
            return
        try:
            await self.client.edit_message_text(chat_id, message["message_id"], f"{message.get('text', '')}\n\n{line}")
        except JobError as exc:
            log.info("telegram edit failed", extra={"reason": exc.reason})

    # ── commands ────────────────────────────────────────────────────────────
    async def _say(self, chat_id: str, text: str, keyboard: list | None = None) -> None:
        if not allowed(self.recipients, chat_id):
            return
        try:
            await self.client.send_message(chat_id, text, keyboard)
        except JobError as exc:
            log.info("telegram send failed", extra={"reason": exc.reason})

    async def _message(self, message: dict) -> None:
        chat = message.get("chat") or {}
        user_id = str((message.get("from") or {}).get("id", ""))
        chat_id = str(chat.get("id", ""))
        command = str(message.get("text") or "").strip().split("@", 1)[0].split(" ", 1)[0].lower()
        if chat.get("type") != "private" or not user_id or chat_id != user_id or not command.startswith("/"):
            log.info("telegram update", extra={"kind": "message_ignored"})
            return
        if not allowed(self.recipients, chat_id):
            log.info("telegram update", extra={"kind": "command", "reason": "not_allowlisted",
                                               "user": user_hash(user_id)})
            return
        if not await self.limiter.hit_rate_limit(f"tg:{user_hash(user_id)}", *RATE_COMMAND):
            log.info("telegram update", extra={"kind": "command", "reason": "rate_limited", "user": user_hash(user_id)})
            return
        handler = {"/start": self._start, "/help": self._start, "/pending": self._pending, "/recent": self._recent,
                   "/stats": self._stats, "/status": self._status, "/settings": self._settings}.get(command)
        log.info("telegram update", extra={"kind": "command", "command": command[:20] if handler else "unknown",
                                           "user": user_hash(user_id)})
        await (handler or self._unknown)(chat_id, user_id)

    async def _start(self, chat_id: str, user_id: str) -> None:
        await self._say(chat_id, HELP)

    async def _unknown(self, chat_id: str, user_id: str) -> None:
        await self._say(chat_id, "❓ Unknown command.\n\n" + HELP.split("<b>Commands:</b>\n", 1)[1])

    def _scoped(self, rows: list[dict]) -> list[dict]:
        return [m for m in rows if creator_allowed(self.creators, m["streamer_slug"])]

    async def _creators(self, chat_id: str, user_id: str, *, active_only: bool = True) -> list[dict]:
        """Every creator the user moderates (allowlist applied in SQL and again here), by creator name. Past
        MAX_ASSIGNMENTS the user is told the list was cut: never a silent truncation."""
        rows = self._scoped(await self.repo.telegram_assignments(user_id, active_only=active_only,
                                                                 slugs=self.creators))
        if len(rows) > MAX_ASSIGNMENTS:
            await self._say(chat_id, f"⚠️ You moderate more than {MAX_ASSIGNMENTS} creators: showing the first "
                                     f"{MAX_ASSIGNMENTS}. Use the dashboard for the others.")
            rows = rows[:MAX_ASSIGNMENTS]
        return rows

    async def _assignments(self, chat_id: str, user_id: str) -> list[dict]:
        rows = await self._creators(chat_id, user_id)
        if not rows:
            await self._say(chat_id, "❌ You are not registered as a moderator.\n\nContact the streamer to add you.")
        return rows

    async def _pending(self, chat_id: str, user_id: str) -> None:
        total = 0
        for m in await self._assignments(chat_id, user_id):
            if not m["telegram_enabled"]:
                continue
            donations = await self.repo.pending_donations(m["streamer_id"])
            if donations:
                total += len(donations)
                await self._say(chat_id, f"📋 <b>{fmt.esc_html(m['streamer_name'])} - {len(donations)} Pending</b>")
            for d in donations:
                actions = {}
                for action in BOT_ACTIONS:
                    if (action != "hide_message" or d.get("message")) and can(m, action):
                        actions[action] = await self.repo.create_callback(
                            "telegram", donation_id=d["id"], streamer_id=d["streamer_id"], action=action,
                            recipient_id=user_id, moderator_id=m["id"])
                await self._say(chat_id, fmt.telegram_text(d), fmt.telegram_keyboard(
                    actions, fmt.links(d, self.public_base, self.frontend_url)))
        if total == 0:
            await self._say(chat_id, "✅ No pending donations to moderate.")

    async def _recent(self, chat_id: str, user_id: str) -> None:
        for m in await self._assignments(chat_id, user_id):
            rows = await self.repo.recent_donations(m["streamer_id"])
            if not rows:
                await self._say(chat_id, f"📋 No recent donations for {fmt.esc_html(m['streamer_name'])}.")
                continue
            lines = [f"📊 <b>Recent Donations - {fmt.esc_html(m['streamer_name'])}</b>", ""]
            for d in rows:
                hidden = "" if d["message_visible"] else " 🙈"
                lines.append(f"💰 {fmt.esc_html(fmt.amount(d))} from <b>{fmt.esc_html(d['donor_name'])}</b>{hidden}")
                if d["message"] and d["message_visible"]:
                    lines.append(f"   💬 \"{fmt.esc_html(fmt.excerpt(d['message'], 50))}\"")
                lines.append(f"   ⏰ {fmt.esc_html(fmt.when(d['created_at']))}")
            await self._say(chat_id, "\n".join(lines))

    async def _stats(self, chat_id: str, user_id: str) -> None:
        for m in await self._assignments(chat_id, user_id):  # D10: no revenue, ever
            pending = await self.repo.pending_count(m["streamer_id"])
            await self._say(chat_id, f"📊 <b>Stats - {fmt.esc_html(m['streamer_name'])}</b>\n\n"
                                     f"⏳ Pending: {pending}\n🛡️ Your Actions: {m['total_actions']}")

    async def _status(self, chat_id: str, user_id: str) -> None:
        rows = await self._creators(chat_id, user_id, active_only=False)
        if not rows:
            await self._say(chat_id, "❌ You are not registered as a moderator.\n\nContact the streamer to add you.")
            return
        lines = ["👤 <b>Moderator Status</b>", ""]
        for m in rows:
            role = m["role"]
            perms = ("All permissions" if role == "owner" else "View only" if role == "viewer" else
                     ", ".join(n for a, n in (("approve", "Approve"), ("reject", "Reject"), ("hide_message", "Hide"),
                                              ("ban_donor", "Ban")) if can({**m, "is_active": True}, a)) or "None")
            lines += [f"🎮 <b>{fmt.esc_html(m['streamer_name'])}</b>",
                      f"   📊 Status: {'✅ Active' if m['is_active'] else '❌ Inactive'}",
                      f"   👑 Role: {role.capitalize()}", f"   🔐 Permissions: {perms}",
                      f"   📅 Since: {fmt.when(m['created_at'])[:10]}", f"   🛡️ Actions: {m['total_actions']}", ""]
        await self._say(chat_id, "\n".join(lines))

    async def _settings(self, chat_id: str, user_id: str) -> None:
        for m in await self._assignments(chat_id, user_id):
            manual = m["moderation_mode"] == "manual"
            await self._say(chat_id, f"⚙️ <b>Moderation Settings</b>\n\n🎮 Streamer: <b>{fmt.esc_html(m['streamer_name'])}"
                                     f"</b>\n\n📋 <b>Mode:</b> {'Manual Approval' if manual else 'Auto-Approve'}\n"
                                     f"📱 <b>Telegram:</b> {'Enabled ✅' if m['telegram_enabled'] else 'Disabled ❌'}")
