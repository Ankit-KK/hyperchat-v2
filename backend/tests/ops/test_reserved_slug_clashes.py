"""R6 (PDP v2.0 addendum §2.1): the marketing site's names are reserved, and deploy/tools/reserved_slug_clashes.py
finds existing creators that hold one. Pure unit tests: no database, no network."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.admin.service import RESERVED_SLUGS, AdminService
from app.auth.service import AuthError

REPO = Path(__file__).resolve().parents[3]
TOOL = REPO / "deploy/tools/reserved_slug_clashes.py"
# Written out independently of RESERVED_SLUGS: the addendum's fifteen unreserved names, robots, and the marketing
# paths that were already reserved.
MARKETING = ("features", "creators", "company", "whats-new", "demo-signup", "viewer-terms", "shipping",
             "legal-archive", "images", "intro", "licenses", "mascot", "partners", "sculpture", "social", "robots",
             "support", "privacy-policy", "terms-and-conditions", "cancellation-refunds", "feature-showcase", "about",
             "contact", "assets", "sitemap")


def load():
    spec = importlib.util.spec_from_file_location("reserved_slug_clashes", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_marketing_name_is_reserved_and_the_earlier_entries_stay():
    assert set(MARKETING) <= RESERVED_SLUGS
    assert {"admin", "api", "auth", "dashboard", "obs", "www", "renew-hyperchat-brand-ambassador"} <= RESERVED_SLUGS
    assert all(s == s.lower() for s in RESERVED_SLUGS)


@pytest.mark.parametrize("slug", MARKETING)
def test_creating_or_approving_a_creator_with_a_marketing_name_is_refused(slug):
    """create_creator and application approval both validate through creator_fields."""
    svc = AdminService(None, payment_accounts=frozenset({"primary"}))
    for candidate in (slug, slug.upper(), f" {slug} "):
        with pytest.raises(AuthError) as err:
            svc.creator_fields({"slug": candidate, "name": "N"})
        assert err.value.reason == "reserved_slug", candidate


def test_the_clash_query_compares_every_status_case_insensitively_against_the_backend_list():
    sql, params = load().build_query()
    assert "lower(streamer_slug) = ANY(%s)" in sql and "status" not in sql  # pending/suspended clash too
    assert sql.lstrip().upper().startswith("SELECT") and params == (sorted(RESERVED_SLUGS),)
    assert load().build_query({"b", "a"})[1] == (["a", "b"],)


def test_the_report_names_slug_and_id_prefix_only():
    rows = [{"id": "0f1e2d3c-0000-4000-8000-000000000001", "streamer_slug": "features"}]
    assert load().report(rows) == ["reserved slug in use: features (id 0f1e2d3c)"]
    assert load().report([]) == []


def test_without_a_dsn_it_refuses_and_never_connects(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "HYPERCHAT_READONLY_DSN"}
    r = subprocess.run([sys.executable, "-B", str(TOOL)], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert r.returncode == 2 and "HYPERCHAT_READONLY_DSN is not set" in r.stderr
