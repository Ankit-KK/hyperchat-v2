"""Donor uploads (Phase 6): single-use upload grants, then server-side storage of voice messages and
media in R2.

Order of checks for an upload (each step fails closed):
  trusted Origin (403) -> configured (503) -> per-IP rate limit (429)      [router dependency]
  -> active creator (404) -> media enabled (403) -> declared type (415) -> declared size (413)
  -> grant: signature, expiry, creator, upload type (403)                   [prepare]
  -> body read with a hard cap (413) -> bytes really are that format, voice duration (415/422)
  -> grant burned in request_nonces (403 replay / 503) -> R2 PutObject (502) [store_*]
Keys are generated here only: <prefix>/<creator slug>_<epoch ms>_<uuid4 hex>.<ext>. Public URLs are built
from the configured public base. Nothing here reads, lists or deletes objects.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

from pydantic import SecretStr

from app.core.errors import ApiError
from app.public import pricing
from app.public.service import PublicService
from app.security.audit import security_event
from app.security.nonce_store import NonceStore
from app.security.upload_grant import GrantCheck, consume_upload_grant, issue_upload_grant, verify_upload_grant
from app.storage.r2 import ObjectStore, StorageError
from app.uploads.media import (
    MB,
    VOICE_MAX_BYTES,
    ImageKind,
    check_voice_content_type,
    declared_image_kind,
    media_limit_bytes,
    validate_image,
    validate_voice,
)

UNAVAILABLE = "Uploads are not available"


@dataclass(frozen=True)
class RateRule:
    bucket: str
    limit: int
    window_seconds: int
    error: str


RATE_RULES = {  # per client IP (legacy limits)
    "grant": RateRule("upload:grant", 10, 60, "Too many requests. Please try again shortly."),
    "voice": RateRule("upload:voice", 5, 60, "Too many uploads. Please try again later."),
    "media": RateRule("upload:media", 3, 60, "Too many uploads. Please try again later."),
}


class RateLimiter(Protocol):
    async def hit_rate_limit(self, key: str, bucket: str, limit: int, window_seconds: int) -> bool: ...


@dataclass(frozen=True)
class Creator:
    id: str  # internal; used for lookups, never returned
    slug: str
    media_upload_enabled: bool
    media_max_file_size_mb: object


class Creators(Protocol):
    async def active(self, slug: str) -> Creator: ...

    async def max_voice_seconds(self, creator: Creator) -> float: ...


class PublicCreators:
    """Creator lookups through the Phase 4 public service: active creators only, identical 404s (D3)."""

    def __init__(self, public: PublicService) -> None:
        self.public = public

    async def active(self, slug: str) -> Creator:
        row = await self.public.active_creator(slug)
        return Creator(row["id"], row["streamer_slug"], bool(row["media_upload_enabled"]),
                       row["media_max_file_size_mb"])

    async def max_voice_seconds(self, creator: Creator) -> float:
        """Longest voice message any price tier allows (the amount is only known at payment, Phase 7)."""
        own, platform = await self.public.repo.voice_tiers(creator.id)
        return float(max(t["max_duration_seconds"] for t in pricing.voice_tiers(own, platform, "INR")))


@dataclass(frozen=True)
class UploadTicket:
    """Everything checked before the body is read."""
    kind: str
    creator: Creator
    grant: GrantCheck
    max_bytes: int
    too_large: ApiError
    image: ImageKind | None = None
    max_seconds: float | None = None


def _too_large(kind: str, max_bytes: int) -> ApiError:
    if kind == "voice":
        return ApiError(413, "Voice message too large. Maximum size is 5MB.", "voice_too_large")
    return ApiError(413, f"File too large. Maximum size is {max_bytes / MB:g}MB.", "media_too_large")


class UploadService:
    def __init__(self, *, grant_secret: SecretStr | None, creators: Creators, nonces: NonceStore | None,
                 limiter: RateLimiter | None, store: ObjectStore | None, now_ms=None) -> None:
        self._grant_secret = grant_secret
        self.creators, self.nonces, self.limiter, self.store = creators, nonces, limiter, store
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

    def _secret(self) -> str | None:
        value = self._grant_secret.get_secret_value().strip() if self._grant_secret is not None else ""
        return value or None

    def ensure_available(self) -> None:
        if not (self._secret() and self.store is not None and self.nonces is not None and self.limiter is not None):
            raise ApiError(503, UNAVAILABLE, "uploads_not_configured")

    async def check_rate(self, action: str, client_ip: str | None) -> None:
        rule = RATE_RULES[action]
        if not await self.limiter.hit_rate_limit(f"ip:{client_ip}", rule.bucket, rule.limit, rule.window_seconds):
            security_event("upload_rejected", reason=f"{action}_rate_limited", client_ip=client_ip)
            raise ApiError(429, rule.error, "upload_rate_limited")

    async def _media_creator(self, slug: str) -> Creator:
        creator = await self.creators.active(slug)
        if not creator.media_upload_enabled or media_limit_bytes(creator.media_max_file_size_mb) <= 0:
            raise ApiError(403, "Media uploads are not enabled for this creator", "media_disabled")
        return creator

    # ── POST /{slug}/upload-grant ────────────────────────────────────────────
    async def issue_grant(self, slug: str, kind: str) -> dict:
        creator = await (self._media_creator(slug) if kind == "media" else self.creators.active(slug))
        grant, expires_at = issue_upload_grant(self._secret(), creator.slug, kind=kind, now_ms=self._now_ms())
        return {"grant": grant, "expires_at": expires_at}

    # ── POST /{slug}/uploads/{voice,media}: checks before the body is read ──
    async def prepare(self, kind: str, slug: str, *, content_type: str | None, content_length: str | None,
                      grant: str | None, client_ip: str | None) -> UploadTicket:
        image = None
        if kind == "media":
            creator = await self._media_creator(slug)
            image = declared_image_kind(content_type)
            max_bytes = media_limit_bytes(creator.media_max_file_size_mb)
        else:
            creator = await self.creators.active(slug)
            check_voice_content_type(content_type)
            max_bytes = VOICE_MAX_BYTES
        too_large = _too_large(kind, max_bytes)
        if content_length is not None:
            if not content_length.isascii() or not content_length.isdigit():
                raise ApiError(400, "Invalid Content-Length", "bad_content_length")
            if int(content_length) > max_bytes:
                raise too_large
        check = verify_upload_grant(self._secret(), grant, creator.slug, kind=kind, now_ms=self._now_ms())
        if not check.ok:
            security_event("upload_rejected", reason="grant_invalid", streamer_slug=creator.slug, client_ip=client_ip)
            raise ApiError(403, check.error or "Invalid upload grant", "grant_invalid")
        max_seconds = await self.creators.max_voice_seconds(creator) if kind == "voice" else None
        return UploadTicket(kind, creator, check, max_bytes, too_large, image, max_seconds)

    # ── after the body is read ──────────────────────────────────────────────
    async def _burn_grant(self, ticket: UploadTicket) -> None:
        used = await consume_upload_grant(self.nonces, ticket.grant)
        if used.unavailable:
            security_event("upload_rejected", reason="grant_store_unavailable", streamer_slug=ticket.creator.slug)
            raise ApiError(503, UNAVAILABLE, "grant_store_unavailable")
        if not used.ok:
            security_event("upload_rejected", reason="grant_replayed", streamer_slug=ticket.creator.slug)
            raise ApiError(403, used.error or "Upload grant already used", "grant_replayed")

    async def _put(self, ticket: UploadTicket, key: str, data: bytes, content_type: str,
                   metadata: dict[str, str] | None = None) -> str:
        try:
            await self.store.put_object(key, data, content_type=content_type, metadata=metadata)
        except StorageError as exc:
            logging.getLogger("hyperchat.storage").warning(
                "storage write failed", extra={"reason": str(exc), "streamer_slug": ticket.creator.slug})
            raise ApiError(502, "Upload storage failed. Please try again.", "storage_failed") from None
        security_event("upload_stored", level=logging.INFO, reason=ticket.kind, streamer_slug=ticket.creator.slug,
                       count=len(data))
        return self.store.public_url(key)

    def _key(self, prefix: str, ticket: UploadTicket, ext: str) -> str:
        return f"{prefix}/{ticket.creator.slug}_{self._now_ms():013d}_{uuid.uuid4().hex}.{ext}"

    async def store_voice(self, ticket: UploadTicket, data: bytes) -> dict:
        if not data:
            raise ApiError(400, "Upload body is empty", "empty_body")
        info = await asyncio.to_thread(validate_voice, data, ticket.max_seconds)
        await self._burn_grant(ticket)
        key = self._key("voice-messages", ticket, "webm")
        url = await self._put(ticket, key, data, "audio/webm",
                              metadata={"duration-ms": str(round(info.duration_s * 1000))})
        return {"voice_message_url": url}

    async def store_media(self, ticket: UploadTicket, data: bytes) -> dict:
        if not data:
            raise ApiError(400, "Upload body is empty", "empty_body")
        kind = validate_image(data, ticket.image)
        await self._burn_grant(ticket)
        key = self._key("donation-media", ticket, kind.ext)
        url = await self._put(ticket, key, data, kind.mime)
        return {"media_url": url, "media_type": kind.media_type}
