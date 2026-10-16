"""KYC rollout operator tool (KYC PR4, migration 0023). Run ONLY by the operator, with the migration role, after the
approvals in docs/kyc-rollout.md. The API and its database role cannot do any of this.

    HYPERCHAT_MIGRATION_DSN=... HYPERCHAT_KYC_WITNESS_PATH=... python -m app.kyc.operator <command>

  report                      read-only: enforcement state, witness check and the per-creator impact preview
  grant --creator ID --days N --reason TEXT --operator NAME --confirm-database DB
                              one finite grace grant (1..90 days) for an active legacy creator whose owner never had a
                              Didit decision, bound to that owner
  lift-restriction --creator ID --restriction ID --note TEXT --operator NAME --confirm-database DB
                              lift an admin restriction (it never bypasses KYC)
  activate --operator NAME --authorization REF --confirm-database DB
                              draft -> active: legacy creators then need their owner's KYC or valid grace
  close --operator NAME --authorization REF --confirm-database DB
                              active -> closed: no more grace, every creator needs its owner's KYC; never reopens
  witness-init --operator NAME --confirm-database DB
                              create the witness file from the database's head (only when no witness file exists)
  acknowledge-restore --operator NAME --detail TEXT --confirm-database DB
                              after a restore was reconciled (lost changes re-applied): an audited database change
                              that re-anchors the witness on the database's history

Every change needs --confirm-database (the database's own name) and the witness path; a database inconsistent with its
witness (a restored snapshot) is refused until it is reconciled. Exit codes: 0 done, 2 refused.
"""

import argparse
import json
import os
import sys

import psycopg
from psycopg.rows import dict_row

from app.kyc.witness import RANK, KycWitness

_HEAD = ("SELECT e.state, e.generation, e.chain, current_database() AS database, (SELECT ev.chain FROM "
         "public.kyc_enforcement_events ev WHERE ev.generation = %s) AS anchor_chain FROM public.kyc_enforcement e")


def _connect() -> psycopg.Connection:
    dsn = os.environ["HYPERCHAT_MIGRATION_DSN"].replace("postgresql+psycopg://", "postgresql://", 1)
    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    conn.execute("SET ROLE hyperchat_owner")
    return conn


def _refuse(message: str) -> int:
    print(f"Refused: {message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.kyc.operator", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report")
    report.add_argument("--json", action="store_true")
    for name in ("grant", "lift-restriction", "activate", "close", "witness-init", "acknowledge-restore"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--confirm-database", required=True)
        cmd.add_argument("--operator", required=True)
    for name in ("grant", "lift-restriction"):
        sub.choices[name].add_argument("--creator", required=True)
    sub.choices["grant"].add_argument("--days", type=int, required=True)
    sub.choices["grant"].add_argument("--reason", required=True)
    sub.choices["lift-restriction"].add_argument("--restriction", required=True)
    sub.choices["lift-restriction"].add_argument("--note", required=True)
    for name in ("activate", "close"):
        sub.choices[name].add_argument("--authorization", required=True, help="the approval reference")
    sub.choices["acknowledge-restore"].add_argument("--detail", required=True)
    args = parser.parse_args(argv)
    witness = KycWitness(os.environ.get("HYPERCHAT_KYC_WITNESS_PATH"))

    with _connect() as conn:
        def head() -> tuple[dict, str | None]:
            row = conn.execute(_HEAD, (witness.anchor_generation(),)).fetchone()
            return row, witness.problem(row["state"], row["generation"], row["anchor_chain"])

        state, problem = head()
        if args.command == "report":
            rows = conn.execute("SELECT * FROM public.kyc_enforcement_preview()").fetchall()
            if args.json:
                print(json.dumps({"enforcement": state, "witness_problem": problem, "creators": rows}, default=str))
            else:
                print(f"database {state['database']}: enforcement {state['state']} (generation {state['generation']}), "
                      f"witness {problem or 'ok'}")
                for r in rows:
                    print(f"{r['streamer_slug']:<32} {r['status']:<9} {r['kyc_track']:<8} now={r['block_now'] or 'ok':<12}"
                          f" active={r['block_when_active'] or 'ok':<12} closed={r['block_when_closed'] or 'ok':<12}"
                          f" kyc={'yes' if r['owner_kyc_approved'] else 'no':<3} grace_until={r['grace_expires_at']}")
            return 0

        if args.confirm_database != state["database"]:
            return _refuse(f"--confirm-database does not name this database ({state['database']})")
        if witness.path is None:
            return _refuse("HYPERCHAT_KYC_WITNESS_PATH is not set (every change must be witnessed)")
        if args.command == "witness-init":
            if witness.seen() is not None:
                return _refuse("a witness file already exists")
            witness.record(state["state"], state["generation"], state["chain"], action="witness_init",
                           operator=args.operator)
            print(f"Witness anchored at generation {state['generation']} ({state['state']}).")
            return 0
        if args.command == "acknowledge-restore":
            seen = witness.seen()
            if problem != "restore_suspected" or seen is None:
                return _refuse("the witness does not show a restore to acknowledge")
            if RANK[state["state"]] < seen["rank"]:
                return _refuse("the database's enforcement state is behind the witness: re-run activate/close first")
            try:
                conn.execute("SELECT public.kyc_acknowledge_restore(%s, %s, %s)",
                             (args.operator, max(seen["generation"], state["generation"]) + 1, args.detail))
            except psycopg.errors.Error as exc:
                return _refuse(exc.diag.message_primary or exc.sqlstate or "database error")
            after, _ = head()
            witness.record(after["state"], after["generation"], after["chain"], action="restore_acknowledged",
                           operator=args.operator)  # the re-anchor: the only line written over a mismatch
            print(f"Restore acknowledged: the witness is anchored at generation {after['generation']}.")
            return 0
        # Activation and closure only tighten the rules, so they may re-apply a state a restore lost (never re-anchoring
        # the witness); everything else waits for the acknowledgement.
        if problem is not None and not (problem == "restore_suspected" and args.command in ("activate", "close")):
            return _refuse(f"the restore witness reports {problem}; reconcile first")
        try:
            if args.command == "grant":
                grant = conn.execute("SELECT public.kyc_grant_grace(%s, %s, %s, %s) AS g",
                                     (args.creator, args.days, args.operator, args.reason)).fetchone()["g"]
                result = {"action": "grace_granted", "grant_id": str(grant), "streamer_id": args.creator}
            elif args.command == "lift-restriction":
                outcome = conn.execute("SELECT public.kyc_lift_restriction(%s, %s, %s, %s) AS r",
                                       (args.creator, args.restriction, args.note, args.operator)).fetchone()["r"]
                if outcome != "ok":
                    return _refuse(f"restriction {outcome}")
                result = {"action": "restriction_lifted", "streamer_id": args.creator}
            elif args.command == "activate":
                conn.execute("SELECT public.kyc_enforcement_activate(%s, %s)", (args.operator, args.authorization))
                result = {"action": "enforcement_activated"}
            else:
                conn.execute("SELECT public.kyc_enforcement_close(%s, %s)", (args.operator, args.authorization))
                result = {"action": "enforcement_closed"}
        except psycopg.errors.Error as exc:
            return _refuse(exc.diag.message_primary or exc.sqlstate or "database error")
        after, after_problem = head()
        if after_problem is None:  # an extension of the witnessed history (always, unless a restore raced this)
            witness.record(after["state"], after["generation"], after["chain"], operator=args.operator, **result)
        print(f"{result['action']}: enforcement {after['state']} (generation {after['generation']}).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
