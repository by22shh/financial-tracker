"""Разделы интерфейса: обзор, категории, история, участники, настройки."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import select

from fintracker.application.conversation import views
from fintracker.application.conversation.context import (
    author_names,
    category_paths,
    current_status,
)
from fintracker.application.conversation.entry import (
    CandidateFields,
    load_draft,
    post_draft,
)
from fintracker.application.conversation.keyboards import (
    Button,
    callback,
    short,
    start_menu,
    transaction_card,
)
from fintracker.application.conversation.types import Reply
from fintracker.application.identity.actor import list_budgets
from fintracker.application.planning.periods import period_for_date
from fintracker.application.planning.plan import line_key, period_status
from fintracker.config import Settings
from fintracker.core.context import ActorContext, Role
from fintracker.core.errors import NotFound
from fintracker.core.money import Money
from fintracker.db.models.access import Beneficiary, Person, Workspace
from fintracker.db.models.ledger import Allocation, Transaction, TransactionRevision
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork


async def list_budgets_reply(settings: Settings, *, user_id: uuid.UUID) -> list[Reply]:
    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        budgets = await list_budgets(session, user_id)
    if not budgets:
        return [Reply(text=views.empty_state("budgets"), buttons=start_menu(returning=False))]
    rows: list[tuple[Button, ...]] = []
    lines = ["Мои бюджеты:"]
    for item in budgets:
        marker = " ← активный" if item.is_active_context else ""
        lines.append(f"• {item.name} · {item.role.value} · ID {item.short_id}{marker}")
        rows.append((Button(item.name, callback("ws", "use", short(item.workspace_id))),))
    rows.append(
        (
            Button("Создать бюджет", callback("wiz", "start")),
            Button("Войти по коду", callback("join", "start")),
        )
    )
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def budget_overview(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    status = await current_status(settings, actor=actor, workspace=workspace)
    return [
        Reply(
            text=views.budget_overview(status, workspace_name=workspace.name),
            buttons=(
                (
                    Button("Все категории", callback("menu", "categories")),
                    Button("Как посчитано", callback("budget", "explain")),
                ),
                (
                    Button("Прошлые периоды", callback("budget", "past")),
                    Button("Следующий период", callback("budget", "next")),
                ),
            ),
        )
    ]


async def categories_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, page: int = 0
) -> list[Reply]:
    status = await current_status(settings, actor=actor, workspace=workspace)
    body, has_more = views.category_lines(status, page=page)
    rows: list[tuple[Button, ...]] = [
        (
            Button("Добавить категорию", callback("cat", "new")),
            Button("Управление", callback("cat", "manage")),
        )
    ]
    if has_more:
        rows.append((Button("Показать ещё", callback("cat", "page", str(page + 1))),))
    rows.append((Button("← Меню", callback("menu", "main")),))
    return [Reply(text=f"Статьи бюджета:\n{body}", buttons=tuple(rows))]


async def history_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, limit: int = 8
) -> list[Reply]:
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        rows = (
            await session.execute(
                select(
                    Transaction.id,
                    TransactionRevision.amount_minor,
                    TransactionRevision.currency,
                    TransactionRevision.occurred_date,
                    TransactionRevision.note,
                    Transaction.status,
                    Transaction.created_by,
                )
                .join(
                    TransactionRevision,
                    (TransactionRevision.workspace_id == Transaction.workspace_id)
                    & (TransactionRevision.transaction_id == Transaction.id)
                    & (TransactionRevision.revision == Transaction.current_revision),
                )
                .where(Transaction.workspace_id == workspace_id)
                .order_by(Transaction.occurred_sort_date.desc(), Transaction.id.desc())
                .limit(limit)
            )
        ).all()
        if not rows:
            from fintracker.application.conversation.keyboards import main_menu

            return [Reply(text=views.empty_state("history"), buttons=main_menu())]
        paths = await category_paths(session, workspace_id=workspace_id)
        allocation_rows = (
            await session.execute(
                select(Allocation.transaction_id, Allocation.category_id)
                .join(
                    Transaction,
                    (Transaction.workspace_id == Allocation.workspace_id)
                    & (Transaction.id == Allocation.transaction_id)
                    & (Transaction.current_revision == Allocation.revision),
                )
                .where(
                    Allocation.workspace_id == workspace_id,
                    Allocation.transaction_id.in_([row[0] for row in rows]),
                )
            )
        ).all()
        by_transaction: dict[uuid.UUID, list[uuid.UUID | None]] = {}
        for transaction_id, category_id in allocation_rows:
            by_transaction.setdefault(transaction_id, []).append(category_id)
        authors = await author_names(session, workspace_id=workspace_id)

    lines = ["Последние операции:"]
    for row in rows:
        category_ids = by_transaction.get(row[0], [])
        if len(category_ids) > 1:
            path = f"{len(category_ids)} статей"
        elif category_ids and category_ids[0]:
            path = paths.get(category_ids[0], "Без категории")
        else:
            path = "Без категории"
        lines.append(
            views.history_line(
                transaction_id=row[0],
                amount_minor=row[1],
                currency=row[2],
                occurred_date=row[3],
                category_path=path,
                author=authors.get(row[6]),
                is_voided=row[5] == "voided",
                has_note=bool(row[4]),
            )
        )
    return [
        Reply(
            text="\n".join(lines),
            buttons=(
                (
                    Button("Фильтры", callback("hist", "filters")),
                    Button("Экспорт", callback("menu", "io")),
                ),
                (Button("← Меню", callback("menu", "main")),),
            ),
        )
    ]


async def members_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    from fintracker.application.identity.membership import list_members

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        members = await list_members(session, workspace_id=workspace_id)
    lines = [f"Участники бюджета «{workspace.name}»:"]
    for member in members:
        label = member.display_name
        role = "администратор" if member.role is Role.ADMIN else "участник"
        lines.append(f"• {label} — {role}")
    rows: list[tuple[Button, ...]] = []
    if actor.is_admin:
        rows.append((Button("Пригласить", callback("inv", "new")),))
    rows.append((Button("Выйти из бюджета", callback("ws", "leave")),))
    rows.append((Button("← Меню", callback("menu", "main")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def settings_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    from fintracker.application.planning.periods import latest_policy, policy_from_row

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        policy_row = await latest_policy(session, workspace_id)
        policy = policy_from_row(policy_row)
        upcoming = policy.preview(4)
    first = upcoming[0]
    lines = [
        f"Настройки бюджета «{workspace.name}»",
        f"Валюта: {workspace.currency} · Часовой пояс: {workspace.timezone}",
        f"Период: {views.format_range(first.start, first.end_inclusive)}",
        f"Повторять: {policy.describe()}",
        "Далее:",
    ]
    lines.extend(
        f"       {views.format_range(item.start, item.end_inclusive)}" for item in upcoming[1:]
    )
    rows: list[tuple[Button, ...]] = [(Button("Мои настройки", callback("set", "personal")),)]
    if actor.is_admin:
        rows.append((Button("Период и повторение", callback("set", "period")),))
        rows.append((Button("Удалить бюджет", callback("ws", "delete")),))
    rows.append((Button("← Меню", callback("menu", "main")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def transaction_card_reply(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    transaction_id: uuid.UUID,
) -> list[Reply]:
    """Карточка операции с остатком по статье (раздел 6.2 ТЗ)."""
    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        transaction = (
            await session.execute(
                select(Transaction).where(
                    Transaction.workspace_id == workspace_id, Transaction.id == transaction_id
                )
            )
        ).scalar_one_or_none()
        if transaction is None:
            raise NotFound("Операция недоступна")
        revision = (
            await session.execute(
                select(TransactionRevision).where(
                    TransactionRevision.workspace_id == workspace_id,
                    TransactionRevision.transaction_id == transaction_id,
                    TransactionRevision.revision == transaction.current_revision,
                )
            )
        ).scalar_one()
        allocations = (
            (
                await session.execute(
                    select(Allocation).where(
                        Allocation.workspace_id == workspace_id,
                        Allocation.transaction_id == transaction_id,
                        Allocation.revision == revision.revision,
                    )
                )
            )
            .scalars()
            .all()
        )
        paths = await category_paths(session, workspace_id=workspace_id)
        authors = await author_names(session, workspace_id=workspace_id)
        beneficiary_name = None
        if allocations and allocations[0].beneficiary_id:
            beneficiary_name = (
                await session.execute(
                    select(Beneficiary.name).where(
                        Beneficiary.workspace_id == workspace_id,
                        Beneficiary.id == allocations[0].beneficiary_id,
                    )
                )
            ).scalar_one_or_none()
        spender_name = None
        if revision.spender_person_id:
            spender_name = (
                await session.execute(
                    select(Person.name).where(
                        Person.workspace_id == workspace_id,
                        Person.id == revision.spender_person_id,
                    )
                )
            ).scalar_one_or_none()
        period = await period_for_date(
            session, workspace_id=workspace_id, day=revision.occurred_date
        )
        status = await period_status(
            session,
            workspace_id=workspace_id,
            period_id=period.id,
            currency=workspace.currency,
            today=today,
        )
        created_by = transaction.created_by
        transaction_status = transaction.status
        amount_minor = revision.amount_minor
        currency = revision.currency
        occurred_date = revision.occurred_date
        note = revision.note

    line = None
    if len(allocations) == 1 and allocations[0].category_id:
        key = line_key(allocations[0].category_id, allocations[0].beneficiary_id)
        line = next(
            (
                item
                for item in status.lines
                if line_key(item.category_id, item.beneficiary_id) == key
            ),
            None,
        )
    if len(allocations) > 1:
        category_path = f"{len(allocations)} статей"
    elif allocations and allocations[0].category_id:
        category_path = paths.get(allocations[0].category_id, "Без категории")
    else:
        category_path = "Без категории"

    text = views.transaction_card(
        workspace_name=workspace.name,
        amount_minor=amount_minor,
        currency=currency,
        category_path=category_path,
        beneficiary=beneficiary_name,
        spender=spender_name,
        account=None,
        occurred_date=occurred_date,
        period=(period.start_date, period.end_exclusive - dt.timedelta(days=1)),
        line=line,
        author_name=authors.get(created_by) if created_by != actor.user_id else None,
        note=note,
        is_voided=transaction_status == "voided",
    )
    return [Reply(text=text, buttons=transaction_card(transaction_id))]


async def confirm_draft(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    draft_id: uuid.UUID,
    origin: str,
) -> list[Reply]:
    """Провести подтверждённый черновик и показать карточку (FR-11)."""
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id)
        draft, candidates = await load_draft(
            session, workspace_id=workspace_id, draft_id=draft_id, owner_id=actor.user_id
        )
        if draft.state in {"cancelled", "expired"}:
            raise NotFound("Черновик больше не активен")
        posted = await post_draft(
            session,
            uow,
            actor=actor,
            draft=draft,
            candidates=candidates,
            timezone=workspace.timezone,
            workspace_currency=workspace.currency,
            origin=origin,
        )
        if posted:
            # Пороговые события пересчитываются под той же блокировкой (FR-52).
            from fintracker.application.delivery.thresholds import evaluate_thresholds
            from fintracker.application.planning.periods import period_for_date

            today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
            period = await period_for_date(session, workspace_id=workspace_id, day=today)
            await evaluate_thresholds(
                session,
                uow,
                workspace=workspace,
                period_id=period.id,
                today=today,
            )
    if not posted:
        return [Reply(text="Нечего записывать: все кандидаты исключены.")]
    if len(posted) == 1:
        return await transaction_card_reply(
            settings, actor=actor, workspace=workspace, transaction_id=posted[0]
        )
    total = await batch_total(settings, actor=actor, transaction_ids=posted)
    return [
        Reply(
            text=(
                f"Записано операций: {len(posted)} на {total}.\n"
                "Откройте список, чтобы проверить каждую."
            ),
            buttons=((Button(f"Открыть {len(posted)} записи", callback("menu", "history")),),),
        )
    ]


async def batch_total(
    settings: Settings, *, actor: ActorContext, transaction_ids: list[uuid.UUID]
) -> str:
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        rows = (
            await session.execute(
                select(TransactionRevision.amount_minor, TransactionRevision.currency)
                .join(
                    Transaction,
                    (Transaction.workspace_id == TransactionRevision.workspace_id)
                    & (Transaction.id == TransactionRevision.transaction_id)
                    & (Transaction.current_revision == TransactionRevision.revision),
                )
                .where(
                    TransactionRevision.workspace_id == workspace_id,
                    TransactionRevision.transaction_id.in_(transaction_ids),
                )
            )
        ).all()
    if not rows:
        return "0"
    currency = rows[0][1]
    return Money(sum(row[0] for row in rows), currency).format()


def draft_summary(candidates: Sequence[CandidateFields], currency: str) -> str:
    lines: list[str] = []
    for index, candidate in enumerate(candidates, start=1):
        amount = (
            Money(candidate.amount_minor, candidate.currency or currency).format()
            if candidate.amount_minor is not None
            else "сумма неизвестна"
        )
        date_part = (
            views.format_date(candidate.occurred_date, with_year=True)
            if candidate.occurred_date
            else "дата неизвестна"
        )
        description = candidate.description or "без описания"
        prefix = f"{index}. " if len(candidates) > 1 else ""
        lines.append(f"{prefix}{amount} · {description} · {date_part}")
        if candidate.note:
            lines.append(f"   Комментарий: {candidate.note}")
    return "\n".join(lines)
