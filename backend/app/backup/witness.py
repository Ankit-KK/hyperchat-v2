"""Choosing the KYC restore witness to keep after a recovery (docs/backup-recovery.md, docs/kyc-rollout.md).

The witness (app.kyc.witness) is append-only: every later copy of one environment's witness starts with every earlier
copy, byte for byte. Recovery must keep the NEWEST copy that exists anywhere (the live file, local backup sets,
off-site sets), never the copy that happens to sit next to the database being restored: an older database paired with
its own older witness would look consistent and silently undo later restrictions, grace revocations or an
activation. So:
  * every candidate must be a byte prefix of the longest one (one history); otherwise refuse (copies diverged:
    tampering, a re-initialised file, or another environment's witness) and leave the decision to the operator;
  * the longest is chosen; its anchor (generation, chain, state) is reported;
  * nothing is written: installing the chosen file and acknowledging the restore stay the operator's manual,
    audited steps (python -m app.kyc.operator acknowledge-restore). This tool never re-anchors anything.

    python -m app.backup.witness select FILE [FILE ...]      prints JSON; exit 0 chosen, 2 refused
"""

import json
import sys
from pathlib import Path

from app.kyc.witness import RANK, KycWitness

STATES = {rank: state for state, rank in RANK.items()}


class WitnessConflict(Exception):
    """The candidates are not one append-only history (message: a reason code)."""


def anchor(path: Path) -> dict:
    seen = KycWitness(str(path)).seen() or {"generation": 0, "chain": "", "rank": 0}
    return {"generation": seen["generation"], "chain": seen["chain"], "state": STATES[seen["rank"]]}


def select(paths: list[Path]) -> dict:
    """The newest candidate of one append-only history, or WitnessConflict."""
    if not paths:
        raise WitnessConflict("no_candidates")
    contents = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise WitnessConflict("not_a_plain_file")
        data = path.read_bytes()
        if data and not data.lstrip().startswith(b"{"):
            raise WitnessConflict("not_a_witness")
        contents.append((path, data))
    contents.sort(key=lambda item: len(item[1]))
    longest = contents[-1][1]
    for _, data in contents:
        if not longest.startswith(data):
            raise WitnessConflict("diverged_histories")
    chosen = contents[-1][0]
    return {"chosen": str(chosen), "bytes": len(longest), **anchor(chosen),
            "candidates": [{"path": str(p), "bytes": len(d), **anchor(p)} for p, d in contents]}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or args[0] != "select":
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2
    try:
        result = select([Path(a) for a in args[1:]])
    except (WitnessConflict, OSError) as exc:
        print(json.dumps({"ok": False, "reason": str(exc) or type(exc).__name__}))
        return 2
    print(json.dumps({"ok": True, **result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
