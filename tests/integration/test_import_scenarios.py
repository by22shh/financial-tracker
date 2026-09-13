"""Импорт и экспорт исторической таблицы (A70, A80, A196, A230, A37, A85, A89, A90)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.analytics.reports import spending_report
from fintracker.application.ledger.operations import post_refund, refundable_parts
from fintracker.application.ledger.service import post_transaction
from fintracker.db.models.ledger import Transaction, TransactionRevision
from tests.conftest import requires_pg
from tests.integration.factories import TZ, build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)


async def test_a70_same_row_number_does_not_merge_categories(
    owner_session: AsyncSession,
) -> None:
    """A70: одинаковый номер строки в источнике не объединяет разные статьи."""
    from fintracker.application.integrations.importer import _category_name_for, _source_key

    first = _source_key("Сентябрь", "11 / Продукты", "B", dt.date(2026, 9, 3))
    second = _source_key("Сентябрь", "11 / Рестораны", "B", dt.date(2026, 9, 3))
    assert first != second, "ключ строки включает подпись, а не только номер"
    assert _category_name_for("11 / Продукты") != _category_name_for("11 / Рестораны")


async def test_a80_changed_cell_creates_revision_not_sum(owner_session: AsyncSession) -> None:
    """A80: изменённая ячейка даёт новую ревизию, а не прибавление суммы."""
    from dataclasses import replace

    from fintracker.application.ledger.service import load_current_spec, revise_transaction
    from fintracker.core.money import Money
    from fintracker.domain.ledger.model import Granularity

    fixture = await build_fixture(owner_session)
    spec = expense_spec(fixture, amount=rub(1_000), category="Продукты", occurred=DAY)
    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=replace(spec, granularity=Granularity.DAILY_AGGREGATE),
        origin="import",
    )
    _, _, current = await load_current_spec(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=posted.transaction_id,
    )
    updated_amount = Money(150_000, "RUB")
    await revise_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        transaction_id=posted.transaction_id,
        new_spec=replace(
            current,
            amount=updated_amount,
            allocations=(replace(current.allocations[0], amount=updated_amount),),
            cash_legs=tuple(replace(leg, signed=-updated_amount) for leg in current.cash_legs),
        ),
        expected_version=None,
        change_kind="import_revision",
    )

    revisions = (
        (
            await owner_session.execute(
                select(TransactionRevision)
                .where(TransactionRevision.transaction_id == posted.transaction_id)
                .order_by(TransactionRevision.revision)
            )
        )
        .scalars()
        .all()
    )
    assert [row.amount_minor for row in revisions] == [100_000, 150_000]
    assert revisions[-1].change_kind == "import_revision"

    transactions = (
        (
            await owner_session.execute(
                select(Transaction).where(Transaction.workspace_id == fixture.workspace.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(transactions) == 1, "правка не создаёт вторую операцию"

    workspace = fixture.workspace
    report = await spending_report(
        owner_session,
        workspace=workspace,
        date_from=DAY,
        date_to_exclusive=DAY + dt.timedelta(days=1),
    )
    assert report.total_minor == 150_000, "сумма заменена, а не удвоена"


async def test_a196_import_does_not_invent_spender(owner_session: AsyncSession) -> None:
    """A196: имя в исходной строке не делает человека совершившим покупку."""
    from dataclasses import replace

    from fintracker.domain.ledger.model import Granularity

    fixture = await build_fixture(owner_session)
    spec = expense_spec(fixture, amount=rub(800), category="Продукты", occurred=DAY)
    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=replace(
            spec,
            granularity=Granularity.DAILY_AGGREGATE,
            description="Заведения / Софа",
        ),
        origin="import",
    )
    revision = (
        await owner_session.execute(
            select(TransactionRevision).where(
                TransactionRevision.transaction_id == posted.transaction_id
            )
        )
    ).scalar_one()
    assert revision.spender_person_id is None, "совершивший покупку остаётся неизвестным"
    assert "Софа" in (revision.description or ""), "исходная подпись сохранена как есть"


async def test_a37_refund_in_next_period_reduces_current_line(
    owner_session: AsyncSession,
) -> None:
    """A37: возврат в следующем периоде уменьшает текущую статью, прошлый отчёт цел."""
    from fintracker.application.planning.periods import period_for_date

    fixture = await build_fixture(owner_session, start=dt.date(2026, 9, 10))
    purchase = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(3_000), category="Продукты", occurred=DAY),
        origin="form",
    )
    parts = await refundable_parts(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=purchase.transaction_id,
    )
    refund_day = dt.date(2026, 10, 12)
    await post_refund(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        source_transaction_id=purchase.transaction_id,
        parts={parts[0].stable_line_id: rub(1_000)},
        occurred_date=refund_day,
        timezone=TZ,
    )

    old_report = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=dt.date(2026, 9, 10),
        date_to_exclusive=dt.date(2026, 10, 10),
    )
    assert old_report.total_minor == 300_000, "прошлый фактический отчёт не переписан"

    await period_for_date(owner_session, workspace_id=fixture.workspace.id, day=refund_day)
    new_report = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=dt.date(2026, 10, 10),
        date_to_exclusive=dt.date(2026, 11, 10),
    )
    assert new_report.total_minor == -100_000, "возврат уменьшает статью текущего периода"


async def test_a85_group_total_equals_sum_of_operations(owner_session: AsyncSession) -> None:
    """FR-57, A85: итог группы равен сумме операций после тех же фильтров."""
    fixture = await build_fixture(owner_session)
    for amount, category in ((1_000, "Продукты"), (2_000, "Продукты"), (500, "Рестораны")):
        await post_transaction(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            spec=expense_spec(fixture, amount=rub(amount), category=category, occurred=DAY),
            origin="form",
        )
    report = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=DAY,
        date_to_exclusive=DAY + dt.timedelta(days=1),
    )
    assert report.total_minor == sum(row.amount_minor for row in report.rows)
    groceries = next(row for row in report.rows if "Продукты" in row.label)
    assert groceries.amount_minor == 300_000
    assert groceries.transaction_count == 2


async def test_a89_average_check_is_refused_on_aggregates(owner_session: AsyncSession) -> None:
    """A89: по дневным агрегатам средний чек не выводится, суммы доступны."""
    from dataclasses import replace

    from fintracker.domain.ledger.model import Granularity

    fixture = await build_fixture(owner_session)
    spec = expense_spec(fixture, amount=rub(5_000), category="Продукты", occurred=DAY)
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=replace(spec, granularity=Granularity.DAILY_AGGREGATE),
        origin="import",
    )
    report = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=DAY,
        date_to_exclusive=DAY + dt.timedelta(days=1),
    )
    row = report.rows[0]
    assert row.has_aggregate is True
    assert row.individual_count == 0, "количество покупок по агрегату неизвестно"
    assert row.amount_minor == 500_000, "сумма по категории доступна"


async def test_a90_category_contribution_without_causality(owner_session: AsyncSession) -> None:
    """A90: вклад категорий в разницу вычислен, причинность не домыслена."""
    from fintracker.application.analytics.reviews import explain_changes

    fixture = await build_fixture(owner_session)
    for offset in range(2):
        await post_transaction(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            spec=expense_spec(
                fixture,
                amount=rub(1_000),
                category="Продукты",
                occurred=dt.date(2026, 9, 3) + dt.timedelta(days=offset),
            ),
            origin="form",
        )
    for offset in range(3):
        await post_transaction(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            spec=expense_spec(
                fixture,
                amount=rub(1_000),
                category="Продукты",
                occurred=dt.date(2026, 9, 10) + dt.timedelta(days=offset),
            ),
            origin="form",
        )
    previous = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=dt.date(2026, 9, 3),
        date_to_exclusive=dt.date(2026, 9, 10),
    )
    current = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=dt.date(2026, 9, 10),
        date_to_exclusive=dt.date(2026, 9, 17),
    )
    changes = explain_changes(current, previous)
    assert changes[0].delta_minor == 100_000
    text = changes[0].describe("RUB")
    assert "покупок 2 → 3" in text
    for forbidden in ("потому что", "из-за", "причина"):
        assert forbidden not in text.lower()
