"""Public read use-cases: slug validation, active-creator lookup, response assembly, metrics cache."""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.core.errors import ApiError
from app.public import pricing, soundboard
from app.public.models import (
    Goal,
    Leaderboard,
    OverlayConfig,
    PlatformMetrics,
    Pricing,
    PublicStreamer,
    ShowcaseMetrics,
    Soundboard,
    TopDonor,
)
from app.public.repository import PublicRepository

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def not_found() -> ApiError:
    return ApiError(404, "Streamer not found", "streamer_not_found")


PLATFORM_METRICS_TTL = 300
SHOWCASE_METRICS_TTL = 60


class TTLCache:
    """Tiny per-process cache for the aggregate metrics (E7/E8). One loader runs at a time per key."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._values: dict[str, tuple[float, object]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, key: str, ttl: float, loader: Callable[[], Awaitable[object]]):
        hit = self._values.get(key)
        if hit and hit[0] > self._clock():
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._values.get(key)
            if hit and hit[0] > self._clock():
                return hit[1]
            value = await loader()
            self._values[key] = (self._clock() + ttl, value)
            return value


class PublicService:
    def __init__(self, repo: PublicRepository, cache: TTLCache | None = None) -> None:
        self.repo = repo
        self.cache = cache or TTLCache()

    async def _streamer(self, slug: str) -> dict:
        slug = (slug or "").strip().lower()
        if not _SLUG.match(slug):
            raise not_found()
        row = await self.repo.active_streamer(slug)
        if row is None:  # unknown, pending and rejected all look the same (D3)
            raise not_found()
        return row

    async def active_creator(self, slug: str) -> dict:
        """Active creator row (internal; id and settings stay server-side) or the D3 404."""
        return await self._streamer(slug)

    async def profile(self, slug: str) -> PublicStreamer:
        s = await self._streamer(slug)
        return PublicStreamer(
            slug=s["streamer_slug"], name=s["streamer_name"], brand_color=s["brand_color"],
            brand_logo_url=s["brand_logo_url"], background_url=s["background_url"], tts_enabled=s["tts_enabled"],
            hyperemotes_enabled=s["hyperemotes_enabled"], media_upload_enabled=s["media_upload_enabled"],
            media_max_file_size_mb=s["media_max_file_size_mb"], soundboard_enabled=s["soundboard_enabled"],
            leaderboard_widget_enabled=s["leaderboard_widget_enabled"])

    async def pricing(self, slug: str, currency: str | None) -> Pricing:
        try:
            cur = pricing.normalize_currency(currency)
        except pricing.UnsupportedCurrency:
            raise ApiError(400, "Unsupported currency", "unsupported_currency") from None
        s = await self._streamer(slug)
        own, platform = await self.repo.voice_tiers(s["id"])
        return Pricing(currency=cur, **pricing.minimums(s, cur), tts_enabled=s["tts_enabled"],
                       message_char_tiers=pricing.message_char_tiers(s["message_char_tiers"], cur),
                       voice_duration_tiers=pricing.voice_tiers(own, platform, cur))

    async def overlay_config(self, slug: str) -> OverlayConfig:
        s = await self._streamer(slug)
        return OverlayConfig(
            slug=s["streamer_slug"], name=s["streamer_name"], brand_color=s["brand_color"],
            brand_logo_url=s["brand_logo_url"], alert_box_scale=s["alert_box_scale"],
            alert_overlay_id=s["alert_overlay_id"], leaderboard_overlay_id=s["leaderboard_overlay_id"],
            goal_overlay_id=s["goal_overlay_id"], overlay_adjust_mode=s["overlay_adjust_mode"],
            leaderboard_widget_enabled=s["leaderboard_widget_enabled"],
            leaderboard_bg_enabled=s["leaderboard_bg_enabled"], hyperemotes_enabled=s["hyperemotes_enabled"],
            media_upload_enabled=s["media_upload_enabled"], tts_enabled=s["tts_enabled"], goal_name=s["goal_name"],
            goal_target_amount=s["goal_target_amount"], goal_activated_at=s["goal_activated_at"],
            goal_is_active=s["goal_is_active"])

    async def soundboard(self, slug: str) -> Soundboard:
        s = await self._streamer(slug)
        if not s["soundboard_enabled"]:
            raise ApiError(404, "Soundboard not enabled", "soundboard_disabled")
        platform, own = await self.repo.soundboard_slots(s["streamer_slug"])
        return Soundboard(sounds=soundboard.merge(platform, own))

    async def leaderboard(self, slug: str) -> Leaderboard:
        s = await self._streamer(slug)
        if not s["leaderboard_widget_enabled"]:  # D2: only when the creator shows a leaderboard
            return Leaderboard(top_donor=None)
        top = await self.repo.top_donor(s["id"])
        if top is None:
            return Leaderboard(top_donor=None)
        return Leaderboard(top_donor=TopDonor(name=top["donator_name"], total_amount_inr=top["total_amount"]))

    async def goal(self, slug: str) -> Goal:
        s = await self._streamer(slug)
        active = bool(s["goal_is_active"] and s["goal_activated_at"])
        progress = await self.repo.goal_progress_inr(s["id"], s["goal_activated_at"]) if active else 0.0
        return Goal(active=active, name=s["goal_name"], target_amount_inr=s["goal_target_amount"],
                    progress_inr=progress)

    async def platform_metrics(self) -> PlatformMetrics:
        async def load():
            return PlatformMetrics(**await self.repo.platform_metrics())
        return await self.cache.get("platform", PLATFORM_METRICS_TTL, load)

    async def showcase_metrics(self) -> ShowcaseMetrics:
        async def load():
            row = await self.repo.showcase_metrics()
            return ShowcaseMetrics(streamers_live=row["streamers_live"], alerts_last_24h=row["alerts_last_24h"],
                                   last_sync=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        return await self.cache.get("showcase", SHOWCASE_METRICS_TTL, load)
