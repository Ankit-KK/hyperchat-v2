"""Dashboard use-cases. Every rule lives here (or in validation / moderation); the router stays thin."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.authz.dependencies import StreamerAccess
from app.core.errors import ApiError
from app.dashboard import validation as v
from app.dashboard.models import (
    DashboardSettings,
    DonationItem,
    DonationPage,
    DonationStats,
    SettingsUpdate,
)
from app.dashboard.moderation import ModerationService
from app.dashboard.repository import DashboardRepository
from app.security.audit import security_event

APPROVED = ("approved", "auto_approved")


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
        rows = await self.repo.donations(access.streamer.id, statuses=APPROVED, limit=limit + 1,
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
        return f"{access.streamer.slug}-donations-{first.isoformat()}-{last.isoformat()}.csv", rows
