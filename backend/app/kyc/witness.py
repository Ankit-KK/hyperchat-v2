"""Restore witness for KYC enforcement (KYC PR4; docs/kyc-rollout.md).

Every enforcement change is one row of public.kyc_enforcement_events at the next generation, carrying a hash chain
over the whole history (migration 0023). The API and the operator tool append the database's (generation, chain, state)
to a file OUTSIDE PostgreSQL (HYPERCHAT_KYC_WITNESS_PATH, one JSON line per change: ids and codes only, fsynced).

The ANCHOR is the witnessed line with the highest generation. The database is consistent with the witness only if
it still holds that exact chain at that generation and has not gone back in state. A database restored from an older
snapshot fails this, and adding changes never heals it: its later rows carry other random ids, so its chain at the
anchor's generation differs forever. Only the operator's audited acknowledgement (app.kyc.operator
acknowledge-restore) re-anchors the witness; ordinary API changes only append lines that extend the anchored history.
While inconsistent, new donations fail closed (app.public).

Limits (docs/kyc-rollout.md): a change committed but not yet witnessed (a crash between commit and write, or a direct
database-role call) is accepted as an extension and only protected once a later line records it (the next change or
the daily cleanup); the check runs in the API and operator tool, never inside PostgreSQL (the donations trigger cannot
read a file); the file protects only while it is kept apart from backups and append-only (chattr +a).

Without a configured path only the untouched pre-rollout database is accepted (state draft, generation 0).
"""

import json
import os
from datetime import UTC, datetime

RANK = {"draft": 0, "active": 1, "closed": 2}


class KycWitness:
    def __init__(self, path: str | None) -> None:
        self.path = path or None

    def seen(self) -> dict | None:
        """{"generation", "chain"} of the highest-generation line and the highest state "rank" over all lines; None
        when the file does not exist. A torn line (a crash mid-write) is skipped, never trusted."""
        try:
            with open(self.path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            return None
        anchor = {"generation": 0, "chain": "", "rank": 0}
        for line in lines:
            try:
                entry = json.loads(line)
                generation, rank, chain = int(entry["generation"]), RANK[entry["state"]], str(entry["chain"])
            except (ValueError, KeyError, TypeError):
                continue
            anchor["rank"] = max(anchor["rank"], rank)
            if generation > anchor["generation"]:
                anchor["generation"], anchor["chain"] = generation, chain
        return anchor

    def anchor_generation(self) -> int:
        """The generation whose chain the database must still hold (0: nothing to look up)."""
        try:
            seen = self.seen() if self.path else None
        except OSError:
            return 0
        return seen["generation"] if seen else 0

    def problem(self, state: str, generation: int, chain_at_anchor: str | None) -> str | None:
        """None when the database extends the witnessed history, else why new donations must stop.
        `chain_at_anchor` is the database's chain at anchor_generation() (None when it has no such row)."""
        untouched = state == "draft" and generation == 0
        if self.path is None:
            return None if untouched else "witness_not_configured"
        try:
            seen = self.seen()
        except OSError:
            return "witness_unreadable"
        if seen is None:
            return None if untouched else "witness_missing"
        if RANK[state] < seen["rank"] or generation < seen["generation"]:
            return "restore_suspected"
        if seen["generation"] and chain_at_anchor != seen["chain"]:
            return "restore_suspected"  # the history at the anchor is not the one that was witnessed
        return None

    def record(self, state: str, generation: int, chain: str, **event) -> None:
        """Append the database's current head. Callers append only after problem() returned None for it (an
        extension of the anchored history), except the operator's acknowledgement / init, which re-anchor."""
        if self.path is None or generation == 0:
            return
        seen = self.seen()
        if seen is not None and generation <= seen["generation"] and RANK[state] <= seen["rank"]:
            return
        line = json.dumps({"generation": generation, "chain": chain, "state": state,
                           "at": datetime.now(UTC).isoformat(), **event}, sort_keys=True) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
