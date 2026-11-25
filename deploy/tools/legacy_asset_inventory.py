#!/usr/bin/env python3
"""Inventory of creator branding URLs (D-M2b, checklist CRE-8): read-only, no network calls to any URL.

    HYPERCHAT_READONLY_DSN='host=/var/run/postgresql port=5433 dbname=hyperchat_dev user=hyperchat_readonly' \\
        python -B deploy/tools/legacy_asset_inventory.py --r2-base https://media.example.test --csv /root/inventory.csv

    python -B deploy/tools/legacy_asset_inventory.py --fixture rows.json --r2-base https://media.example.test --csv out.csv

Lists every creator's `brand_logo_url` and `background_url`, classifies each value and writes a CSV (full values, keep it
outside the repository) plus a summary on stdout (counts only; non-legacy rows are named by an 8-character id prefix).

Classes:
  legacy_frontend_path  `/assets/streamers/<slug>/<file>`: a path of the legacy frontend, served by it, not by R2
  r2                    an https URL on one of the `--r2-base` bases (the configured media domain; compared on `base/`)
  third_party           any other http(s) URL: reviewed separately by the owner (D-M2b)
  empty                 NULL or whitespace
  unknown               anything else (another relative path, a protocol-relative URL, ...): fails closed, listed for review

For a legacy path the CSV carries the proposed target key template `creator-assets/<streamer id>/<kind>/{sha256}.<ext>`
(the copy step fills `{sha256}` with the file's hash; docs/operations/legacy-asset-migration-plan.md). The DSN comes
from the environment only and is never printed; the query runs in a read-only transaction. `--fixture` reads the same
rows from a JSON list (CI, no database).
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter
from urllib.parse import urlsplit

COLUMNS = ("brand_logo_url", "background_url")
KIND = {"brand_logo_url": "logo", "background_url": "background"}
TARGET_PREFIX = "creator-assets"
LEGACY_PATH = re.compile(r"^/assets/streamers/(?P<slug>[A-Za-z0-9_.-]+)/(?P<file>[A-Za-z0-9_.-]+\.(?P<ext>[A-Za-z0-9]+))$")
SELECT = ("SELECT id::text AS id, streamer_slug, status, brand_logo_url, background_url "
          "FROM public.streamers ORDER BY id")
FIELDS = ("streamer_id", "streamer_slug", "status", "column", "value", "class", "target_key_template")


def classify(value, r2_bases: tuple[str, ...]) -> str:
    if value is None or not str(value).strip():
        return "empty"
    value = str(value)
    if LEGACY_PATH.match(value):
        return "legacy_frontend_path"
    parts = urlsplit(value)
    if parts.scheme in ("http", "https") and parts.hostname:
        if any(value.startswith(base.rstrip("/") + "/") for base in r2_bases):
            return "r2"
        return "third_party"
    return "unknown"


def target_key_template(streamer_id: str, column: str, value: str) -> str:
    m = LEGACY_PATH.match(value)
    return f"{TARGET_PREFIX}/{streamer_id}/{KIND[column]}/{{sha256}}.{m.group('ext').lower()}" if m else ""


def inventory(rows, r2_bases: tuple[str, ...]) -> list[dict]:
    """One record per (creator, column), in row order."""
    out = []
    for row in rows:
        for column in COLUMNS:
            value = row.get(column)
            cls = classify(value, r2_bases)
            out.append({"streamer_id": row["id"], "streamer_slug": row["streamer_slug"], "status": row.get("status", ""),
                        "column": column, "value": "" if value is None else str(value), "class": cls,
                        "target_key_template": target_key_template(row["id"], column, value) if cls == "legacy_frontend_path"
                        else ""})
    return out


def summary(records: list[dict]) -> str:
    lines = [f"creators: {len({r['streamer_id'] for r in records})}  cells: {len(records)}"]
    by_class = Counter(r["class"] for r in records)
    lines += [f"  {cls}: {by_class[cls]}" for cls in ("legacy_frontend_path", "r2", "third_party", "empty", "unknown")]
    for column in COLUMNS:
        counts = Counter(r["class"] for r in records if r["column"] == column)
        lines.append(f"  {column}: " + ", ".join(f"{c}={n}" for c, n in sorted(counts.items())))
    by_status = Counter(r["status"] for r in records if r["class"] == "legacy_frontend_path")
    lines.append("  legacy paths by creator status: " + (", ".join(f"{s}={n}" for s, n in sorted(by_status.items())) or "none"))
    lines.append(f"  distinct legacy paths: {len({r['value'] for r in records if r['class'] == 'legacy_frontend_path'})}")
    for r in records:
        if r["class"] in ("third_party", "unknown"):  # named by id prefix only; the value stays in the CSV
            lines.append(f"  review: {r['class']} streamer {r['streamer_id'][:8]} {r['column']}")
    return "\n".join(lines)


def fetch_rows(dsn: str) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    # The role already defaults to read-only; the option holds even for another role.
    with psycopg.connect(dsn, options="-c default_transaction_read_only=on", row_factory=dict_row) as conn:
        return conn.execute(SELECT).fetchall()


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Read-only inventory of creator branding URLs (D-M2b).")
    p.add_argument("--r2-base", action="append", default=[], help="an R2 public base URL (repeatable; no default)")
    p.add_argument("--csv", required=True, help="output CSV path (holds full values: keep it outside the repository)")
    p.add_argument("--fixture", help="JSON list of streamer rows instead of the database (tests, CI)")
    args = p.parse_args(argv)
    if args.fixture:
        with open(args.fixture, encoding="utf-8") as f:
            rows = json.load(f)
    else:
        dsn = os.environ.get("HYPERCHAT_READONLY_DSN", "")
        if not dsn:
            print("HYPERCHAT_READONLY_DSN is not set", file=sys.stderr)
            return 2
        try:
            rows = fetch_rows(dsn)
        except Exception as exc:  # noqa: BLE001 - driver errors can echo the DSN; print the type only
            print(f"database read failed: {type(exc).__name__}", file=sys.stderr)
            return 2
    records = inventory(rows, tuple(args.r2_base))
    with open(args.csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(records)
    print(summary(records))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
