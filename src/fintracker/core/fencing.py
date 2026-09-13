"""Право исполнителя на результат команды (ADR-05, R-02).

Проверка аренды до начала работы не защищает её commit: аренда может быть
потеряна, пока команда выполняется. Поэтому право на запись проверяется ещё раз
внутри той же транзакции, где фиксируется результат, — под блокировкой бюджета,
первым шагом любой команды.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from typing import Any

# Проверка получает сессию транзакции, в которой фиксируется результат.
FenceCheck = Callable[[Any], Awaitable[bool]]

_current: ContextVar[FenceCheck | None] = ContextVar("execution_fence", default=None)
_identity: ContextVar[tuple[uuid.UUID, uuid.UUID] | None] = ContextVar(
    "execution_identity", default=None
)


@contextlib.asynccontextmanager
async def execution_fence(
    check: FenceCheck | None,
    *,
    job_id: uuid.UUID | None = None,
    lease_token: uuid.UUID | None = None,
) -> AsyncIterator[None]:
    """Выполнять вложенный код с правом, проверяемым при каждой записи."""
    token = _current.set(check)
    identity_token = _identity.set(
        (job_id, lease_token) if job_id is not None and lease_token is not None else None
    )
    try:
        yield
    finally:
        _current.reset(token)
        _identity.reset(identity_token)


def get_execution_identity() -> tuple[uuid.UUID, uuid.UUID] | None:
    """Задача и аренда, владеющие текущей попыткой фонового результата."""
    return _identity.get()


async def fence_is_valid(session: Any) -> bool:
    """Действует ли право на результат в этой транзакции.

    Без установленного права ограничения нет: интерактивные команды
    пользователя не связаны с арендой фоновой задачи.
    """
    check = _current.get()
    if check is None:
        return True
    return await check(session)
