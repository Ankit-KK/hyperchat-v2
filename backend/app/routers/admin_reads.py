"""/api/v1/admin read-only views for operators: creators, donations, payment integrity and jobs (admins only).

GET only, so no CSRF or step-up. Every answer is `Cache-Control: no-store`. Nothing here returns a key, token, hash,
password, job payload or job result.
"""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response

from app import alerts
from app.auth.dependencies import CurrentUser, require_admin
from app.auth.service import AuthError
from app.core.config import get_settings
from app.dashboard import validation as v
from app.dashboard.models import DonationItem, DonationPage
from app.db.pool import single_connection_factory
from app.jobs.queue import KINDS, JobQueue
from app.onboarding import checklist
from app.payments import integrity
from app.payments.repository import PaymentRepository
from app.routers.admin import admin_service


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(_no_store)])


class AdminDonationItem(DonationItem):
    streamer_slug: str


class AdminDonationPage(DonationPage):
    donations: list[AdminDonationItem]


@router.get("/creators")
async def list_creators(request: Request, q: str | None = Query(None, max_length=200),
                        status: Literal["pending", "active", "rejected", "suspended"] | None = None,
                        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=100_000),
                        current: CurrentUser = Depends(require_admin)) -> dict:
    """Creators by slug; `q` matches the slug or name."""
    rows, total = await admin_service(request).repo.list_creators(
        query=(q or "").strip() or None, status=status, limit=limit, offset=offset)
    return {"total": total, "limit": limit, "offset": offset, "creators": rows}


@router.get("/donations", response_model=AdminDonationPage)
async def list_donations(request: Request, streamer: str | None = Query(None, max_length=64),
                         limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
                         current: CurrentUser = Depends(require_admin)) -> AdminDonationPage:
    """Paid donations of every creator (or one, `streamer` = slug), newest first, keyset paged by `cursor`."""
    rows = await admin_service(request).repo.list_donations(
        streamer_slug=streamer.strip().lower() if streamer else None, limit=limit + 1,
        after=v.cursor_position(cursor))
    items, next_cursor, has_more = v.keyset_page(rows, limit)
    return AdminDonationPage(donations=[AdminDonationItem(**r) for r in items], next_cursor=next_cursor,
                             has_more=has_more)


@router.get("/payments/integrity")
async def payment_integrity(request: Request, since: date | None = None,
                            current: CurrentUser = Depends(require_admin)) -> dict:
    """The payment-integrity-report CLI's report (disputes, refunds, accounting holds, refund snapshots), read-only,
    plus `needs_attention` (the CLI's exit 1)."""
    admin_service(request)  # 503 without a database
    # A short-lived connection of its own: the report sets its session READ ONLY, which must never reach the pool.
    repo = PaymentRepository(single_connection_factory(get_settings().db_conninfo()))
    summary = integrity.report(await repo.integrity_report(since), await repo.accounting_report(),
                               await repo.refund_snapshot_report())
    return {"read_only": True, "since": since.isoformat() if since else None, **summary,
            "needs_attention": integrity.needs_attention(summary)}


@router.get("/jobs")
async def list_jobs(request: Request, status: Literal["queued", "running", "succeeded", "failed"] = "failed",
                    kind: str | None = None, limit: int = Query(50, ge=1, le=200),
                    offset: int = Query(0, ge=0, le=100_000), current: CurrentUser = Depends(require_admin)) -> dict:
    """Background jobs in one status (default failed), most recently finished first; never payloads or results."""
    if kind is not None and kind not in KINDS:
        raise AuthError(400, "Invalid job kind", "bad_kind")
    admin_service(request)  # 503 without a database
    rows = await JobQueue(request.app.state.db.connection).list(status=status, kind=kind, limit=limit, offset=offset)
    return {"limit": limit, "offset": offset, "jobs": rows}


@router.get("/alerts")
async def list_alerts(request: Request, kind: str | None = None, delivered: bool | None = None,
                      limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=100_000),
                      current: CurrentUser = Depends(require_admin)) -> dict:
    """Operator failure alerts (docs/alerting.md), newest first: source, severity, kind, reason code, entity ids,
    created_at and notified_at (null = not delivered); optionally one kind and/or one delivery state."""
    if kind is not None and kind not in alerts.KINDS:
        raise AuthError(400, "Invalid alert kind", "bad_kind")
    admin_service(request)  # 503 without a database
    rows = await alerts.Alerts(request.app.state.db.connection).list(kind=kind, delivered=delivered, limit=limit,
                                                                     offset=offset)
    return {"limit": limit, "offset": offset, "alerts": rows}


@router.get("/onboarding")
async def onboarding_progress(request: Request, status: Literal["pending", "active"] | None = None,
                              blocked_by: str | None = None, limit: int = Query(50, ge=1, le=200),
                              offset: int = Query(0, ge=0, le=100_000),
                              current: CurrentUser = Depends(require_admin)) -> dict:
    """Onboarding progress (Stage 5 Phase A, read-only): every creator's checklist (app.onboarding.checklist), its
    application id and invitation delivery; optionally one status and/or only creators a condition blocks. Ids and
    slugs only: never the applicant's name, email or phone."""
    if blocked_by is not None and blocked_by not in checklist.CONDITIONS:
        raise AuthError(400, "blocked_by must be one of: " + ", ".join(checklist.CONDITIONS), "bad_condition")
    admin = admin_service(request)  # 503 without a database
    kyc = getattr(request.app.state, "kyc", None)
    return await checklist.admin_progress(
        request.app.state.db.connection, status=status, condition=blocked_by, limit=limit, offset=offset,
        environment=kyc.environment if kyc else None, accounts=admin.payment_accounts)
