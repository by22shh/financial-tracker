"""Полнота учёта, сверка и замещение агрегатов (FR-69–FR-72, AR-20, RV04, RV05)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.analytics.coverage import (
    accept_reconciliation,
    quality_check,
    record_reconciliation,
    replace_aggregate,
    set_period_completeness,
)
from fintracker.application.ledger.service import (
    account_balance,
    load_current_spec,
    post_transaction,
    revise_transaction,
)
from fintracker.core.errors import ConflictError
from fintracker.core.money import Money
from fintracker.db.models.commitments import Reconciliation
from fintracker.db.models.planning import BudgetPeriod
from tests.conftest import requires_pg
from tests.integration.factories import build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)


async def test_fr71_reconciliation_shows_difference(owner_session: AsyncSession) -> None:
    """CMD-15, FR-71: сверка показывает расхождение и не превращает его в доход."""
    fixture = await build_fixture(owner_session)
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1_000), category="Продукты", account="Карта"),
        origin="form",
    )
    result = await record_reconciliation(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        account_id=fixture.accounts["Карта"],
        cutoff_date=DAY,
        observed=Money(-90_000, "RUB"),
    )
    assert result.computed_minor == -100_000
    assert result.difference_minor == 10_000
    assert not result.matches
    assert "Расхождение" in result.explanation("RUB")


async def test_rv05_matching_balance_does_not_prove_completeness(
    owner_session: AsyncSession,
) -> None:
    """RV05: совпадение остатка не объявляет потоки полностью проверенными."""
    fixture = await build_fixture(owner_session)
    result = await record_reconciliation(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        account_id=fixture.accounts["Карта"],
        cutoff_date=DAY,
        observed=Money(0, "RUB"),
    )
    assert result.matches
    text = result.explanation("RUB")
    assert "не доказывает полноту расходов" in text


async def test_reconciliation_adjustment_is_not_income(
    owner_session: AsyncSession,
) -> None:
    """FR-71: техническая корректировка меняет баланс, но не доходы и расходы."""
    fixture = await build_fixture(owner_session)
    result = await record_reconciliation(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        account_id=fixture.accounts["Карта"],
        cutoff_date=DAY,
        observed=Money(50_000, "RUB"),
    )
    await accept_reconciliation(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        reconciliation_id=result.reconciliation_id,
        adjust=True,
        reason="Найдено пополнение вне учёта",
    )
    balance = await account_balance(
        owner_session,
        workspace_id=fixture.workspace.id,
        account_id=fixture.accounts["Карта"],
    )
    assert balance == 50_000

    from fintracker.application.planning.plan import period_status

    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=DAY,
    )
    assert status.total_fact_minor == 0, "корректировка не попала в расходы"


async def test_reference_account_cannot_be_reconciled(
    owner_session: AsyncSession,
) -> None:
    """R01: reference-счёт не даёт достоверного остатка и не сверяется."""
    fixture = await build_fixture(owner_session, accounts=(("Личная карта", "reference"),))
    with pytest.raises(ConflictError, match="полным учётом"):
        await record_reconciliation(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            account_id=fixture.accounts["Личная карта"],
            cutoff_date=DAY,
            observed=Money(100_000, "RUB"),
        )


async def test_ar20_money_change_marks_reconciliation_stale_note_does_not(
    owner_session: AsyncSession,
) -> None:
    """AR-20: денежная правка делает сверку stale, изменение заметки — нет."""
    from dataclasses import replace

    fixture = await build_fixture(owner_session)
    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1_000), category="Продукты", account="Карта"),
        origin="form",
    )
    result = await record_reconciliation(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        account_id=fixture.accounts["Карта"],
        cutoff_date=DAY,
        observed=Money(-100_000, "RUB"),
    )
    await accept_reconciliation(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        reconciliation_id=result.reconciliation_id,
        adjust=False,
    )

    _, _, spec = await load_current_spec(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=posted.transaction_id,
    )
    # Изменение только заметки не сбрасывает сверенный баланс.
    await revise_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        transaction_id=posted.transaction_id,
        new_spec=replace(spec, note="уточнение"),
        expected_version=None,
        change_kind="note_changed",
    )
    row = (
        await owner_session.execute(
            select(Reconciliation).where(Reconciliation.id == result.reconciliation_id)
        )
    ).scalar_one()
    assert not row.is_stale

    # Денежная правка делает сверку требующей проверки.
    await revise_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        transaction_id=posted.transaction_id,
        new_spec=expense_spec(fixture, amount=rub(800), category="Продукты", account="Карта"),
        expected_version=None,
    )
    await owner_session.refresh(row)
    assert row.is_stale


async def test_fr69_personal_confirmation_does_not_close_whole_period(
    owner_session: AsyncSession,
) -> None:
    """FR-69: подтверждение одним человеком своей области не закрывает период."""
    from fintracker.application.catalog.directory import create_person

    fixture = await build_fixture(owner_session)
    person = await create_person(owner_session, fixture.uow, actor=fixture.actor, name="Софа")
    await set_period_completeness(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        period_id=fixture.period.id,
        status="confirmed_complete",
        basis="Проверила свои траты",
        scope_person_id=person.id,
    )
    period = (
        await owner_session.execute(
            select(BudgetPeriod).where(BudgetPeriod.id == fixture.period.id)
        )
    ).scalar_one()
    assert period.completeness == "incomplete", "общая полнота не закрыта"

    await set_period_completeness(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        period_id=fixture.period.id,
        status="confirmed_complete",
        basis="Проверены все участники и счета",
    )
    await owner_session.refresh(period)
    assert period.completeness == "confirmed_complete"


async def test_r02_quality_check_lists_open_items(owner_session: AsyncSession) -> None:
    """FR-70, FR-38, R02: экран «Проверить учёт» показывает незакрытые места."""
    from dataclasses import replace

    from fintracker.domain.ledger.model import AllocationRole, AllocationSpec

    fixture = await build_fixture(owner_session)
    spec = expense_spec(fixture, amount=rub(400), category="Продукты")
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=replace(
            spec,
            allocations=(AllocationSpec(role=AllocationRole.EXPENSE, amount=rub(400)),),
        ),
        origin="form",
    )
    await record_reconciliation(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        account_id=fixture.accounts["Карта"],
        cutoff_date=DAY,
        observed=Money(12_345, "RUB"),
    )
    report = await quality_check(owner_session, workspace_id=fixture.workspace.id, currency="RUB")
    assert report.uncategorized_count == 1
    assert report.uncategorized_minor == 40_000
    assert report.unexplained_reconciliations == 1
    assert report.periods_incomplete >= 1


async def test_a83_a84_aggregate_replacement_requires_matching_sum(
    owner_session: AsyncSession,
) -> None:
    """TECH-07, A83/A84: агрегат и детали не суммируются; расхождение требует решения."""
    from dataclasses import replace

    from fintracker.domain.ledger.model import (
        CashLegSpec,
        CoverageMode,
        Granularity,
    )

    fixture = await build_fixture(owner_session)
    aggregate_spec = replace(
        expense_spec(fixture, amount=rub(1_000), category="Продукты"),
        granularity=Granularity.DAILY_AGGREGATE,
        cash_legs=(CashLegSpec(signed=-rub(1_000), coverage=CoverageMode.UNKNOWN),),
    )
    aggregate = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=aggregate_spec,
        origin="import",
    )
    detail_one = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(600), category="Продукты"),
        origin="form",
    )

    with pytest.raises(ConflictError, match="отличается"):
        await replace_aggregate(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            aggregate_transaction_id=aggregate.transaction_id,
            replacement_transaction_ids=[detail_one.transaction_id],
        )

    detail_two = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(400), category="Продукты"),
        origin="form",
    )
    replaced = await replace_aggregate(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        aggregate_transaction_id=aggregate.transaction_id,
        replacement_transaction_ids=[
            detail_one.transaction_id,
            detail_two.transaction_id,
        ],
    )
    assert replaced == 2

    from fintracker.application.planning.plan import period_status

    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=DAY,
    )
    # Итог содержит только подробные операции, агрегат не суммируется с ними.
    assert status.total_fact_minor == 100_000
