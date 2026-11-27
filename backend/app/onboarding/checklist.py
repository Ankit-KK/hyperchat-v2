"""Onboarding Stage 5 Phase A: the read-only onboarding checklist (docs/onboarding.md).

One facts query per request, in one READ ONLY transaction on the API's own role (hyperchat_app), and one pure
evaluator shared by GET /api/v1/me/onboarding and GET /api/v1/admin/onboarding. Nothing here writes or activates:
activation stays the admin's (POST /api/v1/admin/creators/{id}/activate)
until Stage 5 Phase B (migration 0042, operator switch off by default).

Every condition is evaluated against the creator's CURRENT OWNER (streamers.user_id), never the viewer, so the owner,
a linked account and an admin see the same rows. KYC is the owner's (app.kyc.service). The Razorpay account check
uses this server's configured accounts, as admin activation does (app.admin.repository.change_status).
"""

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from psycopg.rows import dict_row

from app.admin.applications import DELIVERY, invite_delivery
from app.onboarding.images import validated_url

# The §4.1 conditions, in the order the checklist shows them.
CONDITIONS = ("application_approved", "invitation_accepted", "ownership_confirmed", "kyc_approved", "pan_policy",
              "profile_picture", "background", "no_restriction", "razorpay_account", "lifecycle_eligible")
ASSET_PREFIX = "creator-assets/"  # Stage 4A's R2 prefix for creator images

_FACTS = f"""
    SELECT s.id::text AS streamer_id, s.streamer_slug AS slug, s.status, s.kyc_track, s.razorpay_account,
           s.brand_logo_url, s.background_url, s.user_id IS NOT NULL AS claimed,
           (SELECT u.variants -> 0 ->> 'url' FROM public.creator_image_uploads u WHERE u.streamer_id = s.id
              AND u.kind = 'profile' AND u.state = 'ready') AS profile_ready_url,
           (SELECT u.variants -> 0 ->> 'url' FROM public.creator_image_uploads u WHERE u.streamer_id = s.id
              AND u.kind = 'background' AND u.state = 'ready') AS background_ready_url,
           a.id::text AS signup_id, a.review_status,
           (SELECT {DELIVERY} FROM public.user_invites WHERE id = a.invite_id) AS invite_status,
           EXISTS (SELECT 1 FROM public.user_invites i WHERE i.signup_id = a.id AND i.accepted_at IS NOT NULL)
               AS invite_accepted,
           EXISTS (SELECT 1 FROM public.user_invites i WHERE i.signup_id = a.id AND i.accepted_at IS NOT NULL
                   AND i.accepted_user_id = s.user_id) AS owner_is_invitee,
           EXISTS (SELECT 1 FROM public.users u WHERE u.id = s.user_id AND u.is_active) AS owner_active,
           CASE WHEN s.user_id IS NULL OR %(env)s::text IS NULL THEN false
                ELSE public.user_kyc_approved(s.user_id, %(env)s::text) END AS kyc_approved,
           EXISTS (SELECT 1 FROM public.kyc_restrictions r WHERE r.streamer_id = s.id AND r.lifted_at IS NULL)
               AS restricted,
           public.kyc_activation_block(s.id) AS lifecycle_block
    FROM public.streamers s LEFT JOIN public.user_signups a ON a.streamer_id = s.id
"""


def _row(condition: str, met: bool, detail: str | None = None, state: str | None = None) -> dict:
    return {"condition": condition, "state": state or ("ok" if met else "missing"), "detail": detail,
            "blocking": not met}


def image_state(value: str | None, ready_url: str | None = None) -> tuple[str, str | None]:
    """(state, detail). 'ok' only when the pointer is exactly the primary url of the creator's 'ready'
    creator_image_uploads row (migration 0039: validity is the row). An https creator-assets/ pointer without that
    record (a Phase A upload, or a legacy copy) is 'set' with validation_not_recorded and still blocks: the creator
    uploads again. A legacy /assets/... path or any other value is missing (legacy_path); an empty one is missing."""
    if validated_url(value, ready_url):
        return "ok", None
    if not value:
        return "missing", None
    parts = urlsplit(value)
    if parts.scheme == "https" and f"/{ASSET_PREFIX}" in parts.path:
        return "set", "validation_not_recorded"
    return "missing", "legacy_path"


def evaluate(facts: dict, accounts: frozenset[str]) -> dict:
    """One creator's checklist from its facts row. Pure: no database, no settings."""
    claimed = facts["claimed"]
    if not facts["owner_is_invitee"]:
        owner = "no_owner" if not claimed else "owner_not_invitee"
    else:
        owner = None if facts["owner_active"] else "owner_inactive"
    logo, logo_detail = image_state(facts["brand_logo_url"], facts.get("profile_ready_url"))
    background, background_detail = image_state(facts["background_url"], facts.get("background_ready_url"))
    rows = [
        _row("application_approved", facts["review_status"] == "approved",
             None if facts["review_status"] == "approved" else (facts["review_status"] or "no_application")),
        _row("invitation_accepted", facts["invite_accepted"],
             None if facts["invite_accepted"] else (facts["invite_status"] or "no_invitation")),
        _row("ownership_confirmed", owner is None, owner),
        _row("kyc_approved", facts["kyc_approved"], None if facts["kyc_approved"] else
             ("owner_kyc_required" if claimed else "no_owner")),
        # Q6: PAN-only applies to automatic activation; its evidence exists only from Stage 3 (migration 0038).
        _row("pan_policy", False, "pan_evidence_not_recorded", state="policy_pending"),
        _row("profile_picture", logo == "ok", logo_detail, state=logo),
        _row("background", background == "ok", background_detail, state=background),
        _row("no_restriction", not facts["restricted"], "open_restriction" if facts["restricted"] else None,
             state=None if not facts["restricted"] else "blocked"),
        _row("razorpay_account", facts["razorpay_account"] in accounts,
             None if facts["razorpay_account"] in accounts else "account_not_configured"),
        _row("lifecycle_eligible", facts["lifecycle_block"] is None, facts["lifecycle_block"],
             state=None if facts["lifecycle_block"] is None else "blocked"),
    ]
    # Q12: only application-created creators on the standard KYC track qualify for automatic activation.
    qualifies = facts["signup_id"] is not None and facts["kyc_track"] == "standard"
    return {"streamer_id": facts["streamer_id"], "slug": facts["slug"], "status": facts["status"],
            "track": facts["kyc_track"], "qualifies_for_auto_activation": qualifies, "conditions": rows,
            # Never true before Stage 3: the PAN row blocks while its policy is pending.
            "ready_for_activation": qualifies and facts["status"] == "pending" and not any(r["blocking"] for r in rows)}


def blocked_by(entry: dict, condition: str) -> bool:
    return any(r["condition"] == condition and r["blocking"] for r in entry["conditions"])


async def _facts(cur, where: str, params: dict) -> list[dict]:
    await cur.execute(f"{_FACTS} WHERE {where} ORDER BY s.created_at DESC, s.id", params)
    return await cur.fetchall()


@asynccontextmanager
async def _read_only(connection_factory):
    """One connection, one READ ONLY transaction (transaction-scoped: nothing leaks into the pool)."""
    async with connection_factory() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SET TRANSACTION READ ONLY")
        yield cur


async def creator_checklist(connection_factory, *, user_id: str, linked_streamer_id: str | None,
                            environment: str | None, accounts: frozenset[str]) -> list[dict]:
    """The creators the user owns or is linked to (app.authz.repository.streamers_of_user's rule)."""
    async with _read_only(connection_factory) as cur:
        rows = await _facts(cur, "s.user_id = %(uid)s OR (s.id = %(linked)s AND s.user_id IS NOT NULL)",
                            {"uid": user_id, "linked": linked_streamer_id, "env": environment})
    return [evaluate(r, accounts) for r in rows]


async def admin_progress(connection_factory, *, status: str | None, condition: str | None, limit: int, offset: int,
                         environment: str | None, accounts: frozenset[str]) -> dict:
    """Every creator (or one status), newest first, optionally only those a condition blocks; ids and slugs only."""
    async with _read_only(connection_factory) as cur:
        rows = await _facts(cur, "(%(status)s::text IS NULL OR s.status = %(status)s::text)",
                            {"status": status, "env": environment})
        # shortcut: evaluated in Python before paging so blocked_by pages correctly; fine for hundreds of creators,
        # move the filter into SQL (onboarding_activation_reasons, Phase B) if the creator count grows large.
        entries = [(r, evaluate(r, accounts)) for r in rows]
        if condition is not None:
            entries = [(r, e) for r, e in entries if blocked_by(e, condition)]
        page = entries[offset:offset + limit]
        signups = [r["signup_id"] for r, _ in page if r["signup_id"]]
        events: dict[str, list[dict]] = {s: [] for s in signups}
        if signups:
            await cur.execute("SELECT signup_id::text, action, actor_user_id::text, note FROM public.application_events "
                              "WHERE signup_id = ANY(%s::uuid[]) ORDER BY created_at, id", (signups,))
            for e in await cur.fetchall():
                events[e["signup_id"]].append(e)
    return {"total": len(entries), "limit": limit, "offset": offset, "creators": [
        {**e, "signup_id": r["signup_id"],
         "invite_delivery": invite_delivery(r["invite_status"], events[r["signup_id"]]) if r["signup_id"] else None}
        for r, e in page]}
