"""Alembic environment (SQL-first, no ORM models).

Connects with HYPERCHAT_MIGRATION_DSN (migrator role over the Unix socket, peer auth) and runs
every migration as `hyperchat_owner`, so all objects - including alembic_version - are owned by
the owner role, never by a login role.
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool, text

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

OWNER_ROLE = os.environ.get("HYPERCHAT_SCHEMA_OWNER", "hyperchat_owner")


def _dsn() -> str:
    dsn = os.environ.get("HYPERCHAT_MIGRATION_DSN")
    if not dsn:
        raise RuntimeError("HYPERCHAT_MIGRATION_DSN is not set")
    return dsn


def run_migrations_offline() -> None:
    context.configure(url=_dsn(), literal_binds=True, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_dsn(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        connection.execute(text(f'SET ROLE "{OWNER_ROLE}"'))
        connection.execute(text("SET lock_timeout = '10s'"))
        connection.execute(text("SET statement_timeout = '5min'"))
        connection.commit()
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
