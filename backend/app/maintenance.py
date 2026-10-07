"""Housekeeping: delete authentication/security rows that are past their retention window.

Counts only are reported; no row data is ever printed or logged. Retention keeps a short grace period
for investigating recent incidents. Accepted invites are kept (who invited whom is an audit record).
"""

import sys

from app.core.config import Settings
from app.security.audit import security_event

RULES: dict[str, tuple[str, str]] = {
    "sessions": ("public.sessions",
                 "revoked_at < now() - interval '7 days' OR absolute_expires_at < now() - interval '7 days' "
                 "OR idle_expires_at < now() - interval '7 days'"),
    "password_reset_tokens": ("public.password_reset_tokens", "expires_at < now() - interval '1 day'"),
    "request_nonces": ("public.request_nonces", "expires_at < now()"),
    "rate_limits": ("public.rate_limits", "window_start < now() - interval '1 day'"),
    "user_invites": ("public.user_invites",
                     "accepted_at IS NULL AND (revoked_at < now() - interval '30 days' "
                     "OR expires_at < now() - interval '30 days')"),
    # Phase 8: finished background jobs (queued/running are never touched) and the TTS moderation audit
    # (legacy kept 90 days).
    "jobs": ("public.jobs", "status IN ('succeeded', 'failed') AND completed_at < now() - interval '30 days'"),
    "tts_moderation_audit": ("public.tts_moderation_audit", "created_at < now() - interval '90 days'"),
    # Phase 9: finished realtime outbox rows (pending/publishing are never touched). Rows finish within minutes
    # of their creation (10-minute expiry), so created_at dates them.
    "realtime_outbox": ("public.realtime_outbox",
                        "status IN ('published', 'failed', 'expired', 'skipped') AND created_at < now() - interval '7 days'"),
}


async def cleanup(connection_factory, *, dry_run: bool) -> dict[str, int]:
    """Delete (or with dry_run only count) expired rows, all in one transaction."""
    counts: dict[str, int] = {}
    async with connection_factory() as conn, conn.transaction(), conn.cursor() as cur:
        for name, (table, condition) in RULES.items():
            if dry_run:
                await cur.execute(f"SELECT count(*) FROM {table} WHERE {condition}")
                counts[name] = (await cur.fetchone())[0]
            else:
                await cur.execute(f"DELETE FROM {table} WHERE {condition}")
                counts[name] = cur.rowcount
    return counts


async def run_cleanup(settings: Settings, connection_factory, *, dry_run: bool) -> int:
    try:
        counts = await cleanup(connection_factory, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - report the failure class only, never connection details
        print(f"cleanup failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    verb = "would delete" if dry_run else "deleted"
    print(f"cleanup ({settings.db_label}): " + ", ".join(f"{name} {verb} {n}" for name, n in counts.items()))
    if not dry_run:
        security_event("maintenance_cleanup", level=20, count=sum(counts.values()))
    return 0
