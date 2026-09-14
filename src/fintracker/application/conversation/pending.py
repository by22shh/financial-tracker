"""Ожидаемый ввод участника после нажатия кнопки (G-13…G-16).

Кнопка, которая обещает продолжение («Переименовать», «Задать лимит»,
«Добавить цель», «Создать платёж», «Оплачено»), сохраняет свой контекст. Пока
он не истёк и не отменён, следующее сообщение участника относится к этому
действию, а не разбирается как новая трата.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from fintracker.config import Settings
from fintracker.db.models.platform import PendingAction
from fintracker.db.session import RuntimeRole, session_scope

# Ожидание живёт недолго: забытое действие не перехватывает следующую трату.
PENDING_TTL = dt.timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class Pending:
    kind: str
    workspace_id: uuid.UUID | None
    payload: dict[str, Any]


async def set_pending(
    settings: Settings,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    kind: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Запомнить обещанное действие; прежнее ожидание заменяется."""
    async with session_scope(
        settings, RuntimeRole.API, user_id=user_id, workspace_id=workspace_id
    ) as session:
        await session.execute(
            pg_insert(PendingAction)
            .values(
                user_id=user_id,
                workspace_id=workspace_id,
                kind=kind,
                payload=payload or {},
                expires_at=dt.datetime.now(dt.UTC) + PENDING_TTL,
            )
            .on_conflict_do_update(
                index_elements=[PendingAction.user_id],
                set_={
                    "workspace_id": workspace_id,
                    "kind": kind,
                    "payload": payload or {},
                    "expires_at": dt.datetime.now(dt.UTC) + PENDING_TTL,
                },
            )
        )


async def peek_pending(
    settings: Settings, *, user_id: uuid.UUID, workspace_id: uuid.UUID | None
) -> Pending | None:
    """Прочитать ожидание без удаления.

    Ожидание удаляется только после успешного применения действия. Невалидный
    ввод просит повтор и не превращает следующее сообщение в расход.
    """
    async with session_scope(
        settings, RuntimeRole.API, user_id=user_id, workspace_id=workspace_id
    ) as session:
        row = (
            await session.execute(
                select(PendingAction).where(PendingAction.user_id == user_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        expired = row.expires_at <= dt.datetime.now(dt.UTC)
        if expired:
            await session.execute(delete(PendingAction).where(PendingAction.id == row.id))
            return None
        return Pending(kind=row.kind, workspace_id=row.workspace_id, payload=dict(row.payload))


async def take_pending(
    settings: Settings, *, user_id: uuid.UUID, workspace_id: uuid.UUID | None
) -> Pending | None:
    """Забрать ожидание, когда действие уже точно должно завершиться."""
    pending = await peek_pending(settings, user_id=user_id, workspace_id=workspace_id)
    if pending is None:
        return None
    await clear_pending(settings, user_id=user_id, workspace_id=workspace_id)
    return pending


async def clear_pending(
    settings: Settings, *, user_id: uuid.UUID, workspace_id: uuid.UUID | None = None
) -> None:
    async with session_scope(
        settings, RuntimeRole.API, user_id=user_id, workspace_id=workspace_id
    ) as session:
        await session.execute(delete(PendingAction).where(PendingAction.user_id == user_id))
