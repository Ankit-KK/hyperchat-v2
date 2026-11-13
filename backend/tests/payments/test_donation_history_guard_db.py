"""Migration 0030 on PostgreSQL: no API role writes donations.is_hyperemote or donations.legacy_source_table.

HyperEmotes are discontinued (owner decision D16). The flag and the import provenance are history: the legacy import
(hyperchat_migrator, SET ROLE hyperchat_owner) writes them, V2 code never does. A CHECK tied to legacy_source_table
would be forgeable because the runtime roles may write every donations column, so the guard is a trigger keyed on the
connection's login role (session_user)."""

import secrets

import psycopg
import pytest

from tests.integration import db, fixture_db, make_donation, make_streamer, requires_db

pytestmark = [requires_db, pytest.mark.usefixtures("env")]  # scratch-database check and synthetic-row cleanup
API_ROLES = ("hyperchat_app", "hyperchat_worker", "hyperchat_auth", "hyperchat_lifecycle", "hyperchat_stepup",
             "hyperchat_readonly")  # every runtime login role (deploy/postgres/roles.sql) except the migrator
WRITERS = ("hyperchat_app", "hyperchat_worker")  # the roles that may otherwise write donations (0007 grants)
GUARD = "imported history"
REFUSED = psycopg.errors.InsufficientPrivilege
# hyperchat_readonly is refused before privileges or the guard: deploy/postgres/roles.sql gives it
# default_transaction_read_only = on (layer 1), 0007 grants it SELECT only (layer 2), 0030 is layer 3.
READ_ONLY = psycopg.errors.ReadOnlySqlTransaction


def _refusal(role: str) -> type[psycopg.Error]:
    return READ_ONLY if role == "hyperchat_readonly" else REFUSED


def _insert(conn, sid: str, **cols) -> None:
    names = ["streamer_id", "order_id", "donor_name", "amount", "currency", "amount_inr", *cols]
    values = [sid, "authtest-guard-" + secrets.token_hex(6), "D", 5, "INR", 5, *cols.values()]
    conn.execute(f"INSERT INTO public.donations ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(values))})",
                 values)


def _creator(kind: str) -> str:
    return make_streamer(f"authtest-guard-{kind}-{secrets.token_hex(4)}")


def _row(did: str) -> tuple:
    with fixture_db() as conn:
        return conn.execute("SELECT is_hyperemote, legacy_source_table FROM public.donations WHERE id = %s",
                            (did,)).fetchone()


@pytest.mark.parametrize("role", API_ROLES)
@pytest.mark.parametrize("cols", [
    {"is_hyperemote": True},                                                   # a new HyperEmote
    {"is_hyperemote": True, "legacy_source_table": "authtest_donations"},      # ...dressed up as imported
    {"legacy_source_table": "authtest_donations"},                             # forged provenance alone
])
def test_no_api_role_inserts_a_hyperemote_or_import_provenance(role, cols):
    sid = _creator("ins")
    with db(role) as conn, pytest.raises(_refusal(role)) as refused:
        _insert(conn, sid, **cols)
    if role in WRITERS:  # the others lack INSERT altogether; the writers are stopped by the guard itself
        assert GUARD in str(refused.value)
    with fixture_db() as conn:
        assert conn.execute("SELECT count(*) FROM public.donations WHERE streamer_id = %s", (sid,)).fetchone()[0] == 0


@pytest.mark.parametrize("role", API_ROLES)
@pytest.mark.parametrize("change", [
    "is_hyperemote = true",                                    # mark a V2 row as a HyperEmote
    "legacy_source_table = 'authtest_donations'",              # forge provenance on a V2 row
    "is_hyperemote = true, legacy_source_table = 'authtest_donations'",
])
def test_no_api_role_turns_a_v2_row_into_a_hyperemote_or_an_import(role, change):
    sid = _creator("v2")
    did = make_donation(sid)
    with fixture_db() as conn:  # make_donation writes as the migrator: clear the fields to look like V2
        conn.execute("UPDATE public.donations SET legacy_source_table = NULL, is_hyperemote = false WHERE id = %s",
                     (did,))
    with db(role) as conn, pytest.raises(_refusal(role)) as refused:
        conn.execute(f"UPDATE public.donations SET {change} WHERE id = %s", (did,))
    if role in WRITERS:
        assert GUARD in str(refused.value)
    assert _row(did) == (False, None)


def test_the_readonly_role_stays_refused_even_after_switching_its_session_to_read_write():
    """Layer 1 is only a session default the role may change; layer 2 (SELECT-only grants) still refuses every
    forged write, and nothing is changed."""
    sid = _creator("ro")
    did = make_donation(sid)
    with db("hyperchat_readonly") as conn:
        assert conn.execute("SHOW transaction_read_only").fetchone()[0] == "on"  # layer 1 is in place
        conn.execute("SET default_transaction_read_only = off")
        assert conn.execute("SHOW transaction_read_only").fetchone()[0] == "off"
        for statement, args in (
                ("INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, amount_inr, "
                 "is_hyperemote, legacy_source_table) VALUES (%s, %s, 'D', 5, 'INR', 5, true, 'authtest_donations')",
                 (sid, "authtest-guard-" + secrets.token_hex(6))),
                ("UPDATE public.donations SET is_hyperemote = true, legacy_source_table = 'authtest_donations' "
                 "WHERE id = %s", (did,))):
            with pytest.raises(REFUSED):
                conn.execute(statement, args)
    assert _row(did) == (False, None)
    with fixture_db() as conn:
        assert conn.execute("SELECT count(*) FROM public.donations WHERE streamer_id = %s", (sid,)).fetchone()[0] == 1


@pytest.mark.parametrize("role", WRITERS)
@pytest.mark.parametrize("change", ["is_hyperemote = false", "legacy_source_table = NULL",
                                    "legacy_source_table = 'authtest_other_donations'"])
def test_imported_history_cannot_be_rewritten(role, change):
    sid = _creator("hist")
    did = make_donation(sid)
    with fixture_db() as conn:  # an imported HyperEmote, as the legacy import writes it
        conn.execute("UPDATE public.donations SET is_hyperemote = true, legacy_source_table = 'authtest_donations' "
                     "WHERE id = %s", (did,))
    with db(role) as conn, pytest.raises(REFUSED, match=GUARD):
        conn.execute(f"UPDATE public.donations SET {change} WHERE id = %s", (did,))
    assert _row(did) == (True, "authtest_donations")


@pytest.mark.parametrize("role", WRITERS)
def test_ordinary_writes_by_the_runtime_roles_still_work(role):
    """Everything V2 does with donations keeps working: other columns change freely, the guarded ones may be named
    with their current value, and an imported row can still be moderated."""
    sid = _creator("ok")
    did = make_donation(sid)
    with fixture_db() as conn:
        conn.execute("UPDATE public.donations SET is_hyperemote = true, legacy_source_table = 'authtest_donations' "
                     "WHERE id = %s", (did,))
    with db(role) as conn:
        conn.execute("UPDATE public.donations SET moderation_status = 'approved', message_visible = false, "
                     "is_hyperemote = true, legacy_source_table = 'authtest_donations' WHERE id = %s", (did,))
        assert conn.execute("SELECT moderation_status FROM public.donations WHERE id = %s", (did,)).fetchone()[0] \
            == "approved"
    assert _row(did) == (True, "authtest_donations")


def test_the_migrator_and_owner_path_of_the_legacy_import_is_unaffected():
    sid = _creator("etl")
    did = make_donation(sid)  # written as hyperchat_migrator with SET ROLE hyperchat_owner, like the import
    with fixture_db() as conn:
        conn.execute("UPDATE public.donations SET is_hyperemote = true, legacy_source_table = 'authtest_donations' "
                     "WHERE id = %s", (did,))
        conn.execute("UPDATE public.donations SET is_hyperemote = false, legacy_source_table = NULL WHERE id = %s",
                     (did,))
    assert _row(did) == (False, None)


def test_set_role_cannot_borrow_the_owners_freedom():
    """The guard looks at session_user: a runtime connection is refused even if it could SET ROLE (it cannot become
    the owner at all, but the rule does not depend on that)."""
    sid = _creator("setrole")
    with db("hyperchat_app") as conn:
        with pytest.raises(psycopg.Error):
            conn.execute("SET ROLE hyperchat_owner")
    with db("hyperchat_app") as conn, pytest.raises(REFUSED, match=GUARD):
        _insert(conn, sid, is_hyperemote=True)
