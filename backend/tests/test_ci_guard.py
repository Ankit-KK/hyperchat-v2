"""The CI skip guard (tests/ci_guard.py): database skips fail, other skips pass, unusable reports fail closed."""

from tests.ci_guard import main

CASE = '<testcase classname="tests.x" name="test_{n}">{body}</testcase>'


def report(tmp_path, *bodies) -> str:
    cases = "".join(CASE.format(n=i, body=b) for i, b in enumerate(bodies))
    path = tmp_path / "junit.xml"
    path.write_text(f'<testsuites><testsuite name="pytest">{cases}</testsuite></testsuites>', encoding="utf-8")
    return str(path)


def skip(message: str) -> str:
    return f'<skipped type="pytest.skip" message="{message}">tests/x.py:1: {message}</skipped>'


def test_database_skips_fail(tmp_path):
    for reason in ("HYPERCHAT_TEST_AUTH_DB not set", "HYPERCHAT_TEST_MIGRATION_DSN not set",
                   "needs the throwaway CI PostgreSQL (HYPERCHAT_CI_DB_SETUP=1)"):
        assert main([report(tmp_path, "", skip(reason))]) == 1, reason


def test_other_skips_and_passes_are_fine(tmp_path):
    assert main([report(tmp_path, "", skip("needs gpg"), skip("Unix sockets"))]) == 0
    xfail = '<skipped type="pytest.xfail" message="requires_db finding">x</skipped>'  # an xfail, not a skip
    assert main([report(tmp_path, "", xfail)]) == 0


def test_unusable_reports_fail_closed(tmp_path):
    assert main([str(tmp_path / "missing.xml")]) == 1
    assert main([report(tmp_path)]) == 1  # no test cases at all
    (tmp_path / "bad.xml").write_text("<testsuites", encoding="utf-8")
    assert main([str(tmp_path / "bad.xml")]) == 1
    assert main([]) == 2
