"""Pusher private-channel authorization and client configuration (D8, D12).

POST /api/v1/realtime/pusher/auth   {socket_id, channel_name} (form or JSON), exactly ONE credential:
  OBS token  (x-obs-token header, or ?token= for the OBS browser source)
             -> overlay channels only (alerts, audio, settings, leaderboard), exactly within the token's scope
                (obs_tokens.pusher_channel_scope, else the default scope of the token's own creator) AND for
                the token's own creator. Never the dashboard channel, whatever a stored scope says.
  session    (cookie) + Origin + CSRF (x-csrf-token)
             -> the dashboard channel only, for the owner, the linked account or an admin (app.authz rules:
                unclaimed creators are admin-only, inactive users are refused).
  Rejected: both or neither credential, malformed socket ids, unknown/presence/encrypted/public channels.
  Limits: 60 requests/min per IP and 30/min per credential (token hash or user id).
  Answer: {"auth": "<key>:<HMAC-SHA256(secret, socket_id:channel)>"} signed with the creator's Pusher app.

GET /api/v1/realtime/config?streamer=<slug>   {key, cluster, auth_endpoint}: public by Pusher's design.

Both answer 503 while realtime is disabled or no Pusher app serves the creator. Every refusal is a
SecurityError: a security event with the reason code, never the token, cookie or CSRF value.
"""

from app.authz.access import StreamerRef, access_via, denial_reason, parse_streamer_ref
from app.realtime.channels import parse_private, valid_socket_id
from app.realtime.payloads import RealtimeRepository
from app.realtime.pusher import PusherApps, channel_auth
from app.security.audit import security_event
from app.security.crypto import sha256_hex
from app.security.dependencies import SecurityError
from app.security.obs_token import ObsTokenInfo, ObsTokenStore, obs_channel_allowed

AUTH_ENDPOINT = "/api/v1/realtime/pusher/auth"
OVERLAY_SUFFIXES = frozenset({"alerts", "audio", "settings", "leaderboard"})
RATE_IP = ("realtime:auth", 60, 60)
RATE_CREDENTIAL = ("realtime:auth:credential", 30, 60)
RATE_CONFIG = ("realtime:config", 60, 60)


class RealtimeService:
    def __init__(self, *, enabled: bool, apps: PusherApps, repo: RealtimeRepository, obs_tokens: ObsTokenStore,
                 limiter) -> None:
        self.enabled, self.apps, self.repo, self.obs_tokens, self.limiter = enabled, apps, repo, obs_tokens, limiter

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.apps.groups)

    def require_available(self) -> None:
        if not self.available:
            raise SecurityError(503, "Realtime is not available", "realtime_unavailable")

    async def limit(self, key: str, rate: tuple[str, int, int]) -> None:
        bucket, limit, window = rate
        if not await self.limiter.hit_rate_limit(key, bucket, limit, window):
            raise SecurityError(429, "Too many requests", "rate_limited")

    @staticmethod
    def parse(socket_id: str, channel: str) -> tuple[str, str]:
        if not valid_socket_id(socket_id):
            raise SecurityError(400, "Invalid socket_id", "bad_socket_id")
        parsed = parse_private(channel)
        if parsed is None:
            raise SecurityError(403, "Forbidden", "channel_not_allowed")
        return parsed

    def _sign(self, pusher_group: int | None, socket_id: str, channel: str) -> dict:
        app = self.apps.resolve(pusher_group)
        if app is None:
            raise SecurityError(503, "Realtime is not available", "pusher_not_configured")
        return {"auth": channel_auth(app, socket_id, channel)}

    async def authorize_obs(self, token: str, info: ObsTokenInfo, socket_id: str, channel: str,
                            slug: str, suffix: str) -> dict:
        await self.limit(f"obs:{sha256_hex(token)[:32]}", RATE_CREDENTIAL)
        if suffix not in OVERLAY_SUFFIXES or slug != info.streamer_slug \
                or not await obs_channel_allowed(self.obs_tokens, token, info, channel):
            security_event("realtime_channel_denied", reason="obs_scope", streamer_id=info.streamer_id)
            raise SecurityError(403, "Forbidden", "channel_not_allowed")
        streamer = await self.repo.streamer_by_slug(slug)
        if streamer is None or streamer["id"] != info.streamer_id:
            raise SecurityError(403, "Forbidden", "channel_not_allowed")
        return self._sign(streamer["pusher_group"], socket_id, channel)

    async def authorize_session(self, user, socket_id: str, channel: str, slug: str, suffix: str) -> dict:
        await self.limit(f"user:{user.id}", RATE_CREDENTIAL)
        if suffix != "dashboard":
            raise SecurityError(403, "Forbidden", "channel_not_allowed")
        streamer = await self.repo.streamer_by_slug(slug)
        if streamer is None:
            raise SecurityError(403, "Forbidden", "channel_not_allowed")
        ref = StreamerRef(id=streamer["id"], slug=streamer["slug"], name=streamer["name"], user_id=streamer["user_id"])
        if access_via(user, ref) is None:
            reason = denial_reason(user, ref)
            security_event("realtime_channel_denied", reason=reason, user_id=user.id, streamer_id=ref.id)
            raise SecurityError(403, "Forbidden", reason)
        return self._sign(streamer["pusher_group"], socket_id, channel)

    async def config(self, streamer: str) -> dict:
        ref = parse_streamer_ref(streamer)
        found = await self.repo.streamer_by_slug(ref.slug) if ref is not None and ref.slug else None
        if found is None:
            raise SecurityError(404, "Streamer not found", "streamer_not_found")
        app = self.apps.resolve(found["pusher_group"])
        if app is None:
            raise SecurityError(503, "Realtime is not available", "pusher_not_configured")
        return {"key": app.key, "cluster": app.cluster, "auth_endpoint": AUTH_ENDPOINT}
