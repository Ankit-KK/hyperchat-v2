"""Dashboard use-cases. Every rule lives here (or in validation / moderation); the router stays thin."""

from collections.abc import Callable
from datetime import UTC, datetime

from app.dashboard.moderation import ModerationService
from app.dashboard.repository import DashboardRepository


class DashboardService:
    def __init__(self, repo: DashboardRepository, clock: Callable[[], datetime] | None = None) -> None:
        self.repo = repo
        self.clock = clock or (lambda: datetime.now(UTC))
        self.moderation = ModerationService(repo)
