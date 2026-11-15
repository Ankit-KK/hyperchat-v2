"""PAY-15 (D17) operator commands: `python -m app.payments.operator <command>`.

Stage 6a-2 implements only `verify` (read-only, plan revision 10 §9 / D-6):

    HYPERCHAT_READONLY_DSN='host=... dbname=... user=hyperchat_readonly' \
        python -m app.payments.operator verify [--creator UUID | --donation UUID] [--output FILE]

It connects as hyperchat_readonly, prints the canonical JSON report (ids, enums and amounts only) and exits with the
D-7 code: 0 clean, 1 violation, 2 refused, 3 attention. A scoped run (--creator / --donation) has every gate false.
The writing commands (state, reconcile, resolve, revert) belong to stage 6a-3 and are refused here (exit 2).
"""

import argparse
import os
import sys

import psycopg

from app.payments import verify as verifier

LATER = ("state", "reconcile", "resolve", "revert")  # stage 6a-3


def _emit(report: dict, output: str | None) -> int:
    text = verifier.canonical(report)
    if output:
        with open(output, "w", encoding="ascii") as f:
            f.write(text + "\n")
    sys.stdout.write(text + "\n")
    return report["exit_code"]


def run_verify(args) -> int:
    dsn = os.environ.get("HYPERCHAT_READONLY_DSN", "")
    scope = {"kind": "creator" if args.creator else ("donation" if args.donation else "full")}
    if not dsn:
        return _emit(verifier.refused_report("HYPERCHAT_READONLY_DSN is not set", scope), args.output)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10,
                             application_name="hyperchat-accounting-verify") as conn:
            report = verifier.verify(conn, creator=args.creator, donation=args.donation)
    except psycopg.Error as exc:
        report = verifier.refused_report(f"cannot connect: {type(exc).__name__}", scope)
    except Exception as exc:  # noqa: BLE001 - fail closed: an internal error is never a clean report
        report = verifier.refused_report(f"internal error: {type(exc).__name__}", scope)
    return _emit(report, args.output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.payments.operator")
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("verify", help="read-only accounting verification (hyperchat_readonly)")
    group = v.add_mutually_exclusive_group()
    group.add_argument("--creator", help="limit to one creator id (every gate false)")
    group.add_argument("--donation", help="limit to one donation id (every gate false)")
    v.add_argument("--output", help="also write the report to this file")
    for name in LATER:
        sub.add_parser(name, add_help=False)
    args, extra = parser.parse_known_args(argv)
    if args.command != "verify":
        sys.stdout.write(verifier.canonical({"status": "refused", "exit_code": verifier.EXIT_CODES["refused"],
                                             "reason": f"'{args.command}' is not available in stage 6a-2"}) + "\n")
        return verifier.EXIT_CODES["refused"]
    if extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    return run_verify(args)


if __name__ == "__main__":
    sys.exit(main())
