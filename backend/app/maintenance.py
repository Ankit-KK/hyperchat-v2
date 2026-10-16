"""Housekeeping: delete authentication/security rows that are past their retention window, and donor uploads
that no donation uses (D9).

Counts only are reported; no row data, key or URL is ever printed or logged. Retention keeps a short grace period
for investigating recent incidents. Accepted invites are kept (who invited whom is an audit record).

Abandoned donor uploads (owner decision D9, 2026-10-15): media stays public at unguessable URLs, and an upload that
never became part of a donation is deleted once it is older than 24 hours. Fail-closed: only voice-messages/ and
donation-media/ are listed, only keys matching the server's upload pattern are considered, an object is kept when
it is younger than 24 hours by EITHER its LastModified or the time in its key, and when ANY donation's
voice_message_url or media_url ends in its key (whatever the host: a legacy or not-yet-rewritten URL protects its
object too). A failed listing, a failed reference query or an unusable public base deletes nothing. Paid or
otherwise referenced media is kept indefinitely. A checkout naming an upload more than 24 hours old could race
with the cleanup; uploads are used within minutes (the grant lives 10), so this is accepted.
"""

import re
import sys
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.security.audit import security_event
from app.storage.r2 import UPLOAD_KEY, UPLOAD_PREFIXES, MAX_LISTED_UPLOADS, StorageError, build_object_store

UPLOAD_RETENTION = timedelta(hours=24)
_KEY_TIME = re.compile(r"_(\d{13})_[0-9a-f]{32}\.[a-z]+$")
# The key at the end of a stored donation URL (optional query/fragment), whatever the host.
_REFERENCED = """
    SELECT k FROM unnest(%s::text[]) AS k
    WHERE k IN (SELECT substring(u FROM '((?:voice-messages|donation-media)/[^/?#]+)(?:[?#].*)?$')
                FROM (SELECT voice_message_url AS u FROM public.donations WHERE voice_message_url IS NOT NULL
                      UNION ALL
                      SELECT media_url FROM public.donations WHERE media_url IS NOT NULL) urls)
"""

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
    # Phase 7: the Razorpay order -> donation index (legacy cron cleanup-order-lookup: daily, 30 days). A webhook
    # for an older order still resolves through donations.provider_order_id (PaymentRepository.by_provider_order).
    "order_lookup": ("public.order_lookup", "created_at < now() - interval '30 days'"),
    # Phase 11: bot button rows a week after they expired; delivery records after 30 days.
    "telegram_callback_mapping": ("public.telegram_callback_mapping", "expires_at < now() - interval '7 days'"),
    "discord_callback_mapping": ("public.discord_callback_mapping", "expires_at < now() - interval '7 days'"),
    "moderator_notifications": ("public.moderator_notifications", "created_at < now() - interval '30 days'"),
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
        # KYC PR3: Didit webhook receipts after 90 days, through the database (the API's role cannot DELETE them).
        await cur.execute("SELECT public.kyc_purge_webhook_receipts(%s)", (dry_run,))
        counts["kyc_webhook_events"] = (await cur.fetchone())[0]
    return counts


async def abandoned_uploads(store, connection_factory, *, dry_run: bool,
                            now: datetime | None = None) -> dict[str, int]:
    """Delete (or with dry_run only count) donor uploads older than 24 hours that no donation references. Raises
    (deleting nothing) when the public base is unusable, a listing fails or the reference query fails. One failed
    delete does not stop the others: it is counted in delete_failed."""
    base = store.public_url("")
    if not (isinstance(base, str) and base.startswith("https://") and base.endswith("/") and len(base) > 9):
        raise StorageError("public_base_unusable")
    cutoff = (now or datetime.now(UTC)) - UPLOAD_RETENTION
    listed = []
    for prefix in UPLOAD_PREFIXES:  # every listing first: a failure here deletes nothing
        found = await store.list_uploads(prefix)
        listed.append((prefix, found))
    counts = {"scanned": 0, "kept_unrecognized": 0, "kept_too_new": 0, "kept_referenced": 0, "eligible": 0,
              "deleted": 0, "delete_failed": 0, "listing_capped": 0}
    candidates = []
    for _prefix, found in listed:
        counts["scanned"] += len(found)
        counts["listing_capped"] += len(found) >= MAX_LISTED_UPLOADS
        for obj in found:
            m = _KEY_TIME.search(obj.key) if UPLOAD_KEY.match(obj.key) else None
            if m is None:
                counts["kept_unrecognized"] += 1
                continue
            written = datetime.fromtimestamp(int(m.group(1)) / 1000, UTC)
            if obj.last_modified.tzinfo is None or obj.last_modified > cutoff or written > cutoff:
                counts["kept_too_new"] += 1
                continue
            candidates.append(obj.key)
    referenced: set[str] = set()
    if candidates:
        async with connection_factory() as conn, conn.cursor() as cur:
            await cur.execute(_REFERENCED, (candidates,))
            referenced = {row[0] for row in await cur.fetchall()}
    for key in candidates:
        if key in referenced:
            counts["kept_referenced"] += 1
            continue
        counts["eligible"] += 1
        if dry_run:
            continue
        try:
            await store.delete_upload(key)
            counts["deleted"] += 1
        except (StorageError, ValueError):
            counts["delete_failed"] += 1
    return counts


async def run_cleanup(settings: Settings, connection_factory, *, dry_run: bool, store=None) -> int:
    try:
        counts = await cleanup(connection_factory, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - report the failure class only, never connection details
        print(f"cleanup failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    verb = "would delete" if dry_run else "deleted"
    print(f"cleanup ({settings.db_label}): " + ", ".join(f"{name} {verb} {n}" for name, n in counts.items()))
    if not dry_run:
        security_event("maintenance_cleanup", level=20, count=sum(counts.values()))
    store = store or build_object_store(settings)
    if store is None:
        print("abandoned uploads: skipped (R2 not configured)")
        return 0
    try:
        uploads = await abandoned_uploads(store, connection_factory, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - nothing was deleted; the failure class only
        reason = str(exc)[:64] if isinstance(exc, StorageError) else type(exc).__name__  # reason codes only
        print(f"abandoned uploads: failed before deleting anything ({reason})", file=sys.stderr)
        return 1
    label = "dry run" if dry_run else "done"
    print(f"abandoned uploads ({label}): " + ", ".join(f"{k} {v}" for k, v in uploads.items())
          + (f", would delete {uploads['eligible']}" if dry_run else ""))
    if not dry_run:
        security_event("maintenance_upload_cleanup", level=20 if not uploads["delete_failed"] else 30,
                       count=uploads["deleted"], reason=f"failed={uploads['delete_failed']}")
    return 1 if uploads["delete_failed"] or uploads["listing_capped"] else 0
