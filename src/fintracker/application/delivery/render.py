"""Тексты уведомлений (FR-86, FR-52, FR-09).

Содержимое перечитывается под проверенным контекстом получателя; событие
хранит только ID и версию.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.core.money import Money
from fintracker.db.models.access import Beneficiary, Person, User, Workspace
from fintracker.db.models.catalog import Category
from fintracker.db.models.ledger import Allocation, Transaction, TransactionRevision

RU_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def format_date(value: dt.date, *, with_year: bool = False) -> str:
    """Даты без двусмысленности (FR-09)."""
    base = f"{value.day} {RU_MONTHS[value.month - 1]}"
    return f"{base} {value.year}" if with_year else base


def format_range(start: dt.date, end_inclusive: dt.date) -> str:
    same_year = start.year == end_inclusive.year
    left = format_date(start, with_year=not same_year)
    right = (
        format_date(end_inclusive, with_year=True) if not same_year else format_date(end_inclusive)
    )
    return f"{left} — {right}"


async def _actor_name(session: AsyncSession, user_id: uuid.UUID | None) -> str:
    if user_id is None:
        return "Система"
    person = (
        (await session.execute(select(Person.name).where(Person.user_id == user_id)))
        .scalars()
        .first()
    )
    if person:
        return str(person)
    telegram_id = (
        await session.execute(select(User.telegram_user_id).where(User.id == user_id))
    ).scalar_one_or_none()
    return f"Участник {telegram_id}" if telegram_id else "Участник"


async def _transaction_summary(
    session: AsyncSession, *, workspace: Workspace, transaction_id: uuid.UUID
) -> tuple[str, TransactionRevision] | None:
    transaction = (
        await session.execute(
            select(Transaction).where(
                Transaction.workspace_id == workspace.id, Transaction.id == transaction_id
            )
        )
    ).scalar_one_or_none()
    if transaction is None:
        return None
    revision = (
        await session.execute(
            select(TransactionRevision).where(
                TransactionRevision.workspace_id == workspace.id,
                TransactionRevision.transaction_id == transaction_id,
                TransactionRevision.revision == transaction.current_revision,
            )
        )
    ).scalar_one_or_none()
    if revision is None:
        return None
    rows = (
        await session.execute(
            select(Allocation.amount_minor, Category.name, Beneficiary.name)
            .outerjoin(
                Category,
                (Category.workspace_id == Allocation.workspace_id)
                & (Category.id == Allocation.category_id),
            )
            .outerjoin(
                Beneficiary,
                (Beneficiary.workspace_id == Allocation.workspace_id)
                & (Beneficiary.id == Allocation.beneficiary_id),
            )
            .where(
                Allocation.workspace_id == workspace.id,
                Allocation.transaction_id == transaction_id,
                Allocation.revision == revision.revision,
            )
        )
    ).all()
    amount = Money(revision.amount_minor, revision.currency)
    parts: list[str] = []
    for row in rows:
        label = row[1] or "Без категории"
        if row[2]:
            label = f"{label} · для {row[2]}"
        if len(rows) > 1:
            label = f"{label} — {Money(row[0], revision.currency).format()}"
        parts.append(label)
    detail = "; ".join(parts) if parts else "Без категории"
    return f"{amount.format()} — {detail}", revision


async def render_event(
    session: AsyncSession,
    *,
    workspace: Workspace,
    event_type: str,
    payload: dict[str, Any],
    recipient_user_id: uuid.UUID,
) -> tuple[str | None, list[list[dict[str, str]]] | None]:
    """Текст и кнопки уведомления; None означает «нечего показывать»."""
    header = workspace.name

    if event_type in {"TransactionPosted", "TransactionRevised", "TransactionVoided"}:
        raw_id = payload.get("transaction_id")
        if not raw_id:
            return None, None
        summary = await _transaction_summary(
            session, workspace=workspace, transaction_id=uuid.UUID(str(raw_id))
        )
        if summary is None:
            return None, None
        text_body, revision = summary
        author = await _actor_name(session, revision.changed_by)
        verb = {
            "TransactionPosted": "добавил расход",
            "TransactionRevised": "исправил запись",
            "TransactionVoided": "отменил запись",
        }[event_type]
        lines = [
            header,
            f"{author} {verb}: {text_body}",
            f"Дата: {format_date(revision.occurred_date, with_year=True)}",
        ]
        if revision.note:
            preview = revision.note.strip().splitlines()[0][:120]
            lines.append(f"Комментарий: {preview}")
        buttons = [
            [
                {"text": "Открыть операцию", "callback_data": f"tx:{raw_id}"},
                {"text": "Бюджет", "callback_data": "menu:budget"},
            ]
        ]
        return "\n".join(lines), buttons

    if event_type == "BudgetPeriodOpened":
        start = dt.date.fromisoformat(str(payload["start_date"]))
        end = dt.date.fromisoformat(str(payload["end_inclusive"]))
        return (
            f"{header}\nОткрыт новый период: {format_range(start, end)}",
            [[{"text": "Открыть бюджет", "callback_data": "menu:budget"}]],
        )

    if event_type == "BudgetPeriodEnded":
        completeness = {
            "incomplete": "учёт отмечен неполным",
            "reconciled_source": "сверено по доступному источнику",
            "confirmed_complete": "полнота подтверждена участником",
        }.get(str(payload.get("completeness")), "полнота не подтверждена")
        return (
            f"{header}\nПериод завершён. Полнота: {completeness}.",
            [[{"text": "Итог периода", "callback_data": "menu:report"}]],
        )

    if event_type in {"MemberJoined", "MemberLeft", "MemberRemoved", "AdminTransferred"}:
        actor = await _actor_name(
            session, payload.get("user_id") and uuid.UUID(str(payload["user_id"]))
        )
        verb = {
            "MemberJoined": "присоединился к бюджету",
            "MemberLeft": "вышел из бюджета",
            "MemberRemoved": "исключён из бюджета",
            "AdminTransferred": "стал администратором бюджета",
        }[event_type]
        return f"{header}\n{actor} {verb}.", None

    if event_type == "BudgetDeletionRequested":
        # Служебное терминальное сообщение без пересылки финансовых данных.
        return f"Бюджет «{workspace.name}» удалён администратором.", None

    if event_type in {"CategoryCreated", "CategoryChanged", "CategoryArchived", "CategoryRestored"}:
        raw_id = payload.get("category_id")
        name = (
            (
                await session.execute(
                    select(Category.name).where(
                        Category.workspace_id == workspace.id,
                        Category.id == uuid.UUID(str(raw_id)),
                    )
                )
            ).scalar_one_or_none()
            if raw_id
            else None
        )
        if name is None:
            return None, None
        verb = {
            "CategoryCreated": "добавлена категория",
            "CategoryChanged": "изменена категория",
            "CategoryArchived": "категория убрана в архив",
            "CategoryRestored": "категория восстановлена",
        }[event_type]
        return f"{header}\n{verb.capitalize()}: {name}", None

    if event_type == "PaymentReminder":
        # Актуальность проверяется в момент отправки: оплаченный или отменённый
        # платёж не напоминается (FR-53, A61).
        from fintracker.db.models.commitments import Occurrence, ScheduledItem

        raw_id = payload.get("occurrence_id")
        if not raw_id:
            return None, None
        row = (
            await session.execute(
                select(Occurrence, ScheduledItem.name)
                .join(
                    ScheduledItem,
                    (ScheduledItem.workspace_id == Occurrence.workspace_id)
                    & (ScheduledItem.id == Occurrence.schedule_id),
                )
                .where(
                    Occurrence.workspace_id == workspace.id,
                    Occurrence.id == uuid.UUID(str(raw_id)),
                )
            )
        ).one_or_none()
        if row is None:
            return None, None
        occurrence, name = row
        if occurrence.state not in {"planned", "partially_settled"}:
            return None, None
        remaining = (occurrence.expected_minor or 0) - occurrence.settled_minor
        amount = (
            Money(remaining, workspace.currency).format()
            if occurrence.expected_minor is not None
            else "сумма не задана"
        )
        lines = [
            workspace.name,
            f"Плановый платёж: {name}",
            f"Срок: {format_date(occurrence.due_date, with_year=True)} · {amount}",
            "Это ожидаемый платёж, а не проведённый расход.",
        ]
        buttons = [
            [
                {"text": "Оплачено", "callback_data": f"pay:done:{occurrence.id.hex[:16]}"},
                {"text": "Перенести", "callback_data": f"pay:move:{occurrence.id.hex[:16]}"},
            ],
            [{"text": "Пропустить", "callback_data": f"pay:skip:{occurrence.id.hex[:16]}"}],
        ]
        return "\n".join(lines), buttons

    if event_type == "PlanReviewDue":
        end = dt.date.fromisoformat(str(payload["end_inclusive"]))
        return (
            f"{header}\nПериод заканчивается {format_date(end, with_year=True)}.\n"
            "Проверьте план следующего периода: суммы не меняются без вашего решения.",
            [
                [
                    {"text": "План на следующий", "callback_data": "menu:nextplan"},
                    {"text": "Итог периода", "callback_data": "menu:summary"},
                ]
            ],
        )

    if event_type == "ImportCommitted":
        # Одна сводка вместо рассылки по каждой импортированной строке (A63).
        rows = int(payload.get("rows") or 0)
        return (
            f"{header}\nИмпорт завершён: перенесено записей — {rows}.\n"
            "Предупреждения по прошлым периодам не рассылаются.",
            [[{"text": "Открыть бюджет", "callback_data": "menu:budget"}]],
        )

    if event_type == "ThresholdCrossed":
        return str(payload.get("text") or ""), None

    if event_type == "AnalysisCompleted":
        return str(payload.get("text") or ""), None

    return None, None
