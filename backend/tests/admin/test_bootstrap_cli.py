"""`python -m app.cli bootstrap-admin` against a scratch database (never hyperchat_dev).

The password comes from stdin here; the tests check it is never echoed to stdout/stderr.
"""

import io
import json

from app.cli import main
from tests.integration import DOMAIN, db, login, make_user, new_browser, requires_db

pytestmark = requires_db
PASSWORD = "Bootstrap-Pass-42"


def _run(monkeypatch, capsys, email, password=PASSWORD):
    monkeypatch.setattr("sys.stdin", io.StringIO(password + "\n"))
    code = main(["bootstrap-admin", "--email", email, "--password-stdin"])
    out = capsys.readouterr()
    assert password not in out.out and password not in out.err
    return code, out


def test_bootstrap_creates_admin_once(strict_scratch_db, app, monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, "first-admin" + DOMAIN)
    assert code == 0 and "Admin account ready" in out.out
    events = [json.loads(line) for line in out.out.splitlines() if line.startswith("{")]
    with db() as conn:
        uid = conn.execute("SELECT id::text FROM public.users WHERE email = %s", ("first-admin" + DOMAIN,)).fetchone()[0]
    assert [e["user_id"] for e in events if e.get("security_event") == "admin_bootstrapped"] == [uid]
    r = login(new_browser(app), "first-admin" + DOMAIN, PASSWORD)
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    code, out = _run(monkeypatch, capsys, "second-admin" + DOMAIN)
    assert code == 1 and "already exists" in out.err
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.users WHERE email = %s",
                            ("second-admin" + DOMAIN,)).fetchone()[0] == 0


def test_bootstrap_activates_imported_account_without_password(strict_scratch_db, app, monkeypatch, capsys):
    uid = make_user("imported-admin" + DOMAIN, role="streamer", password=None, is_active=False)
    code, _ = _run(monkeypatch, capsys, "IMPORTED-ADMIN" + DOMAIN)
    assert code == 0
    with db() as conn:
        assert conn.execute("SELECT role, is_active, password_hash LIKE '$argon2id$%%' FROM public.users WHERE id = %s",
                            (uid,)).fetchone() == ("admin", True, True)


def test_bootstrap_refuses_account_with_password_and_weak_passwords(strict_scratch_db, app, monkeypatch, capsys):
    make_user("has-password" + DOMAIN)
    code, out = _run(monkeypatch, capsys, "has-password" + DOMAIN)
    assert code == 1 and "already has a password" in out.err
    code, out = _run(monkeypatch, capsys, "weak" + DOMAIN, password="short")
    assert code == 2 and "at least" in out.err
    code, out = _run(monkeypatch, capsys, "not-an-email")
    assert code == 2
