"""D-M2b legacy asset inventory (deploy/tools/legacy_asset_inventory.py): pure unit tests, no database, no network.

The classifier runs on fixture rows; the CLI runs on a JSON fixture (`--fixture`) exactly as CI does."""

import csv
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
TOOL = REPO / "deploy/tools/legacy_asset_inventory.py"
R2 = ("https://media.example.test",)
ID_A = "0f1e2d3c-0000-4000-8000-000000000001"
ID_B = "7a6b5c4d-0000-4000-8000-000000000002"
ROWS = [
    {"id": ID_A, "streamer_slug": "alpha-creator", "status": "active",
     "brand_logo_url": "/assets/streamers/alpha-creator/logo.png",
     "background_url": "/assets/streamers/alpha-creator/background.jpg"},
    {"id": ID_B, "streamer_slug": "beta-creator", "status": "pending",
     "brand_logo_url": "https://pages.example.test/beta/profile.html", "background_url": None},
]


def load():
    spec = importlib.util.spec_from_file_location("legacy_asset_inventory", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = load()


@pytest.mark.parametrize("value, expected", [
    ("/assets/streamers/alpha-creator/logo.png", "legacy_frontend_path"),
    ("/assets/streamers/alpha-creator/background.webp", "legacy_frontend_path"),
    ("https://media.example.test/soundboards/alpha-creator/0f1e2d3c-0000-4000-8000-000000000001.png", "r2"),
    ("https://media.example.test/creator-assets/x/logo/abc.png", "r2"),
    ("https://pages.example.test/beta/profile.html", "third_party"),
    ("http://pages.example.test/beta.png", "third_party"),
    ("https://evil.example.test/assets/streamers/alpha-creator/logo.png", "third_party"),  # a URL, not a legacy path
    ("https://media.example.test.evil.example/x.png", "third_party"),  # base compared on `base/`, not as a prefix
    ("https://media.example.test", "third_party"),  # the bare base is not an object URL
    (None, "empty"), ("", "empty"), ("   ", "empty"),
    ("assets/streamers/alpha-creator/logo.png", "unknown"),  # no leading slash
    ("//media.example.test/x.png", "unknown"),  # protocol-relative
    ("/images/other.png", "unknown"),
    ("/assets/streamers/alpha-creator/logo", "unknown"),  # no extension
    ("/assets/streamers/../secret/logo.png", "unknown"),
])
def test_classifier(value, expected):
    assert tool.classify(value, R2) == expected


def test_without_a_base_no_url_is_r2():
    assert tool.classify("https://media.example.test/x.png", ()) == "third_party"


def test_inventory_proposes_a_content_addressed_key_per_creator_and_kind():
    records = tool.inventory(ROWS, R2)
    assert [r["class"] for r in records] == ["legacy_frontend_path", "legacy_frontend_path", "third_party", "empty"]
    assert records[0]["target_key_template"] == f"creator-assets/{ID_A}/logo/{{sha256}}.png"
    assert records[1]["target_key_template"] == f"creator-assets/{ID_A}/background/{{sha256}}.jpg"
    assert records[2]["target_key_template"] == "" and records[3]["value"] == ""


def test_summary_names_review_rows_by_id_prefix_only():
    text = tool.summary(tool.inventory(ROWS, R2))
    assert "legacy_frontend_path: 2" in text and "third_party: 1" in text and "empty: 1" in text
    assert "legacy paths by creator status: active=2" in text
    assert "review: third_party streamer 7a6b5c4d brand_logo_url" in text
    assert "pages.example.test" not in text and ID_B not in text


def test_cli_runs_on_a_fixture_without_a_database(tmp_path):
    fixture, out = tmp_path / "rows.json", tmp_path / "inventory.csv"
    fixture.write_text(json.dumps(ROWS), encoding="utf-8")
    r = subprocess.run([sys.executable, "-B", str(TOOL), "--fixture", str(fixture), "--r2-base", R2[0], "--csv", str(out)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "creators: 2  cells: 4" in r.stdout
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [tuple(rows[0].keys())] == [tool.FIELDS]
    assert rows[2]["value"] == "https://pages.example.test/beta/profile.html"  # full value only in the CSV


def test_cli_refuses_without_a_dsn(tmp_path, monkeypatch):
    monkeypatch.delenv("HYPERCHAT_READONLY_DSN", raising=False)
    r = subprocess.run([sys.executable, "-B", str(TOOL), "--csv", str(tmp_path / "x.csv")], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 2 and "HYPERCHAT_READONLY_DSN" in r.stderr
    assert not (tmp_path / "x.csv").exists()
