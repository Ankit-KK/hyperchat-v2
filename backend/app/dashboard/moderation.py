"""Dashboard moderation: the donation state machine and its use-cases.

The legacy moderate-donation function is not ported. Moderation changes database state only: no
alerts, TTS, Telegram/Discord, Pusher, OBS or replay calls (later phases react to the stored state,
e.g. audio_scheduled_at). Every action runs in one transaction in DashboardRepository.moderate, which also
writes the donation_audio job (Phase 8) and the realtime outbox events (Phase 9) with it.
"""

import logging
from dataclasses import dataclass
from typing import Literal

from app.authz.dependencies import StreamerAccess
from app.core.errors import ApiError
from app.dashboard import validation as v
from app.dashboard.models import BannedDonor, BannedDonorList, ModerationResult, QueueItem, QueuePage, UnbanResult
from app.security.audit import security_event

ACTIONS = ("approve", "reject", "hide_message", "unhide_message", "ban_donor")
STATUSES = ("pending", "approved", "auto_approved", "rejected")
APPROVED_STATUSES = ("approved", "auto_approved")
CREDITED = frozenset(APPROVED_STATUSES)  # included in streamer_donator_totals
QUEUE_FILTERS = {"pending": ("pending",), "approved": APPROVED_STATUSES, "rejected": ("rejected",), "all": None}
DASHBOARD_LABEL = "Dashboard"
BANNED_LIST_LIMIT = 500  # active bans returned at once (a creator with more sees `truncated`)


class NotPaid(Exception):
    """Only paid donations (payment_status = 'success') can be moderated."""


@dataclass(frozen=True)
class Step:
    """What one action does to one donation. "unchanged" means nothing is written at all."""

    result: Literal["applied", "unchanged"]
    new_status: str
    message_visible: bool
    approve: bool = False  # stamp approved_at, audio_scheduled_at and approved_by_label
    totals_delta: int = 0  # +1 credit / -1 debit of the donor's total (amount_inr and count)
    ban: bool = False      # add an active ban for the donor name


def transition(action: str, *, payment_status: str, moderation_status: str, message_visible: bool,
               donor_banned: bool) -> Step:
    """The state machine (pure).

        current \\ action   approve             reject                  ban_donor
        pending            approved + credit   rejected, hide          ban + rejected, hide
        approved           unchanged           rejected, hide, debit   ban + rejected, hide, debit
        auto_approved      unchanged (A5)      rejected, hide, debit   ban + rejected, hide, debit
        rejected           approved + credit   unchanged               ban (status stays), unchanged if banned

    hide_message / unhide_message only flip message_visible (unchanged when already so). A ban is
    only added when the donor has no active ban. Unpaid donations raise NotPaid for every action.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    if moderation_status not in STATUSES:
        raise ValueError(f"unknown moderation status {moderation_status!r}")
    if payment_status != "success":
        raise NotPaid()
    same = Step("unchanged", moderation_status, message_visible)
    debit = -1 if moderation_status in CREDITED else 0
    if action == "approve":
        if moderation_status in CREDITED:
            return same
        return Step("applied", "approved", message_visible, approve=True, totals_delta=1)
    if action == "reject":
        if moderation_status == "rejected":
            return same
        return Step("applied", "rejected", False, totals_delta=debit)
    if action in ("hide_message", "unhide_message"):
        visible = action == "unhide_message"
        return same if message_visible == visible else Step("applied", moderation_status, visible)
    if moderation_status == "rejected":  # ban_donor on a rejected donation: only the ban is new
        return same if donor_banned else Step("applied", "rejected", message_visible, ban=True)
    return Step("applied", "rejected", False, totals_delta=debit, ban=not donor_banned)


def actor_label(user) -> str:
    """approved_by_label / moderator_name / banned_by_name: the username, else "Dashboard"; never the
    email (A6)."""
    return (user.username or "").strip() or DASHBOARD_LABEL


class ModerationService:
    def __init__(self, repo) -> None:
        self.repo = repo

    async def queue(self, access: StreamerAccess, *, status: str, limit: int, cursor: str | None) -> QueuePage:
        """Paid donations by moderation status (approved = approved + auto_approved), newest first."""
        rows = await self.repo.donations(access.streamer.id, statuses=QUEUE_FILTERS[status], limit=limit + 1,
                                         after=v.cursor_position(cursor), queue=True)
        items, next_cursor, has_more = v.keyset_page(rows, limit)
        pending = await self.repo.pending_count(access.streamer.id)
        return QueuePage(donations=[QueueItem(**r) for r in items], pending_count=pending,
                         next_cursor=next_cursor, has_more=has_more)

    async def banned_donors(self, access: StreamerAccess) -> BannedDonorList:
        """D-R8: the creator's active donor bans, newest first."""
        rows, total = await self.repo.banned_donors(access.streamer.id, BANNED_LIST_LIMIT)
        return BannedDonorList(banned_donors=[BannedDonor(**r) for r in rows], total=total,
                               truncated=total > len(rows))

    async def unban(self, access: StreamerAccess, ban_id: str) -> UnbanResult:
        """D-M6: lift a ban by id (replaces legacy's per-donation unban_donor, A3). Every active ban of that donor
        name for this creator is lifted; a ban of another creator is 404."""
        ban_id = v.parse_uuid(ban_id)
        if ban_id is None:
            raise ApiError(404, "Ban not found", "ban_not_found")
        outcome = await self.repo.unban(access.streamer.id, ban_id, actor_user_id=access.current.user.id,
                                        actor_label=actor_label(access.current.user))
        if outcome is None:
            raise ApiError(404, "Ban not found", "ban_not_found")
        lifted, row = outcome
        security_event("donor_unbanned" if lifted else "donor_unban_unchanged", level=logging.INFO,
                       user_id=access.current.user.id, streamer_id=access.streamer.id, count=lifted)
        return UnbanResult(result="applied" if lifted else "unchanged", bans_lifted=lifted, ban=BannedDonor(**row))

    async def moderate(self, access: StreamerAccess, donation_id: str, action: str,
                       reason: str | None) -> ModerationResult:
        donation_id = v.parse_uuid(donation_id)
        if donation_id is None:
            raise ApiError(404, "Donation not found", "donation_not_found")
        notes = ((reason or "").strip() or None) if action == "ban_donor" else None
        try:
            outcome = await self.repo.moderate(access.streamer.id, donation_id, action,
                                               actor_user_id=access.current.user.id,
                                               actor_label=actor_label(access.current.user), notes=notes)
        except NotPaid:
            raise ApiError(409, "Donation is not paid", "donation_not_paid") from None
        if outcome is None:
            raise ApiError(404, "Donation not found", "donation_not_found")
        previous_status, step, row = outcome
        return ModerationResult(result=step.result, action=action, previous_status=previous_status,
                                new_status=step.new_status, donation=QueueItem(**row))
