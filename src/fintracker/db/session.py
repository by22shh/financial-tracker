"""Соединения, транзакционный контекст RLS и таймауты (ADR-04, ADR-06, ADR-12)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from fintracker.config import LimitsSettings, Settings

# Роль процесса определяет DSN и размер пула (ADR-12).


class RuntimeRole(StrEnum):
    API = "api"
    WORKER = "worker"
    SCHEDULER = "scheduler"
    OWNER = "owner"


class StatementClass(StrEnum):
    """Класс запроса определяет statement_timeout (ADR-04)."""

    COMMAND = "command"
    REPORT = "report"
    BATCH = "batch"


_ENGINES: Final[dict[RuntimeRole, AsyncEngine]] = {}
_SESSION_FACTORIES: Final[dict[RuntimeRole, async_sessionmaker[AsyncSession]]] = {}


def _dsn_for(settings: Settings, role: RuntimeRole) -> str:
    match role:
        case RuntimeRole.API:
            return settings.db.api_dsn
        case RuntimeRole.WORKER | RuntimeRole.SCHEDULER:
            return settings.db.worker_dsn
        case RuntimeRole.OWNER:
            return settings.db.owner_dsn


def _pool_for(settings: Settings, role: RuntimeRole) -> tuple[int, int]:
    match role:
        case RuntimeRole.API:
            return settings.db.api_pool_size, settings.db.api_max_overflow
        case RuntimeRole.WORKER:
            return settings.db.worker_pool_size, settings.db.worker_max_overflow
        case RuntimeRole.SCHEDULER:
            return settings.db.scheduler_pool_size, settings.db.scheduler_max_overflow
        case RuntimeRole.OWNER:
            return 1, 0


def get_engine(settings: Settings, role: RuntimeRole = RuntimeRole.API) -> AsyncEngine:
    engine = _ENGINES.get(role)
    if engine is None:
        pool_size, max_overflow = _pool_for(settings, role)
        engine = create_async_engine(
            _dsn_for(settings, role),
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,
            pool_recycle=1800,
            echo=settings.db.echo_sql,
            future=True,
        )
        _ENGINES[role] = engine
    return engine


def get_sessionmaker(
    settings: Settings, role: RuntimeRole = RuntimeRole.API
) -> async_sessionmaker[AsyncSession]:
    factory = _SESSION_FACTORIES.get(role)
    if factory is None:
        factory = async_sessionmaker(
            bind=get_engine(settings, role),
            expire_on_commit=False,
            autoflush=False,
        )
        _SESSION_FACTORIES[role] = factory
    return factory


async def dispose_engines() -> None:
    for engine in _ENGINES.values():
        await engine.dispose()
    _ENGINES.clear()
    _SESSION_FACTORIES.clear()


async def apply_statement_limits(
    session: AsyncSession, limits: LimitsSettings, statement_class: StatementClass
) -> None:
    """Транзакционные таймауты; значения локальны для транзакции."""
    timeout = {
        StatementClass.COMMAND: limits.statement_timeout_ms,
        StatementClass.REPORT: limits.report_statement_timeout_ms,
        StatementClass.BATCH: limits.batch_statement_timeout_ms,
    }[statement_class]
    await session.execute(text(f"SET LOCAL statement_timeout = {int(timeout)}"))
    await session.execute(text(f"SET LOCAL lock_timeout = {int(limits.lock_timeout_ms)}"))
    await session.execute(
        text(
            "SET LOCAL idle_in_transaction_session_timeout = "
            f"{int(limits.idle_in_transaction_timeout_ms)}"
        )
    )


async def set_rls_context(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
) -> None:
    """Установить контекст RLS на текущую транзакцию (SEC-03).

    ``is_local=true`` гарантирует, что контекст не переносится следующему
    пользователю из пула соединений.
    """
    await session.execute(
        text("SELECT set_config('app.user_id', :value, true)"),
        {"value": str(user_id) if user_id else ""},
    )
    await session.execute(
        text("SELECT set_config('app.workspace_id', :value, true)"),
        {"value": str(workspace_id) if workspace_id else ""},
    )


async def clear_rls_context(session: AsyncSession) -> None:
    await set_rls_context(session, user_id=None, workspace_id=None)


@asynccontextmanager
async def session_scope(
    settings: Settings,
    role: RuntimeRole = RuntimeRole.API,
    *,
    user_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    statement_class: StatementClass = StatementClass.COMMAND,
) -> AsyncIterator[AsyncSession]:
    """Отдельная AsyncSession на задачу/запрос (ADR-12).

    Сессия не разделяется между параллельными задачами: соединение не должно
    удерживаться во время ожидания внешнего провайдера.
    """
    factory = get_sessionmaker(settings, role)
    async with factory() as session, session.begin():
        await apply_statement_limits(session, settings.limits, statement_class)
        await set_rls_context(session, user_id=user_id, workspace_id=workspace_id)
        yield session


@asynccontextmanager
async def readonly_snapshot(
    settings: Settings,
    role: RuntimeRole = RuntimeRole.API,
    *,
    user_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
) -> AsyncIterator[AsyncSession]:
    """Короткая read-only транзакция REPEATABLE READ (ADR-04).

    Многозапросный отчёт получает один согласованный снимок.
    """
    factory = get_sessionmaker(settings, role)
    async with factory() as session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        await apply_statement_limits(session, settings.limits, StatementClass.REPORT)
        await set_rls_context(session, user_id=user_id, workspace_id=workspace_id)
        yield session
