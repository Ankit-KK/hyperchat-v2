"""Read-only queries for authorization (runs as hyperchat_app)."""

from psycopg.rows import dict_row

from app.authz.access import StreamerRef, StreamerRefQuery

_COLS = "id::text, streamer_slug AS slug, streamer_name AS name, user_id::text"


class AuthzRepository:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def streamer(self, ref: StreamerRefQuery) -> StreamerRef | None:
        if ref.streamer_id:
            where, param = "id = %s", ref.streamer_id
        else:
            where, param = "streamer_slug = %s", ref.slug
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"SELECT {_COLS} FROM public.streamers WHERE {where}", (param,))
            row = await cur.fetchone()
        return StreamerRef(**row) if row else None

    async def streamers_of_user(self, user_id: str, linked_streamer_id: str | None) -> list[StreamerRef]:
        """Creators the user owns (streamers.user_id) or is linked to (users.streamer_id)."""
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"""
                SELECT {_COLS} FROM public.streamers
                WHERE user_id = %s OR (id = %s AND user_id IS NOT NULL)
                ORDER BY streamer_name, streamer_slug
            """, (user_id, linked_streamer_id))
            return [StreamerRef(**row) for row in await cur.fetchall()]
