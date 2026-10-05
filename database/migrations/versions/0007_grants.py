"""Least-privilege grants for the runtime roles.

Revision ID: 0007
Revises: 0006

Roles are cluster-level and created by backend/deploy/postgres/roles.sql before migrations run:
  hyperchat_owner    owns every object (migrations run as it)
  hyperchat_app      API: DML on application tables, no DDL
  hyperchat_worker   background jobs: same as app for now, narrowed in later phases
  hyperchat_readonly reporting / verification: SELECT only
alembic_version is never writable by the runtime roles.
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

RUNTIME = "hyperchat_app, hyperchat_worker"


def upgrade() -> None:
    op.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {RUNTIME}, hyperchat_readonly")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {RUNTIME}")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO hyperchat_readonly")
    op.execute(f"REVOKE ALL ON public.alembic_version FROM {RUNTIME}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RUNTIME}")
    op.execute("REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.set_updated_at() TO {RUNTIME}")
    # Objects created by later migrations get the same grants automatically.
    op.execute(f"ALTER DEFAULT PRIVILEGES FOR ROLE hyperchat_owner IN SCHEMA public "
               f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {RUNTIME}")
    op.execute("ALTER DEFAULT PRIVILEGES FOR ROLE hyperchat_owner IN SCHEMA public "
               "GRANT SELECT ON TABLES TO hyperchat_readonly")
    op.execute(f"ALTER DEFAULT PRIVILEGES FOR ROLE hyperchat_owner IN SCHEMA public "
               f"GRANT USAGE, SELECT ON SEQUENCES TO {RUNTIME}")
    op.execute("ALTER DEFAULT PRIVILEGES FOR ROLE hyperchat_owner IN SCHEMA public "
               "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC")


def downgrade() -> None:
    op.execute("ALTER DEFAULT PRIVILEGES FOR ROLE hyperchat_owner IN SCHEMA public "
               "GRANT EXECUTE ON FUNCTIONS TO PUBLIC")
    op.execute(f"ALTER DEFAULT PRIVILEGES FOR ROLE hyperchat_owner IN SCHEMA public "
               f"REVOKE USAGE, SELECT ON SEQUENCES FROM {RUNTIME}")
    op.execute("ALTER DEFAULT PRIVILEGES FOR ROLE hyperchat_owner IN SCHEMA public "
               "REVOKE SELECT ON TABLES FROM hyperchat_readonly")
    op.execute(f"ALTER DEFAULT PRIVILEGES FOR ROLE hyperchat_owner IN SCHEMA public "
               f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {RUNTIME}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {RUNTIME}, hyperchat_readonly")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {RUNTIME}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {RUNTIME}, hyperchat_readonly")
    op.execute("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")
