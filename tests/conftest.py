"""Общие фикстуры. Интеграционные проверки идут на настоящей PostgreSQL 17.

SQLite не заменяет эти проверки (раздел 8 инструкции разработчику): нужны
ограничения, RLS, блокировки, SKIP LOCKED и exclusion constraints.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.config import Settings, reset_settings_cache
from fintracker.core.clock import FixedClock
from fintracker.db.session import RuntimeRole, dispose_engines, get_sessionmaker, set_rls_context

TEST_DB_NAME = os.environ.get("FINTRACKER_TEST_DB", "fintracker_test")
PG_HOST = os.environ.get("FINTRACKER_TEST_PG_HOST", "localhost")
PG_PORT = os.environ.get("FINTRACKER_TEST_PG_PORT", "55432")
PG_OWNER = "fintracker_owner"
PG_PASSWORD = os.environ.get("FINTRACKER_TEST_PG_PASSWORD", "devpassword")


def _dsn(user: str, database: str) -> str:
    return f"postgresql+psycopg://{user}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{database}"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def test_settings() -> Iterator[Settings]:
    """Настройки тестовой среды с отдельной базой (OPS-03)."""
    env = {
        "FINTRACKER_ENV": "test",
        "FINTRACKER_DB__OWNER_DSN": _dsn(PG_OWNER, TEST_DB_NAME),
        "FINTRACKER_DB__API_DSN": _dsn("fintracker_api", TEST_DB_NAME),
        "FINTRACKER_DB__WORKER_DSN": _dsn("fintracker_worker", TEST_DB_NAME),
        "FINTRACKER_SECRETS__INVITE_HMAC_KEY": "test-invite-key",
        "FINTRACKER_SECRETS__CURSOR_HMAC_KEY": "test-cursor-key",
        "FINTRACKER_TELEGRAM__BOT_ID": "1000001",
        "FINTRACKER_TELEGRAM__CREATION_MODE": "open",
    }
    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    reset_settings_cache()
    settings = Settings()
    yield settings
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reset_settings_cache()


def _pg_available() -> bool:
    import socket

    try:
        with socket.create_connection((PG_HOST, int(PG_PORT)), timeout=1.5):
            return True
    except OSError:
        return False


PG_AVAILABLE = _pg_available()
requires_pg = pytest.mark.skipif(
    not PG_AVAILABLE,
    reason=(
        f"PostgreSQL 17 недоступна на {PG_HOST}:{PG_PORT}; "
        "поднимите её командой `docker compose up -d postgres`"
    ),
)


@pytest.fixture(scope="session")
def pg_database(test_settings: Settings) -> Iterator[None]:
    """Создаёт чистую тестовую базу и применяет миграции (AR-35)."""
    if not PG_AVAILABLE:
        pytest.skip("PostgreSQL недоступна")
    import subprocess

    from sqlalchemy import create_engine

    admin_url = _dsn(PG_OWNER, "postgres").replace("+psycopg", "+psycopg")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": TEST_DB_NAME},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
        conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}" OWNER {PG_OWNER}'))
    admin.dispose()

    env = dict(os.environ)
    env["FINTRACKER_DB__OWNER_DSN"] = _dsn(PG_OWNER, TEST_DB_NAME)
    result = subprocess.run(  # noqa: S603
        [".venv/bin/alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Миграции не применились: {result.stderr[-2000:]}")
    yield
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": TEST_DB_NAME},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
    admin.dispose()


@pytest_asyncio.fixture
async def clean_db(pg_database: None, test_settings: Settings) -> AsyncIterator[None]:
    """Очистка данных между проверками; схема не пересоздаётся."""
    factory = get_sessionmaker(test_settings, RuntimeRole.OWNER)
    async with factory() as session, session.begin():
        rows = (
            await session.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                    "AND tablename <> 'alembic_version'"
                )
            )
        ).scalars()
        tables = ", ".join(f'"{name}"' for name in rows)
        if tables:
            await session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield
    await dispose_engines()


@pytest.fixture
def clock() -> FixedClock:
    """Управляемое время: 12 сентября 2026, 12:00 UTC."""
    return FixedClock(dt.datetime(2026, 9, 12, 12, 0, tzinfo=dt.UTC))


@pytest_asyncio.fixture
async def owner_session(
    clean_db: None, test_settings: Settings
) -> AsyncIterator[AsyncSession]:
    """Сессия владельца схемы — для подготовки данных проверок."""
    factory = get_sessionmaker(test_settings, RuntimeRole.OWNER)
    async with factory() as session, session.begin():
        yield session
