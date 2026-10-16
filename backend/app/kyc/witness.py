"""Restore witness for KYC enforcement (KYC PR4; docs/kyc-rollout.md).

public.kyc_enforcement.generation goes up with every enforcement change (activation, closure, grace grant or
revocation, restriction) and its state only moves forward (draft -> active -> closed). A database restored from an
older snapshot silently goes back on both, and nothing inside that database can tell. So the highest generation and
state the database has shown are also appended to a file OUTSIDE PostgreSQL (HYPERCHAT_KYC_WITNESS_PATH, one JSON
line per change: ids and codes only). When the database reports less than the witness, new donations fail closed
(app.public) until an operator reconciles (app.kyc.operator acknowledge-restore). The file is append-only by design;
on the server it is also made append-only for its owner (chattr +a), so it cannot be rewritten or removed by the API.

Without a configured path only the untouched pre-rollout database is accepted (state draft, generation 0).
"""

import json
import os
from datetime import UTC, datetime

RANK = {"draft": 0, "active": 1, "closed": 2}


class KycWitness:
    def __init__(self, path: str | None) -> None:
        self.path = path or None

    def seen(self) -> tuple[int, int] | None:
        """(highest generation, highest state rank) over every valid line; None when the file does not exist. A torn
        last line (a crash mid-write) is skipped, never trusted."""
        try:
            with open(self.path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            return None
        generation = rank = 0
        for line in lines:
            try:
                entry = json.loads(line)
                generation, rank = max(generation, int(entry["generation"])), max(rank, RANK[entry["state"]])
            except (ValueError, KeyError, TypeError):
                continue
        return generation, rank

    def problem(self, state: str, generation: int) -> str | None:
        """None when the database is at least where the witness has seen it, else why new donations must stop."""
        untouched = state == "draft" and generation == 0
        if self.path is None:
            return None if untouched else "witness_not_configured"
        try:
            seen = self.seen()
        except OSError:
            return "witness_unreadable"
        if seen is None:
            return None if untouched else "witness_missing"
        if generation < seen[0] or RANK[state] < seen[1]:
            return "restore_suspected"
        return None

    def record(self, state: str, generation: int, **event) -> None:
        """Append the database's current (state, generation) when it is beyond what the witness holds (fsynced)."""
        if self.path is None:
            return
        seen = self.seen() or (0, 0)
        if generation <= seen[0] and RANK[state] <= seen[1]:
            return
        line = json.dumps({"generation": generation, "state": state, "at": datetime.now(UTC).isoformat(), **event},
                          sort_keys=True) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
