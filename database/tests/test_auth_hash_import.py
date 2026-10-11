"""D1 phase 2: the Supabase password-hash importer. Synthetic fixtures only.

Unit tests always run. The PostgreSQL tests create two throwaway fixture databases (a fake Supabase auth.users and a
minimal public.users) on the CI / throwaway cluster only (HYPERCHAT_CI_DB_SETUP=1). An autouse guard refuses any
connection that is not to those local fixture databases, so no test can reach Supabase or a real HyperChat database.
"""

import hashlib
import logging
import os
import sys
import uuid
from pathlib import Path

import bcrypt
import psycopg
import pytest

from etl import auth_hash_import as imp
from etl.auth_hash_import import Refused, Source, Target, plan

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app.auth.passwords import BCRYPT_PREFIXES, hash_password, needs_rehash, verify_password  # noqa: E402

PASSWORD = "Synthetic-Pass-42"
D = "@authimport.invalid"


def _bcrypt(password: str = PASSWORD) -> str:
    return "$2a$" + bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()[4:]  # GoTrue writes $2a$


HASHES = {k: _bcrypt() for k in ("e1", "e2", "e3", "e4", "m1", "c1", "x1", "p1", "d1", "b1")}
IDS = {k: str(uuid.UUID(int=i)) for i, k in enumerate(
    ("e1", "e2", "e3", "e4", "m1", "c1", "o1", "x1", "x9", "p1", "n1", "n2", "d1", "b1", "s1"), 1)}
# key: (email, encrypted_password, email_confirmed, deleted, banned_until: "future" | "past" | None)
SOURCE = {
    "e1": ("e1" + D, HASHES["e1"], True, False, None),
    "e2": ("e2" + D, HASHES["e2"], False, False, None),
    "e3": ("e3" + D, HASHES["e3"], True, False, None),
    "e4": ("e4" + D, HASHES["e4"], True, False, "past"),  # an expired ban is not a ban
    "m1": ("m1" + D, HASHES["m1"], True, False, None),
    "c1": ("C1" + D, HASHES["c1"], True, False, None),  # case-only difference: still excluded
    "x1": ("shared" + D, HASHES["x1"], True, False, None),
    "x9": ("SHARED" + D, None, False, False, None),
    "p1": ("p1" + D, HASHES["p1"], True, False, None),
    "n1": ("n1" + D, None, True, False, None),
    "n2": ("n2" + D, "", True, False, None),
    "d1": ("d1" + D, HASHES["d1"], True, True, None),
    "b1": ("b1" + D, HASHES["b1"], True, False, "future"),
    "s1": ("s1" + D, _bcrypt(), True, False, None),  # no V2 account: never created
}
# key: (legacy id key or None, email, existing password_hash)
TARGET = {
    "e1": ("e1", "e1" + D, None), "e2": ("e2", "e2" + D, None), "e3": ("e3", "e3" + D, ""),
    "e4": ("e4", "e4" + D, None), "m1": ("m1", "other-m1" + D, None), "c1": ("c1", "c1" + D, None),
    "o1": ("o1", "orphan" + D, None), "x1": ("x1", "shared" + D, None), "p1": ("p1", "p1" + D, "$argon2id$existing"),
    "n1": ("n1", "n1" + D, None), "n2": ("n2", "n2" + D, None), "d1": ("d1", "d1" + D, None),
    "b1": ("b1", "b1" + D, None), "nl": (None, "no-legacy" + D, None),
}
ELIGIBLE = ("e1", "e2", "e3", "e4")
EXPECTED = {
    "source_accounts": 14, "target_accounts": 14, "target_with_legacy_id": 13, "target_without_legacy_id": 1,
    "eligible": 4, "eligible_email_confirmed": 3, "target_legacy_id_not_in_source": 1,
    "target_email_used_by_other_source_id": 1, "id_match_email_mismatch": 2, "source_deleted": 1, "source_banned": 1,
    "target_already_has_password": 1, "source_without_password": 2, "unsupported_hash_format": 0,
    "source_ids_not_in_target": 2, "duplicate_source_ids": 0, "duplicate_target_legacy_ids": 0,
}


def _d(v):
    return None if v is None else hashlib.sha256(v.encode()).hexdigest()


def _n(v):
    return None if v is None else _d(v.strip(" ").lower())


def _sources() -> list[Source]:
    return [Source(IDS[k], _d(e), _n(e), pw or None, conf, dele, ban == "future")
            for k, (e, pw, conf, dele, ban) in SOURCE.items()]


def _targets() -> list[Target]:
    return [Target(str(uuid.uuid5(uuid.NAMESPACE_DNS, k)), IDS[lk] if lk else None, _d(e), _n(e), bool(pw))
            for k, (lk, e, pw) in TARGET.items()]


# ── pure logic ──────────────────────────────────────────────────────────────


def test_plan_categories_and_only_exact_matches_are_eligible():
    p = plan(_sources(), _targets())
    assert p.counts == EXPECTED and not p.blocked
    assert sorted(u.password_hash for u in p.updates) == sorted(HASHES[k] for k in ELIGIBLE)


def test_duplicates_and_unsupported_formats_block():
    s = _sources()
    assert plan(s + [s[0]], _targets()).blocked == ["duplicate_source_ids"]
    t = _targets()
    assert plan(s, t + [Target(str(uuid.uuid4()), t[0].legacy_id, _d("z" + D), _n("z" + D), False)]).blocked == \
        ["duplicate_target_legacy_ids"]
    for bad in ("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA", "$2a$10$tooshort", "$2x$10$" + "a" * 53,
                "$2a$03$" + "a" * 53, "plaintext-password"):
        s2 = [x if x.id != IDS["e1"] else Source(x.id, x.email, x.email_norm, bad, True, False, False) for x in s]
        p = plan(s2, _targets())
        assert p.counts["unsupported_hash_format"] == 1 and p.blocked == ["unsupported_hash_format"]
        with pytest.raises(Refused, match="blocked"):
            imp.apply(None, p)


def test_accepted_formats_are_exactly_what_the_v2_verifier_supports():
    assert {"$2a$", "$2b$", "$2y$"} == set(BCRYPT_PREFIXES)
    for prefix in BCRYPT_PREFIXES:
        h = prefix + _bcrypt()[4:]
        assert imp.BCRYPT.fullmatch(h) and verify_password(PASSWORD, h) and not verify_password("wrong", h)


def test_hashes_never_appear_in_reprs_or_summaries():
    p = plan(_sources(), _targets())
    text = repr(p) + repr(p.updates) + repr(_sources()) + imp.render(p.counts, "PLAN")
    assert not any(h in text for h in HASHES.values()) and "$2a$" not in text
    assert not any(e in text for e, *_ in SOURCE.values() if e)
    with pytest.raises(AssertionError):
        imp.render({"leak": HASHES["e1"]}, "x")


def test_the_select_reads_no_metadata_and_the_update_touches_one_column():
    assert "raw_user_meta_data" not in imp.SOURCE_SQL and "confirmed_at IS" not in imp.SOURCE_SQL.replace(
        "email_confirmed_at IS", "")
    src = Path(imp.__file__).read_text(encoding="utf-8")
    assert src.count("UPDATE public.users SET") == 1 and "SET password_hash = %s WHERE" in src
    for word in ("INSERT ", "DELETE ", "TRUNCATE ", "ALTER ", "CREATE ", "DROP ", "COPY ", "open(", "json", "csv",
                 "logging", "tempfile"):
        assert word not in src, word


# ── PostgreSQL (throwaway fixture databases) ────────────────────────────────

SRC_DB, TGT_DB = "hyperchat_import_src_fixture", "hyperchat_import_tgt_fixture"
READER, WRITER, VIEWER = "import_src_reader", "import_tgt_writer", "import_tgt_viewer"
db_only = pytest.mark.skipif(os.environ.get("HYPERCHAT_CI_DB_SETUP") != "1",
                             reason="needs the throwaway CI PostgreSQL (HYPERCHAT_CI_DB_SETUP=1)")
_real_connect = psycopg.connect


@pytest.fixture(autouse=True)
def only_local_fixture_databases(monkeypatch):
    """No test can connect anywhere but the local fixture databases (and `postgres` to create them)."""
    def guarded(*args, **kwargs):
        host = kwargs.get("host") or os.environ.get("PGHOST", "")
        assert not args and kwargs.get("dbname") in (SRC_DB, TGT_DB, "postgres"), "fixture databases only"
        assert host.startswith("/") or host in ("localhost", "127.0.0.1", "::1", ""), "local server only"
        assert "sslrootcert" not in kwargs, "never a Supabase-style connection in tests"
        return _real_connect(*args, **kwargs)

    monkeypatch.setattr(psycopg, "connect", guarded)


def _admin(dbname: str = "postgres") -> psycopg.Connection:
    return psycopg.connect(dbname=dbname, user=os.environ.get("PGUSER", "postgres"), autocommit=True)


def _reset():
    with _admin(SRC_DB) as c:
        c.execute("TRUNCATE auth.users")
        for k, (e, pw, conf, dele, ban) in SOURCE.items():
            # confirmed_at / phone_confirmed_at are set for EVERY row (phone confirmation): they must be ignored.
            c.execute("INSERT INTO auth.users VALUES (%s, %s, %s, now(), now(), CASE WHEN %s THEN now() END, "
                      "CASE WHEN %s THEN now() END, CASE %s WHEN 'future' THEN now() + interval '1 day' "
                      "WHEN 'past' THEN now() - interval '1 day' END, jsonb_build_object('email', 'meta' || %s))",
                      (IDS[k], e, pw, conf, dele, ban, e or ""))
    with _admin(TGT_DB) as c:
        c.execute("TRUNCATE public.users")
        for k, (lk, e, pw) in TARGET.items():
            c.execute("INSERT INTO public.users (id, email, password_hash, legacy_supabase_user_id, role, streamer_id) "
                      "VALUES (%s, %s, %s, %s, 'streamer', gen_random_uuid())",
                      (str(uuid.uuid5(uuid.NAMESPACE_DNS, k)), e, pw, IDS[lk] if lk else None))


@pytest.fixture(scope="module")
def fixtures():
    if os.environ.get("HYPERCHAT_CI_DB_SETUP") != "1":
        pytest.skip("needs the throwaway CI PostgreSQL")
    with _admin() as c:
        for db in (SRC_DB, TGT_DB):
            c.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
            c.execute(f"CREATE DATABASE {db}")
        for role in (READER, WRITER, VIEWER):
            c.execute(f"DROP ROLE IF EXISTS {role}")
            c.execute(f"CREATE ROLE {role} LOGIN")
    try:
        with _admin(SRC_DB) as c:
            c.execute("CREATE SCHEMA auth")
            c.execute("CREATE TABLE auth.users (id uuid PRIMARY KEY, email varchar(255), encrypted_password "
                      "varchar(255), confirmed_at timestamptz, phone_confirmed_at timestamptz, email_confirmed_at "
                      "timestamptz, deleted_at timestamptz, banned_until timestamptz, raw_user_meta_data jsonb)")
            c.execute(f"GRANT USAGE ON SCHEMA auth TO {READER}")
            c.execute(f"GRANT SELECT (id, email, encrypted_password, email_confirmed_at, deleted_at, banned_until) "
                      f"ON auth.users TO {READER}")
        with _admin(TGT_DB) as c:
            c.execute("CREATE EXTENSION citext")
            c.execute("CREATE TABLE public.users (id uuid PRIMARY KEY, email citext NOT NULL UNIQUE, password_hash "
                      "text, legacy_supabase_user_id uuid UNIQUE, role text NOT NULL, streamer_id uuid, is_active "
                      "boolean NOT NULL DEFAULT true, email_verified_at timestamptz, password_changed_at timestamptz, "
                      "username text)")
            c.execute(f"GRANT SELECT ON public.users TO {WRITER}, {VIEWER}")
            c.execute(f"GRANT UPDATE (password_hash) ON public.users TO {WRITER}")
        yield
    finally:
        with _admin() as c:
            for db in (SRC_DB, TGT_DB):
                c.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
            for role in (READER, WRITER, VIEWER):
                c.execute(f"DROP ROLE IF EXISTS {role}")


@pytest.fixture
def loaded(fixtures):
    _reset()


def _src():
    return imp.open_readonly(dbname=SRC_DB, user=READER)


def _tgt(role: str = WRITER, read_only: bool = False):
    conn = psycopg.connect(dbname=TGT_DB, user=role)
    conn.read_only = read_only
    return conn


def _rows():
    with _admin(TGT_DB) as c:
        return {r[0]: r[1:] for r in c.execute("SELECT id::text, password_hash, email::text, legacy_supabase_user_id, "
                                               "role, streamer_id, is_active, email_verified_at, password_changed_at, "
                                               "username FROM public.users").fetchall()}


def _tid(k):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, k))


@db_only
def test_dry_run_plans_from_real_queries_and_writes_nothing(loaded):
    before = _rows()
    with _src() as s, _tgt(VIEWER, read_only=True) as t:
        p = imp.read_plan(s, t, False)
    assert p.counts == EXPECTED and _rows() == before


@db_only
def test_apply_writes_only_eligible_hashes_and_nothing_else(loaded):
    before = _rows()
    with _src() as s, _tgt() as t:
        p = imp.read_plan(s, t, False)
        assert imp.apply(t, p) == 4
    after = _rows()
    for k in TARGET:
        b, a = before[_tid(k)], after[_tid(k)]
        assert a[1:] == b[1:]  # every other column unchanged (role, ownership, activation, verification, ...)
        assert a[0] == (SOURCE[k][1] if k in ELIGIBLE else b[0])
    assert after[_tid("p1")][0] == "$argon2id$existing"  # never overwritten
    with _src() as s, _tgt() as t:  # a second run finds nothing left to do
        again = imp.read_plan(s, t, False)
    assert again.counts["eligible"] == 0 and again.counts["target_already_has_password"] == 5


@db_only
def test_imported_hash_signs_in_and_upgrades_to_argon2id(loaded):
    with _src() as s, _tgt() as t:
        imp.apply(t, imp.read_plan(s, t, False))
    stored = _rows()[_tid("e1")][0]
    assert stored.startswith("$2a$") and verify_password(PASSWORD, stored) and not verify_password("nope", stored)
    assert needs_rehash(stored)  # the login path then stores an Argon2id hash (end to end: backend auth tests)
    upgraded = hash_password(PASSWORD)
    assert upgraded.startswith("$argon2id$") and verify_password(PASSWORD, upgraded)


@db_only
def test_a_hash_set_after_the_plan_rolls_back_everything(loaded):
    before = _rows()
    with _src() as s, _tgt() as t:
        p = imp.read_plan(s, t, False)
        with _admin(TGT_DB) as other:  # e.g. the user resets their password in V2 meanwhile
            other.execute("UPDATE public.users SET password_hash = '$argon2id$user-reset' WHERE id = %s", (_tid("e2"),))
        with pytest.raises(Refused, match="changed since the plan"):
            imp.apply(t, p)
    after = _rows()
    assert after[_tid("e2")][0] == "$argon2id$user-reset"
    assert all(after[_tid(k)][0] == before[_tid(k)][0] for k in ELIGIBLE if k != "e2")


@db_only
def test_a_locked_row_times_out_and_writes_nothing(loaded, monkeypatch):
    monkeypatch.setattr(imp, "LOCK_TIMEOUT", "200ms")
    before = _rows()
    with _src() as s, _tgt() as t:
        p = imp.read_plan(s, t, False)
        holder = _admin(TGT_DB)
        holder.autocommit = False
        try:
            holder.execute("SELECT 1 FROM public.users WHERE id = %s FOR UPDATE", (_tid("e3"),))
            with pytest.raises(psycopg.errors.LockNotAvailable):
                imp.apply(t, p)
        finally:
            holder.rollback()
            holder.close()
    assert _rows() == before


@db_only
def test_a_failure_mid_write_rolls_back_every_row(loaded, monkeypatch):
    before, calls = _rows(), []
    real = imp._write

    def flaky(cur, u):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("simulated crash")
        return real(cur, u)

    monkeypatch.setattr(imp, "_write", flaky)
    with _src() as s, _tgt() as t:
        p = imp.read_plan(s, t, False)
        with pytest.raises(RuntimeError):
            imp.apply(t, p)
    assert len(calls) == 3 and _rows() == before


@db_only
def test_schema_and_privilege_problems_fail_closed(loaded):
    with _src() as s, _tgt(VIEWER) as t:
        p = imp.read_plan(s, t, False)
        with pytest.raises(Refused, match="UPDATE on public.users.password_hash"):
            imp.apply(t, p)
    with _admin(SRC_DB) as c:
        c.execute(f"REVOKE SELECT (email_confirmed_at) ON auth.users FROM {READER}")
    try:
        with _src() as s, _tgt() as t:
            with pytest.raises(Refused, match="no SELECT privilege on auth.users.email_confirmed_at"):
                imp.read_plan(s, t, False)
    finally:
        with _admin(SRC_DB) as c:
            c.execute(f"GRANT SELECT (email_confirmed_at) ON auth.users TO {READER}")
    with _src() as s:
        with pytest.raises(Refused, match="unexpected type"):
            imp.check_session(s, imp.SOURCE_TABLE, {**imp.SOURCE_COLUMNS, "email_confirmed_at": {"bool"}}, "source",
                              False)
        s.rollback()


@db_only
def test_source_session_is_read_only(loaded):
    with _src() as s:
        with pytest.raises((psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)):
            s.execute("UPDATE auth.users SET encrypted_password = NULL")
        s.rollback()


def _cli(monkeypatch, answers: list[str], target_db: str = TGT_DB, role: str = WRITER):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
    monkeypatch.setattr(imp, "source_params", lambda: {"dbname": SRC_DB, "user": READER})
    monkeypatch.setattr(imp, "target_params", lambda role_=None, **kw: {"dbname": target_db, "user": role})
    monkeypatch.setattr(imp, "connect_source", lambda params: imp.open_readonly(**params))  # fixtures have no TLS


@db_only
def test_cli_dry_run_by_default_and_apply_needs_the_typed_phrase(loaded, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    before = _rows()
    _cli(monkeypatch, [], role=VIEWER)
    assert imp.main([]) == 0 and _rows() == before
    _cli(monkeypatch, ["yes"])
    assert imp.main(["--apply"]) == 3 and _rows() == before  # wrong phrase: cancelled
    _cli(monkeypatch, [f"IMPORT 4 HASHES INTO {TGT_DB}"])
    assert imp.main(["--apply"]) == 0
    assert all(_rows()[_tid(k)][0] == SOURCE[k][1] for k in ELIGIBLE)
    out = capsys.readouterr()
    text = out.out + out.err + caplog.text
    assert "updated" in out.out and "dry run" in out.err
    for secret in [*HASHES.values(), *IDS.values(), *(e for e, *_ in SOURCE.values() if e), "$2a$"]:
        assert secret not in text


@db_only
def test_cli_refuses_apply_into_dev_and_blocked_plans(loaded, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(imp, "connect_source", lambda params: called.append(1))
    monkeypatch.setattr(imp, "source_params", lambda: {})
    monkeypatch.setattr(imp, "target_params", lambda **kw: {"dbname": "hyperchat_dev", "user": WRITER})
    assert imp.main(["--apply"]) == 2 and not called  # refused before any connection
    assert "allow-dev-target" in capsys.readouterr().err
    before = _rows()
    with _admin(SRC_DB) as c:
        c.execute("UPDATE auth.users SET encrypted_password = '$argon2id$v=19$unsupported' WHERE id = %s", (IDS["e1"],))
    _cli(monkeypatch, ["unused"])
    assert imp.main(["--apply"]) == 2 and _rows() == before
    assert "unsupported_hash_format" in capsys.readouterr().err


def test_there_is_no_export_option():
    for flag in ("--export", "--output", "--file", "--json"):
        with pytest.raises(SystemExit):
            imp.main([flag])
