from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import command, context
from sqlalchemy import engine_from_config, pool

from shared.models import metadata
import os

if os.getenv("ALEMBIC_STAMP_ONLY") == "1":
    from alembic.config import Config
    from alembic.command import stamp

    cfg = Config("alembic.ini")
    stamp(cfg, "head")
    raise SystemExit(0)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

if os.getenv("ALEMBIC_STAMP_ONLY") == "1":
    command.stamp(config, "head")
    sys.exit(0)


def get_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url

    instance_connection_name = os.getenv("DB_INSTANCE_CONNECTION_NAME")
    if not instance_connection_name:
        raise RuntimeError("DB_INSTANCE_CONNECTION_NAME is required when DATABASE_URL is not set")

    db_name = os.getenv("DB_NAME")
    if not db_name:
        raise RuntimeError("DB_NAME is required when DATABASE_URL is not set")

    db_password = os.getenv("DB_PASSWORD")
    if not db_password:
        raise RuntimeError("DB_PASSWORD is required when DATABASE_URL is not set")

    db_user = os.getenv("DB_USER", "postgres")
    socket_file = f"/cloudsql/{instance_connection_name}/.s.PGSQL.5432"
    url = f"postgresql+pg8000://{db_user}:{db_password}@/{db_name}?unix_sock={socket_file}"
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
