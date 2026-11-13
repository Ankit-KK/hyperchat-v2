"""PAY-15 (D17) stage 2, migration 0031 on PostgreSQL: the one donor-total writer, its guards and its audit.

Every role and every path (payment completion, dashboard and bot moderation, a direct UPDATE) changes a donor total
only through the donations_credit_writer trigger: target = paid AND approved ? COALESCE(creditable_inr, amount_inr)
: 0, delta = target - credited_inr, applied under the row locks, a debit larger than the total failing closed.
No refund or dispute evidence is involved at this stage."""

import random
import secrets
import threading
from decimal import Decimal

import psycopg
import pytest

from tests.integration import db, fixture_db, make_donation, make_streamer, requires_db

pytestmark = [requires_db, pytest.mark.usefixtures("env")]
WRITERS = ("hyperchat_app", "hyperchat_worker")
REFUSED = psycopg.errors.InsufficientPrivilege
APPROVED = ("approved", "auto_approved")


def _creator() -> str:
    return make_streamer(f"authtest-credit-{secrets.token_hex(4)}")


def _total(sid: str, name: str = "Fan") -> tuple | None:
    with db() as conn:
        return conn.execute("SELECT total_amount, donation_count FROM public.streamer_donator_totals "
                            "WHERE streamer_id = %s AND donator_name_lower = lower(%s)", (sid, name)).fetchone()


def _credit(did: str) -> tuple:
    with db() as conn:
        return conn.execute("SELECT credited_inr, creditable_inr FROM public.donations WHERE id = %s", (did,)).fetchone()


def _audit(did: str) -> list[tuple]:
    with fixture_db() as conn:
        return conn.execute("SELECT cause, credited_before, credited_after, delta_inr, delta_count, total_after, actor "
                            "FROM public.donation_credit_events WHERE donation_id = %s ORDER BY id", (did,)).fetchall()


def _set(did: str, role: str = "hyperchat_app", **cols) -> None:
    sets = ", ".join(f"{k} = %({k})s" for k in cols)
    with db(role) as conn:
        conn.execute(f"UPDATE public.donations SET {sets} WHERE id = %(id)s", {**cols, "id": did})


def _backfilled(sid: str, amount, credited: str, name: str = "Fan") -> str:
    """A donation as public.donations_credit_backfill() leaves a short one (the real function runs once per database):
    inserted paid + approved with its writer lifted (owner DDL, one transaction, as the backfill does), ceiling =
    credit, and its 'backfill' audit row, so its audited deltas still add up to credited_inr. Its donor total is
    set by the test (the backfill never changes totals)."""
    with fixture_db() as conn, conn.transaction():
        conn.execute("ALTER TABLE public.donations DISABLE TRIGGER donations_credit_writer")
        did = conn.execute(
            "INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, amount_inr, payment_status, "
            "moderation_status, creditable_inr, credited_inr) VALUES (%s, %s, %s, %s, 'INR', %s, 'success', 'approved', "
            "%s, %s) RETURNING id::text", (sid, "authtest-" + secrets.token_hex(6), name, amount, amount, credited,
                                          credited)).fetchone()[0]
        conn.execute("ALTER TABLE public.donations ENABLE TRIGGER donations_credit_writer")
        if Decimal(credited) > 0:
            conn.execute("INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause, "
                         "payment_status, moderation_status, credited_before, credited_after, delta_inr, delta_count, "
                         "actor) VALUES (%s, %s, lower(%s), 'backfill', 'success', 'approved', 0, %s, %s, 0, "
                         "session_user)", (did, sid, name, credited, credited))
    return did


def _set_total(sid: str, total, count: int, name: str = "Fan") -> None:
    with fixture_db() as conn:
        conn.execute("INSERT INTO public.streamer_donator_totals AS t (streamer_id, donator_name, donator_name_lower, "
                     "total_amount, donation_count) VALUES (%s, %s, lower(%s), %s, %s) ON CONFLICT (streamer_id, "
                     "donator_name_lower) DO UPDATE SET total_amount = EXCLUDED.total_amount, "
                     "donation_count = EXCLUDED.donation_count", (sid, name, name, total, count))


# ── the writer on every path ────────────────────────────────────────────────────

@pytest.mark.parametrize("role", WRITERS)
def test_a_status_change_by_any_runtime_role_moves_the_total_once_and_is_audited(role):
    sid = _creator()
    did = make_donation(sid, 100, payment="pending", moderation="pending", name="Fan")
    assert _total(sid) is None and _credit(did) == (Decimal("0.00"), None)
    _set(did, role, payment_status="success", moderation_status="auto_approved")
    assert _total(sid) == (Decimal("100.00"), 1) and _credit(did)[0] == Decimal("100.00")
    _set(did, role, moderation_status="approved")                  # still credited: nothing moves
    _set(did, role, message_visible=False)                          # unrelated change: nothing moves
    assert _total(sid) == (Decimal("100.00"), 1)
    _set(did, role, moderation_status="rejected")
    assert _total(sid) == (Decimal("0.00"), 0) and _credit(did)[0] == Decimal("0.00")
    rows = _audit(did)
    assert [(r[0], r[3], r[4], r[5], r[6]) for r in rows] == [
        ("writer", Decimal("100.00"), 1, Decimal("100.00"), role), ("writer", Decimal("-100.00"), -1, Decimal("0.00"), role)]
    assert sum(r[3] for r in rows) == _credit(did)[0]                # credited = sum of its audited deltas


def test_a_paid_approved_insert_is_credited_at_insert():
    sid = _creator()
    did = make_donation(sid, 250, moderation="approved", name="Fan")  # as the migrator (fixtures, never the ETL)
    assert _total(sid) == (Decimal("250.00"), 1) and _credit(did) == (Decimal("250.00"), None)
    assert [r[0] for r in _audit(did)] == ["writer"]


# ── fail closed, never floored ──────────────────────────────────────────────────

def test_a_debit_larger_than_the_total_fails_closed():
    sid = _creator()
    did = make_donation(sid, 250, moderation="approved", name="Fan")
    with fixture_db() as conn:
        conn.execute("UPDATE public.streamer_donator_totals SET total_amount = 100 WHERE streamer_id = %s", (sid,))
    with pytest.raises(psycopg.Error) as refused:
        _set(did, moderation_status="rejected")
    assert refused.value.sqlstate == "HC002"
    assert _credit(did)[0] == Decimal("250.00") and _total(sid) == (Decimal("100.00"), 1) and len(_audit(did)) == 1


def test_a_missing_total_row_fails_closed_too():
    sid = _creator()
    did = make_donation(sid, 40, moderation="approved", name="Fan")
    with fixture_db() as conn:
        conn.execute("DELETE FROM public.streamer_donator_totals WHERE streamer_id = %s", (sid,))
    with pytest.raises(psycopg.Error) as refused:
        _set(did, moderation_status="rejected")
    assert refused.value.sqlstate == "HC002" and _credit(did)[0] == Decimal("40.00")


# ── ceilings (short backfilled rows) ────────────────────────────────────────────

def test_a_short_row_only_ever_moves_its_ceiling():
    sid = _creator()
    full = make_donation(sid, 100, moderation="approved", name="Fan")
    short = _backfilled(sid, 100, "50.00")  # the bucket really holds 150 (an old floor ate 50)
    _set_total(sid, 150, 2)
    for status, total in (("rejected", "100.00"), ("approved", "150.00"), ("rejected", "100.00"), ("approved", "150.00")):
        _set(short, moderation_status=status)
        assert _total(sid)[0] == Decimal(total)
    assert _credit(short) == (Decimal("50.00"), Decimal("50.00")) and _credit(full)[0] == Decimal("100.00")
    _set(full, moderation_status="rejected")
    assert _total(sid)[0] == Decimal("50.00")


def test_a_zero_ceiling_row_moves_nothing_and_counts_nothing():
    sid = _creator()
    other = make_donation(sid, 30, moderation="approved", name="Fan")
    zero = _backfilled(sid, 70, "0.00")
    _set_total(sid, 30, 1)
    _set(zero, moderation_status="rejected")
    _set(zero, moderation_status="approved")
    assert _total(sid) == (Decimal("30.00"), 1) and _credit(other)[0] == Decimal("30.00")


# ── guards: who may write what ──────────────────────────────────────────────────

@pytest.mark.parametrize("role", WRITERS)
@pytest.mark.parametrize("cols", [{"credited_inr": 1}, {"creditable_inr": 1}, {"credited_inr": 0, "creditable_inr": 0}])
def test_runtime_roles_never_write_the_credit_columns(role, cols):
    sid = _creator()
    did = make_donation(sid, 100, moderation="approved", name="Fan")
    before = (_credit(did), _total(sid))
    with pytest.raises(REFUSED):
        _set(did, role, **cols)
    assert (_credit(did), _total(sid)) == before


@pytest.mark.parametrize("role", WRITERS)
def test_runtime_roles_cannot_insert_a_preset_credit(role):
    sid = _creator()
    with db(role) as conn, pytest.raises(REFUSED):
        conn.execute("INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, amount_inr, "
                     "creditable_inr) VALUES (%s, %s, 'Fan', 5, 'INR', 5, 0)", (sid, "authtest-credit-" + secrets.token_hex(6)))


@pytest.mark.parametrize("role", WRITERS)
@pytest.mark.parametrize("sql", [
    "INSERT INTO public.streamer_donator_totals (streamer_id, donator_name, donator_name_lower, total_amount, "
    "donation_count) VALUES (%(sid)s, 'X', 'x', 1, 1)",
    "UPDATE public.streamer_donator_totals SET total_amount = 0 WHERE streamer_id = %(sid)s",
    "DELETE FROM public.streamer_donator_totals WHERE streamer_id = %(sid)s",
    "INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause, payment_status, "
    "moderation_status, credited_before, credited_after, delta_inr, delta_count, total_after, actor) VALUES "
    "(gen_random_uuid(), %(sid)s, 'x', 'writer', 'success', 'approved', 0, 1, 1, 1, 1, 'forged')",
    "UPDATE public.donation_credit_events SET delta_inr = 0 WHERE streamer_id = %(sid)s",
    "DELETE FROM public.donation_credit_events WHERE streamer_id = %(sid)s"])
def test_runtime_roles_have_no_direct_write_to_totals_or_the_audit(role, sql):
    sid = _creator()
    make_donation(sid, 100, moderation="approved", name="Fan")
    with db(role) as conn, pytest.raises(REFUSED):
        conn.execute(sql, {"sid": sid})
    assert _total(sid) == (Decimal("100.00"), 1)


@pytest.mark.parametrize("cols", [{"credited_inr": 10}, {"creditable_inr": 10}, {"credited_inr": 0, "creditable_inr": 0}])
def test_no_session_setting_authorises_a_credit_change_not_even_for_the_owner(cols):
    """Only public.donations_credit_backfill() sets the credit columns (it lifts the writer with owner DDL under the
    table lock); the former transaction-local flag, or any other setting, authorises nothing."""
    sid = _creator()
    did = make_donation(sid, 100, moderation="approved", name="Fan")
    sets = ", ".join(f"{k} = {v}" for k, v in cols.items())
    for role in (*WRITERS, None):
        with (fixture_db() if role is None else db(role)) as conn, conn.transaction(), pytest.raises(REFUSED):
            conn.execute("SELECT set_config('hyperchat.credit_backfill', 'on', true)")
            conn.execute(f"UPDATE public.donations SET {sets} WHERE id = %s", (did,))
    with fixture_db() as conn, pytest.raises(REFUSED):  # nor at insert
        conn.execute("INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, amount_inr, "
                     "creditable_inr) VALUES (%s, %s, 'Fan', 5, 'INR', 5, 5)", (sid, "authtest-" + secrets.token_hex(6)))
    assert _credit(did) == (Decimal("100.00"), None) and _total(sid) == (Decimal("100.00"), 1) and len(_audit(did)) == 1


def test_the_audit_is_append_only_for_the_owner_too():
    sid = _creator()
    did = make_donation(sid, 100, moderation="approved", name="Fan")
    for sql in ("UPDATE public.donation_credit_events SET actor = 'x' WHERE donation_id = %s",
                "DELETE FROM public.donation_credit_events WHERE donation_id = %s"):
        with fixture_db() as conn, pytest.raises(REFUSED):
            conn.execute(sql, (did,))
    with fixture_db() as conn, pytest.raises(REFUSED):
        conn.execute("TRUNCATE public.donation_credit_events")
    assert len(_audit(did)) == 1


@pytest.mark.parametrize("change", [{"donor_name": "Someone Else"}, {"streamer_id": None}])
def test_a_credited_donation_cannot_move_to_another_total_not_even_by_the_owner(change):
    sid = _creator()
    did = make_donation(sid, 100, moderation="approved", name="Fan")
    if "streamer_id" in change:
        change = {"streamer_id": _creator()}
    col, value = next(iter(change.items()))
    with fixture_db() as conn, pytest.raises(REFUSED):
        conn.execute(f"UPDATE public.donations SET {col} = %s WHERE id = %s", (value, did))
    assert _total(sid) == (Decimal("100.00"), 1)


# ── financial fields are fixed for runtime roles ────────────────────────────────

FIXED = [{"amount": 999}, {"amount_inr": 999}, {"currency": "USD"}, {"donor_name": "Someone Else"},
         {"donor_name": "FAN"}, {"streamer_id": None}]


@pytest.mark.parametrize("role", WRITERS)
@pytest.mark.parametrize("change", FIXED)
@pytest.mark.parametrize("state", ["pending", "credited", "rejected"])
def test_runtime_roles_never_change_financial_attribution(role, change, state):
    sid = _creator()
    did = make_donation(sid, 100, payment="pending" if state == "pending" else "success",
                        moderation={"pending": "pending", "credited": "approved", "rejected": "rejected"}[state],
                        name="Fan")
    if "streamer_id" in change:
        change = {"streamer_id": _creator()}
    before = (_credit(did), _total(sid))
    with pytest.raises(REFUSED):
        _set(did, role, **change)
    assert (_credit(did), _total(sid)) == before


@pytest.mark.parametrize("role", WRITERS)
def test_the_payment_id_is_set_once_and_a_paid_donation_stays_paid(role):
    sid = _creator()
    did = make_donation(sid, 100, payment="pending", moderation="pending", name="Fan")
    _set(did, role, payment_status="failed", moderation_status="rejected")          # pending -> failed: allowed
    _set(did, role, payment_status="success", provider_payment_id="pay_TESTcredit" + secrets.token_hex(4),
         moderation_status="auto_approved")                                          # failed -> success: allowed
    assert _total(sid) == (Decimal("100.00"), 1)
    for change in ({"provider_payment_id": "pay_TESTother"}, {"provider_payment_id": None},
                   {"payment_status": "failed"}, {"payment_status": "pending"}):
        with pytest.raises(REFUSED):
            _set(did, role, **change)
    _set(did, role, moderation_status="rejected")                                    # moderation still works
    assert _total(sid) == (Decimal("0.00"), 0) and _credit(did)[0] == Decimal("0.00")


# ── a credited donation is never deleted ────────────────────────────────────────

@pytest.mark.parametrize("role", (*WRITERS, None))
def test_a_credited_donation_cannot_be_deleted_by_any_role(role):
    sid = _creator()
    did = make_donation(sid, 100, moderation="approved", name="Fan")
    with (fixture_db() if role is None else db(role)) as conn, pytest.raises(REFUSED):
        conn.execute("DELETE FROM public.donations WHERE id = %s", (did,))
    assert _credit(did)[0] == Decimal("100.00") and _total(sid) == (Decimal("100.00"), 1)


@pytest.mark.parametrize("role", WRITERS)
def test_an_uncredited_donation_may_still_be_deleted_and_a_rejected_one_after_its_debit(role):
    sid = _creator()
    pending = make_donation(sid, 50, payment="pending", moderation="pending", name="Fan")
    credited = make_donation(sid, 100, moderation="approved", name="Fan")
    _set(credited, role, moderation_status="rejected")  # the writer debits it: it no longer counts
    with db(role) as conn:
        assert conn.execute("DELETE FROM public.donations WHERE id = ANY(%s::uuid[])", ([pending, credited],)).rowcount == 2
    assert _total(sid) == (Decimal("0.00"), 0) and sum(r[3] for r in _audit(credited)) == 0  # audit outlives the row


def test_truncate_is_refused_while_any_donation_is_credited():
    make_donation(_creator(), 100, moderation="approved", name="Fan")
    with fixture_db() as conn, pytest.raises(REFUSED), conn.transaction(force_rollback=True):
        conn.execute("TRUNCATE public.donations CASCADE")
    for role in WRITERS:  # never granted to the runtime roles (0007)
        with db(role) as conn, pytest.raises(REFUSED):
            conn.execute("TRUNCATE public.donations")


def test_only_the_owner_can_lift_the_delete_guard_explicitly():
    sid = _creator()
    did = make_donation(sid, 100, moderation="approved", name="Fan")
    for role in WRITERS:
        with db(role) as conn, pytest.raises(REFUSED):
            conn.execute("ALTER TABLE public.donations DISABLE TRIGGER donations_credit_delete_guard")
    with fixture_db() as conn, conn.transaction(force_rollback=True):  # authorised cleanup (scratch / synthetic)
        conn.execute("ALTER TABLE public.donations DISABLE TRIGGER donations_credit_delete_guard")
        assert conn.execute("DELETE FROM public.donations WHERE id = %s", (did,)).rowcount == 1
    assert _credit(did)[0] == Decimal("100.00")  # rolled back: still there, still guarded


def test_the_backfill_refuses_once_the_audit_has_rows():
    make_donation(_creator(), 100, moderation="approved", name="Fan")  # the audit has at least this row
    with fixture_db() as conn, pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        conn.execute("SELECT * FROM public.donations_credit_backfill()")


# ── concurrency ──────────────────────────────────────────────────────────────────

def test_concurrent_credits_to_one_total_lose_nothing():
    sid = _creator()
    dids = [make_donation(sid, 10 * (i + 1), payment="pending", moderation="pending", name="Fan") for i in range(6)]
    errors = []

    def approve(did):
        try:
            _set(did, payment_status="success", moderation_status="auto_approved")
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=approve, args=(d,)) for d in dids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == [] and _total(sid) == (Decimal("210.00"), 6)


def test_a_debit_racing_a_credit_serialises_on_the_total():
    sid = _creator()
    old = make_donation(sid, 100, moderation="approved", name="Fan")
    new = make_donation(sid, 50, payment="pending", moderation="pending", name="Fan")
    holder = db()
    holder.autocommit = False
    try:
        holder.execute("UPDATE public.donations SET payment_status = 'success', moderation_status = 'auto_approved' "
                       "WHERE id = %s", (new,))                      # holds the total's row lock
        done = threading.Event()
        worker = threading.Thread(target=lambda: (_set(old, moderation_status="rejected"), done.set()))
        worker.start()
        assert not done.wait(0.5)                                     # the debit waits for the credit's lock
        holder.commit()
        worker.join(30)
        assert done.is_set()
    finally:
        holder.close()
    assert _total(sid) == (Decimal("50.00"), 1)
    assert (_credit(old)[0], _credit(new)[0]) == (Decimal("0.00"), Decimal("50.00"))


# ── invariant over random sequences (F3, F4, F7) ─────────────────────────────────

def test_random_status_sequences_keep_credits_totals_and_audit_consistent():
    r = random.Random(20261114)
    sid = _creator()
    dids = [make_donation(sid, r.choice([10, 25, 99.5, 250]), payment="pending", moderation="pending", name="Fan")
            for _ in range(4)]
    for _ in range(60):
        did = r.choice(dids)
        cols = r.choice([{"payment_status": "success", "moderation_status": "auto_approved"},
                         {"moderation_status": "approved"}, {"moderation_status": "rejected"},
                         {"moderation_status": "pending"}, {"message_visible": r.random() < .5}])
        try:
            _set(did, **cols)
        except psycopg.errors.CheckViolation:
            continue  # e.g. a status the table refuses; nothing was written
        with db() as conn:
            rows = conn.execute("SELECT id::text, payment_status, moderation_status, amount_inr, credited_inr FROM "
                                "public.donations WHERE streamer_id = %s", (sid,)).fetchall()
        for row_id, pay, mod, amount, credited in rows:
            assert credited == (amount if pay == "success" and mod in APPROVED else 0)          # F3
            assert sum(a[3] for a in _audit(row_id)) == credited                                # F7
        total = _total(sid)
        assert (total[0] if total else 0) == sum(row[4] for row in rows)                         # F4 (no baseline)
        with fixture_db() as conn:                                                               # ledger = total
            ledger = conn.execute("SELECT coalesce(sum(delta_inr), 0), (array_agg(total_after ORDER BY id DESC))[1] "
                                  "FROM public.donation_credit_events WHERE streamer_id = %s", (sid,)).fetchone()
        assert ledger[0] == (total[0] if total else 0) and (ledger[1] is None or ledger[1] == total[0])
