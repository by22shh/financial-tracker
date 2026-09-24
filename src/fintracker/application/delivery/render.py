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
from fintracker.db.models.access import Beneficiary, Membership, Person, Workspace
from fintracker.db.models.catalog import Account, Category
from fintracker.db.models.ledger import Allocation, CashLeg, Transaction, TransactionRevision

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


def _transaction_event_title(transaction_type: str, event_type: str) -> str:
    forms = {
        "expense": ("Расход", "записан", "изменён", "отменён"),
        "income": ("Доход", "записан", "изменён", "отменён"),
        "refund": ("Возврат", "записан", "изменён", "отменён"),
        "transfer": ("Перевод", "записан", "изменён", "отменён"),
        "mixed_payment": ("Покупка", "записана", "изменена", "отменена"),
        "external_funding": ("Пополнение", "записано", "изменено", "отменено"),
        "loan_received": ("Получение займа", "записано", "изменено", "отменено"),
        "loan_principal_payment": (
            "Платёж по займу",
            "записан",
            "изменён",
            "отменён",
        ),
        "receivable_settlement": ("Возврат долга", "записан", "изменён", "отменён"),
        "adjustment": ("Корректировка", "записана", "изменена", "отменена"),
    }
    kind, posted, revised, voided = forms.get(
        transaction_type,
        ("Операция", "записана", "изменена", "отменена"),
    )
    icon, state = {
        "TransactionPosted": ("✅", posted),
        "TransactionRevised": ("✏️", revised),
        "TransactionVoided": ("↩️", voided),
    }[event_type]
    return f"{icon} {kind} {state}"


async def _actor_name(
    session: AsyncSession, user_id: uuid.UUID | None, *, workspace_id: uuid.UUID
) -> str:
    """Имя участника в этом бюджете; Telegram ID другим не показывается (FR-04)."""
    if user_id is None:
        return "Бот"
    person = (
        (
            await session.execute(
                select(Person.name)
                .join(
                    Membership,
                    (Membership.workspace_id == Person.workspace_id)
                    & (Membership.person_id == Person.id),
                )
                .where(Membership.workspace_id == workspace_id, Membership.user_id == user_id)
            )
        )
        .scalars()
        .first()
    )
    return str(person) if person else "Участник бюджета"


async def _transaction_summary(
    session: AsyncSession, *, workspace: Workspace, transaction_id: uuid.UUID
) -> tuple[str, TransactionRevision, str | None] | None:
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
    cash_rows = (
        await session.execute(
            select(CashLeg.signed_minor, Account.name)
            .outerjoin(
                Account,
                (Account.workspace_id == CashLeg.workspace_id) & (Account.id == CashLeg.account_id),
            )
            .where(
                CashLeg.workspace_id == workspace.id,
                CashLeg.transaction_id == transaction_id,
                CashLeg.revision == revision.revision,
            )
        )
    ).all()
    outgoing = next((name for signed, name in cash_rows if signed < 0 and name), None)
    incoming = next((name for signed, name in cash_rows if signed > 0 and name), None)
    account_flow = f"{outgoing} → {incoming}" if outgoing and incoming else None
    text = (
        amount.format()
        if revision.transaction_type == "transfer"
        else f"{amount.format()} — {detail}"
    )
    return text, revision, account_flow


# Кнопки уведомления открывают раздел новым сообщением: само уведомление
# (напоминание, обзор, предупреждение) остаётся в чате.
KEEP_SOURCE_PREFIX = "+"


async def render_event(
    session: AsyncSession,
    *,
    workspace: Workspace,
    event_type: str,
    payload: dict[str, Any],
    recipient_user_id: uuid.UUID,
) -> tuple[str | None, list[list[dict[str, str]]] | None]:
    """Текст и кнопки уведомления; None означает «нечего показывать»."""
    text, buttons = await _render_event(
        session,
        workspace=workspace,
        event_type=event_type,
        payload=payload,
        recipient_user_id=recipient_user_id,
    )
    if buttons:
        buttons = [
            [
                {
                    **button,
                    "callback_data": button["callback_data"]
                    if button["callback_data"].startswith(KEEP_SOURCE_PREFIX)
                    else KEEP_SOURCE_PREFIX + button["callback_data"],
                }
                for button in row
            ]
            for row in buttons
        ]
    return text, buttons


async def _render_event(
    session: AsyncSession,
    *,
    workspace: Workspace,
    event_type: str,
    payload: dict[str, Any],
    recipient_user_id: uuid.UUID,
) -> tuple[str | None, list[list[dict[str, str]]] | None]:
    header = f"📒 {workspace.name}"

    if event_type in {"TransactionPosted", "TransactionRevised", "TransactionVoided"}:
        raw_id = payload.get("transaction_id")
        if not raw_id:
            return None, None
        summary = await _transaction_summary(
            session, workspace=workspace, transaction_id=uuid.UUID(str(raw_id))
        )
        if summary is None:
            return None, None
        text_body, revision, account_flow = summary
        author = await _actor_name(session, revision.changed_by, workspace_id=workspace.id)
        verb = _transaction_event_title(revision.transaction_type, event_type)
        lines = [
            verb,
            "",
            text_body,
            "",
            header,
            f"{'Изменено' if event_type != 'TransactionPosted' else 'Добавлено'}: {author}",
            f"Дата: {format_date(revision.occurred_date, with_year=True)}",
        ]
        if revision.transaction_type == "transfer" and account_flow:
            lines.insert(3, f"Счета: {account_flow}")
        if revision.note:
            preview = revision.note.strip()
            lines.extend(["", f"💬 Комментарий: {preview}"])
        buttons = [
            [
                {
                    "text": "🧾 Открыть операцию",
                    "callback_data": f"tx:open:{uuid.UUID(str(raw_id)).hex[:16]}",
                },
                {"text": "📒 Бюджет", "callback_data": "menu:budget"},
            ]
        ]
        return "\n".join(lines), buttons

    if event_type == "BudgetPeriodOpened":
        start = dt.date.fromisoformat(str(payload["start_date"]))
        end = dt.date.fromisoformat(str(payload["end_inclusive"]))
        return (
            f"📅 Открыт новый период\n\n{header}\n{format_range(start, end)}\n\n"
            "Можно записывать новые траты и проверить план на этот период.",
            [[{"text": "📒 Открыть бюджет", "callback_data": "menu:budget"}]],
        )

    if event_type == "BudgetPeriodEnded":
        completeness = {
            "incomplete": "учёт отмечен неполным",
            "reconciled_source": "сверено по доступному источнику",
            "confirmed_complete": "полнота подтверждена участником",
        }.get(str(payload.get("completeness")), "полнота не подтверждена")
        return (
            f"📋 Период завершён\n\n{header}\n\nПолнота: {completeness}.\n\n"
            "Откройте итоги, чтобы сравнить расходы с планом.",
            [[{"text": "📋 Итог периода", "callback_data": "menu:summary"}]],
        )

    if event_type in {"MemberJoined", "MemberLeft", "MemberRemoved", "AdminTransferred"}:
        actor = await _actor_name(
            session,
            payload.get("user_id") and uuid.UUID(str(payload["user_id"])),
            workspace_id=workspace.id,
        )
        verb = {
            "MemberJoined": "присоединился к бюджету",
            "MemberLeft": "вышел из бюджета",
            "MemberRemoved": "исключён из бюджета",
            "AdminTransferred": "стал администратором бюджета",
        }[event_type]
        return f"👥 Изменение участников\n\n{header}\n\n{actor} {verb}.", None

    if event_type == "AdminTransferProposed":
        if str(payload.get("to_user_id")) != str(recipient_user_id):
            return None, None
        proposal = str(payload.get("proposal_id") or "").replace("-", "")[:16]
        return (
            f"👑 Вам предлагают управление бюджетом\n\n{header}\n\n"
            "Администратор приглашает, добавляет участников и может удалить бюджет. "
            "Примите роль или откажитесь.",
            [
                [
                    {"text": "✅ Принять роль", "callback_data": f"ws:acceptadmin:{proposal}"},
                    {"text": "✕ Отказаться", "callback_data": f"ws:declineadmin:{proposal}"},
                ]
            ],
        )

    if event_type == "BudgetDeletionRequested":
        # Служебное терминальное сообщение без пересылки финансовых данных.
        return f"🗑 Бюджет удалён\n\nАдминистратор удалил бюджет «{workspace.name}».", None

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
        return f"🗂 {verb.capitalize()}\n\n{name}\n\n{header}", None

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
            "🔔 Напоминание о платеже",
            "",
            f"{name} · {amount}",
            f"Срок: {format_date(occurrence.due_date, with_year=True)}",
            "",
            header,
            "",
            "Уже оплатили? Нажмите «Оплачено», чтобы записать расход.",
            "Пока это ожидаемый платёж — в расходы он не включён.",
        ]
        buttons = [
            [
                {"text": "✅ Оплачено", "callback_data": f"pay:done:{occurrence.id.hex[:16]}"},
                {"text": "📅 Перенести", "callback_data": f"pay:move:{occurrence.id.hex[:16]}"},
            ],
            [{"text": "Пропустить →", "callback_data": f"pay:skip:{occurrence.id.hex[:16]}"}],
        ]
        return "\n".join(lines), buttons

    if event_type == "PlanReviewDue":
        end = dt.date.fromisoformat(str(payload["end_inclusive"]))
        return (
            f"📅 Пора проверить следующий план\n\n{header}\n"
            f"Текущий период заканчивается {format_date(end, with_year=True)}.\n\n"
            "Откройте план следующего периода и проверьте лимиты. "
            "Суммы не меняются без вашего решения.",
            [
                [
                    {"text": "📅 Следующий план", "callback_data": "menu:nextplan"},
                    {"text": "📋 Итог периода", "callback_data": "menu:summary"},
                ]
            ],
        )

    if event_type == "ImportCommitted":
        # Одна сводка вместо рассылки по каждой импортированной строке (A63).
        rows = int(payload.get("rows") or 0)
        return (
            f"✅ Импорт завершён\n\n{header}\nПеренесено записей: {rows}\n\n"
            "Можно проверить историю и обновлённые итоги. "
            "Предупреждения по прошлым периодам не рассылаются.",
            [[{"text": "📒 Открыть бюджет", "callback_data": "menu:budget"}]],
        )

    if event_type == "ThresholdCrossed":
        return str(payload.get("text") or ""), None

    if event_type == "AnalysisCompleted":
        return str(payload.get("text") or ""), None

    return None, None
