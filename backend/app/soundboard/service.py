"""Creator soundboard use-cases (Phase 6). Every route already passed require_streamer_access (signed in;
owner, linked account or admin; Origin + CSRF on writes); the Custom Soundboard gate (D3, terms.py) is checked
after that.

Uploads: POST /uploads returns a presigned PUT (300 s, signed host + content-type + content-length) for a
server-generated key soundboards/<slug>/<uuid>.<ext>. Create, swap and replace then take that key and
confirm it: it must be THIS creator's key shape for the right kind, not used by any slot yet, present in
R2 with the expected type and a size within the limit, and its bytes must really be an MP3 of at most 7 s
(measured here) or a GIF/PNG/JPEG/WebP image. A rejected upload is deleted.

R2 deletion only ever happens after the database change committed, best-effort, and only for objects
that (a) are soundboard keys of the current creator's prefix, (b) were stored by THIS environment (their
URL is this bucket's public URL: legacy rows point elsewhere) and (c) no slot references any more.
Platform hypersounds, donor uploads and legacy objects can therefore never be deleted from here.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app.authz.dependencies import StreamerAccess
from app.core.errors import ApiError
from app.dashboard.validation import USER_AGENT_MAX, parse_uuid
from app.security.audit import security_event
from app.soundboard import rules, terms
from app.soundboard.audio import Mp3Error, parse_mp3
from app.soundboard.models import (
    Board,
    Budget,
    ChangesApplied,
    Restored,
    Slot,
    SlotCreated,
    SlotResult,
    SoundboardChanges,
    SoundRequest,
    Swapped,
    TermsStatus,
    UploadTicket,
)
from app.soundboard.repository import (
    CapExceeded,
    KeyInUse,
    OrderConflict,
    PlatformSlot,
    SlotHidden,
    SlotLocked,
    SlotNotFound,
    SoundboardRepository,
    SwapLimited,
)
from app.storage.r2 import SOUNDBOARD_KEY, ObjectStore, StorageError
from app.uploads.media import looks_like_video, sniff_image

log = logging.getLogger("hyperchat.soundboard")
UNAVAILABLE = "Uploads are not available"
STORAGE_FAILED = "Upload storage failed. Please try again."


class SwapRejected(Exception):
    """429 with the budget/cooldown details the dashboard shows."""

    def __init__(self, body: dict) -> None:
        super().__init__(body.get("code"))
        self.body = body


@dataclass(frozen=True)
class Confirmed:
    key: str
    url: str
    duration_s: float | None = None
    image_type: str | None = None


def _errors(exc: Exception) -> ApiError:
    if isinstance(exc, SlotNotFound):
        return ApiError(404, "Sound not found", "slot_not_found")
    if isinstance(exc, SlotLocked):
        return ApiError(403, "Locked sounds cannot be changed", "slot_locked")
    if isinstance(exc, PlatformSlot):
        return ApiError(403, "Platform sounds cannot be deleted", "platform_slot")
    if isinstance(exc, SlotHidden):
        return ApiError(409, "Hidden sounds cannot be reordered", "slot_hidden")
    if isinstance(exc, CapExceeded):
        return ApiError(409, f"At most {rules.VISIBLE_MAX} sounds can be visible", "visible_cap")
    if isinstance(exc, OrderConflict):
        return ApiError(409, "Two visible sounds cannot share a position", "order_conflict")
    if isinstance(exc, KeyInUse):
        return ApiError(422, "This upload was already used", "soundboard_key_in_use")
    raise exc


def _resolve(r: dict) -> dict:
    """A creator row merged with its platform slot (legacy get-soundboard resolution)."""
    return {
        "id": r["id"], "source": r["source"], "is_visible": r["is_visible"], "order_index": r["order_index"],
        "is_locked": bool(r["is_locked"]),
        "swappable_index": r["swappable_index"] if r["swappable_index"] is not None else r["p_swappable_index"],
        "last_swapped_at": r["last_swapped_at"],
        "display_name": r["display_name"] or r["p_display_name"] or "",
        "audio_url": r["audio_url"] or r["p_audio_url"] or "",
        "audio_duration_s": float(r["audio_duration_s"] if r["audio_duration_s"] is not None
                                  else r["p_audio_duration_s"] if r["p_audio_duration_s"] is not None else 0),
        "image_url": r["image_url"] or r["p_image_url"] or "",
        "image_type": r["image_type"] or r["p_image_type"] or "png",
        "default_order_index": r["default_order_index"],
    }


class SoundboardService:
    def __init__(self, repo: SoundboardRepository, store: ObjectStore | None, limiter) -> None:
        self.repo, self.store, self.limiter = repo, store, limiter

    async def _enabled(self, access: StreamerAccess) -> None:
        """D3 gate for every Custom Soundboard route: eligible AND the current terms accepted, not revoked."""
        state = await self.repo.terms_state(access.streamer.id)
        if not state or not state["enabled"]:
            raise ApiError(403, "Soundboard not enabled", "soundboard_disabled")
        if state["accepted_at"] is None or state["revoked_at"] is not None:
            raise ApiError(403, "Accept the current Custom Soundboard terms first", "soundboard_terms_required")

    # ── terms (D3): status for anyone with access; acceptance by the owner only ──
    async def terms_status(self, access: StreamerAccess) -> TermsStatus:
        state = await self.repo.terms_state(access.streamer.id) or {"enabled": False, "accepted_at": None,
                                                                      "revoked_at": None}
        accepted, revoked = state["accepted_at"] is not None, state["revoked_at"] is not None
        is_owner = access.streamer.user_id == access.current.user.id  # an admin owner counts; "via" says admin
        return TermsStatus(current_version=terms.VERSION, eligible=bool(state["enabled"]), accepted=accepted,
                           accepted_at=state["accepted_at"], revoked=revoked,
                           usable=bool(state["enabled"]) and accepted and not revoked,
                           can_accept=is_owner and not revoked)

    async def accept_terms(self, access: StreamerAccess, version: str, user_agent: str | None) -> TermsStatus:
        if access.streamer.user_id != access.current.user.id:  # never a linked account or an admin for the owner
            self._event("soundboard_terms_rejected", access, "not_owner", logging.WARNING)
            raise ApiError(403, "Only the creator's owner can accept these terms", "soundboard_terms_owner_only")
        if version != terms.VERSION:  # exactly the server's version, nothing normalised
            raise ApiError(409, "Terms version is not current", "terms_version_mismatch")
        state = await self.repo.terms_state(access.streamer.id)
        if state and state["revoked_at"] is not None:  # a revoked acceptance is HyperChat's call, not re-acceptable
            raise ApiError(409, "This acceptance was revoked by HyperChat", "soundboard_terms_revoked")
        agent = (user_agent or "")[:USER_AGENT_MAX] or None
        if await self.repo.accept_terms(access.streamer.id, access.current.user.id, agent):
            self._event("soundboard_terms_accepted", access, terms.VERSION)
        return await self.terms_status(access)

    def _store(self) -> ObjectStore:
        if self.store is None:
            raise ApiError(503, UNAVAILABLE, "storage_not_configured")
        return self.store

    def _event(self, event: str, access: StreamerAccess, reason: str, level=logging.INFO) -> None:
        security_event(event, level=level, reason=reason, user_id=access.current.user.id,
                       streamer_id=access.streamer.id)

    # ── read / edit ─────────────────────────────────────────────────────────
    async def board(self, access: StreamerAccess) -> Board:
        await self._enabled(access)
        rows, budget = await self.repo.board(access.streamer.id, access.streamer.slug)
        slots = [_resolve(r) for r in rows]
        active = sorted((s for s in slots if s["is_visible"]), key=lambda s: (s["order_index"], s["id"]))
        hidden = sorted((s for s in slots if not s["is_visible"]),
                        key=lambda s: (s["default_order_index"] if s["default_order_index"] is not None else 999, s["id"]))
        revision = max((int(r["updated_at"].timestamp() * 1000) for r in rows if r["updated_at"]), default=0)
        now = datetime.now(UTC)
        return Board(active=[Slot(**s) for s in active], hidden=[Slot(**s) for s in hidden], revision=revision,
                     budget=Budget(swaps_used=budget["used"], swaps_limit=rules.SWAP_BUDGET,
                                   window_days=rules.SWAP_WINDOW.days, next_budget_slot_frees_at=budget["frees_at"],
                                   slot_cooldowns={str(i): t for i, t in budget["cooldowns"].items() if t > now}))

    async def apply(self, access: StreamerAccess, changes: SoundboardChanges) -> ChangesApplied:
        await self._enabled(access)
        if not (changes.hide or changes.update or changes.reorder or changes.unhide):
            raise ApiError(400, "No changes", "empty_changes")
        ids = {name: [parse_uuid(c.id) for c in getattr(changes, name)] for name in ("hide", "update", "reorder", "unhide")}
        if any(i is None for values in ids.values() for i in values):
            raise _errors(SlotNotFound())  # malformed ids are unknown sounds (404), as in Phase 5
        if any(len(values) != len(set(values)) for values in ids.values()):
            raise ApiError(422, "A sound appears twice in the same list", "duplicate_slot")
        try:
            count = await self.repo.apply_changes(
                access.streamer.id, access.streamer.slug, ids["hide"],
                list(zip(ids["update"], (c.display_name for c in changes.update))),
                list(zip(ids["reorder"], (c.order_index for c in changes.reorder))),
                list(zip(ids["unhide"], (c.order_index for c in changes.unhide))))
        except (SlotNotFound, SlotLocked, SlotHidden, OrderConflict, CapExceeded) as exc:
            raise _errors(exc) from None
        return ChangesApplied(visible_count=count)

    async def restore(self, access: StreamerAccess) -> Restored:
        await self._enabled(access)
        await self.repo.restore_defaults(access.streamer.id, access.streamer.slug)
        self._event("soundboard_changed", access, "restore_defaults")
        return Restored(restored=True)

    # ── presigned upload ────────────────────────────────────────────────────
    async def presign(self, access: StreamerAccess, kind: str, content_type: str, content_length: int) -> UploadTicket:
        await self._enabled(access)
        store = self._store()
        bucket, max_hits, window = rules.UPLOAD_RATE  # every attempt counts, valid or not
        if not await self.limiter.hit_rate_limit(f"user:{access.current.user.id}", bucket, max_hits, window):
            self._event("soundboard_rejected", access, "upload_rate_limited", logging.WARNING)
            raise ApiError(429, "Too many uploads. Please try again later.", "soundboard_rate_limited")
        ext, stored_type = rules.content_type_ext(kind, content_type)
        limit = rules.MAX_BYTES[kind]
        if not 0 < content_length <= limit:
            raise ApiError(400, f"Invalid size (max {limit} bytes)", "soundboard_bad_size")
        key = rules.new_key(access.streamer.slug, ext)
        try:
            url = store.presign_put(key, content_type=stored_type, content_length=content_length)
        except StorageError:
            raise ApiError(502, STORAGE_FAILED, "presign_failed") from None
        return UploadTicket(upload_url=url, key=key, required_headers={"Content-Type": stored_type},
                            expires_in=300)

    # ── confirming an uploaded object ───────────────────────────────────────
    async def _discard(self, slug: str, keys: list) -> None:
        """Best-effort removal of rejected or superseded uploads. Only soundboard keys under THIS creator's
        prefix that no slot references (re-checked here: another request may have just started using one)."""
        for key in keys:
            m = SOUNDBOARD_KEY.match(key) if isinstance(key, str) else None
            if m is None or m.group("slug") != slug:
                continue
            try:
                if await self.repo.keys_in_use([key]):
                    continue
                await self._store().delete_object(key)
            except (StorageError, ValueError, ApiError) as exc:
                log.warning("soundboard object not removed", extra={"reason": str(exc)[:64]})

    async def _confirm(self, access: StreamerAccess, key: object, kind: str) -> Confirmed:
        store = self._store()
        slug = access.streamer.slug
        key = rules.owned_key(slug, key, kind)  # this creator's prefix, right kind, server key shape (422)
        if await self.repo.keys_in_use([key]):
            raise _errors(KeyInUse())
        ext = rules.key_ext(key)
        limit = rules.MAX_BYTES[kind]
        try:
            head = await store.head_object(key)
            if head is None:
                raise ApiError(422, "Uploaded file not found", "soundboard_missing_object")
            if head.size <= 0 or head.size > limit:
                await self._discard(slug, [key])
                raise ApiError(422, f"Uploaded file is too large (max {limit} bytes)", "soundboard_too_large")
            if head.content_type.split(";", 1)[0].strip().lower() != rules.STORED_TYPE[ext]:
                await self._discard(slug, [key])
                raise ApiError(422, "Uploaded file type does not match", "soundboard_type_mismatch")
            data = await store.get_object(key, max_bytes=limit)
        except StorageError:
            raise ApiError(502, STORAGE_FAILED, "storage_failed") from None
        if len(data) != head.size:
            raise ApiError(422, "Uploaded file changed while it was checked", "soundboard_size_mismatch")
        if kind == "audio":
            try:
                info = await asyncio.to_thread(parse_mp3, data)
            except Mp3Error:
                await self._discard(slug, [key])
                raise ApiError(422, "Sound must be an MP3 file", "soundboard_not_mp3") from None
            if info.duration_s > rules.AUDIO_MAX_SECONDS + rules.AUDIO_DURATION_TOLERANCE_S:
                await self._discard(slug, [key])
                raise ApiError(422, f"Sound is longer than {rules.AUDIO_MAX_SECONDS:g} seconds", "soundboard_too_long")
            return Confirmed(key, store.public_url(key), duration_s=round(min(info.duration_s, 999.99), 2))
        if looks_like_video(data) or sniff_image(data) != rules.STORED_TYPE[ext]:
            await self._discard(slug, [key])
            raise ApiError(422, "Image content does not match its type", "soundboard_image_mismatch")
        return Confirmed(key, store.public_url(key), image_type=ext)

    async def _confirm_pair(self, access: StreamerAccess, body: SoundRequest) -> dict:
        audio = await self._confirm(access, body.audio_key, "audio")  # .mp3 keys only, so never the image key
        try:
            image = await self._confirm(access, body.image_key, "image")
        except ApiError:
            await self._discard(access.streamer.slug, [audio.key])
            raise
        return {"display_name": body.display_name, "audio_key": audio.key, "audio_url": audio.url,
                "audio_duration_s": audio.duration_s, "image_key": image.key, "image_url": image.url,
                "image_type": image.image_type}

    def _deletable(self, slug: str, key: str | None, url: str | None) -> bool:
        if not key or not url:
            return False
        m = SOUNDBOARD_KEY.match(key)
        return bool(m and m.group("slug") == slug and url == self._store().public_url(key))

    async def _cleanup(self, slug: str, pairs: list[tuple]) -> None:
        """After a committed change: remove superseded objects of this creator in this environment's bucket."""
        if self.store is None:
            return
        await self._discard(slug, [k for k, u in pairs if self._deletable(slug, k, u)])

    # ── create / swap / replace / delete ────────────────────────────────────
    async def create(self, access: StreamerAccess, body: SoundRequest) -> SlotCreated:
        await self._enabled(access)
        sound = await self._confirm_pair(access, body)
        try:
            slot_id = await self.repo.create_custom(access.streamer.id, access.streamer.slug, sound)
        except Exception as exc:
            await self._discard(access.streamer.slug, [sound["audio_key"], sound["image_key"]])
            if isinstance(exc, KeyInUse):
                raise _errors(exc) from None
            raise
        self._event("soundboard_changed", access, "create")
        return SlotCreated(slot_id=slot_id)

    async def swap(self, access: StreamerAccess, index: str, body: SoundRequest) -> Swapped:
        await self._enabled(access)
        if not (index.isascii() and index.isdigit() and rules.SWAPPABLE_MIN <= int(index) <= rules.SWAPPABLE_MAX):
            raise ApiError(400, "Invalid slot index (1-45)", "soundboard_bad_index")
        sound = await self._confirm_pair(access, body)
        try:
            result = await self.repo.swap(access.streamer.id, access.streamer.slug, int(index), sound)
        except Exception as exc:
            await self._discard(access.streamer.slug, [sound["audio_key"], sound["image_key"]])  # legacy: drop it
            if isinstance(exc, SwapLimited):
                self._event("soundboard_rejected", access, exc.code, logging.WARNING)
                body_429 = {"error": "Swap limit reached. Try again later." if exc.code == "budget_exhausted"
                            else "This position was swapped less than 24 hours ago.", "code": exc.code,
                            "swaps_used": exc.swaps_used, "swaps_limit": rules.SWAP_BUDGET}
                if exc.frees_at is not None:
                    body_429["next_budget_slot_frees_at"] = exc.frees_at.isoformat()
                if exc.cooldown_ends_at is not None:
                    body_429["slot_cooldown_ends_at"] = exc.cooldown_ends_at.isoformat()
                raise SwapRejected(body_429) from None
            if isinstance(exc, (KeyInUse, OrderConflict, CapExceeded)):
                raise _errors(exc) from None
            raise
        self._event("soundboard_changed", access, "swap")
        await self._cleanup(access.streamer.slug, result["previous"])
        return Swapped(slot_id=result["slot_id"], slot_cooldown_ends_at=result["cooldown_ends_at"],
                       swaps_used=result["swaps_used"], swaps_limit=rules.SWAP_BUDGET)

    async def replace(self, access: StreamerAccess, slot_id: str, kind: str, key: object) -> SlotResult:
        await self._enabled(access)
        slot_id = parse_uuid(slot_id)
        if slot_id is None:
            raise _errors(SlotNotFound())
        sound = await self._confirm(access, key, kind)
        try:
            previous = await self.repo.replace(access.streamer.id, access.streamer.slug, slot_id, kind,
                                               {"key": sound.key, "url": sound.url, "duration_s": sound.duration_s,
                                                "image_type": sound.image_type})
        except Exception as exc:
            await self._discard(access.streamer.slug, [sound.key])
            if isinstance(exc, (SlotNotFound, SlotLocked, KeyInUse)):
                raise _errors(exc) from None
            raise
        self._event("soundboard_changed", access, f"replace_{kind}")
        await self._cleanup(access.streamer.slug, [previous])
        return SlotResult(result="replaced")

    async def delete(self, access: StreamerAccess, slot_id: str) -> SlotResult:
        await self._enabled(access)
        slot_id = parse_uuid(slot_id)
        if slot_id is None:
            raise _errors(SlotNotFound())
        try:
            pairs = await self.repo.delete_custom(access.streamer.id, access.streamer.slug, slot_id)
        except (SlotNotFound, PlatformSlot) as exc:
            raise _errors(exc) from None
        self._event("soundboard_changed", access, "delete")
        await self._cleanup(access.streamer.slug, pairs)
        return SlotResult(result="removed")
