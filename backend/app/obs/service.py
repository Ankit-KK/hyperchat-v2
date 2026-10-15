"""OBS use-cases (Phase 10). See app.routers.obs for the endpoints.

Rate limits (app.auth rate_limits table):
  claim / next      120 per minute per OBS token          session   30 per minute per OBS token
  token create      10 per hour per creator               replay    30 per hour per creator (re-downloads)
Logs carry ids, kinds, counts and reason codes only: never tokens, URLs, donor names or messages.
"""

import logging
import uuid

from app.authz.dependencies import StreamerAccess
from app.core.errors import ApiError
from app.dashboard.moderation import actor_label
from app.obs.queue import AudioQueue, ClaimResult, NotReplayable
from app.obs.tokens import TokenRepository, obs_urls
from app.realtime.auth import AUTH_ENDPOINT
from app.realtime.channels import channel_name
from app.realtime.outbox import OVERLAY_SETTINGS
from app.realtime.payloads import RealtimeRepository, now_playing
from app.realtime.pusher import PusherApps
from app.security.crypto import sha256_hex
from app.security.dependencies import ObsCaller
from app.security.obs_token import obs_channel_allowed
from app.storage.r2 import SILENCE_KEY

log = logging.getLogger("hyperchat.obs")
RATE_CLAIM = ("obs:claim", 120, 60)
RATE_SESSION = ("obs:session", 30, 60)
RATE_TOKEN_CREATE = ("obs:token-create", 10, 3600)
RATE_REPLAY = ("obs:replay", 30, 3600)
RATE_OVERLAY = ("obs:overlay", 60, 60)  # overlay settings + leaderboard reads, per token
POLL_SECONDS = 30  # the player's safety-net claim interval while idle (missed audio-ready events)


def _iso(value):
    return value.isoformat() if value is not None else None


def _token_view(row: dict) -> dict:
    return {"id": row["id"], "state": row["state"], "created_at": _iso(row["created_at"]),
            "last_used_at": _iso(row["last_used_at"]), "usage_count": row["usage_count"],
            "expires_at": _iso(row["expires_at"]), "revoked_at": _iso(row["revoked_at"])}


class ObsService:
    def __init__(self, *, tokens: TokenRepository, queue: AudioQueue, realtime: RealtimeRepository, apps: PusherApps,
                 realtime_enabled: bool, public_base: str | None, limiter) -> None:
        self.tokens, self.queue, self.realtime, self.apps = tokens, queue, realtime, apps
        self.realtime_enabled, self.public_base, self.limiter = realtime_enabled, public_base, limiter

    async def _limit(self, key: str, rate: tuple[str, int, int]) -> None:
        bucket, limit, window = rate
        if not await self.limiter.hit_rate_limit(key, bucket, limit, window):
            raise ApiError(429, "Too many requests", "rate_limited")

    @staticmethod
    def _token_key(caller: ObsCaller) -> str:
        return f"obs:{sha256_hex(caller.token)[:32]}"

    # ── dashboard: token management ──────────────────────────────────────────
    async def create_token(self, access: StreamerAccess, api_base: str) -> dict:
        await self._limit(f"streamer:{access.streamer.id}", RATE_TOKEN_CREATE)
        token, row, rotated = await self.tokens.create(access.streamer.id)
        log.info("obs token created", extra={"streamer_id": access.streamer.id, "token_id": row["id"],
                                             "rotated": rotated, "via": access.via})
        return {"token": _token_view(row), "previous_tokens_revoking": rotated, **obs_urls(api_base, token)}

    async def list_tokens(self, access: StreamerAccess) -> dict:
        return {"tokens": [_token_view(r) for r in await self.tokens.list(access.streamer.id)]}

    async def revoke_token(self, access: StreamerAccess, token_id: str) -> dict:
        try:
            token_id = str(uuid.UUID(token_id))
        except ValueError:
            raise ApiError(404, "OBS token not found", "obs_token_not_found") from None
        row = await self.tokens.revoke(access.streamer.id, token_id)
        if row is None:
            raise ApiError(404, "OBS token not found", "obs_token_not_found")
        log.info("obs token revoked", extra={"streamer_id": access.streamer.id, "token_id": token_id})
        return {"token": _token_view(row)}

    # ── dashboard: replay ────────────────────────────────────────────────────
    async def replay(self, access: StreamerAccess, donation_id: str) -> dict:
        try:
            donation_id = str(uuid.UUID(donation_id))
        except ValueError:
            raise ApiError(404, "Donation not found", "donation_not_found") from None
        await self._limit(f"streamer:{access.streamer.id}", RATE_REPLAY)
        try:
            await self.queue.replay(access.streamer.id, donation_id, actor_user_id=access.current.user.id,
                                    actor_label=actor_label(access.current.user))
        except LookupError:
            raise ApiError(404, "Donation not found", "donation_not_found") from None
        except NotReplayable:
            raise ApiError(409, "Only approved donations with audio can be replayed", "not_replayable") from None
        log.info("donation audio replay queued", extra={"streamer_id": access.streamer.id, "donation_id": donation_id})
        return {"result": "queued", "donation_id": donation_id}

    # ── OBS: player session and claims ───────────────────────────────────────
    async def session(self, caller: ObsCaller) -> dict:
        await self._limit(self._token_key(caller), RATE_SESSION)
        realtime = None
        streamer = await self.realtime.streamer_by_slug(caller.info.streamer_slug)
        app = self.apps.resolve(streamer["pusher_group"]) if streamer and self.realtime_enabled else None
        if app is not None:
            slug = caller.info.streamer_slug
            realtime = {"key": app.key, "cluster": app.cluster, "auth_endpoint": AUTH_ENDPOINT,
                        "audio_channel": f"private-{slug}-audio"}
        return {"streamer": {"slug": caller.info.streamer_slug, "name": caller.info.streamer_name},
                "realtime": realtime, "claim_endpoint": "/api/v1/obs/audio/claim", "poll_seconds": POLL_SECONDS,
                "silence_url": f"{self.public_base}/{SILENCE_KEY}" if self.public_base else None}

    # ── OBS: overlay settings and leaderboard (initial state of the private settings / leaderboard channels) ──
    async def _overlay_streamer(self, caller: ObsCaller, obs_tokens, channel: str) -> dict:
        """The token's own creator (never a slug from the request), if the token may read that channel."""
        await self._limit(self._token_key(caller), RATE_OVERLAY)
        if not await obs_channel_allowed(obs_tokens, caller.token, caller.info,
                                         channel_name(caller.info.streamer_slug, channel)):
            raise ApiError(403, "Forbidden", "channel_not_allowed")
        streamer = await self.realtime.streamer(caller.info.streamer_id)
        if streamer is None:
            raise ApiError(404, "Streamer not found", "streamer_not_found")
        return streamer

    async def overlay_config(self, caller: ObsCaller, obs_tokens) -> dict:
        """Exactly what settings-updated carries on private-{slug}-settings."""
        s = await self._overlay_streamer(caller, obs_tokens, "settings")
        return {k: float(s[k]) if k == "alert_box_scale" and s[k] is not None else s[k] for k in OVERLAY_SETTINGS}

    async def leaderboard(self, caller: ObsCaller, obs_tokens) -> dict:
        """Top donor (approved donations' running totals) while the creator shows the leaderboard widget."""
        s = await self._overlay_streamer(caller, obs_tokens, "leaderboard")
        top = await self.realtime.top_donor(caller.info.streamer_id) if s["leaderboard_widget_enabled"] else None
        return {"enabled": bool(s["leaderboard_widget_enabled"]),
                "top_donor": {"name": top["name"], "total_amount_inr": float(top["total_amount"])} if top else None}

    async def claim(self, caller: ObsCaller) -> ClaimResult:
        await self._limit(self._token_key(caller), RATE_CLAIM)
        result = await self.queue.claim(caller.info.streamer_id, caller.info.streamer_slug, self.public_base)
        if result.item is not None or result.skipped:
            log.info("obs audio claimed", extra={
                "streamer_id": caller.info.streamer_id, "skipped_unservable": result.skipped,
                "donation_id": result.item.donation["id"] if result.item else None,
                "kind": result.item.kind if result.item else None})
        return result

    @staticmethod
    def claim_body(result: ClaimResult) -> dict:
        item = result.item
        return {"donation_id": item.donation["id"], "kind": item.kind, "audio_url": item.audio_url,
                "display": now_playing(item.donation, item.kind, item.audio_url)}
