"""Pure input rules for the dashboard API (no I/O), so every rule is unit-testable."""

import uuid


def parse_uuid(value: str | None) -> str | None:
    """Canonical UUID text, or None when `value` is not a UUID (callers answer 404)."""
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None
