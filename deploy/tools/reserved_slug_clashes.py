#!/usr/bin/env python3
"""Existing creators whose slug is now reserved (owner decision R6, PDP v2.0 addendum §2.1): read-only.

    HYPERCHAT_READONLY_DSN='host=/var/run/postgresql port=5433 dbname=hyperchat_dev user=hyperchat_readonly' \\
        python -B deploy/tools/reserved_slug_clashes.py

Compares every creator (any status) against `RESERVED_SLUGS` in backend/app/admin/service.py, case-insensitively.
Prints one line per clash (slug and an 8-character id prefix only) and exits 1; exits 0 when there is none; exits 2
when the DSN is missing or the read fails. The DSN comes from the environment only and is never printed; the query
runs in a read-only transaction. Run from a release or repository checkout (it imports the backend's list).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.admin.service import RESERVED_SLUGS  # noqa: E402


def build_query(reserved=RESERVED_SLUGS) -> tuple[str, tuple]:
    return ("SELECT id::text AS id, streamer_slug FROM public.streamers "
            "WHERE lower(streamer_slug) = ANY(%s) ORDER BY streamer_slug", (sorted(reserved),))


def report(rows) -> list[str]:
    return [f"reserved slug in use: {r['streamer_slug']} (id {r['id'][:8]})" for r in rows]


def main() -> int:
    dsn = os.environ.get("HYPERCHAT_READONLY_DSN", "")
    if not dsn:
        print("HYPERCHAT_READONLY_DSN is not set", file=sys.stderr)
        return 2
    try:
        import psycopg
        from psycopg.rows import dict_row

        # The role already defaults to read-only; the option holds even for another role.
        with psycopg.connect(dsn, options="-c default_transaction_read_only=on", row_factory=dict_row) as conn:
            rows = conn.execute(*build_query()).fetchall()
    except Exception as exc:  # noqa: BLE001 - driver errors can echo the DSN; print the type only
        print(f"database read failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    lines = report(rows)
    print("\n".join(lines) if lines else f"no clashes ({len(RESERVED_SLUGS)} reserved slugs checked)")
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main())
