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
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from app.payments import accounting, figures
from app.public.pricing import EXCHANGE_RATES_TO_INR

VERIFIER_VERSION = "6a-2.9"
EXPECTED_HEAD = "0034"  # the base the I10 pins below describe; EXPECTED_HEADS is derived from HEAD_DELTAS
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
    "evidence_attribution": "attention",        # I16 (revision 7 §2.3)
    "refund_snapshot_mismatch": "attention",    # I17 (revision 7 §2.4)
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
    "L-3: 0032- and 0034-written evaluation / hold / resolved rows are indistinguishable; a mode-impossible row stays "
    "structural (with provenance), and no transaction-id / xmin signal is used.",
    "L-4: run expiry is judged at the transaction start; a later use within a long transaction is undetectable on "
    "0034.",
    "L-5: an unchanged / acknowledged reconciliation writes no history but refreshes evaluation metadata, so a run "
    "used only that way appears unused.",
    "L-6: money is compared only within one of the producer's eight two-decimal currencies, never converted; equal "
    "integers wrongly scaled by the provider are not detectable; other currencies are reported, never compared.",
    "L-7: a continuous I18 chain cannot prove that no complete cycle was removed, and the frozen -> off financial "
    "prerequisite is not checked historically.",
    "L-8: historical eligibility uses the donation's current provider_payment_id and currency; an owner-level change "
    "to them is outside the proof.",
    "L-9: cross-account evidence cannot be produced through the 0029 constraints; account_mismatch is reachable only "
    "through corruption.",
    "L-10: snapshot_regression is conservative: any comparable value at a strictly earlier provider time above one "
    "at a strictly later time blocks, even where the true sequence may have been monotonic (fails closed).",
    "I16-I17: I16 / I17 cannot be cleared by verify-adjacent tools; they may block the write gates until a "
    "separately approved "
    "resolution path exists.",
    "INSTALL: the database owner is enforced (database_owner); approved administrators come from the external "
    "allow-list whose SHA-256 is reported; Windows file permissions are best effort.",
    "RUNTIME: the run ends by the deadline plus one bounded statement or batch, one checkpoint interval, one "
    "non-interruptible operation over ceiling-bounded input and cleanup latency; measured, not a hard real-time "
    "guarantee. Report publication is outside the deadline.",
)
NOT_ATTESTED = ("the 6a-3 second report-only reconcile plans zero changes (plan §4.3)",)
STATES = ("off", "active", "frozen")
MODES = ("hold", "apply")
LEGAL_STATE_PAIRS = frozenset({("off", "active"), ("active", "frozen"), ("frozen", "active"), ("frozen", "off")})
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
    # §6A I3 / I4 (OD-5, A5a-R2): the two bodies that were SKIPPED_COMPARISONS until Phase 2B pinned them
    "set_updated_at": "a09aad262b073272ea783771aa8e40de",
    "donations_require_eligible_creator": "dd4165dcb779d3f952d4b66796ea561a",
}
PIN_ROWS = {"set_updated_at": "I3", "donations_require_eligible_creator": "I4"}  # compared only while that row is active
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
# revision 7: every evidence event, attributed or not (I12-H raw counts and eligibility, I16, I17)
ALL_EVENTS = ("SELECT e.id::text AS id, e.kind, e.provider_ref, e.provider_payment_id, "
              "e.donation_id::text AS donation_id, "
              "e.account, e.match_type, e.phase, e.status, e.amount_paise, e.currency, e.amount_deducted_paise, "
              "e.payment_amount_refunded_paise AS snapshot, e.provider_event_at, e.received_at, e.recorded_at "
              "FROM public.payment_integrity_events e ORDER BY e.provider_ref, e.id")
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


def ref_states(events, did, each=iter, limit: int | None = None) -> list | None:
    """Decision 5: every current row one ref could have had for donation `did` under some commit order: the ranked
    winner, with its conflicting flag, of any non-empty subset of its append-only events, or no row at all. None
    over the bound. `each` is the §2.7.3 checkpoint (one unit per subset and per member scanned, as it runs).
    `limit`: stop as soon as more than `limit` distinct states exist (the caller then knows the combination product
    is over the bound, and needs no further expansion)."""
    if len(events) > MAX_REF_EVENTS:
        return None
    out = {None: None}
    for k in range(1, len(events) + 1):
        for subset in each(itertools.combinations(events, k)):
            w = max(each(subset), key=_rank_key)
            if w["donation_id"] != did:  # the view shows only this donation's rows
                continue
            top = _rank_key(w)[:2]
            group = {e["status"] for e in each(subset) if _rank_key(e)[:2] == top}
            row = {**{f: w[f] for f in ROW_FIELDS}, "conflicting": len(group) > 1}
            out.setdefault(tuple(sorted(row.items(), key=lambda kv: kv[0])), row)
            if limit is not None and len(out) > limit:
                return [r for r in each(out.values())]  # provably over the bound: the rest is not needed
    return [r for r in each(out.values())]


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

# I10 (revision 7 §5): names and logical identities only, never OIDs in the output
CAT_ROLES = """SELECT r.oid::bigint AS oid, r.rolname::text AS name, r.rolsuper AS super, r.rolinherit AS inherit,
    r.rolcreaterole AS createrole, r.rolcreatedb AS createdb, r.rolreplication AS replication,
    r.rolbypassrls AS bypassrls, r.rolcanlogin AS login FROM pg_catalog.pg_roles r ORDER BY r.rolname"""
CAT_MEMBERS_16 = """SELECT m.member::bigint AS member, m.roleid::bigint AS role, m.admin_option,
    m.inherit_option, m.set_option FROM pg_catalog.pg_auth_members m"""
CAT_MEMBERS_15 = """SELECT m.member::bigint AS member, m.roleid::bigint AS role, m.admin_option,
    NULL::boolean AS inherit_option, NULL::boolean AS set_option FROM pg_catalog.pg_auth_members m"""
CAT_DATABASE = """SELECT pg_catalog.pg_get_userbyid(d.datdba)::text AS owner FROM pg_catalog.pg_database d
    WHERE d.datname = current_database()"""
CAT_RELATIONS = """SELECT c.relname::text AS rel, c.relkind::text AS kind,
    pg_catalog.pg_get_userbyid(c.relowner)::text AS owner, c.relrowsecurity AS rls, c.relforcerowsecurity AS force_rls,
    EXISTS (SELECT 1 FROM pg_catalog.pg_policy p WHERE p.polrelid = c.oid) AS policy
    FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname = ANY(%(rels)s)"""
CAT_SCHEMA = """SELECT pg_catalog.pg_get_userbyid(n.nspowner)::text AS owner FROM pg_catalog.pg_namespace n
    WHERE n.nspname = 'public'"""
CAT_TABLE_RIGHTS = """WITH principals AS (SELECT r.rolname::text AS name FROM pg_catalog.pg_roles r
                    UNION ALL SELECT 'public')
    SELECT p.name AS role, c.relname::text AS rel, priv
    FROM principals p CROSS JOIN pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']) AS priv
    WHERE n.nspname = 'public' AND c.relname = ANY(%(rels)s) AND has_table_privilege(p.name, c.oid, priv)"""
# U-5 (A5b): PostgreSQL 17 adds the MAINTAIN table privilege; the effective-rights question must include it there.
CAT_TABLE_RIGHTS_17 = """WITH principals AS (SELECT r.rolname::text AS name FROM pg_catalog.pg_roles r
                    UNION ALL SELECT 'public')
    SELECT p.name AS role, c.relname::text AS rel, priv
    FROM principals p CROSS JOIN pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER', 'MAINTAIN']) AS priv
    WHERE n.nspname = 'public' AND c.relname = ANY(%(rels)s) AND has_table_privilege(p.name, c.oid, priv)"""
CAT_COLUMN_RIGHTS = """WITH principals AS (SELECT r.rolname::text AS name FROM pg_catalog.pg_roles r
                    UNION ALL SELECT 'public')
    SELECT p.name AS role, c.relname::text AS rel, a.attname::text AS col, priv
    FROM principals p CROSS JOIN pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
    CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'REFERENCES']) AS priv
    WHERE n.nspname = 'public' AND c.relname = ANY(%(rels)s)
      AND has_column_privilege(p.name, c.oid, a.attnum, priv) AND NOT has_table_privilege(p.name, c.oid, priv)"""
CAT_FUNCTION_RIGHTS = """WITH principals AS (SELECT r.rolname::text AS name FROM pg_catalog.pg_roles r
                    UNION ALL SELECT 'public')
    SELECT p.name AS role, f.proname::text AS name, pg_catalog.pg_get_function_identity_arguments(f.oid)::text AS args
    FROM principals p CROSS JOIN pg_catalog.pg_proc f JOIN pg_catalog.pg_namespace n ON n.oid = f.pronamespace
    WHERE n.nspname = 'public' AND f.proname = ANY(%(names)s) AND has_function_privilege(p.name, f.oid, 'EXECUTE')"""
CAT_SCHEMA_CREATE = """WITH principals AS (SELECT r.rolname::text AS name FROM pg_catalog.pg_roles r
                    UNION ALL SELECT 'public')
    SELECT p.name AS role FROM principals p WHERE has_schema_privilege(p.name, 'public', 'CREATE')"""
CAT_FUNCTION_ACL = """SELECT p.proname::text AS name,
    pg_catalog.pg_get_function_identity_arguments(p.oid)::text AS args,
    CASE WHEN x.grantee = 0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(x.grantee)::text END AS grantee,
    x.privilege_type::text AS privilege, x.is_grantable AS grantable
    FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
    CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) x
    WHERE n.nspname = 'public' AND p.proname = ANY(%(names)s)"""
CAT_SCHEMA_ACL = """SELECT CASE WHEN x.grantee = 0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(x.grantee)::text END
    AS grantee, x.privilege_type::text AS privilege, x.is_grantable AS grantable
    FROM pg_catalog.pg_namespace n CROSS JOIN LATERAL aclexplode(COALESCE(n.nspacl, acldefault('n', n.nspowner))) x
    WHERE n.nspname = 'public'"""
CAT_TABLE_ACL = """SELECT c.relname::text AS rel,
    CASE WHEN x.grantee = 0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(x.grantee)::text END AS grantee,
    x.privilege_type::text AS privilege, x.is_grantable AS grantable
    FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) x
    WHERE n.nspname = 'public' AND c.relname = ANY(%(rels)s)"""
CAT_COLUMN_ACL = """SELECT c.relname::text AS rel, a.attname::text AS col,
    CASE WHEN x.grantee = 0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(x.grantee)::text END AS grantee,
    x.privilege_type::text AS privilege, x.is_grantable AS grantable
    FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
                                    AND a.attacl IS NOT NULL
    CROSS JOIN LATERAL aclexplode(a.attacl) x
    WHERE n.nspname = 'public' AND c.relname = ANY(%(rels)s)"""
CAT_TRIGGERS = """SELECT c.relname::text AS rel, t.tgname::text AS name, fn.nspname::text AS fn_schema,
    p.proname::text AS fn_name, pg_catalog.pg_get_function_identity_arguments(p.oid)::text AS fn_args,
    t.tgtype::int AS tgtype, t.tgenabled::text AS enabled, t.tgnargs::int AS tgnargs,
    encode(t.tgargs, 'hex') AS tgargs, t.tgdeferrable AS deferrable, t.tginitdeferred AS initdeferred,
    t.tgconstraint <> 0 AS is_constraint, t.tgqual IS NULL AS no_qual,
    ARRAY(SELECT a.attname::text FROM pg_catalog.pg_attribute a
           WHERE a.attrelid = t.tgrelid AND a.attnum = ANY (t.tgattr::int2[]) ORDER BY a.attnum) AS update_cols
    FROM pg_catalog.pg_trigger t
    JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_proc p ON p.oid = t.tgfoid
    JOIN pg_catalog.pg_namespace fn ON fn.oid = p.pronamespace
    WHERE n.nspname = 'public' AND NOT t.tgisinternal AND c.relname = ANY(%(rels)s)"""
CAT_FUNCTIONS = """SELECT n.nspname::text AS schema, p.proname::text AS name,
    pg_catalog.pg_get_function_identity_arguments(p.oid)::text AS args,
    format_type(p.prorettype, NULL)::text AS returns, l.lanname::text AS lanname, p.prokind::text AS prokind,
    p.provolatile::text AS provolatile, p.proparallel::text AS proparallel, p.proisstrict AS proisstrict,
    p.proleakproof AS proleakproof, p.prosecdef AS prosecdef, p.proconfig::text[] AS proconfig,
    pg_catalog.pg_get_userbyid(p.proowner)::text AS owner, md5(p.prosrc) AS src, p.probin IS NULL AS no_bin
    FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
    JOIN pg_catalog.pg_language l ON l.oid = p.prolang
    WHERE n.nspname = 'public' AND p.proname = ANY(%(names)s)"""


def _select(cur, sql: str, params=None) -> list[dict]:
    """Every data statement of the verifier goes through here: SELECT / WITH only (STOP-2)."""
    head = sql.lstrip().split(None, 1)[0].upper()
    if head not in ("SELECT", "WITH"):
        raise Refused(f"non-SELECT statement refused: {head}")
    if isinstance(cur, Session):  # verify(): the §2.7 protocol, deadline and ceilings
        return cur.select(sql, params)
    cur.execute(sql, params)
    return cur.fetchall()


# ── runtime (revision 7 §2.7): one monotonic deadline, the exact statement protocol, bounded reads ─────────────────
# PROVISIONAL ceilings (owner item 7): the values follow the §8 measurements and need owner approval
STATEMENT_CAP_MS = 120_000   # §2.7.3: min(120 000, remaining)
MIN_STATEMENT_MS = 50        # refuse before issuing below this (never statement_timeout = 0)
FETCH_BATCH = 5_000          # rows per FETCH of a named cursor
MAX_ROWS = 5_000_000         # rows fetched over the whole run
MAX_BYTES = 2_000_000_000    # approximate in-memory size of the fetched values over the whole run
MAX_WORK = 200_000_000       # CPU work units (enumeration steps, donations classified)
CHECKPOINT_EVERY = 1_024     # work units between deadline checks
PROTOCOL = tuple(re.compile(x) for x in (
    r"BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY",
    r"SET LOCAL statement_timeout = '[1-9][0-9]{0,8}ms'",
    r"SET LOCAL lock_timeout = '5s'",
    r"SET LOCAL row_security = off",
    r"FETCH FORWARD [1-9][0-9]{0,8} FROM vfy_[1-9][0-9]{0,8}",
    r"CLOSE vfy_[1-9][0-9]{0,8}",
    r"ROLLBACK",
))
_DECLARE = re.compile(r"DECLARE (vfy_[1-9][0-9]{0,8}) NO SCROLL CURSOR WITHOUT HOLD FOR (.*)", re.S)


def statement_allowed(stmt: str) -> bool:
    """§2.7.2: an approved module-constant data query, a DECLARE wrapping a cursored constant, or one of the fixed
    protocol statements with validated integers and generated cursor names. Nothing else is ever issued."""
    if stmt in _APPROVED_SQL:
        return True
    m = _DECLARE.fullmatch(stmt)
    if m:
        return m.group(2) in _APPROVED_SQL  # every data query is read through a bounded cursor
    return any(x.fullmatch(stmt) for x in PROTOCOL)


class Budget:
    """§2.7.1 / §2.7.3: the whole-run monotonic deadline and the CPU work ceiling."""

    def __init__(self, max_seconds, clock=time.monotonic):
        if isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) \
                or not math.isfinite(max_seconds) or max_seconds <= 0:
            raise Refused("max_seconds must be a finite number > 0 (there is no production default)")
        self.clock = clock
        self.units = self.rows = self.bytes = 0  # actual: work done, rows and bytes retained (reported)
        self.deadline = clock() + max_seconds

    def remaining_ms(self) -> int:
        return math.floor((self.deadline - self.clock()) * 1000)

    def expired(self) -> bool:
        return self.clock() > self.deadline

    def work(self, n: int = 1):
        before, self.units = self.units, self.units + n
        if self.units > MAX_WORK:
            raise Refused("work ceiling exceeded")
        if before // CHECKPOINT_EVERY != self.units // CHECKPOINT_EVERY and self.expired():
            raise Refused("deadline exceeded")

    def check(self):
        """A checkpoint before / after a non-interruptible operation (a sort, a phase, serialization)."""
        if self.expired():
            raise Refused("deadline exceeded")

    def each(self, items):
        """§2.7.3: iterate `items`, charging one work unit per item AS it is produced, so the clock is checked at
        least every CHECKPOINT_EVERY items of actual progress (never a bulk charge before a scan)."""
        for x in items:
            self.work()
            yield x

    def collect(self, *iterables) -> set:
        """A set (union) built incrementally: one unit per item inserted, so no union or copy is eager."""
        out = set()
        for items in iterables:
            for x in self.each(items):
                out.add(x)
        return out

    def sort(self, items, key=None) -> list:
        """The accepted bounded atomic operation (§2.7.3): one sort of an already-built sized collection, its
        n log n comparisons charged (the work ceiling enforced) before it runs, the deadline checked immediately
        before and after it, so two such sorts never run back to back without a check."""
        n = len(items)
        self.work(n * max(1, n.bit_length()))
        self.check()
        out = sorted(items, key=key)
        self.check()
        return out


class _NoBudget:
    """Unit tests drive a Verifier without verify(): no deadline and no work ceiling (the report size still is)."""

    units = rows = bytes = 0

    def work(self, n: int = 1):
        pass

    def check(self):
        pass

    def each(self, items):
        return items

    def collect(self, *iterables) -> set:
        out = set()
        for items in iterables:
            out.update(items)
        return out

    def sort(self, items, key=None) -> list:
        return sorted(items, key=key)


def _each(items, tick):
    """Budget.each for a bare work callback (the pure I10 classifier)."""
    for x in items:
        tick()
        yield x


def _value_bytes(value, limit: int | None = None, budget=None) -> int:
    """The retained size of a value and of everything nested in it (sys.getsizeof of each distinct object: strings,
    bytes, numbers, lists, tuples, sets, dicts and their keys, so JSON / jsonb values and arrays are counted in
    full). Iterative, so no recursion limit applies; each object is counted once, so a cycle terminates; and it stops
    as soon as `limit` is exceeded, so its own work is bounded by the limit (a container's own size, which covers its
    element pointers, is counted before its elements are visited)."""
    total, seen, stack = 0, set(), [value]
    while stack:
        v = stack.pop()
        if id(v) in seen:
            continue
        seen.add(id(v))
        if budget is not None and len(seen) % CHECKPOINT_EVERY == 0:
            budget.check()  # a clock check during the walk itself; measuring is not counted as work
        total += sys.getsizeof(v)
        if limit is not None and total > limit:
            return total
        if isinstance(v, dict):
            stack.extend(v.keys())
            stack.extend(v.values())
        elif isinstance(v, (list, tuple, set, frozenset)):
            stack.extend(v)
    return total


class Session:
    """The only path to the server inside verify(): every statement is checked against the protocol, every data
    statement and FETCH gets min(120 s, remaining) and is refused below 50 ms, and every fetched row counts against
    the row and byte ceilings. Large parameterless reads use a named NO SCROLL cursor fetched in batches."""

    def __init__(self, cur, budget: Budget):
        self.cur, self.budget = cur, budget
        self.issued = self._cursors = 0  # a count, not a retained log (each DECLARE carries its full SQL)
        self._timeout_ms = None

    def _issue(self, stmt: str, params=None):
        if not statement_allowed(stmt):
            raise Refused("statement outside the approved protocol")
        self.issued += 1
        if params is None:
            self.cur.execute(stmt)
        else:
            self.cur.execute(stmt, params)

    def protocol(self, stmt: str):
        self._issue(stmt)

    def _cap(self):
        ms = self.budget.remaining_ms()
        if ms < MIN_STATEMENT_MS:
            raise Refused("deadline: less than 50 ms remain for the next statement")
        ms = min(STATEMENT_CAP_MS, ms)
        if ms != self._timeout_ms:
            self._issue(f"SET LOCAL statement_timeout = '{ms}ms'")
            self._timeout_ms = ms

    def _retain(self, batch: list, out: list):
        """Each row is counted (rows, nested bytes) before it is kept: a ceiling refuses before excess is retained."""
        b = self.budget
        for row in b.each(batch):
            b.rows += 1
            if b.rows > MAX_ROWS:
                raise Refused("row ceiling exceeded")
            b.bytes += _value_bytes(row, MAX_BYTES - b.bytes, b)
            if b.bytes > MAX_BYTES:
                raise Refused("byte ceiling exceeded")
            out.append(row)

    def select(self, sql: str, params=None) -> list:
        """Every data statement, parameterised or not: DECLARE a NO SCROLL cursor over the approved constant, FETCH
        at most FETCH_BATCH rows (never more than one row past the row ceiling), CLOSE."""
        if sql not in _APPROVED_SQL:
            raise Refused("data statement is not an approved module constant")
        self._cursors += 1
        name = f"vfy_{self._cursors}"
        self._cap()
        self._issue(f"DECLARE {name} NO SCROLL CURSOR WITHOUT HOLD FOR {sql}", params)
        out = []
        while True:
            n = min(FETCH_BATCH, MAX_ROWS - self.budget.rows + 1)
            self._cap()
            self._issue(f"FETCH FORWARD {n} FROM {name}")
            batch = self.cur.fetchall()
            self._retain(batch, out)
            if len(batch) < n:
                break
        self._issue(f"CLOSE {name}")  # cleanup: exempt from the 50 ms rule; an error path is closed by ROLLBACK
        return out


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


def parse_scope(scope, created_at: datetime, each=iter) -> tuple[str, dict | None]:
    """('unscoped', None) for {} or no "kind"; ('malformed', None) for any defect (not an object, an unknown kind or
    key, a missing key, wrong types, an invalid uuid, an empty or duplicated list, an expires_at that is missing,
    unparsable, before created_at or more than 24 h after it); else ('valid', {kind, donations, creator, expires})."""
    if not isinstance(scope, dict):
        return "malformed", None
    if "kind" not in scope:  # rev. 3 §6: "missing scope ({} or no kind)"
        return "unscoped", None
    kind = scope["kind"]
    # equal to set(scope) != SCOPE_KEYS[kind] (dict keys are unique), in constant work whatever the scope's size
    if kind not in SCOPE_KEYS or len(scope) != len(SCOPE_KEYS[kind]) or any(k not in scope for k in SCOPE_KEYS[kind]):
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
        donations = set()  # one set, built as the ids are validated: each id normalized and hashed once
        for i in each(ids):  # each: the verifier's checkpoint over one scope's list
            u = _uuid(i)
            if u is None:
                return "malformed", None
            before = len(donations)
            donations.add(u)
            if len(donations) == before:  # a duplicate: the insertion did not grow the set
                return "malformed", None
        info["donations"] = donations
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


def _norm(v):
    if isinstance(v, dict):
        return {str(k): _norm(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        items = [_norm(x) for x in v]
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


def canonical(obj) -> str:
    return json.dumps(_norm(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_stream(obj, limit: int, budget=None) -> tuple[int, str]:
    """The exact length and SHA-256 of canonical(obj), streamed chunk by chunk without building the string, refused
    as soon as the length exceeds `limit`. The one normalization copy (whose source the caller has bounded) is the
    accepted atomic step, with a deadline check before and after it; every chunk is then a checkpoint."""
    enc = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if budget is not None:
        budget.check()
    normalized = _norm(obj)
    if budget is not None:
        budget.check()
    total, digest = 0, hashlib.sha256()
    for chunk in enc.iterencode(normalized):
        total += len(chunk)
        if total > limit:
            raise Refused("report byte ceiling exceeded")
        digest.update(chunk.encode("ascii"))
        if budget is not None:
            budget.work()
    return total, digest.hexdigest()


def canonical_size(obj, limit: int, budget=None) -> int:
    """The exact length of canonical(obj), streamed (see canonical_stream)."""
    return canonical_stream(obj, limit, budget)[0]


def finish(report: dict) -> dict:
    body = {k: v for k, v in report.items() if k != "digest"}
    report["digest"] = hashlib.sha256(canonical(body).encode("ascii")).hexdigest()
    return report


def refused_report(reason: str, scope: dict | None = None) -> dict:
    return finish({"verifier": VERIFIER_VERSION, "status": "refused", "exit_code": EXIT_CODES["refused"],
                   "reason": reason, "scope": scope or {}, "findings": [], "counts": {},
                   "gates": dict.fromkeys(GATE_KEYS, False)})


# ── revision 7 §3.2 / §4.2: closed finding variants and the write-blocker predicate ──────────────────────────────────
REF_FORMAT = re.compile(r"^(disp|rfnd)_[A-Za-z0-9]{1,40}$")  # 0029's provider_ref CHECK
PRODUCER_CURRENCIES = frozenset(EXCHANGE_RATES_TO_INR)        # integrity._refund_snapshot's two-decimal set
UNIT_STATUSES = frozenset({"comparable", "currency_unknown", "unsupported_currency", "currency_mismatch"})
VARIANT_CLASSES = frozenset({"historical_ambiguous", "evidence_attribution", "refund_snapshot_mismatch"})
RESERVED = frozenset({
    "enumeration", "chain", "basis", "bound", "events", "combinations", "ref", "refs", "run",
    "subject", "source_ids", "members", "snapshot_paise", "processed_paise", "pending_paise", "conditional",
    "conditional_match", "earlier_id", "earlier_paise", "earlier_at", "later_id", "later_paise", "later_at",
    "regressions", "payment", "account", "stored_donation", "versions", "refund_currency", "donation_currency"})
DIAGNOSTIC = frozenset({
    "donation", "stored", "modes_checked", "supporting_modes", "candidates", "max_impact_paise",
    "superseded_incomparable", "newer_unsnapshotted_events", "older_snapshot_paise", "older_snapshot_at",
    "snapshot_at", "kind", "status", "amount_paise", "deducted_paise", "currency", "detail_note",
    "hold", "applied_paise", "processed_refunds_paise", "unapplied_refunds_paise"})  # F-12: harmless diagnostics


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _eq(x):
    return lambda v: v == x


def _in(*xs):
    return lambda v: v in xs


def _int_gt(n):
    return lambda v: _is_int(v) and v > n


def _each_aware(fn):
    """Marks a domain / cond that takes the §2.7.3 checkpoint iterator (it scans a list of unbounded length)."""
    fn.uses_each = True
    return fn


def _ids(lo, hi=None):
    return _each_aware(lambda v, each=iter: (isinstance(v, list) and lo <= len(v) <= (hi or len(v))
                                             and all(isinstance(x, str) and _uuid(x) == x for x in each(v))))


_REF = lambda v: isinstance(v, str) and bool(REF_FORMAT.match(v))  # noqa: E731
_TEXT = lambda v: isinstance(v, str) and v != ""  # noqa: E731
_UUID1 = lambda v: isinstance(v, str) and _uuid(v) == v  # noqa: E731
_TS = lambda v: isinstance(v, datetime)  # noqa: E731
_BOOL = lambda v: isinstance(v, bool)  # noqa: E731
_BASIS = _in("mode", "evidence")
_CHAINS = _in("valid", "invalid")


_MEMBER_KEYS = frozenset({"id", "unit_status", "currency", "value"})


@_each_aware
def _members(v, each=iter) -> bool:
    # set(m) == _MEMBER_KEYS without building a set per member: same size and every expected key present
    return isinstance(v, list) and bool(v) and all(
        isinstance(m, dict) and len(m) == len(_MEMBER_KEYS) and all(k in m for k in _MEMBER_KEYS)
        and m["unit_status"] in UNIT_STATUSES for m in each(v))


def _same_multiset(a: list, b: list, each=iter) -> bool:
    """sorted(a) == sorted(b) as a checkpointed count instead of two sorts: equal lengths and equal multiplicities.
    An unhashable element (which sorted() could not order consistently either) is never equal: refused."""
    if len(a) != len(b):
        return False
    counts = {}
    try:
        for x in each(a):
            counts[x] = counts.get(x, 0) + 1
        for y in each(b):
            c = counts.get(y, 0)
            if c == 0:
                return False
            counts[y] = c - 1
    except TypeError:
        return False
    return True


@_each_aware
def _s2_cond(d, each=iter) -> bool:
    """V-S2: the members are exactly the source ids (as a multiset) and at least one member is not comparable."""
    return (_same_multiset([m["id"] for m in each(d["members"])], d["source_ids"], each)
            and any(m["unit_status"] != "comparable" for m in each(d["members"])))


@dataclass(frozen=True)
class Variant:
    name: str
    required: dict
    optional: dict = field(default_factory=dict)
    cond: object = None         # payload -> bool
    blockers: object = None     # (reason, payload) -> set of blocker codes
    cross_check: object = None  # (finding, findings, replicas) -> bool


def _s2_blockers(reason, d):
    return {"s2_over_bound"} | ({"chain_invalid"} if d["chain"] == "invalid" else set())


def replica_index(findings, each=iter) -> frozenset:
    """The donations with an I10 structural fp_replica_mismatch finding (V-A4's cross-check), built once."""
    return frozenset(g.entity_id for g in each(findings) if g.invariant == "I10" and g.cls == "structural"
                     and g.detail.get("problem") == "fp_replica_mismatch")


def _replica_found(f, findings, replicas=None) -> bool:
    if replicas is None:  # a caller without the prebuilt index (unit tests): build it here
        replicas = replica_index(findings)
    return f.detail.get("donation") in replicas


def _snapshot_totals(rule):
    return Variant("V-S" + rule[0], {"subject": _eq("snapshot"), "source_ids": _ids(1), "snapshot_paise": _is_int,
                                     "processed_paise": _is_int, "pending_paise": _is_int},
                   {"conditional": _BOOL}, cond=rule[1], blockers=lambda r, d: {"i17:" + r})


_H1 = Variant("V-H1", {"enumeration": _eq("complete"), "chain": _eq("valid"), "basis": _BASIS})
_H2 = Variant("V-H2", {"enumeration": _eq("complete"), "chain": _eq("invalid"), "basis": _BASIS},
              blockers=lambda r, d: {"chain_invalid"})
_H3 = Variant("V-H3", {"enumeration": _eq("over_bound"), "bound": _eq("ref_events"), "chain": _CHAINS,
                       "basis": _eq("evidence"), "ref": _REF, "events": _int_gt(12)}, blockers=_s2_blockers)
_H4 = Variant("V-H4", {"enumeration": _eq("over_bound"), "bound": _eq("combinations"), "chain": _CHAINS,
                       "basis": _eq("evidence"), "combinations": _int_gt(4096)}, blockers=_s2_blockers)
_A = {"run": _is_int}
_I = {"ref": _REF, "payment": _TEXT, "account": _TEXT, "source_ids": _ids(1)}
_R = {"subject": _eq("refund"), "ref": _REF, "source_ids": _ids(1, 1)}
_I17 = lambda r, d: {"i17:" + r}  # noqa: E731
VARIANTS = {
    ("I12-H", "historical_ambiguous", "mode_order"): (_H1, _H2),
    ("I12-H", "historical_ambiguous", "evidence_order"): (_H1, _H2),
    ("I12-H", "historical_ambiguous", "evidence_cut"): (_H1, _H2, _H3, _H4),
    ("I12", "historical_ambiguous", "ack_mode_order"): (
        Variant("V-A1", {"enumeration": _eq("complete"), "basis": _BASIS, **_A}),
        Variant("V-A2", {"enumeration": _eq("over_bound"), "bound": _eq("ref_events"), "ref": _REF,
                         "events": _int_gt(12), **_A}, {"basis": _BASIS}, blockers=lambda r, d: {"a1_over_bound"}),
        Variant("V-A3", {"enumeration": _eq("over_bound"), "bound": _eq("combinations"),
                         "combinations": _int_gt(4096), **_A}, {"basis": _BASIS},
                blockers=lambda r, d: {"a1_over_bound"}),
        Variant("V-A4", {"enumeration": _eq("replica_failed"), **_A}, {"basis": _BASIS},
                blockers=lambda r, d: {"a1_replica_failed"}, cross_check=_replica_found)),
    **{("I16", "evidence_attribution", r): (Variant("V-I1", _I, blockers=lambda r, d: {"i16:" + r}),)
       for r in ("unattributed", "account_mismatch")},
    ("I16", "evidence_attribution", "wrong_donation"): (
        Variant("V-I2", {**_I, "stored_donation": _UUID1},
                blockers=lambda r, d: {"i16:" + r},
                cross_check=lambda f, fs, replicas=None: f.detail["stored_donation"] != f.entity_id),),
    ("I16", "evidence_attribution", "ref_identity_conflict"): (
        Variant("V-I3", {**_I, "versions": lambda v: _is_int(v) and v >= 2}, blockers=lambda r, d: {"i16:" + r}),),
    ("I17", "refund_snapshot_mismatch", "snapshot_conflict"): (
        Variant("V-S1", {"subject": _eq("snapshot"), "source_ids": _ids(2)}, blockers=_I17),),
    ("I17", "refund_snapshot_mismatch", "snapshot_unit_conflict"): (
        Variant("V-S2", {"subject": _eq("snapshot"), "source_ids": _ids(1), "members": _members},
                cond=_s2_cond, blockers=_I17),),
    ("I17", "refund_snapshot_mismatch", "snapshot_regression"): (
        Variant("V-S3", {"subject": _eq("snapshot"), "source_ids": _ids(2, 2), "earlier_id": _UUID1, "earlier_at": _TS,
                         "later_id": _UUID1, "later_at": _TS,
                         "earlier_paise": _is_int, "later_paise": _is_int,
                         "regressions": lambda v: _is_int(v) and v >= 1},
                cond=lambda d: (d["source_ids"] == [d["earlier_id"], d["later_id"]]
                                and d["earlier_at"] < d["later_at"] and d["earlier_paise"] > d["later_paise"]),
                blockers=_I17),),
    ("I17", "refund_snapshot_mismatch", "pending_refunds"): (
        _snapshot_totals(("4", lambda d: d["processed_paise"] < d["snapshot_paise"]
                          <= d["processed_paise"] + d["pending_paise"])),),
    ("I17", "refund_snapshot_mismatch", "missing_refund_evidence"): (
        _snapshot_totals(("5", lambda d: d["snapshot_paise"] > d["processed_paise"] + d["pending_paise"])),),
    ("I17", "refund_snapshot_mismatch", "snapshot_behind"): (
        _snapshot_totals(("6", lambda d: d["snapshot_paise"] < d["processed_paise"])),),
    ("I17", "refund_snapshot_mismatch", "amount_unknown"): (
        Variant("V-R1", _R, {"conditional_match": _BOOL}, blockers=_I17),),
    ("I17", "refund_snapshot_mismatch", "currency_unknown"): (
        Variant("V-R2", _R, {"conditional_match": _BOOL}, blockers=_I17),),
    ("I17", "refund_snapshot_mismatch", "currency_mismatch"): (
        Variant("V-R3", {**_R, "refund_currency": _in(*PRODUCER_CURRENCIES),
                         "donation_currency": _in(*PRODUCER_CURRENCIES)}, {"conditional_match": _BOOL},
                cond=lambda d: d["refund_currency"] != d["donation_currency"], blockers=_I17),),
    ("I17", "refund_snapshot_mismatch", "unsupported_currency"): (
        Variant("V-R4", {**_R, "refund_currency": lambda v: isinstance(v, str) and v not in PRODUCER_CURRENCIES},
                {"conditional_match": _BOOL}, blockers=_I17),
        Variant("V-D1", {"subject": _eq("donation"),
                         "donation_currency": lambda v: v is None or (isinstance(v, str)
                                                                    and v not in PRODUCER_CURRENCIES)},
                blockers=_I17)),
}


def _apply(fn, value, each):
    return fn(value, each) if getattr(fn, "uses_each", False) else fn(value)


def _matches(v: Variant, payload: dict, each=iter) -> bool:
    """The key-set rules run over the finding's field names (a fixed, emitter-defined set: constant size); every
    list-valued domain and cross-field rule scans through `each`."""
    keys = set(payload)
    if not (keys & RESERVED) <= (set(v.required) | set(v.optional)):  # an unlisted reserved field: not this variant
        return False
    if not set(v.required) <= keys:
        return False
    if not all(_apply(v.required[k], payload[k], each) for k in v.required):
        return False
    if not all(_apply(v.optional[k], payload[k], each) for k in v.optional if k in keys):
        return False
    return v.cond is None or bool(_apply(v.cond, payload, each))


def write_blockers(f, findings, replicas=None, each=iter) -> frozenset:
    """§4.2: the write blockers one finding carries. A finding of a variant class must match exactly one closed
    variant (§3.2), with `reason` validated as the discriminator outside the payload (revision 7); anything else
    refuses the whole report."""
    if f.cls not in SEVERITY:
        raise Refused(f"unknown class {f.cls!r}")
    if f.cls not in VARIANT_CLASSES:
        return frozenset()
    reason = f.detail.get("reason")
    if not isinstance(reason, str):
        raise Refused(f"missing or invalid reason on {f.invariant} {f.cls}")
    payload = {k: x for k, x in f.detail.items() if k != "reason"}
    for k in payload:
        if k not in RESERVED and k not in DIAGNOSTIC and not k.startswith("diag_"):
            raise Refused(f"unknown field {k!r} on {f.invariant} {f.cls}")
    cands = [v for v in VARIANTS.get((f.invariant, f.cls, reason), ()) if _matches(v, payload, each)]
    if len(cands) != 1:
        raise Refused(f"no single variant for {f.invariant} {f.cls} {reason!r} ({len(cands)} match)")
    v = cands[0]
    if v.cross_check is not None and not v.cross_check(f, findings, replicas):
        raise Refused(f"cross-check failed for {v.name}")
    return frozenset(v.blockers(reason, payload)) if v.blockers else frozenset()


def status_of(classes) -> str:
    """Status = the maximum severity over the emitted classes (C-IR6). An unknown class refuses the report."""
    worst = 0
    for cls in classes:
        if cls not in SEVERITY:
            raise Refused(f"unknown class {cls!r}")
        worst = max(worst, RANK[SEVERITY[cls]])
    return STATUS_OF[worst]


def gates(status: str, complete: bool, state: str, counts: dict, blockers=frozenset()) -> dict:
    """The §4.3 gates. activation_complete also needs the 6a-3 second report-only reconcile to plan zero changes,
    which verify never attests (§4.3, §9.1): it is therefore ALWAYS false in a verify report (CR-1). The part verify
    can establish (full scope, active, status clean) is reported separately as activation_verification_ready: clean
    is necessary, never sufficient. Nothing in a report activates or authorizes activation. `complete` is SCOPE
    completeness only; `blockers` (revision 7 §4.3) closes both write-capable preconditions."""
    full_active = complete and state == "active"
    violations = {c: n for c, n in counts.items() if SEVERITY[c] == "violation" and n}
    recovery_ok = full_active and not (set(violations) - {"overdue_reconciliation"}) and not blockers
    g = {"reconcile_precondition": full_active and not violations and not blockers,
         "recovery_precondition": recovery_ok,
         "activation_verification_ready": full_active and status == "clean",
         "activation_complete": False}  # needs the unattested 6a-3 condition (NOT_ATTESTED)
    if set(g) != set(GATE_KEYS) or g["activation_complete"] or (
            g["activation_verification_ready"] and (status != "clean" or not complete)) or (  # the §9 invariant
            blockers and (g["reconcile_precondition"] or g["recovery_precondition"] or status == "clean")):
        raise Refused("gate invariant violated")
    return g


# ── I10 (revision 7 §5): migration-derived expectations, classified over captured catalog rows ───────────────────
OWNER, MIGRATOR = "hyperchat_owner", "hyperchat_migrator"
APP_ROLES = (OWNER, MIGRATOR, *RUNTIME_ROLES)  # deploy/postgres/roles.sql
DONATION_WRITERS = ("hyperchat_app", "hyperchat_worker")  # 0007 RUNTIME table DML
ROLE_ATTRS = {"super": "rolsuper", "createrole": "rolcreaterole", "createdb": "rolcreatedb",
              "replication": "rolreplication", "bypassrls": "rolbypassrls"}
PROTECTED_TABLES = NO_RUNTIME_WRITES
RIGHTS_RELS = (*PROTECTED_TABLES, *APPEND_ONLY_EVIDENCE, "donations")
READ_RELATIONS = ("alembic_version", "donations", "streamers", "streamer_donator_totals", "donation_credit_events",
                  "donation_reversals", "donation_accounting_evaluations", "payment_integrity_events",
                  "payment_integrity_current", "payment_accounting_state", "payment_accounting_events",
                  "payment_accounting_runs", "payment_webhook_events")
OWNED_RELATIONS = tuple(r for r in READ_RELATIONS if r != "alembic_version")  # alembic_version: unverified
SCHEMA_OWNER_CANDIDATES = frozenset({"pg_database_owner", OWNER})  # the exact one is derived in Phase 2B
TRIGGER_RELS = (*PROTECTED_TABLES, *APPEND_ONLY_EVIDENCE, "donations")
# (relation, trigger) -> (function, timing, level, events), from the 0004-0034 DDL; no WHEN, no args, no UPDATE OF
TRIGGER_PINS = {
    ("donations", "donations_set_updated_at"): ("set_updated_at", "BEFORE", "ROW", frozenset({"UPDATE"})),
    ("donations", "donations_require_eligible_creator"):
        ("donations_require_eligible_creator", "BEFORE", "ROW", frozenset({"INSERT"})),
    ("donations", "donations_protect_history_columns"):
        ("donations_protect_history_columns", "BEFORE", "ROW", frozenset({"INSERT", "UPDATE"})),
    ("donations", "donations_credit_writer"):
        ("donations_credit_writer", "BEFORE", "ROW", frozenset({"INSERT", "UPDATE"})),
    ("donations", "donations_credit_delete_guard"):
        ("donations_credit_delete_guard", "BEFORE", "ROW", frozenset({"DELETE"})),
    ("donations", "donations_credit_delete_guard_truncate"):
        ("donations_credit_delete_guard", "BEFORE", "STATEMENT", frozenset({"TRUNCATE"})),
    ("donation_credit_events", "donation_credit_events_guard"):
        ("donation_credit_events_guard", "BEFORE", "ROW", frozenset({"UPDATE", "DELETE"})),
    ("donation_credit_events", "donation_credit_events_guard_truncate"):
        ("donation_credit_events_guard", "BEFORE", "STATEMENT", frozenset({"TRUNCATE"})),
    **{(t, f"{t}_guard"): ("payment_evidence_guard", "BEFORE", "ROW", frozenset({"UPDATE", "DELETE"}))
       for t in APPEND_ONLY_EVIDENCE},
    **{(t, f"{t}_guard_truncate"): ("payment_evidence_guard", "BEFORE", "STATEMENT", frozenset({"TRUNCATE"}))
       for t in APPEND_ONLY_EVIDENCE},
    ("payment_accounting_state", "payment_accounting_state_guard"):
        ("payment_accounting_state_guard", "BEFORE", "ROW", frozenset({"INSERT", "UPDATE", "DELETE"})),
    **{(t, f"{t}_guard"): ("payment_accounting_append_only", "BEFORE", "ROW", frozenset({"UPDATE", "DELETE"}))
       for t in ("payment_accounting_events", "payment_accounting_runs")},
    **{(t, f"{t}_guard_truncate"): ("payment_accounting_append_only", "BEFORE", "STATEMENT", frozenset({"TRUNCATE"}))
       for t in ("payment_accounting_state", "payment_accounting_events", "payment_accounting_runs")},
}


class _Unverified:
    """A baseline value that only the pristine PG15 / PG17 derivation (Phase 2B) can pin: never compared, and its
    presence keeps I10 failed closed (baseline_unverified)."""

    def __repr__(self):
        return "UNVERIFIED"


UNVERIFIED = _Unverified()
_SP, _SPC = ("search_path=pg_catalog, public",), ("search_path=pg_catalog",)
_SPT = ("search_path=pg_catalog, public, pg_temp",)  # 0043: pg_temp last in every SECURITY DEFINER search path
_ARGS_APPLY = "p_donation uuid, p_reversed bigint, p_hold text, p_flags text[], p_refs text[]"


def _fn(args, returns, lanname, config, volatile="v"):
    return {"args": args, "returns": returns, "lanname": lanname, "proconfig": config, "provolatile": volatile}


# CREATE FUNCTION headers of the head migrations (0001, 0020, 0029-0034); identity-argument text is unverified
FN_EXPECT = {
    "set_updated_at": _fn("", "trigger", "plpgsql", ("search_path=public",)),
    "donations_require_eligible_creator": _fn("", "trigger", "plpgsql", None),
    "payment_evidence_guard": _fn("", "trigger", "plpgsql", _SPC),
    "donations_protect_history_columns": _fn("", "trigger", "plpgsql", _SP),
    "donation_credit_events_guard": _fn("", "trigger", "plpgsql", _SPC),
    "donations_credit_writer": _fn("", "trigger", "plpgsql", _SP),
    "donations_credit_delete_guard": _fn("", "trigger", "plpgsql", _SPC),
    "payment_accounting_state_guard": _fn("", "trigger", "plpgsql", _SP),
    "payment_accounting_append_only": _fn("", "trigger", "plpgsql", _SP),
    # U-3 (A5b; A5a-R2 reconciled A = B = C on PG15 / PG17): the identity text of the two OUT arguments, no IN ones
    "payment_accounting_lock_state": _fn("OUT state text, OUT dispute_mode text", "record", "sql", _SP),
    "payment_evidence_fp": _fn("p_donation uuid", "text", "sql", _SP, volatile="s"),
    "donation_accounting_core": _fn(_ARGS_APPLY + ", p_cause text, p_run bigint", "text", "plpgsql", _SP),
    "donation_accounting_apply": _fn(_ARGS_APPLY, "text", "plpgsql", _SP),
    "payment_accounting_set_state": _fn("p_state text, p_mode text, p_operator text, p_reason text, p_ref text",
                                        "text", "plpgsql", (*_SP, "lock_timeout=5s")),
    "payment_accounting_open_run": _fn("p_command text, p_operator text, p_reason text, p_ref text, p_scope jsonb",
                                       "bigint", "sql", _SP),
    "payment_accounting_reconcile": _fn(_ARGS_APPLY + ", p_run bigint", "text", "plpgsql", _SP),
    "payment_accounting_keep": _fn("p_donation uuid, p_run bigint", "text", "plpgsql", _SP),
    "payment_accounting_apply_evaluation": _fn("p_donation uuid, p_reversed bigint, p_flags text[], p_refs text[], "
                                               "p_run bigint", "text", "plpgsql", _SP),
    "payment_accounting_revert": _fn("p_donation uuid, p_run bigint", "text", "plpgsql", _SP),
}
assert set(FUNCTION_PINS) <= set(FN_EXPECT) and {p[0] for p in TRIGGER_PINS.values()} <= set(FN_EXPECT)
FN_NAMES = tuple(sorted(FN_EXPECT))
# §5.4 function ACL baseline beyond the owner: (grantee, EXECUTE, is_grantable false); migration-derived
FN_ACL_BASELINE = {
    "donation_accounting_apply": frozenset({"hyperchat_app"}),        # 0034 APP_EXECUTE
    "payment_accounting_lock_state": frozenset({"hyperchat_app"}),    # 0034
    "set_updated_at": frozenset(DONATION_WRITERS),                    # 0007 GRANT ... TO RUNTIME
    "donations_require_eligible_creator": frozenset({"PUBLIC"}),      # 0020: the global default PUBLIC EXECUTE
}
# The head registry: every migration from 0034 on, in revision order, with the I10 objects it adds or replaces
# (OD-35-V for 0035: derived from its text, md5 of the rendered prosrc, as the 0034 pins above). A head's
# expectations are 0034's plus EVERY delta up to and including it (head_expectations).
# Scope (what I10 sees): functions by the pinned names only (FN_EXPECT and the deltas' "functions"), and triggers on
# TRIGGER_RELS only (the accounting tables, the evidence tables and donations). Triggers and functions on any other
# relation (streamers, streamer_voice_tiers, user_invites, jobs, kyc_*, stepup_*, the onboarding tables) are neither
# verified nor flagged. A migration that leaves that scope alone registers no_payment_delta(why); one that creates or
# replaces a pinned function, or creates a trigger on a TRIGGER_RELS table, must register it here, or I10 reports
# missing / body_drift / unexpected_trigger (fail closed). test_verify_rev7_units checks every migration file
# against this registry from its text, so a no_payment_delta marker cannot hide such a change.
_SCOPE_ARGS = "p_scope jsonb, p_created timestamp with time zone, p_command text, p_donation uuid"


def no_payment_delta(why: str) -> dict:
    """A registered head that adds or replaces nothing I10 verifies (why: the migration and why it is out of scope)."""
    return {"functions": {}, "pins": {}, "triggers": {}, "configs": {}, "why": "no payment-object delta: " + why}


def _with_pg_temp(name: str) -> tuple:
    """0043: the function's proconfig with search_path = pg_catalog, public, pg_temp; every other setting kept."""
    config = FN_EXPECT[name]["proconfig"]
    assert config[0] == _SP[0], name
    return (*_SPT, *config[1:])


HEAD_DELTAS = {
    "0034": {"functions": {}, "pins": {}, "triggers": {}, "configs": {}, "why": "the base: every value above"},
    "0035": {
        "why": "OD-35-V: the F-7 ack guard and the F-11 scope check, and the four run-bound owner functions",
        "functions": {"payment_accounting_ack_guard": _fn("", "trigger", "plpgsql", _SP),
                      "payment_accounting_scope_covers": _fn(_SCOPE_ARGS, "boolean", "plpgsql", _SP, volatile="s")},
        "pins": {"payment_accounting_ack_guard": "68d17111c7148d8235b990dc8cfad4fc",
                 "payment_accounting_scope_covers": "5a76bcd9e3fa99a913a78e5c0c3683eb",
                 "payment_accounting_reconcile": "e4bb1de5c6e0e4a85c81e7ac68cc81b4",
                 "payment_accounting_keep": "5a046a34f622f154a382a7a2506fc9fc",
                 "payment_accounting_apply_evaluation": "dcd997d14f6e302be6c4255335141e36",
                 "payment_accounting_revert": "0b16c1eff5dda4450c1b979277241833"},
        "triggers": {("donation_accounting_evaluations", "donation_accounting_evaluations_ack_guard"):
                     ("payment_accounting_ack_guard", "BEFORE", "ROW", frozenset({"INSERT", "UPDATE"}))},
        "configs": {},
    },
    "0036": no_payment_delta("PAY-14 creator pricing limits: runtime-role triggers on streamers and "
                             "streamer_voice_tiers (not TRIGGER_RELS), no pinned function"),
    "0037": no_payment_delta("SEC-A soundboard / reopen step-up anchors and NV-3 (the applicant's address in "
                             "application.approve): step-up guards only, no payment object"),
    "0038": no_payment_delta("onboarding invite guard exception: onboarding_issue_invite (lifecycle-only definer) "
                             "and the user_invites step-up guard (not TRIGGER_RELS), no pinned function"),
    "0039": no_payment_delta("creator image records: the creator_image_uploads table and its guard trigger (not "
                             "TRIGGER_RELS), no pinned function"),
    "0040": no_payment_delta("several Didit accounts: kyc_* tables, triggers and functions (not TRIGGER_RELS); "
                             "creator_donation_block and the donations trigger unchanged, no pinned function"),
    "0041": no_payment_delta("PAN evidence: kyc_* tables, triggers and functions (not TRIGGER_RELS); the donation "
                             "rule unchanged, no pinned function"),
    "0042": no_payment_delta("automatic activation: onboarding_* tables and functions, the streamers step-up guard "
                             "(not TRIGGER_RELS); kyc_change_creator_status and the donation rule unchanged, no "
                             "pinned function"),
    "0043": {
        "why": "security audit 2026-11-26: pg_temp last in the search_path of the 11 SECURITY DEFINER payment "
               "functions (ALTER FUNCTION: proconfig only, bodies and pins unchanged)",
        "functions": {}, "pins": {}, "triggers": {},
        "configs": {name: _with_pg_temp(name) for name in DEFINER_FUNCTIONS},
    },
    "0044": no_payment_delta("security audit run-4: a BEFORE INSERT guard on streamers refuses a claimed or "
                             "soundboard-enabled new creator from the API roles (not TRIGGER_RELS), no pinned function"),
    "0045": no_payment_delta("security audit run-4: kyc_change_creator_status, kyc_account_revoke and "
                             "kyc_receive_webhook re-created (KYC witness events; revoked accounts' webhooks "
                             "ignored); creator_donation_block and the donations trigger unchanged, no pinned function"),
    "0046": no_payment_delta("per-mode moderation: streamers columns, and a CHECK constraint (no trigger) on donations "
                             "that refuses a message with an attachment; TRIGGER_PINS, ACLs and pinned functions "
                             "unchanged"),
}
EXPECTED_HEADS = tuple(HEAD_DELTAS)  # the heads verify accepts, in revision order
assert list(EXPECTED_HEADS) == sorted(EXPECTED_HEADS) and EXPECTED_HEADS[0] == EXPECTED_HEAD
assert all(d.get("why") and set(d) == {"functions", "pins", "triggers", "configs", "why"}
           for d in HEAD_DELTAS.values())
assert all(set(d["configs"]) <= set(FN_EXPECT) for d in HEAD_DELTAS.values())  # a config change of a known function
FN_NAMES_ALL = tuple(sorted(set(FN_EXPECT).union(*(d["functions"] for d in HEAD_DELTAS.values()))))


def head_expectations(head: str) -> tuple[dict, dict, dict]:
    """(FN_EXPECT, FUNCTION_PINS, TRIGGER_PINS) of a database head: 0034's, plus every delta up to and including it."""
    fns, pins, triggers = dict(FN_EXPECT), dict(FUNCTION_PINS), dict(TRIGGER_PINS)
    for h, d in HEAD_DELTAS.items():
        fns |= d["functions"]
        pins |= d["pins"]
        triggers |= d["triggers"]
        for name, config in d["configs"].items():  # proconfig-only changes (ALTER FUNCTION ... SET)
            fns[name] = {**fns[name], "proconfig": config}
        if h == head:
            return fns, pins, triggers
    raise KeyError(head)


# ── §6A baseline values (OD-5, owner 2026-11-17): the 0034 values reconciled A = B = C on PG 15.20 / 17.12 (A5a-R2) ──
SCHEMA_OWNER_PIN = "pg_database_owner"  # I1
SCHEMA_ACL_BASELINE = frozenset({("pg_database_owner", "CREATE"), ("pg_database_owner", "USAGE"),  # I2 (no PUBLIC)
                                 *((r, "USAGE") for r in RUNTIME_ROLES)})
TABLE_PRIVILEGES = {15: ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"),
                    17: ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN")}
_TABLE_GRANTS = {  # I5: the non-owner entries per scoped table (0007 defaults narrowed by 0029 / 0031 / 0032 / 0034)
    "donations": {"hyperchat_app": ("SELECT", "INSERT", "UPDATE", "DELETE"),
                  "hyperchat_worker": ("SELECT", "INSERT", "UPDATE", "DELETE"), "hyperchat_readonly": ("SELECT",)},
    **{t: {"hyperchat_app": ("SELECT", "INSERT"), "hyperchat_readonly": ("SELECT",)} for t in APPEND_ONLY_EVIDENCE},
    **{t: {"hyperchat_app": ("SELECT",), "hyperchat_worker": ("SELECT",), "hyperchat_readonly": ("SELECT",)}
       for t in PROTECTED_TABLES},
}
TABLE_ACL_BASELINE = {  # version-keyed: V-1, the owner's MAINTAIN rows exist on PG17 only (10 extra entries)
    major: frozenset({(rel, OWNER, p) for rel in RIGHTS_RELS for p in privs}
                     | {(rel, g, p) for rel in RIGHTS_RELS for g, ps in _TABLE_GRANTS[rel].items() for p in ps})
    for major, privs in TABLE_PRIVILEGES.items()
}
COLUMN_ACL_BASELINE = frozenset()  # I6: no column-level grant targets the scoped tables at 0034
SCHEMA_PRIVILEGES = ("USAGE", "CREATE")
COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
# I9 (V-2) is the existing allowed-edge rule: PG15 rolinherit false; PG16+ inherit_option false, set_option true.
# The comparisons A5b implements, declared row by row (plan §6A; the A1 tests gate on this set). Declaring a row
# active is NOT activation: I10_BASELINE_CONFIRMED stays False and no list text changes until A5c.
I10_ACTIVE_COMPARISONS = frozenset({f"I{i}" for i in range(1, 17)})
# what only the pristine PG15 / PG17 derivation can confirm (Phase 2B); until then I10 fails closed
I10_BASELINE_CONFIRMED = False
I10_UNVERIFIED = (  # compared against migration-derived values that Phase 2B must confirm
    "function ACL baseline (signature-exact, derived on pristine PG15 and PG17 at 0034)",
    "migrator -> owner edge inherit_option and set_option (PG16+)",
    "function identity-argument, return-type and proconfig texts",
    "trigger pins (tgtype, args, deferrable, constraint) on pristine catalogs",
)
# comparisons NOT made at all until Phase 2B pins their values: reported whatever I10_BASELINE_CONFIRMED says, so the
# flag alone can never make I10 clean
SKIPPED_COMPARISONS = (
    "schema public exact owner (only pg_database_owner / hyperchat_owner accepted) and ACL",
    "prosrc md5 of set_updated_at and donations_require_eligible_creator",
    "table and column ACL exact pins (only PUBLIC entries and grant options are judged)",
    "alembic_version ownership",
)
_TG_EVENTS = ((4, "INSERT"), (8, "DELETE"), (16, "UPDATE"), (32, "TRUNCATE"))


def decode_tgtype(t: int) -> tuple:
    timing = "INSTEAD" if t & 64 else ("BEFORE" if t & 2 else "AFTER")
    return timing, "ROW" if t & 1 else "STATEMENT", frozenset(e for bit, e in _TG_EVENTS if t & bit)


def _config(entries, each=iter) -> dict:
    return dict(e.split("=", 1) for e in each(entries or ()))


def i10_problems(cat: dict, version: int, admins=frozenset(), work=None, check=None,
                 head: str = "0034") -> tuple[list, list]:
    """§5 over captured catalog rows (pure: unit-testable without PostgreSQL). Returns the structural problems as
    (entity_kind, entity_id, problem, detail) and the baseline components still unverified. `work` is the §2.7.3
    checkpoint (one unit per catalog row and per membership edge visited); `check` the deadline check around each
    accepted atomic sort (Budget.check; derived from a bound Budget.work when omitted)."""
    out = []
    fn_expect, fn_pins, trigger_pins = head_expectations(head)
    fn_names = tuple(sorted(fn_expect))
    tick = work or (lambda n=1: None)
    chk = check or getattr(getattr(work, "__self__", None), "check", None) or (lambda: None)

    def each(items):
        return _each(items, tick)

    def sort(items):  # the accepted bounded atomic sort: charged first, checked before and after
        n = len(items)
        tick(n * max(1, n.bit_length()))
        chk()
        result = sorted(items)
        chk()
        return result

    def flag(kind, entity, problem, **extra):
        out.append((kind, str(entity), problem, extra))

    roles = {r["name"]: r for r in each(cat["roles"])}
    by_oid = {r["oid"]: r["name"] for r in each(cat["roles"])}
    bootstrap = {n for n, r in each(roles.items()) if r["oid"] == 10}
    approved = {a for a in each(admins) if a in roles and a not in APP_ROLES}  # C36: never an application role
    trusted = bootstrap | approved
    predefined = {n for n, r in each(roles.items()) if n.startswith("pg_") and r["oid"] < 16384}
    supers = {n for n, r in each(roles.items()) if r["super"]}

    # §5.1: application-role attributes first, then other superusers
    for name in APP_ROLES:
        r = roles.get(name)
        if r is None:
            flag("role", name, "role_attribute:missing")
            continue
        for attr, col in ROLE_ATTRS.items():
            if r[attr]:
                flag("role", name, f"role_attribute:{col}")
        if bool(r["login"]) != (name != OWNER):
            flag("role", name, "role_attribute:rolcanlogin")
    if MIGRATOR in roles and roles[MIGRATOR]["inherit"]:
        flag("role", MIGRATOR, "role_attribute:rolinherit")  # roles.sql: NOINHERIT (the PG15 path rule)
    for n in each(sort([x for x in each(supers) if x not in trusted and x not in APP_ROLES])):
        flag("role", n, "unexpected_superuser")

    # §5.2: default-deny membership edges
    edges = []
    for m in each(cat["members"]):
        member, role = by_oid.get(m["member"]), by_oid.get(m["role"])
        if member is None or role is None:
            flag("role", f"{m['member']}->{m['role']}", "role_edge:unknown_oid")
            continue
        edges.append((member, role, m))
        if (member, role) == (MIGRATOR, OWNER):
            if m["admin_option"]:
                flag("role", MIGRATOR, "admin_option")
            if version >= 160000 and (m["set_option"] is not True or m["inherit_option"] is not False):
                flag("role", MIGRATOR, f"role_edge:{member}->{role}")
            continue
        if (member in predefined and role in predefined) or member in trusted:
            continue
        flag("role", member, f"role_edge:{member}->{role}")

    out_edges = defaultdict(list)  # member -> its edges, built once (same edges, same order as before)
    for member, role, m in each(edges):
        out_edges[member].append((role, m))

    def reach(start, how):
        seen, todo = set(), [start]
        while todo:
            node = todo.pop()
            for role, m in each(out_edges[node]):  # a checkpoint per edge actually examined
                if role in seen:
                    continue
                if how == "set":
                    ok = m["set_option"] if version >= 160000 else True
                else:  # PG16+: the edge's inherit_option; PG15: the member's rolinherit
                    ok = m["inherit_option"] if version >= 160000 else roles[node]["inherit"]
                if ok:
                    seen.add(role)
                    todo.append(role)
        return seen

    for pr in each(sort([n for n in each(roles) if n not in trusted and n not in predefined])):
        got = reach(pr, "set")
        for x in each(reach(pr, "inherit")):  # admin_path: any path counts (SET alone suffices)
            got.add(x)
        for a in each(sort([x for x in each(got) if x in trusted])):  # built and emitted under checkpoints
            flag("role", pr, f"admin_path:{pr}->{a}")
        if pr not in (OWNER, MIGRATOR) and (OWNER in got or MIGRATOR in got):
            flag("role", pr, "owner_path")
        for r in each(sort([x for x in each(got) if x in predefined])):
            flag("role", pr, f"predefined_role_path:{r}")
    if cat["database_owner"] != OWNER:
        flag("database", "current", "database_owner", owner=cat["database_owner"])

    # §5.3 ownership
    active = I10_ACTIVE_COMPARISONS  # §6A rows whose comparison A5b implemented (never emitted while inactive)
    rels = {r["rel"]: r for r in each(cat["relations"])}
    for rel in (*OWNED_RELATIONS, *(("alembic_version",) if "I7" in active else ())):
        r = rels.get(rel)
        if r is None or r["owner"] != OWNER:
            flag("relation", rel, f"ownership:{rel}", owner=r and r["owner"])
    if "I1" in active:
        if cat["schema_owner"] != SCHEMA_OWNER_PIN:  # the exact pinned owner replaces the candidate set
            flag("schema", "public", "ownership:schema_public", owner=cat["schema_owner"])
    elif cat["schema_owner"] not in SCHEMA_OWNER_CANDIDATES:
        flag("schema", "public", "ownership:schema_public", owner=cat["schema_owner"])

    # §5.4 (a) effective rights; owners, trusted, predefined and (already flagged) superusers are exempt
    class _Skip:  # trusted | predefined | supers | {OWNER}, tested without building the union
        def __contains__(self, n):
            return n in trusted or n in predefined or n in supers or n == OWNER
    skip = _Skip()
    shown = lambda n: "PUBLIC" if n == "public" else n  # noqa: E731
    for row in each(cat["table_rights"]):
        role, rel, priv = row["role"], row["rel"], row["priv"]
        if role in skip:
            continue
        if rel == "donations":
            if not (role in DONATION_WRITERS and priv in ("INSERT", "UPDATE", "DELETE")):
                flag("table", rel, f"donations_write:{priv}:{shown(role)}")
        elif not (rel in APPEND_ONLY_EVIDENCE and (role, priv) == ("hyperchat_app", "INSERT")):
            flag("table", rel, f"table_{priv.lower()}_granted:{shown(role)}")
    for row in each(cat["column_rights"]):
        role, rel, col, priv = row["role"], row["rel"], row["col"], row["priv"]
        if role in skip:
            continue
        if rel == "donations":
            if not (role in DONATION_WRITERS and priv in ("INSERT", "UPDATE")):  # writer columns: owner item 6
                flag("table", rel, f"donations_column_write:{priv}:{col}:{shown(role)}")
        elif not (rel in APPEND_ONLY_EVIDENCE and (role, priv) == ("hyperchat_app", "INSERT")):
            flag("table", rel, f"column_{priv.lower()}_granted:{col}:{shown(role)}")

    fns = defaultdict(list)
    for f in each(cat["functions"]):
        fns[f["name"]].append(f)

    def sig(name):
        return f"{name}({fns[name][0]['args'] if fns.get(name) else fn_expect[name]['args']})"

    holders = defaultdict(set)
    for row in each(cat["function_rights"]):
        role, name = row["role"], row["name"]
        holders[name].add(shown(role))
        base = FN_ACL_BASELINE.get(name, frozenset())
        if role in skip or "PUBLIC" in base or shown(role) in base:
            continue
        flag("function", name, f"function_acl:{sig(name)}:{shown(role)}:EXECUTE")
    for name, base in sorted(FN_ACL_BASELINE.items()):
        for grantee in sorted(base - holders[name]):
            flag("function", name, f"function_acl_missing:{sig(name)}:{grantee}")
    for row in each(cat["schema_create"]):
        if row["role"] not in skip:
            flag("schema", "public", f"schema_create:{shown(row['role'])}")

    # §5.4 (b) explicit ACL entries (is_grantable always selected)
    entries = defaultdict(set)
    for row in each(cat["function_acl"]):
        owner = fns[row["name"]][0]["owner"] if fns.get(row["name"]) else OWNER
        if row["grantee"] != owner:
            entries[row["name"]].add((row["grantee"], row["privilege"], bool(row["grantable"])))
    for name in fn_names:
        base = {(g, "EXECUTE") for g in FN_ACL_BASELINE.get(name, ())}
        for grantee, priv, grantable in each(sort(entries.get(name, set()))):
            if grantable:
                flag("function", name, f"grant_option:{sig(name)}:{grantee}")
            if (grantee, priv) in base:
                continue
            if grantee == "PUBLIC":
                flag("function", name, f"public_function_execute:{sig(name)}")
                flag("function", name, "public_execute")  # kept from the reviewed code (§5.7)
            else:
                flag("function", name, f"function_acl:{sig(name)}:{grantee}:{priv}")
        present = {(g, p) for g, p, _ in each(entries.get(name, ()))}
        for grantee, priv in sorted(x for x in base if x not in present):  # base: the fixed baseline (<= 2)
            flag("function", name, f"function_acl_missing:{sig(name)}:{grantee}")
    for row in each(cat["schema_acl"]):
        if row["grantee"] == "PUBLIC" and row["privilege"] == "CREATE":
            flag("schema", "public", "public_schema_create")
        if row["grantable"] and row["grantee"] != cat["schema_owner"]:
            flag("schema", "public", f"grant_option:schema_public:{row['grantee']}")
    for row in each(cat["table_acl"]):
        owner = (rels.get(row["rel"]) or {}).get("owner", OWNER)
        if row["grantee"] == "PUBLIC":
            flag("table", row["rel"], f"public_table_{row['privilege'].lower()}")
        if row["grantable"] and row["grantee"] != owner:
            flag("table", row["rel"], f"grant_option:{row['rel']}:{row['grantee']}")
    for row in each(cat["column_acl"]):
        if row["grantee"] == "PUBLIC":
            flag("table", row["rel"], f"public_column_{row['privilege'].lower()}:{row['col']}")
        if row["grantable"]:
            flag("table", row["rel"], f"grant_option:{row['rel']}.{row['col']}:{row['grantee']}")

    # §5.7 OD-11 addendum: explicit ACL entries against the OD-5 baseline (I2 schema, I5 tables, I6 columns).
    # PUBLIC entries and grant options keep the codes above; an altered entry is the missing + unexpected pair.
    extra_unverified = []
    major = version // 10000

    def acl_entries(rows, obj_of, names, label):
        """(object -> {(grantee, privilege)}) from captured rows; unreadable rows refuse the report (acl_unreadable)."""
        seen, out_ = set(), defaultdict(set)
        for row in each(rows):
            g, p, gr = row.get("grantee"), row.get("privilege"), row.get("grantable")
            obj = obj_of(row)
            if not isinstance(g, str) or not g or p not in names or not isinstance(gr, bool) or (obj, g, p) in seen:
                raise Refused(f"acl_unreadable: {label(row)}")
            seen.add((obj, g, p))
            if g != "PUBLIC":
                out_[obj].add((g, p))
        return out_

    def compare(kind, entity, family, key, observed, expected):
        """key: the object text inside the code (schema_public, <rel>, <rel>.<col>); entity: the finding's entity."""
        for g, p in each(sort(observed - expected)):
            flag(kind, entity, f"{family}:{key}:{g}:{p}")
        for g, p in each(sort(expected - observed)):
            flag(kind, entity, f"{family}_missing:{key}:{g}:{p}")

    if "I2" in active:
        got = acl_entries(cat["schema_acl"], lambda r: "schema_public", SCHEMA_PRIVILEGES, lambda r: "schema public")
        compare("schema", "public", "schema_acl", "schema_public", got.get("schema_public", set()),
                set(SCHEMA_ACL_BASELINE))
    if "I5" in active:
        if major not in TABLE_ACL_BASELINE:  # fail closed on an unpinned major: no value is guessed
            extra_unverified.append(f"table ACL baseline for server_version_num {version} (unpinned major)")
        else:
            got = acl_entries(cat["table_acl"], lambda r: r.get("rel"), TABLE_PRIVILEGES[major],
                              lambda r: f"table {r.get('rel')}")
            base = {}  # Part B correction 1: a plain dict, so an absent relation is a MISSING baseline, never {}
            for rel, g, p in each(TABLE_ACL_BASELINE[major]):
                base.setdefault(rel, set()).add((g, p))
            for rel in RIGHTS_RELS:
                if rel not in base:
                    # The tuple representation cannot state an intentionally empty expected ACL, and no scoped table
                    # has one (the owner always holds its default privileges): no tuples == missing configuration.
                    # Fail closed: nothing is compared for this relation (not even an empty catalog), and I10 stays
                    # unverified for it.
                    extra_unverified.append(f"table ACL baseline missing for {rel}")
                    continue
                compare("table", rel, "table_acl", rel, got.get(rel, set()), base[rel])
    if "I6" in active:
        got = acl_entries(cat["column_acl"], lambda r: (r.get("rel"), r.get("col")), COLUMN_PRIVILEGES,
                          lambda r: f"column {r.get('rel')}.{r.get('col')}")
        base = defaultdict(set)
        for rel, col, g, p in each(COLUMN_ACL_BASELINE):
            base[(rel, col)].add((g, p))
        for rel, col in each(sort(set(got) | set(base))):
            if rel in RIGHTS_RELS:
                compare("table", rel, "column_acl", f"{rel}.{col}", got.get((rel, col), set()), base[(rel, col)])

    # §5.5 triggers: exact binding; any other trigger on these relations is unexpected
    live = {(t["rel"], t["name"]): t for t in each(cat["triggers"])}
    for key, (fn, timing, level, events) in sorted(trigger_pins.items()):
        t, entity = live.get(key), ".".join(key)
        if t is None:
            flag("trigger", entity, "trigger_binding:missing")
            continue
        checks = {
            "function": (t["fn_schema"], t["fn_name"], t["fn_args"]) == ("public", fn, ""),
            "type": decode_tgtype(t["tgtype"]) == (timing, level, events),
            "enabled": t["enabled"] == "O",
            "when": bool(t["no_qual"]),
            "args": t["tgnargs"] == 0 and not t["tgargs"],
            "constraint": not (t["deferrable"] or t["initdeferred"] or t["is_constraint"]),
            "columns": not t["update_cols"],
        }
        for field_, good in checks.items():
            if not good:
                flag("trigger", entity, f"trigger_binding:{field_}")
    for key in each(sort([k for k in each(live) if k not in trigger_pins])):
        flag("trigger", ".".join(key), "unexpected_trigger")

    # §5.5 functions: exactly one row per name, every column pinned
    for name in fn_names:
        exp, rows = fn_expect[name], fns.get(name, [])
        if not rows:
            flag("function", name, "missing")
            continue
        if len(rows) > 1:
            flag("function", name, "overloaded", count=len(rows))
            continue
        f = rows[0]
        pins = {"args": exp["args"], "returns": exp["returns"], "lanname": exp["lanname"], "prokind": "f",
                "provolatile": exp["provolatile"], "proparallel": "u", "proisstrict": False, "proleakproof": False,
                "schema": "public"}
        for col, want in pins.items():
            if f[col] != want:
                flag("function", name, f"function_attr:{col}")
        if bool(f["prosecdef"]) != (name in DEFINER_FUNCTIONS):
            flag("function", name, "security")
        if f["owner"] != OWNER:
            flag("function", name, f"ownership:{name}")
        src = fn_pins.get(name, UNVERIFIED)
        if name in PIN_ROWS and PIN_ROWS[name] not in active:
            src = UNVERIFIED  # I3 / I4: pinned by OD-5, compared only once their row is active
        if src is not UNVERIFIED and f["src"] != src:
            flag("function", name, "body_drift")
        if not f["no_bin"]:
            flag("function", name, "function_attr:probin")
        got, want = _config(f["proconfig"], each), _config(exp["proconfig"], each)
        if got.get("search_path") != want.get("search_path"):
            flag("function", name, "search_path")
        if "lock_timeout" in want and got.get("lock_timeout") != want["lock_timeout"]:
            flag("function", name, "lock_timeout")
        rest = lambda c: {k: x for k, x in each(c.items()) if k not in ("search_path", "lock_timeout")}  # noqa: E731
        if rest(got) != rest(want) or ("lock_timeout" in got and "lock_timeout" not in want):
            flag("function", name, "function_attr:proconfig")
    unique = {(k, e, pr, canonical(x)): (k, e, pr, x) for k, e, pr, x in each(out)}  # (a) and (b) may name one problem
    return [x for x in each(unique.values())], [*([] if I10_BASELINE_CONFIRMED else I10_UNVERIFIED),
                                                 *SKIPPED_COMPARISONS, *extra_unverified]


# ── the verifier ──────────────────────────────────────────────────────────────────────────────────────────────────

class Verifier:
    def __init__(self, cur, first: dict, scope_kind: str, scope_id: str | None, *, budget=None,
                 admins=frozenset(), admin_list_sha256: str | None = None):
        self.cur, self.first = cur, first
        self.budget = budget or _NoBudget()
        self.admins, self.admin_list_sha256 = frozenset(admins), admin_list_sha256
        self.scope_kind, self.scope_id = scope_kind, scope_id
        self.findings: list[Finding] = []
        self.c_v: datetime = first["c_v"]
        self.all_events: list[dict] = []          # revision 7: every evidence event (load())
        self.events_by_ref: dict = {}
        self.chain_valid = True                   # set by state_event_chain() (I18) before classify()

    def add(self, invariant, cls, kind, entity, /, **detail):  # positional-only: detail may carry "kind" (I16)
        self.findings.append(Finding(invariant, cls, kind, str(entity), detail))

    # ── load ──
    def load(self):
        q = lambda sql, p=None: _select(self.cur, sql, p)  # noqa: E731
        major = self.first["server_version_num"] // 10000
        if major not in TABLE_PRIVILEGES:  # U-5 (A5b): the effective-rights question and the I10 baselines are pinned
            raise Refused(f"unsupported PostgreSQL major {major}: I10 is pinned for 15 and 17 only")  # for 15 / 17 only
        # §5.6, before any financial read: any row security or policy refuses the whole report
        self.relations = q(CAT_RELATIONS, {"rels": list(READ_RELATIONS)})
        for r in sorted(self.relations, key=lambda r: r["rel"]):
            if r["rls"] or r["force_rls"] or r["policy"]:
                raise Refused(f"row_security: {r['rel']}")
        tick, each = self.budget.work, self.budget.each  # every pass over retained rows is checkpointed
        self.donations = {r["id"]: r for r in each(q(DONATIONS))}
        self.streamers = {r["id"] for r in each(q(STREAMERS))}
        self.totals = {(r["streamer_id"], r["donator_name_lower"]): r["total_amount"] for r in each(q(TOTALS))}
        self.history = q(HISTORY)
        self.reversals = {r["donation_id"]: r for r in each(q(REVERSALS))}
        self.current = defaultdict(list)
        for r in q(CURRENT):
            tick()
            self.current[r["donation_id"]].append(r)
        self.evidence = defaultdict(list)
        for r in q(EVIDENCE):
            tick()
            self.evidence[r["donation_id"]].append(r)
        self.evals = {r["donation_id"]: r for r in each(q(EVALUATIONS))}
        self.all_events = q(ALL_EVENTS)
        self.events_by_ref = defaultdict(list)
        for e in self.all_events:
            tick()
            self.events_by_ref[e["provider_ref"]].append(e)
        states = q(STATE)
        if len(states) != 1:
            raise Refused("the accounting state row is missing or duplicated")
        self.state, self.mode = states[0]["state"], states[0]["dispute_mode"]
        self.events = q(EVENTS)
        self.runs = {r["id"]: r for r in each(q(RUNS))}
        self.by_donation = defaultdict(list)
        for h in self.history:
            tick()
            self.by_donation[h["donation_id"]].append(h)
        relevant = self.budget.sort(self.budget.collect(self.evals, self.current, self.reversals, self.evidence))
        relevant = [d for d in each(relevant) if d in self.donations]
        self.fp = {m: {r["id"]: r["fp"] for r in each(q(FP_NOW, {"mode": m, "ids": relevant}))}
                   for m in ("hold", "apply")}
        names, rels = {"names": list(FN_NAMES_ALL)}, {"rels": list(RIGHTS_RELS)}
        members = CAT_MEMBERS_16 if self.first["server_version_num"] >= 160000 else CAT_MEMBERS_15
        table_rights = CAT_TABLE_RIGHTS_17 if self.first["server_version_num"] // 10000 >= 17 else CAT_TABLE_RIGHTS
        self.catalog = {
            "roles": q(CAT_ROLES), "members": q(members),
            "database_owner": (q(CAT_DATABASE) or [{"owner": None}])[0]["owner"],
            "relations": self.relations, "schema_owner": (q(CAT_SCHEMA) or [{"owner": None}])[0]["owner"],
            "table_rights": q(table_rights, rels), "column_rights": q(CAT_COLUMN_RIGHTS, rels),
            "function_rights": q(CAT_FUNCTION_RIGHTS, names), "schema_create": q(CAT_SCHEMA_CREATE),
            "function_acl": q(CAT_FUNCTION_ACL, names), "schema_acl": q(CAT_SCHEMA_ACL),
            "table_acl": q(CAT_TABLE_ACL, rels), "column_acl": q(CAT_COLUMN_ACL, rels),
            "triggers": q(CAT_TRIGGERS, {"rels": list(TRIGGER_RELS)}), "functions": q(CAT_FUNCTIONS, names),
        }

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
        return ["-infinity", *(r["recorded_at"] for r in self.budget.each(_select(self.cur, EVIDENCE_CUTS,
                                                                                    {"d": did}))), "infinity"]

    def ref_events(self, did: str) -> dict:
        """Every append-only event of the donation's refs, by ref."""
        out = defaultdict(list)
        for e in self.budget.each(_select(self.cur, REF_EVENTS, {"d": did})):
            out[e["provider_ref"]].append(e)
        return out

    def raw_events(self, did: str, ref: str) -> list[dict]:
        """Revision 7 §2.1: every recorded event of one stored ref, any donation, attribution or status."""
        return self.events_by_ref.get(ref, [])

    def conceivable_fps(self, did: str, modes) -> tuple[str, object]:
        """Decision 5 / A1: the fingerprints of every evidence state the donation could have had under some commit
        order, under each mode: ("ok", fps). Revision 7 keeps the cause of an incomplete enumeration:
        ("over_bound", bound detail) or ("replica_failed", None) when 0034's fingerprint is not reproduced."""
        by_ref = self.ref_events(did)
        for ref, evs in self.budget.each(self.budget.sort(by_ref.items())):
            if len(evs) > MAX_REF_EVENTS:
                return "over_bound", {"bound": "ref_events", "ref": ref, "events": len(evs)}
        # the combination product is built incrementally: each ref is expanded only while the product can still
        # be in bound (its own expansion stops once its states alone prove it is not); exact up to the bound
        per_ref, combinations = [], len(modes)
        for evs in self.budget.each(by_ref.values()):
            states = ref_states(evs, did, self.budget.each, limit=ENUMERATION_BOUND // combinations)
            per_ref.append(states)
            combinations *= len(states)
            if combinations > ENUMERATION_BOUND:  # inclusive: exactly 4 096 stays in bound
                return "over_bound", {"bound": "combinations", "combinations": combinations}
        sets = [{"mode": m, "rows": [r for r in combo if r is not None]}
                for m in modes for combo in self.budget.each(itertools.product(*per_ref))]
        sets.append({"mode": self.mode, "rows": [{k: r[k] for k in (*ROW_FIELDS, "conflicting")}
                                                 for r in self.budget.each(self.current.get(did, []))]})
        self.budget.check()
        payload = json.dumps(sets, default=str)  # accepted atomic: <= ENUMERATION_BOUND + 1 sets, checked around it
        self.budget.check()
        fps = [r["fp"] for r in self.budget.each(_select(self.cur, FP_SETS, {"d": did, "sets": payload}))]
        if fps[-1] != self.fp[self.mode].get(did):
            return "replica_failed", None
        return "ok", self.budget.collect(itertools.islice(fps, len(fps) - 1))

    def near_cuts(self, did: str, t) -> list:
        """The evidence states within the D-15 margin of a row stamped t."""
        cuts = {e["recorded_at"] for e in self.budget.each(self.evidence.get(did, ()))
                if t - MARGIN < e["recorded_at"] <= t + MARGIN}
        cuts.add(t - MARGIN)
        return self.budget.sort(cuts)

    # CR-2 / Decisions 2-3: 0034 stamps are transaction starts and commit times are unbounded, so no historical mode
    # is chosen or excluded by timestamp proximity; a mode is excluded only by a rule the core enforces. The guard
    # changes the mode only while off / frozen, so an active period's mode is its activation event's to_mode.
    def initial_mode(self) -> str:
        return self.events[0]["from_mode"] if self.events else self.mode

    def period_modes(self) -> list[tuple]:
        """(start, end, mode) of every active period, the mode fixed by its activation event."""
        modes = {e["id"]: e["to_mode"] for e in self.budget.each(self.events)}
        return [(a, end, modes[aid]) for a, end, aid in self.budget.each(self.periods())]

    def every_mode(self) -> set:
        """Every mode the state ever had: the possibilities for a keep (allowed active or frozen)."""
        modes = {e["to_mode"] for e in self.budget.each(self.events)}
        modes.add(self.initial_mode())
        return modes

    def row_modes(self, names_dispute: bool) -> set:
        """The modes an active-only clear row could have been written under: every active period's mode (on an
        invalid I18 chain, the universe {hold, apply}: revision 7 §2.1 step 4), minus hold when its refs name a
        dispute (the core refuses a clear result in hold while a lost dispute is current, and the refs are exactly
        the counted refunds and lost disputes, so a named dispute was lost and current)."""
        if self.chain_valid:
            modes = {m for _, _, m in self.budget.each(self.period_modes())} or {self.initial_mode()}
        else:
            modes = {"hold", "apply"}
        if names_dispute:
            modes.discard("hold")
        return modes

    # ── I18: the accounting state event chain (revision 7 §2.5) ──
    def state_event_chain(self):
        """Replay the transitions in id order (commit order). Any defect is structural and suspends mode-dependent
        I12-H conclusions (chain_valid = False)."""
        problems = []
        prev = ("off", "hold")  # the 0034 migration's state row
        for i, e in enumerate(self.events):
            self.budget.work()  # §2.7.3 checkpoint
            f, t = (e["from_state"], e["from_mode"]), (e["to_state"], e["to_mode"])
            if not (f[0] in STATES and t[0] in STATES and f[1] in MODES and t[1] in MODES):
                rule = "vocabulary"
            elif f != prev:
                rule = "initial" if i == 0 else "link"
            elif f == t:
                rule = "noop"
            elif f[0] != t[0] and f[1] != t[1]:
                rule = "simultaneous"
            elif f[0] != t[0] and (f[0], t[0]) not in LEGAL_STATE_PAIRS:
                rule = "transition"
            elif f[1] != t[1] and f[0] not in ("off", "frozen"):
                rule = "mode_change"
            else:
                rule = None
            if rule:
                problems.append((rule, e["id"]))
            prev = t
        if (prev != (self.state, self.mode)):
            problems.append(("final" if self.events else "initial", None))
        for rule, eid in self.budget.each(problems):
            self.add("I18", "structural", "accounting_state", "state", problem="state_event_chain:" + rule, event=eid)
        self.chain_valid = not problems

    # ── I16 / I17: evidence attribution and provider refund snapshots (revision 7 §2.3-§2.4) ──
    def owners(self) -> dict:
        """provider_payment_id -> the V2 donation that completed it (unique: donations_provider_payment_key)."""
        return {d["provider_payment_id"]: d for d in self.budget.each(self.donations.values())
                if d["v2"] and d["payment_status"] == "success" and d["provider_payment_id"]}

    def evidence_attribution(self):
        owners = self.owners()
        refs = self.budget.sort(self.events_by_ref.items())
        each = self.budget.each
        for ref, evs in each(refs):
            w = max(each(evs), key=_rank_key)  # §2.7.3: checkpointed inside one hot ref's scan
            a = owners.get(w["provider_payment_id"])
            base = {"ref": ref, "payment": w["provider_payment_id"], "account": w["account"],
                    "source_ids": [w["id"]], "kind": w["kind"], "status": w["status"],
                    "amount_paise": w["amount_paise"], "deducted_paise": w["amount_deducted_paise"],
                    "currency": w["currency"]}
            if a is not None and self.in_scope_donation(a["id"]):
                if w["account"] != a["payment_account"]:
                    self.add("I16", "evidence_attribution", "donation", a["id"], reason="account_mismatch", **base)
                elif w["donation_id"] is None:
                    if (w["kind"], w["status"]) in COUNTED:
                        self.add("I16", "evidence_attribution", "donation", a["id"], reason="unattributed", **base)
                elif w["donation_id"] != a["id"]:
                    self.add("I16", "evidence_attribution", "donation", a["id"], reason="wrong_donation",
                             stored_donation=w["donation_id"], **base)
            if len({(v["account"], v["provider_payment_id"], v["kind"]) for v in each(evs)}) > 1:
                named = self.budget.sort({owners[v["provider_payment_id"]]["id"] for v in each(evs)
                                          if v["provider_payment_id"] in owners
                                          and owners[v["provider_payment_id"]]["payment_account"] == v["account"]})
                for did in each(named):
                    if not self.in_scope_donation(did):
                        continue
                    o = self.donations[did]
                    self.add("I16", "evidence_attribution", "donation", did, reason="ref_identity_conflict", ref=ref,
                             payment=o["provider_payment_id"], account=o["payment_account"],
                             source_ids=self.budget.sort([v["id"] for v in each(evs)]), versions=len(evs))

    def refund_snapshots(self):
        snaps, refunds, every = defaultdict(list), defaultdict(list), defaultdict(list)
        each = self.budget.each
        for e in each(self.all_events):
            every[(e["account"], e["provider_payment_id"])].append(e)
            if e["snapshot"] is not None:
                snaps[(e["account"], e["provider_payment_id"])].append(e)
        for ref, evs in each(self.events_by_ref.items()):
            w = max(each(evs), key=_rank_key)
            if w["kind"] == "refund" and w["status"] in ("processed", "pending"):
                refunds[(w["account"], w["provider_payment_id"])].append(w)
        owners = self.budget.sort(self.owners().items())
        for payment, a in each(owners):
            if self.in_scope_donation(a["id"]):
                key = (a["payment_account"], payment)
                self.snapshot_one(a, snaps.get(key, []),
                                  self.budget.sort(refunds.get(key, []), key=lambda w: w["provider_ref"]),
                                  every.get(key, []))

    def snapshot_one(self, a, observations, refund_rows, events=()):
        """§2.4 for one V2 donation: the candidate set, the whole-chronology regression, refund-side exclusions and
        the S / P / Q comparison. Uncertainty is attention and a write blocker, never clean."""
        did, cur = a["id"], a["currency"]
        each = self.budget.each  # §2.7.3: one unit per item of every scan below, as it runs
        add = lambda reason, **x: self.add("I17", "refund_snapshot_mismatch", "donation", did,  # noqa: E731
                                           reason=reason, **x)
        if cur not in PRODUCER_CURRENCIES:  # §2.4.1: I17 compares nothing for A
            add("unsupported_currency", subject="donation", donation_currency=cur)
            return

        def unit(c):
            return ("currency_unknown" if c is None else "unsupported_currency" if c not in PRODUCER_CURRENCIES
                    else "currency_mismatch" if c != cur else "comparable")
        processed = pending = 0
        excluded = []
        for w in each(refund_rows):
            why = "amount_unknown" if w["amount_paise"] is None else unit(w["currency"])
            if why != "comparable":
                excluded.append((w, why))
            elif w["status"] == "processed":
                processed += w["amount_paise"]
            else:
                pending += w["amount_paise"]
        # the whole comparable, dated chronology (revision 6 §2.4.4): any strictly-ordered decrease
        groups = defaultdict(list)
        for o in each(observations):
            if o["provider_event_at"] is not None and unit(o["currency"]) == "comparable":
                groups[o["provider_event_at"]].append(o)
        top, first, count = None, None, 0
        for t in each(self.budget.sort(groups)):
            low = min(each(groups[t]), key=lambda o: (o["snapshot"], o["id"]))
            if top is not None and top["snapshot"] > low["snapshot"]:
                count += 1
                first = first or (top, low)
            high = max(each(groups[t]), key=lambda o: (o["snapshot"], o["id"]))
            top = high if top is None or high["snapshot"] > top["snapshot"] else top
        if first:
            e, f = first
            add("snapshot_regression", subject="snapshot", source_ids=[e["id"], f["id"]], earlier_id=e["id"],
                earlier_at=e["provider_event_at"], earlier_paise=e["snapshot"], later_id=f["id"],
                later_at=f["provider_event_at"], later_paise=f["snapshot"], regressions=count)
        # the candidate set: the top provider time plus every undated observation (revision 5 §2.4.3)
        t_max = None  # the latest provider time, found incrementally (no eager max over the observations)
        for o in each(observations):
            if o["provider_event_at"] is not None and (t_max is None or o["provider_event_at"] > t_max):
                t_max = o["provider_event_at"]
        cands = [o for o in each(observations) if o["provider_event_at"] is None or o["provider_event_at"] == t_max]
        # informational only (never read by the predicate or the gates)
        diag = {"snapshot_at": t_max} if t_max is not None else {}
        superseded = self.budget.sort([o["id"] for o in each(observations) if t_max is not None
                                       and o["provider_event_at"] is not None and o["provider_event_at"] < t_max
                                       and unit(o["currency"]) != "comparable"])
        if superseded:
            diag["superseded_incomparable"] = superseded
        if t_max is not None:
            diag["newer_unsnapshotted_events"] = sum(1 for e in each(events) if e["snapshot"] is None
                                                     and e["provider_event_at"] is not None
                                                     and e["provider_event_at"] > t_max)
        s_value = None
        if cands:
            ids = self.budget.sort([o["id"] for o in each(cands)])
            if any(unit(o["currency"]) != "comparable" for o in each(cands)):
                add("snapshot_unit_conflict", subject="snapshot", source_ids=ids,
                    members=[{"id": o["id"], "unit_status": unit(o["currency"]), "currency": o["currency"],
                              "value": o["snapshot"]} for o in each(self.budget.sort(cands, key=lambda o: o["id"]))],
                    **diag)
            elif len({o["snapshot"] for o in each(cands)}) > 1:
                add("snapshot_conflict", subject="snapshot", source_ids=ids, **diag)
            else:
                s_value = cands[0]["snapshot"]
        if s_value is not None and s_value != processed:
            reason = ("pending_refunds" if s_value <= processed + pending and s_value > processed
                      else "missing_refund_evidence" if s_value > processed else "snapshot_behind")
            if reason == "snapshot_behind":  # §2.4.5: both times reported
                latest = None  # incrementally, as above
                for w in each(refund_rows):
                    if w["status"] == "processed" and w["amount_paise"] is not None \
                            and unit(w["currency"]) == "comparable" and w["provider_event_at"] is not None \
                            and (latest is None or w["provider_event_at"] > latest):
                        latest = w["provider_event_at"]
                diag["diag_latest_processed_refund_at"] = latest
            add(reason, subject="snapshot", source_ids=self.budget.sort([o["id"] for o in each(cands)]),
                snapshot_paise=s_value,
                processed_paise=processed, pending_paise=pending, **({"conditional": True} if excluded else {}),
                **diag)
        for w, why in each(excluded):
            extra = {"refund_currency": w["currency"]} if why in ("currency_mismatch", "unsupported_currency") else {}
            if why == "currency_mismatch":
                extra["donation_currency"] = cur
            if s_value is not None and s_value == processed:
                extra["conditional_match"] = True
            add(why, subject="refund", ref=w["provider_ref"], source_ids=[w["id"]], **extra)

    # ── I10: triggers, function security and pins, ACLs, grants ──
    def i10(self):
        problems, unverified = i10_problems(self.catalog, self.first["server_version_num"], self.admins,
                                            work=self.budget.work, check=self.budget.check,
                                            head=self.first.get("head", EXPECTED_HEAD))
        for kind, entity, problem, extra in self.budget.each(problems):  # the emission is checkpointed too
            self.add("I10", "structural", kind, entity, problem=problem, **extra)
        if unverified:  # fail closed: an unconfirmed baseline is never clean (Phase 2B pins it)
            self.add("I10", "structural", "baseline", "i10", problem="baseline_unverified", components=unverified,
                     schema_owner=self.catalog["schema_owner"])

    # ── I1, I2, I5, I7, I8, I11, I15: per donation ──
    def per_donation(self):
        for did, d in self.donations.items():
            self.budget.work()  # §2.7.3 checkpoint
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
            has_acct_history = any(h["cause"] in ACCOUNTING_CAUSES
                                   for h in self.budget.each(self.by_donation.get(did, ())))
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
                if not all(x in FLAG_CODES for x in self.budget.each(rev["flags"] or ())):  # set(flags) <= codes
                    self.add("I11", "structural", "donation", did, flags=self.budget.sort(rev["flags"]))
            applied = rev["applied"] if rev else 0
            python = accounting.reversed_inr(d, applied) if d["amount"] and d["amount"] > 0 else Decimal("0.00")
            if Decimal(d["reversed_inr_sql"]) != python:
                self.add("I15", "structural", "donation", did, sql=d["reversed_inr_sql"], python=python)

    # ── I3, I3-O, I4, I9, I14: per donation history; I6, I7: per bucket (Erratum 1) ──
    def chains(self):
        for did, rows in self.by_donation.items():
            self.budget.work()  # §2.7.3 checkpoint
            d = self.donations.get(did)
            if d is None:
                self.orphan(did, rows)
                continue
            if not self.in_scope_donation(did):
                continue
            self.credit_chain("I3", did, rows, d["credited_inr"])
            for h in self.budget.each(rows):
                if (h["streamer_id"], h["donator_name_lower"]) != (d["streamer_id"], d["name_lower"]):
                    self.add("I9", "structural", "history", h["id"], donation=did)
            self.reversal_chain(did, rows)
            seen = defaultdict(int)
            for h in self.budget.each(rows):
                if h["cause"] in ACCOUNTING_CAUSES:
                    seen[h["txid"]] += 1
            for txid, n in self.budget.each(seen.items()):
                if n > 1:
                    self.add("I14", "structural", "donation", did, txid=txid, rows=n)
        for did, d in self.donations.items():
            self.budget.work()  # §2.7.3 checkpoint
            if self.in_scope_donation(did) and d["credited_inr"] and d["credited_inr"] > 0 \
                    and did not in self.by_donation:
                self.add("I3", "structural", "donation", did, problem="credited_without_history")
        self.buckets()

    def credit_chain(self, inv, did, rows, credited) -> bool:
        ok = bool(rows) and rows[0]["credited_before"] == 0
        for prev, cur in self.budget.each(zip(rows, itertools.islice(rows, 1, None))):
            ok = ok and cur["credited_before"] == prev["credited_after"]
        ok = ok and all(h["delta_inr"] == h["credited_after"] - h["credited_before"] for h in self.budget.each(rows))
        if credited is not None:
            ok = ok and rows[-1]["credited_after"] == credited
        if not ok:
            self.add(inv, "structural", "donation", did, problem="credit_chain")
        return ok

    def reversal_chain(self, did, rows):
        rev = self.reversals.get(did)
        prev_rev, prev_hold = 0, None
        for h in self.budget.each(rows):
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
        buckets = {(h["streamer_id"], h["donator_name_lower"]) for h in self.budget.each(rows)}
        sid = rows[0]["streamer_id"]
        if not self.in_scope_bucket(sid) and not (self.scope_kind == "donation" and did == self.scope_id):
            return
        if len(buckets) > 1:
            self.add("I9", "structural", "orphan", did, problem="orphan_moved_bucket", buckets=len(buckets))
            return
        if sid not in self.streamers:
            return  # deleted-creator bucket: excluded and counted (buckets())
        net = sum(h["delta_inr"] for h in self.budget.each(rows))
        chain_ok = self.credit_chain("I3-O", did, rows, None)
        if chain_ok and (net != 0 or rows[-1]["credited_after"] != 0):
            self.add("I3-O", "structural", "orphan", did, net=net, last=rows[-1]["credited_after"])

    def buckets(self):
        writer = defaultdict(list)
        all_rows = defaultdict(list)
        for h in self.history:
            self.budget.work()  # §2.7.3 checkpoint
            key = (h["streamer_id"], h["donator_name_lower"])
            all_rows[key].append(h)
            if h["cause"] == "writer":
                writer[key].append(h)
        self.excluded, self.baselines = [], []
        self.orphan_buckets = self.orphan_rows = 0
        credited = defaultdict(Decimal)
        for d in self.donations.values():
            self.budget.work()  # §2.7.3 checkpoint
            credited[(d["streamer_id"], d["name_lower"])] += d["credited_inr"] or 0
        for key in self.budget.each(self.budget.sort(self.budget.collect(all_rows, self.totals, credited))):
            sid = key[0]
            if not self.in_scope_bucket(sid):
                continue
            bucket = _bucket_key(*key)
            if sid not in self.streamers:
                rows = all_rows.get(key, [])
                each = self.budget.each
                acct = sum(1 for h in each(rows) if h["cause"] in ACCOUNTING_CAUSES)
                net = sum((h["delta_inr"] for h in each(rows)), Decimal(0))
                self.orphan_buckets += 1
                self.orphan_rows += len(rows)
                self.excluded.append({"bucket": bucket, "rows": len(rows), "net_credit": net,
                                      "accounting_rows": acct,
                                      "first": min((h["created_at"] for h in each(rows)), default=None),
                                      "last": max((h["created_at"] for h in each(rows)), default=None)})
                if acct:
                    self.add("Erratum-1", "deleted_creator_with_accounting_history", "bucket", bucket, rows=acct)
                if net != 0:
                    self.add("Erratum-1", "excluded_bucket_net_credit", "bucket", bucket, net_credit=net)
                continue
            w = writer.get(key, [])
            for prev, cur in self.budget.each(zip(w, itertools.islice(w, 1, None))):
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
            self.budget.work()  # §2.7.3 checkpoint
            if h["run_id"] is not None:
                used[h["run_id"]].add(h["donation_id"])
        for ev in self.evals.values():
            self.budget.work()  # §2.7.3 checkpoint
            if ev["ack_run"] is not None:
                used[ev["ack_run"]].add(ev["donation_id"])
        def relevant(rid) -> bool:  # a scoped report judges only the runs its donations used
            return self.scope_kind == "full" or any(self.in_scope_donation(d)
                                                    for d in self.budget.each(used.get(rid, ())))

        self.valid_runs = {}
        for rid, run in self.runs.items():
            self.budget.work()  # §2.7.3 checkpoint
            kind, info = parse_scope(run["scope"], run["created_at"], self.budget.each)
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
        for rid in self.budget.each(self.budget.sort([r for r in self.budget.each(used) if r not in self.runs])):
            if relevant(rid):
                self.add("D-10", "audit_integrity", "run", rid, problem="missing_run")
        for h in self.history:
            self.budget.work()  # §2.7.3 checkpoint
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
            self.budget.work()  # §2.7.3 checkpoint
            info = self.valid_runs.get(ev["ack_run"]) if ev["ack_run"] is not None else None
            if info is None or not self.in_scope_donation(did):
                continue
            # anchored on the keep's own history row: evaluated_at is refreshed by every later 'acknowledged'
            # evaluation (0034 core upsert), so it would expire a valid keep (an unmatched ack is I12's finding)
            keep = next((h for h in self.budget.each(self.by_donation.get(did, ())) if h["run_id"] == ev["ack_run"]
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
            self.budget.work()  # §2.7.3 checkpoint
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
            self.budget.work()  # §2.7.3 checkpoint
            if h["cause"] not in ("evaluation", "hold", "resolved", "activation") and not (
                    h["cause"] == "manual" and h["evidence_refs"] is not None):
                continue
            if not self.in_scope_donation(h["donation_id"], h["streamer_id"]):
                continue
            t = h["created_at"]
            if not any(a - MARGIN <= t < ((end + MARGIN) if end else datetime.max.replace(tzinfo=t.tzinfo))
                       for a, end, _ in self.budget.each(periods)):
                self.add("L-2", "legacy_uncertain", "history", h["id"], donation=h["donation_id"])

    # ── I12 / I12-H / I13 per V2 donation ──
    def frozen_value(self, rows, hold) -> int:
        """F(d): the reversal of the last clear (hold_after NULL) row before the current hold began, or 0."""
        accounting_rows = [h for h in self.budget.each(rows) if h["reversed_after_paise"] is not None]
        i = len(accounting_rows)
        while i > 0 and accounting_rows[i - 1]["hold_after"] == hold:
            self.budget.work()  # §2.7.3: a checkpoint per row stepped back over
            i -= 1
        for j in self.budget.each(range(i - 1, -1, -1)):  # the prefix, newest first, without a slice copy
            h = accounting_rows[j]
            if h["hold_after"] is None:
                return _paise(h["reversed_after_paise"])
        return 0

    def row_shape(self, h) -> list[str]:
        """Revision 7 §2.2: what the 0032 / 0034 writers guarantee for each accounting cause."""
        cause, refs, run_id = h["cause"], h["evidence_refs"], h["run_id"]
        clear, same = h["hold_after"] is None, h["reversed_after_paise"] == h["reversed_before_paise"]
        command = (self.runs.get(run_id) or {}).get("command") if run_id is not None else None
        out = []
        if refs is not None:
            each = self.budget.each
            if any(r is None for r in each(refs)):
                out.append("null_ref")
            elif len(set(each(refs))) != len(refs):
                out.append("duplicate_ref")
            elif not all(isinstance(r, str) and REF_FORMAT.match(r) for r in each(refs)):
                out.append("ref_format")
        if cause in ("evaluation", "resolved"):
            if not clear:
                out.append("hold_after")
            if (h["hold_before"] is None) != (cause == "evaluation"):
                out.append("hold_before")
            if refs is None:
                out.append("missing_evidence_refs")
            if run_id is not None:
                out.append("run")
        elif cause == "hold":
            if clear:
                out.append("hold_after")
            if refs is not None:
                out.append("hold_refs")
            if not same:
                out.append("amount_changed")
            if run_id is not None:
                out.append("run")
        elif cause == "activation":
            if command != "reconcile":
                out.append("run")
            if clear and refs is None:
                out.append("missing_evidence_refs")
            if not clear and (refs is not None or not same):
                out.append("hold_refs" if refs is not None else "amount_changed")
        elif cause == "manual":
            if not clear:
                out.append("hold_after")
            if refs is not None:
                if command != "resolve-apply":
                    out.append("run")
            else:  # a keep: the whole keep-operation contract (0034 l. 543-554)
                if command is None:  # §2.2: a missing run is row_shape:run (and D-10's missing_run)
                    out.append("run")
                elif command == "resolve-apply":
                    out.append("apply_refs")
                elif command != "resolve-keep":
                    out.append("keep_run")
                if h["hold_before"] != accounting.DISPUTE_MODE_HOLD:
                    out.append("keep_hold")
                if not same:
                    out.append("amount_changed")
        elif cause == "revert":
            if command != "revert":
                out.append("run")
            if refs is not None:
                out.append("revert_refs")
            if _paise(h["reversed_after_paise"]) != 0 or not clear:
                out.append("revert_shape")
        return out

    def eligible(self, v, d) -> bool:
        """§2.1 step 3: a version the writer could have counted for this donation and its completing payment."""
        payment = d["provider_payment_id"]
        if v["donation_id"] != d["id"] or not payment or v["provider_payment_id"] != payment:
            return False
        if (v["kind"], v["status"]) == ("refund", "processed"):
            return v["amount_paise"] is not None and v["currency"] == d["currency"]
        if (v["kind"], v["status"]) == ("dispute", "lost"):
            return ((v["amount_deducted_paise"] or 0) > 0 and v["phase"] is not None
                    and v["currency"] == "INR" == d["currency"])
        return False

    def i12h(self, d, rows):
        """Revision 7 §2.1, the precedence ladder: row shape, global absence, historical eligibility, chain validity
        and mode impossibility, then the raw-count bounds, then the S2 enumeration. Steps 1-5 never depend on
        enumeration, so a provable defect survives any bound. Exact means consistency, never proof (D5-2)."""
        donation = self.eval_donation(d)
        chain = "valid" if self.chain_valid else "invalid"
        for h in rows:
            self.budget.work()  # §2.7.3 checkpoint
            if h["cause"] not in ACCOUNTING_CAUSES:
                continue
            rules = self.row_shape(h)
            if rules:  # step 1
                self.add("I12-H", "structural", "history", h["id"], donation=d["id"], problem="row_shape:" + rules[0],
                         rules=rules)
                continue
            if h["hold_after"] is not None or h["evidence_refs"] is None:
                continue  # holds, keeps and reverts: shape-checked, nothing to reconstruct
            refs, stored = self.budget.sort(h["evidence_refs"]), _paise(h["reversed_after_paise"])
            raw = {r: self.raw_events(d["id"], r) for r in self.budget.each(refs)}
            detail = {"donation": d["id"], "stored": stored}
            absent = [r for r in self.budget.each(refs) if not raw[r]]
            if absent:  # step 2
                self.add("I12-H", "structural", "history", h["id"], problem="absent_reference", ref=absent[0],
                         **detail)
                continue
            each = self.budget.each  # §2.7.3: one unit per raw version scanned, as the scan runs
            pools = [[{**{f: v[f] for f in ROW_FIELDS}, "conflicting": False} for v in each(raw[r])
                      if self.eligible(v, d)] for r in each(refs)]
            ineligible = [r for r, p in each(zip(refs, pools)) if not p]
            if ineligible:  # step 3
                self.add("I12-H", "structural", "history", h["id"], problem="ineligible_reference",
                         ref=ineligible[0], versions=len(raw[ineligible[0]]), **detail)
                continue
            names_dispute = any(v["kind"] == "dispute" for r in each(refs) for v in each(raw[r]))
            modes = sorted(self.row_modes(names_dispute))
            detail["modes_checked"] = modes
            if not modes:  # step 4 on a valid chain: no dispute mode could have written it (Decision B: no downgrade)
                proven = h["cause"] in ("activation", "manual") or h["run_id"] is not None \
                    or accounting.DISPUTE_MODE_HOLD in (h["hold_before"], h["hold_after"])
                self.add("I12-H", "structural", "history", h["id"], problem="unsupported_amount", candidates=[],
                         supporting_modes=[], provenance="0034" if proven else "unproven_0034", **detail)
                continue
            over = next(((r, len(raw[r])) for r in each(refs) if len(raw[r]) > MAX_REF_EVENTS), None)
            combinations = math.prod(len(p) for p in each(pools)) * len(modes)
            if over or combinations > ENUMERATION_BOUND:  # step 5: raw counts, inclusive bounds
                bound = {"bound": "ref_events", "ref": over[0], "events": over[1]} if over \
                    else {"bound": "combinations", "combinations": combinations}
                self.add("I12-H", "historical_ambiguous", "history", h["id"], reason="evidence_cut", basis="evidence",
                         enumeration="over_bound", chain=chain, candidates=[], max_impact_paise=None, **bound,
                         **detail)
                continue
            by_mode = {}
            for m in modes:
                by_mode[m] = set()
                for combo in itertools.product(*pools):
                    self.budget.work()  # §2.7.3 CPU checkpoint
                    rows_ = list(combo)
                    e = accounting.evaluate_in_mode(donation, rows_, m)
                    if e.hold is None and accounting.counted_refs(donation, rows_) == refs:
                        by_mode[m].add(e.reversed_paise)
            supporting = [m for m in modes if stored in by_mode[m]]
            candidates = self.budget.sort(self.budget.collect(*by_mode.values()))  # incremental, one checked sort
            detail["supporting_modes"] = supporting
            impact = max((abs(stored - a) for a in self.budget.each(candidates)), default=None)
            amb = {"enumeration": "complete", "chain": chain, "candidates": candidates, "max_impact_paise": impact}
            if not supporting:  # step 6: no eligible version, under any possible mode, gives it
                self.add("I12-H", "structural", "history", h["id"], problem="unsupported_amount",
                         candidates=candidates, **detail)
            elif supporting != modes or (not self.chain_valid and set(supporting) != set(MODES)):
                # another possible mode gives another outcome; on an invalid I18 chain also any exactness that needs
                # a specific mode (revision 7 §2.1 step 4, H6): only mode-independent conclusions stay definitive
                self.add("I12-H", "historical_ambiguous", "history", h["id"], reason="mode_order", basis="mode",
                         **amb, **detail)
            elif len(candidates) > 1:  # another version of a stored ref gives another amount
                self.add("I12-H", "historical_ambiguous", "history", h["id"], reason="evidence_cut",
                         basis="evidence", **amb, **detail)
            elif not self.nominal(donation, d["id"], h["created_at"], refs, stored, modes):
                # the refs determine the amount, but only an order no prefix within +-M shows explains the row
                self.add("I12-H", "historical_ambiguous", "history", h["id"], reason="evidence_order",
                         basis="evidence", **amb, **detail)

    def nominal(self, donation, did, t, refs, stored, modes) -> bool:
        """S2: a recorded_at prefix within +-M of t reproduces (refs, stored) under every possible mode."""
        states = [self.cut_rows(did, c) for c in self.budget.each(self.near_cuts(did, t))]
        return all(any(e.hold is None and e.reversed_paise == stored
                       and accounting.counted_refs(donation, rows_c) == refs
                       for rows_c in self.budget.each(states) for e in [accounting.evaluate_in_mode(donation, rows_c, m)])
                   for m in modes)

    def eval_donation(self, d) -> dict:
        return {"id": d["id"], "provider_payment_id": d["provider_payment_id"], "amount": d["amount"],
                "amount_inr": d["amount_inr"], "currency": d["currency"], "payment_status": d["payment_status"],
                "legacy_source_table": d["legacy_source_table"]}

    def ack_reconstructs(self, did, keep_row, ack_fp) -> tuple[str, dict | None]:
        """A1 (revision 7 §2.4a): never proven on 0034. "forged": no conceivable state under any mode the state ever
        had gives it; "possible": some does; "over_bound" / "replica_failed": the enumeration could not finish."""
        status, out = self.conceivable_fps(did, sorted(self.every_mode()))
        if status != "ok":
            return status, out
        return ("possible" if ack_fp in out else "forged"), None

    def origins(self, did, d, ev, fp_now, rows) -> tuple[datetime | None, str]:
        """§4.2 / §5 / §7: the earliest applicable origin and its kind."""
        each = self.budget.each
        events = self.budget.sort([e["recorded_at"] for e in each(self.evidence.get(did, ()))])
        if ev is None:
            return (events[0], "o_ev") if events else (None, "none")
        m_e, f_e = ev["dispute_mode"], ev["evidence_fp"]
        mode_changed = m_e != self.mode
        evidence_changed = self.fp.get(m_e, {}).get(did) != f_e
        found = []
        if mode_changed:
            # m_e != m_c itself proves a mode change committed after the evaluation; only its time is uncertain when
            # the change's transaction started before the evaluation's (CR-2): never indeterminate for that
            changes = [e["created_at"] for e in each(self.events) if e["from_mode"] != e["to_mode"]]
            later = [t for t in each(changes) if t > ev["evaluated_at"]]
            near = [t for t in each(changes) if t > ev["evaluated_at"] - MARGIN]
            if later:
                found.append((later[0], "o_mode"))
            elif changes:
                found.append((near[0] if near else changes[-1], "o_mode_uncertain"))
        if evidence_changed:
            cands = [t for t in each(events) if t >= ev["evaluated_at"] - MARGIN]
            if cands:
                found.append((cands[0], "o_ev"))
            else:
                caps = [h["created_at"] for h in each(rows) if h["cause"] == "writer" and h["credited_before"] == 0]
                later = [t for t in each(caps) if t > ev["evaluated_at"]]
                near = [t for t in each(caps) if t > ev["evaluated_at"] - MARGIN]  # a capture stamped just before
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
        each = self.budget.each
        for a, end, aid in each(periods):
            if a <= t_o and (end is None or t_o < end):
                return active_class(self.c_v - t_o, commit_proven=False), {**detail, "activation_event": aid}
        if any(abs(e["created_at"] - t_o) <= MARGIN for e in each(self.events)
               if (e["from_state"] == "active") != (e["to_state"] == "active")):  # an activation or a freeze
            return "boundary_uncertain", detail
        if self.state != "active":
            return "awaiting_reconciliation", detail
        governing = next(((a, aid) for a, _, aid in each(periods) if a > t_o), None)
        if governing is None:
            return "indeterminate", detail
        a, aid = governing
        return window_class(self.c_v - a, commit_proven=False), {**detail, "activation_event": aid}

    def classify(self):
        """I11 R-DM, I12 agreement and I13 timing for every V2 donation with accounting relevance (§4-§8)."""
        b = self.budget
        relevant = b.collect(self.evals, self.current, self.reversals, self.evidence)
        history_only = {did for did, rows in b.each(self.by_donation.items()) if did not in relevant
                        and any(h["cause"] in ACCOUNTING_CAUSES for h in b.each(rows))}
        for did in b.each(b.sort(b.collect(relevant, history_only))):
            d = self.donations.get(did)
            if d is None or not d["v2"] or not self.in_scope_donation(did):
                continue
            rows = self.by_donation.get(did, [])
            self.i12h(d, rows)
            if did in history_only:  # revision 7 §2.1: I12-H only; nothing current to classify
                continue
            if did in self.violated_donations():  # §4.2 (8): structural / audit classes take precedence
                continue
            self.classify_one(did, d, rows)

    def violated_donations(self) -> set:
        """Every donation with a violation-class finding so far (unchanged definition). Findings are append-only,
        so the index scans only the findings added since its last call (checkpointed); a replaced findings list is
        re-indexed from scratch, so the index is never stale."""
        if getattr(self, "_violations_of", None) is not self.findings or self._violations_seen > len(self.findings):
            self._violations_of, self._violations_seen, self._violated = self.findings, 0, set()
        end = len(self.findings)
        for i in self.budget.each(range(self._violations_seen, end)):  # by index: no copy of the new findings
            f = self.findings[i]
            if SEVERITY[f.cls] == "violation":
                self._violated.add(f.entity_id if f.entity_kind == "donation" else f.detail.get("donation"))
        self._violations_seen = end
        return self._violated  # the index itself (callers test membership only): never copied per call

    def classify_one(self, did, d, rows):
        cur = self.current.get(did, [])
        donation = self.eval_donation(d)
        e = accounting.evaluate_in_mode(donation, cur, self.mode)
        rev = self.reversals.get(did)
        x_applied, x_hold = (rev["applied"], rev["hold"]) if rev else (0, None)
        x_flags = frozenset(self.budget.each(rev["flags"] or ())) if rev else frozenset()
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
                h["run_id"] == ev["ack_run"] and h["cause"] == "manual" and h["evidence_refs"] is None
                for h in self.budget.each(rows)):
            self.add("I12", "audit_integrity", "donation", did, problem="unmatched_ack", run=ev["ack_run"])
            return
        fp_equal = ev is not None and fp_now == ev["evidence_fp"]
        e_flags = e.flags & FLAG_CODES
        last_acct = next((h for h in self.budget.each(reversed(rows)) if h["cause"] in ACCOUNTING_CAUSES), None)

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
            result, bound = self.ack_reconstructs(did, last_acct, ev["ack_fp"]) \
                if ok and ev["ack_fp"] is not None else ("unchecked", None)
            if result == "forged":  # under no mode the state ever had, at no evidence state
                self.add("I12", "audit_integrity", "donation", did, problem="forged_ack", run=last_acct["run_id"])
                return
            if result != "unchecked":  # possibly valid, never proven (A1): no exception; findings are additive only
                if result == "replica_failed":
                    self.add("I10", "structural", "donation", did, problem="fp_replica_mismatch")
                self.add("I12", "historical_ambiguous", "history", last_acct["id"], donation=did,
                         reason="ack_mode_order", basis="mode" if len(self.every_mode()) > 1 else "evidence",
                         run=last_acct["run_id"], enumeration="complete" if result == "possible" else result,
                         **(bound or {}), **detail)
                if ev["ack_fp"] == fp_now and x_applied == _paise(last_acct["reversed_after_paise"]):
                    # the keep would be the audited exception if its ack were established: a legacy history row
                    # whose authority 0034's timestamps cannot settle (permanent, like I12-H ambiguity)
                    return
                detail["ack"] = "mode_order_uncertain"  # fceadd95 continued here; the later checks still run
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
        for phase in (self.load, self.i10, self.view_replica, self.per_donation, self.chains, self.runs_check,
                      self.legacy_timeline, self.state_event_chain, self.evidence_attribution, self.refund_snapshots,
                      self.classify):
            self.budget.check()  # §2.7.3: before and after every phase
            phase()
        self.budget.check()
        return self.report()

    def view_replica(self):
        """The verifier's cut replica of 0029's view must equal the view at cut = infinity (no silent drift)."""
        b = self.budget
        for did in b.each(b.sort(b.collect(self.current, self.evidence))):
            if did not in self.donations or not self.in_scope_donation(did):
                continue
            mine = self.cut_rows(did, "infinity")
            view = b.sort(self.current.get(did, []), key=lambda r: r["provider_ref"])
            if len(mine) != len(view) or any(dict(x) != dict(y) for x, y in b.each(zip(mine, view))):  # pairwise
                self.add("I10", "structural", "view", "payment_integrity_current", problem="view_replica_mismatch",
                         donation=did)

    def report(self) -> dict:
        b = self.budget
        b.check()
        # F8: identity includes the detail, so distinct problems on one entity never collapse; order-independent
        keyed, key_bytes, limit = {}, 0, MAX_BYTES - b.bytes
        for f in b.each(self.findings):
            detail = canonical(f.detail)  # one canonical() call: the accepted single-finding atomic step
            b.check()  # ... checked before the next one
            key_bytes += len(detail)  # the identity strings count against the byte ceiling as they are built
            if key_bytes > limit:
                raise Refused("report byte ceiling exceeded")
            keyed[(f.invariant, f.cls, f.entity_kind, f.entity_id, detail)] = f
        findings = [keyed[k] for k in b.each(b.sort(keyed))]  # one checked sort (§2.7.3)
        counts = defaultdict(int)
        for f in b.each(findings):
            counts[f.cls] += 1
        status = status_of(counts)
        replicas = replica_index(findings, b.each)  # V-A4: one indexed pass, not a scan per A1 finding
        codes = set()
        for f in b.each(findings):
            for code in write_blockers(f, findings, replicas, b.each):  # refuses a malformed finding
                codes.add((code, f.invariant, f.cls, f.entity_kind, f.entity_id))
        blocking = b.sort(codes)
        complete = self.scope_kind == "full"
        g = gates(status, complete, self.state, counts, frozenset(x[0] for x in b.each(blocking)))
        first = self.first
        body = {
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
            "findings": [f.as_dict() for f in b.each(findings)],
            "excluded_buckets": b.sort(self.excluded, key=lambda x: x["bucket"]),
            "orphans": {"orphan_buckets": self.orphan_buckets, "orphan_rows": self.orphan_rows},
            "baselines": b.sort(self.baselines, key=lambda x: x["bucket"]),
            "limitations": list(LIMITATIONS),
            "not_attested": list(NOT_ATTESTED),
            "administrators": {"allow_list_sha256": self.admin_list_sha256, "approved": b.sort(self.admins)},
            # work_units: everything done up to here; the report's own size check and serialization (below)
            # are enforced against the same ceiling and deadline but cannot be included in the report itself
            "runtime": {"rows_loaded": b.rows, "bytes_loaded": b.bytes, "work_units": b.units,
                        "work_units_scope": "excludes the report's own serialization",
                        "provisional_ceilings": {"statement_cap_ms": STATEMENT_CAP_MS, "fetch_batch": FETCH_BATCH,
                                                 "max_rows": MAX_ROWS, "max_bytes": MAX_BYTES, "max_work": MAX_WORK}},
            "write_blockers": [{"blocker": b, "invariant": i, "class": c, "entity_kind": k, "entity_id": e}
                               for b, i, c, k, e in self.budget.each(blocking)],
            "gates": g,
            "status": status,
            "exit_code": EXIT_CODES[status],
        }
        # §2.7.3: the byte ceiling includes the canonical report; the retained report is bounded first (cheap, stops
        # at the limit), then its exact canonical size is streamed; both refuse before anything is serialized
        limit = MAX_BYTES - b.bytes
        if _value_bytes(body, limit, b) > limit:
            raise Refused("report byte ceiling exceeded")
        _, body["digest"] = canonical_stream(body, limit, b)  # == finish(body)["digest"], in one checked stream
        b.check()
        return body


def _guard(first: dict):
    if first["session_user"] != READONLY_ROLE or first["current_user"] != READONLY_ROLE:
        raise Refused("verify runs as hyperchat_readonly only (session and current user)")
    if first["isolation"] != "repeatable read":
        raise Refused("verify needs one REPEATABLE READ snapshot")
    if first["read_only"] != "on":
        raise Refused("verify needs a READ ONLY transaction")
    if first["server_version_num"] < MIN_SERVER:
        raise Refused("PostgreSQL 15 or later required")
    if first["head"] not in EXPECTED_HEADS:
        raise Refused(f"database head {first['head']!r}, expected one of {', '.join(EXPECTED_HEADS)} (6a-2 verifies "
                      "0034 data, on a registered head's schema)")


def scope_args(creator: str | None, donation: str | None) -> tuple[str, str | None]:
    """F7: identity tests, never truthiness; an explicit empty, blank or malformed id refuses (no widening)."""
    if creator is not None and donation is not None:
        raise Refused("--creator and --donation are exclusive")
    kind, sid = ("creator", creator) if creator is not None else (
        ("donation", donation) if donation is not None else ("full", None))
    if sid is not None:
        try:
            sid = str(uuid.UUID(sid))
        except (ValueError, TypeError, AttributeError):
            raise Refused("scope id is not a uuid") from None
    return kind, sid


def _db_reason(exc: Exception) -> str:
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return "statement timeout or cancellation (deadline)"
    if isinstance(exc, psycopg.errors.LockNotAvailable):
        return "lock timeout"
    if isinstance(exc, psycopg.errors.InsufficientPrivilege) and "row-level security" in str(exc):
        return "row_security: read_filtered"  # §5.6: a policy appeared after the catalog check
    return f"database error: {type(exc).__name__}"


def _close(conn):
    try:
        conn.close()
    except Exception:  # noqa: BLE001 - cleanup never masks the result
        pass


def _release(conn, session: Session) -> bool:
    """§2.7.1 steps 2-3: ROLLBACK (close the connection if it fails), then confirm IDLE or closed. Cleanup always
    runs, whatever time remains."""
    try:
        session.protocol("ROLLBACK")
    except Exception:  # noqa: BLE001
        _close(conn)
    try:
        session.cur.close()
    except Exception:  # noqa: BLE001
        pass
    if getattr(conn, "closed", False):
        return True
    try:
        idle = conn.info.transaction_status == TransactionStatus.IDLE
    except Exception:  # noqa: BLE001
        idle = False
    if not idle:
        _close(conn)
    return idle


def verify(conn: psycopg.Connection, *, creator: str | None = None, donation: str | None = None,
           max_seconds: float | None = None, clock=time.monotonic, admins=frozenset(),
           admin_list_sha256: str | None = None) -> dict:
    """Run the verifier on `conn` (autocommit, hyperchat_readonly) within `max_seconds` (required: no production
    default). The report is built, the snapshot released and the release confirmed, and only then is the deadline
    checked: no success is returned after it (§2.7.1). Never raises for a verification problem."""
    try:
        scope_kind, scope_id = scope_args(creator, donation)
    except Refused as exc:
        both = creator is not None and donation is not None
        return refused_report(str(exc), {} if both else {"kind": "creator" if creator is not None else "donation"})
    scope = {"kind": scope_kind, "id": scope_id, "complete": scope_kind == "full"}
    try:
        budget = Budget(max_seconds, clock)
    except Refused as exc:
        return refused_report(str(exc), scope)
    if not conn.autocommit:
        return refused_report("verify needs an autocommit connection (it opens its own transaction)", scope)
    report = reason = None
    session = Session(conn.cursor(row_factory=dict_row), budget)
    try:
        session.protocol("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        session.protocol("SET LOCAL lock_timeout = '5s'")
        session.protocol("SET LOCAL row_security = off")  # §5.6: a filtered read errors instead
        first = _select(session, FIRST)[0]  # the transaction's snapshot is taken here
        _guard(first)
        report = Verifier(session, first, scope_kind, scope_id, budget=budget, admins=admins,
                          admin_list_sha256=admin_list_sha256).run()
    except Refused as exc:
        reason = str(exc)
    except psycopg.Error as exc:
        reason = _db_reason(exc)
    finally:
        released = _release(conn, session)
    if not released:
        return refused_report("the snapshot release could not be confirmed (connection closed)", scope)
    if reason is not None:
        return refused_report(reason, scope)
    if budget.expired():
        return refused_report("deadline exceeded (checked after the snapshot release)", scope)
    return report


# §2.7.2: the approved data statements (exact module constants); each is read through a bounded cursor
_APPROVED_SQL = frozenset({
    FIRST, DONATIONS, STREAMERS, TOTALS, HISTORY, REVERSALS, CURRENT, EVIDENCE, ALL_EVENTS, EVALUATIONS, STATE, EVENTS,
    RUNS, CUT_ROWS, REF_EVENTS, FP_SETS, EVIDENCE_CUTS, FP_CUT, FP_NOW, CAT_ROLES, CAT_MEMBERS_16, CAT_MEMBERS_15,
    CAT_DATABASE, CAT_RELATIONS, CAT_SCHEMA, CAT_TABLE_RIGHTS, CAT_TABLE_RIGHTS_17, CAT_COLUMN_RIGHTS, CAT_FUNCTION_RIGHTS,
    CAT_SCHEMA_CREATE, CAT_FUNCTION_ACL, CAT_SCHEMA_ACL, CAT_TABLE_ACL, CAT_COLUMN_ACL, CAT_TRIGGERS, CAT_FUNCTIONS,
})
