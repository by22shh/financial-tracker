"""Alembic окружение. Миграции выполняются ролью-владельцем (ADR-06, OPS-01)."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from fintracker.config import get_settings
from fintracker.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _owner_url() -> str:
    override = config.get_main_option("sqlalchemy.url", "")
    if override:
        return override
    return get_settings().db.owner_dsn


def run_migrations_offline() -> None:
    context.configure(
        url=_owner_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _owner_url().replace("+psycopg", "+psycopg")
    engine = create_engine(url, poolclass=pool.NullPool, future=True)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
