"""Управление категориями из чата (FR-06, FR-21, FR-22, A113–A122)."""

from __future__ import annotations

import re
import uuid

from fintracker.application.catalog.categories import (
    create_category,
    list_categories,
    removal_preview,
    remove_category,
    restore_category,
)
from fintracker.application.conversation.keyboards import Button, callback, short
from fintracker.application.conversation.types import Reply
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.core.errors import ConflictError, ValidationFailed
from fintracker.db.models.access import Workspace
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork

_CREATE_PATTERN = re.compile(
    r"(?:созда(?:й|ть)|добав(?:ь|ить)|заведи)\s+категори[юя]\s+(?P<name>.+)$",
    re.IGNORECASE | re.DOTALL,
)


async def create_category_from_text(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, text: str
) -> list[Reply]:
    """«Создай категорию Путешествия» (FR-21, A113)."""
    match = _CREATE_PATTERN.search(text.strip())
    if match is None:
        return [Reply(text="Напишите «Создай категорию Название».")]
    name = match.group("name").strip(" .,«»\"'")
    if not name:
        return [Reply(text="Укажите название категории.")]
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id)
        try:
            view = await create_category(session, uow, actor=actor, name=name)
        except ConflictError as exc:
            return [Reply(text=exc.message)]
    return [
        Reply(
            text=(
                f"Категория «{view.full_path}» создана. Лимит не задан.\n"
                "Прошлые расходы не перенесены автоматически."
            ),
            buttons=(
                (
                    Button("Задать лимит", callback("cat", "limit", short(view.id))),
                    Button("Категории", callback("menu", "categories")),
                ),
            ),
        )
    ]


async def manage_categories(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        active = await list_categories(session, workspace_id=workspace_id)
        archived = [
            item
            for item in await list_categories(
                session, workspace_id=workspace_id, include_archived=True
            )
            if item.archived
        ]
    lines = ["Управление категориями:"]
    lines.extend(f"• {item.full_path}" for item in active[:20])
    if archived:
        lines.append(f"В архиве: {len(archived)}")
    rows: list[tuple[Button, ...]] = [
        (
            Button("Добавить", callback("cat", "new")),
            Button("Архив", callback("cat", "archive")),
        )
    ]
    rows.extend(
        (Button(item.name[:24], callback("cat", "open", short(item.id))),) for item in active[:8]
    )
    rows.append((Button("← Меню", callback("menu", "main")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def category_card(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, category_id: uuid.UUID
) -> list[Reply]:
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        preview = await removal_preview(session, workspace_id=workspace_id, category_id=category_id)
    lines = [
        f"Категория: {preview.name}",
        f"Связанных операций: {preview.transaction_count}",
        f"Строк плана: {preview.budget_line_count}",
        f"Подкатегорий: {preview.child_count}",
        f"Правил классификации: {preview.rule_count}",
        f"Будущих платежей: {preview.scheduled_count}",
    ]
    code = short(category_id)
    rows: list[tuple[Button, ...]] = [
        (
            Button("Задать лимит", callback("cat", "limit", code)),
            Button("Переименовать", callback("cat", "rename", code)),
        )
    ]
    if "delete" in preview.options:
        lines.append("Связей нет: категорию можно удалить окончательно.")
        rows.append((Button("Удалить", callback("cat", "del", code)),))
    else:
        lines.append(
            "У категории есть связи: доступны архив или перенос записей. "
            "Каскадное удаление трат запрещено."
        )
        rows.append(
            (
                Button("Убрать в архив", callback("cat", "arch", code)),
                Button("Перенести и убрать", callback("cat", "move", code)),
            )
        )
    rows.append((Button("← Категории", callback("cat", "manage")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def apply_category_removal(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    category_id: uuid.UUID,
    option: str,
    reassign_to: uuid.UUID | None = None,
) -> list[Reply]:
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id)
        try:
            preview = await remove_category(
                session,
                uow,
                actor=actor,
                category_id=category_id,
                option=option,
                reassign_to=reassign_to,
            )
        except (ConflictError, ValidationFailed) as exc:
            return [Reply(text=exc.message)]
    text = {
        "delete": f"Категория «{preview.name}» удалена. Другие сущности не затронуты.",
        "archive": (
            f"Категория «{preview.name}» убрана в архив. Расходы и план остались в отчётах."
        ),
        "reassign_and_archive": (
            f"Записи перенесены, категория «{preview.name}» убрана в архив. "
            "Общий расход не изменился."
        ),
    }[option]
    return [Reply(text=text, buttons=((Button("Категории", callback("menu", "categories")),),))]


async def apply_category_restore(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, category_id: uuid.UUID
) -> list[Reply]:
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id)
        try:
            view = await restore_category(session, uow, actor=actor, category_id=category_id)
        except ConflictError as exc:
            return [Reply(text=exc.message)]
    return [
        Reply(
            text=f"Категория «{view.full_path}» восстановлена с прежним идентификатором.",
            buttons=((Button("Категории", callback("menu", "categories")),),),
        )
    ]


async def choose_reassign_target(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, category_id: uuid.UUID
) -> list[Reply]:
    """Выбрать статью, куда переносятся записи перед архивом (FR-22, A120).

    Связанные правила и будущие платежи показываются явно: они не
    переназначаются молча.
    """
    from fintracker.application.catalog.categories import list_categories

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        preview = await removal_preview(session, workspace_id=workspace_id, category_id=category_id)
        categories = await list_categories(session, workspace_id=workspace_id)
    others = [item for item in categories if item.id != category_id][:6]
    if not others:
        return [
            Reply(
                text="Переносить записи некуда: в бюджете нет другой активной статьи.",
                buttons=((Button("← Категории", callback("cat", "manage")),),),
            )
        ]
    lines = [
        f"Куда перенести записи статьи «{preview.name}»?",
        f"Операций: {preview.transaction_count} · строк плана: {preview.budget_line_count}",
    ]
    if preview.rule_count:
        lines.append(
            f"Правил классификации: {preview.rule_count} — они будут отключены, "
            "новые записи в архивную статью не попадут."
        )
    if preview.scheduled_count:
        lines.append(
            f"Будущих платежей: {preview.scheduled_count} — укажите для них статью отдельно."
        )
    source = short(category_id)
    rows = [
        (Button(item.name[:24], callback("cat", "mv", source, short(item.id))),) for item in others
    ]
    rows.append((Button("← Категория", callback("cat", "open", source)),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]
