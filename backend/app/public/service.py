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
    PlatformMetrics,
    Pricing,
    PublicStreamer,
    ShowcaseMetrics,
    Soundboard,
    goal_active,
    goal_view,
)
from app.public.repository import PublicRepository
from app.security.audit import security_event

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def not_found() -> ApiError:
    return ApiError(404, "Streamer not found", "streamer_not_found")


def donations_unavailable() -> ApiError:  # generic on purpose: never says why (KYC, owner change, ...)
    return ApiError(503, "Donations are temporarily unavailable", "donations_unavailable")


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
    def __init__(self, repo: PublicRepository, cache: TTLCache | None = None, witness=None) -> None:
        self.repo = repo
        self.cache = cache or TTLCache()
        self.witness = witness  # app.kyc.witness.KycWitness (always set by app.main; None only in unit tests)

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

    async def donation_creator(self, slug: str) -> dict:
        """An active creator that may accept NEW donations and donor uploads (checkout, upload grants, uploads), else
        the D3 404 (no longer active) or a generic 503. Payment readiness is checked separately, at checkout."""
        s = await self._streamer(slug)
        block = await self.repo.donation_block(s["id"])
        if block in ("not_found", "not_active"):  # changed since the lookup: look exactly like an unknown creator
            raise not_found()
        if block is not None:
            raise donations_unavailable()
        if self.witness is not None:  # KYC PR4: a database behind its restore witness accepts no new donations
            problem = self.witness.problem(*await self.repo.enforcement())
            if problem is not None:
                security_event("kyc_enforcement_witness_mismatch", level=50, reason=problem,
                               streamer_slug=s["streamer_slug"])
                raise donations_unavailable()
        return s

    async def profile(self, slug: str) -> PublicStreamer:
        s = await self._streamer(slug)
        return PublicStreamer(
            slug=s["streamer_slug"], name=s["streamer_name"], brand_color=s["brand_color"],
            brand_logo_url=s["brand_logo_url"], background_url=s["background_url"], tts_enabled=s["tts_enabled"],
            hyperemotes_enabled=s["hyperemotes_enabled"], media_upload_enabled=s["media_upload_enabled"],
            media_max_file_size_mb=s["media_max_file_size_mb"], soundboard_enabled=s["soundboard_enabled"])

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

    async def soundboard(self, slug: str) -> Soundboard:
        s = await self._streamer(slug)
        if not s["soundboard_enabled"]:
            raise ApiError(404, "Soundboard not enabled", "soundboard_disabled")
        platform, own = await self.repo.soundboard_slots(s["streamer_slug"])
        return Soundboard(sounds=soundboard.merge(platform, own))

    async def goal(self, slug: str) -> Goal:
        s = await self._streamer(slug)
        progress = await self.repo.goal_progress_inr(s["id"], s["goal_activated_at"]) if goal_active(s) else None
        return Goal(**goal_view(s, progress))

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
