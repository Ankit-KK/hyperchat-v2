"""Donation history guard (HyperEmotes discontinued, owner decision D16): no API role writes is_hyperemote or
legacy_source_table.

Revision ID: 0030
Revises: 0029

HyperEmotes are permanently discontinued in V2. donations.is_hyperemote stays as the imported history of legacy
donations (the ETL loads it and reconciles its distribution), and donations.legacy_source_table records which legacy
table a row was imported from (payment-reconcile uses it to tell V2 rows from imported ones). Neither is ever written
by V2 application code.

The runtime roles hold INSERT and UPDATE on every donations column (0007's grants), so a CHECK constraint such as
"is_hyperemote only on imported rows" would be bypassed by setting legacy_source_table as well. Instead one BEFORE
INSERT OR UPDATE trigger refuses, for every runtime login role (the API, worker, auth,
lifecycle and step-up roles, and hyperchat_readonly, which its grants already keep read-only):
  * an INSERT with is_hyperemote = true, or with legacy_source_table set;
  * an UPDATE that changes is_hyperemote (either way: history is not rewritten) or legacy_source_table.
The role is session_user, the login role of the connection, which SET ROLE cannot change (as in 0027). The legacy
import connects as hyperchat_migrator and loads with user triggers disabled, so it is unaffected; so are migrations.

No row is read, changed or backfilled. Downgrade drops the trigger and its function.
"""

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

RUNTIME_ROLES = ("('hyperchat_app', 'hyperchat_worker', 'hyperchat_auth', 'hyperchat_lifecycle', 'hyperchat_stepup', "
                 "'hyperchat_readonly')")  # every login role in deploy/postgres/roles.sql except hyperchat_migrator
EVERYONE = ("hyperchat_app, hyperchat_worker, hyperchat_auth, hyperchat_lifecycle, hyperchat_readonly, "
            "hyperchat_stepup, PUBLIC")


def upgrade() -> None:
    op.execute(f"""
        CREATE FUNCTION public.donations_protect_history_columns() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public AS $$
        BEGIN
            IF session_user IN {RUNTIME_ROLES} AND (
                   (TG_OP = 'INSERT' AND (NEW.is_hyperemote IS DISTINCT FROM false
                                          OR NEW.legacy_source_table IS NOT NULL))
                OR (TG_OP = 'UPDATE' AND (NEW.is_hyperemote IS DISTINCT FROM OLD.is_hyperemote
                                          OR NEW.legacy_source_table IS DISTINCT FROM OLD.legacy_source_table)))
            THEN
                RAISE EXCEPTION 'donations.is_hyperemote and legacy_source_table are imported history: no API role '
                                'writes them (HyperEmotes are discontinued)' USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(f"REVOKE ALL ON FUNCTION public.donations_protect_history_columns() FROM {EVERYONE}")
    op.execute("CREATE TRIGGER donations_protect_history_columns BEFORE INSERT OR UPDATE ON public.donations "
               "FOR EACH ROW EXECUTE FUNCTION public.donations_protect_history_columns()")


def downgrade() -> None:
    op.execute("DROP TRIGGER donations_protect_history_columns ON public.donations")
    op.execute("DROP FUNCTION public.donations_protect_history_columns()")
