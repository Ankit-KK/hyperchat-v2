"""D1 phase 2 end to end: a bcrypt hash imported by database/etl/auth_hash_import.py (synthetic, GoTrue-style $2a$)
signs in through the real API on the migrated schema and is upgraded to Argon2id. No Supabase involved: the source
side is an in-memory synthetic account."""

import hashlib
import sys
import uuid
from pathlib import Path

import bcrypt
import psycopg

from app.auth import passwords

from tests.integration import DOMAIN, PASSWORD, PG_HOST, requires_db, target
from tests.integration import db as _db
from tests.integration import login as _login
from tests.integration import make_user as _user
from tests.integration import new_browser as _new_client

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "database"))
from etl import auth_hash_import as imp  # noqa: E402

pytestmark = requires_db


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_imported_supabase_hash_signs_in_and_is_upgraded(app):
    email = "imported-hash" + DOMAIN
    uid, legacy = _user(email, password=None), str(uuid.uuid4())
    with _db() as conn:
        conn.execute("UPDATE public.users SET legacy_supabase_user_id = %s WHERE id = %s", (legacy, uid))
    gotrue_hash = "$2a$" + bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()[4:]
    source = [imp.Source(legacy, _digest(email), _digest(email.lower()), gotrue_hash, True, False, False)]
    assert _login(app, email).status_code == 401  # no password yet

    name, port = target()
    with psycopg.connect(f"host={PG_HOST} port={port} dbname={name} user=hyperchat_app") as conn:
        p = imp.plan(source, imp.read_targets(conn))
        conn.rollback()
        assert p.counts["eligible"] == 1 and not p.blocked
        p.fingerprint = _digest("synthetic source snapshot")  # read_plan sets it from the real source
        run_id = str(uuid.uuid4())
        assert imp.apply(conn, p, run_id=run_id) == 1  # as hyperchat_app on the migrated schema (0017)

    assert _login(_new_client(app), email, "wrong-password").status_code == 401
    assert _login(_new_client(app), email).status_code == 200
    with _db() as conn:
        stored, verified = conn.execute("SELECT password_hash, email_verified_at FROM public.users WHERE id = %s",
                                        (uid,)).fetchone()
    assert stored.startswith("$argon2id$") and verified is None  # upgraded on sign-in; verification untouched
    with _db() as conn:  # the ledger still tells this run's hash from the upgraded one: a rollback leaves it alone
        recorded, now = conn.execute(
            "SELECT l.hash_sha256, encode(sha256(convert_to(u.password_hash, 'UTF8')), 'hex') FROM "
            "public.d1_hash_import_ledger l JOIN public.users u ON u.id = l.user_id WHERE l.run_id = %s",
            (run_id,)).fetchone()
    assert recorded == _digest(gotrue_hash) and now != recorded
    assert _login(_new_client(app), email).status_code == 200


def test_an_unexpectedly_expensive_bcrypt_hash_fails_sign_in_without_bcrypt_work(app, monkeypatch):
    """A hash above MAX_BCRYPT_COST (however it got into the table) is refused with the generic 401 after one dummy
    Argon2 check; bcrypt.checkpw is never called, so it cannot tie up the hashing threads."""
    cheap = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
    _user("expensive-hash" + DOMAIN, hashed="$2a$31$" + cheap[7:])
    calls = []
    monkeypatch.setattr(passwords.bcrypt, "checkpw", lambda *a: calls.append(1) or True)
    r = _login(_new_client(app), "expensive-hash" + DOMAIN)
    assert r.status_code == 401 and calls == []
    with _db() as conn:
        stored = conn.execute("SELECT password_hash FROM public.users WHERE email = %s",
                              ("expensive-hash" + DOMAIN,)).fetchone()[0]
    assert stored.startswith("$2a$31$")  # nothing rewritten
