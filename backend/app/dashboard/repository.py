"""SQL for the creator dashboard (runs as hyperchat_app; DML only).

Every query is scoped by streamer_id: a donation or moderator id from another creator never matches.
"""

from psycopg.rows import dict_row


class DashboardRepository:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def _all(self, sql: str, params: dict | tuple | None = None) -> list[dict]:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params or None)
            return await cur.fetchall()

    async def _one(self, sql: str, params: dict | tuple | None = None) -> dict | None:
        rows = await self._all(sql, params)
        return rows[0] if rows else None
