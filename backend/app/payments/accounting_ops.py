"""PAY-15 (D17) stage 6a-3: the owner-only accounting operations behind `python -m app.payments.operator`.

Every command is report-only by default. Writing needs --apply, --confirm-database <current database>, --operator and
--reason, and goes only through migration 0034's owner functions (0035 binds each use to its run's scope, F-11):

    state            payment_accounting_set_state(state, mode, operator, reason, NULL)    one statement
    reconcile        payment_accounting_open_run('reconcile', ...) then, per donation in its own transaction,
                     payment_accounting_reconcile(donation, reversed, hold, flags, refs, run)          state active
    resolve keep     ... payment_accounting_keep(donation, run)                     (H-DISPUTE-MODE) active / frozen
    resolve apply    ... payment_accounting_apply_evaluation(donation, reversed, flags, refs, run)     state active
    revert           ... payment_accounting_revert(donation, run)                                   frozen / off

The plan uses the application's own evaluator (accounting.evaluate_in_mode over payment_integrity_current), exactly as
repository._settle_accounting does; the database re-derives and refuses anything else. A run always carries a
"donations" scope naming exactly the planned donations and expiring within one hour (OD-6a3-S: never unscoped,
never creator / all_v2). Exit codes (D-7): 0 nothing to do / all applied, 3 work planned (report), 1 a donation was
refused or a reconcile's second report is not zero, 2 refused before anything was written.
"""

import json
import re
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.rows import dict_row

from app.payments import accounting
from app.payments.repository import V2_PROVENANCE

OWNER_DSN_ENV = "HYPERCHAT_ACCOUNTING_OWNER_DSN"
OWNER_ROLE = "hyperchat_owner"
OPERATOR = re.compile(r"[a-z][a-z0-9._-]{1,62}")
RUN_LIFE = timedelta(hours=1)
EXIT = {"clean": 0, "failed": 1, "refused": 2, "planned": 3}
COMMANDS = {"reconcile": "reconcile", "keep": "resolve-keep", "apply": "resolve-apply", "revert": "revert"}
STATES_FOR = {"reconcile": ("active",), "keep": ("active", "frozen"), "apply": ("active",), "revert": ("frozen", "off")}

STATE = "SELECT state, dispute_mode, current_database() AS database FROM public.payment_accounting_state"
DONATIONS = f"""SELECT d.id::text AS id, d.streamer_id::text AS streamer_id, d.amount, d.amount_inr,
    d.currency::text AS currency, d.payment_status, d.provider_payment_id, d.legacy_source_table, ({V2_PROVENANCE}) AS v2,
    COALESCE(r.applied_reversed_paise, 0) AS applied, r.hold, COALESCE(r.flags, '{{}}') AS flags
    FROM public.donations d LEFT JOIN public.donation_reversals r ON r.donation_id = d.id
    WHERE ({V2_PROVENANCE}) AND (d.id = ANY (%(ids)s::uuid[]) OR (%(ids)s::uuid[] IS NULL AND (r.donation_id IS NOT NULL
        OR EXISTS (SELECT 1 FROM public.payment_integrity_current c WHERE c.donation_id = d.id))))
    ORDER BY d.id"""
CURRENT = """SELECT kind, provider_ref, provider_payment_id, donation_id::text AS donation_id, match_type, phase,
    status, amount_paise, currency, amount_deducted_paise, conflicting
    FROM public.payment_integrity_current WHERE donation_id = ANY (%s::uuid[])"""
OPEN_RUN = "SELECT public.payment_accounting_open_run(%s, %s, %s, NULL, %s::jsonb) AS run"
CALLS = {
    "reconcile": "SELECT public.payment_accounting_reconcile(%(d)s, %(reversed)s, %(hold)s, %(flags)s, %(refs)s, %(run)s)",
    "keep": "SELECT public.payment_accounting_keep(%(d)s, %(run)s)",
    "apply": "SELECT public.payment_accounting_apply_evaluation(%(d)s, %(reversed)s, %(flags)s, %(refs)s, %(run)s)",
    "revert": "SELECT public.payment_accounting_revert(%(d)s, %(run)s)",
}
SET_STATE = "SELECT public.payment_accounting_set_state(%s, %s, %s, %s, NULL) AS outcome"


class Refusal(Exception):
    """Refused before anything was written (exit 2)."""


def validate(*, apply: bool, confirm: str | None, operator: str | None, reason: str | None, donations) -> list | None:
    """The argument contract (pure). Returns the normalised donation filter (None = every candidate)."""
    ids = None
    if donations:
        ids = []
        for value in donations:
            try:
                ids.append(str(uuid.UUID(value)))
            except (ValueError, TypeError):
                raise Refusal(f"not a donation id: {value!r}") from None
        if len(set(ids)) != len(ids):
            raise Refusal("a donation id is repeated")
    if apply:
        if not confirm:
            raise Refusal("--apply needs --confirm-database <the database name>")
        if not operator or not OPERATOR.fullmatch(operator):
            raise Refusal("--apply needs --operator matching [a-z][a-z0-9._-]{1,62}")
        if not reason or not 10 <= len(reason.strip()) <= 500:
            raise Refusal("--apply needs --reason of 10-500 characters")
    return ids


def plan_item(action: str, d: dict, rows: list, mode: str) -> dict | None:
    """What `action` would change for one donation (pure), or None when nothing would change."""
    current = (d["applied"], d["hold"], sorted(d["flags"] or ()))
    if action == "revert":
        return None if current[:2] == (0, None) else {"donation": d["id"], "from": list(current[:2]), "to": [0, None]}
    if action == "keep":
        return {"donation": d["id"], "hold": d["hold"]} if d["hold"] == accounting.DISPUTE_MODE_HOLD else None
    result = accounting.evaluate_in_mode(d, rows, mode)
    flags = sorted(result.flags & {"F-BOTH", "F-OPEN"})
    refs = [] if result.hold else accounting.counted_refs(d, rows)
    if action == "apply":
        if d["hold"] is None or result.hold is not None:
            return None  # only a held donation whose evidence is now clear
        return {"donation": d["id"], "reversed": result.reversed_paise, "flags": flags, "refs": refs}
    target = (d["applied"] if result.hold else result.reversed_paise, result.hold, flags)
    if target == current:
        return None
    return {"donation": d["id"], "reversed": result.reversed_paise, "hold": result.hold, "flags": flags, "refs": refs,
            "from": [current[0], current[1], current[2]], "to": [target[0], target[1], target[2]]}


def connect(dsn: str | None):
    if not dsn:
        raise Refusal(f"{OWNER_DSN_ENV} is not set (the owner connection: migrator credentials, then {OWNER_ROLE})")
    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
    conn.execute(f"SET ROLE {OWNER_ROLE}")
    return conn


def _plan(conn, action: str, ids: list | None) -> tuple[dict, list]:
    with conn.cursor(row_factory=dict_row) as cur:
        state = cur.execute(STATE).fetchone()
        donations = cur.execute(DONATIONS, {"ids": ids}).fetchall()
        rows = cur.execute(CURRENT, ([d["id"] for d in donations],)).fetchall() if donations else []
    by = {}
    for r in rows:
        by.setdefault(r["donation_id"], []).append(r)
    items = [x for d in donations if (x := plan_item(action, d, by.get(d["id"], []), state["dispute_mode"]))]
    return state, items


def run_action(conn, action: str, *, apply: bool, confirm: str | None, operator: str | None, reason: str | None,
               ids: list | None) -> dict:
    state, items = _plan(conn, action, ids)
    out = {"command": COMMANDS[action], "database": state["database"], "state": state["state"],
           "dispute_mode": state["dispute_mode"], "planned": items, "mode": "apply" if apply else "report"}
    if not apply:
        return {**out, "status": "planned" if items else "clean"}
    if confirm != state["database"]:
        raise Refusal(f"--confirm-database {confirm!r} is not the connected database {state['database']!r}")
    if state["state"] not in STATES_FOR[action]:
        raise Refusal(f"{COMMANDS[action]} needs the accounting state {' or '.join(STATES_FOR[action])} "
                      f"(it is {state['state']})")
    if not items:
        return {**out, "status": "clean", "results": []}
    scope = {"kind": "donations", "donations": [x["donation"] for x in items],
             "expires_at": (datetime.now(UTC) + RUN_LIFE).isoformat()}
    run = conn.execute(OPEN_RUN, (COMMANDS[action], operator, reason.strip(), json.dumps(scope))).fetchone()[0]
    results, status = [], "clean"
    for x in items:
        params = {"d": x["donation"], "run": run, "reversed": x.get("reversed"), "hold": x.get("hold"),
                  "flags": x.get("flags", []), "refs": x.get("refs", [])}
        try:
            with conn.transaction():  # one donation, one transaction (the database's lock order inside)
                outcome = conn.execute(CALLS[action], params).fetchone()[0]
        except psycopg.Error as exc:
            results.append({"donation": x["donation"], "refused": exc.sqlstate or "error"})
            status = "failed"
            break  # stop at the first refusal; the earlier donations stay written (each its own transaction)
        results.append({"donation": x["donation"], "outcome": outcome})
    out = {**out, "run": run, "results": results, "status": status}
    if action == "reconcile" and status == "clean":
        _, again = _plan(conn, action, [x["donation"] for x in items])
        out["second_report"] = again
        if again:  # F13: the second report must plan zero
            out["status"] = "failed"
    return out


def run_state(conn, *, to: str | None, mode: str | None, apply: bool, confirm: str | None, operator: str | None,
              reason: str | None) -> dict:
    with conn.cursor(row_factory=dict_row) as cur:
        state = cur.execute(STATE).fetchone()
    out = {"command": "state", "database": state["database"], "state": state["state"],
           "dispute_mode": state["dispute_mode"], "mode": "apply" if apply else "report"}
    if not apply:
        planned = (to or mode) is not None and (to or state["state"], mode or state["dispute_mode"]) != (
            state["state"], state["dispute_mode"])
        return {**out, "planned": {"to": to, "dispute_mode": mode} if planned else None,
                "status": "planned" if planned else "clean"}
    if confirm != state["database"]:
        raise Refusal(f"--confirm-database {confirm!r} is not the connected database {state['database']!r}")
    if to is None and mode is None:
        raise Refusal("state --apply needs --to and / or --mode")
    try:
        outcome = conn.execute(SET_STATE, (to, mode, operator, reason.strip())).fetchone()[0]  # one statement
    except psycopg.Error as exc:
        return {**out, "results": [{"refused": exc.sqlstate or "error"}], "status": "failed"}
    return {**out, "results": [{"outcome": outcome}], "status": "clean"}
