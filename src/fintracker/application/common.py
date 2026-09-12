"""Общие примитивы команд: идемпотентность и результат (DATA_CONTRACT §2.6, §5)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.core.errors import IdempotencyConflict
from fintracker.core.ids import canonical_hash
from fintracker.db.models.platform import CommandResult


def canonical_body(payload: dict[str, Any]) -> str:
    """Каноническое типизированное содержимое команды.

    Хэш не зависит от случайного порядка JSON полей (DATA_CONTRACT §2.6).
    """
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Наблюдаемая завершённость команды (DATA_CONTRACT §5)."""

    command_id: uuid.UUID
    status: str
    entity_id: uuid.UUID | None = None
    entity_revision: int | None = None
    result: dict[str, Any] | None = None
    replayed: bool = False


async def find_existing_result(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    command: str,
    idempotency_key: str,
    body_hash: str,
) -> CommandResult | None:
    """Найти сохранённый результат того же ключа.

    Один ключ с другим содержимым даёт конфликт. Проверка доступа выполняется
    вызывающим кодом до возврата результата.
    """
    stmt = select(CommandResult).where(
        CommandResult.user_id == user_id,
        CommandResult.command == command,
        CommandResult.idempotency_key == idempotency_key,
    )
    stmt = stmt.where(
        CommandResult.workspace_id == workspace_id
        if workspace_id is not None
        else CommandResult.workspace_id.is_(None)
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is None:
        return None
    if existing.canonical_body_hash != body_hash:
        raise IdempotencyConflict(
            "Тот же ключ идемпотентности использован с другим содержимым команды"
        )
    return existing


async def store_result(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    command: str,
    idempotency_key: str,
    body_hash: str,
    entity_id: uuid.UUID | None,
    entity_revision: int | None,
    result: dict[str, Any],
) -> CommandResult:
    row = CommandResult(
        user_id=user_id,
        workspace_id=workspace_id,
        command=command,
        idempotency_key=idempotency_key,
        canonical_body_hash=body_hash,
        entity_id=entity_id,
        entity_revision=entity_revision,
        result=result,
    )
    session.add(row)
    return row


def body_hash_of(payload: dict[str, Any]) -> str:
    return canonical_hash(canonical_body(payload))
