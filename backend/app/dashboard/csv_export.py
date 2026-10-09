"""CSV encoding for the donation export (pure, no I/O)."""

import csv
import io
from collections.abc import Iterable, Iterator
from datetime import datetime
from decimal import Decimal

from app.core.timezones import IST

COLUMNS = ("created_at", "donor_name", "amount", "currency", "amount_inr")
CHUNK_ROWS = 500
# Spreadsheet formula injection: a text cell starting with one of these is prefixed with "'".
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def neutralize(text: str) -> str:
    return "'" + text if text.startswith(_FORMULA_START) else text


def cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(IST).isoformat(timespec="seconds")  # e.g. 2026-10-06T08:15:30+05:30
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (int, float)):
        return str(value)
    return neutralize(str(value))


def encode(rows: Iterable[dict]) -> Iterator[str]:
    """The header line, then the rows in chunks (RFC 4180 quoting, CRLF line endings)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(COLUMNS)
    for count, row in enumerate(rows, 1):
        writer.writerow([cell(row[column]) for column in COLUMNS])
        if count % CHUNK_ROWS == 0:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
    yield buffer.getvalue()
