"""D1 phase 2: the Supabase password-hash importer. Synthetic fixtures only.

Unit tests always run. The PostgreSQL tests create two throwaway fixture databases (a fake Supabase auth.users and a
minimal public.users) on the CI / throwaway cluster only (HYPERCHAT_CI_DB_SETUP=1). An autouse guard refuses any
connection that is not to those local fixture databases, so no test can reach Supabase or a real HyperChat database.
"""

import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import bcrypt
import psycopg
import pytest

from etl import auth_hash_import as imp
from etl.auth_hash_import import Refused, Source, Target, plan

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app.auth import bcrypt_format  # noqa: E402
from app.auth.passwords import (  # noqa: E402
    BCRYPT_PREFIXES,
    hash_password,
    needs_rehash,
    verify_password,
)

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
                "$2a$03$" + "a" * 53, "plaintext-password", "$2a$13$" + "a" * 53, "$2b$31$" + "a" * 53,
                "$2a$1x$" + "a" * 53, "$2a$10$" + "a" * 52 + "!", HASHES["e1"][:30] + "-" + HASHES["e1"][31:],
                HASHES["e1"][:6] + "#" + HASHES["e1"][7:], HASHES["e1"] + "a"):
        s2 = [x if x.id != IDS["e1"] else Source(x.id, x.email, x.email_norm, bad, True, False, False) for x in s]
        p = plan(s2, _targets())
        assert p.counts["unsupported_hash_format"] == 1 and p.blocked == ["unsupported_hash_format"]
        with pytest.raises(Refused, match="blocked"):
            imp.apply(None, p)


def test_cost_cap_matches_the_verifier_and_the_boundary_is_exact():
    assert bcrypt_format.MAX_BCRYPT_COST == 12
    assert imp.is_supported_bcrypt is bcrypt_format.is_supported_bcrypt  # one shared validator, no copy
    at_max = "$2a$" + bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=12)).decode()[4:]
    assert imp.is_supported_bcrypt(at_max) and verify_password(PASSWORD, at_max)  # cost 12: imported and verified
    above = at_max[:4] + "13" + at_max[6:]  # well-formed, one step too expensive
    assert not imp.is_supported_bcrypt(above) and not verify_password(PASSWORD, above)
    for cost in range(4, 13):
        assert imp.is_supported_bcrypt(at_max[:4] + f"{cost:02d}" + at_max[6:])
    for cost in (0, 1, 2, 3, *range(13, 100)):
        assert not imp.is_supported_bcrypt(at_max[:4] + f"{cost:02d}" + at_max[6:])


def test_accepted_formats_are_exactly_what_the_v2_verifier_supports():
    assert {"$2a$", "$2b$", "$2y$"} == set(BCRYPT_PREFIXES)
    for prefix in BCRYPT_PREFIXES:
        h = prefix + _bcrypt()[4:]
        assert imp.is_supported_bcrypt(h) and verify_password(PASSWORD, h) and not verify_password("wrong", h)


def test_hashes_never_appear_in_reprs_or_summaries():
    p = plan(_sources(), _targets())
    text = repr(p) + repr(p.updates) + repr(_sources()) + imp.render(p.counts, "PLAN")
    assert not any(h in text for h in HASHES.values()) and "$2a$" not in text
    assert not any(e in text for e, *_ in SOURCE.values() if e)
    with pytest.raises(ValueError):
        imp.render({"leak": HASHES["e1"]}, "x")


def test_a_plan_whose_counts_do_not_add_up_is_refused(monkeypatch):
    """The accounting invariant is a real check (security audit F13), so a miscounted plan can never be applied."""
    monkeypatch.setattr(imp, "EXCLUSIONS", tuple(e for e in imp.EXCLUSIONS if e != "source_without_password"))
    with pytest.raises(Refused, match="do not add up"):
        plan(_sources(), _targets())


@pytest.mark.parametrize("call", ["auth_hash_import.render({'leak': 'x'}, 'T')",
                                  "auth_readonly_report.render({'leak': 'x'})"])
def test_the_counts_only_guard_survives_python_optimisation(call):
    """Security audit F13: `python -O` strips assert statements; the output guards must still refuse a non-count."""
    code = ("import sys\nfrom etl import auth_hash_import, auth_readonly_report\n"
            f"try:\n    {call}\nexcept ValueError:\n    sys.exit(0)\nsys.exit(1)\n")
    run = subprocess.run([sys.executable, "-O", "-c", code], cwd=Path(__file__).resolve().parents[1],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-500:]


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
                      "timestamptz, deleted_at timestamptz, banned_until timestamptz, raw_user_meta_data jsonb, "
                      "last_sign_in_at timestamptz)")
            c.execute(f"GRANT USAGE ON SCHEMA auth TO {READER}")
            c.execute(f"GRANT SELECT (id, email, encrypted_password, email_confirmed_at, deleted_at, banned_until) "
                      f"ON auth.users TO {READER}")
            c.execute(f"GRANT MAINTAIN ON auth.users TO {READER}")  # may take the --apply source lock, not write
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
    _run_freeze_sql("unfreeze.sql")
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
    _run_freeze_sql("freeze.sql")
    _cli(monkeypatch, [], role=VIEWER)
    assert imp.main([]) == 0 and _rows() == before
    dry = capsys.readouterr()
    reviewed = _printed_fingerprint(dry.out)
    assert "dry run" in dry.err
    _cli(monkeypatch, ["yes"])
    assert imp.main(["--apply", "--expected-fingerprint", reviewed]) == 3 and _rows() == before  # wrong phrase
    _cli(monkeypatch, [f"IMPORT 4 HASHES INTO {TGT_DB}"])
    assert imp.main(["--apply", "--expected-fingerprint", reviewed]) == 0
    assert all(_rows()[_tid(k)][0] == SOURCE[k][1] for k in ELIGIBLE)
    out = capsys.readouterr()
    text = out.out + out.err + caplog.text
    assert "updated" in out.out
    for secret in [*HASHES.values(), *IDS.values(), *(e for e, *_ in SOURCE.values() if e), "$2a$"]:
        assert secret not in text


@db_only
def test_cli_refuses_apply_into_dev_and_blocked_plans(loaded, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(imp, "connect_source", lambda params: called.append(1))
    monkeypatch.setattr(imp, "source_params", lambda: {})
    monkeypatch.setattr(imp, "target_params", lambda **kw: {"dbname": "hyperchat_dev", "user": WRITER})
    assert imp.main(["--apply", "--expected-fingerprint", "0" * 64]) == 2 and not called  # before any connection
    assert "allow-dev-target" in capsys.readouterr().err
    before = _rows()
    too_expensive = HASHES["e1"][:4] + "13" + HASHES["e1"][6:]  # valid bcrypt shape, above the cost cap
    bad_body = HASHES["e1"][:30] + "-" + HASHES["e1"][31:]  # right length and cost, invalid salt/hash character
    for bad in ("$argon2id$v=19$unsupported", too_expensive, bad_body):
        with _admin(SRC_DB) as c:
            c.execute("UPDATE auth.users SET encrypted_password = %s WHERE id = %s", (bad, IDS["e1"]))
        _cli(monkeypatch, [])  # no answer available: reaching the confirmation prompt would fail the run (exit 1)
        assert imp.main(["--apply", "--expected-fingerprint", "0" * 64]) == 2 and _rows() == before  # blocked
        out = capsys.readouterr()
        assert "unsupported_hash_format" in out.err and bad not in out.out + out.err
        _cli(monkeypatch, [], role=VIEWER)
        assert imp.main([]) == 0 and _rows() == before  # the dry run still reports it
        assert "unsupported_hash_format" in capsys.readouterr().out


def test_a_summary_that_is_not_counts_only_fails_clearly_without_printing_it(monkeypatch, capsys):
    """Security audit F13: a non-count value in the plan is refused by the renderer; main() reports the error class
    (exit 1), not a misleading message, and never the value."""
    class Conn:
        read_only = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    secret = HASHES["e1"]
    monkeypatch.setattr(imp, "source_params", lambda: {})
    monkeypatch.setattr(imp, "target_params", lambda **kw: {"dbname": TGT_DB})
    monkeypatch.setattr(imp, "connect_source", lambda params: Conn())
    monkeypatch.setattr(imp.psycopg, "connect", lambda **kw: Conn())
    monkeypatch.setattr(imp, "read_plan", lambda *a: imp.Plan({"leak": secret}, []))
    assert imp.main([]) == 1
    out = capsys.readouterr()
    assert "failed: ValueError" in out.err and secret not in out.out + out.err


def test_there_is_no_export_option():
    for flag in ("--export", "--output", "--file", "--json"):
        with pytest.raises(SystemExit):
            imp.main([flag])


# ── D1 cutover safeguards: source freeze trigger, fingerprint, --apply guard ─

FREEZE_DIR = Path(__file__).resolve().parents[2] / "docs" / "operations" / "d1-auth-freeze"


def _run_freeze_sql(name: str, replace: tuple[str, str] | None = None, role: str | None = None) -> None:
    sql = (FREEZE_DIR / name).read_text(encoding="utf-8")
    if replace:
        sql = sql.replace(*replace)
    with (psycopg.connect(dbname=SRC_DB, user=role, autocommit=True) if role else _admin(SRC_DB)) as c:
        c.execute(sql)


def _tgenabled() -> str | None:
    with _admin(SRC_DB) as c:
        row = c.execute("SELECT tgenabled::text FROM pg_trigger WHERE tgname = 'hyperchat_d1_auth_freeze'").fetchone()
    return row and row[0]


def _printed_fingerprint(out: str) -> str:
    line = next(x for x in out.splitlines() if x.startswith("source_fingerprint"))
    value = line.split()[-1]
    assert len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)
    return value


def _reference_fingerprint(rows: list[tuple]) -> str:
    """An independent implementation of the documented serialization: rows of (id, email, password, deleted, banned)."""
    def field(v):
        return "-|" if v is None else f"{len(v)}:{v}|"
    body = "".join(field(i) + field(e) + field(pw or None) + ("d1|" if dele else "d0|") + ("b1" if ban else "b0") + "\n"
                   for i, e, pw, dele, ban in sorted(rows, key=lambda r: r[0]))
    return hashlib.sha256(f"{imp.FINGERPRINT_VERSION}\n{body}".encode()).hexdigest()


def _fixture_rows() -> list[tuple]:
    return [(IDS[k], e, pw, dele, ban == "future") for k, (e, pw, conf, dele, ban) in SOURCE.items()]


def _fingerprint_now() -> str:
    with _src() as s:
        try:
            return imp.source_fingerprint(s)
        finally:
            s.rollback()


def _freeze_state() -> str | None:
    with _src() as s:
        try:
            return imp.freeze_problem(s)
        finally:
            s.rollback()


def _safe(errors: list, fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - surfaced by the test
        errors.append(exc)


def _wait_for_lock(fragment: str) -> bool:
    deadline = time.monotonic() + 10
    with _admin(SRC_DB) as probe:
        while time.monotonic() < deadline:
            if probe.execute("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query LIKE %s",
                             (f"%{fragment}%",)).fetchone()[0]:
                return True
            time.sleep(0.01)  # probe interval only; the lock decides the outcome
    return False


def test_the_pinned_function_hash_is_the_body_in_freeze_sql():
    body = re.search(r"AS \$freeze\$(.*?)\$freeze\$", (FREEZE_DIR / "freeze.sql").read_text(encoding="utf-8"), re.S)
    assert hashlib.sha256(body.group(1).replace("\r", "").encode()).hexdigest() == imp.FREEZE_FUNCTION_SHA256


def test_the_serialization_is_unambiguous():
    # Naive concatenation of email and password gives "x||y" for both of the first two rows; length prefixes do not.
    one = "00000000-0000-0000-0000-000000000001"
    a = _reference_fingerprint([(one, "x|", "y", False, False)])
    b = _reference_fingerprint([(one, "x", "|y", False, False)])
    c = _reference_fingerprint([(one, None, "", False, False)])
    d = _reference_fingerprint([(one, "", None, False, False)])
    assert len({a, b, c, d}) == 4


@db_only
def test_the_database_fingerprint_matches_the_documented_serialization(loaded):
    assert _fingerprint_now() == _reference_fingerprint(_fixture_rows())
    assert _fingerprint_now() == _fingerprint_now()  # deterministic


@db_only
@pytest.mark.parametrize("change, sql", [
    ("email", "UPDATE auth.users SET email = 'changed@authimport.invalid' WHERE id = %(e1)s"),
    ("password", "UPDATE auth.users SET encrypted_password = %(other)s WHERE id = %(e1)s"),
    ("insertion", "INSERT INTO auth.users (id, email) VALUES ('00000000-0000-0000-0000-0000000000ff', 'n@x.invalid')"),
    ("deletion", "DELETE FROM auth.users WHERE id = %(e1)s"),
    ("deleted_at", "UPDATE auth.users SET deleted_at = now() WHERE id = %(e1)s"),
    ("ban", "UPDATE auth.users SET banned_until = now() + interval '1 day' WHERE id = %(e1)s"),
])
def test_every_change_that_matters_changes_the_fingerprint(loaded, change, sql):
    base = _fingerprint_now()
    with _admin(SRC_DB) as c:
        c.execute(sql, {"e1": IDS["e1"], "other": HASHES["e2"]})
    assert _fingerprint_now() != base, change


@db_only
def test_unrelated_columns_do_not_change_the_fingerprint(loaded):
    base = _fingerprint_now()
    with _admin(SRC_DB) as c:
        c.execute("UPDATE auth.users SET last_sign_in_at = now(), email_confirmed_at = now(), "
                  "raw_user_meta_data = '{}'::jsonb")
    assert _fingerprint_now() == base


@db_only
def test_freeze_check_requires_the_reviewed_trigger(loaded):
    assert "missing" in _freeze_state()
    _run_freeze_sql("freeze.sql")
    assert _freeze_state() is None
    _run_freeze_sql("freeze.sql")  # idempotent
    assert _freeze_state() is None
    with _admin(SRC_DB) as c:
        c.execute("ALTER TABLE auth.users DISABLE TRIGGER hyperchat_d1_auth_freeze")
    assert "not enabled" in _freeze_state() and "'D'" in _freeze_state()
    with _admin(SRC_DB) as c:
        c.execute("ALTER TABLE auth.users ENABLE REPLICA TRIGGER hyperchat_d1_auth_freeze")
    assert "'R'" in _freeze_state()
    with _admin(SRC_DB) as c:
        c.execute("ALTER TABLE auth.users ENABLE ALWAYS TRIGGER hyperchat_d1_auth_freeze")
    assert _freeze_state() is None  # 'A' counts as active


@db_only
def test_rerunning_the_freeze_keeps_enable_always(loaded):
    """Rehearsal finding: CREATE OR REPLACE TRIGGER resets ENABLE ALWAYS to plain enabled, so a second freeze.sql run
    silently reopened the replica-mode path. The script restores it; a disabled trigger is still re-armed."""
    _run_freeze_sql("freeze.sql")
    with _admin(SRC_DB) as c:
        c.execute("ALTER TABLE auth.users ENABLE ALWAYS TRIGGER hyperchat_d1_auth_freeze")
    _run_freeze_sql("freeze.sql")
    assert _tgenabled() == "A" and _freeze_state() is None
    base = _fingerprint_now()
    with _admin(SRC_DB) as c:
        c.execute("SET session_replication_role = replica")
        with pytest.raises(psycopg.errors.RaiseException, match="hyperchat_d1_freeze"):
            c.execute("UPDATE auth.users SET encrypted_password = %s WHERE id = %s", (HASHES["e2"], IDS["e1"]))
    assert _fingerprint_now() == base
    with _admin(SRC_DB) as c:
        c.execute("ALTER TABLE auth.users DISABLE TRIGGER hyperchat_d1_auth_freeze")
    _run_freeze_sql("freeze.sql")
    assert _tgenabled() == "O" and _freeze_state() is None


FREEZER = "import_src_freezer"


@db_only
def test_a_role_that_cannot_restore_enable_always_changes_nothing(loaded):
    """Like Supabase's dashboard role in the rehearsal (TRIGGER on auth.users, not its owner): it can install the
    freeze but not make it ALWAYS. If the owner did, the role's re-run fails and leaves the ALWAYS trigger and the
    function exactly as they were: never a silent downgrade."""
    _run_freeze_sql("unfreeze.sql")
    with _admin(SRC_DB) as c:
        c.execute("DROP SCHEMA IF EXISTS hyperchat_ops CASCADE")
    with _admin() as c:
        c.execute(f"DROP ROLE IF EXISTS {FREEZER}")
        c.execute(f"CREATE ROLE {FREEZER} LOGIN")
        c.execute(f"GRANT CREATE ON DATABASE {SRC_DB} TO {FREEZER}")
    try:
        with _admin(SRC_DB) as c:
            c.execute(f"GRANT USAGE ON SCHEMA auth TO {FREEZER}")
            c.execute(f"GRANT TRIGGER ON auth.users TO {FREEZER}")
        _run_freeze_sql("freeze.sql", role=FREEZER)
        _run_freeze_sql("freeze.sql", role=FREEZER)  # a plain re-run works for this role
        assert _tgenabled() == "O" and _freeze_state() is None
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="must be owner"):
            with psycopg.connect(dbname=SRC_DB, user=FREEZER, autocommit=True) as c:
                c.execute("ALTER TABLE auth.users ENABLE ALWAYS TRIGGER hyperchat_d1_auth_freeze")
        with _admin(SRC_DB) as c:  # the owner makes it ALWAYS
            c.execute("ALTER TABLE auth.users ENABLE ALWAYS TRIGGER hyperchat_d1_auth_freeze")
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="must be owner"):
            _run_freeze_sql("freeze.sql", role=FREEZER)
        assert _tgenabled() == "A" and _freeze_state() is None
    finally:
        _run_freeze_sql("unfreeze.sql")
        with _admin(SRC_DB) as c:
            c.execute("DROP SCHEMA IF EXISTS hyperchat_ops CASCADE")
            c.execute(f"REVOKE ALL ON auth.users FROM {FREEZER}")
            c.execute(f"REVOKE ALL ON SCHEMA auth FROM {FREEZER}")
        with _admin() as c:
            c.execute(f"REVOKE ALL ON DATABASE {SRC_DB} FROM {FREEZER}")
            c.execute(f"DROP ROLE {FREEZER}")


_GUARD = "EXECUTE FUNCTION hyperchat_ops.d1_auth_freeze_guard()"
_ON = "ON auth.users FOR EACH ROW"


@db_only
@pytest.mark.parametrize("sql, expected", [
    (f"CREATE OR REPLACE TRIGGER hyperchat_d1_auth_freeze BEFORE UPDATE {_ON} {_GUARD}", "malformed"),
    (f"CREATE OR REPLACE TRIGGER hyperchat_d1_auth_freeze BEFORE INSERT OR UPDATE OF email OR DELETE {_ON} {_GUARD}",
     "malformed"),
    (f"CREATE OR REPLACE TRIGGER hyperchat_d1_auth_freeze BEFORE INSERT OR UPDATE OR DELETE {_ON} WHEN (false) "
     f"{_GUARD}", "malformed"),
    (f"CREATE OR REPLACE TRIGGER hyperchat_d1_auth_freeze AFTER INSERT OR UPDATE OR DELETE {_ON} {_GUARD}",
     "malformed"),
    ("CREATE OR REPLACE TRIGGER hyperchat_d1_auth_freeze BEFORE INSERT OR UPDATE OR DELETE ON auth.users "
     f"FOR EACH STATEMENT {_GUARD}", "malformed"),
    ("CREATE FUNCTION hyperchat_ops.other_guard() RETURNS trigger LANGUAGE plpgsql AS $x$BEGIN RETURN NEW; END;$x$; "
     f"CREATE OR REPLACE TRIGGER hyperchat_d1_auth_freeze BEFORE INSERT OR UPDATE OR DELETE {_ON} "
     "EXECUTE FUNCTION hyperchat_ops.other_guard()", "calls hyperchat_ops.other_guard"),
    ("CREATE OR REPLACE FUNCTION hyperchat_ops.d1_auth_freeze_guard() RETURNS trigger LANGUAGE plpgsql AS "
     "$x$BEGIN RETURN NEW; END;$x$", "body differs"),
], ids=["update-only", "column-list", "when", "after", "statement", "other-function", "changed-body"])
def test_a_wrong_trigger_or_function_is_not_a_freeze(loaded, sql, expected):
    _run_freeze_sql("freeze.sql")
    try:
        with _admin(SRC_DB) as c:
            c.execute(sql)
        assert expected in (_freeze_state() or "")
    finally:
        with _admin(SRC_DB) as c:
            c.execute("DROP TRIGGER IF EXISTS hyperchat_d1_auth_freeze ON auth.users")
            c.execute("DROP FUNCTION IF EXISTS hyperchat_ops.other_guard()")


@db_only
@pytest.mark.parametrize("sql", [
    "UPDATE auth.users SET encrypted_password = %(other)s WHERE id = %(e1)s",
    "UPDATE auth.users SET email = 'other@authimport.invalid' WHERE id = %(e1)s",
    "UPDATE auth.users SET deleted_at = now() WHERE id = %(e1)s",
    "UPDATE auth.users SET banned_until = now() + interval '1 day' WHERE id = %(e1)s",
    "UPDATE auth.users SET id = '00000000-0000-0000-0000-0000000000fe' WHERE id = %(e1)s",
    "INSERT INTO auth.users (id, email) VALUES ('00000000-0000-0000-0000-0000000000fd', 'new@authimport.invalid')",
    "DELETE FROM auth.users WHERE id = %(e1)s",
], ids=["password", "email", "deleted_at", "banned_until", "id", "insert", "delete"])
def test_the_freeze_rejects_every_change_to_the_import_set(loaded, sql):
    """Plain PostgreSQL only: this proves the SQL, not GoTrue (that needs the Supabase rehearsal in the README)."""
    _run_freeze_sql("freeze.sql")
    base = _fingerprint_now()
    with _admin(SRC_DB) as c:
        with pytest.raises(psycopg.errors.RaiseException, match="hyperchat_d1_freeze"):
            c.execute(sql, {"e1": IDS["e1"], "other": HASHES["e2"]})
    assert _fingerprint_now() == base


@db_only
def test_the_freeze_lets_unrelated_updates_through_and_lifts_cleanly(loaded):
    _run_freeze_sql("freeze.sql")
    with _admin(SRC_DB) as c:
        assert c.execute("UPDATE auth.users SET last_sign_in_at = now(), email_confirmed_at = now() WHERE id = %s",
                         (IDS["e1"],)).rowcount == 1  # what an ordinary sign-in writes
    _run_freeze_sql("unfreeze.sql")
    _run_freeze_sql("unfreeze.sql")  # idempotent
    assert "missing" in _freeze_state()
    with _admin(SRC_DB) as c:
        assert c.execute("UPDATE auth.users SET encrypted_password = %s WHERE id = %s",
                         (HASHES["e2"], IDS["e1"])).rowcount == 1


@db_only
def test_installing_the_freeze_drains_an_in_flight_password_change(loaded):
    """A password change still open when freeze.sql runs: creating the trigger waits for it, so once the freeze is in
    place the change has landed, and a fingerprint taken afterwards includes it."""
    writer = _admin(SRC_DB)
    writer.autocommit = False
    writer.execute("UPDATE auth.users SET encrypted_password = %s WHERE id = %s", (HASHES["e2"], IDS["e1"]))
    errors: list = []
    installing = threading.Thread(target=_safe, args=(errors, lambda: _run_freeze_sql("freeze.sql")))
    try:
        installing.start()
        # pg_stat_activity shows the script's first 1 KB (track_activity_query_size), i.e. its header line
        assert _wait_for_lock("D1 auth freeze: PROPOSED")
        assert installing.is_alive()  # the freeze cannot be in place before the change commits
        writer.commit()
        installing.join(15)
        assert not installing.is_alive() and errors == []
    finally:
        writer.rollback()
        writer.close()
        installing.join(15)
    assert _freeze_state() is None
    rows = [(i, e, HASHES["e2"] if i == IDS["e1"] else pw, dele, ban) for i, e, pw, dele, ban in _fixture_rows()]
    assert _fingerprint_now() == _reference_fingerprint(rows)


@db_only
def test_the_freeze_gives_up_safely_when_it_cannot_drain_in_time(loaded):
    writer = _admin(SRC_DB)
    writer.autocommit = False
    writer.execute("UPDATE auth.users SET last_sign_in_at = now() WHERE id = %s", (IDS["e1"],))
    try:
        with pytest.raises(psycopg.errors.LockNotAvailable):
            _run_freeze_sql("freeze.sql", replace=("lock_timeout = '10s'", "lock_timeout = '200ms'"))
    finally:
        writer.rollback()
        writer.close()
    assert "missing" in _freeze_state()  # nothing half-installed


@db_only
def test_dry_run_prints_the_fingerprint_and_the_freeze_state(loaded, monkeypatch, capsys):
    before = _rows()
    _cli(monkeypatch, [], role=VIEWER)
    assert imp.main([]) == 0 and _rows() == before
    out = capsys.readouterr().out
    assert _printed_fingerprint(out) == _reference_fingerprint(_fixture_rows())
    assert "source_freeze       NOT ACTIVE: the trigger hyperchat_d1_auth_freeze is missing" in out
    _run_freeze_sql("freeze.sql")
    assert imp.main([]) == 0 and "source_freeze       active" in capsys.readouterr().out


def test_apply_without_a_fingerprint_is_refused_before_any_connection(monkeypatch, capsys):
    monkeypatch.setattr(imp, "source_params", lambda: pytest.fail("must not prompt or connect"))
    assert imp.main(["--apply"]) == 2
    assert "--expected-fingerprint" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        imp.main(["--apply", "--expected-fingerprint", "not-a-digest"])


@db_only
def test_apply_is_refused_without_the_freeze(loaded, monkeypatch, capsys):
    before = _rows()
    _cli(monkeypatch, [])  # no answers: reaching the confirmation prompt would fail the run
    assert imp.main(["--apply", "--expected-fingerprint", _fingerprint_now()]) == 2 and _rows() == before
    assert "the source freeze is not active: the trigger hyperchat_d1_auth_freeze is missing" in capsys.readouterr().err


@db_only
def test_apply_is_refused_when_the_freeze_is_disabled(loaded, monkeypatch, capsys):
    _run_freeze_sql("freeze.sql")
    reviewed = _fingerprint_now()
    with _admin(SRC_DB) as c:
        c.execute("ALTER TABLE auth.users DISABLE TRIGGER hyperchat_d1_auth_freeze")
    before = _rows()
    _cli(monkeypatch, [])
    assert imp.main(["--apply", "--expected-fingerprint", reviewed]) == 2 and _rows() == before
    assert "not enabled" in capsys.readouterr().err


@db_only
def test_apply_is_refused_when_the_source_changed_since_the_review(loaded, monkeypatch, capsys):
    """The dry run was reviewed, then a password changed (before the freeze went in): the reviewed fingerprint no
    longer describes the source, so --apply refuses even though the freeze is active now."""
    reviewed = _fingerprint_now()
    with _admin(SRC_DB) as c:
        c.execute("UPDATE auth.users SET encrypted_password = %s WHERE id = %s", (HASHES["e2"], IDS["e1"]))
    _run_freeze_sql("freeze.sql")
    before = _rows()
    _cli(monkeypatch, [])
    assert imp.main(["--apply", "--expected-fingerprint", reviewed]) == 2 and _rows() == before
    err = capsys.readouterr().err
    assert "fingerprint differs" in err and HASHES["e2"] not in err


@db_only
def test_the_fingerprint_and_the_imported_hashes_come_from_one_snapshot(loaded, monkeypatch):
    """A change committed between the fingerprint query and the hash read inside read_plan is invisible to that run:
    the plan's hashes are exactly the fingerprinted ones, so a matching fingerprint proves what will be written."""
    reviewed = _fingerprint_now()
    real = imp.read_sources

    def change_then_read(conn):
        with _admin(SRC_DB) as c:  # another session commits a new password for e1 right now
            c.execute("UPDATE auth.users SET encrypted_password = %s WHERE id = %s", (HASHES["e2"], IDS["e1"]))
        return real(conn)
    monkeypatch.setattr(imp, "read_sources", change_then_read)
    with _src() as s, _tgt(VIEWER, read_only=True) as t:
        p = imp.read_plan(s, t, False)
    assert p.fingerprint == reviewed
    e1 = next(u for u in p.updates if u.target_id == _tid("e1"))
    assert e1.password_hash == HASHES["e1"]  # the snapshot's value, not the one committed meanwhile
    assert _fingerprint_now() != reviewed  # a fresh run sees the change and would refuse


@db_only
def test_apply_with_the_freeze_and_the_reviewed_fingerprint_imports(loaded, monkeypatch, capsys):
    _run_freeze_sql("freeze.sql")
    _cli(monkeypatch, [], role=VIEWER)
    assert imp.main([]) == 0
    reviewed = _printed_fingerprint(capsys.readouterr().out)
    _cli(monkeypatch, [f"IMPORT 4 HASHES INTO {TGT_DB}"])
    assert imp.main(["--apply", "--expected-fingerprint", reviewed.upper()]) == 0  # case-insensitive input
    assert all(_rows()[_tid(k)][0] == SOURCE[k][1] for k in ELIGIBLE)


# ── row-level security (local GoTrue rehearsal finding) ─────────────────────
# Supabase enables row-level security on auth.users with no policies. A source role without BYPASSRLS read no rows
# and no error: the dry run planned nothing and printed the fingerprint of an empty set. A policy hiding only some rows
# is worse: hiding x9 (x1's email under another Supabase id) made x1 look eligible, which the exclusion policy forbids.

@pytest.fixture
def row_security(loaded):
    def enable(policy: str | None = None) -> None:
        with _admin(SRC_DB) as c:
            c.execute("ALTER TABLE auth.users ENABLE ROW LEVEL SECURITY")
            if policy:
                c.execute(f"CREATE POLICY import_test ON auth.users FOR SELECT TO {READER} USING ({policy})")
    yield enable
    with _admin(SRC_DB) as c:
        c.execute("DROP POLICY IF EXISTS import_test ON auth.users")
        c.execute("ALTER TABLE auth.users DISABLE ROW LEVEL SECURITY")
    with _admin(TGT_DB) as c:
        c.execute("ALTER TABLE public.users DISABLE ROW LEVEL SECURITY")
    with _admin() as c:
        c.execute(f"ALTER ROLE {READER} NOBYPASSRLS")


@db_only
def test_the_column_grant_source_role_passes_the_row_security_probe(loaded):
    """The importer's source role has column-level SELECT on auth.users only (database/README.md), as READER does in
    every test of this section. With RLS disabled the probe accepts it and every row is planned (review of #47, Codex
    P1: the probe selects a column check_columns has verified)."""
    with _admin(SRC_DB) as c:
        assert c.execute("SELECT has_table_privilege(%s, 'auth.users', 'SELECT'), has_column_privilege(%s, "
                         "'auth.users', 'encrypted_password', 'SELECT')", (READER, READER)).fetchone() == (False, True)
    with _src() as s, _tgt(VIEWER, read_only=True) as t:
        p = imp.read_plan(s, t, False)
    assert p.counts == EXPECTED and p.fingerprint == _reference_fingerprint(_fixture_rows())


@db_only
@pytest.mark.parametrize("policy, visible", [(None, 0), (f"id <> '{IDS['x9']}'", 13), ("true", 14)],
                         ids=["no-policy", "hides-x9", "shows-all"])
def test_row_security_on_the_source_is_refused_not_planned(row_security, policy, visible):
    row_security(policy)
    with _src() as s:  # without the guard: a filtered count, no error
        assert s.execute("SELECT count(*) FROM auth.users").fetchone()[0] == visible
        s.rollback()
    before = _rows()
    with _src() as s, _tgt(VIEWER, read_only=True) as t:
        with pytest.raises(Refused, match="^source: row-level security on auth.users would hide rows"):
            imp.read_plan(s, t, False)
    assert _rows() == before


@db_only
def test_cli_refuses_a_source_hidden_by_row_security_and_prints_nothing_sensitive(row_security, monkeypatch, capsys,
                                                                                  caplog):
    caplog.set_level(logging.DEBUG)
    _run_freeze_sql("freeze.sql")
    reviewed = _fingerprint_now()
    row_security()
    before = _rows()
    _cli(monkeypatch, [], role=VIEWER)
    assert imp.main([]) == 2  # the dry run
    _cli(monkeypatch, [])  # no answers: reaching the confirmation prompt would fail the run
    assert imp.main(["--apply", "--expected-fingerprint", reviewed]) == 2
    assert _rows() == before
    out = capsys.readouterr()
    assert out.err.count("refused: source: row-level security on auth.users would hide rows") == 2
    assert "source_fingerprint" not in out.out and "PLAN" not in out.out  # no counts or fingerprint of a filtered set
    text = out.out + out.err + caplog.text
    for secret in [*HASHES.values(), *IDS.values(), *(e for e, *_ in SOURCE.values() if e), "$2a$"]:
        assert secret not in text
    _assert_source_unlocked()


@db_only
def test_a_role_that_bypasses_row_security_plans_and_imports_every_row(row_security, monkeypatch, capsys):
    row_security()
    with _admin() as c:
        c.execute(f"ALTER ROLE {READER} BYPASSRLS")
    reviewed = _reviewed_and_frozen(monkeypatch, capsys)
    assert reviewed == _reference_fingerprint(_fixture_rows())  # every row, not a filtered set
    with _src() as s, _tgt(VIEWER, read_only=True) as t:
        assert imp.read_plan(s, t, False).counts == EXPECTED
    _cli(monkeypatch, [PHRASE])
    assert imp.main(["--apply", "--expected-fingerprint", reviewed]) == 0
    assert _imported_reviewed_hashes(_rows())
    _assert_source_unlocked()


@db_only
def test_row_security_enabled_while_the_operator_confirms_writes_nothing(row_security, monkeypatch, capsys):
    """Turned on after the plan (a privileged change during the prompt): the fresh transaction under the lock raises
    instead of reading a filtered set; nothing is written and the lock is released."""
    reviewed = _reviewed_and_frozen(monkeypatch, capsys)
    before = _rows()

    def enable_then_confirm(prompt=""):
        row_security()
        return PHRASE
    _cli(monkeypatch, [])
    monkeypatch.setattr("builtins.input", enable_then_confirm)
    assert imp.main(["--apply", "--expected-fingerprint", reviewed]) == 1
    assert "failed: InsufficientPrivilege (SQLSTATE 42501)" in capsys.readouterr().err
    assert _rows() == before
    _assert_source_unlocked()


@db_only
def test_row_security_on_the_target_is_refused_before_planning_and_before_writing(row_security):
    with _src() as s, _tgt() as t:
        p = imp.read_plan(s, t, False)
        before = _rows()
        with _admin(TGT_DB) as c:
            c.execute("ALTER TABLE public.users ENABLE ROW LEVEL SECURITY")
        with pytest.raises(Refused, match="^target: row-level security on public.users would hide rows"):
            imp.apply(t, p)
        with pytest.raises(Refused, match="^target: row-level security on public.users would hide rows"):
            imp.read_plan(s, t, False)
    assert _rows() == before


# ── privileged bypass and the source lock (post-#45 audit) ──────────────────
# A superuser (the fixture's admin role) can write with session_replication_role = replica, disable or drop the freeze
# trigger, or TRUNCATE (no row trigger fires). These tests do that at the real boundaries of an --apply run and assert
# the target database itself, not only exit codes or messages.

PHRASE = f"IMPORT 4 HASHES INTO {TGT_DB}"
BYPASSES = {
    "update-replica": ["SET session_replication_role = replica",
                       "UPDATE auth.users SET encrypted_password = %(other)s WHERE id = %(e1)s"],
    "truncate": ["TRUNCATE auth.users"],
    "disable-write-enable": ["ALTER TABLE auth.users DISABLE TRIGGER hyperchat_d1_auth_freeze",
                             "UPDATE auth.users SET encrypted_password = %(other)s WHERE id = %(e1)s",
                             "ALTER TABLE auth.users ENABLE TRIGGER hyperchat_d1_auth_freeze"],
    "drop-trigger": ["DROP TRIGGER hyperchat_d1_auth_freeze ON auth.users"],
}
BLOCKING_STATEMENT = {"update-replica": "UPDATE auth.users SET encrypted_password", "truncate": "TRUNCATE auth.users",
                      "disable-write-enable": "ALTER TABLE auth.users DISABLE TRIGGER",
                      "drop-trigger": "DROP TRIGGER hyperchat_d1_auth_freeze"}


def _run_bypass(name: str) -> None:
    params = {"e1": IDS["e1"], "other": HASHES["e2"]}
    with _admin(SRC_DB) as c:
        for sql in BYPASSES[name]:
            c.execute(sql, params if "%(" in sql else None)


def _source_after(name: str) -> bool:
    """Whether the bypass has taken effect on the source."""
    with _admin(SRC_DB) as c:
        if name == "truncate":
            return c.execute("SELECT count(*) FROM auth.users").fetchone()[0] == 0
        if name == "drop-trigger":
            return c.execute("SELECT count(*) FROM pg_trigger WHERE tgname = 'hyperchat_d1_auth_freeze'").fetchone()[0] == 0
        return c.execute("SELECT encrypted_password FROM auth.users WHERE id = %s", (IDS["e1"],)).fetchone()[0] \
            == HASHES["e2"]


def _share_locks_on_source(pid: int | None = None) -> int:
    with _admin(SRC_DB) as c:
        return c.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'relation' AND relation = 'auth.users'::regclass "
                         "AND mode = 'ShareLock' AND granted AND (%s::int IS NULL OR pid = %s::int)",
                         (pid, pid)).fetchone()[0]


def _assert_source_unlocked() -> None:
    """No session holds the importer's lock, and a sign-in style write goes through at once."""
    assert _share_locks_on_source() == 0
    with _admin(SRC_DB) as c:
        c.execute("SET lock_timeout = '1s'")
        assert c.execute("UPDATE auth.users SET last_sign_in_at = now() WHERE id = %s", (IDS["s1"],)).rowcount in (0, 1)


def _imported_reviewed_hashes(rows: dict) -> bool:
    return all(rows[_tid(k)][0] == SOURCE[k][1] for k in ELIGIBLE)


def _reviewed_and_frozen(monkeypatch, capsys) -> str:
    _run_freeze_sql("freeze.sql")
    _cli(monkeypatch, [], role=VIEWER)
    assert imp.main([]) == 0
    return _printed_fingerprint(capsys.readouterr().out)


def _capture_source_pid(monkeypatch, seen: dict) -> None:
    real = imp.lock_source

    def wrapper(src, expected, p):
        seen["pid"] = src.info.backend_pid
        return real(src, expected, p)
    monkeypatch.setattr(imp, "lock_source", wrapper)


@db_only
def test_the_plan_snapshot_alone_cannot_see_a_bypass_made_after_it(loaded):
    """The limitation behind this follow-up, with real transactions: a privileged change committed after the plan's
    snapshot is invisible to it; the plan still carries the old hash and still matches the reviewed fingerprint."""
    _run_freeze_sql("freeze.sql")
    reviewed = _fingerprint_now()
    with _src() as s, _tgt(VIEWER, read_only=True) as t:
        p = imp.read_plan(s, t, False)
    _run_bypass("disable-write-enable")
    assert p.fingerprint == reviewed and p.freeze_problem is None  # the snapshot still looks perfect
    assert next(u for u in p.updates if u.target_id == _tid("e1")).password_hash == HASHES["e1"]  # the old hash
    assert _fingerprint_now() != reviewed  # only a fresh look at the source shows the change


@db_only
@pytest.mark.parametrize("name, expected", [
    ("disable-write-enable", "no longer matches the reviewed fingerprint"),
    ("truncate", "no longer matches the reviewed fingerprint"),
    ("drop-trigger", "freeze is not active under the lock"),
])
def test_a_bypass_during_the_confirmation_prompt_is_caught_under_the_lock(loaded, monkeypatch, capsys, name, expected):
    """After the plan was read and checked, while the operator is at the prompt: the verification under the lock sees
    the change, nothing is written, and the lock is released."""
    reviewed = _reviewed_and_frozen(monkeypatch, capsys)
    before = _rows()
    _cli(monkeypatch, [])

    def operator_types_the_phrase(prompt=""):
        _run_bypass(name)
        return PHRASE
    monkeypatch.setattr("builtins.input", operator_types_the_phrase)
    assert imp.main(["--apply", "--expected-fingerprint", reviewed]) == 2
    assert _rows() == before  # the target database is untouched
    err = capsys.readouterr().err
    assert expected in err and "nothing was written" in err and HASHES["e2"] not in err
    _assert_source_unlocked()


@db_only
@pytest.mark.parametrize("name", list(BYPASSES))
def test_a_bypass_in_the_gap_before_the_target_commit_waits_for_it(loaded, monkeypatch, capsys, name):
    """The exact gap: the source was verified under the lock and the target rows are written but not yet committed.
    A privileged change fired now must block on the source lock (it cannot commit in the gap), so the import commits
    the verified, still-current hashes, and the change lands only after the commit and the lock release."""
    reviewed = _reviewed_and_frozen(monkeypatch, capsys)
    before = _rows()
    _cli(monkeypatch, [PHRASE])
    real = imp.require_source_lock
    bypass, state = None, {}

    def bypass_in_the_gap(src):
        nonlocal bypass
        errors: list = []
        bypass = threading.Thread(target=_safe, args=(errors, lambda: _run_bypass(name)))
        state["errors"] = errors
        bypass.start()
        state["blocked"] = _wait_for_lock(BLOCKING_STATEMENT[name])  # waiting for the importer's source lock
        # Nothing reached the source yet. A fresh read of auth.users would queue behind a waiting TRUNCATE (PostgreSQL's
        # lock queue) and so behind the importer itself; for TRUNCATE, "still waiting" below is the proof instead.
        state["unchanged_in_gap"] = name == "truncate" or not _source_after(name)
        state["target_uncommitted"] = _rows() == before  # we are inside the uncommitted target transaction
        state["still_blocked"] = _wait_for_lock(BLOCKING_STATEMENT[name])  # right before the target COMMIT
        return real(src)
    monkeypatch.setattr(imp, "require_source_lock", bypass_in_the_gap)
    try:
        assert imp.main(["--apply", "--expected-fingerprint", reviewed]) == 0
        after_import = _rows()
    finally:
        if bypass is not None:
            bypass.join(15)
    assert state == {"errors": [], "blocked": True, "unchanged_in_gap": True, "target_uncommitted": True,
                     "still_blocked": True}
    assert not bypass.is_alive()
    assert _imported_reviewed_hashes(after_import)  # committed the verified hashes
    assert {k: v for k, v in after_import.items() if k not in {_tid(e) for e in ELIGIBLE}} == \
        {k: v for k, v in before.items() if k not in {_tid(e) for e in ELIGIBLE}}  # nothing else touched
    assert _source_after(name)  # the privileged change landed, after the commit: the documented residual
    assert _share_locks_on_source() == 0


@db_only
def test_the_source_lock_is_held_from_verification_until_after_the_target_commit(loaded, monkeypatch, capsys):
    reviewed = _reviewed_and_frozen(monkeypatch, capsys)
    before = _rows()
    _cli(monkeypatch, [PHRASE])
    seen: dict = {}
    _capture_source_pid(monkeypatch, seen)
    held_during: list[str] = []
    real_write, real_require, real_release = imp._write, imp.require_source_lock, imp.release_source

    def write(cur, u):
        held_during.append("write" if _share_locks_on_source(seen["pid"]) == 1 else "write:NOT HELD")
        return real_write(cur, u)

    def require(src):
        held_during.append("pre-commit" if _share_locks_on_source(seen["pid"]) == 1 else "pre-commit:NOT HELD")
        return real_require(src)

    def release(src):
        committed = _imported_reviewed_hashes(_rows())  # visible from another session: the commit has happened
        held = _share_locks_on_source(seen["pid"]) == 1
        held_during.append("after-commit" if committed and held else f"after-commit:{committed}/{held}")
        real_release(src)
        held_during.append("released" if _share_locks_on_source(seen["pid"]) == 0 else "released:STILL HELD")
    monkeypatch.setattr(imp, "_write", write)
    monkeypatch.setattr(imp, "require_source_lock", require)
    monkeypatch.setattr(imp, "release_source", release)
    assert imp.main(["--apply", "--expected-fingerprint", reviewed]) == 0
    assert held_during == ["write"] * 4 + ["pre-commit", "after-commit", "released"]
    assert _imported_reviewed_hashes(_rows()) and _rows() != before


@db_only
def test_losing_the_source_session_before_the_pre_commit_check_writes_nothing(loaded, monkeypatch, capsys):
    """The source session ends while the target is written (a privileged pg_terminate_backend, an idle timeout, a
    network failure) and the source then changes: the pre-commit check fails, the target rolls back."""
    reviewed = _reviewed_and_frozen(monkeypatch, capsys)
    before = _rows()
    _cli(monkeypatch, [PHRASE])
    seen: dict = {}
    _capture_source_pid(monkeypatch, seen)
    real = imp.require_source_lock

    def kill_then_check(src):
        with _admin(SRC_DB) as c:
            c.execute("SELECT pg_terminate_backend(%s)", (seen["pid"],))
        _wait_until(lambda: _share_locks_on_source(seen["pid"]) == 0)
        _run_bypass("update-replica")  # now nothing stops it
        return real(src)
    monkeypatch.setattr(imp, "require_source_lock", kill_then_check)
    assert imp.main(["--apply", "--expected-fingerprint", reviewed]) == 2
    assert _rows() == before  # rolled back: the stale hash was not imported
    assert _source_after("update-replica")
    assert "source lock was lost before the target commit" in capsys.readouterr().err
    _assert_source_unlocked()


@db_only
def test_losing_the_source_session_right_after_the_pre_commit_check_is_reported_not_prevented(loaded, monkeypatch,
                                                                                               capsys):
    """The remaining window: the session ends after the pre-commit check, before the target COMMIT, and the source
    changes at once. The import commits (a read-only source cannot stop that); the post-commit check cannot prove the
    lock was held and alerts (exit 4). This is detection only: the target now holds a stale hash."""
    reviewed = _reviewed_and_frozen(monkeypatch, capsys)
    _cli(monkeypatch, [PHRASE])
    seen: dict = {}
    _capture_source_pid(monkeypatch, seen)
    real = imp.require_source_lock

    def check_then_kill(src):
        real(src)  # passes: the lock is held
        with _admin(SRC_DB) as c:
            c.execute("SELECT pg_terminate_backend(%s)", (seen["pid"],))
        _wait_until(lambda: _share_locks_on_source(seen["pid"]) == 0)
        _run_bypass("update-replica")
    monkeypatch.setattr(imp, "require_source_lock", check_then_kill)
    assert imp.main(["--apply", "--expected-fingerprint", reviewed]) == 4
    after = _rows()
    assert _imported_reviewed_hashes(after) and after[_tid("e1")][0] == HASHES["e1"]  # committed, now stale
    assert _source_after("update-replica")  # the source moved on
    err = capsys.readouterr().err
    assert "ALERT" in err and "could not be shown to have been held" in err and HASHES["e2"] not in err
    _assert_source_unlocked()


@db_only
def test_a_source_lock_that_cannot_be_taken_in_time_writes_nothing(loaded, monkeypatch, capsys):
    """A write to auth.users is still in flight (a sign-in): the lock wait is bounded, nothing is written, and the lock
    request does not linger after the timeout."""
    monkeypatch.setattr(imp, "SOURCE_LOCK_TIMEOUT", "300ms")
    reviewed = _reviewed_and_frozen(monkeypatch, capsys)
    before = _rows()
    _cli(monkeypatch, [PHRASE])
    writer = _admin(SRC_DB)
    writer.autocommit = False
    writer.execute("UPDATE auth.users SET last_sign_in_at = now() WHERE id = %s", (IDS["s1"],))  # not committed
    try:
        started = time.monotonic()
        assert imp.main(["--apply", "--expected-fingerprint", reviewed]) == 2
        assert time.monotonic() - started < 5
    finally:
        writer.commit()
        writer.close()
    assert _rows() == before
    assert "could not lock the source within 300ms" in capsys.readouterr().err
    _assert_source_unlocked()


@db_only
def test_a_source_role_that_may_not_lock_is_refused_before_any_write(loaded, monkeypatch, capsys):
    reviewed = _reviewed_and_frozen(monkeypatch, capsys)
    before = _rows()
    _cli(monkeypatch, [PHRASE])
    with _admin(SRC_DB) as c:
        c.execute(f"REVOKE MAINTAIN ON auth.users FROM {READER}")
    try:
        assert imp.main(["--apply", "--expected-fingerprint", reviewed]) == 2
    finally:
        with _admin(SRC_DB) as c:
            c.execute(f"GRANT MAINTAIN ON auth.users TO {READER}")
    assert _rows() == before
    assert "may not lock auth.users" in capsys.readouterr().err
    _assert_source_unlocked()


@db_only
def test_a_writable_source_role_needs_the_explicit_flag_and_still_reads_only(loaded, monkeypatch, capsys):
    """Before PostgreSQL 17 (no MAINTAIN), only update / delete / truncate allow the lock, so the role is writable: the
    importer refuses it unless --accept-writable-source-role, and its own transaction stays READ ONLY."""
    reviewed = _reviewed_and_frozen(monkeypatch, capsys)
    before = _rows()
    with _admin(SRC_DB) as c:
        c.execute(f"REVOKE MAINTAIN ON auth.users FROM {READER}")
        c.execute(f"GRANT UPDATE ON auth.users TO {READER}")
    try:
        _cli(monkeypatch, [PHRASE])
        assert imp.main(["--apply", "--expected-fingerprint", reviewed]) == 2 and _rows() == before
        assert "can write to auth.users" in capsys.readouterr().err
        seen: dict = {}
        real = imp.lock_source

        def check_read_only(src, expected, p):
            real(src, expected, p)
            seen["read_only"] = src.execute("SHOW transaction_read_only").fetchone()[0]
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                src.execute("UPDATE auth.users SET last_sign_in_at = now() WHERE id = %s", (IDS["s1"],))
        monkeypatch.setattr(imp, "lock_source", check_read_only)
        _cli(monkeypatch, [PHRASE])
        # the failed write above ended the locked transaction: the pre-commit check must see that and roll back
        assert imp.main(["--apply", "--accept-writable-source-role", "--expected-fingerprint", reviewed]) == 2
        assert seen["read_only"] == "on" and _rows() == before
        monkeypatch.setattr(imp, "lock_source", real)
        _cli(monkeypatch, [PHRASE])
        assert imp.main(["--apply", "--accept-writable-source-role", "--expected-fingerprint", reviewed]) == 0
        assert _imported_reviewed_hashes(_rows())
    finally:
        with _admin(SRC_DB) as c:
            c.execute(f"REVOKE UPDATE ON auth.users FROM {READER}")
            c.execute(f"GRANT MAINTAIN ON auth.users TO {READER}")
    _assert_source_unlocked()


@db_only
def test_planned_hashes_are_checked_against_the_locked_source(loaded):
    """Even with a matching fingerprint, a plan carrying a hash the locked source does not have is refused."""
    _run_freeze_sql("freeze.sql")
    with _src() as s, _tgt(VIEWER, read_only=True) as t:
        p = imp.read_plan(s, t, False)
        e1 = next(u for u in p.updates if u.target_id == _tid("e1"))
        tampered = imp.Plan(p.counts, [imp.Update(e1.target_id, e1.legacy_id, e1.email, HASHES["e2"])]
                            + [u for u in p.updates if u is not e1], p.fingerprint, None)
        try:
            with pytest.raises(Refused, match="planned hash no longer matches"):
                imp.lock_source(s, p.fingerprint, tampered)
        finally:
            imp.release_source(s)
        imp.lock_source(s, p.fingerprint, p)  # the real plan verifies under the lock
        try:
            assert imp.source_lock_held(s)
        finally:
            imp.release_source(s)
        assert not imp.source_lock_held(s)
    _assert_source_unlocked()


@db_only
@pytest.mark.parametrize("failure", ["target-write-error", "target-row-changed"])
def test_target_failures_under_the_lock_roll_back_and_release_it(loaded, monkeypatch, capsys, failure):
    reviewed = _reviewed_and_frozen(monkeypatch, capsys)
    _cli(monkeypatch, [PHRASE])
    if failure == "target-write-error":
        calls: list = []
        real_write = imp._write

        def flaky(cur, u):
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError("synthetic target failure")
            return real_write(cur, u)
        monkeypatch.setattr(imp, "_write", flaky)
        before, expected_exit, expected_err = _rows(), 1, "failed: RuntimeError"
    else:
        real_lock = imp.lock_source

        def lock_then_change_target(src, expected, p):
            real_lock(src, expected, p)
            with _admin(TGT_DB) as c:  # someone sets a password in V2 meanwhile
                c.execute("UPDATE public.users SET password_hash = '$argon2id$v2-reset' WHERE id = %s", (_tid("e2"),))
        monkeypatch.setattr(imp, "lock_source", lock_then_change_target)
        before = None
        expected_exit, expected_err = 2, "target changed since the plan"
    assert imp.main(["--apply", "--expected-fingerprint", reviewed]) == expected_exit
    rows = _rows()
    if before is not None:
        assert rows == before
    else:
        assert rows[_tid("e2")][0] == "$argon2id$v2-reset"
        assert all(rows[_tid(k)][0] is None for k in ("e1", "e4")) and rows[_tid("e3")][0] == ""
    assert expected_err in capsys.readouterr().err
    _assert_source_unlocked()


def _wait_until(predicate, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)  # poll interval only
    raise AssertionError("condition not reached")


@db_only
def test_measure_how_long_sign_in_writes_wait_during_a_134_account_import(loaded, monkeypatch, capsys):
    """Impact on GoTrue-style writes: a loop of sign-in updates (last_sign_in_at) runs against the source during a
    134-account import. They must wait while the lock is held and resume right after; the numbers are printed (run
    with -s) for the freeze README. Local connections: production adds the round trips to Supabase."""
    extra = 130
    with _admin(SRC_DB) as c:
        for i in range(extra):
            c.execute("INSERT INTO auth.users (id, email, encrypted_password, email_confirmed_at) VALUES (%s, %s, %s, now())",
                      (str(uuid.UUID(int=1000 + i)), f"bulk{i}{D}", _bcrypt()))
    with _admin(TGT_DB) as c:
        for i in range(extra):
            c.execute("INSERT INTO public.users (id, email, legacy_supabase_user_id, role) VALUES (%s, %s, %s, 'streamer')",
                      (str(uuid.UUID(int=5000 + i)), f"bulk{i}{D}", str(uuid.UUID(int=1000 + i))))
    reviewed = _reviewed_and_frozen(monkeypatch, capsys)
    stop = threading.Event()
    latencies: list[tuple[float, float]] = []

    def sign_ins():
        with _admin(SRC_DB) as c:
            while not stop.is_set():
                started = time.perf_counter()
                c.execute("UPDATE auth.users SET last_sign_in_at = now() WHERE id = %s", (IDS["s1"],))
                latencies.append((started, time.perf_counter() - started))
                time.sleep(0.002)
    marks: dict = {}
    real_lock, real_require, real_release = imp.lock_source, imp.require_source_lock, imp.release_source

    def lock(src, expected, p):
        marks["lock_requested"] = time.perf_counter()
        real_lock(src, expected, p)
        marks["verified"] = time.perf_counter()

    def require(src):
        marks["sign_in_waiting"] = _wait_for_lock("UPDATE auth.users SET last_sign_in_at")
        return real_require(src)

    def release(src):
        real_release(src)
        marks["released"] = time.perf_counter()
    monkeypatch.setattr(imp, "lock_source", lock)
    monkeypatch.setattr(imp, "require_source_lock", require)
    monkeypatch.setattr(imp, "release_source", release)
    writer = threading.Thread(target=sign_ins)
    writer.start()
    try:
        time.sleep(0.2)
        _cli(monkeypatch, [f"IMPORT {4 + extra} HASHES INTO {TGT_DB}"])
        assert imp.main(["--apply", "--expected-fingerprint", reviewed]) == 0
        time.sleep(0.2)
    finally:
        stop.set()
        writer.join(15)
    assert marks["sign_in_waiting"]  # a sign-in write was waiting on the lock during the import
    after_release = [d for s, d in latencies if s > marks["released"]]
    assert after_release and max(after_release) < 0.5  # and writes are quick again right after
    hold = marks["released"] - marks["lock_requested"]
    worst = max(d for s, d in latencies)
    assert hold < 5 and worst < 5
    print(f"\nD1 source-lock measurement: {4 + extra} accounts imported; lock requested to released "
          f"{hold * 1000:.0f} ms (lock + verification {(marks['verified'] - marks['lock_requested']) * 1000:.0f} ms); "
          f"longest sign-in write {worst * 1000:.0f} ms; {len(latencies)} sign-in writes, "
          f"{sum(1 for _, d in latencies if d > 0.05)} took over 50 ms")
