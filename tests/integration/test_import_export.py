"""Импорт исходной таблицы и экспорт (FR-63–FR-67, A68–A84, A108, A200).

Используется реальная структура двух листов с обезличенными суммами
(QA-02); исходный файл не входит в репозиторий.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import pathlib
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.integrations.exporter import (
    build_matrix,
    build_snapshot,
    sanitize_cell,
    to_csv,
    to_xlsx,
)
from fintracker.application.integrations.importer import (
    build_preview,
    commit_import,
    revert_import,
)
from fintracker.application.integrations.sheet_parser import (
    ParsedSheet,
    ParsedWorkbook,
    SheetDailyCell,
    SheetIncome,
    SheetRow,
    parse_sheet_title,
    parse_workbook,
)
from fintracker.application.ledger.service import post_transaction
from fintracker.application.planning.plan import period_status
from fintracker.core.errors import ImportReconciliationFailed
from fintracker.db.models.ledger import Transaction, TransactionRevision
from tests.conftest import requires_pg
from tests.integration.factories import TZ, build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]

SOURCE_FILE = pathlib.Path(".research-private/source.xlsx")
NOW = dt.datetime(2026, 9, 12, 12, 0, tzinfo=dt.UTC)


def synthetic_workbook(*, snapshot_hash: str = "hash-1") -> ParsedWorkbook:
    """Обезличенный снимок с той же структурой, что исходная таблица."""
    start = dt.date(2026, 8, 10)
    rows = (
        SheetRow(
            index=14,
            label="Продукты питания / Супермаркеты",
            group="Продукты питания",
            detail="Супермаркеты",
            fact=Decimal("1500.00"),
            plan=Decimal("2000.00"),
            deviation=Decimal("500.00"),
        ),
        SheetRow(
            index=15,
            label="Рестораны / Заведения",
            group="Рестораны",
            detail="Заведения",
            fact=Decimal("700.50"),
            plan=Decimal("1000.00"),
            deviation=Decimal("299.50"),
        ),
        SheetRow(
            index=16,
            label="Долги",
            group="Долги",
            detail=None,
            fact=Decimal("300.00"),
            plan=None,
            deviation=None,
        ),
    )
    cells = (
        SheetDailyCell(
            sheet_name="10.08 - 09.09",
            row_label="Продукты питания / Супермаркеты",
            row_index=14,
            column_letter="I",
            day_offset=0,
            occurred_date=start,
            amount=Decimal("1000.00"),
            formula=None,
        ),
        SheetDailyCell(
            sheet_name="10.08 - 09.09",
            row_label="Продукты питания / Супермаркеты",
            row_index=14,
            column_letter="J",
            day_offset=1,
            occurred_date=start + dt.timedelta(days=1),
            amount=Decimal("500.00"),
            formula="=200+300",
        ),
        SheetDailyCell(
            sheet_name="10.08 - 09.09",
            row_label="Рестораны / Заведения",
            row_index=15,
            column_letter="K",
            day_offset=2,
            occurred_date=start + dt.timedelta(days=2),
            amount=Decimal("700.50"),
            formula="=(438+963)/2",
        ),
        SheetDailyCell(
            sheet_name="10.08 - 09.09",
            row_label="Долги",
            row_index=16,
            column_letter="L",
            day_offset=3,
            occurred_date=start + dt.timedelta(days=3),
            amount=Decimal("300.00"),
            formula=None,
        ),
    )
    sheet = ParsedSheet(
        name="10.08 - 09.09",
        period_start=start,
        period_end_exclusive=dt.date(2026, 9, 10),
        rows=rows,
        daily_cells=cells,
        incomes=(SheetIncome(label="Зарплата", amount=Decimal("5000.00"), occurred_date=None),),
    )
    return ParsedWorkbook(
        sheets=(sheet,),
        snapshot_hash=snapshot_hash,
        taken_at=NOW,
        source_name="synthetic.xlsx",
    )


def test_a68_sheet_title_gives_period_bounds() -> None:
    """A68: период листа «10.08 - 09.09» разбирается с переходом через год."""
    assert parse_sheet_title("10.08 - 09.09", year=2026) == (
        dt.date(2026, 8, 10),
        dt.date(2026, 9, 10),
    )
    assert parse_sheet_title("10.12 - 09.01", year=2026) == (
        dt.date(2026, 12, 10),
        dt.date(2027, 1, 10),
    )
    assert parse_sheet_title("Сводка", year=2026) is None


@pytest.mark.skipif(not SOURCE_FILE.exists(), reason="исходный снимок недоступен")
def test_a69_a74_a82_real_source_reconciles_to_kopeck() -> None:
    """A69/A74/A82: все строки сохранены, итоги не удвоены, сверка до копейки."""
    workbook = parse_workbook(SOURCE_FILE, year=2026, taken_at=NOW)
    assert len(workbook.sheets) == 2
    first, last = workbook.sheets
    assert len(first.rows) == 37, "37 бюджетных строк первого листа"
    assert len(last.rows) == 40, "40 бюджетных строк последнего листа"
    assert first.nonzero_cells == 24
    assert last.nonzero_cells == 89
    for sheet in workbook.sheets:
        assert sheet.daily_total() == sheet.rows_total(), "итог сходится до копейки"


@pytest.mark.skipif(not SOURCE_FILE.exists(), reason="исходный снимок недоступен")
def test_a73_formula_is_preserved_as_text() -> None:
    """A73: формула доли сохраняется текстом и не создаёт две покупки."""
    workbook = parse_workbook(SOURCE_FILE, year=2026, taken_at=NOW)
    formulas = [
        cell.formula for sheet in workbook.sheets for cell in sheet.daily_cells if cell.formula
    ]
    assert any("/2" in formula for formula in formulas), "формула деления доли найдена"
    halves = [
        cell
        for sheet in workbook.sheets
        for cell in sheet.daily_cells
        if cell.formula and "/2" in cell.formula
    ]
    # Одна ячейка даёт одну агрегированную запись, а не два слагаемых.
    assert all(cell.amount > 0 for cell in halves)


async def test_a72_daily_cell_becomes_single_aggregate(
    owner_session: AsyncSession,
) -> None:
    """A72: одна дневная ячейка — один агрегат, а не несколько покупок."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    preview = await build_preview(
        owner_session, actor=fixture.actor, workbook=synthetic_workbook(), currency="RUB"
    )
    assert preview.reconciliation.balanced
    assert not preview.blocked
    daily_rows = [row for row in preview.rows if row.granularity == "daily_aggregate"]
    assert len(daily_rows) == 4

    await commit_import(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        batch_id=preview.batch_id,
        currency="RUB",
        timezone=TZ,
        max_rows=5000,
    )
    granularities = (
        (
            await owner_session.execute(
                select(TransactionRevision.granularity).where(
                    TransactionRevision.workspace_id == fixture.workspace.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert granularities.count("daily_aggregate") == 4
    assert granularities.count("period_aggregate") == 1, "недатированный доход (A54)"


async def test_a79_repeated_snapshot_adds_nothing(owner_session: AsyncSession) -> None:
    """CMD-28, A79: повторный импорт неизменного снимка не создаёт новых операций."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    preview = await build_preview(
        owner_session, actor=fixture.actor, workbook=synthetic_workbook(), currency="RUB"
    )
    await commit_import(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        batch_id=preview.batch_id,
        currency="RUB",
        timezone=TZ,
        max_rows=5000,
    )
    first_count = int(
        (
            await owner_session.execute(
                select(func.count())
                .select_from(Transaction)
                .where(Transaction.workspace_id == fixture.workspace.id)
            )
        ).scalar_one()
    )

    repeat = await build_preview(
        owner_session, actor=fixture.actor, workbook=synthetic_workbook(), currency="RUB"
    )
    assert all(row.decision == "skip" for row in repeat.rows)
    await commit_import(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        batch_id=repeat.batch_id,
        currency="RUB",
        timezone=TZ,
        max_rows=5000,
    )
    second_count = int(
        (
            await owner_session.execute(
                select(func.count())
                .select_from(Transaction)
                .where(Transaction.workspace_id == fixture.workspace.id)
            )
        ).scalar_one()
    )
    assert second_count == first_count


async def test_a81_preview_alone_changes_nothing(owner_session: AsyncSession) -> None:
    """A81: после предпросмотра без подтверждения итоги не изменились."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    await build_preview(
        owner_session, actor=fixture.actor, workbook=synthetic_workbook(), currency="RUB"
    )
    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=dt.date(2026, 8, 20),
    )
    assert status.total_fact_minor == 0


async def test_a82_unbalanced_import_is_blocked(owner_session: AsyncSession) -> None:
    """A82: расхождение сверки блокирует применение импорта."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    workbook = synthetic_workbook(snapshot_hash="hash-broken")
    broken_sheet = workbook.sheets[0]
    # Факт строки не совпадает с суммой её дневных ячеек.
    tampered = ParsedSheet(
        name=broken_sheet.name,
        period_start=broken_sheet.period_start,
        period_end_exclusive=broken_sheet.period_end_exclusive,
        rows=(
            SheetRow(
                index=14,
                label="Продукты питания / Супермаркеты",
                group="Продукты питания",
                detail="Супермаркеты",
                fact=Decimal("9999.00"),
                plan=None,
                deviation=None,
            ),
        ),
        daily_cells=broken_sheet.daily_cells,
        incomes=(),
    )
    broken = ParsedWorkbook(
        sheets=(tampered,),
        snapshot_hash="hash-broken",
        taken_at=NOW,
        source_name="broken.xlsx",
    )
    preview = await build_preview(
        owner_session, actor=fixture.actor, workbook=broken, currency="RUB"
    )
    assert preview.blocked
    assert not preview.reconciliation.balanced
    with pytest.raises(ImportReconciliationFailed):
        await commit_import(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            batch_id=preview.batch_id,
            currency="RUB",
            timezone=TZ,
            max_rows=5000,
        )


async def test_a77_historic_debt_keeps_unknown_meaning(
    owner_session: AsyncSession,
) -> None:
    """A77: строка «Долги» остаётся движением неопределённого типа."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    preview = await build_preview(
        owner_session, actor=fixture.actor, workbook=synthetic_workbook(), currency="RUB"
    )
    await commit_import(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        batch_id=preview.batch_id,
        currency="RUB",
        timezone=TZ,
        max_rows=5000,
    )
    types = (
        (
            await owner_session.execute(
                select(TransactionRevision.transaction_type).where(
                    TransactionRevision.workspace_id == fixture.workspace.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert "legacy_unclassified_flow" in types
    # Историческое движение не маскируется под расход и не меняет счёт.
    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=dt.date(2026, 8, 20),
    )
    assert status.total_fact_minor == 220_050, "в расход вошли только продукты и рестораны"


async def test_import_revert_uses_ledger(owner_session: AsyncSession) -> None:
    """AR-28: откат импорта проходит через ledger, а не удалением строк."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    preview = await build_preview(
        owner_session, actor=fixture.actor, workbook=synthetic_workbook(), currency="RUB"
    )
    await commit_import(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        batch_id=preview.batch_id,
        currency="RUB",
        timezone=TZ,
        max_rows=5000,
    )
    reverted = await revert_import(
        owner_session, fixture.uow, actor=fixture.actor, batch_id=preview.batch_id
    )
    assert reverted == 5
    statuses = (
        (
            await owner_session.execute(
                select(Transaction.status).where(Transaction.workspace_id == fixture.workspace.id)
            )
        )
        .scalars()
        .all()
    )
    assert set(statuses) == {"voided"}, "записи отменены, а не удалены"


async def test_a63_import_batch_limit(owner_session: AsyncSession) -> None:
    """LIM-08: пакет больше предела не применяется как «целиком атомарный»."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    preview = await build_preview(
        owner_session, actor=fixture.actor, workbook=synthetic_workbook(), currency="RUB"
    )
    from fintracker.core.errors import ConflictError

    with pytest.raises(ConflictError, match="разделите импорт"):
        await commit_import(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            batch_id=preview.batch_id,
            currency="RUB",
            timezone=TZ,
            max_rows=2,
        )


def test_a108_formula_text_is_escaped() -> None:
    """SEC-08, A108: внешний текст, похожий на формулу, экспортируется как текст."""
    assert sanitize_cell("=SUM(A1:A9)").startswith("'=")
    assert sanitize_cell("+1234").startswith("'+")
    assert sanitize_cell("-1234").startswith("'-")
    assert sanitize_cell("@cmd").startswith("'@")
    assert sanitize_cell("Продукты") == "Продукты"
    assert sanitize_cell(None) == ""


async def test_a200_export_preserves_note_and_roles(owner_session: AsyncSession) -> None:
    """A200: экспорт различает автора, человека и получателя, экранирует заметку."""
    fixture = await build_fixture(owner_session)
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(
            fixture,
            amount=rub(1_200),
            category="Продукты",
            beneficiary="Софа",
            note='=HYPERLINK("evil")\nвторая строка с "кавычками"',
        ),
        origin="form",
    )
    snapshot = await build_snapshot(owner_session, workspace=fixture.workspace)
    assert len(snapshot.rows) == 1

    data = to_csv(snapshot).decode("utf-8-sig")
    reader = list(csv.reader(io.StringIO(data)))
    header, row = reader[0], reader[1]
    note_index = header.index("note")
    assert row[note_index].startswith("'="), "формула обезврежена"
    assert "\n" in row[note_index], "перенос строки сохранён"
    assert '"кавычками"' in row[note_index]

    xlsx = to_xlsx(snapshot)
    assert xlsx.startswith(b"PK"), "валидный XLSX"
    assert len(snapshot.rows[0].allocations) == 1
    assert snapshot.rows[0].allocations[0]["beneficiary"] == "Софа"


async def test_export_matrix_is_not_limited_to_31_columns(
    owner_session: AsyncSession,
) -> None:
    """FR-67: матрица строится по реальным датам интервала без обрезки."""
    fixture = await build_fixture(owner_session)
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(100), category="Продукты"),
        origin="form",
    )
    snapshot = await build_snapshot(owner_session, workspace=fixture.workspace)
    matrix = build_matrix(snapshot, start=dt.date(2026, 9, 1), end_inclusive=dt.date(2026, 11, 30))
    # 91 день интервала + колонка категории + итог.
    assert len(matrix[0]) == 91 + 2
