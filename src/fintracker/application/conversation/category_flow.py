"""Управление категориями из чата (FR-06, FR-21, FR-22, A113–A122)."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from fintracker.application.catalog.categories import (
    create_category,
    list_categories,
    removal_preview,
    remove_category,
    rename_category,
    restore_category,
)
from fintracker.application.conversation.keyboards import Button, callback, short
from fintracker.application.conversation.types import Reply
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.core.errors import ConflictError, DomainError, ValidationFailed
from fintracker.core.money import Money
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
        await uow.lock_workspace(workspace_id, actor=actor)
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
        await uow.lock_workspace(workspace_id, actor=actor)
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
        await uow.lock_workspace(workspace_id, actor=actor)
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


async def archived_categories(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    """Архив статей с возможностью восстановления (FR-22, G-13)."""
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        archived = [
            item
            for item in await list_categories(
                session, workspace_id=workspace_id, include_archived=True
            )
            if item.archived
        ]
    if not archived:
        return [
            Reply(
                text="В архиве нет статей.",
                buttons=((Button("← Категории", callback("cat", "manage")),),),
            )
        ]
    lines = ["Архив статей:"]
    lines.extend(f"• {item.full_path}" for item in archived[:20])
    rows: list[tuple[Button, ...]] = [
        (Button(f"Вернуть {item.name[:16]}", callback("cat", "restore", short(item.id))),)
        for item in archived[:8]
    ]
    rows.append((Button("← Категории", callback("cat", "manage")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def apply_pending_rename(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    category_id: uuid.UUID,
    name: str,
) -> list[Reply]:
    """Применить новое название статьи из ответа участника (FR-22, G-13)."""
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        try:
            view = await rename_category(
                session, uow, actor=actor, category_id=category_id, name=name
            )
        except DomainError as exc:
            return [Reply(text=exc.message)]
    return [
        Reply(
            text=f"Статья переименована: «{view.full_path}». Записи и лимит сохранены.",
            buttons=((Button("Категории", callback("menu", "categories")),),),
        )
    ]


async def apply_pending_limit(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    category_id: uuid.UUID,
    text: str,
) -> list[Reply]:
    """Применить новый лимит статьи из ответа участника (FR-21, G-13)."""
    from fintracker.application.planning.periods import period_for_date
    from fintracker.application.planning.plan import (
        PlanLineSpec,
        change_line_limit,
        create_budget_version,
        current_budget_version,
        line_key,
    )
    from fintracker.db.models.planning import BudgetLine
    from fintracker.domain.parsing.amounts import parse_amounts

    amounts = parse_amounts(text)
    if not amounts:
        return [Reply(text="Не понял сумму лимита. Отправьте число, например 8000.")]
    limit = Money.from_decimal(Decimal(amounts[0].value), workspace.currency)

    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        period = await period_for_date(session, workspace_id=workspace_id, day=today)
        version = await current_budget_version(
            session, workspace_id=workspace_id, period_id=period.id
        )
        if version is None:
            return [Reply(text="План периода ещё не создан: задайте лимит через «Бюджет».")]
        line = (
            await session.execute(
                select(BudgetLine).where(
                    BudgetLine.workspace_id == workspace_id,
                    BudgetLine.budget_version_id == version.id,
                    BudgetLine.category_id == category_id,
                )
            )
        ).scalar_one_or_none()
        if line is None:
            stable_line_id = uuid.uuid5(uuid.NAMESPACE_URL, line_key(category_id, None))
            payload = [
                PlanLineSpec(
                    category_id=row.category_id,
                    beneficiary_id=row.beneficiary_id,
                    limit_minor=row.limit_minor,
                    rollover_mode=row.rollover_mode,
                    is_protected=row.is_protected,
                    stable_line_id=row.stable_line_id,
                )
                for row in (
                    (
                        await session.execute(
                            select(BudgetLine).where(
                                BudgetLine.workspace_id == workspace_id,
                                BudgetLine.budget_version_id == version.id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            ]
            payload.append(
                PlanLineSpec(
                    category_id=category_id,
                    beneficiary_id=None,
                    limit_minor=limit.minor,
                    stable_line_id=stable_line_id,
                )
            )
            await create_budget_version(
                session,
                workspace_id=workspace_id,
                period_id=period.id,
                kind="working",
                plan_status="approved",
                origin="manual",
                lines=payload,
                overall_limit_minor=version.overall_limit_minor,
                approved_by=actor.user_id,
                reason="Добавление лимита новой статьи в текущий период",
            )
            await uow.bump_revisions(workspace_id, plan=True)
        else:
            stable_line_id = line.stable_line_id
            try:
                await change_line_limit(
                    session,
                    uow,
                    actor=actor,
                    period_id=period.id,
                    stable_line_id=stable_line_id,
                    new_limit_minor=limit.minor,
                    expected_version=version.version,
                )
            except DomainError as exc:
                return [Reply(text=exc.message)]
    return [
        Reply(
            text=f"Лимит статьи обновлён: {limit.format()}.",
            buttons=((Button("Категории", callback("menu", "categories")),),),
        )
    ]
