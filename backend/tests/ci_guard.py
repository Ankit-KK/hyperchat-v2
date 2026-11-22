"""CI skip guard (audit T-8): fail when a database-backed test was SKIPPED for a missing database, so a misconfigured
integration job cannot pass green. Reads pytest --junitxml reports; fails closed on a missing, unreadable or empty one.

    python tests/ci_guard.py <junit.xml> [<junit.xml> ...]
"""

import re
import sys
import xml.etree.ElementTree as ET

# requires_db (tests/integration.py), the database suites' DSN / role gates, and their CI-PostgreSQL gate
DB_SKIP = re.compile(r"HYPERCHAT_TEST_\w+|requires_db|throwaway CI PostgreSQL")


def db_skips(path: str) -> list[str]:
    """Skipped test ids whose reason names a missing database. Raises ValueError on a report with no test cases."""
    cases = list(ET.parse(path).getroot().iter("testcase"))
    if not cases:
        raise ValueError(f"{path}: no test cases")
    found = []
    for case in cases:
        for skipped in case.iter("skipped"):
            if skipped.get("type") == "pytest.xfail":  # junit reports xfails as <skipped> too: not a skip
                continue
            reason = f"{skipped.get('message') or ''} {skipped.text or ''}"
            if DB_SKIP.search(reason):
                found.append(f"{case.get('classname')}::{case.get('name')}: {skipped.get('message')}")
    return found


def main(paths: list[str]) -> int:
    if not paths:
        print("usage: ci_guard.py <junit.xml> ...", file=sys.stderr)
        return 2
    bad = []
    for path in paths:
        try:
            bad += db_skips(path)
        except (OSError, ET.ParseError, ValueError) as exc:
            print(f"::error::skip guard cannot read {path}: {exc}")
            return 1
    for line in bad:
        print(f"::error::database test skipped: {line}")
    print(f"skip guard: {len(bad)} database-backed test(s) skipped in {len(paths)} report(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
