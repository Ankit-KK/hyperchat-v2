"""PostgreSQL connection pool (psycopg 3, async)."""

import logging

from psycopg_pool import AsyncConnectionPool

from app.core.config import Settings

log = logging.getLogger("hyperchat.db")


class Database:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: AsyncConnectionPool | None = None

    @property
    def enabled(self) -> bool:
        return self._settings.db_enabled

    async def open(self) -> None:
        if not self.enabled:
            log.warning("database disabled by configuration")
            return
        s = self._settings
        self._pool = AsyncConnectionPool(
            conninfo=s.db_conninfo(),
            min_size=s.db_pool_min,
            max_size=s.db_pool_max,
            open=False,
            name="hyperchat-api",
        )
        # wait=False: the API starts (and /health answers) even if the database is down.
        await self._pool.open(wait=False)
        log.info(
            "database pool opened",
            extra={"db_label": s.db_label, "db_name": s.db_name, "db_port": s.db_port},
        )

    def connection(self, timeout: float = 5.0):
        """Async context manager yielding a pooled connection."""
        if self._pool is None:
            raise RuntimeError("database is not enabled")
        return self._pool.connection(timeout=timeout)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def ping(self, timeout: float = 2.0) -> str:
        """Return "ok", "unavailable" or "disabled". Never raises."""
        if not self.enabled or self._pool is None:
            return "disabled"
        try:
            async with self._pool.connection(timeout=timeout) as conn:
                await conn.execute("SELECT 1")
            return "ok"
        except Exception as exc:  # noqa: BLE001 - health must not fail on DB errors
            log.warning("database ping failed", extra={"error": type(exc).__name__})
            return "unavailable"
