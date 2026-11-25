"""G17: frontend/openapi/openapi.json (the frontend's typed client is generated from it) matches the API's code."""

import difflib

import pytest

from scripts.export_openapi import ENV, SNAPSHOT, render

REGENERATE = "python backend/scripts/export_openapi.py"


@pytest.fixture
def rendered(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield render()
    get_settings.cache_clear()


def test_the_committed_openapi_snapshot_is_current(rendered):
    assert SNAPSHOT.is_file(), f"missing {SNAPSHOT}; run: {REGENERATE}"
    committed = SNAPSHOT.read_text(encoding="utf-8").replace("\r\n", "\n")
    if committed != rendered:
        diff = list(difflib.unified_diff(committed.splitlines(), rendered.splitlines(),
                                         "committed openapi.json", "generated from the code", lineterm="", n=2))
        shown = "\n".join(diff[:60]) + (f"\n... {len(diff) - 60} more diff lines" if len(diff) > 60 else "")
        pytest.fail(f"frontend/openapi/openapi.json is stale. Regenerate it with:\n  {REGENERATE}\n\n{shown}")


def test_the_snapshot_holds_the_routes_the_frontend_foundation_reads(rendered):
    import json

    paths = json.loads(rendered)["paths"]
    for path in ("/api/v1/public/config", "/api/v1/metrics/showcase", "/api/v1/public/streamers/{slug}",
                 "/api/v1/auth/session", "/api/v1/auth/login"):
        assert path in paths, path
