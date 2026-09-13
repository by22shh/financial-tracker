"""Числовые отчёты: только детерминированный код и SQL (ADR-09, FR-57–FR-60).

Каждый отчёт содержит период, валюту, полноту, версию данных, метод, фильтры
и время расчёта. AI не вычисляет суммы.
Команда CMD-23: числовые отчёты по разрешённым метрикам.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.planning.plan import PeriodStatus, period_status
from fintracker.core.money import Money
from fintracker.db.models.access import Beneficiary, Person, Workspace
from fintracker.db.models.catalog import Category, TransactionTag
from fintracker.db.models.ledger import Allocation, Transaction, TransactionRevision
from fintracker.db.models.planning import BudgetPeriod
from fintracker.domain.ledger.model import CONSUMPTION_REDUCING_ROLES, CONSUMPTION_ROLES


@dataclass(frozen=True, slots=True)
class ReportMeta:
    """Обязательные атрибуты любого числового ответа (ADR-09)."""

    workspace_id: uuid.UUID
    date_from: dt.date
    date_to_inclusive: dt.date
    currency: str
    coverage: str
    data_revision: int
    method: str
    filters: dict[str, Any]
    computed_at: dt.datetime

    def describe_period(self) -> str:
        from fintracker.application.delivery.render import format_range

        return format_range(self.date_from, self.date_to_inclusive)


@dataclass(frozen=True, slots=True)
class SpendingRow:
    label: str
    category_id: uuid.UUID | None
    beneficiary_id: uuid.UUID | None
    amount_minor: int
    transaction_count: int
    # Количество отдельных операций и признак агрегата (FR-59): по дневным и
    # периодным агрегатам количество покупок не восстанавливается.
    individual_count: int = 0
    has_aggregate: bool = False


@dataclass(frozen=True, slots=True)
class SpendingReport:
    meta: ReportMeta
    rows: tuple[SpendingRow, ...]
    total_minor: int
    # Полная сумма затронутых покупок отдельно от суммы подходящих частей (R05).
    matched_transaction_total_minor: int
    transaction_count: int
    uncategorized_minor: int
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FilterSpec:
    """Фильтры журнала (FR-89, R05).

    Автор, совершивший покупку, комментарий и метки фильтруют событие;
    категория и получатель — подходящие строки распределения совместно.
    """

    category_ids: tuple[uuid.UUID, ...] = ()
    beneficiary_ids: tuple[uuid.UUID, ...] = ()
    spender_person_ids: tuple[uuid.UUID, ...] = ()
    actor_user_ids: tuple[uuid.UUID, ...] = ()
    # Автор последнего изменения — отдельный фильтр: он не совпадает с автором
    # записи после исправления чужой операции (FR-07).
    editor_user_ids: tuple[uuid.UUID, ...] = ()
    account_ids: tuple[uuid.UUID, ...] = ()
    transaction_types: tuple[str, ...] = ()
    origins: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    tag_ids: tuple[uuid.UUID, ...] = ()
    tag_mode: str = "any"
    note_query: str | None = None
    has_note: bool | None = None
    min_amount_minor: int | None = None
    max_amount_minor: int | None = None
    include_voided: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "category_ids": [str(i) for i in self.category_ids],
            "beneficiary_ids": [str(i) for i in self.beneficiary_ids],
            "spender_person_ids": [str(i) for i in self.spender_person_ids],
            "actor_user_ids": [str(i) for i in self.actor_user_ids],
            "editor_user_ids": [str(i) for i in self.editor_user_ids],
            "account_ids": [str(i) for i in self.account_ids],
            "transaction_types": list(self.transaction_types),
            "origins": list(self.origins),
            "statuses": list(self.statuses),
            "tag_ids": [str(i) for i in self.tag_ids],
            "tag_mode": self.tag_mode,
            "note_query": self.note_query,
            "has_note": self.has_note,
            "min_amount_minor": self.min_amount_minor,
            "max_amount_minor": self.max_amount_minor,
        }


def escape_like(value: str) -> str:
    """Экранирование литералов '%' и '_' для поиска подстроки (DATA_CONTRACT §3)."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _apply_transaction_filters(statement: Any, *, filters: FilterSpec) -> Any:
    if filters.spender_person_ids:
        statement = statement.where(
            TransactionRevision.spender_person_id.in_(filters.spender_person_ids)
        )
    if filters.actor_user_ids:
        statement = statement.where(Transaction.created_by.in_(filters.actor_user_ids))
    if filters.editor_user_ids:
        statement = statement.where(TransactionRevision.changed_by.in_(filters.editor_user_ids))
    if filters.transaction_types:
        statement = statement.where(
            TransactionRevision.transaction_type.in_(filters.transaction_types)
        )
    if filters.origins:
        statement = statement.where(Transaction.origin.in_(filters.origins))
    if filters.statuses:
        statement = statement.where(Transaction.status.in_(filters.statuses))
    if filters.account_ids:
        # Счёт проверяется через EXISTS по денежным частям текущей ревизии.
        from fintracker.db.models.ledger import CashLeg

        statement = statement.where(
            select(1)
            .select_from(CashLeg)
            .where(
                CashLeg.workspace_id == Transaction.workspace_id,
                CashLeg.transaction_id == Transaction.id,
                CashLeg.revision == Transaction.current_revision,
                CashLeg.account_id.in_(filters.account_ids),
            )
            .exists()
        )
    if filters.note_query:
        pattern = f"%{escape_like(filters.note_query)}%"
        statement = statement.where(TransactionRevision.note.ilike(pattern, escape="\\"))
    if filters.has_note is True:
        statement = statement.where(TransactionRevision.note.is_not(None))
    if filters.has_note is False:
        statement = statement.where(TransactionRevision.note.is_(None))
    if filters.min_amount_minor is not None:
        statement = statement.where(TransactionRevision.amount_minor >= filters.min_amount_minor)
    if filters.max_amount_minor is not None:
        statement = statement.where(TransactionRevision.amount_minor <= filters.max_amount_minor)
    if filters.tag_ids:
        # Метки проверяются через EXISTS и не размножают суммы (ADR-09).
        condition = (
            select(1)
            .select_from(TransactionTag)
            .where(
                TransactionTag.workspace_id == Transaction.workspace_id,
                TransactionTag.transaction_id == Transaction.id,
                TransactionTag.revision == Transaction.current_revision,
                TransactionTag.tag_id.in_(filters.tag_ids),
            )
        )
        if filters.tag_mode == "all":
            for tag_id in filters.tag_ids:
                statement = statement.where(
                    select(1)
                    .select_from(TransactionTag)
                    .where(
                        TransactionTag.workspace_id == Transaction.workspace_id,
                        TransactionTag.transaction_id == Transaction.id,
                        TransactionTag.revision == Transaction.current_revision,
                        TransactionTag.tag_id == tag_id,
                    )
                    .exists()
                )
        else:
            statement = statement.where(condition.exists())
    return statement


async def spending_report(
    session: AsyncSession,
    *,
    workspace: Workspace,
    date_from: dt.date,
    date_to_exclusive: dt.date,
    filters: FilterSpec | None = None,
    group_by: str = "category",
    coverage: str = "incomplete",
) -> SpendingReport:
    """Расходы за интервал с группировкой (FR-57, R05, AR-19)."""
    active_filters = filters or FilterSpec()
    consumption = [role.value for role in CONSUMPTION_ROLES]
    reducing = [role.value for role in CONSUMPTION_REDUCING_ROLES]

    base = (
        select(
            Allocation.category_id,
            Allocation.beneficiary_id,
            Allocation.economic_role,
            Allocation.amount_minor,
            Allocation.transaction_id,
            TransactionRevision.amount_minor.label("transaction_total"),
            TransactionRevision.granularity,
        )
        .join(
            Transaction,
            (Transaction.workspace_id == Allocation.workspace_id)
            & (Transaction.id == Allocation.transaction_id)
            & (Transaction.current_revision == Allocation.revision),
        )
        .join(
            TransactionRevision,
            (TransactionRevision.workspace_id == Allocation.workspace_id)
            & (TransactionRevision.transaction_id == Allocation.transaction_id)
            & (TransactionRevision.revision == Allocation.revision),
        )
        .where(
            Allocation.workspace_id == workspace.id,
            TransactionRevision.occurred_date >= date_from,
            TransactionRevision.occurred_date < date_to_exclusive,
            Allocation.economic_role.in_(consumption + reducing),
        )
    )
    if not active_filters.include_voided:
        base = base.where(Transaction.status == "posted")
    # Категория и получатель должны совпасть в одной строке распределения (R05).
    if active_filters.category_ids:
        base = base.where(Allocation.category_id.in_(active_filters.category_ids))
    if active_filters.beneficiary_ids:
        base = base.where(Allocation.beneficiary_id.in_(active_filters.beneficiary_ids))
    base = _apply_transaction_filters(base, filters=active_filters)

    rows = (await session.execute(base)).all()

    grouped: dict[tuple[uuid.UUID | None, uuid.UUID | None], int] = {}
    transactions: dict[tuple[uuid.UUID | None, uuid.UUID | None], set[uuid.UUID]] = {}
    individual: dict[tuple[uuid.UUID | None, uuid.UUID | None], set[uuid.UUID]] = {}
    aggregated: set[tuple[uuid.UUID | None, uuid.UUID | None]] = set()
    matched_transactions: dict[uuid.UUID, int] = {}
    total = 0
    uncategorized = 0
    for row in rows:
        signed = row.amount_minor if row.economic_role in consumption else -row.amount_minor
        key: tuple[uuid.UUID | None, uuid.UUID | None]
        if group_by == "beneficiary":
            key = (None, row.beneficiary_id)
        elif group_by == "none":
            key = (None, None)
        else:
            key = (row.category_id, row.beneficiary_id)
        grouped[key] = grouped.get(key, 0) + signed
        transactions.setdefault(key, set()).add(row.transaction_id)
        if row.granularity == "individual":
            individual.setdefault(key, set()).add(row.transaction_id)
        else:
            aggregated.add(key)
        matched_transactions[row.transaction_id] = row.transaction_total
        total += signed
        if row.category_id is None:
            uncategorized += signed

    labels = await _labels(session, workspace_id=workspace.id)
    report_rows = tuple(
        SpendingRow(
            label=_label_for(key, labels),
            category_id=key[0],
            beneficiary_id=key[1],
            amount_minor=amount,
            transaction_count=len(transactions.get(key, set())),
            individual_count=len(individual.get(key, set())),
            has_aggregate=key in aggregated,
        )
        for key, amount in sorted(grouped.items(), key=lambda item: -item[1])
    )
    meta = ReportMeta(
        workspace_id=workspace.id,
        date_from=date_from,
        date_to_inclusive=date_to_exclusive - dt.timedelta(days=1),
        currency=workspace.currency,
        coverage=coverage,
        data_revision=workspace.data_revision,
        method=f"allocations/{group_by}",
        filters=active_filters.as_dict(),
        computed_at=dt.datetime.now(dt.UTC),
    )
    return SpendingReport(
        meta=meta,
        rows=report_rows,
        total_minor=total,
        matched_transaction_total_minor=sum(matched_transactions.values()),
        transaction_count=len(matched_transactions),
        uncategorized_minor=uncategorized,
    )


async def _labels(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> dict[str, dict[uuid.UUID, str]]:
    categories = (
        await session.execute(
            select(Category.id, Category.name).where(Category.workspace_id == workspace_id)
        )
    ).all()
    beneficiaries = (
        await session.execute(
            select(Beneficiary.id, Beneficiary.name).where(Beneficiary.workspace_id == workspace_id)
        )
    ).all()
    people = (
        await session.execute(
            select(Person.id, Person.name).where(Person.workspace_id == workspace_id)
        )
    ).all()
    return {
        "categories": {row[0]: row[1] for row in categories},
        "beneficiaries": {row[0]: row[1] for row in beneficiaries},
        "people": {row[0]: row[1] for row in people},
    }


def _label_for(
    key: tuple[uuid.UUID | None, uuid.UUID | None], labels: dict[str, dict[uuid.UUID, str]]
) -> str:
    category_id, beneficiary_id = key
    parts: list[str] = []
    if category_id is not None:
        parts.append(labels["categories"].get(category_id, "Без названия"))
    elif beneficiary_id is None:
        parts.append("Всего")
    else:
        parts.append("Без категории")
    if beneficiary_id is not None:
        parts.append(labels["beneficiaries"].get(beneficiary_id, "?"))
    return " · ".join(parts)


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """Сравнение периодов с явной сопоставимостью (FR-60, A87, A88)."""

    current_minor: int
    previous_minor: int
    absolute_change_minor: int
    percent_change: float | None
    comparable: bool
    note: str
    current_days: int
    previous_days: int


def compare_periods(
    *,
    current_minor: int,
    previous_minor: int,
    current_days: int,
    previous_days: int,
    current_partial: bool,
    previous_complete: bool,
) -> ComparisonResult:
    """Проценты и сопоставимость интервалов (FR-60).

    При нулевом прошлом значении показывается абсолютное изменение; деления
    на ноль и бесконечного процента не возникает.
    """
    change = current_minor - previous_minor
    percent: float | None = None
    notes: list[str] = []
    comparable = current_days == previous_days and not current_partial and previous_complete

    if previous_minor == 0:
        notes.append("раньше расходов не было")
    else:
        percent = change / previous_minor * 100
    if current_days != previous_days:
        notes.append(f"разная длительность: {current_days} и {previous_days} дн.")
    if current_partial:
        notes.append("текущий период неполный")
    if not previous_complete:
        notes.append("полнота прошлого периода не подтверждена")
    return ComparisonResult(
        current_minor=current_minor,
        previous_minor=previous_minor,
        absolute_change_minor=change,
        percent_change=percent,
        comparable=comparable,
        note="; ".join(notes) if notes else "интервалы сопоставимы",
        current_days=current_days,
        previous_days=previous_days,
    )


@dataclass(frozen=True, slots=True)
class Forecast:
    """Базовый прогноз расхода периода (FR-41, B8)."""

    fact_minor: int
    commitments_minor: int
    flexible_forecast_minor: int | None
    total_minor: int | None
    method: str
    limitations: tuple[str, ...]


def build_forecast(
    *,
    status: PeriodStatus,
    today: dt.date,
    observed_days: int,
    flexible_fact_minor: int,
    coverage: str,
) -> Forecast:
    """Факт + неисполненные обязательства + прогноз гибких (FORM-03).

    Аренда, оплаченная в первый день, не умножается на число дней месяца:
    обязательства берутся из расписаний, а темп считается только по гибким
    тратам и только после семи наблюдаемых дней.
    """
    commitments = sum(line.commitments_minor for line in status.lines)
    limitations: list[str] = []
    flexible: int | None = None
    method = "fact_plus_commitments"

    remaining_days = max(0, (status.end_inclusive - today).days)

    if coverage == "incomplete":
        limitations.append("полнота учёта не подтверждена")
    if observed_days < 7:
        limitations.append("менее семи наблюдаемых дней: темп не рассчитывается")
    elif remaining_days > 0:
        daily = flexible_fact_minor / observed_days
        flexible = int(daily * remaining_days)
        method = "fact_plus_commitments_plus_pace"
        limitations.append("оценка при сохранении темпа")

    total = status.total_fact_minor + commitments + (flexible or 0)
    return Forecast(
        fact_minor=status.total_fact_minor,
        commitments_minor=commitments,
        flexible_forecast_minor=flexible,
        total_minor=total if observed_days >= 7 or remaining_days == 0 else None,
        method=method,
        limitations=tuple(limitations) or ("ограничений нет",),
    )


async def period_snapshot_metrics(
    session: AsyncSession,
    *,
    workspace: Workspace,
    period_id: uuid.UUID,
    today: dt.date,
) -> dict[str, Any]:
    """Числовой снимок для AI: только проверяемые показатели (AI-08)."""
    status = await period_status(
        session,
        workspace_id=workspace.id,
        period_id=period_id,
        currency=workspace.currency,
        today=today,
    )
    period = (
        await session.execute(
            select(BudgetPeriod).where(
                BudgetPeriod.workspace_id == workspace.id, BudgetPeriod.id == period_id
            )
        )
    ).scalar_one()
    observed_days = max(0, min((today - period.start_date).days + 1, status_days(status)))
    lines_payload = [
        {
            "metric_id": f"line:{line.stable_line_id}",
            "category": line.category_name,
            "beneficiary": line.beneficiary_name,
            "limit_minor": line.effective_limit_minor,
            "fact_minor": line.fact_minor,
            "remaining_minor": line.remaining_minor,
            "commitments_minor": line.commitments_minor,
            "available_minor": line.available_minor,
            "usage_percent": round(line.usage_percent, 1) if line.usage_percent else None,
            "is_protected": line.is_protected,
            "limit_state": line.limit_state.value,
        }
        for line in status.lines
    ]
    return {
        "metric_id": f"period:{period_id}",
        "period": {
            "id": str(period_id),
            "start": status.start_date.isoformat(),
            "end_inclusive": status.end_inclusive.isoformat(),
            "days": status_days(status),
            "observed_days": observed_days,
        },
        "currency": workspace.currency,
        "coverage": status.completeness,
        "plan_status": status.plan_status,
        "total_fact_minor": status.total_fact_minor,
        "total_limit_minor": status.total_limit_minor,
        "uncategorized_fact_minor": status.uncategorized_fact_minor,
        "pending_drafts": status.pending_drafts,
        "pending_confident_minor": status.pending_confident_minor,
        "lines": lines_payload,
        "data_revision": workspace.data_revision,
        "plan_revision": workspace.plan_revision,
        "calendar_revision": workspace.calendar_revision,
        "catalog_revision": workspace.catalog_revision,
        "coverage_revision": workspace.coverage_revision,
    }


def status_days(status: PeriodStatus) -> int:
    return (status.end_inclusive - status.start_date).days + 1


def format_report(report: SpendingReport, *, limit: int = 10) -> str:
    """Ответ содержит период, валюту, полноту и определение показателя."""
    currency = report.meta.currency
    lines = [
        f"Период: {report.meta.describe_period()}",
        f"Итого: {Money(report.total_minor, currency).format()}",
    ]
    if report.transaction_count:
        lines.append(f"Операций: {report.transaction_count}")
    if report.matched_transaction_total_minor != report.total_minor:
        # Полная сумма чеков показывается отдельно от суммы подходящих частей.
        lines.append(
            "Полная сумма затронутых покупок: "
            f"{Money(report.matched_transaction_total_minor, currency).format()}"
        )
    for row in report.rows[:limit]:
        lines.append(f"• {row.label}: {Money(row.amount_minor, currency).format()}")
    if report.uncategorized_minor:
        lines.append(f"Без категории: {Money(report.uncategorized_minor, currency).format()}")
    coverage_label = {
        "incomplete": "не подтверждена",
        "reconciled_source": "сверена по доступному источнику",
        "confirmed_complete": "подтверждена участником",
    }.get(report.meta.coverage, report.meta.coverage)
    lines.append(f"Полнота учёта: {coverage_label}")
    return "\n".join(lines)
