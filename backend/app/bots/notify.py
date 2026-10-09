"""The moderator_notify job (Phase 11, D5..D7): one job per newly completed donation, enqueued in the payment-
completion transaction (app.payments.repository). Historical donations are never enqueued (there is no scan of
mod_notified = false rows), and a donation more than 24 h old is never notified (D6).

run:
  donation missing / already notified                       -> skipped
  bots disabled                                             -> skipped (bots_disabled), mod_notified = true
  outside production, a creator that is not allow-listed    -> skipped (creator_not_allowlisted), mod_notified = true
  not paid, rejected, older than 24 h                       -> skipped, mod_notified = true
  recipients (Telegram DMs; a Discord channel post or Discord DMs) -> one moderator_notifications row each
    (INSERT .. ON CONFLICT DO NOTHING: a retry never creates or re-sends a delivery that was already sent)
    outside production, a recipient id that is not allow-listed -> 'skipped' (not_allowlisted), nothing sent
    sent -> 'sent' (+ the platform message id); 400/401/403/404 -> 'failed' (that recipient only)
    timeout / network / 429 / 5xx -> stays 'pending'; the job is retried (backoff, Retry-After) for the pending ones
  every delivery sent / failed / skipped                    -> succeeded, mod_notified = true (same transaction)
fallback (attempts exhausted): leftover pending deliveries -> 'failed', mod_notified = true. No endless re-notify.
Logs: job ids, counts and reason codes; never donor text, recipient ids or tokens.
"""

import logging
import re

from app.bots import format as fmt
from app.bots.clients import DiscordClient, TelegramClient
from app.bots.permissions import BOT_ACTIONS, can
from app.bots.repository import BotRepository, Target
from app.jobs.queue import Job
from app.jobs.retry import Backoff, Done, Effects, Permanent, Retryable

log = logging.getLogger("hyperchat.bots")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TELEGRAM_ID = re.compile(r"-?[0-9]{1,20}")
DISCORD_ID = re.compile(r"[0-9]{15,21}")


def allowed(recipients: frozenset[str] | None, target_id: str) -> bool:
    """D13: outside production (recipients is a set) only allow-listed ids receive anything."""
    return recipients is None or target_id in recipients


def creator_allowed(creators: frozenset[str] | None, slug: str | None) -> bool:
    """Outside production (creators is a set) the bots act only for allow-listed creator slugs."""
    return creators is None or (slug or "").strip().lower() in creators


def _donation_id(job: Job) -> str:
    did = job.payload.get("donation_id") if isinstance(job.payload, dict) else None
    if not isinstance(did, str) or not _UUID.fullmatch(did) or did != job.dedupe_key:
        raise Permanent("bad_payload")
    return did


class ModeratorNotifyHandler:
    kind = "moderator_notify"
    backoff = Backoff(base_seconds=10, cap_seconds=300, jitter=0.2)

    def __init__(self, repo: BotRepository, *, telegram: TelegramClient | None, discord: DiscordClient | None,
                 enabled: bool, recipients: frozenset[str] | None, frontend_url: str, public_base: str | None,
                 creators: frozenset[str] | None = None) -> None:
        self.repo, self.telegram, self.discord = repo, telegram, discord
        self.enabled, self.recipients, self.creators = enabled, recipients, creators
        self.frontend_url, self.public_base = frontend_url, public_base

    def _notified(self, did: str) -> Effects:
        async def effects(cur) -> None:
            await self.repo.mark_notified(cur, did)
        return effects

    def targets(self, d: dict, moderators: list[dict]) -> list[Target]:
        out = []
        if self.telegram is not None and d["telegram_enabled"]:
            out += [Target("telegram", "dm", m["telegram_user_id"].strip(), m["id"]) for m in moderators
                    if m["telegram_user_id"] and TELEGRAM_ID.fullmatch(m["telegram_user_id"].strip())]
        if self.discord is not None and d["discord_enabled"]:
            mods = [m for m in moderators if m["discord_user_id"] and DISCORD_ID.fullmatch(m["discord_user_id"].strip())]
            channel = (d["discord_moderation_channel_id"] or "").strip()
            if mods and DISCORD_ID.fullmatch(channel):
                out.append(Target("discord", "channel", channel, None))
            elif mods:
                out += [Target("discord", "dm", m["discord_user_id"].strip(), m["id"]) for m in mods]
        return out

    async def run(self, job: Job) -> Done:
        did = _donation_id(job)
        d = await self.repo.notify_context(did)
        if d is None:
            return Done({"outcome": "skipped", "reason": "donation_missing"})
        if d["mod_notified"]:
            return Done({"outcome": "skipped", "reason": "already_notified"})
        if not self.enabled:
            return Done({"outcome": "skipped", "reason": "bots_disabled"}, self._notified(did))
        if not creator_allowed(self.creators, d["streamer_slug"]):
            return Done({"outcome": "skipped", "reason": "creator_not_allowlisted"}, self._notified(did))
        if d["payment_status"] != "success" or d["moderation_status"] not in ("pending", "approved", "auto_approved"):
            return Done({"outcome": "skipped", "reason": "not_eligible"}, self._notified(did))
        if d["too_old"]:
            return Done({"outcome": "skipped", "reason": "too_old"}, self._notified(did))
        moderators = await self.repo.moderators(d["streamer_id"])
        targets = self.targets(d, moderators)
        if not targets:
            return Done({"outcome": "skipped", "reason": "no_recipients"}, self._notified(did))
        rows = await self.repo.ensure_deliveries(did, d["streamer_id"], targets)
        by_id = {m["id"]: m for m in moderators}
        counts = {"sent": 0, "failed": 0, "skipped": 0, "pending": 0}
        transient: Retryable | None = None
        for row in rows:
            if row["status"] != "pending":
                counts[row["status"]] = counts.get(row["status"], 0) + 1
                continue
            if not allowed(self.recipients, row["target_id"]):
                await self.repo.mark_delivery(row["id"], "skipped", reason="not_allowlisted")
                counts["skipped"] += 1
                continue
            try:
                message_id = await self._send(d, row, by_id.get(row["moderator_id"]), moderators)
            except Retryable as exc:
                await self.repo.mark_delivery(row["id"], "pending", reason=exc.reason)
                counts["pending"] += 1
                transient = transient or exc
                continue
            except Permanent as exc:
                await self.repo.mark_delivery(row["id"], "failed", reason=exc.reason)
                counts["failed"] += 1
                continue
            await self.repo.mark_delivery(row["id"], "sent", message_id=message_id)
            counts["sent"] += 1
        log.info("moderator notification", extra={"job_id": job.id, **counts})
        if transient is not None:
            raise transient  # retried later for the pending deliveries only
        return Done({"outcome": "notified", **counts}, self._notified(did))

    async def _actions(self, platform: str, d: dict, recipient_id: str, moderator_id: str | None,
                       allowed_by) -> dict[str, str]:
        if d["moderation_status"] != "pending":
            return {}
        actions = {}
        for action in BOT_ACTIONS:
            if action == "hide_message" and not d.get("message"):
                continue
            if allowed_by(action):
                actions[action] = await self.repo.create_callback(
                    platform, donation_id=d["id"], streamer_id=d["streamer_id"], action=action,
                    recipient_id=recipient_id, moderator_id=moderator_id)
        return actions

    async def _send(self, d: dict, row: dict, moderator: dict | None, moderators: list[dict]) -> str:
        link_buttons = fmt.links(d, self.public_base, self.frontend_url)
        if row["platform"] == "telegram":
            if moderator is None:
                raise Permanent("moderator_removed")
            actions = await self._actions("telegram", d, row["target_id"], moderator["id"],
                                          lambda a: can(moderator, a))
            return await self.telegram.send_message(row["target_id"], fmt.telegram_text(d),
                                                    fmt.telegram_keyboard(actions, link_buttons))
        discord_mods = [m for m in moderators if m["discord_user_id"]]
        if row["target_kind"] == "channel":
            # Channel buttons appear when any moderator may take the action (legacy); who clicked is checked then.
            actions = await self._actions("discord", d, row["target_id"], None,
                                          lambda a: any(can(m, a) for m in discord_mods))
            mentions = [m["discord_user_id"].strip() for m in discord_mods]
            return await self.discord.create_message(row["target_id"], fmt.discord_payload(d, actions, link_buttons,
                                                                                            mentions))
        if moderator is None:
            raise Permanent("moderator_removed")
        actions = await self._actions("discord", d, row["target_id"], moderator["id"], lambda a: can(moderator, a))
        channel = await self.discord.open_dm(row["target_id"])
        return await self.discord.create_message(channel, fmt.discord_payload(d, actions, link_buttons, []))

    async def fallback(self, job: Job, reason: str) -> Effects | None:
        try:
            did = _donation_id(job)
        except Permanent:
            return None

        async def effects(cur) -> None:
            await self.repo.give_up(cur, did, reason)
        return effects
