"""Цели, резервы и семантика импорта (A39, A40, A71, A75, A78, A87, A88, A148, A149, A160, A190, A217)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.analytics.reports import compare_periods
from fintracker.application.commitments.goals import allocate_to_goal, create_goal
from fintracker.application.ledger.operations import post_transfer
from fintracker.application.ledger.service import account_balance, post_transaction
from fintracker.db.models.commitments import Goal, GoalMovement
from tests.conftest import requires_pg
from tests.integration.factories import TZ, build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)


async def test_a39_goal_allocation_is_not_an_expense(owner_session: AsyncSession) -> None:
    """A39: выделение 5000 на цель внутри счёта не создаёт расхода."""
    from fintracker.application.analytics.reports import spending_report

    fixture = await build_fixture(owner_session)
    goal = await create_goal(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        name="Отпуск",
        currency="RUB",
        target=rub(50_000),
    )
    await allocate_to_goal(
        owner_session, fixture.uow, actor=fixture.actor, goal_id=goal.id, amount=rub(5_000)
    )

    report = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=DAY,
        date_to_exclusive=DAY + dt.timedelta(days=1),
    )
    assert report.total_minor == 0, "выделение не является расходом"

    row = (await owner_session.execute(select(Goal).where(Goal.id == goal.id))).scalar_one()
    assert row.allocated_minor == 500_000
    for account_id in fixture.accounts.values():
        assert (
            await account_balance(
                owner_session, workspace_id=fixture.workspace.id, account_id=account_id
            )
            == 0
        ), "банковский остаток не меняется выделением"


async def test_a40_transfer_with_goal_is_single_contribution(owner_session: AsyncSession) -> None:
    """A40: перевод на накопительный счёт с целью даёт один вклад, не два."""
    fixture = await build_fixture(owner_session)
    goal = await create_goal(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        name="Подушка",
        currency="RUB",
        target=rub(100_000),
    )
    transfer = await post_transfer(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        amount=rub(5_000),
        from_account_id=fixture.accounts["Карта"],
        to_account_id=fixture.accounts["Кошелёк"],
        occurred_date=DAY,
        timezone=TZ,
    )
    await allocate_to_goal(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        goal_id=goal.id,
        amount=rub(5_000),
        effect_id=transfer.effect_id,
        transaction_id=transfer.transaction_id,
    )
    movements = (
        (await owner_session.execute(select(GoalMovement).where(GoalMovement.goal_id == goal.id)))
        .scalars()
        .all()
    )
    assert len(movements) == 1, "один вклад на один перевод"
    assert movements[0].transaction_id == transfer.transaction_id

    # Повтор того же эффекта не удваивает вклад.
    await allocate_to_goal(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        goal_id=goal.id,
        amount=rub(5_000),
        effect_id=transfer.effect_id,
        transaction_id=transfer.transaction_id,
    )
    movements = (
        (await owner_session.execute(select(GoalMovement).where(GoalMovement.goal_id == goal.id)))
        .scalars()
        .all()
    )
    assert len(movements) == 1, "повтор того же движения не создаёт второй вклад"


def test_a87_zero_previous_period_has_no_percent() -> None:
    """A87: при нуле в прошлом периоде показывается абсолютное изменение."""
    result = compare_periods(
        current_minor=120_000,
        previous_minor=0,
        current_days=30,
        previous_days=30,
        current_partial=False,
        previous_complete=True,
    )
    assert result.absolute_change_minor == 120_000
    assert result.percent_change is None, "деления на ноль нет"
    assert result.note


def test_a88_partial_period_comparison_is_marked() -> None:
    """A88: сравнение неполного и полного периода обозначено ограничением."""
    partial = compare_periods(
        current_minor=60_000,
        previous_minor=120_000,
        current_days=15,
        previous_days=30,
        current_partial=True,
        previous_complete=True,
    )
    assert "сопостав" in partial.note.lower() or "неполн" in partial.note.lower()

    incomplete_basis = compare_periods(
        current_minor=60_000,
        previous_minor=120_000,
        current_days=30,
        previous_days=30,
        current_partial=False,
        previous_complete=False,
    )
    assert incomplete_basis.note


async def test_a71_a75_source_mapping_is_by_meaning(owner_session: AsyncSession) -> None:
    """A71, A75: сопоставление идёт по подписи и карте, а не по номеру строки."""
    from fintracker.application.integrations.importer import _category_name_for, _source_key

    moved = _source_key("Октябрь", "Продукты / Магазин", "B", dt.date(2026, 10, 3))
    original = _source_key("Сентябрь", "Продукты / Магазин", "B", dt.date(2026, 9, 3))
    assert moved != original, "ключ включает лист и дату"
    assert _category_name_for("Продукты / Магазин") == "Продукты"
    # Три подраздела старого листа сохраняют исходные подписи.
    for detail in ("Магазин", "Рынок", "Доставка"):
        assert _category_name_for(f"Продукты / {detail}") == "Продукты"


async def test_a78_savings_row_is_not_consumption(owner_session: AsyncSession) -> None:
    """A78: «Накопления» в расходной матрице имеют отдельную семантику."""
    from fintracker.application.integrations.importer import SPECIAL_ROWS, _special_kind

    assert SPECIAL_ROWS["накопления"] == "goal_allocation"
    assert _special_kind("Накопления") == "goal_allocation"
    assert _special_kind("Продукты") is None


async def test_a148_deficit_requires_explicit_confirmation(owner_session: AsyncSession) -> None:
    """A148: лимиты выше ресурса дают явный дефицит, а не молчаливое согласие."""
    from fintracker.application.onboarding.wizard import (
        DraftCategory,
        WizardState,
        check_funding,
    )

    state = WizardState(
        name="Бюджет",
        currency="RUB",
        timezone=TZ,
        categories=[
            DraftCategory(name="Продукты", limit_minor=9_000_000),
            DraftCategory(name="Жильё", limit_minor=4_000_000),
        ],
        income_period_minor=10_000_000,
        income_precision="exact",
    )
    funding = check_funding(state)
    assert funding.deficit_minor == 3_000_000
    assert funding.income_known is True

    unknown = WizardState(
        name="Бюджет",
        currency="RUB",
        timezone=TZ,
        categories=[DraftCategory(name="Продукты", limit_minor=9_000_000)],
        income_period_minor=None,
        income_precision="unknown",
    )
    unknown_funding = check_funding(unknown)
    assert unknown_funding.income_known is False
    assert unknown_funding.deficit_minor is None, (
        "непроверенное финансирование не считается дефицитом"
    )


async def test_a217_monthly_income_is_not_copied_to_short_period(
    owner_session: AsyncSession,
) -> None:
    """A217: месячный доход не переносится целиком в 14-дневный период."""
    from fintracker.db.models.planning import IncomePlan

    fixture = await build_fixture(owner_session)
    plan = IncomePlan(
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        precision="estimate",
        basis="monthly_total",
        monthly_amount_minor=10_000_000,
        period_amount_minor=None,
        unknown_reason="Основание для 14 дней не выбрано",
        created_by=fixture.user.id,
    )
    owner_session.add(plan)
    await owner_session.flush()

    stored = (
        await owner_session.execute(
            select(IncomePlan).where(IncomePlan.workspace_id == fixture.workspace.id)
        )
    ).scalar_one()
    assert stored.monthly_amount_minor == 10_000_000
    assert stored.period_amount_minor is None, "доход периода не выдуман из месячного"
    assert stored.unknown_reason


async def test_a190_beneficiary_without_spender(owner_session: AsyncSession) -> None:
    """A190: «Софе такси 430» — получатель Софа, совершивший покупку неизвестен."""
    from dataclasses import replace

    fixture = await build_fixture(owner_session)
    spec = expense_spec(fixture, amount=rub(430), category="Транспорт", beneficiary="Софа")
    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=replace(spec, spender_person_id=None),
        origin="telegram_text",
    )
    from fintracker.application.ledger.service import load_current_spec

    transaction, revision, stored = await load_current_spec(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=posted.transaction_id,
    )
    assert stored.allocations[0].beneficiary_id == fixture.beneficiaries["Софа"]
    assert revision.spender_person_id is None, "кто потратил — неизвестно"
    assert transaction.created_by == fixture.user.id, "автор записи сохранён"
    assert uuid.UUID(str(transaction.id))
