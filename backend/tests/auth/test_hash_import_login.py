"""D1 phase 2 end to end: a bcrypt hash imported by database/etl/auth_hash_import.py (synthetic, GoTrue-style $2a$)
signs in through the real API on the migrated schema and is upgraded to Argon2id. No Supabase involved: the source
side is an in-memory synthetic account."""

import hashlib
import sys
import uuid
from pathlib import Path

import bcrypt
import psycopg

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
        assert imp.apply(conn, p) == 1

    assert _login(_new_client(app), email, "wrong-password").status_code == 401
    assert _login(_new_client(app), email).status_code == 200
    with _db() as conn:
        stored, verified = conn.execute("SELECT password_hash, email_verified_at FROM public.users WHERE id = %s",
                                        (uid,)).fetchone()
    assert stored.startswith("$argon2id$") and verified is None  # upgraded on sign-in; verification untouched
    assert _login(_new_client(app), email).status_code == 200
