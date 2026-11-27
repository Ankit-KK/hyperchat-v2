"""Security audit 2026-11-26: pg_temp last in the search_path of the 11 SECURITY DEFINER payment functions.

The SECURITY DEFINER payment functions of 0031/0032/0034/0035 (the PAY-15 verifier's DEFINER_FUNCTIONS) set
search_path = pg_catalog, public and omit pg_temp. PostgreSQL then searches pg_temp FIRST for relations, so a caller's
TEMP table could shadow an unqualified table name inside a function that runs with its owner's rights. Every other
SECURITY DEFINER function in public already ends its search_path in pg_temp.

ALTER FUNCTION ... SET search_path = pg_catalog, public, pg_temp changes only that setting, function by function
(lock_timeout on payment_accounting_set_state is kept). Bodies, owners, grants and every pin are unchanged, and no
table data is read or written. The PAY-15 verifier registers the new proconfig as head 0043's "configs"
(app.payments.verify.HEAD_DELTAS), so I10 expects pg_temp from 0043 on and flags any other search path.

Refused unless exactly the 11 functions are found, so a schema that drifted stops the migration instead of
half-applying it. Downgrade restores pg_catalog, public on the same 11.
"""

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

PG_TEMP_FUNCTIONS = ("donation_accounting_apply", "donation_accounting_core", "donations_credit_writer",
                     "payment_accounting_apply_evaluation", "payment_accounting_keep", "payment_accounting_lock_state",
                     "payment_accounting_open_run", "payment_accounting_reconcile", "payment_accounting_revert",
                     "payment_accounting_set_state", "payment_evidence_fp")


def upgrade() -> None:
    op.execute(search_path_sql("pg_catalog, public, pg_temp"))


def downgrade() -> None:
    op.execute(search_path_sql("pg_catalog, public"))


def search_path_sql(path: str) -> str:
    """ALTER FUNCTION ... SET search_path for exactly these 11 functions (one each; their exact signatures from the
    catalog), refused unless all 11 are found: a schema that drifted stops the migration instead of half-applying."""
    names = ", ".join(f"'{n}'" for n in PG_TEMP_FUNCTIONS)
    return f"""
        DO $$
        DECLARE f regprocedure; n integer := 0;
        BEGIN
            FOR f IN SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace s ON s.oid = p.pronamespace
                     WHERE s.nspname = 'public' AND p.prosecdef AND p.proname IN ({names}) ORDER BY p.proname LOOP
                EXECUTE format('ALTER FUNCTION %s SET search_path = {path}', f);
                n := n + 1;
            END LOOP;
            IF n <> {len(PG_TEMP_FUNCTIONS)} THEN
                RAISE EXCEPTION 'expected {len(PG_TEMP_FUNCTIONS)} SECURITY DEFINER payment functions, found %', n;
            END IF;
        END $$
    """
