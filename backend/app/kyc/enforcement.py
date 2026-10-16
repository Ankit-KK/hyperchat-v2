"""KYC enforcement administration (KYC PR4, migration 0023; docs/kyc-rollout.md).

Read-only: the enforcement record and rollout readiness, the impact preview, the audit, one creator's eligibility.
Changes through the API are only the fail-closed ones: add a restriction (it also revokes the creator's grace) and
revoke grace; they stay allowed while a restore is suspected (they are how lost revocations are re-applied) but never
re-anchor the witness. Lifting a restriction, activation, closure and grace grants are NOT here: they loosen
eligibility, need the operator's approvals and run with the migration role (app.kyc.operator); the application's
database credentials cannot do them either (no grant).
"""

import uuid

from psycopg.rows import dict_row

from app.core.errors import ApiError
from app.kyc.witness import KycWitness
from app.security.audit import security_event

RESTRICTION_REASONS = ("identity_concern", "owner_dispute", "fraud_review", "kyc_lost", "admin_hold")
_STATE_COLS = """state, generation, grace_max_days, policy, activated_at, activated_by, activation_authorization,
    activation_migration, closed_at, closed_by, closure_authorization"""


def _iso(row: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else str(v) if isinstance(v, uuid.UUID) else v)
            for k, v in row.items()}


def _creator_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ApiError(404, "Creator not found", "creator_not_found") from None


class EnforcementRepository:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, args)
            return await cur.fetchall()

    async def state(self) -> dict:
        return (await self._rows(f"SELECT {_STATE_COLS} FROM public.kyc_enforcement"))[0]

    async def head(self, anchor_generation: int) -> dict:
        """state, generation, chain (the head) and the chain at the witness's anchor generation."""
        return (await self._rows("SELECT e.state, e.generation, e.chain, (SELECT ev.chain FROM "
                                 "public.kyc_enforcement_events ev WHERE ev.generation = %s) AS anchor_chain "
                                 "FROM public.kyc_enforcement e", (anchor_generation,)))[0]

    async def counts(self) -> dict:
        return (await self._rows("""
            SELECT (SELECT count(*) FROM public.kyc_grace_grants
                    WHERE revoked_at IS NULL AND expires_at > clock_timestamp()) AS open_grace_grants,
                   (SELECT count(*) FROM public.kyc_restrictions WHERE lifted_at IS NULL) AS open_restrictions
        """))[0]

    async def preview(self) -> list[dict]:
        return await self._rows("SELECT * FROM public.kyc_enforcement_preview()")

    async def events(self, streamer_id: str | None, limit: int, offset: int) -> list[dict]:
        return await self._rows("""
            SELECT action, streamer_id, grant_id, restriction_id, actor_user_id, operator, detail, generation, created_at
            FROM public.kyc_enforcement_events WHERE (%(s)s::uuid IS NULL OR streamer_id = %(s)s::uuid)
            ORDER BY created_at DESC, id DESC LIMIT %(l)s OFFSET %(o)s
        """, {"s": streamer_id, "l": limit, "o": offset})

    async def creator(self, streamer_id: str) -> dict | None:
        rows = await self._rows("SELECT * FROM public.kyc_enforcement_preview() WHERE streamer_id = %s",
                                (streamer_id,))
        if not rows:
            return None
        restrictions = await self._rows("""
            SELECT id, reason, note, created_by, created_at, lifted_at, lifted_by, lift_note
            FROM public.kyc_restrictions WHERE streamer_id = %s ORDER BY created_at, id""", (streamer_id,))
        grants = await self._rows("""
            SELECT id, owner_id, granted_at, expires_at, granted_by, reason, revoked_at, revoked_by, revoke_reason
            FROM public.kyc_grace_grants WHERE streamer_id = %s ORDER BY granted_at, id""", (streamer_id,))
        return {**rows[0], "restrictions": restrictions, "grace_grants": grants}

    async def call(self, sql: str, args: tuple):
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(sql, args)
            return (await cur.fetchone())[0]


class EnforcementService:
    def __init__(self, repo: EnforcementRepository, witness: KycWitness) -> None:
        self.repo, self.witness = repo, witness

    async def check(self) -> tuple[dict, str | None]:
        head = await self.repo.head(self.witness.anchor_generation())
        return head, self.witness.problem(head["state"], head["generation"], head["anchor_chain"])

    async def sync_witness(self, **event) -> None:
        """Record the database's head outside PostgreSQL, only as an extension of the witnessed history: a database
        that diverged from it (a restore) is never re-anchored here (that is the operator's acknowledgement)."""
        head, problem = await self.check()
        if problem is None:
            self.witness.record(head["state"], head["generation"], head["chain"], **event)
        elif self.witness.path is not None:
            security_event("kyc_enforcement_witness_mismatch", level=50, reason=problem)

    def _require_witness(self) -> None:
        if self.witness.path is None:  # an unwitnessed change could be undone by a restore without anyone noticing
            raise ApiError(503, "KYC enforcement changes need the restore witness (HYPERCHAT_KYC_WITNESS_PATH)",
                           "kyc_witness_not_configured")

    async def status(self) -> dict:
        state = await self.repo.state()
        _, problem = await self.check()
        return {"enforcement": _iso(state), **await self.repo.counts(),
                "witness": {"configured": self.witness.path is not None, "problem": problem},
                "new_donations_fail_closed": problem is not None}

    async def preview(self) -> dict:
        rows = [_iso(r) for r in await self.repo.preview()]
        would_lose = [r for r in rows if r["block_now"] is None and r["block_when_active"] is not None]
        return {"creators": rows, "summary": {
            "creators": len(rows),
            "eligible_now": sum(r["block_now"] is None for r in rows),
            "eligible_when_active": sum(r["block_when_active"] is None for r in rows),
            "eligible_when_closed": sum(r["block_when_closed"] is None for r in rows),
            "would_lose_new_donations_when_active": len(would_lose),
            "grace_candidates": sum(r["kyc_track"] == "legacy" and r["status"] == "active" and r["owner_is_baseline"]
                                    and not r["owner_kyc_approved"] and not r["open_restrictions"] for r in rows),
            "without_owner": sum(r["owner_id"] is None for r in rows),
            "owner_changed": sum(r["kyc_track"] == "legacy" and r["owner_id"] is not None
                                 and not r["owner_is_baseline"] for r in rows)}}

    async def history(self, streamer_id: str | None, limit: int, offset: int) -> dict:
        sid = _creator_id(streamer_id) if streamer_id else None
        return {"events": [_iso(e) for e in await self.repo.events(sid, limit, offset)]}

    async def creator(self, streamer_id: str) -> dict:
        view = await self.repo.creator(_creator_id(streamer_id))
        if view is None:
            raise ApiError(404, "Creator not found", "creator_not_found")
        return {**_iso({k: v for k, v in view.items() if k not in ("restrictions", "grace_grants")}),
                "restrictions": [_iso(r) for r in view["restrictions"]],
                "grace_grants": [_iso(g) for g in view["grace_grants"]]}

    async def restrict(self, admin_id: str, streamer_id: str, reason: str, note: str | None, ip: str | None) -> dict:
        streamer_id = _creator_id(streamer_id)
        if reason not in RESTRICTION_REASONS:
            raise ApiError(400, "Unknown restriction reason", "bad_reason")
        self._require_witness()
        restriction = await self.repo.call("SELECT public.kyc_restrict_creator(%s, %s, %s, %s)",
                                           (streamer_id, reason, note, admin_id))
        if restriction is None:
            raise ApiError(404, "Creator not found", "creator_not_found")
        await self.sync_witness(action="restriction_added", streamer_id=streamer_id)
        security_event("kyc_creator_restricted", level=20, user_id=admin_id, streamer_id=streamer_id, reason=reason,
                       client_ip=ip)
        return await self.creator(streamer_id)

    async def revoke_grace(self, admin_id: str, streamer_id: str, reason: str, ip: str | None) -> dict:
        streamer_id, reason = _creator_id(streamer_id), " ".join((reason or "").split())
        if not reason:
            raise ApiError(400, "A reason is required to revoke grace", "reason_required")
        self._require_witness()
        revoked = await self.repo.call("SELECT public.kyc_revoke_grace(%s, %s, %s)", (streamer_id, reason, admin_id))
        if revoked:
            await self.sync_witness(action="grace_revoked", streamer_id=streamer_id)
            security_event("kyc_grace_revoked", level=20, user_id=admin_id, streamer_id=streamer_id, count=revoked,
                           client_ip=ip)
        return {**await self.creator(streamer_id), "revoked": revoked}
