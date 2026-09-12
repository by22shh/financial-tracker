"""Импорт исходной таблицы с предпросмотром и сверкой (FR-63–FR-66, FR-72).

До подтверждения ничего не попадает в рабочие итоги. Повторный импорт
неизменного снимка не создаёт новых данных. Дневная ячейка становится
`legacy_daily_aggregate`, а не отдельной покупкой.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.catalog.categories import create_category
from fintracker.application.catalog.normalize import normalize_name
from fintracker.application.integrations.sheet_parser import ParsedWorkbook
from fintracker.application.ledger.service import post_transaction
from fintracker.core.context import ActorContext
from fintracker.core.errors import ConflictError, ImportReconciliationFailed, NotFound
from fintracker.core.money import Money
from fintracker.db.models.catalog import Category
from fintracker.db.models.integrations import ImportBatch, ImportRow, SourceMapping
from fintracker.db.uow import UnitOfWork
from fintracker.domain.ledger.model import (
    AllocationRole,
    AllocationSpec,
    CashLegSpec,
    CoverageMode,
    Granularity,
    TransactionSpec,
    TransactionType,
)

# Специальные строки исходной таблицы (SOURCE_ANALYSIS §2, A77, A78).
SPECIAL_ROWS: dict[str, str] = {
    "накопления": "goal_allocation",
    "долги": "legacy_unclassified_flow",
}


@dataclass(frozen=True, slots=True)
class ImportPreviewRow:
    source_key: str
    sheet_name: str
    label: str
    occurred_date: dt.date | None
    occurred_end_date: dt.date | None
    amount_minor: int
    granularity: str
    transaction_type: str
    decision: str
    category_name: str
    formula: str | None
    conflict_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """Сверка импорта: расхождение должно быть нулевым (FR-65, A82)."""

    by_sheet: dict[str, dict[str, int]]
    total_source_minor: int
    total_import_minor: int
    difference_minor: int
    unknown_beneficiary_rows: int
    special_rows: tuple[str, ...]

    @property
    def balanced(self) -> bool:
        return self.difference_minor == 0


@dataclass(frozen=True, slots=True)
class ImportPreview:
    batch_id: uuid.UUID
    rows: tuple[ImportPreviewRow, ...]
    reconciliation: ReconciliationReport
    new_categories: tuple[str, ...]
    periods: tuple[tuple[dt.date, dt.date], ...]
    blocked: bool
    blocked_reason: str | None = None


def _money(value: Decimal, currency: str) -> Money:
    return Money.from_decimal(value, currency)


def _source_key(sheet: str, row_label: str, column: str, day: dt.date | None) -> str:
    """Стабильный ключ: источник, лист, ячейка, период и подпись (FR-66).

    Хэш суммы помогает обнаружить изменение, но не становится единственным ID.
    """
    date_part = day.isoformat() if day else "period"
    return f"sheets:{sheet}:{row_label}:{column}:{date_part}"


async def build_preview(
    session: AsyncSession,
    *,
    actor: ActorContext,
    workbook: ParsedWorkbook,
    currency: str,
) -> ImportPreview:
    """Разобрать снимок в изолированной staging области (CMD-28)."""
    workspace_id = actor.require_workspace()

    existing_committed = (
        await session.execute(
            select(ImportBatch).where(
                ImportBatch.workspace_id == workspace_id,
                ImportBatch.snapshot_hash == workbook.snapshot_hash,
                ImportBatch.state == "committed",
            )
        )
    ).scalar_one_or_none()

    batch = ImportBatch(
        workspace_id=workspace_id,
        source_kind="sheets_xlsx",
        source_name=workbook.source_name,
        snapshot_hash=workbook.snapshot_hash,
        snapshot_taken_at=workbook.taken_at,
        mapping_version=1,
        state="preview",
        created_by=actor.user_id,
    )
    session.add(batch)
    await session.flush()

    mappings = {
        row.normalized_label: row
        for row in (
            (
                await session.execute(
                    select(SourceMapping).where(
                        SourceMapping.workspace_id == workspace_id,
                        SourceMapping.source_kind == "sheets_xlsx",
                    )
                )
            )
            .scalars()
            .all()
        )
    }
    known_categories = {
        normalize_name(row[1]): row[0]
        for row in (
            await session.execute(
                select(Category.id, Category.name).where(
                    Category.workspace_id == workspace_id, Category.archived_at.is_(None)
                )
            )
        ).all()
    }

    preview_rows: list[ImportPreviewRow] = []
    new_categories: set[str] = set()
    by_sheet: dict[str, dict[str, int]] = {}
    total_source = 0
    total_import = 0
    special: set[str] = set()
    unknown_beneficiary = 0

    for sheet in workbook.sheets:
        sheet_source = sum(_money(row.fact or Decimal(0), currency).minor for row in sheet.rows)
        sheet_import = 0
        for cell in sheet.daily_cells:
            amount = _money(cell.amount, currency)
            label = cell.row_label
            normalized = normalize_name(label)
            special_kind = _special_kind(label)
            if special_kind:
                special.add(label)
            transaction_type = (
                "legacy_unclassified_flow"
                if special_kind == "legacy_unclassified_flow"
                else "expense"
            )
            mapping = mappings.get(normalized)
            category_name = _category_name_for(label)
            if mapping is None and normalize_name(category_name) not in known_categories:
                new_categories.add(category_name)
            # Получатель исходной строки не восстанавливается из суммы (A76).
            if _has_unknown_beneficiary(label):
                unknown_beneficiary += 1

            key = _source_key(sheet.name, label, cell.column_letter, cell.occurred_date)
            decision = "new"
            conflict: str | None = None
            if existing_committed is not None:
                decision = "skip"
                conflict = "Этот снимок уже импортирован без изменений"
            preview_rows.append(
                ImportPreviewRow(
                    source_key=key,
                    sheet_name=sheet.name,
                    label=label,
                    occurred_date=cell.occurred_date,
                    occurred_end_date=None,
                    amount_minor=amount.minor,
                    granularity=Granularity.DAILY_AGGREGATE.value,
                    transaction_type=transaction_type,
                    decision=decision,
                    category_name=category_name,
                    formula=cell.formula,
                    conflict_reason=conflict,
                )
            )
            sheet_import += amount.minor

        for income in sheet.incomes:
            amount = _money(income.amount, currency)
            key = _source_key(sheet.name, income.label, "income", income.occurred_date)
            preview_rows.append(
                ImportPreviewRow(
                    source_key=key,
                    sheet_name=sheet.name,
                    label=income.label,
                    occurred_date=income.occurred_date or sheet.period_start,
                    # Недатированный доход остаётся агрегатом периода (A54).
                    occurred_end_date=None
                    if income.occurred_date
                    else sheet.period_end_exclusive - dt.timedelta(days=1),
                    amount_minor=amount.minor,
                    granularity=(
                        Granularity.INDIVIDUAL.value
                        if income.occurred_date
                        else Granularity.PERIOD_AGGREGATE.value
                    ),
                    transaction_type="income",
                    decision="skip" if existing_committed else "new",
                    category_name="Доход",
                    formula=None,
                )
            )

        by_sheet[sheet.name] = {
            "source_minor": sheet_source,
            "import_minor": sheet_import,
            "difference_minor": sheet_source - sheet_import,
            "nonzero_cells": sheet.nonzero_cells,
        }
        total_source += sheet_source
        total_import += sheet_import

    reconciliation = ReconciliationReport(
        by_sheet=by_sheet,
        total_source_minor=total_source,
        total_import_minor=total_import,
        difference_minor=total_source - total_import,
        unknown_beneficiary_rows=unknown_beneficiary,
        special_rows=tuple(sorted(special)),
    )
    batch.reconciliation = {
        "by_sheet": by_sheet,
        "total_source_minor": total_source,
        "total_import_minor": total_import,
        "difference_minor": reconciliation.difference_minor,
    }
    batch.row_count = len(preview_rows)
    await session.flush()

    for row in preview_rows:
        session.add(
            ImportRow(
                workspace_id=workspace_id,
                batch_id=batch.id,
                source_key=row.source_key,
                sheet_name=row.sheet_name,
                source_cell=row.source_key.rsplit(":", 2)[-2],
                source_label=row.label,
                source_formula=row.formula,
                granularity=row.granularity,
                occurred_date=row.occurred_date,
                occurred_end_date=row.occurred_end_date,
                amount_minor=row.amount_minor,
                currency=currency,
                transaction_type=row.transaction_type,
                decision=row.decision,
                status="pending",
                conflict_reason=row.conflict_reason,
            )
        )
    await session.flush()

    blocked = not reconciliation.balanced
    return ImportPreview(
        batch_id=batch.id,
        rows=tuple(preview_rows),
        reconciliation=reconciliation,
        new_categories=tuple(sorted(new_categories)),
        periods=tuple(
            (sheet.period_start, sheet.period_end_exclusive - dt.timedelta(days=1))
            for sheet in workbook.sheets
        ),
        blocked=blocked,
        blocked_reason=(
            "Сверка не сошлась: импорт заблокирован до устранения расхождения" if blocked else None
        ),
    )


def _special_kind(label: str) -> str | None:
    normalized = normalize_name(label)
    for token, kind in SPECIAL_ROWS.items():
        if normalized.startswith(token):
            return kind
    return None


def _category_name_for(label: str) -> str:
    """Категория из исходной подписи; имена владельца сохраняются (A69, A75)."""
    parts = [part.strip() for part in label.split("/") if part.strip()]
    return parts[0] if parts else label


def _has_unknown_beneficiary(label: str) -> bool:
    """Старая агрегированная строка без получателя остаётся неопределённой (A76)."""
    normalized = normalize_name(label)
    return "заведения" in normalized and not any(
        name in normalized for name in ("мы", "ниджат", "софа")
    )


async def commit_import(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    batch_id: uuid.UUID,
    currency: str,
    timezone: str,
    max_rows: int,
) -> ReconciliationReport:
    """Применить проверенный пакет атомарно (CMD-28, LIM-08).

    Больший файл делится пользователем на независимые проверяемые пакеты.
    """
    workspace_id = actor.require_workspace()
    batch = (
        await session.execute(
            select(ImportBatch)
            .where(ImportBatch.workspace_id == workspace_id, ImportBatch.id == batch_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if batch is None:
        raise NotFound("Пакет импорта недоступен")
    if batch.state == "committed":
        # Повтор применения не создаёт второй импорт (AR-28).
        return _report_from_batch(batch)
    if batch.state != "preview":
        raise ConflictError(f"Пакет в состоянии «{batch.state}» не применяется")

    difference = int(batch.reconciliation.get("difference_minor", 0))
    if difference != 0:
        raise ImportReconciliationFailed(
            "Сверка импорта не сошлась: применение заблокировано",
            details={"difference_minor": difference},
        )

    rows = (
        (
            await session.execute(
                select(ImportRow)
                .where(
                    ImportRow.workspace_id == workspace_id,
                    ImportRow.batch_id == batch_id,
                    ImportRow.status == "pending",
                    ImportRow.decision != "skip",
                )
                .order_by(ImportRow.occurred_date, ImportRow.id)
            )
        )
        .scalars()
        .all()
    )
    if len(rows) > max_rows:
        raise ConflictError(
            f"В пакете {len(rows)} строк при пределе {max_rows}: разделите импорт",
            details={"rows": len(rows), "limit": max_rows},
        )
    if not rows:
        # Повторный импорт неизменного снимка не создаёт новых данных и не
        # занимает место уже применённого пакета (A79, FR-63).
        batch.state = "cancelled"
        batch.version += 1
        await session.flush()
        return _report_from_batch(batch)

    batch.state = "committing"
    await session.flush()

    category_cache: dict[str, uuid.UUID] = {}
    for row in rows:
        category_id = await _ensure_category(
            session, uow, actor=actor, label=row.source_label, cache=category_cache
        )
        amount = Money(row.amount_minor, currency)
        spec = _spec_for_row(row, amount=amount, timezone=timezone, category_id=category_id)
        posted = await post_transaction(session, uow, actor=actor, spec=spec, origin="import")
        row.transaction_id = posted.transaction_id
        row.category_id = category_id
        row.status = "applied"

    batch.state = "committed"
    batch.committed_at = func.now()
    batch.version += 1
    await session.flush()
    await uow.bump_revisions(workspace_id, data=True, catalog=True, coverage=True)
    await uow.emit(
        workspace_id=workspace_id,
        event_type="ImportCommitted",
        aggregate_type="import_batch",
        aggregate_id=batch_id,
        payload={"batch_id": str(batch_id), "rows": len(rows)},
        actor_user_id=actor.user_id,
    )
    return _report_from_batch(batch)


def _spec_for_row(
    row: ImportRow, *, amount: Money, timezone: str, category_id: uuid.UUID
) -> TransactionSpec:
    """Спецификация импортной записи без выдуманного счёта и автора покупки."""
    granularity = Granularity(row.granularity)
    if row.transaction_type == "income":
        return TransactionSpec(
            transaction_type=TransactionType.INCOME,
            amount=amount,
            occurred_date=row.occurred_date or dt.date.today(),
            occurred_end_date=row.occurred_end_date,
            date_precision="interval" if row.occurred_end_date else "day",
            timezone=timezone,
            granularity=granularity,
            description=row.source_label[:300],
            allocations=(
                AllocationSpec(role=AllocationRole.INCOME, amount=amount, category_id=category_id),
            ),
            # Счёт неизвестен: движение фиксируется без привязки к счёту,
            # поэтому банковский остаток из истории не восстанавливается
            # (SOURCE_ANALYSIS §5, A65).
            cash_legs=(CashLegSpec(signed=amount, coverage=CoverageMode.UNKNOWN),),
        )
    if row.transaction_type == "legacy_unclassified_flow":
        return TransactionSpec(
            transaction_type=TransactionType.LEGACY_UNCLASSIFIED_FLOW,
            amount=amount,
            occurred_date=row.occurred_date or dt.date.today(),
            occurred_end_date=row.occurred_end_date,
            date_precision="interval" if row.occurred_end_date else "day",
            timezone=timezone,
            granularity=granularity,
            description=row.source_label[:300],
            allocations=(
                AllocationSpec(
                    role=AllocationRole.UNCLASSIFIED, amount=amount, category_id=category_id
                ),
            ),
            # Историческое движение неизвестного типа не проводится по счёту.
            cash_legs=(),
        )
    return TransactionSpec(
        transaction_type=TransactionType.EXPENSE,
        amount=amount,
        occurred_date=row.occurred_date or dt.date.today(),
        occurred_end_date=row.occurred_end_date,
        date_precision="interval" if row.occurred_end_date else "day",
        timezone=timezone,
        granularity=granularity,
        description=row.source_label[:300],
        allocations=(
            AllocationSpec(role=AllocationRole.EXPENSE, amount=amount, category_id=category_id),
        ),
        cash_legs=(CashLegSpec(signed=-amount, coverage=CoverageMode.UNKNOWN),),
    )


async def _ensure_category(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    label: str,
    cache: dict[str, uuid.UUID],
) -> uuid.UUID:
    name = _category_name_for(label)
    normalized = normalize_name(name)
    if normalized in cache:
        return cache[normalized]
    view = await create_category(session, uow, actor=actor, name=name)
    cache[normalized] = view.id
    return view.id


def _report_from_batch(batch: ImportBatch) -> ReconciliationReport:
    data = dict(batch.reconciliation)
    return ReconciliationReport(
        by_sheet=dict(data.get("by_sheet", {})),
        total_source_minor=int(data.get("total_source_minor", 0)),
        total_import_minor=int(data.get("total_import_minor", 0)),
        difference_minor=int(data.get("difference_minor", 0)),
        unknown_beneficiary_rows=0,
        special_rows=(),
    )


async def revert_import(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    batch_id: uuid.UUID,
) -> int:
    """Откат импорта через ledger, без удаления строк в обход зависимостей."""
    from fintracker.application.ledger.service import void_transaction

    workspace_id = actor.require_workspace()
    batch = (
        await session.execute(
            select(ImportBatch)
            .where(ImportBatch.workspace_id == workspace_id, ImportBatch.id == batch_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if batch is None or batch.state != "committed":
        raise NotFound("Применённый пакет импорта не найден")
    rows = (
        (
            await session.execute(
                select(ImportRow).where(
                    ImportRow.workspace_id == workspace_id,
                    ImportRow.batch_id == batch_id,
                    ImportRow.transaction_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    reverted = 0
    for row in rows:
        assert row.transaction_id is not None
        await void_transaction(
            session,
            uow,
            actor=actor,
            transaction_id=row.transaction_id,
            reason=f"Откат импорта {batch_id}",
        )
        row.status = "skipped"
        reverted += 1
    batch.state = "reverted"
    batch.version += 1
    await session.flush()
    return reverted
