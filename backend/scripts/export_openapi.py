"""Write the API's OpenAPI document to frontend/openapi/openapi.json (PDP v2.0 review §6 G17).

The frontend's typed client is generated from that committed snapshot; tests/test_openapi_snapshot.py fails when it
is stale. Run from anywhere:  python backend/scripts/export_openapi.py
The app is built as the unit tests build it (no database, test environment, docs on); nothing is served or connected.
"""

import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SNAPSHOT = BACKEND.parent / "frontend" / "openapi" / "openapi.json"
ENV = {"HYPERCHAT_DB_ENABLED": "false", "HYPERCHAT_APP_ENV": "test", "HYPERCHAT_ENABLE_DOCS": "true"}


def render() -> str:
    """The snapshot text for the current code. The caller sets ENV and clears the settings cache."""
    from app.main import create_app

    return json.dumps(create_app().openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    sys.path.insert(0, str(BACKEND))
    os.environ.update(ENV)
    from app.core.config import get_settings

    get_settings.cache_clear()
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {SNAPSHOT.relative_to(BACKEND.parent).as_posix()}")


if __name__ == "__main__":
    main()
