"""Automatic creator activation (onboarding Stage 5 Phase B, migration 0042, docs/onboarding.md): a poll loop in the
worker next to the automatic invitations. No job kind and no attempt rows: every pass re-reads the database.

Each pass, while the operator switch (public.onboarding_settings.auto_activation) is on:
  * picks, on the worker's hyperchat_app connection (read only), up to BATCH pending creators whose Razorpay account is
    configured on this server and for which public.onboarding_activation_block is NULL (the one rule: application-
    created on the standard track, owner = the invitee and active, approved KYC with PAN evidence or a payout hold,
    validated profile picture and background, no open restriction);
  * activates each through public.onboarding_auto_activate on the hyperchat_lifecycle connection, which locks the
    creator, re-checks the whole rule and the account under the lock and writes the status, the creator_status_events
    row and the onboarding_activations row in one transaction. It needs no step-up grant: 0042's guard exception
    accepts exactly the transition whose success row that function wrote in the same transaction.
A refusal ('status_changed', a condition that changed between the pick and the lock, ...) changes nothing and is
picked again only while the rule still says yes. Logs carry ids and codes only.

shortcut: no activation email yet; the page is public as soon as the creator is active. Add a mail category when the
owner wants one.
"""

import asyncio
import logging

import psycopg
from psycopg.rows import dict_row

from app.security.audit import security_event

log = logging.getLogger("hyperchat.onboarding")

BATCH = 10  # creators per pass at most
SWITCH = "SELECT auto_activation FROM public.onboarding_settings"
PICK = """
SELECT s.id::text, s.razorpay_account FROM public.streamers s
WHERE s.status = 'pending' AND s.kyc_track = 'standard' AND s.razorpay_account = ANY(%(accounts)s::text[])
  AND NOT (s.id = ANY(%(skip)s::uuid[])) AND public.onboarding_activation_block(s.id) IS NULL
ORDER BY s.created_at, s.id
LIMIT %(batch)s
"""


VIEW = """
SELECT a.streamer_id::text, s.streamer_slug AS slug, s.status, a.created_at, a.pan_status,
       a.payout_hold_id IS NOT NULL AS payout_hold, a.signup_id::text, a.kyc_session_id::text
FROM public.onboarding_activations a JOIN public.streamers s ON s.id = a.streamer_id
ORDER BY a.created_at DESC, a.id LIMIT %(limit)s OFFSET %(offset)s
"""


async def admin_view(connection_factory, *, limit: int, offset: int) -> dict:
    """GET /api/v1/admin/onboarding/auto-activation: the operator switch and the automatic activations, newest first.
    Read only; ids, slugs and codes (the operator's name and approval reference are the switch's own audit)."""
    async with connection_factory() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SET TRANSACTION READ ONLY")
        await cur.execute("SELECT auto_activation, changed_at, changed_by, approval_reference "
                          "FROM public.onboarding_settings")
        switch = await cur.fetchone()
        await cur.execute("SELECT count(*) AS n FROM public.onboarding_activations")
        total = (await cur.fetchone())["n"]
        await cur.execute(VIEW, {"limit": limit, "offset": offset})
        rows = await cur.fetchall()
    return {"enabled": switch["auto_activation"],
            "changed_at": switch["changed_at"].isoformat() if switch["changed_at"] else None,
            "changed_by": switch["changed_by"], "approval_reference": switch["approval_reference"],
            "total": total, "limit": limit, "offset": offset,
            "activations": [{**r, "created_at": r["created_at"].isoformat()} for r in rows]}


class AutoActivator:
    def __init__(self, connection_factory, lifecycle_factory, accounts: frozenset[str], *, poll_seconds: float) -> None:
        self._conn, self._lifecycle, self.accounts, self.poll_seconds = (connection_factory, lifecycle_factory,
                                                                         accounts, poll_seconds)
        self.paused: set[str] = set()  # creators a database guard refused: not retried until a restart

    async def run_once(self) -> list[tuple[str, str]]:
        """One pass; returns (creator id, 'ok' | refusal code | 'paused') per creator tried."""
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(SWITCH)
            row = await cur.fetchone()
            if not (row and row[0]) or not self.accounts:
                return []
            await cur.execute(PICK, {"accounts": sorted(self.accounts), "skip": sorted(self.paused), "batch": BATCH})
            picked = await cur.fetchall()
        return [(sid, await self._activate(sid, account)) for sid, account in picked]

    async def _activate(self, streamer_id: str, account: str) -> str:
        try:
            async with self._lifecycle() as conn, conn.transaction(), conn.cursor() as cur:
                await cur.execute("SELECT public.onboarding_auto_activate(%s, %s)", (streamer_id, account))
                result = (await cur.fetchone())[0]
        except psycopg.errors.InsufficientPrivilege:  # a database guard refused: never expected (migration 0042)
            self.paused.add(streamer_id)
            log.warning("automatic activation refused by a database guard", extra={"streamer_id": streamer_id,
                                                                                    "reason": "guard_refused"})
            return "paused"
        if result == "ok":
            security_event("creator_status_changed", level=20, streamer_id=streamer_id, reason="auto_activate",
                           status="active")
        else:
            log.info("automatic activation not done", extra={"streamer_id": streamer_id, "reason": result})
        return result

    async def run(self, stop: asyncio.Event) -> None:
        log.info("automatic activation started", extra={"poll_s": self.poll_seconds})
        while not stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - e.g. the database is down: try again next pass
                log.warning("automatic activation pass failed", extra={
                    "reason": "database_unavailable" if isinstance(exc, psycopg.Error) else "internal_error",
                    "error": type(exc).__name__})
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
        log.info("automatic activation stopped")
