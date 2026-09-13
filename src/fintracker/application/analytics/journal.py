"""Журнал операций с фильтрами и сортировкой (FR-07, FR-89, R05).

Журнал общий для всех активных участников текущего бюджета: доступ к записи
не зависит от доступа к чужому личному диалогу Telegram.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.analytics.reports import FilterSpec, _apply_transaction_filters
from fintracker.db.models.ledger import Allocation, Transaction, TransactionRevision

SORT_MODES = ("occurred", "added")


@dataclass(frozen=True, slots=True)
class JournalEntry:
    transaction_id: uuid.UUID
    amount_minor: int
    currency: str
    occurred_date: dt.date
    transaction_type: str
    status: str
    origin: str
    author_user_id: uuid.UUID
    editor_user_id: uuid.UUID | None
    note: str | None
    merchant: str | None
    category_ids: tuple[uuid.UUID | None, ...]
    created_at: dt.datetime


@dataclass(frozen=True, slots=True)
class JournalPage:
    entries: tuple[JournalEntry, ...]
    total: int
    sort: str


def _base_statement(
    *, workspace_id: uuid.UUID, filters: FilterSpec
) -> Select[tuple[Transaction, TransactionRevision]]:
    statement = (
        select(Transaction, TransactionRevision)
        .join(
            TransactionRevision,
            (TransactionRevision.workspace_id == Transaction.workspace_id)
            & (TransactionRevision.transaction_id == Transaction.id)
            & (TransactionRevision.revision == Transaction.current_revision),
        )
        .where(Transaction.workspace_id == workspace_id)
    )
    if not filters.include_voided and not filters.statuses:
        statement = statement.where(Transaction.status == "posted")
    if filters.category_ids or filters.beneficiary_ids:
        # Категория и получатель должны совпасть в одной строке распределения (R05).
        condition = (
            select(1)
            .select_from(Allocation)
            .where(
                Allocation.workspace_id == Transaction.workspace_id,
                Allocation.transaction_id == Transaction.id,
                Allocation.revision == Transaction.current_revision,
            )
        )
        if filters.category_ids:
            condition = condition.where(Allocation.category_id.in_(filters.category_ids))
        if filters.beneficiary_ids:
            condition = condition.where(Allocation.beneficiary_id.in_(filters.beneficiary_ids))
        statement = statement.where(condition.exists())
    filtered: Select[tuple[Transaction, TransactionRevision]] = _apply_transaction_filters(
        statement, filters=filters
    )
    return filtered


async def list_journal(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    filters: FilterSpec | None = None,
    date_from: dt.date | None = None,
    date_to_exclusive: dt.date | None = None,
    sort: str = "occurred",
    limit: int = 8,
    offset: int = 0,
) -> JournalPage:
    """Страница журнала с явной сортировкой (FR-07).

    ``occurred`` — по дате операции, ``added`` — по времени добавления:
    «последние добавленные» не то же самое, что «самые поздние по дате».
    """
    if sort not in SORT_MODES:
        from fintracker.core.errors import ValidationFailed

        raise ValidationFailed("Недопустимый порядок сортировки журнала")
    active = filters or FilterSpec()
    statement = _base_statement(workspace_id=workspace_id, filters=active)
    if date_from is not None:
        statement = statement.where(TransactionRevision.occurred_date >= date_from)
    if date_to_exclusive is not None:
        statement = statement.where(TransactionRevision.occurred_date < date_to_exclusive)

    from sqlalchemy import func

    total = (
        await session.execute(select(func.count()).select_from(statement.subquery()))
    ).scalar_one()

    if sort == "added":
        statement = statement.order_by(Transaction.created_seq.desc())
    else:
        statement = statement.order_by(
            Transaction.occurred_sort_date.desc(), Transaction.created_seq.desc()
        )
    rows = (await session.execute(statement.limit(limit).offset(offset))).all()

    ids = [row[0].id for row in rows]
    allocations: dict[uuid.UUID, list[uuid.UUID | None]] = {}
    if ids:
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
                    Allocation.transaction_id.in_(ids),
                )
            )
        ).all()
        for transaction_id, category_id in allocation_rows:
            allocations.setdefault(transaction_id, []).append(category_id)

    entries = tuple(
        JournalEntry(
            transaction_id=transaction.id,
            amount_minor=revision.amount_minor,
            currency=revision.currency,
            occurred_date=revision.occurred_date,
            transaction_type=revision.transaction_type,
            status=transaction.status,
            origin=transaction.origin,
            author_user_id=transaction.created_by,
            editor_user_id=revision.changed_by,
            note=revision.note,
            merchant=revision.merchant,
            category_ids=tuple(allocations.get(transaction.id, [])),
            created_at=transaction.created_at,
        )
        for transaction, revision in rows
    )
    return JournalPage(entries=entries, total=int(total), sort=sort)
