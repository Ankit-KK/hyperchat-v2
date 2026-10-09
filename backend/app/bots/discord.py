"""The Discord interactions endpoint (POST /api/v1/webhooks/discord), Phase 11.

Authentication (D3): Ed25519 signature of timestamp + body with the application's public key, and a timestamp at
most 5 minutes off. Each interaction id is processed once (request_nonces "dc:<id>").

PING -> PONG. Button presses (D4, D9): custom_id "m:<16-character id>" -> the stored row decides the action,
donation and creator; a DM button must be pressed by its recipient, a channel button inside that channel by an
active moderator of the creator; Discord moderation must be on and the role / permissions must allow the action.
The action runs synchronously through the shared moderation core (well within Discord's 3 s), so the reply always
reports the real result (legacy replied "Approved" before the action had run): success updates the message
(type 7: result line, buttons removed), refusals are ephemeral (type 4, flags 64). No mention is ever parsed.
"""

import logging
import re
from datetime import UTC, datetime, timedelta

from app.bots import format as fmt
from app.bots.clients import parse_json, verify_discord
from app.bots.notify import creator_allowed
from app.bots.permissions import BOT_ACTIONS, can
from app.bots.repository import BotRepository
from app.dashboard.moderation import NotPaid
from app.dashboard.repository import BotActor, DashboardRepository
from app.security.crypto import sha256_hex
from app.security.nonce_store import NonceReplayError, NonceStore, NonceStoreError

log = logging.getLogger("hyperchat.bots")
MAX_BODY = 64 * 1024
_CUSTOM_ID = re.compile(r"m:([A-Za-z0-9]{16})")
NO_MENTIONS = {"parse": []}


def ephemeral(text: str) -> dict:
    return {"type": 4, "data": {"content": text, "flags": 64, "allowed_mentions": NO_MENTIONS}}


class DiscordBot:
    def __init__(self, *, public_key: str, repo: BotRepository, moderation: DashboardRepository,
                 nonces: NonceStore, creators: frozenset[str] | None = None) -> None:
        self.public_key, self.repo, self.moderation, self.nonces = public_key, repo, moderation, nonces
        self.creators = creators  # outside production: only allow-listed creator slugs are moderated

    async def handle(self, body: bytes, signature: str | None, timestamp: str | None,
                     now: float | None = None) -> tuple[int, dict | None]:
        if len(body) > MAX_BODY:
            return 413, None
        if not verify_discord(self.public_key, signature, timestamp, body, now):
            return 401, None
        interaction = parse_json(body)
        if interaction is None:
            return 400, None
        if interaction.get("type") == 1:
            return 200, {"type": 1}
        if interaction.get("type") != 3:
            return 400, None
        interaction_id = str(interaction.get("id") or "")
        if not re.fullmatch(r"[0-9]{1,25}", interaction_id):
            return 400, None
        try:
            await self.nonces.insert(f"dc:{interaction_id}", None, datetime.now(UTC) + timedelta(days=2))
        except NonceReplayError:
            return 200, ephemeral("ℹ️ Already handled.")
        except NonceStoreError:
            return 503, None
        reason, response = await self._press(interaction)
        user = str(((interaction.get("member") or {}).get("user") or interaction.get("user") or {}).get("id", ""))
        log.info("discord interaction", extra={"kind": "button", "reason": reason,
                                               "user": sha256_hex(f"discord:{user}")[:12]})
        return 200, response

    async def _press(self, interaction: dict) -> tuple[str, dict]:
        user_id = str(((interaction.get("member") or {}).get("user") or interaction.get("user") or {}).get("id", ""))
        channel_id = str(interaction.get("channel_id") or (interaction.get("channel") or {}).get("id") or "")
        match = _CUSTOM_ID.fullmatch(str((interaction.get("data") or {}).get("custom_id") or ""))
        row = await self.repo.callback("discord", match.group(1)) if match and user_id else None
        if row is None:
            return "unknown_callback", ephemeral("❌ This action has expired or is invalid.")
        if row["expired"]:
            return "expired", ephemeral("❌ This action has expired. Please use the dashboard.")
        # DM buttons belong to their recipient; channel buttons to that channel (recipient_id = channel id there).
        if row["moderator_id"] is not None and row["recipient_id"] != user_id:
            return "not_recipient", ephemeral("❌ This button was not sent to you.")
        if row["moderator_id"] is None and row["recipient_id"] != channel_id:
            return "wrong_channel", ephemeral("❌ This action is not valid here.")
        moderator = await self.repo.moderator_for("discord", row["streamer_id"], user_id)
        if moderator is None:
            return "not_moderator", ephemeral("❌ You are not a registered moderator for this streamer.")
        if not creator_allowed(self.creators, moderator["streamer_slug"]):
            return "creator_not_allowlisted", ephemeral("❌ This action has expired or is invalid.")
        if not moderator["discord_enabled"]:
            return "discord_disabled", ephemeral("❌ Discord moderation is disabled.")
        action = row["action_type"]
        if action not in BOT_ACTIONS or not can(moderator, action):
            return "not_permitted", ephemeral("❌ You do not have permission for this action.")
        try:
            outcome = await self.moderation.moderate(
                row["streamer_id"], row["donation_id"], action, actor_user_id=None, actor_label=moderator["mod_name"],
                notes="Banned via Discord" if action == "ban_donor" else None,
                bot=BotActor("discord", moderator["id"], user_id))
        except NotPaid:
            return "not_paid", ephemeral("❌ This donation is not paid.")
        if outcome is None:
            return "donation_missing", ephemeral("❌ Donation not found.")
        _, step, _ = outcome
        if step.result != "applied":
            return "unchanged", ephemeral(fmt.result_line(action, False, "", step.new_status))
        line = fmt.result_line(action, True, fmt.esc_md(moderator["mod_name"]), step.new_status)
        return "applied", {"type": 7, "data": {"content": line, "components": [], "allowed_mentions": NO_MENTIONS}}
