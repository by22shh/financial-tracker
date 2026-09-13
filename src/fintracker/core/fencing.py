"""Право исполнителя на результат команды (ADR-05, R-02).

Проверка аренды до начала работы не защищает её commit: аренда может быть
потеряна, пока команда выполняется. Поэтому право на запись проверяется ещё раз
внутри той же транзакции, где фиксируется результат, — под блокировкой бюджета,
первым шагом любой команды.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from typing import Any

# Проверка получает сессию транзакции, в которой фиксируется результат.
FenceCheck = Callable[[Any], Awaitable[bool]]

_current: ContextVar[FenceCheck | None] = ContextVar("execution_fence", default=None)


@contextlib.asynccontextmanager
async def execution_fence(check: FenceCheck | None) -> AsyncIterator[None]:
    """Выполнять вложенный код с правом, проверяемым при каждой записи."""
    token = _current.set(check)
    try:
        yield
    finally:
        _current.reset(token)


async def fence_is_valid(session: Any) -> bool:
    """Действует ли право на результат в этой транзакции.

    Без установленного права ограничения нет: интерактивные команды
    пользователя не связаны с арендой фоновой задачи.
    """
    check = _current.get()
    if check is None:
        return True
    return await check(session)
