"""Управление категориями из чата (FR-06, FR-21, FR-22, A113–A122)."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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

if TYPE_CHECKING:
    from fintracker.application.planning.periods import MaterializedPeriod

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
        return [Reply(text="✍️ Напишите «Создай категорию Название».")]
    name = match.group("name").strip(" .,«»\"'")
    return await _create_named_category(settings, actor=actor, workspace=workspace, name=name)


async def _create_named_category(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, name: str
) -> list[Reply]:
    if not name:
        return [
            Reply(text="✍️ Отправьте название категории, например «Путешествия».", retry_input=True)
        ]
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        try:
            view = await create_category(session, uow, actor=actor, name=name)
        except ConflictError as exc:
            return [Reply(text=f"⚠️ {exc.message}", retry_input=True)]
    return [
        Reply(
            text=(
                f"✅ Категория «{view.full_path}» создана\n\nЛимит пока не задан — "
                "его можно добавить сейчас или позже. Прошлые записи остались в своих "
                "категориях."
            ),
            buttons=(
                (
                    Button("💰 Задать лимит", callback("cat", "limit", short(view.id))),
                    Button("🗂 Категории", callback("menu", "categories")),
                ),
            ),
        )
    ]


def _page_buttons(action: str, page: int, count: int, *prefix: str) -> list[tuple[Button, ...]]:
    buttons = []
    if page:
        buttons.append(Button("← Назад", callback("cat", action, *prefix, str(page - 1))))
    if (page + 1) * 8 < count:
        buttons.append(Button("Далее →", callback("cat", action, *prefix, str(page + 1))))
    return [tuple(buttons)] if buttons else []


async def manage_categories(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, page: int = 0
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
    page = min(max(page, 0), max(0, (len(active) - 1) // 8))
    visible = active[page * 8 : (page + 1) * 8]
    lines = [f"🗂 Управление категориями · страница {page + 1}\n"]
    lines.extend(f"• {item.full_path}" for item in visible)
    if archived:
        lines.append(f"В архиве: {len(archived)}")
    rows: list[tuple[Button, ...]] = [
        (
            Button("➕ Добавить", callback("cat", "new")),
            Button("🗃 Архив", callback("cat", "archive")),
        )
    ]
    rows.extend(
        (Button(item.name[:24], callback("cat", "open", short(item.id))),) for item in visible
    )
    rows.extend(_page_buttons("manage", page, len(active)))
    rows.append((Button("← Меню", callback("menu", "main")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def category_card(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, category_id: uuid.UUID
) -> list[Reply]:
    from fintracker.application.conversation.context import current_status
    from fintracker.application.conversation.views import money, plural

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        preview = await removal_preview(session, workspace_id=workspace_id, category_id=category_id)
    status = await current_status(settings, actor=actor, workspace=workspace)
    line = next((item for item in status.lines if item.category_id == category_id), None)
    lines = [f"🗂 {preview.name}", ""]
    if line is not None and line.effective_limit_minor is not None:
        lines.append(
            f"Этот период: {money(line.fact_minor, workspace.currency)} из "
            f"{money(line.effective_limit_minor, workspace.currency)}"
        )
        lines.append(line.status_text)
    elif line is not None:
        lines.append(f"Этот период: {money(line.fact_minor, workspace.currency)}, лимит не задан")
    else:
        lines.append("Лимит не задан")
    count = preview.transaction_count
    lines.append(f"Записей за всё время: {count}")
    if preview.scheduled_count:
        lines.append(f"Плановых платежей: {preview.scheduled_count}")
    if preview.rule_count:
        lines.append(
            f"Сохранённых правил: {preview.rule_count} "
            f"({plural(preview.rule_count, 'слово', 'слова', 'слов')} для автоматического выбора)"
        )
    code = short(category_id)
    rows: list[tuple[Button, ...]] = [
        (
            Button("💰 Задать лимит", callback("cat", "limit", code)),
            Button("✏️ Переименовать", callback("cat", "rename", code)),
        )
    ]
    if "delete" in preview.options:
        lines.append("\nКатегорией ещё не пользовались — её можно удалить.")
        rows.append((Button("🗑 Удалить", callback("cat", "del", code)),))
    elif count:
        lines.append(
            "\nУбрать категорию можно в архив: записи и история сохранятся. "
            "Или сначала перенести записи в другую категорию."
        )
        rows.append(
            (
                Button("🗃 Убрать в архив", callback("cat", "arch", code)),
                Button("↪️ Перенести и убрать", callback("cat", "move", code)),
            )
        )
    else:
        lines.append(
            "\nЗаписей нет, но категория есть в плане. В архиве она перестанет "
            "показываться, а её лимит — учитываться."
        )
        rows.append((Button("🗃 Убрать в архив", callback("cat", "arch", code)),))
    rows.append((Button("← Категории", callback("cat", "manage")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def create_category_from_name(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, name: str
) -> list[Reply]:
    """Название после кнопки «Добавить категорию»: ключевые слова не нужны."""
    return await _create_named_category(
        settings, actor=actor, workspace=workspace, name=name.strip(" .,«»\"'")
    )


async def _next_period(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    period_id: uuid.UUID,
    today: dt.date,
) -> MaterializedPeriod:
    """Resolve the exact next period or reject a stale editor button."""
    from fintracker.application.planning.periods import period_for_date

    current = await period_for_date(session, workspace_id=workspace_id, day=today)
    following = await period_for_date(session, workspace_id=workspace_id, day=current.end_exclusive)
    if following.id != period_id:
        raise ConflictError("Период на кнопке уже не следующий. Откройте план заново")
    return following


async def future_limits_view(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    period_id: uuid.UUID,
    page: int = 0,
) -> list[Reply]:
    """List editable categories for one bound future period (UX-01)."""
    from fintracker.application.delivery.render import format_range
    from fintracker.application.planning.periods import period_for_date
    from fintracker.application.planning.plan import current_budget_version
    from fintracker.db.models.planning import BudgetLine

    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    try:
        async with session_scope(
            settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
        ) as session:
            following = await _next_period(
                session, workspace_id=workspace_id, period_id=period_id, today=today
            )
            categories = await list_categories(session, workspace_id=workspace_id)
            version = await current_budget_version(
                session, workspace_id=workspace_id, period_id=period_id
            )
            if version is None:
                current = await period_for_date(session, workspace_id=workspace_id, day=today)
                version = await current_budget_version(
                    session, workspace_id=workspace_id, period_id=current.id
                )
            limits: dict[uuid.UUID, int | None] = {}
            if version is not None:
                limit_rows = (
                    (
                        await session.execute(
                            select(BudgetLine).where(
                                BudgetLine.workspace_id == workspace_id,
                                BudgetLine.budget_version_id == version.id,
                                BudgetLine.beneficiary_id.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                limits = {row.category_id: row.limit_minor for row in limit_rows}
    except ConflictError as exc:
        return [
            Reply(
                text=f"🔄 {exc.message}",
                buttons=((Button("📅 Открыть следующий план", callback("menu", "nextplan")),),),
            )
        ]

    page = min(max(page, 0), max(0, (len(categories) - 1) // 8))
    visible = categories[page * 8 : (page + 1) * 8]
    period_text = format_range(following.start_date, following.end_exclusive - dt.timedelta(days=1))
    lines = [
        f"💰 Лимиты на {period_text}",
        "",
        "Выберите категорию, лимит которой хотите изменить.",
        "Текущий период останется без изменений.",
    ]
    if len(categories) > 8:
        lines.extend(["", f"Страница {page + 1} из {(len(categories) + 7) // 8}"])
    for category in visible:
        limit = limits.get(category.id)
        value = Money(limit, workspace.currency).format() if limit is not None else "без лимита"
        lines.append(f"• {category.full_path}: {value}")
    rows: list[tuple[Button, ...]] = [
        (
            Button(
                category.name[:24],
                callback("nlimit", "pick", short(period_id), short(category.id)),
            ),
        )
        for category in visible
    ]
    navigation: list[Button] = []
    if page:
        navigation.append(
            Button("← Назад", callback("nlimit", "show", short(period_id), str(page - 1)))
        )
    if (page + 1) * 8 < len(categories):
        navigation.append(
            Button("Далее →", callback("nlimit", "show", short(period_id), str(page + 1)))
        )
    if navigation:
        rows.append(tuple(navigation))
    rows.append((Button("← Следующий план", callback("menu", "nextplan")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def begin_future_limit_input(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    user_id: uuid.UUID,
    period_id: uuid.UUID,
    category_id: uuid.UUID,
) -> list[Reply]:
    """Remember the category and exact future period before asking for an amount."""
    from fintracker.application.conversation.pending import set_pending
    from fintracker.application.delivery.render import format_range
    from fintracker.core.errors import ConflictError

    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    try:
        async with session_scope(
            settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
        ) as session:
            following = await _next_period(
                session, workspace_id=workspace_id, period_id=period_id, today=today
            )
            category = next(
                (
                    item
                    for item in await list_categories(session, workspace_id=workspace_id)
                    if item.id == category_id
                ),
                None,
            )
    except ConflictError as exc:
        return [Reply(text=f"🔄 {exc.message}")]
    if category is None:
        return [Reply(text="🔄 Категория больше недоступна. Откройте план заново.")]
    await set_pending(
        settings,
        user_id=user_id,
        workspace_id=workspace_id,
        kind="category_limit",
        payload={
            "category_id": str(category_id),
            "target_period_id": str(period_id),
        },
    )
    period_text = format_range(following.start_date, following.end_exclusive - dt.timedelta(days=1))
    return [
        Reply(
            text=(
                f"💰 Новый лимит «{category.full_path}»\n\n"
                f"Период: {period_text}\n\n"
                "Отправьте сумму числом, например 8000."
            )
        )
    ]


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
            return [Reply(text=f"⚠️ {exc.message}")]
    text = {
        "delete": f"🗑 Категория «{preview.name}» удалена.",
        "archive": (
            f"🗃 Категория «{preview.name}» убрана в архив.\n\nРасходы и план остались в отчётах."
        ),
        "reassign_and_archive": (
            f"✅ Записи перенесены\n\nКатегория «{preview.name}» убрана в архив. "
            "Общий расход не изменился."
        ),
    }[option]
    return [Reply(text=text, buttons=((Button("🗂 Категории", callback("menu", "categories")),),))]


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
            return [Reply(text=f"⚠️ {exc.message}")]
    return [
        Reply(
            text=f"✅ Категория «{view.full_path}» восстановлена из архива.",
            buttons=((Button("🗂 Категории", callback("menu", "categories")),),),
        )
    ]


async def choose_reassign_target(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    category_id: uuid.UUID,
    page: int = 0,
) -> list[Reply]:
    """Выбрать категорию, куда переносятся записи перед архивом (FR-22, A120).

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
    others = [item for item in categories if item.id != category_id]
    page = min(max(page, 0), max(0, (len(others) - 1) // 8))
    if not others:
        return [
            Reply(
                text="ℹ️ Переносить записи некуда: в бюджете нет другой активной категории.",
                buttons=((Button("← Категории", callback("cat", "manage")),),),
            )
        ]
    lines = [
        f"↪️ Куда перенести записи категории «{preview.name}»?",
        "",
        f"Записей: {preview.transaction_count}. После переноса категория уйдёт в архив.",
    ]
    if preview.rule_count:
        lines.append(
            f"Сохранённых правил: {preview.rule_count} — они перестанут работать, новые "
            "записи сюда не попадут."
        )
    if preview.scheduled_count:
        lines.append(
            f"Плановых платежей: {preview.scheduled_count} — выберите для них категорию "
            "в разделе «Платежи»."
        )
    source = short(category_id)
    rows: list[tuple[Button, ...]] = [
        (Button(item.name[:24], callback("cat", "mv", source, short(item.id))),)
        for item in others[page * 8 : (page + 1) * 8]
    ]
    rows.extend(_page_buttons("move", page, len(others), source))
    rows.append((Button("← Категория", callback("cat", "open", source)),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def archived_categories(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, page: int = 0
) -> list[Reply]:
    """Архив категорий с возможностью восстановления (FR-22, G-13)."""
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
                text=(
                    "🗃 Архив пуст\n\nЗдесь появятся категории, которые вы уберёте из "
                    "активного списка."
                ),
                buttons=((Button("← Категории", callback("cat", "manage")),),),
            )
        ]
    page = min(max(page, 0), max(0, (len(archived) - 1) // 8))
    visible = archived[page * 8 : (page + 1) * 8]
    lines = [f"🗃 Архив категорий · страница {page + 1}\n"]
    lines.extend(f"• {item.full_path}" for item in visible)
    rows: list[tuple[Button, ...]] = [
        (Button(f"Вернуть {item.name[:16]}", callback("cat", "restore", short(item.id))),)
        for item in visible
    ]
    rows.extend(_page_buttons("archive", page, len(archived)))
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
    """Применить новое название категории из ответа участника (FR-22, G-13)."""
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
            return [Reply(text=f"⚠️ {exc.message}")]
    return [
        Reply(
            text=f"✅ Категория переименована: «{view.full_path}».\n\nЗаписи и лимит сохранены.",
            buttons=((Button("🗂 Категории", callback("menu", "categories")),),),
        )
    ]


async def apply_pending_limit(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    category_id: uuid.UUID,
    text: str,
    target_period_id: uuid.UUID | None = None,
) -> list[Reply]:
    """Apply a category limit to the current or explicitly bound future period."""
    from fintracker.application.delivery.render import format_range
    from fintracker.application.planning.periods import period_for_date
    from fintracker.application.planning.plan import (
        PlanLineSpec,
        change_line_limit,
        create_budget_version,
        current_budget_version,
    )
    from fintracker.db.models.planning import BudgetLine
    from fintracker.domain.parsing.amounts import parse_amounts

    amounts = parse_amounts(text)
    if not amounts:
        return [
            Reply(
                text="✍️ Уточните лимит\n\nОтправьте сумму числом, например 8000.", retry_input=True
            )
        ]
    limit = Money.from_decimal(Decimal(amounts[0].value), workspace.currency)

    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        current = await period_for_date(session, workspace_id=workspace_id, day=today)
        future_edit = target_period_id is not None
        if target_period_id is None:
            period = current
        else:
            try:
                period = await _next_period(
                    session,
                    workspace_id=workspace_id,
                    period_id=target_period_id,
                    today=today,
                )
            except ConflictError as exc:
                return [Reply(text=f"🔄 {exc.message}")]
        version = await current_budget_version(
            session, workspace_id=workspace_id, period_id=period.id
        )
        if version is None and future_edit:
            # The next-plan preview uses the current plan as its source until an
            # individual future plan exists. Materialize exactly that basis,
            # keeping a baseline before applying the requested edit.
            source_version = await current_budget_version(
                session, workspace_id=workspace_id, period_id=current.id
            )
            source_rows = (
                (
                    await session.execute(
                        select(BudgetLine).where(
                            BudgetLine.workspace_id == workspace_id,
                            BudgetLine.budget_version_id == source_version.id,
                        )
                    )
                )
                .scalars()
                .all()
                if source_version is not None
                else []
            )
            source_payload = [
                PlanLineSpec(
                    category_id=row.category_id,
                    beneficiary_id=row.beneficiary_id,
                    limit_minor=row.limit_minor,
                    rollover_mode=row.rollover_mode,
                    is_protected=row.is_protected,
                    stable_line_id=row.stable_line_id,
                )
                for row in source_rows
            ]
            await create_budget_version(
                session,
                workspace_id=workspace_id,
                period_id=period.id,
                kind="baseline",
                plan_status="approved",
                origin="manual",
                lines=source_payload,
                overall_limit_minor=(
                    source_version.overall_limit_minor if source_version is not None else None
                ),
                approved_by=actor.user_id,
                reason="Основа индивидуального плана будущего периода",
            )
            version = await create_budget_version(
                session,
                workspace_id=workspace_id,
                period_id=period.id,
                kind="working",
                plan_status="approved",
                origin="manual",
                lines=source_payload,
                overall_limit_minor=(
                    source_version.overall_limit_minor if source_version is not None else None
                ),
                approved_by=actor.user_id,
                reason="Основа индивидуального плана будущего периода",
            )
        if version is None:
            return [Reply(text="✅ План периода ещё не создан: задайте лимит через «Бюджет».")]
        version_rows = (
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
        line = next(
            (
                row
                for row in version_rows
                if row.category_id == category_id and row.beneficiary_id is None
            ),
            None,
        )
        if future_edit:
            payload = [
                PlanLineSpec(
                    category_id=row.category_id,
                    beneficiary_id=row.beneficiary_id,
                    limit_minor=(
                        limit.minor
                        if row.category_id == category_id and row.beneficiary_id is None
                        else row.limit_minor
                    ),
                    rollover_mode=row.rollover_mode,
                    is_protected=row.is_protected,
                    stable_line_id=row.stable_line_id,
                )
                for row in version_rows
            ]
            if line is None:
                payload.append(
                    PlanLineSpec(
                        category_id=category_id,
                        beneficiary_id=None,
                        limit_minor=limit.minor,
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
                reason="Изменение лимита выбранного будущего периода",
            )
            await uow.bump_revisions(workspace_id, plan=True)
        elif line is None:
            payload = [
                PlanLineSpec(
                    category_id=row.category_id,
                    beneficiary_id=row.beneficiary_id,
                    limit_minor=row.limit_minor,
                    rollover_mode=row.rollover_mode,
                    is_protected=row.is_protected,
                    stable_line_id=row.stable_line_id,
                )
                for row in version_rows
            ]
            payload.append(
                PlanLineSpec(
                    category_id=category_id,
                    beneficiary_id=None,
                    limit_minor=limit.minor,
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
                reason="Добавление лимита новой категории в текущий период",
            )
            await uow.bump_revisions(workspace_id, plan=True)
        else:
            try:
                await change_line_limit(
                    session,
                    uow,
                    actor=actor,
                    period_id=period.id,
                    stable_line_id=line.stable_line_id,
                    new_limit_minor=limit.minor,
                    expected_version=version.version,
                )
            except DomainError as exc:
                return [Reply(text=f"⚠️ {exc.message}", retry_input=True)]
    if future_edit:
        period_text = format_range(period.start_date, period.end_exclusive - dt.timedelta(days=1))
        return [
            Reply(
                text=(
                    f"✅ Лимит на {period_text} обновлён: {limit.format()}.\n\n"
                    "Текущий период не изменился."
                ),
                buttons=(
                    (
                        Button(
                            "💰 Другие лимиты",
                            callback("nlimit", "show", short(period.id), "0"),
                        ),
                    ),
                    (Button("📅 Следующий план", callback("menu", "nextplan")),),
                ),
            )
        ]
    return [
        Reply(
            text=f"✅ Лимит категории обновлён: {limit.format()}.",
            buttons=((Button("🗂 Категории", callback("menu", "categories")),),),
        )
    ]
