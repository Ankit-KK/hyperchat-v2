"""Public read API (Phase 4): /api/v1/public/streamers/{slug}/... and /api/v1/metrics/*.

No authentication, no cookies, GET only. Each route declares its response model (fixed field set) and a
Cache-Control header. Per-IP rate limiting is done by Nginx (deploy/nginx/snippets/hyperchat-public-*).
"""

from fastapi import APIRouter, Query, Request, Response

from app.core.errors import ApiError
from app.public.models import (
    Goal,
    Leaderboard,
    OverlayConfig,
    PlatformMetrics,
    Pricing,
    PublicStreamer,
    ShowcaseMetrics,
    Soundboard,
)
from app.public.service import PublicService

streamers_router = APIRouter(prefix="/api/v1/public/streamers", tags=["public"])
metrics_router = APIRouter(prefix="/api/v1/metrics", tags=["public"])

CACHE_SECONDS = {"profile": 60, "pricing": 60, "overlay": 30, "soundboard": 60, "leaderboard": 10, "goal": 10,
                 "platform": 300, "showcase": 60}


def _service(request: Request) -> PublicService:
    service = getattr(request.app.state, "public", None)
    if service is None:
        raise ApiError(503, "Service unavailable", "public_not_configured")
    return service


def _cache(response: Response, key: str) -> None:
    response.headers["Cache-Control"] = f"public, max-age={CACHE_SECONDS[key]}"


@streamers_router.get("/{slug}", response_model=PublicStreamer)
async def profile(slug: str, request: Request, response: Response) -> PublicStreamer:
    result = await _service(request).profile(slug)
    _cache(response, "profile")
    return result


@streamers_router.get("/{slug}/pricing", response_model=Pricing)
async def streamer_pricing(slug: str, request: Request, response: Response,
                           currency: str = Query("INR", max_length=8)) -> Pricing:
    result = await _service(request).pricing(slug, currency)
    _cache(response, "pricing")
    return result


@streamers_router.get("/{slug}/overlay-config", response_model=OverlayConfig)
async def overlay_config(slug: str, request: Request, response: Response) -> OverlayConfig:
    result = await _service(request).overlay_config(slug)
    _cache(response, "overlay")
    return result


@streamers_router.get("/{slug}/soundboard", response_model=Soundboard)
async def streamer_soundboard(slug: str, request: Request, response: Response) -> Soundboard:
    result = await _service(request).soundboard(slug)
    _cache(response, "soundboard")
    return result


@streamers_router.get("/{slug}/leaderboard", response_model=Leaderboard)
async def leaderboard(slug: str, request: Request, response: Response) -> Leaderboard:
    result = await _service(request).leaderboard(slug)
    _cache(response, "leaderboard")
    return result


@streamers_router.get("/{slug}/goal", response_model=Goal)
async def goal(slug: str, request: Request, response: Response) -> Goal:
    result = await _service(request).goal(slug)
    _cache(response, "goal")
    return result


@metrics_router.get("/platform", response_model=PlatformMetrics)
async def platform_metrics(request: Request, response: Response) -> PlatformMetrics:
    result = await _service(request).platform_metrics()
    _cache(response, "platform")
    return result


@metrics_router.get("/showcase", response_model=ShowcaseMetrics)
async def showcase_metrics(request: Request, response: Response) -> ShowcaseMetrics:
    result = await _service(request).showcase_metrics()
    _cache(response, "showcase")
    return result
