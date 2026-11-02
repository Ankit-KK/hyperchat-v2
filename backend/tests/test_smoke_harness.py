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
    assert versions[-1] == "0029" and sp.KNOWN_HEADS[-1] == versions[-1]  # a new migration must be added here
    assert sp.head_check("0028", None)[0] and sp.head_check("0028", "0028")[0]  # DEV today
    assert sp.head_check("0029", None)[0] and sp.head_check("0029", "0029")[0]


@pytest.mark.parametrize("head", ["0030", "0016", "", "abcd", "0028 ", "ERROR:  relation does not exist"])
def test_unknown_heads_always_fail(head):
    assert sp.head_check(head, None)[0] is False and sp.head_check(head, "0028")[0] is False


def test_in_the_rehearsal_an_older_known_head_fails():
    assert sp.head_check("0019", None)[0] is True  # an ordinary run on the deployed 0019 release
    ok, label = sp.head_check("0019", "0028")
    assert ok is False and "expects 0028" in label


@pytest.mark.parametrize("value, expected", [(None, None), ("", None), ("0028", "0028"), ("0024", "0024")])
def test_the_expected_head_must_be_known(value, expected):
    assert sp.parse_expect_head(value) == expected


@pytest.mark.parametrize("value", ["0030", "28", "latest", "0028;"])
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
    {"HYPERCHAT_SMOKE_EXPECT_HEAD": "0030"}])
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
TAG = "authtest-smk"  # the harness run's creator-slug prefix (smoke_dev_backend.TAG)


def _seed(owner, app):
    """A synthetic admin (owner only, since 0024), a synthetic streamer and creator, and real-looking rows that the
    cleanup must never touch."""
    from app.auth.passwords import hash_password
    admin = owner.execute(sp.ADMIN_INSERT, (TAG + "-admin" + DOMAIN, hash_password("Smoke-pw-12345"))).fetchone()[0]
    owner.execute("INSERT INTO public.users (email, role) VALUES (%s, 'streamer'), (%s, 'admin')",
                  (TAG + "-owner" + DOMAIN, "keep-admin@realkeep.invalid"))
    owner.execute("INSERT INTO public.users (email, role) VALUES (%s, 'streamer')", ("keep-owner@realkeep.invalid",))
    owner.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, status, kyc_track) VALUES "
                  "('authtest-smkharness', 'Smoke', 'active', 'legacy'), ('realkeep-smkharness', 'Keep', 'active', "
                  "'legacy')")
    owner.execute("INSERT INTO public.user_signups (name, email, mobile_number) VALUES ('Smoke', %s, '+91 99999 00000')",
                  (TAG + "-signup" + DOMAIN,))
    for slug in ("authtest-smkharness", "realkeep-smkharness"):  # what admin suspend / reactivate leaves (0020, 0023)
        _audit(owner, slug)
    return admin


def _audit(owner, slug):
    owner.execute("INSERT INTO public.creator_status_events (streamer_id, action, from_status, to_status) "
                  "SELECT id, 'suspend', 'active', 'suspended' FROM public.streamers WHERE streamer_slug = %s", (slug,))


def _audits(owner, slug_like):
    return owner.execute("SELECT count(*) FROM public.creator_status_events e JOIN public.streamers s "
                         "ON s.id = e.streamer_id WHERE s.streamer_slug LIKE %s", (slug_like,)).fetchone()[0]


def _drop_synthetic(owner):
    """Test teardown only (the schema owner, this harness's own rows)."""
    owner.execute("DELETE FROM public.creator_status_events WHERE streamer_id IN "
                  "(SELECT id FROM public.streamers WHERE streamer_slug LIKE 'authtest-smk%%' "
                  "OR streamer_slug LIKE 'authtest-zzother%%')")
    owner.execute("DELETE FROM public.streamers WHERE streamer_slug LIKE 'authtest-smk%%' "
                  "OR streamer_slug LIKE 'authtest-zzother%%'")
    owner.execute("DELETE FROM public.user_signups WHERE email LIKE %s", ("%" + DOMAIN,))
    owner.execute("DELETE FROM public.users WHERE email::text LIKE %s", ("%" + DOMAIN,))


def _real_rows(owner):
    return owner.execute("SELECT (SELECT count(*) FROM public.users WHERE email::text LIKE '%%@realkeep.invalid'), "
                         "(SELECT count(*) FROM public.streamers WHERE streamer_slug = 'realkeep-smkharness')"
                         ).fetchone() + (_audits(owner, "realkeep-smkharness"),)


def _drop_real(owner):
    owner.execute("DELETE FROM public.creator_status_events WHERE streamer_id IN "
                  "(SELECT id FROM public.streamers WHERE streamer_slug = 'realkeep-smkharness')")
    owner.execute("DELETE FROM public.streamers WHERE streamer_slug = 'realkeep-smkharness'")
    owner.execute("DELETE FROM public.users WHERE email::text LIKE '%%@realkeep.invalid'")


@requires_db
def test_since_0024_the_runtime_role_cannot_create_or_delete_the_synthetic_admin(strict_scratch_db):
    with fixture_db() as owner, db() as app:
        try:
            with pytest.raises(psycopg.Error):
                app.execute(sp.ADMIN_INSERT, (TAG + "-direct" + DOMAIN, None))
            admin = owner.execute(sp.ADMIN_INSERT, (TAG + "-direct" + DOMAIN, None)).fetchone()[0]
            with pytest.raises(psycopg.Error):
                app.execute("DELETE FROM public.users WHERE id = %s", (admin,))
        finally:
            owner.execute("DELETE FROM public.users WHERE email::text LIKE %s", ("%" + DOMAIN,))


@pytest.mark.parametrize("role,streamer_id,owner", [
    ("admin", None, True), ("streamer", "a-creator-id", True), ("user", "a-creator-id", True),
    ("streamer", None, False), ("user", None, False)])
def test_only_admins_and_creator_linked_accounts_are_written_by_the_owner(role, streamer_id, owner):
    assert sp.owner_creates_user(role, streamer_id) is owner


def test_the_suite_creates_every_synthetic_user_through_owner_creates_user():
    src = SUITE.read_text(encoding="utf-8")
    body = src[src.index("def make_user("):src.index("def email(")]
    assert "with (fixture_db() if owner_creates_user(role, streamer_id) else db()) as c:" in body
    assert body.count("USER_INSERT") == 1 and "INSERT INTO public.users" not in body


@requires_db
def test_since_0027_a_creator_linked_account_is_written_by_the_owner_not_the_runtime_role(strict_scratch_db):
    """The E1 failure of 2026-10-24: the runtime role inserting a creator-linked account is refused by
    stepup_guard_users (no invitation accepted in this transaction); the schema owner writes the synthetic one, and the
    cleanup removes it. An account without a creator is still written by the runtime role."""
    with fixture_db() as owner, db() as app:
        try:
            creator = owner.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, status, kyc_track) "
                                    "VALUES ('authtest-smk0027', 'Smoke', 'active', 'legacy') RETURNING id::text"
                                    ).fetchone()[0]
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match="created only by an invitation"):
                app.execute(sp.USER_INSERT, (TAG + "-linked-app" + DOMAIN, None, "streamer", creator, True))
            linked = owner.execute(sp.USER_INSERT, (TAG + "-linked" + DOMAIN, None, "streamer", creator, True)).fetchone()[0]
            plain = app.execute(sp.USER_INSERT, (TAG + "-plain" + DOMAIN, None, "user", None, True)).fetchone()[0]
            assert owner.execute("SELECT count(*) FROM public.users WHERE id IN (%s, %s)", (linked, plain)).fetchone()[0] == 2
            left = sp.cleanup(app, owner, DOMAIN, datetime.now(UTC), tag=TAG)
            assert left["users"] == 0 and left["creators"] == 0, left
        finally:
            owner.execute("DELETE FROM public.users WHERE email::text LIKE %s", ("%" + DOMAIN,))
            owner.execute("DELETE FROM public.streamers WHERE streamer_slug = 'authtest-smk0027'")


@requires_db
def test_the_suites_other_runtime_role_fixture_writes_are_accepted_at_head_while_mfa_is_draft(strict_scratch_db):
    """The runtime-role fixture writes smoke_dev_backend.py makes besides users (same statements): with MFA
    enforcement draft, 0027's guards take their legacy path for them. If enforcement were active they would need
    grants, which the rehearsal never activates (the suite itself requires draft)."""
    with fixture_db() as owner, db() as app:
        try:
            assert owner.execute("SELECT state FROM public.stepup_enforcement").fetchone()[0] == "draft"
            creator = owner.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, status, kyc_track) "
                                    "VALUES ('authtest-smkfix', 'Smoke', 'active', 'legacy') RETURNING id::text"
                                    ).fetchone()[0]
            admin = owner.execute(sp.ADMIN_INSERT, (TAG + "-fix-admin" + DOMAIN, None)).fetchone()[0]
            old = app.execute(sp.USER_INSERT, (TAG + "-fix-old" + DOMAIN, None, "streamer", None, True)).fetchone()[0]
            app.execute("UPDATE public.users SET streamer_id = %s WHERE id = %s", (creator, old))  # D13 migrated shape
            app.execute("UPDATE public.streamers SET soundboard_enabled = true WHERE id = %s", (creator,))
            app.execute("UPDATE public.users SET email_verified_at = now() WHERE id = %s", (old,))  # D11
            app.execute("INSERT INTO public.user_invites (email, role, token_hash, invited_by, expires_at) VALUES "
                        "(%s, 'streamer', %s, %s, now() + interval '1 day')", (TAG + "-fix-inv" + DOMAIN, "a" * 64, admin))
            app.execute("INSERT INTO public.user_invites (email, token_hash, expires_at) VALUES "
                        "(%s, %s, now() + interval '1 day')", (TAG + "-fix-inv2" + DOMAIN, "b" * 64))
            app.execute("INSERT INTO public.partner_api_keys (partner_slug, key_hash, key_prefix) VALUES "
                        "('authtest-smkfix-partner', %s, 'hc_smkfix')", ("c" * 64,))
            app.execute("UPDATE public.partner_api_keys SET revoked = true WHERE key_hash = %s", ("c" * 64,))
        finally:
            owner.execute("DELETE FROM public.partner_api_keys WHERE key_hash = %s", ("c" * 64,))
            owner.execute("DELETE FROM public.user_invites WHERE email::text LIKE %s", ("%" + DOMAIN,))
            owner.execute("DELETE FROM public.users WHERE email::text LIKE %s", ("%" + DOMAIN,))
            owner.execute("DELETE FROM public.streamers WHERE streamer_slug = 'authtest-smkfix'")


@requires_db
def test_cleanup_removes_every_synthetic_row_and_nothing_real(strict_scratch_db):
    with fixture_db() as owner, db() as app:
        try:
            _seed(owner, app)
            left = sp.cleanup(app, owner, DOMAIN, datetime.now(UTC), tag=TAG)
            assert left["users"] == 0 and left["creators"] == 0 and left["signups"] == 0, left
            assert _real_rows(owner) == (2, 1, 1)  # the admin and the owner outside the domain, the other creator, its audit
            assert _audits(owner, "authtest-smk%") == 0
        finally:
            _drop_real(owner)
            _drop_synthetic(owner)


@requires_db
def test_a_failing_cleanup_statement_does_not_skip_the_rest_and_is_reported(strict_scratch_db):
    with fixture_db() as owner, db() as app:
        try:
            _seed(owner, app)
            with pytest.raises(RuntimeError) as exc:
                sp.cleanup(FailingConn(app, "DELETE FROM public.streamers"), owner, DOMAIN, datetime.now(UTC), tag=TAG)
            message = str(exc.value)
            assert "DELETE FROM public.streamers" in message and "'creators': 0" not in message  # reported, with leftovers
            with db() as check:
                users = check.execute("SELECT count(*) FROM public.users WHERE email::text LIKE %s",
                                      ("%" + DOMAIN,)).fetchone()[0]
            assert users == 0  # the users (incl. the synthetic admin) were still removed, through the owner
            assert _real_rows(owner) == (2, 1, 1)
        finally:
            _drop_real(owner)
            _drop_synthetic(owner)


@requires_db
def test_the_e1_stop_of_2026_10_24_the_status_audit_must_go_before_its_creator(strict_scratch_db):
    """The second E1 stop: without the status-audit step, deleting the audited synthetic creator hits
    creator_status_events_streamer_id_fkey (ON DELETE RESTRICT since 0023), the creators stay and the cleanup reports
    it. A later cleanup with the step then removes everything (safe after a partial teardown); the real audit stays."""
    with fixture_db() as owner, db() as app:
        try:
            _seed(owner, app)
            assert owner.execute("SELECT confdeltype FROM pg_constraint WHERE conname = "
                                 "'creator_status_events_streamer_id_fkey'").fetchone()[0] == "r"
            with pytest.raises(RuntimeError) as exc:
                sp.cleanup(app, FailingConn(owner, "DELETE FROM public.creator_status_events"), DOMAIN,
                           datetime.now(UTC), tag=TAG)
            assert "DELETE FROM public.streamers: ForeignKeyViolation" in str(exc.value)
            assert "'creators': 1" in str(exc.value) and _audits(owner, "authtest-smkharness") == 1
            left = sp.cleanup(app, owner, DOMAIN, datetime.now(UTC), tag=TAG)
            assert all(v == 0 for v in left.values()), left
            assert _audits(owner, "authtest-smk%") == 0 and _real_rows(owner) == (2, 1, 1)
        finally:
            _drop_real(owner)
            _drop_synthetic(owner)


@requires_db
def test_another_runs_audited_creator_and_its_audit_survive_this_runs_cleanup(strict_scratch_db):
    """Another run's creator WITH a status audit: neither the audit nor the creator (nor its other rows) is touched,
    and this run's cleanup succeeds."""
    with fixture_db() as owner, db() as app:
        try:
            _seed(owner, app)
            _seed_other_run(owner, app, audited=True)
            left = sp.cleanup(app, owner, DOMAIN, datetime.now(UTC), tag=TAG)
            assert all(v == 0 for v in left.values()), left
            assert _audits(owner, "authtest-zzother1") == 1 and _audits(owner, "authtest-smkharness") == 0
            assert _other_run_rows(owner) == (1, 1, 1, 1, 1, 1, 1, 1)
            assert _real_rows(owner) == (2, 1, 1)
        finally:
            _drop_real(owner)
            _drop_other_run(owner)
            _drop_synthetic(owner)


@requires_db
def test_this_runs_audited_creator_is_still_reported_if_its_audit_step_fails_with_another_run_present(
        strict_scratch_db):
    """Partial teardown with another run present: this run's creator stays (reported, non-zero), the other run's rows
    are untouched, and a second cleanup finishes this run only."""
    with fixture_db() as owner, db() as app:
        try:
            _seed(owner, app)
            _seed_other_run(owner, app, audited=True)
            with pytest.raises(RuntimeError, match="DELETE FROM public.streamers: ForeignKeyViolation"):
                sp.cleanup(app, FailingConn(owner, "DELETE FROM public.creator_status_events"), DOMAIN,
                           datetime.now(UTC), tag=TAG)
            left = sp.cleanup(app, owner, DOMAIN, datetime.now(UTC), tag=TAG)
            assert all(v == 0 for v in left.values()), left
            assert _other_run_rows(owner) == (1, 1, 1, 1, 1, 1, 1, 1) and _audits(owner, "authtest-zzother1") == 1
        finally:
            _drop_real(owner)
            _drop_other_run(owner)
            _drop_synthetic(owner)


def _seed_other_run(owner, app, audited: bool):
    """Another run's leftovers (tag authtest-zzother): a creator with a donation, moderator and OBS token, its owner
    account, an invitation, a signup and a partner key, all in the shared synthetic namespace."""
    other = owner.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, status, kyc_track) VALUES "
                          "('authtest-zzother1', 'Other run', 'active', 'legacy') RETURNING id::text").fetchone()[0]
    owner.execute("INSERT INTO public.users (email, role) VALUES ('authtest-zzother-owner@smokeharness.invalid', 'user')")
    owner.execute("INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, amount_inr, "
                  "payment_status, moderation_status) VALUES (%s, 'authtest-zzother-o1', 'Smoke Other', 10, 'INR', 10, "
                  "'success', 'pending')", (other,))
    owner.execute("INSERT INTO public.streamers_moderators (streamer_id, mod_name) VALUES (%s, 'Smoke Mod')", (other,))
    owner.execute("INSERT INTO public.obs_tokens (streamer_id, token_hash) VALUES (%s, %s)", (other, "e" * 64))
    app.execute("INSERT INTO public.user_invites (email, token_hash, expires_at) VALUES "
                "('authtest-zzother-inv@smokeharness.invalid', %s, now() + interval '1 day')", ("d" * 64,))
    owner.execute("INSERT INTO public.user_signups (name, email, mobile_number) VALUES ('Other', "
                  "'authtest-zzother-signup@smokeharness.invalid', '+91 99999 00001')")
    app.execute("INSERT INTO public.partner_api_keys (partner_slug, key_hash, key_prefix) VALUES "
                "('authtest-zzother-partner', %s, 'hc_zzother')", ("f" * 64,))
    if audited:
        _audit(owner, "authtest-zzother1")
    return other


def _other_run_rows(owner):
    return owner.execute(
        "SELECT (SELECT count(*) FROM public.streamers WHERE streamer_slug = 'authtest-zzother1'), "
        "(SELECT count(*) FROM public.donations WHERE order_id = 'authtest-zzother-o1'), "
        "(SELECT count(*) FROM public.streamers_moderators m JOIN public.streamers s ON s.id = m.streamer_id "
        " WHERE s.streamer_slug = 'authtest-zzother1'), "
        "(SELECT count(*) FROM public.obs_tokens WHERE token_hash = %s), "
        "(SELECT count(*) FROM public.users WHERE email = 'authtest-zzother-owner@smokeharness.invalid'), "
        "(SELECT count(*) FROM public.user_invites WHERE email = 'authtest-zzother-inv@smokeharness.invalid'), "
        "(SELECT count(*) FROM public.user_signups WHERE email = 'authtest-zzother-signup@smokeharness.invalid'), "
        "(SELECT count(*) FROM public.partner_api_keys WHERE partner_slug = 'authtest-zzother-partner')",
        ("e" * 64,)).fetchone()


def _drop_other_run(owner):
    owner.execute("DELETE FROM public.donations WHERE order_id = 'authtest-zzother-o1'")
    owner.execute("DELETE FROM public.user_invites WHERE email = 'authtest-zzother-inv@smokeharness.invalid'")
    owner.execute("DELETE FROM public.user_signups WHERE email = 'authtest-zzother-signup@smokeharness.invalid'")
    owner.execute("DELETE FROM public.partner_api_keys WHERE partner_slug = 'authtest-zzother-partner'")
    owner.execute("DELETE FROM public.users WHERE email = 'authtest-zzother-owner@smokeharness.invalid'")


@requires_db
def test_another_runs_unaudited_rows_all_survive_this_runs_cleanup(strict_scratch_db):
    """Review of PR #96: another run's creator WITHOUT a status audit (and its donation, moderator, OBS token, account,
    invitation, signup and partner key) is never deleted by this run's cleanup, which itself succeeds."""
    with fixture_db() as owner, db() as app:
        try:
            _seed(owner, app)
            _seed_other_run(owner, app, audited=False)
            left = sp.cleanup(app, owner, DOMAIN, datetime.now(UTC), tag=TAG)
            assert all(v == 0 for v in left.values()), left  # this run's rows only
            assert _other_run_rows(owner) == (1, 1, 1, 1, 1, 1, 1, 1)
            assert _audits(owner, "authtest-smk%") == 0 and _real_rows(owner) == (2, 1, 1)
        finally:
            _drop_real(owner)
            _drop_other_run(owner)
            _drop_synthetic(owner)


@requires_db
def test_cleanup_after_a_partial_setup_or_an_empty_run_succeeds(strict_scratch_db):
    with fixture_db() as owner, db() as app:
        try:
            left = sp.cleanup(app, owner, DOMAIN, datetime.now(UTC), tag=TAG)  # nothing was created
            assert all(v == 0 for v in left.values()), left
            owner.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, status, kyc_track) VALUES "
                          "('authtest-smkpartial', 'Partial', 'pending', 'legacy')")  # setup stopped after one creator
            left = sp.cleanup(app, owner, DOMAIN, datetime.now(UTC), tag=TAG)
            assert all(v == 0 for v in left.values()), left
        finally:
            _drop_synthetic(owner)


@pytest.mark.parametrize("tag", ["", "authtest-", "authtest-%", "authtest-ab", "authtest-smk%", "real-abcdef",
                                 "authtest-smk'x", "AUTHTEST-SMK"])
def test_cleanup_refuses_a_broad_or_malformed_run_tag_before_any_statement(tag):
    class Recorder:
        def __init__(self):
            self.sql = []

        def execute(self, sql, args=()):
            self.sql.append(sql)

    app, owner = Recorder(), Recorder()
    with pytest.raises(ValueError):
        sp.cleanup(app, owner, DOMAIN, datetime.now(UTC), tag=tag)
    assert app.sql == [] and owner.sql == []


def test_the_suite_passes_its_own_run_tag_to_the_cleanup():
    src = SUITE.read_text(encoding="utf-8")
    assert 'TAG = "authtest-sm" + secrets.token_hex(3)' in src
    assert "rate_limit_keys(EMAILS, SUITE_IP), tag=TAG)" in src
    assert 'f"{TAG}-{secrets.token_hex(6)}"' in src  # its orders carry the tag (order rate-limit keys too)


def test_no_smoke_statement_matches_the_whole_synthetic_namespace():
    """Every delete and leftover count is scoped to the run (tag / exact email), never 'authtest-%' or the bare
    domain, so another run's rows are never deleted."""
    import inspect
    import re
    sources = {"smoke_policy": inspect.getsource(sp)} | {
        p.name: p.read_text(encoding="utf-8") for p in sorted(SUITE.parent.glob("*.py"))}
    for name, src in sources.items():
        assert not re.search(r"authtest-%", src), name
        assert not re.search(r"LIKE '%%?@authtest\.invalid'", src), name
        assert "(d,)" not in src and "%(d)s" not in src, name


# The keys that refuse deleting a creator (anything but CASCADE / SET NULL), each with how the cleanup handles it.
BLOCKING_KEYS = {
    ("creator_status_events", "streamer_id"): "creator_status_events",  # deleted first, this run's creators only
    ("donations", "streamer_id"): "donations",  # deleted first
    ("user_signups", "streamer_id"): "user_signups",  # deleted first (an approved application's creator)
    ("kyc_grace_grants", "streamer_id"): None,  # the suite never grants, restricts or activates (KYC section is
    ("kyc_restrictions", "streamer_id"): None,  # read-only); a row here makes the delete fail and the suite FAIL
    ("kyc_enforcement_events", "streamer_id"): None,
}


@requires_db
def test_every_key_that_blocks_deleting_a_creator_is_known_and_cleaned_first(strict_scratch_db):
    with fixture_db() as owner:
        rows = owner.execute(
            "SELECT c.conrelid::regclass::text, a.attname FROM pg_constraint c JOIN pg_attribute a "
            "ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) WHERE c.contype = 'f' "
            "AND c.confrelid = 'public.streamers'::regclass AND c.confdeltype NOT IN ('c', 'n')").fetchall()
    assert {(t.removeprefix("public."), col) for t, col in rows} == set(BLOCKING_KEYS)
    import inspect
    src = inspect.getsource(sp.cleanup)
    creators = src.index('"DELETE FROM public.streamers')
    for table in filter(None, BLOCKING_KEYS.values()):
        assert -1 < src.index(f"DELETE FROM public.{table}") < creators, table
    suite = SUITE.read_text(encoding="utf-8")
    for table in ("kyc_grace_grants", "kyc_restrictions", "kyc_enforcement_events"):
        assert table not in suite
    assert "/restrict" not in suite and "/grace" not in suite



# ── schema-gated sections: "at or after" the feature's revision, never an exact head ──

VERSIONS = {p.name.split("_", 1)[0]: p.name for p in
            (Path(__file__).resolve().parents[2] / "database/migrations/versions").glob("[0-9]*_*.py")}


def test_each_gated_feature_names_the_migration_that_brought_it():
    assert VERSIONS[sp.FEATURE_HEADS["payments-account"]].startswith("0018_retire_secondary_razorpay")
    assert VERSIONS[sp.FEATURE_HEADS["owner-transfer"]].startswith("0019_owner_specific_soundboard_terms")
    assert VERSIONS[sp.FEATURE_HEADS["mfa"]].startswith("0025_stepup_core")


@pytest.mark.parametrize("section", sorted(sp.FEATURE_HEADS))
def test_a_gated_section_runs_at_its_revision_and_every_later_head(section):
    first = sp.KNOWN_HEADS.index(sp.FEATURE_HEADS[section])
    for i, head in enumerate(sp.KNOWN_HEADS):
        assert sp.head_at_least(head, sp.FEATURE_HEADS[section]) is (i >= first), head
    assert sp.head_at_least("0028", sp.FEATURE_HEADS[section]) is True
    assert sp.head_at_least("0030", sp.FEATURE_HEADS[section]) is False  # an unknown head never opens a gate


def _d6_section(head, strict, gate):
    """The suite's D6 branch, reported as the suite reports it."""
    r = sp.Results(strict=strict, out=lambda _line: None)
    if not gate(head):
        r.blocked("payments-account", "Razorpay secondary retired (D6)", "not deployed on DEV yet")
    else:
        r.check("payments-account", "D6 section executed", True)
    return r


def test_the_d6_section_executes_at_0028_in_the_rehearsal_instead_of_being_blocked():
    def gate(head):
        return sp.head_at_least(head, sp.FEATURE_HEADS["payments-account"])

    def old_gate(head):  # the previous rule
        return head in ("0018", "0019")

    assert _d6_section("0028", True, gate).counts() == (1, 0, 0)
    assert _d6_section("0028", True, old_gate).exit_code() == 1  # it would have failed the rehearsal


def test_the_suite_never_compares_heads_exactly_or_as_strings():
    import re
    src = SUITE.read_text(encoding="utf-8")
    assert not re.search(r"\bhead\s*(==|!=|<=|>=|<|>|in\s|not\s+in\s)", src)
    for section in sp.FEATURE_HEADS:
        assert f'FEATURE_HEADS["{section}"]' in src, section


def test_since_0023_the_suite_never_writes_a_creator_status_as_the_runtime_role():
    import re
    src = SUITE.read_text(encoding="utf-8")
    writes = list(re.finditer(r"UPDATE public\.streamers SET status", src))
    assert writes
    for m in writes:
        block = src[:m.start()].rsplit("with ", 1)[1]
        assert block.startswith("fixture_db()"), block[:40]


# ── rate limits: only this run's own counters are ever cleared ──────────────────

SUITE_IP, OTHER_IP = "198.51.100.10", "203.0.113.7"
REAL = "keep-real@realkeep.invalid"


def _sha(value):
    import hashlib
    return hashlib.sha256(value.lower().encode()).hexdigest()


def _seed_rate_limits(owner, app):
    """Synthetic users / creator / OBS token and this run's counters, plus counters of a real-looking account, another
    address, a real creator, token, order and partner. Returns (synthetic emails, synthetic pairs, unrelated pairs)."""
    syn_user = owner.execute("INSERT INTO public.users (email, role) VALUES (%s, 'streamer') RETURNING id::text",
                             (TAG + "-rl" + DOMAIN,)).fetchone()[0]
    real_user = owner.execute("INSERT INTO public.users (email, role) VALUES (%s, 'streamer') RETURNING id::text",
                              (REAL,)).fetchone()[0]
    syn_c, real_c = (owner.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, status, kyc_track) "
                                   "VALUES (%s, 'X', 'active', 'legacy') RETURNING id::text", (slug,)).fetchone()[0]
                     for slug in ("authtest-smkrl", "realkeep-smkrl"))
    owner.execute("INSERT INTO public.obs_tokens (streamer_id, token_hash) VALUES (%s, %s), (%s, %s)",
                  (syn_c, _sha("syn-token"), real_c, _sha("real-token")))
    emails = [TAG + "-rl" + DOMAIN, TAG + "-probe" + DOMAIN]
    synthetic = [(f"ip:{SUITE_IP}", "auth:login"), (f"ip:{SUITE_IP}", "auth:login-day"),
                 (f"acct:{_sha(emails[1])}:ip:{SUITE_IP}", "auth:login-account"),
                 (f"acct:{_sha(emails[0])}:ip:{SUITE_IP}", "auth:login-account-day"),
                 (f"email:{_sha(emails[0])}", "auth:reset-email"), (f"user:{syn_user}", "auth:password-change"),
                 (f"streamer:{syn_c}", "obs:token-create"), (f"obs:{_sha('syn-token')[:32]}", "obs:session"),
                 ("order:authtest-smkrl-1", "payment:poll"), ("partner:authtest-smkrl", "partner:check")]
    unrelated = [(f"ip:{OTHER_IP}", "auth:login"), (f"ip:{OTHER_IP}", "signup:create"),
                 (f"acct:{_sha(REAL)}:ip:{SUITE_IP}", "auth:login-account"),  # a real account, even from this host
                 (f"acct:{_sha(REAL)}:ip:{OTHER_IP}", "auth:login-account"),
                 (f"email:{_sha(REAL)}", "auth:reset-email"), (f"user:{real_user}", "auth:password-change"),
                 (f"admin:{real_user}", "kyc:refresh"), (f"streamer:{real_c}", "obs:token-create"),
                 (f"obs:{_sha('real-token')[:32]}", "obs:session"), ("order:realkeep-1", "payment:poll"),
                 ("partner:realkeep", "partner:check"),
                 ("order:authtest-zzother-1", "payment:poll"), ("partner:authtest-zzother", "partner:check")]  # another run
    for key, endpoint in synthetic + unrelated:
        app.execute("INSERT INTO public.rate_limits (ip_address, endpoint, request_count) VALUES (%s, %s, 3)",
                    (key, endpoint))
    return emails, synthetic, unrelated


def _present(app, pairs):
    rows = {(k, e) for k, e in app.execute("SELECT ip_address, endpoint FROM public.rate_limits").fetchall()}
    return rows & set(pairs)


def _drop_rate_limit_fixtures(owner, app, synthetic, unrelated):
    for key, endpoint in synthetic + unrelated:
        app.execute("DELETE FROM public.rate_limits WHERE ip_address = %s AND endpoint = %s", (key, endpoint))
    owner.execute("DELETE FROM public.streamers WHERE streamer_slug IN ('authtest-smkrl', 'realkeep-smkrl')")
    owner.execute("DELETE FROM public.users WHERE email::text IN (%s, %s)", (TAG + "-rl" + DOMAIN, REAL))


@requires_db
def test_setup_finds_this_hosts_key_and_resets_only_this_runs_sign_in_counters(strict_scratch_db):
    with fixture_db() as owner, db() as app:
        emails, synthetic, unrelated = _seed_rate_limits(owner, app)
        try:
            assert sp.suite_ip(app, emails[1]) == SUITE_IP  # from the run's own probe row
            assert sp.suite_ip(app, "never-used" + DOMAIN) is None
            sp.reset_login_counters(app, sp.rate_limit_keys(emails, SUITE_IP))
            assert _present(app, unrelated) == set(unrelated)
            login = [p for p in synthetic if p[1].startswith("auth:login")]
            assert not _present(app, login) and _present(app, synthetic) == set(synthetic) - set(login)
        finally:
            _drop_rate_limit_fixtures(owner, app, synthetic, unrelated)


@requires_db
def test_repeated_sign_in_resets_never_touch_other_counters(strict_scratch_db):
    with fixture_db() as owner, db() as app:
        emails, synthetic, unrelated = _seed_rate_limits(owner, app)
        try:
            for _ in range(5):  # the suite resets after every block of sign-ins
                app.execute("INSERT INTO public.rate_limits (ip_address, endpoint) VALUES (%s, 'auth:login') "
                            "ON CONFLICT DO NOTHING", (f"ip:{SUITE_IP}",))
                sp.reset_login_counters(app, sp.rate_limit_keys(emails, SUITE_IP))
                assert _present(app, unrelated) == set(unrelated)
            # without an identified host address no ip:* counter is ever cleared, only the synthetic accounts'
            app.execute("INSERT INTO public.rate_limits (ip_address, endpoint) VALUES (%s, 'auth:login')",
                        (f"ip:{SUITE_IP}",))
            sp.reset_login_counters(app, sp.rate_limit_keys(emails, None))
            assert (f"ip:{SUITE_IP}", "auth:login") in _present(app, synthetic)
            assert _present(app, unrelated) == set(unrelated)
        finally:
            _drop_rate_limit_fixtures(owner, app, synthetic, unrelated)


@requires_db
def test_cleanup_removes_this_runs_rate_limits_and_keeps_every_other_counter(strict_scratch_db):
    with fixture_db() as owner, db() as app:
        emails, synthetic, unrelated = _seed_rate_limits(owner, app)
        try:
            left = sp.cleanup(app, owner, DOMAIN, datetime.now(UTC), sp.rate_limit_keys(emails, SUITE_IP), tag=TAG)
            assert left["rate limits (this run)"] == 0 and not _present(app, synthetic)
            assert _present(app, unrelated) == set(unrelated)
        finally:
            _drop_rate_limit_fixtures(owner, app, synthetic, unrelated)


@requires_db
@pytest.mark.parametrize("fail_on", ["DELETE FROM public.streamers", "DELETE FROM public.rate_limits"])
def test_a_partial_cleanup_failure_keeps_every_other_counter(strict_scratch_db, fail_on):
    with fixture_db() as owner, db() as app:
        emails, synthetic, unrelated = _seed_rate_limits(owner, app)
        try:
            with pytest.raises(RuntimeError) as exc:
                sp.cleanup(FailingConn(app, fail_on), owner, DOMAIN, datetime.now(UTC),
                           sp.rate_limit_keys(emails, SUITE_IP), tag=TAG)
            assert fail_on in str(exc.value)
            assert _present(app, unrelated) == set(unrelated)
            if fail_on.endswith("rate_limits"):  # reported as left over, never deleted more broadly elsewhere
                assert "'rate limits (this run)': 0" not in str(exc.value)
            else:
                assert not _present(app, synthetic)
        finally:
            _drop_rate_limit_fixtures(owner, app, synthetic, unrelated)


def test_no_smoke_script_deletes_rate_limits_without_this_runs_keys():
    import re
    for script in sorted(SUITE.parent.glob("*.py")):
        src = script.read_text(encoding="utf-8")
        for m in re.finditer(r"DELETE FROM public\.rate_limits", src):
            statement = src[m.start():m.start() + 250]  # the statement and its scope, written on the next lines
            assert "ANY(%(exact)s)" in statement or "OWNED_RATE_LIMITS" in statement, (script.name, statement[:120])


# ── the DEV email and Google configuration (smoke_policy.config_values / email_mode / google_start_ok) ──────────
CID = "123456789012-abcdefghij0123456789.apps.googleusercontent.com"
REDIR = "https://api.quicktestspace.site/api/v1/auth/google/callback"
DEV_ENV = "\n".join([
    "HYPERCHAT_EMAIL_BACKEND=resend", "HYPERCHAT_EMAIL_ACTIVE_CATEGORIES=onboarding",
    "HYPERCHAT_BOT_RECIPIENT_ALLOWLIST=tg:1,dc:2",
    "HYPERCHAT_EMAIL_RECIPIENT_ALLOWLIST=a@example.invalid,B@example.invalid",
    f"HYPERCHAT_GOOGLE_CLIENT_ID={CID}", "HYPERCHAT_GOOGLE_CLIENT_SECRET=GOCSPX-never-read-value",
    f"HYPERCHAT_GOOGLE_REDIRECT_URI={REDIR}", "HYPERCHAT_EMAIL_ONBOARDING_RESEND_API_KEY=re_never_read"])
SYN = ["authtest-x1-invitee@authtest.invalid"]


def test_config_values_reads_only_the_listed_non_secret_names():
    cfg = sp.config_values(DEV_ENV)
    assert set(cfg) == set(sp.CONFIG_NAMES)
    assert "GOCSPX-never-read-value" not in repr(cfg) and "re_never_read" not in repr(cfg)
    assert cfg["HYPERCHAT_EMAIL_RECIPIENT_ALLOWLIST"] == "a@example.invalid,B@example.invalid"  # never the BOT_ one
    assert sp.config_values("HYPERCHAT_EMAIL_BACKEND=disabled\nHYPERCHAT_EMAIL_BACKEND=resend")["HYPERCHAT_EMAIL_BACKEND"] == "resend"


def test_email_mode_disabled_guarded_and_fail_closed():
    assert sp.email_mode({}, SYN) == "disabled"
    assert sp.email_mode({"HYPERCHAT_EMAIL_BACKEND": "disabled"}, SYN) == "disabled"
    assert sp.email_mode(sp.config_values(DEV_ENV), SYN) == "guarded"
    with pytest.raises(ValueError, match="allowlist"):  # a synthetic address deliverable: the suite must not run
        sp.email_mode({"HYPERCHAT_EMAIL_BACKEND": "resend", "HYPERCHAT_EMAIL_RECIPIENT_ALLOWLIST": "x@y.invalid," + SYN[0].upper()}, SYN)
    with pytest.raises(ValueError, match="unknown"):
        sp.email_mode({"HYPERCHAT_EMAIL_BACKEND": "smtp"}, SYN)


def _start(**over):
    q = {"client_id": CID, "redirect_uri": REDIR, "response_type": "code", "scope": "openid email profile",
         "state": "s" * 32, "nonce": "n" * 32, "code_challenge": "c" * 43, "code_challenge_method": "S256",
         "prompt": "select_account", **over}
    from urllib.parse import urlencode
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({k: v for k, v in q.items() if v is not None})


COOKIE = "__Host-hc_oauth=abc.def; HttpOnly; Max-Age=600; Path=/; SameSite=lax; Secure"


def test_google_start_expectation_follows_the_configuration():
    on, off = sp.config_values(DEV_ENV), {}
    assert sp.google_enabled(on) and not sp.google_enabled(off)
    assert sp.google_start_ok(404, "", "", off)[0]
    assert not sp.google_start_ok(302, _start(), COOKIE, off)[0]  # Google on although the file says off
    assert sp.google_start_ok(302, _start(), COOKIE, on)[0]
    assert not sp.google_start_ok(404, "", "", on)[0]  # configured but not answering


@pytest.mark.parametrize("location, cookie", [
    (_start(client_id="999-other.apps.googleusercontent.com"), COOKIE), (_start(redirect_uri="https://evil.example/cb"), COOKIE),
    (_start(code_challenge_method="plain"), COOKIE), (_start(code_challenge=None), COOKIE), (_start(nonce=None), COOKIE),
    (_start(state="short"), COOKIE), (_start(prompt=None), COOKIE), (_start(scope="openid"), COOKIE),
    (_start().replace("accounts.google.com", "accounts.google.com.evil.example"), COOKIE),
    (_start(), COOKIE.replace("; Secure", "")), (_start(), COOKIE.replace("; HttpOnly", "")),
    (_start(), COOKIE + "; Domain=quicktestspace.site"), (_start(), COOKIE.replace("SameSite=lax", "SameSite=none")),
    (_start(), COOKIE.replace("__Host-hc_oauth=abc.def", "hc_oauth=abc.def")), (_start(), "")])
def test_google_start_refuses_every_weakened_redirect_or_cookie(location, cookie):
    ok, detail = sp.google_start_ok(302, location, cookie, sp.config_values(DEV_ENV))
    assert not ok and CID not in detail and "abc.def" not in detail


def test_the_suites_assert_configuration_aware_email_and_google_behaviour():
    backend = SUITE.read_text(encoding="utf-8")
    auth = (SUITE.parent / "smoke_auth.py").read_text(encoding="utf-8")
    assert "Google sign-in disabled -> 404" not in auth and "google_start_ok(" in auth
    assert "smoke_policy.email_mode(" in backend and "invite_delivery_unconfirmed" in backend
    for src in (backend, auth):  # both suites prove that no email left the server during the run
        assert "no email left the server during the run" in src and "email_provider_contacts(" in src
        assert "'\"email sent\"' not in journal" not in src  # never back to the sent-only check
    # the check's verdict is the provider-contact count itself, not just a call next to it
    assert "not any(contacts.values())" in backend
    assert "not any(email_provider_contacts(journal).values())" in auth


def test_a_failed_provider_attempt_counts_as_an_email_that_left():
    from smoke_policy import email_provider_contacts
    line = lambda m: '{"level": "INFO", "message": "%s", "category": "onboarding"}' % m
    assert not any(email_provider_contacts(line("email refused: recipient not on the allowlist")).values())
    assert not any(email_provider_contacts(line("email refused: category inactive")).values())
    assert email_provider_contacts(line("email sent"))["email sent"] == 1
    assert email_provider_contacts(line("email send failed"))["email send failed"] == 1  # never hidden
    assert email_provider_contacts("") == {"email sent": 0, "email send failed": 0}


# ── invitation refused by the allowlist: generic public error, reason proved from the security log ──
ADMIN_ID, INVITE_ID = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
GENERIC_INVITE_ERROR = '{"error":"Invitation delivery could not be confirmed. Please retry."}'


def _log(msg, **fields):
    import json
    return json.dumps({"ts": "2026-10-27T18:00:00.000+00:00", "level": "WARNING", "logger": "x", "msg": msg, **fields})


def _refusal_journal(**override):
    lines = {
        "refused": _log("email refused: recipient not on the allowlist", category="onboarding", template="invite"),
        "unconfirmed": _log("invite_delivery_unconfirmed", security_event="invite_delivery_unconfirmed",
                            reason="EmailRecipientNotAllowed", user_id=ADMIN_ID, invite_id=INVITE_ID),
        "withdrawn": _log("invite_withdrawn", security_event="invite_withdrawn", user_id=ADMIN_ID, invite_id=INVITE_ID),
        "public": _log("auth_unavailable", security_event="auth_unavailable", reason="invite_delivery_unconfirmed",
                       status=502, path="/api/v1/auth/invites"),
        "noise": "uvicorn started (not json)",
    }
    lines.update(override)
    return "\n".join(v for v in lines.values() if v)


def test_invite_refusal_response_accepts_only_the_generic_502():
    from smoke_policy import invite_refusal_response_ok
    assert invite_refusal_response_ok(502, GENERIC_INVITE_ERROR)[0]
    bad = [
        (201, '{"invite_id": "x", "expires_at": "y"}'),  # the invitation was created
        (502, '{"error":"Invitation delivery could not be confirmed. Please retry.","reason":"invite_delivery_unconfirmed"}'),
        (502, '{"error":"Invitation delivery could not be confirmed. Please retry.","invite_id":"x"}'),
        (502, '{"error":"Something else"}'),
        (502, '{"error":"Invitation delivery could not be confirmed. Please retry. token=abc"}'),
        (502, '{"error":"Invitation delivery could not be confirmed. Please retry.","to":"authtest-sm1-invitee@authtest.invalid"}'),
        (502, "Bad Gateway"),  # a proxy error, not the API's refusal
        (500, GENERIC_INVITE_ERROR),
    ]
    for status, text in bad:
        ok, detail = invite_refusal_response_ok(status, text)
        assert not ok, (status, text)
        assert "authtest" not in detail and "token" not in detail  # the detail never echoes the body


def test_invite_refusal_reason_is_proved_from_the_security_log():
    from smoke_policy import invite_refusal_logged
    assert invite_refusal_logged(_refusal_journal(), ADMIN_ID, INVITE_ID)[0]
    wrong = {
        "no unconfirmed event": ({"unconfirmed": None}, ADMIN_ID, INVITE_ID),
        "provider was tried": ({"unconfirmed": _log("invite_delivery_unconfirmed", security_event="invite_delivery_unconfirmed",
                                                   reason="EmailDeliveryError", user_id=ADMIN_ID, invite_id=INVITE_ID)},
                               ADMIN_ID, INVITE_ID),
        "another invitation": ({}, ADMIN_ID, "33333333-3333-3333-3333-333333333333"),
        "another admin": ({}, "44444444-4444-4444-4444-444444444444", INVITE_ID),
        "unknown admin": ({}, None, INVITE_ID),
        "unknown invitation": ({}, ADMIN_ID, None),
        "two refusals": ({"extra": _log("invite_delivery_unconfirmed", security_event="invite_delivery_unconfirmed",
                                        reason="EmailRecipientNotAllowed", user_id=ADMIN_ID, invite_id="x")},
                         ADMIN_ID, INVITE_ID),
        "an invitation was created": ({"created": _log("invite_created", security_event="invite_created",
                                                       user_id=ADMIN_ID)}, ADMIN_ID, INVITE_ID),
        "not withdrawn": ({"withdrawn": None}, ADMIN_ID, INVITE_ID),
        "withdraw failed": ({"failed": _log("invite_withdraw_failed", security_event="invite_withdraw_failed",
                                            reason="not_open", user_id=ADMIN_ID, invite_id=INVITE_ID)},
                            ADMIN_ID, INVITE_ID),
        "no allowlist refusal line": ({"refused": None}, ADMIN_ID, INVITE_ID),
        "refusal of another template": ({"refused": _log("email refused: recipient not on the allowlist",
                                                         template="reset-password")}, ADMIN_ID, INVITE_ID),
        "public 502 not recorded": ({"public": None}, ADMIN_ID, INVITE_ID),
        "public 502 for another path": ({"public": _log("auth_unavailable", security_event="auth_unavailable",
                                                        reason="invite_delivery_unconfirmed", status=502,
                                                        path="/api/v1/admin/applications/x/invite")},
                                        ADMIN_ID, INVITE_ID),
    }
    for name, (override, admin, invite) in wrong.items():
        ok, detail = invite_refusal_logged(_refusal_journal(**override), admin, invite)
        assert not ok, name
        assert ADMIN_ID not in detail and INVITE_ID not in detail  # counts only


def test_the_backend_suite_checks_the_generic_response_and_the_logged_reason():
    backend = SUITE.read_text(encoding="utf-8")
    assert '"invite_delivery_unconfirmed" in r.text' not in backend  # the reason code is never in the response
    assert "smoke_policy.invite_refusal_response_ok(r.status_code, r.text)" in backend
    assert 'REFUSED_INVITE = sent[0].get("id") if len(sent) == 1 else None' in backend
    assert 'smoke_policy.invite_refusal_logged(journal, ids.get("admin"), REFUSED_INVITE)' in backend


# ── D-G (#117): goal_activated_at is server-owned; the suite only activates the goal and checks the server's stamp ──
_START = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
_NOW = datetime(2026, 11, 2, 15, 5, tzinfo=UTC)


@pytest.mark.parametrize("value, ok", [
    ("2026-11-02T15:02:00+00:00", True),             # stamped inside this run
    ("2026-11-02T14:59:00+00:00", True),             # inside the clock slack before the start
    ("2026-11-02T14:00:00+00:00", False),            # backdated (what a client could have sent before D-G)
    ("2026-11-02T16:00:00+00:00", False),            # in the future
    ("2026-11-02T15:02:00", False),                  # no time zone
    (None, False), ("", False), ("not a time", False),
])
def test_the_goal_activation_must_be_the_servers_stamp_within_this_run(value, ok):
    assert sp.goal_stamped_by_server(value, _START, _NOW)[0] is ok


def test_the_suite_never_sends_goal_activated_at_except_to_check_the_422_refusal():
    backend = SUITE.read_text(encoding="utf-8")
    writes = [line for line in backend.splitlines() if "goal_activated_at" in line and ".write(" in line]
    assert len(writes) == 1 and 'json={"goal_activated_at"' in writes[0]   # only the refusal probe sends it
    refusal = backend[backend.index(writes[0]):]
    assert 'r.status_code == 422' in refusal[:400]                       # ... and it must be refused
    assert "smoke_policy.goal_stamped_by_server(got.get(\"goal_activated_at\"), STARTED, datetime.now(UTC))" in backend
    activation = backend[backend.index('"goal_is_active": True, "goal_name": "Smoke goal"') - 200:][:400]
    assert "goal_activated_at" not in activation.split("check(S")[0]    # the activation PATCH carries no time
