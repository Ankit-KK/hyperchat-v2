"""KYC rollout operator tool (KYC PR4, migration 0023). Run ONLY by the operator, with the migration role, after the
approvals in docs/kyc-rollout.md. The API cannot do any of this.

    HYPERCHAT_MIGRATION_DSN=... HYPERCHAT_KYC_WITNESS_PATH=... python -m app.kyc.operator <command>

  report                      read-only: enforcement state, witness check and the per-creator impact preview
  grant --creator ID --days N --reason TEXT --operator NAME --confirm-database DB
                              one finite grace grant (1..90 days) for an active legacy creator, bound to its owner
  activate --operator NAME --authorization REF --confirm-database DB
                              draft -> active: legacy creators then need their owner's KYC or valid grace
  close --operator NAME --authorization REF --confirm-database DB
                              active -> closed: no more grace, every creator needs its owner's KYC; never reopens
  witness-init --confirm-database DB
                              create the witness file from the database (only when no witness file exists)
  acknowledge-restore --operator NAME --detail TEXT --confirm-database DB
                              after a restore was reconciled: move the generation past the witness

Every change needs --confirm-database (the database's own name) and the witness path; a database that is behind its
witness (a restored snapshot) is refused until it is reconciled. Exit codes: 0 done, 2 refused.
"""

import argparse
import json
import os
import sys

import psycopg
from psycopg.rows import dict_row

from app.kyc.witness import RANK, KycWitness


def _connect() -> psycopg.Connection:
    dsn = os.environ["HYPERCHAT_MIGRATION_DSN"].replace("postgresql+psycopg://", "postgresql://", 1)
    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    conn.execute("SET ROLE hyperchat_owner")
    return conn


def _state(conn) -> dict:
    return conn.execute("SELECT state, generation, activated_at, activated_by, activation_authorization, closed_at, "
                        "current_database() AS database FROM public.kyc_enforcement").fetchone()


def _refuse(message: str) -> int:
    print(f"Refused: {message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.kyc.operator", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report")
    report.add_argument("--json", action="store_true")
    for name in ("grant", "activate", "close", "witness-init", "acknowledge-restore"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--confirm-database", required=True)
        if name != "witness-init":
            cmd.add_argument("--operator", required=True)
    sub.choices["grant"].add_argument("--creator", required=True)
    sub.choices["grant"].add_argument("--days", type=int, required=True)
    sub.choices["grant"].add_argument("--reason", required=True)
    for name in ("activate", "close"):
        sub.choices[name].add_argument("--authorization", required=True, help="the approval reference")
    sub.choices["acknowledge-restore"].add_argument("--detail", required=True)
    args = parser.parse_args(argv)
    witness = KycWitness(os.environ.get("HYPERCHAT_KYC_WITNESS_PATH"))

    with _connect() as conn:
        state = _state(conn)
        problem = witness.problem(state["state"], state["generation"])
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
            witness.record(state["state"], state["generation"], action="witness_init")  # nothing for untouched draft
            print(f"Witness at generation {state['generation']} ({state['state']}).")
            return 0
        if args.command == "acknowledge-restore":
            seen = witness.seen()
            if problem != "restore_suspected" or seen is None:
                return _refuse("the witness does not show a restore to acknowledge")
            if RANK[state["state"]] < seen[1]:
                return _refuse("the database's enforcement state is behind the witness: re-run activate/close first")
            generation = conn.execute("SELECT public.kyc_acknowledge_restore(%s, %s, %s) AS g",
                                      (args.operator, seen[0] + 1, args.detail)).fetchone()["g"]
            witness.record(state["state"], generation, action="restore_acknowledged")
            print(f"Restore acknowledged: generation {generation}.")
            return 0
        if problem is not None:
            return _refuse(f"the restore witness reports {problem}; reconcile first")
        try:
            if args.command == "grant":
                grant = conn.execute("SELECT public.kyc_grant_grace(%s, %s, %s, %s) AS g",
                                     (args.creator, args.days, args.operator, args.reason)).fetchone()["g"]
                result = {"action": "grace_granted", "grant_id": str(grant), "streamer_id": args.creator}
            elif args.command == "activate":
                conn.execute("SELECT public.kyc_enforcement_activate(%s, %s)", (args.operator, args.authorization))
                result = {"action": "enforcement_activated"}
            else:
                conn.execute("SELECT public.kyc_enforcement_close(%s, %s)", (args.operator, args.authorization))
                result = {"action": "enforcement_closed"}
        except psycopg.errors.Error as exc:
            return _refuse(exc.diag.message_primary or exc.sqlstate or "database error")
        after = _state(conn)
        witness.record(after["state"], after["generation"], **result)
        print(f"{result['action']}: enforcement {after['state']} (generation {after['generation']}).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
