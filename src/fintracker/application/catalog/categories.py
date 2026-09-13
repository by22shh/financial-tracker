"""Справочник категорий (FR-21, FR-22, CMD-09).

Переименование сохраняет ID и историю; архивирование не удаляет прошлые
расходы; каскадное удаление трат запрещено.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.catalog.normalize import clean_display_name, normalize_name
from fintracker.core.context import ActorContext
from fintracker.core.errors import ConflictError, NotFound, ValidationFailed
from fintracker.db.models.catalog import Category, CategoryMergeMap, ClassificationRule
from fintracker.db.models.commitments import ScheduleVersion
from fintracker.db.models.ledger import Allocation
from fintracker.db.models.planning import BudgetLine
from fintracker.db.uow import UnitOfWork

MAX_DEPTH = 4


@dataclass(frozen=True, slots=True)
class CategoryView:
    id: uuid.UUID
    name: str
    full_path: str
    parent_id: uuid.UUID | None
    archived: bool
    version: int
    sort_order: int


@dataclass(frozen=True, slots=True)
class RemovalPreview:
    category_id: uuid.UUID
    name: str
    transaction_count: int
    budget_line_count: int
    child_count: int
    rule_count: int
    scheduled_count: int
    options: tuple[str, ...]


async def _path_of(session: AsyncSession, workspace_id: uuid.UUID, category: Category) -> str:
    parts = [category.name]
    parent_id = category.parent_id
    depth = 0
    while parent_id is not None and depth < MAX_DEPTH:
        parent = (
            await session.execute(
                select(Category).where(
                    Category.workspace_id == workspace_id, Category.id == parent_id
                )
            )
        ).scalar_one_or_none()
        if parent is None:
            break
        parts.append(parent.name)
        parent_id = parent.parent_id
        depth += 1
    return " / ".join(reversed(parts))


async def list_categories(
    session: AsyncSession, *, workspace_id: uuid.UUID, include_archived: bool = False
) -> list[CategoryView]:
    statement = select(Category).where(Category.workspace_id == workspace_id)
    if not include_archived:
        statement = statement.where(Category.archived_at.is_(None))
    rows = (
        (await session.execute(statement.order_by(Category.sort_order, Category.name)))
        .scalars()
        .all()
    )
    return [
        CategoryView(
            id=row.id,
            name=row.name,
            full_path=await _path_of(session, workspace_id, row),
            parent_id=row.parent_id,
            archived=row.archived_at is not None,
            version=row.version,
            sort_order=row.sort_order,
        )
        for row in rows
    ]


async def _check_depth(
    session: AsyncSession, workspace_id: uuid.UUID, parent_id: uuid.UUID | None
) -> None:
    depth = 0
    cursor = parent_id
    while cursor is not None:
        depth += 1
        if depth >= MAX_DEPTH:
            raise ValidationFailed(f"Слишком глубокая вложенность категорий (предел {MAX_DEPTH})")
        # Отсутствие строки и пустой parent_id различаются: у категории верхнего
        # уровня родителя нет, и это не ошибка (FR-21, A121).
        row = (
            await session.execute(
                select(Category.id, Category.parent_id).where(
                    Category.workspace_id == workspace_id, Category.id == cursor
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFound("Родительская категория недоступна")
        cursor = row[1]


async def create_category(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    name: str,
    parent_id: uuid.UUID | None = None,
    description: str | None = None,
) -> CategoryView:
    """Создать категорию; обязательно только название (FR-21, A113)."""
    workspace_id = actor.require_workspace()
    display = clean_display_name(name)
    if not display:
        raise ValidationFailed("Название категории не может быть пустым")
    normalized = normalize_name(display)
    await _check_depth(session, workspace_id, parent_id)

    existing = (
        await session.execute(
            select(Category).where(
                Category.workspace_id == workspace_id,
                Category.normalized_name == normalized,
                Category.parent_id.is_(parent_id)
                if parent_id is None
                else Category.parent_id == parent_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.archived_at is None:
            # Повтор запроса не создаёт вторую категорию (A114).
            return CategoryView(
                id=existing.id,
                name=existing.name,
                full_path=await _path_of(session, workspace_id, existing),
                parent_id=existing.parent_id,
                archived=False,
                version=existing.version,
                sort_order=existing.sort_order,
            )
        raise ConflictError(
            "Такая категория есть в архиве: восстановите её или выберите другое название",
            details={"archived_category_id": str(existing.id)},
        )

    max_order = (
        await session.execute(
            select(func.coalesce(func.max(Category.sort_order), 0)).where(
                Category.workspace_id == workspace_id
            )
        )
    ).scalar_one()
    row = Category(
        workspace_id=workspace_id,
        parent_id=parent_id,
        name=display,
        normalized_name=normalized,
        description=description,
        sort_order=int(max_order) + 10,
    )
    session.add(row)
    await session.flush()
    await uow.bump_revisions(workspace_id, catalog=True)
    await uow.emit(
        workspace_id=workspace_id,
        event_type="CategoryCreated",
        aggregate_type="category",
        aggregate_id=row.id,
        payload={"category_id": str(row.id)},
        actor_user_id=actor.user_id,
    )
    return CategoryView(
        id=row.id,
        name=row.name,
        full_path=await _path_of(session, workspace_id, row),
        parent_id=row.parent_id,
        archived=False,
        version=row.version,
        sort_order=row.sort_order,
    )


async def rename_category(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    category_id: uuid.UUID,
    name: str | None = None,
    description: str | None = None,
    parent_id: uuid.UUID | None = None,
    expected_version: int | None = None,
) -> CategoryView:
    """Переименование сохраняет ID и историю (FR-22, A116)."""
    workspace_id = actor.require_workspace()
    row = (
        await session.execute(
            select(Category)
            .where(Category.workspace_id == workspace_id, Category.id == category_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Категория недоступна")
    uow.check_expected_version(row.version, expected_version, label="Категория")

    if name is not None:
        display = clean_display_name(name)
        if not display:
            raise ValidationFailed("Название категории не может быть пустым")
        row.name = display
        row.normalized_name = normalize_name(display)
    if description is not None:
        row.description = description or None
    if parent_id is not None and parent_id != row.parent_id:
        if parent_id == category_id:
            raise ValidationFailed("Категория не может быть собственным родителем")
        await _check_no_cycle(session, workspace_id, category_id, parent_id)
        await _check_depth(session, workspace_id, parent_id)
        row.parent_id = parent_id
    row.version += 1
    await session.flush()
    await uow.bump_revisions(workspace_id, catalog=True)
    await uow.emit(
        workspace_id=workspace_id,
        event_type="CategoryChanged",
        aggregate_type="category",
        aggregate_id=category_id,
        aggregate_revision=row.version,
        payload={"category_id": str(category_id)},
        actor_user_id=actor.user_id,
    )
    return CategoryView(
        id=row.id,
        name=row.name,
        full_path=await _path_of(session, workspace_id, row),
        parent_id=row.parent_id,
        archived=row.archived_at is not None,
        version=row.version,
        sort_order=row.sort_order,
    )


async def _check_no_cycle(
    session: AsyncSession, workspace_id: uuid.UUID, category_id: uuid.UUID, new_parent: uuid.UUID
) -> None:
    """Циклы проверяются рекурсивным обходом под блокировкой бюджета."""
    cursor: uuid.UUID | None = new_parent
    seen: set[uuid.UUID] = set()
    while cursor is not None:
        if cursor == category_id:
            raise ValidationFailed("Такое перемещение создаёт цикл в справочнике")
        if cursor in seen:
            raise ValidationFailed("В справочнике обнаружен цикл")
        seen.add(cursor)
        cursor = (
            await session.execute(
                select(Category.parent_id).where(
                    Category.workspace_id == workspace_id, Category.id == cursor
                )
            )
        ).scalar_one_or_none()


async def removal_preview(
    session: AsyncSession, *, workspace_id: uuid.UUID, category_id: uuid.UUID
) -> RemovalPreview:
    """Показать связи до удаления; данные не меняются (FR-22, CMD-09)."""
    row = (
        await session.execute(
            select(Category).where(
                Category.workspace_id == workspace_id, Category.id == category_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Категория недоступна")
    transaction_count = int(
        (
            await session.execute(
                select(func.count(func.distinct(Allocation.transaction_id))).where(
                    Allocation.workspace_id == workspace_id,
                    Allocation.category_id == category_id,
                )
            )
        ).scalar_one()
    )
    budget_line_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(BudgetLine)
                .where(
                    BudgetLine.workspace_id == workspace_id,
                    BudgetLine.category_id == category_id,
                )
            )
        ).scalar_one()
    )
    child_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Category)
                .where(Category.workspace_id == workspace_id, Category.parent_id == category_id)
            )
        ).scalar_one()
    )
    rule_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ClassificationRule)
                .where(
                    ClassificationRule.workspace_id == workspace_id,
                    ClassificationRule.archived_at.is_(None),
                    ClassificationRule.action["category_id"].astext == str(category_id),
                )
            )
        ).scalar_one()
    )
    scheduled_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ScheduleVersion)
                .where(
                    ScheduleVersion.workspace_id == workspace_id,
                    ScheduleVersion.category_id == category_id,
                )
            )
        ).scalar_one()
    )
    linked = transaction_count + budget_line_count + child_count + rule_count + scheduled_count
    options = ("delete",) if linked == 0 else ("archive", "reassign_and_archive")
    return RemovalPreview(
        category_id=category_id,
        name=row.name,
        transaction_count=transaction_count,
        budget_line_count=budget_line_count,
        child_count=child_count,
        rule_count=rule_count,
        scheduled_count=scheduled_count,
        options=options,
    )


async def remove_category(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    category_id: uuid.UUID,
    option: str,
    reassign_to: uuid.UUID | None = None,
    expected_version: int | None = None,
) -> RemovalPreview:
    """Удалить пустую либо архивировать по принятому варианту (FR-22, A117–A121).

    Каскадное удаление операций запрещено.
    """
    workspace_id = actor.require_workspace()
    preview = await removal_preview(session, workspace_id=workspace_id, category_id=category_id)
    if option not in preview.options:
        raise ConflictError(
            f"Вариант «{option}» недоступен: у категории есть связанные записи",
            details={"available_options": list(preview.options)},
        )
    row = (
        await session.execute(
            select(Category)
            .where(Category.workspace_id == workspace_id, Category.id == category_id)
            .with_for_update()
        )
    ).scalar_one()
    uow.check_expected_version(row.version, expected_version, label="Категория")

    if option == "delete":
        await session.delete(row)
        event = "CategoryRemoved"
    elif option == "reassign_and_archive":
        if reassign_to is None:
            raise ValidationFailed("Для переноса нужна целевая категория")
        if reassign_to == category_id:
            raise ValidationFailed("Нельзя перенести записи в ту же категорию")
        target = (
            await session.execute(
                select(Category).where(
                    Category.workspace_id == workspace_id,
                    Category.id == reassign_to,
                    Category.archived_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if target is None:
            raise NotFound("Целевая категория недоступна")
        moved = await _reassign_allocations(
            session, uow, actor=actor, source=category_id, target=reassign_to
        )
        session.add(
            CategoryMergeMap(
                workspace_id=workspace_id,
                source_category_id=category_id,
                target_category_id=reassign_to,
                merged_by=actor.user_id,
                affected_transactions=moved,
            )
        )
        row.archived_at = func.now()
        row.merged_into_id = reassign_to
        row.version += 1
        event = "CategoryArchived"
    else:
        row.archived_at = func.now()
        row.version += 1
        event = "CategoryArchived"

    await session.flush()
    await uow.bump_revisions(workspace_id, catalog=True, data=option == "reassign_and_archive")
    await uow.emit(
        workspace_id=workspace_id,
        event_type=event,
        aggregate_type="category",
        aggregate_id=category_id,
        payload={"category_id": str(category_id), "option": option},
        actor_user_id=actor.user_id,
    )
    return preview


async def _reassign_allocations(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    source: uuid.UUID,
    target: uuid.UUID,
) -> int:
    """Перенести текущие распределения в другую категорию (FR-22, A122).

    Распределения неизменяемы (ADR-03): перенос создаёт новую ревизию каждой
    затронутой операции, а не переписывает историю. Общий расход остаётся
    прежним — меняется только разрез отчёта.
    """
    from dataclasses import replace

    from fintracker.application.ledger.service import load_current_spec, revise_transaction
    from fintracker.db.models.ledger import Transaction

    workspace_id = actor.require_workspace()
    affected = (
        (
            await session.execute(
                select(Allocation.transaction_id)
                .join(
                    Transaction,
                    (Transaction.workspace_id == Allocation.workspace_id)
                    & (Transaction.id == Allocation.transaction_id)
                    & (Transaction.current_revision == Allocation.revision),
                )
                .where(
                    Allocation.workspace_id == workspace_id,
                    Allocation.category_id == source,
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    for transaction_id in affected:
        _, _, spec = await load_current_spec(
            session, workspace_id=workspace_id, transaction_id=transaction_id
        )
        allocations = tuple(
            replace(item, category_id=target) if item.category_id == source else item
            for item in spec.allocations
        )
        await revise_transaction(
            session,
            uow,
            actor=actor,
            transaction_id=transaction_id,
            new_spec=replace(spec, allocations=allocations),
            expected_version=None,
            change_reason="Перенос записей архивируемой статьи",
        )

    await _merge_budget_lines(session, workspace_id=workspace_id, source=source, target=target)
    return len(affected)


async def _merge_budget_lines(
    session: AsyncSession, *, workspace_id: uuid.UUID, source: uuid.UUID, target: uuid.UUID
) -> None:
    """Объединить строки плана при переносе статьи (FR-22, FR-37).

    Если в той же версии плана уже есть строка целевой статьи с тем же
    получателем, лимиты складываются: общий план периода не меняется и не
    теряется. Незаданный лимит не обнуляет заданный.
    """
    source_lines = (
        (
            await session.execute(
                select(BudgetLine).where(
                    BudgetLine.workspace_id == workspace_id,
                    BudgetLine.category_id == source,
                )
            )
        )
        .scalars()
        .all()
    )
    for line in source_lines:
        existing = (
            await session.execute(
                select(BudgetLine).where(
                    BudgetLine.workspace_id == workspace_id,
                    BudgetLine.budget_version_id == line.budget_version_id,
                    BudgetLine.category_id == target,
                    BudgetLine.beneficiary_id.is_(line.beneficiary_id)
                    if line.beneficiary_id is None
                    else BudgetLine.beneficiary_id == line.beneficiary_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            line.category_id = target
            continue
        if line.limit_minor is not None:
            existing.limit_minor = (existing.limit_minor or 0) + line.limit_minor
        existing.is_protected = existing.is_protected or line.is_protected
        await session.delete(line)
    await session.flush()


async def restore_category(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    category_id: uuid.UUID,
) -> CategoryView:
    """Восстановление возвращает тот же ID (FR-22, A119)."""
    workspace_id = actor.require_workspace()
    row = (
        await session.execute(
            select(Category)
            .where(Category.workspace_id == workspace_id, Category.id == category_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Категория недоступна")
    if row.archived_at is None:
        raise ConflictError("Категория не находится в архиве")
    conflict = (
        await session.execute(
            select(Category.id).where(
                Category.workspace_id == workspace_id,
                Category.normalized_name == row.normalized_name,
                Category.parent_id.is_(row.parent_id)
                if row.parent_id is None
                else Category.parent_id == row.parent_id,
                Category.archived_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if conflict is not None:
        raise ConflictError(
            "Активная категория с таким названием уже существует: выберите другое имя",
            details={"conflicting_category_id": str(conflict)},
        )
    row.archived_at = None
    row.merged_into_id = None
    row.version += 1
    await session.flush()
    await uow.bump_revisions(workspace_id, catalog=True)
    await uow.emit(
        workspace_id=workspace_id,
        event_type="CategoryRestored",
        aggregate_type="category",
        aggregate_id=category_id,
        payload={"category_id": str(category_id)},
        actor_user_id=actor.user_id,
    )
    return CategoryView(
        id=row.id,
        name=row.name,
        full_path=await _path_of(session, workspace_id, row),
        parent_id=row.parent_id,
        archived=False,
        version=row.version,
        sort_order=row.sort_order,
    )
