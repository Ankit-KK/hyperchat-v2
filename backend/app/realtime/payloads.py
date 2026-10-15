"""Realtime payloads, rendered from the CURRENT database state when an event is published (D2).

Every payload carries event_id (consumers drop duplicates by it) and v (contract version). Fields are
allow-listed per event; never: session/CSRF/status/upload tokens, order/payment/provider ids, streamer or user
ids, emails, secrets, provider responses, moderator identities.

  new-donation / donation-updated (private dashboard)
      {event_id, v, action?, previous_status?, donation{id, name, amount, currency, message, message_visible,
       moderation_status, is_hyperemote, voice_message_url, tts_audio_url, hypersound_url, media_url,
       media_type, created_at}}       message is null while hidden (message_visible = false)
  goal-progress (PUBLIC {slug}-goal)  {event_id, v, active, name, target_amount_inr, progress_inr, brand_color,
                                       goal_overlay_id}: exactly GET /api/v1/public/streamers/{slug}/goal (all
                                      null while no goal is active); active creators only
  leaderboard-updated (private)       {event_id, v, enabled, top_donor{name, total_amount}|null,
                                       latest{name, amount, currency, created_at}|null}  (approved only)
  settings-updated (private)          {event_id, v, <overlay display settings>}
  audio-ready (private audio)         {event_id, v, donation_id}
  audio-now-playing (private alerts)  {event_id, v, donation: now_playing(...)}   (Phase 10, see app.obs)
  vaani-celebration (private alerts)  {event_id, v, donation_id, name, amount, currency, created_at}

A rendered payload larger than SIZE_LIMIT is replaced by {event_id, v, donation_id?, truncated: true}: the
client refetches.
"""

import json
from dataclasses import dataclass

from psycopg.rows import dict_row

from app.public.models import goal_active, goal_view
from app.realtime.channels import channel_name
from app.realtime.outbox import OVERLAY_SETTINGS, Event

VERSION = 1
SIZE_LIMIT = 9_000
APPROVED = ("approved", "auto_approved")
_DONATION_FIELDS = ("id", "name", "amount", "currency", "message", "message_visible", "moderation_status",
                    "is_hyperemote", "voice_message_url", "tts_audio_url", "hypersound_url", "media_url", "media_type",
                    "created_at")


@dataclass(frozen=True)
class Rendered:
    channel: str  # full Pusher channel name
    event: str
    data: dict
    pusher_group: int | None


@dataclass(frozen=True)
class Skip:
    reason: str


def _number(value):
    return float(value) if value is not None else None


def _time(value):
    return value.isoformat() if value is not None else None


AUDIO_KINDS = ("hypersound", "voice", "tts", "silence")


def audio_url_for(d: dict, kind: str, silence_url: str | None) -> str | None:
    """The URL a claimed item plays, for its kind (chosen and allow-listed by the claim)."""
    return {"hypersound": d.get("hypersound_url"), "voice": d.get("voice_message_url"),
            "tts": d.get("tts_audio_url"), "silence": silence_url}.get(kind)


def now_playing(d: dict, kind: str, audio_url: str | None) -> dict:
    """The donation as the alerts overlay shows it while its audio plays (claim response and audio-now-playing).
    A hidden message is null."""
    return {"id": d["id"], "name": d["name"], "amount": _number(d["amount"]), "currency": d["currency"],
            "message": d["message"] if d["message_visible"] else None, "is_hyperemote": bool(d["is_hyperemote"]),
            "kind": kind, "audio_url": audio_url, "voice_message_url": d["voice_message_url"],
            "hypersound_url": d["hypersound_url"], "media_url": d["media_url"], "media_type": d["media_type"],
            "created_at": _time(d["created_at"])}


class RealtimeRepository:
    """Read-only SQL for rendering payloads and for the channel-auth endpoint (runs as hyperchat_app)."""

    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def _one(self, sql: str, params) -> dict | None:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()

    async def streamer(self, streamer_id: str) -> dict | None:
        return await self._one("""
            SELECT streamer_slug AS slug, status, pusher_group, brand_color, alert_box_scale, alert_overlay_id,
                   leaderboard_overlay_id, goal_overlay_id, overlay_adjust_mode,
                   COALESCE(leaderboard_widget_enabled, true) AS leaderboard_widget_enabled, leaderboard_bg_enabled,
                   goal_name, goal_target_amount, COALESCE(goal_is_active, false) AS goal_is_active, goal_activated_at
            FROM public.streamers WHERE id = %s
        """, (streamer_id,))

    async def streamer_by_slug(self, slug: str) -> dict | None:
        return await self._one("SELECT id::text AS id, streamer_slug AS slug, streamer_name AS name, "
                               "user_id::text AS user_id, pusher_group, status FROM public.streamers "
                               "WHERE streamer_slug = %s", (slug,))

    async def donation(self, donation_id: str, streamer_id: str) -> dict | None:
        return await self._one("""
            SELECT id::text AS id, donor_name AS name, amount, currency::text AS currency, message, message_visible,
                   moderation_status, payment_status, is_hyperemote, voice_message_url, tts_audio_url, hypersound_url,
                   media_url, media_type, created_at
            FROM public.donations WHERE id = %s AND streamer_id = %s
        """, (donation_id, streamer_id))

    async def goal_progress_inr(self, streamer_id: str, since) -> float:
        row = await self._one("SELECT COALESCE(SUM(amount_inr), 0) AS total FROM public.donations "
                              "WHERE streamer_id = %s AND payment_status = 'success' "
                              "AND moderation_status IN ('approved', 'auto_approved') AND created_at >= %s",
                              (streamer_id, since))
        return float(row["total"])

    async def top_donor(self, streamer_id: str) -> dict | None:
        return await self._one("SELECT donator_name AS name, total_amount FROM public.streamer_donator_totals "
                               "WHERE streamer_id = %s AND total_amount > 0 "
                               "ORDER BY total_amount DESC, donator_name_lower LIMIT 1", (streamer_id,))

    async def latest_approved(self, streamer_id: str) -> dict | None:
        return await self._one("SELECT donor_name AS name, amount, currency::text AS currency, created_at "
                               "FROM public.donations WHERE streamer_id = %s AND payment_status = 'success' "
                               "AND moderation_status IN ('approved', 'auto_approved') "
                               "ORDER BY created_at DESC, id DESC LIMIT 1", (streamer_id,))


class Renderer:
    def __init__(self, repo: RealtimeRepository, silence_url: str | None = None) -> None:
        self.repo, self.silence_url = repo, silence_url

    async def render(self, event: Event) -> Rendered | Skip:
        s = await self.repo.streamer(event.streamer_id)
        if s is None:
            return Skip("streamer_missing")
        base = {"event_id": event.event_id, "v": VERSION}
        if event.event in ("new-donation", "donation-updated"):
            data = await self._donation(event, base)
        elif event.event == "audio-ready":
            data = await self._audio_ready(event, base)
        elif event.event == "audio-now-playing":
            data = await self._now_playing(event, base)
        elif event.event == "vaani-celebration":
            data = await self._vaani(event, base)
        elif event.event == "goal-progress":
            data = await self._goal(event, s, base)
        elif event.event == "leaderboard-updated":
            data = await self._leaderboard(event, s, base)
        elif event.event == "settings-updated":
            data = {**base, **{k: _number(s[k]) if k == "alert_box_scale" else s[k] for k in OVERLAY_SETTINGS}}
        else:
            return Skip("unknown_event")
        if isinstance(data, Skip):
            return data
        if len(json.dumps(data, default=str).encode()) > SIZE_LIMIT:
            data = {**base, **({"donation_id": event.subject_id} if event.subject_id else {}), "truncated": True}
        return Rendered(channel_name(s["slug"], event.channel), event.event, data, s["pusher_group"])

    async def _donation(self, event: Event, base: dict) -> dict | Skip:
        if event.subject_id is None:
            return Skip("subject_missing")
        d = await self.repo.donation(event.subject_id, event.streamer_id)
        if d is None:
            return Skip("subject_missing")
        if d["payment_status"] != "success":
            return Skip("not_paid")
        donation = {k: d[k] for k in _DONATION_FIELDS}
        donation["amount"] = _number(d["amount"])
        donation["created_at"] = _time(d["created_at"])
        if not d["message_visible"]:
            donation["message"] = None  # a hidden message is never broadcast
        facts = {k: event.facts[k] for k in ("action", "previous_status") if k in (event.facts or {})}
        return {**base, **facts, "donation": donation}

    async def _audio_ready(self, event: Event, base: dict) -> dict | Skip:
        if event.subject_id is None:
            return Skip("subject_missing")
        d = await self.repo.donation(event.subject_id, event.streamer_id)
        if d is None:
            return Skip("subject_missing")
        if d["payment_status"] != "success" or d["moderation_status"] not in APPROVED:
            return Skip("not_approved")
        return {**base, "donation_id": d["id"]}

    async def _now_playing(self, event: Event, base: dict) -> dict | Skip:
        kind = (event.facts or {}).get("kind")
        if event.subject_id is None or kind not in AUDIO_KINDS:
            return Skip("subject_missing")
        d = await self.repo.donation(event.subject_id, event.streamer_id)
        if d is None:
            return Skip("subject_missing")
        if d["payment_status"] != "success" or d["moderation_status"] not in APPROVED:
            return Skip("not_approved")
        return {**base, "donation": now_playing(d, kind, audio_url_for(d, kind, self.silence_url))}

    async def _vaani(self, event: Event, base: dict) -> dict | Skip:
        if event.subject_id is None:
            return Skip("subject_missing")
        d = await self.repo.donation(event.subject_id, event.streamer_id)
        if d is None:
            return Skip("subject_missing")
        if d["payment_status"] != "success" or d["moderation_status"] not in APPROVED:
            return Skip("not_approved")
        return {**base, "donation_id": d["id"], "name": d["name"], "amount": _number(d["amount"]),
                "currency": d["currency"], "created_at": _time(d["created_at"])}

    async def _goal(self, event: Event, s: dict, base: dict) -> dict | Skip:
        if s["status"] != "active":
            return Skip("streamer_not_public")  # a public channel: only for public (active) creators
        progress = await self.repo.goal_progress_inr(event.streamer_id, s["goal_activated_at"]) if goal_active(s) else None
        return {**base, **goal_view(s, progress)}

    async def _leaderboard(self, event: Event, s: dict, base: dict) -> dict:
        if not s["leaderboard_widget_enabled"]:
            return {**base, "enabled": False, "top_donor": None, "latest": None}
        top = await self.repo.top_donor(event.streamer_id)
        latest = await self.repo.latest_approved(event.streamer_id)
        return {**base, "enabled": True,
                "top_donor": {"name": top["name"], "total_amount": _number(top["total_amount"])} if top else None,
                "latest": {"name": latest["name"], "amount": _number(latest["amount"]), "currency": latest["currency"],
                           "created_at": _time(latest["created_at"])} if latest else None}
