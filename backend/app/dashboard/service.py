"""Dashboard use-cases. Every rule lives here (or in validation / moderation); the router stays thin."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from app.authz.dependencies import StreamerAccess
from app.core.errors import ApiError
from app.dashboard.models import DashboardSettings, SettingsUpdate
from app.dashboard.moderation import ModerationService
from app.dashboard.repository import DashboardRepository
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
