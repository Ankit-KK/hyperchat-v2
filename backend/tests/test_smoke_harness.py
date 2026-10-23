"""The DEV smoke suite's own rules (tests/e2e/smoke_policy.py): migration heads, the rehearsal's strict PASS / FAIL /
BLOCKED reporting, the admin MFA expectations without a verifier, production refusals, and the cleanup (fakes, and a
disposable database for the 0024 admin-fixture rule). Nothing here touches DEV or production."""

import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests" / "e2e"))
import smoke_policy as sp  # noqa: E402
from tests.integration import db, fixture_db, requires_db  # noqa: E402

GOOD_STATUS = {"enforcement": "draft", "aal": 1, "mfa_required": False, "verifier": "not_configured",
               "credentials": None}


# ── migration heads ─────────────────────────────────────────────────────────────

def test_every_merged_revision_is_a_known_head_and_0028_is_accepted():
    versions = sorted(p.name.split("_", 1)[0] for p in
                      (Path(__file__).resolve().parents[2] / "database/migrations/versions").glob("[0-9]*_*.py"))
    assert versions[-1] == "0028" and sp.KNOWN_HEADS[-1] == versions[-1]  # a new migration must be added here
    assert sp.head_check("0028", None)[0] and sp.head_check("0028", "0028")[0]


@pytest.mark.parametrize("head", ["0029", "0016", "", "abcd", "0028 ", "ERROR:  relation does not exist"])
def test_unknown_heads_always_fail(head):
    assert sp.head_check(head, None)[0] is False and sp.head_check(head, "0028")[0] is False


def test_in_the_rehearsal_an_older_known_head_fails():
    assert sp.head_check("0019", None)[0] is True  # an ordinary run on the deployed 0019 release
    ok, label = sp.head_check("0019", "0028")
    assert ok is False and "expects 0028" in label


@pytest.mark.parametrize("value, expected", [(None, None), ("", None), ("0028", "0028"), ("0024", "0024")])
def test_the_expected_head_must_be_known(value, expected):
    assert sp.parse_expect_head(value) == expected


@pytest.mark.parametrize("value", ["0029", "28", "latest", "0028;"])
def test_an_unknown_expected_head_is_refused(value):
    with pytest.raises(sp.SmokeError):
        sp.parse_expect_head(value)


# ── strict PASS / FAIL / BLOCKED ────────────────────────────────────────────────

def results(strict):
    lines = []
    return sp.Results(strict=strict, out=lines.append), lines


def test_in_the_rehearsal_a_core_check_can_never_be_blocked():
    r, lines = results(strict=True)
    r.check("core", "health", True)
    r.blocked("creator-lifecycle", "Creator lifecycle (KYC PR1)", "not deployed on DEV yet")
    assert r.counts() == (1, 1, 0) and r.exit_code() == 1
    assert lines[-1].startswith("FAIL") and "BLOCKED is not allowed in the rehearsal" in lines[-1]


def test_only_approved_external_integrations_stay_blocked():
    r, lines = results(strict=True)
    r.check("core", "health", True)
    r.blocked("blocked", "email delivery (reset, invites)", "Resend not configured (D8)", external=True)
    assert r.counts() == (1, 0, 1) and r.exit_code() == 0 and lines[-1].startswith("BLOCKED")


def test_outside_the_rehearsal_undeployed_features_stay_blocked_as_before():
    r, _ = results(strict=False)
    r.check("core", "health", True)
    r.blocked("kyc", "KYC enforcement (KYC PR4)", "not deployed on DEV yet")
    assert r.counts() == (1, 0, 1) and r.exit_code() == 0


def test_any_failure_or_an_empty_run_exits_nonzero():
    r, _ = results(strict=False)
    assert r.exit_code() == 1  # nothing ran: never a green result
    r.check("a", "ok", True)
    r.check("a", "bad", False, "boom")
    assert r.exit_code() == 1 and r.failed() == [("a", "bad", "FAIL", "boom")]


# ── MFA / KYC draft state and the verifier-less MFA contract ───────────────────

@pytest.mark.parametrize("kyc, mfa, ok", [("draft|0", "draft", True), ("active|1", "draft", False),
                                          ("draft|1", "draft", False), ("draft|0", "active", False),
                                          ("", "draft", False), ("draft|0", "ERROR: permission denied", False)])
def test_enforcement_must_be_draft_with_generation_0(kyc, mfa, ok):
    assert sp.enforcement_draft(kyc, mfa)[0] is ok


def test_the_admin_status_without_a_verifier_is_accepted():
    assert sp.mfa_status_ok(200, GOOD_STATUS)[0] is True
    assert sp.mfa_status_ok(200, {**GOOD_STATUS, "aal": 0})[0] is True


@pytest.mark.parametrize("status, change", [
    (200, {"enforcement": "active"}), (200, {"mfa_required": True}), (200, {"verifier": "ok"}),
    (200, {"verifier": "unavailable"}), (200, {"credentials": []}), (200, {"aal": 2}), (200, {"aal": True}),
    (200, {"aal": None}), (200, {"extra": 1}), (500, {}), (503, {}), (403, {}), (401, {})])
def test_a_wrong_admin_status_or_an_error_fails(status, change):
    assert sp.mfa_status_ok(status, {**GOOD_STATUS, **change})[0] is False
    assert sp.mfa_status_ok(200, "<html>error</html>")[0] is False


def test_a_ceremony_without_a_verifier_must_fail_closed_exactly():
    assert sp.ceremony_refused_safely(503, {"error": "Passkey verification is not configured",
                                            "reason": "stepup_not_configured"})[0] is True


@pytest.mark.parametrize("status, body", [
    (200, {"challenge_id": "x"}),                          # unexpected success: something answered a ceremony
    (201, {}), (500, {"error": "Internal Server Error"}), (502, {"reason": "stepup_not_configured"}),
    (503, {"reason": "stepup_unavailable"}),               # a configured but broken verifier is another state
    (503, {}), (403, {"reason": "stepup_not_configured"}), (503, "Service Unavailable")])
def test_unexpected_success_errors_or_other_reasons_fail(status, body):
    assert sp.ceremony_refused_safely(status, body)[0] is False


# ── never production ────────────────────────────────────────────────────────────

DEV_HEALTH = {"environment": "development"}


@pytest.mark.parametrize("db_name, api, health, local", [
    ("hyperchat", "https://api.quicktestspace.site", DEV_HEALTH, ""),
    ("hyperchat_restore_test", "https://api.quicktestspace.site", DEV_HEALTH, ""),
    ("hyperchat_dev", "https://api.hyperchat.space", DEV_HEALTH, ""),
    ("hyperchat_dev", "https://API.HYPERCHAT.SPACE:443/x", DEV_HEALTH, ""),
    ("hyperchat_dev", "https://hyperchat.space", DEV_HEALTH, ""),
    ("hyperchat_dev", "https://api.quicktestspace.site", DEV_HEALTH, "https://api.hyperchat.space"),
    ("hyperchat_dev", "https://api.quicktestspace.site", {"environment": "production"}, ""),
    ("hyperchat_dev", "https://api.quicktestspace.site", {}, "")])
def test_production_is_always_refused(db_name, api, health, local):
    assert sp.refusal(db_name, api, health, local)


def test_the_dev_environment_is_accepted():
    assert sp.refusal("hyperchat_dev", "https://api.quicktestspace.site", DEV_HEALTH, "http://127.0.0.1:8000") is None


SUITE = Path(__file__).resolve().parents[2] / "tests" / "e2e" / "smoke_dev_backend.py"


@pytest.mark.parametrize("env", [
    {"HYPERCHAT_SMOKE_API": "https://api.hyperchat.space"},
    {"HYPERCHAT_SMOKE_LOCAL": "https://api.hyperchat.space"},
    {"HYPERCHAT_SMOKE_DB": "hyperchat:5432"},
    {"HYPERCHAT_SMOKE_DB": "hyperchat_restore_test:5433"},
    {"HYPERCHAT_SMOKE_EXPECT_HEAD": "0029"}])
def test_the_suite_refuses_production_and_unknown_targets_before_any_request(env):
    """The real script, run as on the server: it must exit 2 (refused) before reading /health or touching a database
    (HYPERCHAT_SMOKE_LOCAL points at a closed port, so reaching the network would give another message)."""
    import os
    import subprocess
    run = subprocess.run([sys.executable, str(SUITE)], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "HYPERCHAT_SMOKE_LOCAL": "http://127.0.0.1:9", **env})
    assert run.returncode == 2 and "REFUSED" in run.stderr, run.stderr[-300:]
    assert "cannot read the local health endpoint" not in run.stderr


# ── fixtures and cleanup (disposable database at head) ─────────────────────────

class FailingConn:
    """A runtime-role connection whose one statement fails, to prove the rest of the cleanup still runs."""

    def __init__(self, conn, fail_on):
        self.conn, self.fail_on = conn, fail_on

    def execute(self, sql, args=()):
        if self.fail_on in sql:
            raise psycopg.errors.QueryCanceled("injected failure")
        return self.conn.execute(sql, args)


DOMAIN = "@smokeharness.invalid"


def _seed(owner, app):
    """A synthetic admin (owner only, since 0024), a synthetic streamer and creator, and real-looking rows that the
    cleanup must never touch."""
    from app.auth.passwords import hash_password
    admin = owner.execute(sp.ADMIN_INSERT, ("smk-admin" + DOMAIN, hash_password("Smoke-pw-12345"))).fetchone()[0]
    owner.execute("INSERT INTO public.users (email, role) VALUES (%s, 'streamer'), (%s, 'admin')",
                  ("smk-owner" + DOMAIN, "keep-admin@realkeep.invalid"))
    owner.execute("INSERT INTO public.users (email, role) VALUES (%s, 'streamer')", ("keep-owner@realkeep.invalid",))
    owner.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, status, kyc_track) VALUES "
                  "('authtest-smkharness', 'Smoke', 'active', 'legacy'), ('realkeep-smkharness', 'Keep', 'active', "
                  "'legacy')")
    owner.execute("INSERT INTO public.user_signups (name, email, mobile_number) VALUES ('Smoke', %s, '+91 99999 00000')",
                  ("smk-signup" + DOMAIN,))
    return admin


def _real_rows(owner):
    return owner.execute("SELECT (SELECT count(*) FROM public.users WHERE email::text LIKE '%%@realkeep.invalid'), "
                         "(SELECT count(*) FROM public.streamers WHERE streamer_slug = 'realkeep-smkharness')"
                         ).fetchone()


def _drop_real(owner):
    owner.execute("DELETE FROM public.streamers WHERE streamer_slug = 'realkeep-smkharness'")
    owner.execute("DELETE FROM public.users WHERE email::text LIKE '%%@realkeep.invalid'")


@requires_db
def test_since_0024_the_runtime_role_cannot_create_or_delete_the_synthetic_admin(strict_scratch_db):
    with fixture_db() as owner, db() as app:
        try:
            with pytest.raises(psycopg.Error):
                app.execute(sp.ADMIN_INSERT, ("smk-direct" + DOMAIN, None))
            admin = owner.execute(sp.ADMIN_INSERT, ("smk-direct" + DOMAIN, None)).fetchone()[0]
            with pytest.raises(psycopg.Error):
                app.execute("DELETE FROM public.users WHERE id = %s", (admin,))
        finally:
            owner.execute("DELETE FROM public.users WHERE email::text LIKE %s", ("%" + DOMAIN,))


@requires_db
def test_cleanup_removes_every_synthetic_row_and_nothing_real(strict_scratch_db):
    with fixture_db() as owner, db() as app:
        try:
            _seed(owner, app)
            left = sp.cleanup(app, owner, DOMAIN, datetime.now(UTC))
            assert left["users"] == 0 and left["creators"] == 0 and left["signups"] == 0, left
            assert _real_rows(owner) == (2, 1)  # the admin and the owner outside the domain, the other creator
        finally:
            _drop_real(owner)
            owner.execute("DELETE FROM public.users WHERE email::text LIKE %s", ("%" + DOMAIN,))
            owner.execute("DELETE FROM public.streamers WHERE streamer_slug = 'authtest-smkharness'")


@requires_db
def test_a_failing_cleanup_statement_does_not_skip_the_rest_and_is_reported(strict_scratch_db):
    with fixture_db() as owner, db() as app:
        try:
            _seed(owner, app)
            with pytest.raises(RuntimeError) as exc:
                sp.cleanup(FailingConn(app, "DELETE FROM public.streamers"), owner, DOMAIN, datetime.now(UTC))
            message = str(exc.value)
            assert "DELETE FROM public.streamers" in message and "'creators': 0" not in message  # reported, with leftovers
            with db() as check:
                users = check.execute("SELECT count(*) FROM public.users WHERE email::text LIKE %s",
                                      ("%" + DOMAIN,)).fetchone()[0]
            assert users == 0  # the users (incl. the synthetic admin) were still removed, through the owner
            assert _real_rows(owner) == (2, 1)
        finally:
            _drop_real(owner)
            owner.execute("DELETE FROM public.streamers WHERE streamer_slug = 'authtest-smkharness'")
            owner.execute("DELETE FROM public.users WHERE email::text LIKE %s", ("%" + DOMAIN,))
