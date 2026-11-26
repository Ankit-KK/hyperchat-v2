"""Realtime payloads, rendered from the CURRENT database state when an event is published (D2).

Every payload carries event_id (consumers drop duplicates by it) and v (contract version). Fields are
allow-listed per event; never: session/CSRF/status/upload tokens, order/payment/provider ids, streamer or user
ids, emails, secrets, provider responses, moderator identities.

  new-donation / donation-updated (private dashboard)
      {event_id, v, action?, previous_status?, donation{id, name, amount, currency, message, message_visible,
       moderation_status, voice_message_url, tts_audio_url, hypersound_url, media_url, media_type,
       created_at}}       message is null while hidden (message_visible = false)
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

from app.payments import figures
from app.public.models import goal_active, goal_view
from app.realtime.channels import OBS_SUFFIXES, channel_name, dashboard_channel_name, obs_channel_name, token_tag
from app.realtime.outbox import OVERLAY_SETTINGS, Event

VERSION = 1
SIZE_LIMIT = 9_000
APPROVED = ("approved", "auto_approved")
_DONATION_FIELDS = ("id", "name", "amount", "currency", "message", "message_visible", "moderation_status",
                    "voice_message_url", "tts_audio_url", "hypersound_url", "media_url", "media_type", "created_at")


@dataclass(frozen=True)
class Rendered:
    channel: str  # the untagged channel name (logging, tests); what is published is `channels`
    event: str
    data: dict
    pusher_group: int | None
    channels: tuple[str, ...] = ()  # the Pusher channels to publish to; empty means (channel,)


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
            "message": d["message"] if d["message_visible"] else None,
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

    async def obs_token_hashes(self, streamer_id: str) -> list[str]:
        """The hashes of the creator's OBS tokens that are valid now: the same rule as PostgresObsTokenStore.validate
        (active, not revoked, not expired), so a revoked token's channels stop receiving at the next event (NV-4)."""
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("SELECT token_hash FROM public.obs_tokens WHERE streamer_id = %s AND is_active IS TRUE "
                              "AND (revoked_at IS NULL OR revoked_at > now()) AND (expires_at IS NULL OR expires_at > now()) "
                              "ORDER BY token_hash", (streamer_id,))
            return [row[0] for row in await cur.fetchall()]

    async def dashboard_session_ids(self, streamer_id: str, admin_idle_hours: int, admin_absolute_hours: int
                                    ) -> list[str]:
        """The sessions that may read this creator's dashboard channel right now (NV-4b), by the same rules as a
        request: the session is valid (AuthRepository.active_session: not revoked, within its idle and absolute
        expiry, and an administrator's within the admin limits), its user is active, and the user has access
        (app.authz.access.access_via: an administrator; else the owner or the linked account of a claimed creator).
        An administrator's session also needs passkey assurance while enforcement is not 'draft'
        (require_admin_assurance; a missing enforcement row counts as enforced). Runs as hyperchat_app (the worker's
        role, D6), which may execute stepup_session_aal."""
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("""
                SELECT s.id::text FROM public.sessions s
                JOIN public.users u ON u.id = s.user_id
                JOIN public.streamers st ON st.id = %(sid)s
                WHERE s.revoked_at IS NULL AND s.idle_expires_at > now() AND s.absolute_expires_at > now()
                  AND u.is_active
                  AND CASE WHEN u.role = 'admin' THEN
                        s.created_at > now() - make_interval(hours => %(aa)s)
                        AND s.last_seen_at > now() - make_interval(hours => %(ai)s)
                        AND ((SELECT state FROM public.stepup_enforcement) = 'draft'
                             OR public.stepup_session_aal(s.id) >= 2)
                      ELSE st.user_id IS NOT NULL AND (st.user_id = u.id OR u.streamer_id = st.id) END
                ORDER BY s.id
            """, {"sid": streamer_id, "ai": admin_idle_hours, "aa": admin_absolute_hours})
            return [row[0] for row in await cur.fetchall()]

    async def streamer_by_slug(self, slug: str) -> dict | None:
        return await self._one("SELECT id::text AS id, streamer_slug AS slug, streamer_name AS name, "
                               "user_id::text AS user_id, pusher_group, status FROM public.streamers "
                               "WHERE streamer_slug = %s", (slug,))

    async def donation(self, donation_id: str, streamer_id: str) -> dict | None:
        return await self._one("""
            SELECT id::text AS id, donor_name AS name, amount, currency::text AS currency, message, message_visible,
                   moderation_status, payment_status, voice_message_url, tts_audio_url, hypersound_url,
                   media_url, media_type, created_at
            FROM public.donations WHERE id = %s AND streamer_id = %s
        """, (donation_id, streamer_id))

    async def goal_progress_inr(self, streamer_id: str, since) -> float:
        """The public goal's figure (app.payments.figures.GOAL_PROGRESS): net of applied reversals."""
        row = await self._one(figures.GOAL_PROGRESS, (streamer_id, since))
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
    def __init__(self, repo: RealtimeRepository, silence_url: str | None = None,
                 admin_session_hours: tuple[int, int] = (2, 12)) -> None:
        """admin_session_hours: (idle, absolute) limits of an administrator's session, as Settings
        admin_session_idle_hours / admin_session_absolute_hours (their defaults here)."""
        self.repo, self.silence_url, self.admin_session_hours = repo, silence_url, admin_session_hours

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
        base = channel_name(s["slug"], event.channel)
        if event.channel == "dashboard":  # read now: losing access counts from the next event (NV-4b)
            sessions = await self.repo.dashboard_session_ids(event.streamer_id, *self.admin_session_hours)
            if not sessions:
                return Skip("no_dashboard_session")
            return Rendered(base, event.event, data, s["pusher_group"],
                            tuple(dashboard_channel_name(s["slug"], sid) for sid in sessions))
        if event.channel not in OBS_SUFFIXES:
            return Rendered(base, event.event, data, s["pusher_group"], (base,))
        hashes = await self.repo.obs_token_hashes(event.streamer_id)  # read now: a revoke counts from the next event
        if not hashes:
            return Skip("no_active_obs_token")
        return Rendered(base, event.event, data, s["pusher_group"],
                        tuple(obs_channel_name(s["slug"], event.channel, token_tag(h)) for h in hashes))

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
