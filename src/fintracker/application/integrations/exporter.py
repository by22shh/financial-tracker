"""Выгрузка журнала в XLSX и CSV (FR-67, SEC-08, A108, A200).

Экспорт доступен независимо от AI. Внешний текст, похожий на формулу,
экспортируется как текст для защиты от CSV injection.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.conversation.context import category_paths
from fintracker.core.money import Money
from fintracker.db.models.access import Beneficiary, Person, User, Workspace
from fintracker.db.models.catalog import Tag, TransactionTag
from fintracker.db.models.ledger import Allocation, Transaction, TransactionRevision
from fintracker.db.models.planning import BudgetPeriod, PeriodPolicyRow

# Символы, с которых начинается формула в табличных редакторах.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitize_cell(value: str | None) -> str:
    """Обезвредить внешний текст, похожий на формулу (OWASP CSV injection)."""
    if value is None:
        return ""
    text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


@dataclass(frozen=True, slots=True)
class ExportRow:
    transaction_id: uuid.UUID
    revision: int
    transaction_type: str
    occurred_date: dt.date
    date_precision: str
    granularity: str
    amount_minor: int
    currency: str
    status: str
    origin: str
    actor_name: str
    spender_name: str
    note: str
    tags: str
    allocations: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class ExportSnapshot:
    workspace_name: str
    currency: str
    data_revision: int
    generated_at: dt.datetime
    rows: tuple[ExportRow, ...]
    periods: tuple[dict[str, str], ...]


async def build_snapshot(
    session: AsyncSession,
    *,
    workspace: Workspace,
    date_from: dt.date | None = None,
    date_to_exclusive: dt.date | None = None,
) -> ExportSnapshot:
    """Один зафиксированный снимок журнала (ADR-09)."""
    statement = (
        select(Transaction, TransactionRevision)
        .join(
            TransactionRevision,
            (TransactionRevision.workspace_id == Transaction.workspace_id)
            & (TransactionRevision.transaction_id == Transaction.id)
            & (TransactionRevision.revision == Transaction.current_revision),
        )
        .where(Transaction.workspace_id == workspace.id)
        .order_by(TransactionRevision.occurred_date, Transaction.id)
    )
    if date_from is not None:
        statement = statement.where(TransactionRevision.occurred_date >= date_from)
    if date_to_exclusive is not None:
        statement = statement.where(TransactionRevision.occurred_date < date_to_exclusive)
    rows = (await session.execute(statement)).all()

    paths = await category_paths(session, workspace_id=workspace.id)
    beneficiaries = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(Beneficiary.id, Beneficiary.name).where(
                    Beneficiary.workspace_id == workspace.id
                )
            )
        ).all()
    }
    people = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(Person.id, Person.name).where(Person.workspace_id == workspace.id)
            )
        ).all()
    }
    users = {
        row[0]: str(row[1])
        for row in (await session.execute(select(User.id, User.telegram_user_id))).all()
    }

    export_rows: list[ExportRow] = []
    for transaction, revision in rows:
        allocations = (
            (
                await session.execute(
                    select(Allocation).where(
                        Allocation.workspace_id == workspace.id,
                        Allocation.transaction_id == transaction.id,
                        Allocation.revision == revision.revision,
                    )
                )
            )
            .scalars()
            .all()
        )
        tag_names = (
            (
                await session.execute(
                    select(Tag.name)
                    .join(
                        TransactionTag,
                        (TransactionTag.workspace_id == Tag.workspace_id)
                        & (TransactionTag.tag_id == Tag.id),
                    )
                    .where(
                        TransactionTag.workspace_id == workspace.id,
                        TransactionTag.transaction_id == transaction.id,
                        TransactionTag.revision == revision.revision,
                    )
                )
            )
            .scalars()
            .all()
        )
        export_rows.append(
            ExportRow(
                transaction_id=transaction.id,
                revision=revision.revision,
                transaction_type=revision.transaction_type,
                occurred_date=revision.occurred_date,
                date_precision=revision.date_precision,
                granularity=revision.granularity,
                amount_minor=revision.amount_minor,
                currency=revision.currency,
                status=transaction.status,
                origin=transaction.origin,
                actor_name=users.get(transaction.created_by, ""),
                spender_name=people.get(revision.spender_person_id, "")
                if revision.spender_person_id
                else "",
                note=revision.note or "",
                tags="; ".join(str(name) for name in tag_names),
                allocations=tuple(
                    {
                        "allocation_id": str(item.id),
                        "stable_line_id": str(item.stable_line_id),
                        "role": item.economic_role,
                        "category": paths.get(item.category_id, "") if item.category_id else "",
                        "beneficiary": beneficiaries.get(item.beneficiary_id, "")
                        if item.beneficiary_id
                        else "",
                        "amount_minor": str(item.amount_minor),
                        "label": item.line_label or "",
                    }
                    for item in allocations
                ),
            )
        )

    periods = (
        await session.execute(
            select(BudgetPeriod, PeriodPolicyRow)
            .join(
                PeriodPolicyRow,
                (PeriodPolicyRow.workspace_id == BudgetPeriod.workspace_id)
                & (PeriodPolicyRow.id == BudgetPeriod.policy_id),
            )
            .where(BudgetPeriod.workspace_id == workspace.id)
            .order_by(BudgetPeriod.start_date)
        )
    ).all()
    return ExportSnapshot(
        workspace_name=workspace.name,
        currency=workspace.currency,
        data_revision=workspace.data_revision,
        generated_at=dt.datetime.now(dt.UTC),
        rows=tuple(export_rows),
        periods=tuple(
            {
                "budget_id": str(workspace.id),
                "period_id": str(period.id),
                "start_date": period.start_date.isoformat(),
                # Включённая дата конца с явным названием поля (FR-67).
                "end_date_inclusive": (period.end_exclusive - dt.timedelta(days=1)).isoformat(),
                "repeat_rule": f"{policy.mode}:{policy.interval}",
                "policy_version": str(policy.version),
                "plan_origin": period.state,
            }
            for period, policy in periods
        ),
    )


CSV_COLUMNS = (
    "transaction_id",
    "revision",
    "type",
    "date",
    "date_precision",
    "granularity",
    "amount_minor",
    "currency",
    "status",
    "origin",
    "actor",
    "spender",
    "note",
    "tags",
    "allocations_count",
)

ALLOCATION_COLUMNS = (
    "transaction_id",
    "revision",
    "allocation_id",
    "stable_line_id",
    "role",
    "category",
    "beneficiary",
    "amount_minor",
    "label",
)


def to_csv(snapshot: ExportSnapshot) -> bytes:
    """Нормализованный журнал в UTF-8 с экранированием (FR-67)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in snapshot.rows:
        writer.writerow(
            [
                str(row.transaction_id),
                str(row.revision),
                row.transaction_type,
                row.occurred_date.isoformat(),
                row.date_precision,
                row.granularity,
                str(row.amount_minor),
                row.currency,
                row.status,
                row.origin,
                sanitize_cell(row.actor_name),
                sanitize_cell(row.spender_name),
                sanitize_cell(row.note),
                sanitize_cell(row.tags),
                str(len(row.allocations)),
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")


def to_xlsx(snapshot: ExportSnapshot) -> bytes:
    """XLSX с листами «Операции», «Распределения», «Периоды», «Описание полей»."""
    from openpyxl import Workbook

    workbook = Workbook()
    operations = workbook.active
    operations.title = "Операции"
    operations.append(list(CSV_COLUMNS))
    for row in snapshot.rows:
        operations.append(
            [
                str(row.transaction_id),
                row.revision,
                row.transaction_type,
                row.occurred_date.isoformat(),
                row.date_precision,
                row.granularity,
                row.amount_minor,
                row.currency,
                row.status,
                row.origin,
                sanitize_cell(row.actor_name),
                sanitize_cell(row.spender_name),
                sanitize_cell(row.note),
                sanitize_cell(row.tags),
                len(row.allocations),
            ]
        )

    allocations = workbook.create_sheet("Распределения")
    allocations.append(list(ALLOCATION_COLUMNS))
    for row in snapshot.rows:
        for allocation in row.allocations:
            allocations.append(
                [
                    str(row.transaction_id),
                    row.revision,
                    allocation["allocation_id"],
                    allocation["stable_line_id"],
                    allocation["role"],
                    sanitize_cell(allocation["category"]),
                    sanitize_cell(allocation["beneficiary"]),
                    int(allocation["amount_minor"]),
                    sanitize_cell(allocation["label"]),
                ]
            )

    periods = workbook.create_sheet("Периоды")
    if snapshot.periods:
        periods.append(list(snapshot.periods[0].keys()))
        for period in snapshot.periods:
            periods.append([sanitize_cell(value) for value in period.values()])

    description = workbook.create_sheet("Описание полей")
    description.append(["Поле", "Значение"])
    for field_name, explanation in (
        ("amount_minor", "Сумма в минимальных единицах валюты, целое число"),
        ("granularity", "individual — отдельная операция; daily_aggregate — дневной итог"),
        ("date_precision", "Точность даты: day, time, interval, unknown"),
        ("status", "posted — учитывается; voided — отменена"),
        ("role", "Экономическая роль части: expense, expense_refund и другие"),
        (
            "Операции и Распределения",
            "Суммы распределений не складываются с суммой операции: "
            "это разные листы одного события",
        ),
        ("end_date_inclusive", "Дата конца периода включительно"),
        ("Валюта", snapshot.currency),
        ("Версия данных", str(snapshot.data_revision)),
        ("Снимок", snapshot.generated_at.isoformat()),
    ):
        description.append([field_name, sanitize_cell(explanation)])

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def build_matrix(
    snapshot: ExportSnapshot, *, start: dt.date, end_inclusive: dt.date
) -> list[list[str]]:
    """Привычная матрица «категории × дни» по реальным датам интервала (FR-67).

    Ограничения в 31 колонку нет: длинный отчёт сохраняет все дни.
    """
    days = [start + dt.timedelta(days=offset) for offset in range((end_inclusive - start).days + 1)]
    categories: dict[str, dict[dt.date, int]] = {}
    for row in snapshot.rows:
        if row.status != "posted":
            continue
        if not (start <= row.occurred_date <= end_inclusive):
            continue
        for allocation in row.allocations:
            if allocation["role"] not in {"expense", "expense_refund"}:
                continue
            name = allocation["category"] or "Без категории"
            signed = int(allocation["amount_minor"])
            if allocation["role"] == "expense_refund":
                signed = -signed
            bucket = categories.setdefault(name, {})
            bucket[row.occurred_date] = bucket.get(row.occurred_date, 0) + signed

    header = ["Категория", *[day.isoformat() for day in days], "Итого"]
    matrix = [header]
    for name in sorted(categories):
        values = categories[name]
        line = [sanitize_cell(name)]
        total = 0
        for day in days:
            amount = values.get(day, 0)
            total += amount
            line.append(Money(amount, snapshot.currency).format(with_symbol=False))
        line.append(Money(total, snapshot.currency).format(with_symbol=False))
        matrix.append(line)
    return matrix
