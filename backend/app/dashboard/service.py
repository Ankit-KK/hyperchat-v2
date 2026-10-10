"""Dashboard use-cases. Every rule lives here (or in validation / moderation); the router stays thin."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.auth.repository import User
from app.authz.dependencies import StreamerAccess
from app.core.errors import ApiError
from app.dashboard import validation as v
from app.dashboard.models import (
    DashboardSettings,
    DonationItem,
    DonationPage,
    DonationStats,
    Moderator,
    ModeratorCreate,
    ModeratorList,
    ModeratorPermissionsPatch,
    ModeratorPermissionsResult,
    ModeratorRemoval,
    SettingsUpdate,
    TermsAccepted,
    TermsStatus,
)
from app.dashboard.moderation import APPROVED_STATUSES, ModerationService
from app.dashboard.repository import DashboardRepository, DuplicateModerator
from app.security.audit import security_event


def streamer_gone() -> ApiError:
    # The creator passed the access check but was deleted before the query ran.
    return ApiError(404, "Streamer not found", "streamer_not_found")


class DashboardService:
    def __init__(self, repo: DashboardRepository, clock: Callable[[], datetime] | None = None) -> None:
        self.repo = repo
        self.clock = clock or (lambda: datetime.now(UTC))
        self.moderation = ModerationService(repo)

    # ── G1 settings ────────────────────────────────────────────────────────
    async def settings(self, access: StreamerAccess) -> DashboardSettings:
        row = await self.repo.settings(access.streamer.id)
        if row is None:
            raise streamer_gone()
        return DashboardSettings(**row)

    async def update_settings(self, access: StreamerAccess, changes: dict) -> SettingsUpdate:
        if not changes:
            raise ApiError(400, "No settings to update", "empty_patch")
        row, changed = await self.repo.update_settings(access.streamer.id, changes)
        if row is None:
            raise streamer_gone()
        if changed:
            security_event("streamer_settings_updated", level=logging.INFO, user_id=access.current.user.id,
                           streamer_id=access.streamer.id, reason=",".join(changed))
        return SettingsUpdate(settings=DashboardSettings(**row), changed=changed)

    # ── G2 donations ───────────────────────────────────────────────────────
    async def donations(self, access: StreamerAccess, *, limit: int, cursor: str | None) -> DonationPage:
        """Paid and approved (or auto-approved) donations, newest first: the legacy dashboard list."""
        rows = await self.repo.donations(access.streamer.id, statuses=APPROVED_STATUSES, limit=limit + 1,
                                         after=v.cursor_position(cursor))
        items, next_cursor, has_more = v.keyset_page(rows, limit)
        return DonationPage(donations=[DonationItem(**r) for r in items], next_cursor=next_cursor, has_more=has_more)

    async def donation_stats(self, access: StreamerAccess) -> DonationStats:
        """Revenue over every paid donation (any moderation status), bucketed by IST day and month."""
        now = self.clock()
        row = await self.repo.donation_stats(access.streamer.id, day=v.ist_day_bounds(now),
                                             month=v.ist_month_bounds(now))
        return DonationStats(**row, timezone=v.TIMEZONE_NAME, as_of=now)

    async def export_donations(self, access: StreamerAccess, start: str | None,
                               end: str | None) -> tuple[str, list[dict]]:
        """(file name, rows) of the paid donations in whole IST days [from, to]. Never truncated: more
        than EXPORT_MAX_ROWS rows is an error (A4)."""
        try:
            first, last = v.export_days(start, end)
        except ValueError as exc:
            raise ApiError(400, str(exc), "bad_export_range") from None
        rows = await self.repo.export_rows(access.streamer.id, start=v.ist_midnight(first),
                                           end=v.ist_midnight(last + timedelta(days=1)),
                                           limit=v.EXPORT_MAX_ROWS + 1)
        if len(rows) > v.EXPORT_MAX_ROWS:
            raise ApiError(400, f"More than {v.EXPORT_MAX_ROWS} donations in this range: choose a shorter range",
                           "export_too_large")
        security_event("donations_exported", level=logging.INFO, user_id=access.current.user.id,
                       streamer_id=access.streamer.id, count=len(rows))
        name = v.file_name_part(access.streamer.slug)
        return f"{name}-donations-{first.isoformat()}-{last.isoformat()}.csv", rows

    # ── G4 moderators ──────────────────────────────────────────────────────
    async def moderators(self, access: StreamerAccess) -> ModeratorList:
        return ModeratorList(moderators=[Moderator(**r) for r in await self.repo.moderators(access.streamer.id)])

    async def add_moderator(self, access: StreamerAccess, body: ModeratorCreate) -> Moderator:
        try:
            row = await self.repo.add_moderator(access.streamer.id, body.mod_name, body.telegram_user_id,
                                                body.discord_user_id)
        except DuplicateModerator as exc:
            platform = "Telegram" if exc.kind == "telegram" else "Discord"
            raise ApiError(409, f"An active moderator with this {platform} ID already exists",
                           f"duplicate_{exc.kind}_moderator") from None
        security_event("moderator_added", level=logging.INFO, user_id=access.current.user.id,
                       streamer_id=access.streamer.id)
        return Moderator(**row)

    async def update_moderator(self, access: StreamerAccess, moderator_id: str,
                               body: ModeratorPermissionsPatch) -> ModeratorPermissionsResult:
        """D-M7: a creator edits an active moderator's role and permissions (new in v2; legacy only had the column
        defaults). Takes effect on the moderator's next Telegram / Discord action (checked at click time)."""
        moderator_id = v.parse_uuid(moderator_id)
        changes = {f: getattr(body, f) for f in body.model_fields_set}
        outcome = await self.repo.update_moderator(access.streamer.id, moderator_id, changes) if moderator_id else None
        if outcome is None:
            raise ApiError(404, "Moderator not found", "moderator_not_found")
        result, changed, row = outcome
        if result == "removed":
            raise ApiError(409, "This moderator has been removed", "moderator_removed")
        if changed:
            security_event("moderator_permissions_changed", level=logging.INFO, user_id=access.current.user.id,
                           streamer_id=access.streamer.id, reason=",".join(changed))
        return ModeratorPermissionsResult(result=result, changed=changed, moderator=Moderator(**row))

    async def remove_moderator(self, access: StreamerAccess, moderator_id: str) -> ModeratorRemoval:
        moderator_id = v.parse_uuid(moderator_id)
        outcome = await self.repo.remove_moderator(access.streamer.id, moderator_id) if moderator_id else None
        if outcome is None:
            raise ApiError(404, "Moderator not found", "moderator_not_found")
        result, row = outcome
        if result == "removed":
            security_event("moderator_removed", level=logging.INFO, user_id=access.current.user.id,
                           streamer_id=access.streamer.id)
        return ModeratorRemoval(result=result, moderator=Moderator(**row))

    # ── G5 terms (a client-side gate: no other API checks acceptance, A9) ───
    async def terms_status(self, user: User) -> TermsStatus:
        if user.role == "admin":  # admins are never asked to accept (legacy behaviour)
            return TermsStatus(current_version=v.TERMS_VERSION, accepted=True, is_admin=True)
        accepted = await self.repo.terms_accepted(user.id, v.TERMS_VERSION)
        return TermsStatus(current_version=v.TERMS_VERSION, accepted=accepted, is_admin=False)

    async def accept_terms(self, user: User, version: str, user_agent: str | None) -> TermsAccepted:
        if version.strip() != v.TERMS_VERSION:
            raise ApiError(409, "Terms version is not current", "terms_version_mismatch")
        if await self.repo.accept_terms(user.id, v.TERMS_VERSION, (user_agent or "")[:v.USER_AGENT_MAX] or None):
            security_event("terms_accepted", level=logging.INFO, user_id=user.id, reason=v.TERMS_VERSION)
        return TermsAccepted(accepted=True, current_version=v.TERMS_VERSION)
