"""PAY-15 (D17) stage 6a-2: the read-only accounting verifier for migration 0034's data (plan revision 10).

One REPEATABLE READ READ ONLY transaction as hyperchat_readonly, SELECT statements only: no DML, DDL, temporary
tables, SECURITY DEFINER calls or advisory locks (STOP-2). It checks I1-I15 (incl. Erratum 1's I3-O and live-bucket
I6), the I12 agreement rules with their audited exceptions and the I12-H historical derivation, and classifies every
V2 donation with accounting relevance (§4-§8). The report is canonical JSON with a SHA-256 digest; its status maps to
the D-7 exit codes clean 0 / violation 1 / refused 2 / attention 3 through the closed class table SEVERITY (§9.1).

0034's data cannot prove a transition's commit time (L-2 / L-2b: event timestamps are transaction starts), so this
verifier NEVER asserts a missed evaluation or an overdue 24 h reconciliation (C-IR2): those deadlines are reported as
attention (boundary_uncertain / deadline_uncertain). Reconciliation is decided by the state observed under the state
lock (ev.state = 'active') with the fingerprint and I12, never by evaluated_at (C-IR5). Nothing here reads a 0035
object (receipts, observations, epochs): 6a-2 runs unchanged on a 0034 database.
"""

import hashlib
import itertools
import json
import math
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from app.payments import accounting, figures

VERIFIER_VERSION = "6a-2.1"
EXPECTED_HEAD = "0034"
MIN_SERVER = 150000
READONLY_ROLE = "hyperchat_readonly"

MARGIN = timedelta(minutes=5)       # D-15 (approved 2026-11-15): legacy timing margin M, 0034 rows only
MISS_AFTER = timedelta(minutes=10)  # an uncovered origin while active
WINDOW = timedelta(hours=24)        # D-9: the backlog reconciliation window
R_MAX = Decimal("0.0005")           # A-CLK maximum clock slew, 500 ppm (C-IR7)

EXIT_CODES = {"clean": 0, "violation": 1, "refused": 2, "attention": 3}  # D-7 (approved 2026-11-15)

# §9.1, the 6a-2 rows: every class literal this module emits, exactly once. An unknown class -> refused.
SEVERITY = {
    "structural": "violation",
    "audit_integrity": "violation",
    "missed_evaluation": "violation",
    "overdue_reconciliation": "violation",
    "stale_hold": "violation",
    "indeterminate": "violation",
    "pending_reconciliation": "attention",
    "awaiting_reconciliation": "attention",
    "in_flight": "attention",
    "boundary_uncertain": "attention",
    "deadline_uncertain": "attention",
    "historical_ambiguous": "attention",
    "legacy_uncertain": "attention",
    "unscoped_unused_run": "attention",
    "excluded_bucket_net_credit": "attention",
    "deleted_creator_with_accounting_history": "attention",
    "processed": "processed",
}
RANK = {"processed": 0, "record": 0, "attention": 1, "violation": 2}
STATUS_OF = {0: "clean", 1: "attention", 2: "violation"}

ACCOUNTING_CAUSES = ("evaluation", "hold", "resolved", "manual", "activation", "revert")
HOLD_CODES = frozenset(accounting.HOLDS + (accounting.DISPUTE_MODE_HOLD,))
FLAG_CODES = frozenset({"F-BOTH", "F-OPEN"})
RUN_COMMAND = {"activation": "reconcile", "revert": "revert", "keep": "resolve-keep", "apply": "resolve-apply"}
RUNTIME_ROLES = ("hyperchat_app", "hyperchat_worker", "hyperchat_auth", "hyperchat_lifecycle", "hyperchat_stepup",
                 "hyperchat_readonly")

LIMITATIONS = (
    "L-1: a missed evaluation later covered by another evaluation leaves no trace in 0034's data (not detected).",
    "L-2: a transition recorded in a long transaction carries its start time (created_at), not its lock time.",
    "L-2b: a transition's commit time is unbounded on 0034 data; missed and overdue deadlines are never asserted.",
    "D5-1: evidence commit order cannot be reconstructed from 0034's data (random uuid ids; recorded_at is a "
    "transaction start), so the evidence state any accounting row or keep saw is unknown.",
    "D5-2: an I12-H exact result proves arithmetic consistency of the stored amount with its refs under the S2 "
    "rules, not that the evidence state existed when the row was written; a false historical pass is possible.",
    "D5-3: some self-consistent forged historical attribution can remain undetected by I12-H; I4 and I12 may catch "
    "such tampering but do not guarantee it.",
    "D5-4: no 0034 keep acknowledgement can be proven (A1): it never grants an exception, and migration 0035 cannot "
    "retroactively prove it.",
    "D5-5: the evidence enumeration is bounded (12 events per ref, 4096 states); beyond it the result is uncertain, "
    "never exact and never forged.",
)
NOT_ATTESTED = ("the 6a-3 second report-only reconcile plans zero changes (plan §4.3)",)
GATE_KEYS = ("reconcile_precondition", "recovery_precondition", "activation_verification_ready",
             "activation_complete")

# I10: what migrations 0029-0034 install (live catalog, PG 15 and 17; prosrc is version-independent).
EXPECTED_TRIGGERS = {
    ("donation_credit_events", "donation_credit_events_guard"),
    ("donation_credit_events", "donation_credit_events_guard_truncate"),
    ("donations", "donations_credit_delete_guard"),
    ("donations", "donations_credit_delete_guard_truncate"),
    ("donations", "donations_credit_writer"),
    ("donations", "donations_protect_history_columns"),
    ("payment_accounting_events", "payment_accounting_events_guard"),
    ("payment_accounting_events", "payment_accounting_events_guard_truncate"),
    ("payment_accounting_runs", "payment_accounting_runs_guard"),
    ("payment_accounting_runs", "payment_accounting_runs_guard_truncate"),
    ("payment_accounting_state", "payment_accounting_state_guard"),
    ("payment_accounting_state", "payment_accounting_state_guard_truncate"),
    ("payment_integrity_events", "payment_integrity_events_guard"),
    ("payment_integrity_events", "payment_integrity_events_guard_truncate"),
    ("payment_webhook_events", "payment_webhook_events_guard"),
    ("payment_webhook_events", "payment_webhook_events_guard_truncate"),
}
# md5(prosrc) of the 0034 head (the writer rule, the core and its guards): any drift is structural.
FUNCTION_PINS = {
    "donation_accounting_apply": "5193c012c6ae5531d75200b5b60e8f2f",
    "donation_accounting_core": "b1ff631bb77a2cae673d937fc706529f",
    "donation_credit_events_guard": "2e3114a00f2445b0d6fabab4c54ceac3",
    "donations_credit_delete_guard": "4ff30aea4df651a5aa6c62489f58b627",
    "donations_credit_writer": "f9402b6992a826412119ccaf2de53e2e",
    "donations_protect_history_columns": "b77b98e433360b1623936664fe7281df",
    "payment_accounting_append_only": "34b93ed672f22d783ab60e9108875062",
    "payment_accounting_apply_evaluation": "89567f946979bc6a9ce2563085cb3df0",
    "payment_accounting_keep": "e7ee9329c0284b9e473ac93461873e4e",
    "payment_accounting_lock_state": "f05a3d53a49cf64e2431680683308318",
    "payment_accounting_open_run": "085e5844b4da9a2a719d1e1a5b553cbe",
    "payment_accounting_reconcile": "ccd258cffc4a9efa27f8b318a493c644",
    "payment_accounting_revert": "c14ab1cd3f9181670cbbf9b34c82e9d8",
    "payment_accounting_set_state": "168bbcbe25cb0a5fcc6f15bfd720dde1",
    "payment_accounting_state_guard": "d248d1851c140236c3035244075d191f",
    "payment_evidence_fp": "5b2e29e7a92e9074a40ea9559791b5d1",
    "payment_evidence_guard": "c3612b809be594eec6a03f210ca2aee2",
}
DEFINER_FUNCTIONS = ("donation_accounting_apply", "donation_accounting_core", "donations_credit_writer",
                     "payment_accounting_apply_evaluation", "payment_accounting_keep", "payment_accounting_lock_state",
                     "payment_accounting_open_run", "payment_accounting_reconcile", "payment_accounting_revert",
                     "payment_accounting_set_state", "payment_evidence_fp")
APP_EXECUTE = {"donation_accounting_apply", "payment_accounting_lock_state"}  # hyperchat_app only; nobody else
NO_RUNTIME_WRITES = ("donation_credit_events", "donation_reversals", "donation_accounting_evaluations",
                     "payment_accounting_state", "payment_accounting_events", "payment_accounting_runs",
                     "streamer_donator_totals")
APPEND_ONLY_EVIDENCE = ("payment_integrity_events", "payment_webhook_events")  # app INSERT only


class Refused(Exception):
    """Verification cannot be performed safely: no invariant output, every gate false."""


@dataclass(frozen=True)
class Finding:
    invariant: str
    cls: str
    entity_kind: str
    entity_id: str
    detail: dict = field(default_factory=dict, compare=False)

    def as_dict(self) -> dict:
        return {"invariant": self.invariant, "class": self.cls, "entity_kind": self.entity_kind,
                "entity_id": self.entity_id, "detail": self.detail}


# ── the SQL (every statement a SELECT / WITH; checked by _select) ─────────────────────────────────────────────────

FIRST = """SELECT transaction_timestamp() AS t_v, clock_timestamp() AS c_v, pg_current_snapshot()::text AS snapshot,
    current_setting('transaction_isolation') AS isolation, current_setting('transaction_read_only') AS read_only,
    session_user::text AS session_user, current_user::text AS current_user, current_database()::text AS database,
    current_setting('server_version_num')::int AS server_version_num,
    (SELECT string_agg(version_num, ',') FROM public.alembic_version) AS head"""

# the repository's V2 checkout provenance, verbatim (repository.V2_PROVENANCE; 0032 checks the same)
V2_PROVENANCE = ("d.legacy_source_table IS NULL AND d.payment_account IS NOT NULL "
                 "AND d.provider_order_id IS NOT NULL")
WRITER_TARGET = """(CASE WHEN d.payment_status = 'success' AND d.moderation_status IN ('approved', 'auto_approved')
    THEN COALESCE(d.creditable_inr, d.amount_inr)
         - CASE WHEN COALESCE(dr.applied_reversed_paise, 0) > 0
                THEN floor(COALESCE(d.creditable_inr, d.amount_inr) * 100 * dr.applied_reversed_paise
                           / ((d.amount * 100)::bigint)) / 100 ELSE 0 END
    ELSE 0 END)::numeric(12,2)"""  # 0032's donations_credit_writer target, verbatim arithmetic

DONATIONS = f"""SELECT d.id::text AS id, d.streamer_id::text AS streamer_id, lower(d.donor_name) AS name_lower,
    d.amount, d.amount_inr, d.creditable_inr, d.credited_inr, d.currency::text AS currency, d.payment_status,
    d.moderation_status, d.provider_payment_id, d.legacy_source_table, d.payment_account, d.provider_order_id,
    ({V2_PROVENANCE}) AS v2, {WRITER_TARGET} AS writer_target, {figures.REVERSED_INR} AS reversed_inr_sql
    FROM public.donations d {figures.REVERSAL_JOIN}"""
STREAMERS = "SELECT id::text AS id FROM public.streamers"
TOTALS = ("SELECT streamer_id::text AS streamer_id, donator_name_lower, total_amount "
          "FROM public.streamer_donator_totals")
HISTORY = """SELECT id, donation_id::text AS donation_id, streamer_id::text AS streamer_id, donator_name_lower, cause,
    credited_before, credited_after, delta_inr, delta_count, total_after, created_at, reversed_before_paise,
    reversed_after_paise, hold_before, hold_after, evidence_refs, txid, run_id
    FROM public.donation_credit_events ORDER BY id"""
REVERSALS = ("SELECT donation_id::text AS donation_id, captured_paise, applied_reversed_paise AS applied, hold, flags "
             "FROM public.donation_reversals")
CURRENT = """SELECT kind, provider_ref, provider_payment_id, donation_id::text AS donation_id, match_type, phase, status,
    amount_paise, currency, amount_deducted_paise, conflicting FROM public.payment_integrity_current
    WHERE donation_id IS NOT NULL"""
EVIDENCE = ("SELECT donation_id::text AS donation_id, provider_ref, kind, recorded_at "
            "FROM public.payment_integrity_events WHERE donation_id IS NOT NULL")
EVALUATIONS = ("SELECT donation_id::text AS donation_id, evidence_fp, evaluated_at, state, dispute_mode, outcome, "
               "ack_fp, ack_run FROM public.donation_accounting_evaluations")
STATE = "SELECT state, dispute_mode FROM public.payment_accounting_state"
EVENTS = ("SELECT id, from_state, to_state, from_mode, to_mode, created_at FROM public.payment_accounting_events "
          "ORDER BY id")
RUNS = "SELECT id, command, scope, created_at FROM public.payment_accounting_runs ORDER BY id"

# the current-state ranking of migration 0029's view, at an evidence cut (recorded_at <= cut), for one donation's
# references. At cut = 'infinity' it must equal the view itself (checked: view_replica_mismatch).
_RANKED = """WITH ranked AS (
        SELECT e.*,
               CASE e.phase WHEN 'arbitration' THEN 3 WHEN 'pre_arbitration' THEN 2 WHEN 'chargeback' THEN 1
                            ELSE 0 END AS phase_rank,
               CASE e.status WHEN 'open' THEN 0 WHEN 'pending' THEN 0
                             WHEN 'under_review' THEN 1 WHEN 'action_required' THEN 1 WHEN 'failed' THEN 1
                             ELSE 2 END AS status_rank
        FROM public.payment_integrity_events e
        WHERE e.recorded_at <= %(cut)s
          AND e.provider_ref IN (SELECT x.provider_ref FROM public.payment_integrity_events x
                                 WHERE x.donation_id = %(d)s::uuid)
    ), ordered AS (
        SELECT r.*,
               row_number() OVER (PARTITION BY provider_ref ORDER BY phase_rank DESC, status_rank DESC,
                                  provider_event_at DESC NULLS LAST, received_at DESC, recorded_at DESC, id DESC) AS n,
               min(status) OVER same_rank <> max(status) OVER same_rank AS conflicting
        FROM ranked r
        WINDOW same_rank AS (PARTITION BY provider_ref, phase_rank, status_rank)
    ), cut_rows AS (
        SELECT kind, provider_ref, provider_payment_id, donation_id::text AS donation_id, match_type, phase, status,
               amount_paise, currency, amount_deducted_paise, conflicting
        FROM ordered WHERE n = 1 AND donation_id = %(d)s::uuid
    )"""
CUT_ROWS = _RANKED + " SELECT * FROM cut_rows ORDER BY provider_ref"
# Decision 5: evidence commit order is unknowable on 0034 (random uuid ids, recorded_at = transaction start), so a
# row or a keep may have seen any combination of each ref's append-only versions. The enumeration is bounded: over the
# bound a case is uncertain, never exact and never forged.
ENUMERATION_BOUND = 4096
MAX_REF_EVENTS = 12  # 2^12 subsets per ref
REF_EVENTS = ("SELECT e.id::text AS id, e.kind, e.provider_ref, e.provider_payment_id, "
              "e.donation_id::text AS donation_id, e.match_type, e.phase, e.status, e.amount_paise, e.currency, "
              "e.amount_deducted_paise, e.provider_event_at, e.received_at, e.recorded_at "
              "FROM public.payment_integrity_events e WHERE e.provider_ref IN "
              "(SELECT x.provider_ref FROM public.payment_integrity_events x WHERE x.donation_id = %(d)s::uuid) "
              "ORDER BY e.provider_ref, e.id")
# 0034's payment_evidence_fp over candidate rows (D-1's FP_CUT with the rows supplied): typed as the 0029 columns
FP_SETS = """SELECT s.n, md5(ROW((SELECT x.provider_payment_id FROM public.donations x WHERE x.id = %(d)s::uuid),
        s.v->>'mode',
        (SELECT string_agg(ROW(c.provider_ref, c.kind, c.match_type, c.provider_payment_id, c.status, c.phase,
                               c.amount_paise, c.amount_deducted_paise, c.currency, c.conflicting)::text,
                           ',' ORDER BY c.provider_ref)
           FROM jsonb_to_recordset(s.v->'rows') AS c(provider_ref text, kind text, match_type text,
                provider_payment_id text, status text, phase text, amount_paise bigint, amount_deducted_paise bigint,
                currency text, conflicting boolean)))::text) AS fp
    FROM jsonb_array_elements(%(sets)s::jsonb) WITH ORDINALITY AS s(v, n) ORDER BY s.n"""
ROW_FIELDS = ("kind", "provider_ref", "provider_payment_id", "donation_id", "match_type", "phase", "status",
              "amount_paise", "currency", "amount_deducted_paise")
COUNTED = (("refund", "processed"), ("dispute", "lost"))
PHASE_RANK = {"arbitration": 3, "pre_arbitration": 2, "chargeback": 1}


def _status_rank(status) -> int:
    return 0 if status in ("open", "pending") else 1 if status in ("under_review", "action_required", "failed") else 2


def _rank_key(e) -> tuple:
    """0029's ranking: phase, status, provider_event_at DESC NULLS LAST, received_at, recorded_at, id (max wins)."""
    return (PHASE_RANK.get(e["phase"], 0), _status_rank(e["status"]), e["provider_event_at"] is not None,
            e["provider_event_at"], e["received_at"], e["recorded_at"], e["id"])


def ref_states(events, did) -> list | None:
    """Decision 5: every current row one ref could have had for donation `did` under some commit order: the ranked
    winner, with its conflicting flag, of any non-empty subset of its append-only events, or no row at all. None
    over the bound."""
    if len(events) > MAX_REF_EVENTS:
        return None
    out = {None: None}
    for k in range(1, len(events) + 1):
        for subset in itertools.combinations(events, k):
            w = max(subset, key=_rank_key)
            if w["donation_id"] != did:  # the view shows only this donation's rows
                continue
            group = {e["status"] for e in subset if _rank_key(e)[:2] == _rank_key(w)[:2]}
            row = {**{f: w[f] for f in ROW_FIELDS}, "conflicting": len(group) > 1}
            out.setdefault(tuple(sorted(row.items(), key=lambda kv: kv[0])), row)
    return list(out.values())


EVIDENCE_CUTS = ("SELECT DISTINCT e.recorded_at FROM public.payment_integrity_events e WHERE e.provider_ref IN "
                 "(SELECT x.provider_ref FROM public.payment_integrity_events x WHERE x.donation_id = %(d)s::uuid) "
                 "ORDER BY 1")
# 0034's payment_evidence_fp, inline (D-1): the mode bound as text renders as the text column does
FP_CUT = _RANKED + """ SELECT md5(ROW((SELECT x.provider_payment_id FROM public.donations x WHERE x.id = %(d)s::uuid),
        %(mode)s::text,
        (SELECT string_agg(ROW(c.provider_ref, c.kind, c.match_type, c.provider_payment_id, c.status, c.phase,
                               c.amount_paise, c.amount_deducted_paise, c.currency, c.conflicting)::text,
                           ',' ORDER BY c.provider_ref) FROM cut_rows c))::text) AS fp"""
FP_NOW = """SELECT d.id::text AS id, md5(ROW((SELECT x.provider_payment_id FROM public.donations x WHERE x.id = d.id),
        %(mode)s::text,
        (SELECT string_agg(ROW(c.provider_ref, c.kind, c.match_type, c.provider_payment_id, c.status, c.phase,
                               c.amount_paise, c.amount_deducted_paise, c.currency, c.conflicting)::text,
                           ',' ORDER BY c.provider_ref)
           FROM public.payment_integrity_current c WHERE c.donation_id = d.id))::text) AS fp
    FROM public.donations d WHERE d.id = ANY(%(ids)s::uuid[])"""

CATALOG_TRIGGERS = """SELECT c.relname::text AS rel, t.tgname::text AS name, t.tgenabled::text AS enabled
    FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND NOT t.tgisinternal"""
CATALOG_FUNCTIONS = """SELECT p.proname::text AS name, p.oid::regprocedure::text AS signature, p.prosecdef AS definer,
    COALESCE(p.proconfig, '{}') AS config, md5(p.prosrc) AS src,
    EXISTS (SELECT 1 FROM aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) a
            WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE') AS public_execute
    FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public' AND p.proname = ANY(%(names)s)"""
CATALOG_EXECUTE = """SELECT p.proname::text AS name, r.rolname::text AS role
    FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
    CROSS JOIN pg_catalog.pg_roles r
    WHERE n.nspname = 'public' AND p.proname = ANY(%(names)s) AND r.rolname = ANY(%(roles)s)
      AND has_function_privilege(r.oid, p.oid, 'EXECUTE')"""
CATALOG_TABLE_WRITES = """SELECT c.relname::text AS rel, r.rolname::text AS role, priv
    FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    CROSS JOIN pg_catalog.pg_roles r CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']) AS priv
    WHERE n.nspname = 'public' AND c.relname = ANY(%(tables)s) AND r.rolname = ANY(%(roles)s)
      AND has_table_privilege(r.oid, c.oid, priv)"""


def _select(cur, sql: str, params=None) -> list[dict]:
    """Every data statement of the verifier goes through here: SELECT / WITH only (STOP-2)."""
    head = sql.lstrip().split(None, 1)[0].upper()
    if head not in ("SELECT", "WITH"):
        raise Refused(f"non-SELECT statement refused: {head}")
    cur.execute(sql, params)
    return cur.fetchall()


# ── helpers ───────────────────────────────────────────────────────────────────────────────────────────────────────

def _bucket_key(streamer_id: str, name_lower: str) -> str:
    """A bucket in reports: the creator id and a digest of the donor name (no personal data in the output)."""
    return f"{streamer_id}:{hashlib.sha256(name_lower.encode()).hexdigest()[:16]}"


def _paise(value) -> int:
    return 0 if value is None else int(value)


def window_class(elapsed: timedelta, commit_proven: bool) -> str:
    """The 24 h window (D-9) of a backlog item while active. `elapsed` is measured from a LOWER bound of the
    governing activation's commit (on 0034: its created_at) to an UPPER bound of now (C_v). pending only when that
    measurement is provably inside the window with the clock margin; overdue only with a proven commit (0035);
    everything else is deadline_uncertain (C-IR2 / C-IR7)."""
    # a clock off by at most r_max makes the true interval lie in [L / (1 + r), L / (1 - r)]: at least as strict
    # as L ± ε(L), so neither class is proven when the clock-error interval crosses the deadline
    seconds, window = Decimal(str(elapsed.total_seconds())), Decimal(str(WINDOW.total_seconds()))
    if seconds <= window * (1 - R_MAX):
        return "pending_reconciliation"
    if commit_proven and seconds > window * (1 + R_MAX):
        return "overdue_reconciliation"
    return "deadline_uncertain"


def active_class(elapsed: timedelta, commit_proven: bool) -> str:
    """An uncovered origin inside an active period (W1). Without a proven activation commit (0034, L-2b) a miss is
    never asserted: boundary_uncertain (C-IR2)."""
    if elapsed < MISS_AFTER:
        return "in_flight"
    return "missed_evaluation" if commit_proven else "boundary_uncertain"


# D-10 (plan rev. 3 §6, retained by rev. 10): the run-scope contract the 6a-3 CLI writes (enforced by 0035 later)
SCOPE_KEYS = {"donations": {"kind", "donations", "expires_at"},
              "creator": {"kind", "creator", "expires_at"},
              "all_v2": {"kind", "expires_at"},
              "recovery": {"kind", "donations", "verify_report_sha256", "expires_at"}}
SCOPE_MAX_LIFE = timedelta(hours=24)


def _uuid(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def parse_scope(scope, created_at: datetime) -> tuple[str, dict | None]:
    """('unscoped', None) for {} or no "kind"; ('malformed', None) for any defect (not an object, an unknown kind or
    key, a missing key, wrong types, an invalid uuid, an empty or duplicated list, an expires_at that is missing,
    unparsable, before created_at or more than 24 h after it); else ('valid', {kind, donations, creator, expires})."""
    if not isinstance(scope, dict):
        return "malformed", None
    if "kind" not in scope:  # rev. 3 §6: "missing scope ({} or no kind)"
        return "unscoped", None
    kind = scope["kind"]
    if kind not in SCOPE_KEYS or set(scope) != SCOPE_KEYS[kind]:
        return "malformed", None
    expires = scope["expires_at"]
    try:
        expires = datetime.fromisoformat(expires.replace("Z", "+00:00")) if isinstance(expires, str) else None
    except ValueError:
        expires = None
    if expires is None or expires.tzinfo is None or not created_at <= expires <= created_at + SCOPE_MAX_LIFE:
        return "malformed", None
    info = {"kind": kind, "donations": None, "creator": None, "expires": expires}
    if kind in ("donations", "recovery"):
        ids = scope["donations"]
        if not isinstance(ids, list) or not ids:
            return "malformed", None
        parsed = [_uuid(i) for i in ids]
        if None in parsed or len(set(parsed)) != len(parsed):
            return "malformed", None
        info["donations"] = set(parsed)
    if kind == "creator":
        info["creator"] = _uuid(scope["creator"])
        if info["creator"] is None:
            return "malformed", None
    if kind == "recovery" and not (isinstance(scope["verify_report_sha256"], str)
                                   and re.fullmatch(r"[0-9a-f]{64}", scope["verify_report_sha256"])):
        return "malformed", None
    return "valid", info


def scope_covers(info: dict, command: str, did: str, streamer_id: str | None, v2: bool, at: datetime) -> bool:
    """A valid scope covers a use of donation `did` at `at` (rev. 3 §6: listed / same creator / all V2 for
    reconcile only, never after expires_at)."""
    if at > info["expires"]:
        return False
    if info["kind"] in ("donations", "recovery"):
        return did in info["donations"]
    if info["kind"] == "creator":
        return streamer_id == info["creator"]
    return v2 and command == "reconcile"  # all_v2


def canonical(obj) -> str:
    def norm(v):
        if isinstance(v, dict):
            return {str(k): norm(x) for k, x in v.items()}
        if isinstance(v, (list, tuple, set, frozenset)):
            items = [norm(x) for x in v]
            return sorted(items, key=lambda x: json.dumps(x, sort_keys=True)) if isinstance(v, (set, frozenset)) \
                else items
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, timedelta):
            return v.total_seconds()
        if isinstance(v, uuid.UUID):
            return str(v)
        return v
    return json.dumps(norm(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def finish(report: dict) -> dict:
    body = {k: v for k, v in report.items() if k != "digest"}
    report["digest"] = hashlib.sha256(canonical(body).encode("ascii")).hexdigest()
    return report


def refused_report(reason: str, scope: dict | None = None) -> dict:
    return finish({"verifier": VERIFIER_VERSION, "status": "refused", "exit_code": EXIT_CODES["refused"],
                   "reason": reason, "scope": scope or {}, "findings": [], "counts": {},
                   "gates": dict.fromkeys(GATE_KEYS, False)})


def status_of(classes) -> str:
    """Status = the maximum severity over the emitted classes (C-IR6). An unknown class refuses the report."""
    worst = 0
    for cls in classes:
        if cls not in SEVERITY:
            raise Refused(f"unknown class {cls!r}")
        worst = max(worst, RANK[SEVERITY[cls]])
    return STATUS_OF[worst]


def gates(status: str, complete: bool, state: str, counts: dict) -> dict:
    """The §4.3 gates. activation_complete also needs the 6a-3 second report-only reconcile to plan zero changes,
    which verify never attests (§4.3, §9.1): it is therefore ALWAYS false in a verify report (CR-1). The part verify
    can establish (full scope, active, status clean) is reported separately as activation_verification_ready: clean
    is necessary, never sufficient. Nothing in a report activates or authorizes activation."""
    full_active = complete and state == "active"
    violations = {c: n for c, n in counts.items() if SEVERITY[c] == "violation" and n}
    recovery_ok = full_active and not (set(violations) - {"overdue_reconciliation"})
    g = {"reconcile_precondition": full_active and not violations,
         "recovery_precondition": recovery_ok,
         "activation_verification_ready": full_active and status == "clean",
         "activation_complete": False}  # needs the unattested 6a-3 condition (NOT_ATTESTED)
    if set(g) != set(GATE_KEYS) or g["activation_complete"] or (
            g["activation_verification_ready"] and (status != "clean" or not complete)):  # the §9 invariant
        raise Refused("gate invariant violated")
    return g


# ── the verifier ──────────────────────────────────────────────────────────────────────────────────────────────────

class Verifier:
    def __init__(self, cur, first: dict, scope_kind: str, scope_id: str | None):
        self.cur, self.first = cur, first
        self.scope_kind, self.scope_id = scope_kind, scope_id
        self.findings: list[Finding] = []
        self.c_v: datetime = first["c_v"]

    def add(self, invariant, cls, kind, entity, **detail):
        self.findings.append(Finding(invariant, cls, kind, str(entity), detail))

    # ── load ──
    def load(self):
        q = lambda sql, p=None: _select(self.cur, sql, p)  # noqa: E731
        self.donations = {r["id"]: r for r in q(DONATIONS)}
        self.streamers = {r["id"] for r in q(STREAMERS)}
        self.totals = {(r["streamer_id"], r["donator_name_lower"]): r["total_amount"] for r in q(TOTALS)}
        self.history = q(HISTORY)
        self.reversals = {r["donation_id"]: r for r in q(REVERSALS)}
        self.current = defaultdict(list)
        for r in q(CURRENT):
            self.current[r["donation_id"]].append(r)
        self.evidence = defaultdict(list)
        for r in q(EVIDENCE):
            self.evidence[r["donation_id"]].append(r)
        self.evals = {r["donation_id"]: r for r in q(EVALUATIONS)}
        states = q(STATE)
        if len(states) != 1:
            raise Refused("the accounting state row is missing or duplicated")
        self.state, self.mode = states[0]["state"], states[0]["dispute_mode"]
        self.events = q(EVENTS)
        self.runs = {r["id"]: r for r in q(RUNS)}
        self.by_donation = defaultdict(list)
        for h in self.history:
            self.by_donation[h["donation_id"]].append(h)
        relevant = sorted(set(self.evals) | set(self.current) | set(self.reversals) | set(self.evidence))
        relevant = [d for d in relevant if d in self.donations]
        self.fp = {m: {r["id"]: r["fp"] for r in q(FP_NOW, {"mode": m, "ids": relevant})} for m in ("hold", "apply")}
        self.catalog_triggers = q(CATALOG_TRIGGERS)
        self.catalog_functions = q(CATALOG_FUNCTIONS, {"names": sorted(FUNCTION_PINS)})
        self.catalog_execute = q(CATALOG_EXECUTE, {"names": sorted(FUNCTION_PINS), "roles": list(RUNTIME_ROLES)})
        self.catalog_writes = q(CATALOG_TABLE_WRITES, {"tables": list(NO_RUNTIME_WRITES + APPEND_ONLY_EVIDENCE),
                                                       "roles": list(RUNTIME_ROLES)})

    def in_scope_donation(self, did: str, streamer_id: str | None = None) -> bool:
        if self.scope_kind == "full":
            return True
        if self.scope_kind == "donation":
            return did == self.scope_id
        sid = streamer_id or (self.donations.get(did) or {}).get("streamer_id")
        return sid == self.scope_id

    def in_scope_bucket(self, streamer_id: str) -> bool:
        return self.scope_kind == "full" or (self.scope_kind == "creator" and streamer_id == self.scope_id)

    def cut_rows(self, did: str, cut) -> list[dict]:
        return _select(self.cur, CUT_ROWS, {"d": did, "cut": cut})

    def fp_at(self, did: str, cut, mode: str) -> str:
        return _select(self.cur, FP_CUT, {"d": did, "cut": cut, "mode": mode})[0]["fp"]

    def evidence_cuts(self, did: str) -> list:
        """Decision 4: every evidence state the donation's refs ever had, from empty to the latest."""
        return ["-infinity", *(r["recorded_at"] for r in _select(self.cur, EVIDENCE_CUTS, {"d": did})), "infinity"]

    def ref_events(self, did: str) -> dict:
        """Every append-only event of the donation's refs, by ref."""
        out = defaultdict(list)
        for e in _select(self.cur, REF_EVENTS, {"d": did}):
            out[e["provider_ref"]].append(e)
        return out

    def versions(self, did: str) -> list[dict]:
        """Decision 5: every recorded version of the donation's refs, as a (non-conflicting) current row."""
        return [{**{f: e[f] for f in ROW_FIELDS}, "conflicting": False}
                for evs in self.ref_events(did).values() for e in evs]

    def conceivable_fps(self, did: str, modes) -> set | None:
        """Decision 5 / A1: the fingerprints of every evidence state the donation could have had under some commit
        order, under each mode. None (uncertain) over the bound, or when the replica of 0034's fingerprint does not
        reproduce the current one (fail closed)."""
        per_ref = [ref_states(evs, did) for evs in self.ref_events(did).values()]
        if any(s is None for s in per_ref) or math.prod(len(s) for s in per_ref) * len(modes) > ENUMERATION_BOUND:
            return None
        sets = [{"mode": m, "rows": [r for r in combo if r is not None]}
                for m in modes for combo in itertools.product(*per_ref)]
        sets.append({"mode": self.mode, "rows": [{k: r[k] for k in (*ROW_FIELDS, "conflicting")}
                                                 for r in self.current.get(did, [])]})
        fps = [r["fp"] for r in _select(self.cur, FP_SETS, {"d": did, "sets": json.dumps(sets, default=str)})]
        if fps[-1] != self.fp[self.mode].get(did):
            return None
        return set(fps[:-1])

    def near_cuts(self, did: str, t) -> list:
        """The evidence states within the D-15 margin of a row stamped t."""
        return sorted({t - MARGIN} | {e["recorded_at"] for e in self.evidence.get(did, ())
                                      if t - MARGIN < e["recorded_at"] <= t + MARGIN})

    # CR-2 / Decisions 2-3: 0034 stamps are transaction starts and commit times are unbounded, so no historical mode
    # is chosen or excluded by timestamp proximity; a mode is excluded only by a rule the core enforces. The guard
    # changes the mode only while off / frozen, so an active period's mode is its activation event's to_mode.
    def initial_mode(self) -> str:
        return self.events[0]["from_mode"] if self.events else self.mode

    def period_modes(self) -> list[tuple]:
        """(start, end, mode) of every active period, the mode fixed by its activation event."""
        modes = {e["id"]: e["to_mode"] for e in self.events}
        return [(a, end, modes[aid]) for a, end, aid in self.periods()]

    def every_mode(self) -> set:
        """Every mode the state ever had: the possibilities for a keep (allowed active or frozen)."""
        return {self.initial_mode()} | {e["to_mode"] for e in self.events}

    def row_modes(self, did: str, refs) -> set:
        """The modes an active-only clear row could have been written under: every active period's mode, minus hold
        when its refs name a dispute (the core refuses a clear result in hold while a lost dispute is current, and
        the refs are exactly the counted refunds and lost disputes, so a named dispute was lost and current)."""
        modes = {m for _, _, m in self.period_modes()} or {self.initial_mode()}
        if {e["provider_ref"] for e in self.evidence.get(did, ()) if e.get("kind") == "dispute"} & set(refs):
            modes.discard("hold")
        return modes

    # ── I10: triggers, function security and pins, ACLs, grants ──
    def i10(self):
        live = {(t["rel"], t["name"]): t["enabled"] for t in self.catalog_triggers}
        for key in sorted(EXPECTED_TRIGGERS):
            if live.get(key) != "O":
                self.add("I10", "structural", "trigger", ".".join(key),
                         problem="missing" if key not in live else f"enabled={live[key]}")
        funcs = {f["name"]: f for f in self.catalog_functions}
        for name, pin in sorted(FUNCTION_PINS.items()):
            f = funcs.get(name)
            if f is None:
                self.add("I10", "structural", "function", name, problem="missing")
                continue
            if f["src"] != pin:
                self.add("I10", "structural", "function", name, problem="body_drift")
            config = set(f["config"] or ())
            if "search_path=pg_catalog, public" not in config and "search_path=pg_catalog" not in config:
                self.add("I10", "structural", "function", name, problem="search_path")
            if (name in DEFINER_FUNCTIONS) != bool(f["definer"]):
                self.add("I10", "structural", "function", name, problem="security")
            if name == "payment_accounting_set_state" and "lock_timeout=5s" not in config:
                self.add("I10", "structural", "function", name, problem="lock_timeout")
            if f["public_execute"]:
                self.add("I10", "structural", "function", name, problem="public_execute")
        granted = {(e["name"], e["role"]) for e in self.catalog_execute}
        for name in sorted(FUNCTION_PINS):
            for role in RUNTIME_ROLES:
                expected = name in APP_EXECUTE and role == "hyperchat_app"
                if ((name, role) in granted) != expected:
                    self.add("I10", "structural", "grant", f"{name}:{role}",
                             problem="execute_granted" if not expected else "execute_missing")
        for w in self.catalog_writes:
            if w["rel"] in APPEND_ONLY_EVIDENCE and (w["role"], w["priv"]) == ("hyperchat_app", "INSERT"):
                continue
            self.add("I10", "structural", "grant", f"{w['rel']}:{w['role']}", problem=f"{w['priv'].lower()}_granted")

    # ── I1, I2, I5, I7, I8, I11, I15: per donation ──
    def per_donation(self):
        for did, d in self.donations.items():
            if not self.in_scope_donation(did):
                continue
            ceiling = d["creditable_inr"] if d["creditable_inr"] is not None else d["amount_inr"]
            ok1 = (d["credited_inr"] is not None and ceiling is not None and d["amount_inr"] is not None
                   and Decimal(0) <= d["credited_inr"] <= ceiling <= d["amount_inr"])
            if not ok1:
                self.add("I1", "structural", "donation", did, credited=d["credited_inr"], ceiling=ceiling,
                         amount_inr=d["amount_inr"])
            if d["credited_inr"] != d["writer_target"]:
                self.add("I2", "structural", "donation", did, credited=d["credited_inr"], target=d["writer_target"])
            if d["credited_inr"] is not None and d["credited_inr"] > 0 \
                    and (d["streamer_id"], d["name_lower"]) not in self.totals:
                self.add("I5", "structural", "donation", did, credited=d["credited_inr"])
            rev = self.reversals.get(did)
            has_acct_history = any(h["cause"] in ACCOUNTING_CAUSES for h in self.by_donation.get(did, ()))
            if not d["v2"] and (rev is not None or has_acct_history or did in self.evals):
                self.add("I8", "structural", "donation", did, reversal=rev is not None,
                         accounting_history=has_acct_history, evaluation=did in self.evals)
            if rev is not None:
                captured = int((d["amount"] * 100).to_integral_value())
                if not (0 <= rev["applied"] <= rev["captured_paise"] == captured):
                    self.add("I11", "structural", "donation", did, applied=rev["applied"],
                             captured_paise=rev["captured_paise"], captured=captured)
                if rev["hold"] is not None and rev["hold"] not in HOLD_CODES:
                    self.add("I11", "structural", "donation", did, hold=rev["hold"])
                if not set(rev["flags"] or ()) <= FLAG_CODES:
                    self.add("I11", "structural", "donation", did, flags=sorted(rev["flags"]))
            applied = rev["applied"] if rev else 0
            python = accounting.reversed_inr(d, applied) if d["amount"] and d["amount"] > 0 else Decimal("0.00")
            if Decimal(d["reversed_inr_sql"]) != python:
                self.add("I15", "structural", "donation", did, sql=d["reversed_inr_sql"], python=python)

    # ── I3, I3-O, I4, I9, I14: per donation history; I6, I7: per bucket (Erratum 1) ──
    def chains(self):
        for did, rows in self.by_donation.items():
            d = self.donations.get(did)
            if d is None:
                self.orphan(did, rows)
                continue
            if not self.in_scope_donation(did):
                continue
            self.credit_chain("I3", did, rows, d["credited_inr"])
            for h in rows:
                if (h["streamer_id"], h["donator_name_lower"]) != (d["streamer_id"], d["name_lower"]):
                    self.add("I9", "structural", "history", h["id"], donation=did)
            self.reversal_chain(did, rows)
            seen = defaultdict(int)
            for h in rows:
                if h["cause"] in ACCOUNTING_CAUSES:
                    seen[h["txid"]] += 1
            for txid, n in seen.items():
                if n > 1:
                    self.add("I14", "structural", "donation", did, txid=txid, rows=n)
        for did, d in self.donations.items():
            if self.in_scope_donation(did) and d["credited_inr"] and d["credited_inr"] > 0 \
                    and did not in self.by_donation:
                self.add("I3", "structural", "donation", did, problem="credited_without_history")
        self.buckets()

    def credit_chain(self, inv, did, rows, credited) -> bool:
        ok = bool(rows) and rows[0]["credited_before"] == 0
        for prev, cur in zip(rows, rows[1:]):
            ok = ok and cur["credited_before"] == prev["credited_after"]
        ok = ok and all(h["delta_inr"] == h["credited_after"] - h["credited_before"] for h in rows)
        if credited is not None:
            ok = ok and rows[-1]["credited_after"] == credited
        if not ok:
            self.add(inv, "structural", "donation", did, problem="credit_chain")
        return ok

    def reversal_chain(self, did, rows):
        rev = self.reversals.get(did)
        prev_rev, prev_hold = 0, None
        for h in rows:
            before = _paise(h["reversed_before_paise"])
            after = _paise(h["reversed_after_paise"])
            if before != prev_rev or (h["reversed_before_paise"] is not None and h["hold_before"] != prev_hold):
                self.add("I4", "structural", "history", h["id"], donation=did, problem="reversal_link")
            if h["cause"] in ("writer", "backfill", "hold") and after != before:
                self.add("I4", "structural", "history", h["id"], donation=did, problem="reversal_changed_by_" +
                         h["cause"])
            if h["cause"] == "manual" and h["evidence_refs"] is None and after != before:
                self.add("I4", "structural", "history", h["id"], donation=did, problem="keep_changed_reversal")
            if h["cause"] == "revert" and (after != 0 or h["hold_after"] is not None):
                self.add("I4", "structural", "history", h["id"], donation=did, problem="revert_not_zero")
            prev_rev = after
            prev_hold = h["hold_after"] if h["reversed_after_paise"] is not None else prev_hold
        applied, hold = (rev["applied"], rev["hold"]) if rev else (0, None)
        if prev_rev != applied or prev_hold != hold:  # a hold without the history that set it is a violation too
            self.add("I4", "structural", "donation", did, problem="reversal_state", applied=applied,
                     history=prev_rev)

    def orphan(self, did, rows):
        buckets = {(h["streamer_id"], h["donator_name_lower"]) for h in rows}
        sid = rows[0]["streamer_id"]
        if not self.in_scope_bucket(sid) and not (self.scope_kind == "donation" and did == self.scope_id):
            return
        if len(buckets) > 1:
            self.add("I9", "structural", "orphan", did, problem="orphan_moved_bucket", buckets=len(buckets))
            return
        if sid not in self.streamers:
            return  # deleted-creator bucket: excluded and counted (buckets())
        net = sum(h["delta_inr"] for h in rows)
        chain_ok = self.credit_chain("I3-O", did, rows, None)
        if chain_ok and (net != 0 or rows[-1]["credited_after"] != 0):
            self.add("I3-O", "structural", "orphan", did, net=net, last=rows[-1]["credited_after"])

    def buckets(self):
        writer = defaultdict(list)
        all_rows = defaultdict(list)
        for h in self.history:
            key = (h["streamer_id"], h["donator_name_lower"])
            all_rows[key].append(h)
            if h["cause"] == "writer":
                writer[key].append(h)
        self.excluded, self.baselines = [], []
        self.orphan_buckets = self.orphan_rows = 0
        credited = defaultdict(Decimal)
        for d in self.donations.values():
            credited[(d["streamer_id"], d["name_lower"])] += d["credited_inr"] or 0
        for key in sorted(set(all_rows) | set(self.totals) | set(credited)):
            sid = key[0]
            if not self.in_scope_bucket(sid):
                continue
            bucket = _bucket_key(*key)
            if sid not in self.streamers:
                rows = all_rows.get(key, [])
                acct = sum(1 for h in rows if h["cause"] in ACCOUNTING_CAUSES)
                net = sum((h["delta_inr"] for h in rows), Decimal(0))
                self.orphan_buckets += 1
                self.orphan_rows += len(rows)
                self.excluded.append({"bucket": bucket, "rows": len(rows), "net_credit": net,
                                      "accounting_rows": acct,
                                      "first": min((h["created_at"] for h in rows), default=None),
                                      "last": max((h["created_at"] for h in rows), default=None)})
                if acct:
                    self.add("Erratum-1", "deleted_creator_with_accounting_history", "bucket", bucket, rows=acct)
                if net != 0:
                    self.add("Erratum-1", "excluded_bucket_net_credit", "bucket", bucket, net_credit=net)
                continue
            w = writer.get(key, [])
            for prev, cur in zip(w, w[1:]):
                if cur["total_after"] != prev["total_after"] + cur["delta_inr"]:
                    self.add("I6", "structural", "history", cur["id"], bucket=bucket, problem="P1")
            total = self.totals.get(key)
            if w:
                if total is None or total != w[-1]["total_after"]:
                    self.add("I6", "structural", "bucket", bucket, problem="P2", total=total,
                             last=w[-1]["total_after"])
                self.baselines.append({"bucket": bucket, "baseline": w[0]["total_after"] - w[0]["delta_inr"]})
            if total is not None:
                if credited[key] > total:
                    self.add("I7", "structural", "bucket", bucket, credited=credited[key], total=total)

    # ── D-10 runs ──
    def runs_check(self):
        used = defaultdict(set)
        for h in self.history:
            if h["run_id"] is not None:
                used[h["run_id"]].add(h["donation_id"])
        for ev in self.evals.values():
            if ev["ack_run"] is not None:
                used[ev["ack_run"]].add(ev["donation_id"])
        def relevant(rid) -> bool:  # a scoped report judges only the runs its donations used
            return self.scope_kind == "full" or any(self.in_scope_donation(d) for d in used.get(rid, ()))

        self.valid_runs = {}
        for rid, run in self.runs.items():
            kind, info = parse_scope(run["scope"], run["created_at"])
            if kind == "valid":
                self.valid_runs[rid] = info
            if not relevant(rid):
                continue
            if kind == "malformed":  # referenced or not (S4)
                self.add("D-10", "audit_integrity", "run", rid, problem="malformed_scope")
            elif kind == "unscoped":
                if rid in used:  # S2 / S3: a used run without scope, by history or an acknowledgement
                    self.add("D-10", "audit_integrity", "run", rid, problem="unscoped_used_run")
                else:  # S1
                    self.add("D-10", "unscoped_unused_run", "run", rid)
        for rid in sorted(set(used) - set(self.runs)):
            if relevant(rid):
                self.add("D-10", "audit_integrity", "run", rid, problem="missing_run")
        for h in self.history:
            if h["cause"] not in ("manual", "activation", "revert") or h["run_id"] not in self.runs \
                    or not self.in_scope_donation(h["donation_id"], h["streamer_id"]):
                continue
            kind = "keep" if h["cause"] == "manual" and h["evidence_refs"] is None else (
                "apply" if h["cause"] == "manual" else h["cause"])
            run = self.runs[h["run_id"]]
            info = self.valid_runs.get(h["run_id"])
            d = self.donations.get(h["donation_id"]) or {}
            if run["command"] != RUN_COMMAND[kind] or (info is not None and not scope_covers(
                    info, run["command"], h["donation_id"], h["streamer_id"], bool(d.get("v2")), h["created_at"])):
                self.add("D-10", "audit_integrity", "history", h["id"], problem="scope_mismatch", run=h["run_id"],
                         donation=h["donation_id"])  # S5-S10: outside the scope, expired, or the wrong command
        for did, ev in self.evals.items():  # an acknowledgement under a run that does not cover its donation
            info = self.valid_runs.get(ev["ack_run"]) if ev["ack_run"] is not None else None
            if info is None or not self.in_scope_donation(did):
                continue
            # anchored on the keep's own history row: evaluated_at is refreshed by every later 'acknowledged'
            # evaluation (0034 core upsert), so it would expire a valid keep (an unmatched ack is I12's finding)
            keep = next((h for h in self.by_donation.get(did, ()) if h["run_id"] == ev["ack_run"]
                         and h["cause"] == "manual" and h["evidence_refs"] is None), None)
            d = self.donations.get(did) or {}
            if keep is not None and (self.runs[ev["ack_run"]]["command"] != "resolve-keep" or not scope_covers(
                    info, "resolve-keep", did, d.get("streamer_id"), bool(d.get("v2")), keep["created_at"])):
                self.add("D-10", "audit_integrity", "donation", did, problem="scope_mismatch", run=ev["ack_run"])

    def run_valid_for(self, rid, row, command) -> bool:
        """An I12 exception's run: valid scope, the right command, covering this row's donation at its time."""
        info = self.valid_runs.get(rid)
        if info is None or self.runs[rid]["command"] != command:
            return False
        d = self.donations.get(row["donation_id"]) or {}
        return scope_covers(info, command, row["donation_id"], row["streamer_id"], bool(d.get("v2")),
                            row["created_at"])

    # ── timeline ──
    def periods(self) -> list[tuple]:
        """Active periods [A.created_at, end) from the events in id order (provably commit order), with A's id."""
        out, start, aid = [], None, None
        for e in self.events:
            if e["to_state"] == "active" and e["from_state"] != "active":
                start, aid = e["created_at"], e["id"]
            elif e["to_state"] != "active" and e["from_state"] == "active" and start is not None:
                out.append((start, e["created_at"], aid))
                start = None
        if start is not None:
            out.append((start, None, aid))
        return out

    def legacy_timeline(self):
        """Accounting rows that only an active state allows (evaluation, hold, resolved, activation, manual apply),
        timestamped outside every active period by more than the margin: 0034's start-time stamps (CE-1, L-2)
        cannot place them. Reported, never a violation (state gating is enforced inside the database)."""
        periods = self.periods()
        for h in self.history:
            if h["cause"] not in ("evaluation", "hold", "resolved", "activation") and not (
                    h["cause"] == "manual" and h["evidence_refs"] is not None):
                continue
            if not self.in_scope_donation(h["donation_id"], h["streamer_id"]):
                continue
            t = h["created_at"]
            if not any(a - MARGIN <= t < ((end + MARGIN) if end else datetime.max.replace(tzinfo=t.tzinfo))
                       for a, end, _ in periods):
                self.add("L-2", "legacy_uncertain", "history", h["id"], donation=h["donation_id"])

    # ── I12 / I12-H / I13 per V2 donation ──
    def frozen_value(self, rows, hold) -> int:
        """F(d): the reversal of the last clear (hold_after NULL) row before the current hold began, or 0."""
        accounting_rows = [h for h in rows if h["reversed_after_paise"] is not None]
        i = len(accounting_rows)
        while i > 0 and accounting_rows[i - 1]["hold_after"] == hold:
            i -= 1
        for h in reversed(accounting_rows[:i]):
            if h["hold_after"] is None:
                return _paise(h["reversed_after_paise"])
        return 0

    def i12h(self, d, rows):
        """Every clear accounting row re-derivable from its evidence_refs (Decisions 2, 4, 5 / S2). The core proves
        that the refs were exactly the counted rows and the amount GREATEST(refunds, lost) over them; the commit
        order (which versions those rows were) is not provable, so every recorded version is a candidate. Exact
        only when every possible mode gives the stored amount alone AND a recorded_at prefix within +-M reproduces
        it (the nominal order: consistency, never proof that the state existed)."""
        donation = self.eval_donation(d)
        versions = None
        for h in rows:
            if h["cause"] not in ("evaluation", "resolved", "manual", "activation") or h["hold_after"] is not None \
                    or h["evidence_refs"] is None:
                continue
            versions = self.versions(d["id"]) if versions is None else versions
            refs, stored = sorted(h["evidence_refs"]), _paise(h["reversed_after_paise"])
            modes = sorted(self.row_modes(d["id"], refs))
            pools = [[v for v in versions if v["provider_ref"] == r and (v["kind"], v["status"]) in COUNTED]
                     for r in refs]
            detail = {"donation": d["id"], "stored": stored, "modes_checked": modes}
            if math.prod(len(p) for p in pools) * max(len(modes), 1) > ENUMERATION_BOUND:
                self.add("I12-H", "historical_ambiguous", "history", h["id"], reason="evidence_cut", basis="evidence",
                         enumeration="over_bound", candidates=[], max_impact_paise=None, **detail)
                continue
            by_mode = {m: {e.reversed_paise for combo in itertools.product(*pools)
                           for e in [accounting.evaluate_in_mode(donation, list(combo), m)]
                           if e.hold is None and accounting.counted_refs(donation, list(combo)) == refs}
                       for m in modes}
            supporting = [m for m in modes if stored in by_mode[m]]
            candidates = sorted(set().union(*by_mode.values()))
            detail["supporting_modes"] = supporting
            impact = max((abs(stored - a) for a in candidates), default=None)
            if not supporting:  # no version of the stored refs, in any commit order or possible mode, gives it
                self.add("I12-H", "structural", "history", h["id"], problem="unsupported_amount",
                         candidates=candidates, **detail)
            elif supporting != modes:  # another possible mode, unordered by any proof, gives another outcome
                self.add("I12-H", "historical_ambiguous", "history", h["id"], reason="mode_order", basis="mode",
                         candidates=candidates, max_impact_paise=impact, **detail)
            elif len(candidates) > 1:  # another version of a stored ref gives another amount
                self.add("I12-H", "historical_ambiguous", "history", h["id"], reason="evidence_cut",
                         basis="evidence", candidates=candidates, max_impact_paise=impact, **detail)
            elif not self.nominal(donation, d["id"], h["created_at"], refs, stored, modes):
                # the refs determine the amount, but only an order no prefix within +-M shows explains the row
                self.add("I12-H", "historical_ambiguous", "history", h["id"], reason="evidence_order",
                         basis="evidence", candidates=candidates, max_impact_paise=impact, **detail)

    def nominal(self, donation, did, t, refs, stored, modes) -> bool:
        """S2: a recorded_at prefix within +-M of t reproduces (refs, stored) under every possible mode."""
        states = [self.cut_rows(did, c) for c in self.near_cuts(did, t)]
        return all(any(e.hold is None and e.reversed_paise == stored
                       and accounting.counted_refs(donation, rows_c) == refs
                       for rows_c in states for e in [accounting.evaluate_in_mode(donation, rows_c, m)])
                   for m in modes)

    def eval_donation(self, d) -> dict:
        return {"id": d["id"], "provider_payment_id": d["provider_payment_id"], "amount": d["amount"],
                "amount_inr": d["amount_inr"], "currency": d["currency"], "payment_status": d["payment_status"],
                "legacy_source_table": d["legacy_source_table"]}

    def ack_reconstructs(self, did, keep_row, ack_fp) -> bool | None:
        """A1 (Decision 5): a keep's snapshot is never observable on 0034, so an acknowledgement is never proven.
        False: no evidence state the donation could have had, under any commit order and any mode the state ever
        had, gives it (a forged acknowledgement). None: possibly valid, or the enumeration is incomplete."""
        fps = self.conceivable_fps(did, sorted(self.every_mode()))
        return None if fps is None or ack_fp in fps else False

    def origins(self, did, d, ev, fp_now, rows) -> tuple[datetime | None, str]:
        """§4.2 / §5 / §7: the earliest applicable origin and its kind."""
        events = sorted(e["recorded_at"] for e in self.evidence.get(did, ()))
        if ev is None:
            return (events[0], "o_ev") if events else (None, "none")
        m_e, f_e = ev["dispute_mode"], ev["evidence_fp"]
        mode_changed = m_e != self.mode
        evidence_changed = self.fp.get(m_e, {}).get(did) != f_e
        found = []
        if mode_changed:
            # m_e != m_c itself proves a mode change committed after the evaluation; only its time is uncertain when
            # the change's transaction started before the evaluation's (CR-2): never indeterminate for that
            changes = [e["created_at"] for e in self.events if e["from_mode"] != e["to_mode"]]
            later = [t for t in changes if t > ev["evaluated_at"]]
            near = [t for t in changes if t > ev["evaluated_at"] - MARGIN]
            if later:
                found.append((later[0], "o_mode"))
            elif changes:
                found.append((near[0] if near else changes[-1], "o_mode_uncertain"))
        if evidence_changed:
            cands = [t for t in events if t >= ev["evaluated_at"] - MARGIN]
            if cands:
                found.append((cands[0], "o_ev"))
            else:
                caps = [h["created_at"] for h in rows if h["cause"] == "writer" and h["credited_before"] == 0]
                later = [t for t in caps if t > ev["evaluated_at"]]
                near = [t for t in caps if t > ev["evaluated_at"] - MARGIN]  # a capture stamped just before
                if later:
                    found.append((later[0], "o_cap"))
                elif near:
                    found.append((near[0], "o_cap_uncertain"))
        if not found:
            return None, "none"
        return min(found)

    def timing(self, did, t_o, kind) -> tuple[str, dict]:
        """W1-W5 for an origin t_o (0034 data: no proven commit, C-IR2 / C-IR7)."""
        periods = self.periods()
        detail = {"origin": t_o, "origin_kind": kind}
        if kind.endswith("_uncertain"):  # the origin's time is not established (CR-2): attention, never a deadline
            return "boundary_uncertain", detail
        for a, end, aid in periods:
            if a <= t_o and (end is None or t_o < end):
                return active_class(self.c_v - t_o, commit_proven=False), {**detail, "activation_event": aid}
        if any(abs(e["created_at"] - t_o) <= MARGIN for e in self.events
               if (e["from_state"] == "active") != (e["to_state"] == "active")):  # an activation or a freeze
            return "boundary_uncertain", detail
        if self.state != "active":
            return "awaiting_reconciliation", detail
        governing = next(((a, aid) for a, _, aid in periods if a > t_o), None)
        if governing is None:
            return "indeterminate", detail
        a, aid = governing
        return window_class(self.c_v - a, commit_proven=False), {**detail, "activation_event": aid}

    def classify(self):
        """I11 R-DM, I12 agreement and I13 timing for every V2 donation with accounting relevance (§4-§8)."""
        relevant = set(self.evals) | set(self.current) | set(self.reversals) | set(self.evidence)
        for did in sorted(relevant):
            d = self.donations.get(did)
            if d is None or not d["v2"] or not self.in_scope_donation(did):
                continue
            rows = self.by_donation.get(did, [])
            self.i12h(d, rows)
            if did in self.violated_donations():  # §4.2 (8): structural / audit classes take precedence
                continue
            self.classify_one(did, d, rows)

    def violated_donations(self) -> set[str]:
        out = set()
        for f in self.findings:
            if SEVERITY[f.cls] == "violation":
                out.add(f.entity_id if f.entity_kind == "donation" else f.detail.get("donation"))
        return out

    def classify_one(self, did, d, rows):
        cur = self.current.get(did, [])
        donation = self.eval_donation(d)
        e = accounting.evaluate_in_mode(donation, cur, self.mode)
        rev = self.reversals.get(did)
        x_applied, x_hold = (rev["applied"], rev["hold"]) if rev else (0, None)
        x_flags = frozenset(rev["flags"] or ()) if rev else frozenset()
        ev = self.evals.get(did)
        fp_now = self.fp[self.mode].get(did)
        detail = {}
        if x_hold is not None:  # F-12: what refunds alone would reverse while held
            refunds = accounting.processed_refunds_paise(donation, cur)
            detail = {"hold": x_hold, "applied_paise": x_applied, "processed_refunds_paise": refunds,
                      "unapplied_refunds_paise": max(0, refunds - x_applied)}
        if ev is not None:  # §7 metadata consistency
            if ev["dispute_mode"] not in ("hold", "apply") or ev["state"] not in ("off", "active", "frozen"):
                self.add("I12", "audit_integrity", "donation", did, problem="evaluation_metadata_altered")
                return
            if fp_now == ev["evidence_fp"] and ev["dispute_mode"] != self.mode:
                self.add("I12", "audit_integrity", "donation", did, problem="evaluation_metadata_inconsistent")
                return
        if ev is not None and ev["ack_run"] is not None and not any(
                h["run_id"] == ev["ack_run"] and h["cause"] == "manual" and h["evidence_refs"] is None for h in rows):
            self.add("I12", "audit_integrity", "donation", did, problem="unmatched_ack", run=ev["ack_run"])
            return
        fp_equal = ev is not None and fp_now == ev["evidence_fp"]
        e_flags = e.flags & FLAG_CODES
        last_acct = next((h for h in reversed(rows) if h["cause"] in ACCOUNTING_CAUSES), None)

        # R-DM (D-4): a refunds-only hold without a current attributed loss
        if x_hold == accounting.DISPUTE_MODE_HOLD and not accounting.has_lost_dispute(donation, cur):
            if self.state != "active":
                self.add("I11", "awaiting_reconciliation", "donation", did, reason="dispute_mode_hold", **detail)
                return
            if fp_equal:
                self.add("I11", "stale_hold", "donation", did, **detail)
                return

        agree, exception = False, None
        if e.hold is None:
            agree = x_hold is None and x_applied == e.reversed_paise and x_flags == e_flags
        elif x_hold == e.hold:
            agree = x_applied == self.frozen_value(rows, x_hold) and x_flags == e_flags
            if not agree and fp_equal:
                self.add("I12", "structural", "donation", did, problem="frozen_value", applied=x_applied,
                         frozen=self.frozen_value(rows, x_hold))
                return
        elif x_hold is not None and fp_equal:
            self.add("I12", "structural", "donation", did, problem="hold_disagreement", expected=e.hold,
                     stored=x_hold)
            return
        if not agree and x_hold is None and e.hold == accounting.DISPUTE_MODE_HOLD and last_acct is not None \
                and last_acct["cause"] == "manual" and last_acct["evidence_refs"] is None \
                and last_acct["hold_before"] == accounting.DISPUTE_MODE_HOLD and last_acct["hold_after"] is None:
            ok = (self.run_valid_for(last_acct["run_id"], last_acct, "resolve-keep") and ev is not None
                  and ev["ack_run"] == last_acct["run_id"])
            rebuilt = self.ack_reconstructs(did, last_acct, ev["ack_fp"]) if ok and ev["ack_fp"] is not None else True
            if rebuilt is False:  # under no mode the state ever had, at no evidence state
                self.add("I12", "audit_integrity", "donation", did, problem="forged_ack", run=last_acct["run_id"])
                return
            if rebuilt is None:  # possibly valid, never proven (Decisions 3, 5 / A1): no exception, no violation
                if ev["ack_fp"] == fp_now and x_applied == _paise(last_acct["reversed_after_paise"]):
                    # the keep would be the audited exception if its ack were established: a legacy history row
                    # whose authority 0034's timestamps cannot settle (permanent, like I12-H ambiguity)
                    self.add("I12", "historical_ambiguous", "history", last_acct["id"], donation=did,
                             reason="ack_mode_order", basis="mode" if len(self.every_mode()) > 1 else "evidence",
                             run=last_acct["run_id"], **detail)
                    return
                detail["ack"] = "mode_order_uncertain"
            # A1: no other outcome grants the keep exception; an unproven ack never makes the keep agree
        if not agree and last_acct is not None and last_acct["cause"] == "revert" and (x_applied, x_hold) == (0, None) \
                and self.run_valid_for(last_acct["run_id"], last_acct, "revert"):
            if self.state != "active":
                agree, exception = True, "revert"
            else:
                self.finish_timing(did, last_acct["created_at"], "o_rev", detail)
                return
        need = ev is None or not fp_equal or not agree
        if not need:
            if exception == "revert" or ev["state"] == "active":  # C-IR5: observed under S, not evaluated_at
                self.add("I13", "processed", "donation", did, **({"exception": exception} if exception else {}))
                return
            self.finish_timing(did, ev["evaluated_at"], "o_ack", detail)  # e.g. a keep while frozen
            return
        if fp_equal and not agree:
            self.add("I12", "structural", "donation", did, problem="evaluation_disagreement",
                     expected=e.reversed_paise, applied=x_applied, hold=x_hold, expected_hold=e.hold)
            return
        t_o, kind = self.origins(did, d, ev, fp_now, rows)
        if t_o is None:
            self.add("I13", "indeterminate", "donation", did, **detail)
            return
        self.finish_timing(did, t_o, kind, detail)

    def finish_timing(self, did, t_o, kind, detail):
        cls, timing = self.timing(did, t_o, kind)
        self.add("I13", cls, "donation", did, **detail, **timing)

    def run(self) -> dict:
        self.load()
        self.i10()
        self.view_replica()
        self.per_donation()
        self.chains()
        self.runs_check()
        self.legacy_timeline()
        self.classify()
        return self.report()

    def view_replica(self):
        """The verifier's cut replica of 0029's view must equal the view at cut = infinity (no silent drift)."""
        for did in sorted(set(self.current) | set(self.evidence)):
            if did not in self.donations or not self.in_scope_donation(did):
                continue
            mine = self.cut_rows(did, "infinity")
            view = sorted(self.current.get(did, []), key=lambda r: r["provider_ref"])
            if [dict(r) for r in mine] != [dict(r) for r in view]:
                self.add("I10", "structural", "view", "payment_integrity_current", problem="view_replica_mismatch",
                         donation=did)

    def report(self) -> dict:
        findings = sorted(set(self.findings), key=lambda f: (f.invariant, f.cls, f.entity_kind, f.entity_id))
        counts = defaultdict(int)
        for f in findings:
            counts[f.cls] += 1
        status = status_of(counts)
        complete = self.scope_kind == "full"
        g = gates(status, complete, self.state, counts)
        first = self.first
        return finish({
            "verifier": VERIFIER_VERSION,
            "database": {"name": first["database"], "head": first["head"],
                         "server_version_num": first["server_version_num"]},
            "snapshot": {"pg_current_snapshot": first["snapshot"], "transaction_timestamp": first["t_v"],
                         "first_statement_clock": first["c_v"]},
            "scope": {"kind": self.scope_kind, "id": self.scope_id, "complete": complete},
            "accounting": {"state": self.state, "dispute_mode": self.mode},
            "constants": {"margin_seconds": int(MARGIN.total_seconds()), "missed_seconds": int(MISS_AFTER.total_seconds()),
                          "window_seconds": int(WINDOW.total_seconds()), "r_max_ppm": int(R_MAX * 1_000_000)},
            "counts": dict(sorted(counts.items())),
            "findings": [f.as_dict() for f in findings],
            "excluded_buckets": sorted(self.excluded, key=lambda b: b["bucket"]),
            "orphans": {"orphan_buckets": self.orphan_buckets, "orphan_rows": self.orphan_rows},
            "baselines": sorted(self.baselines, key=lambda b: b["bucket"]),
            "limitations": list(LIMITATIONS),
            "not_attested": list(NOT_ATTESTED),
            "gates": g,
            "status": status,
            "exit_code": EXIT_CODES[status],
        })


def _guard(first: dict):
    if first["session_user"] != READONLY_ROLE or first["current_user"] != READONLY_ROLE:
        raise Refused("verify runs as hyperchat_readonly only (session and current user)")
    if first["isolation"] != "repeatable read":
        raise Refused("verify needs one REPEATABLE READ snapshot")
    if first["read_only"] != "on":
        raise Refused("verify needs a READ ONLY transaction")
    if first["server_version_num"] < MIN_SERVER:
        raise Refused("PostgreSQL 15 or later required")
    if first["head"] != EXPECTED_HEAD:
        raise Refused(f"database head {first['head']!r}, expected {EXPECTED_HEAD} (6a-2 verifies 0034 data only)")


def verify(conn: psycopg.Connection, *, creator: str | None = None, donation: str | None = None,
           statement_timeout: str = "120s") -> dict:
    """Run the verifier on `conn` (autocommit, hyperchat_readonly). Always ends with ROLLBACK; never raises for a
    verification problem: a refused report instead."""
    if creator and donation:
        return refused_report("--creator and --donation are exclusive")
    scope_kind, scope_id = ("creator", creator) if creator else (("donation", donation) if donation else ("full", None))
    try:
        if scope_id is not None:
            scope_id = str(uuid.UUID(scope_id))
    except ValueError:
        return refused_report("scope id is not a uuid", {"kind": scope_kind})
    scope = {"kind": scope_kind, "id": scope_id, "complete": scope_kind == "full"}
    if not conn.autocommit:
        return refused_report("verify needs an autocommit connection (it opens its own transaction)", scope)
    try:
        if not re.fullmatch(r"[1-9][0-9]{0,5}(ms|s|min)", statement_timeout):
            return refused_report("invalid statement timeout", scope)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            try:
                cur.execute(f"SET LOCAL statement_timeout = '{statement_timeout}'")  # validated literal
                cur.execute("SET LOCAL lock_timeout = '5s'")
                first = _select(cur, FIRST)[0]  # the transaction's snapshot is taken here
                _guard(first)
                return Verifier(cur, first, scope_kind, scope_id).run()
            finally:
                cur.execute("ROLLBACK")
    except Refused as exc:
        return refused_report(str(exc), scope)
    except psycopg.Error as exc:
        return refused_report(f"database error: {type(exc).__name__}", scope)
