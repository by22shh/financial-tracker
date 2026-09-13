"""Числовые примеры бюджетных расчётов B1–B10 (раздел 5 ACCEPTANCE)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.commitments.goals import (
    allocate_to_goal,
    create_goal,
    suggested_contribution,
    use_goal,
)
from fintracker.application.ledger.operations import post_refund, refundable_parts
from fintracker.application.ledger.service import post_transaction
from fintracker.application.planning.plan import (
    LimitState,
    PlanLineSpec,
    create_budget_version,
    line_key,
    period_status,
)
from fintracker.application.planning.rollover import propose_rollovers
from fintracker.core.money import Money
from fintracker.db.models.commitments import (
    Occurrence,
    ScheduledItem,
    ScheduleVersion,
)
from fintracker.db.models.planning import BudgetPeriod, Rollover
from tests.conftest import requires_pg
from tests.integration.factories import TZ, Fixture, build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)


async def _line(fixture: Fixture, session: AsyncSession, category: str, today: dt.date = DAY):
    status = await period_status(
        session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=today,
    )
    key = line_key(fixture.categories[category], None)
    return next(
        item for item in status.lines if line_key(item.category_id, item.beneficiary_id) == key
    )


async def _add_commitment(
    session: AsyncSession,
    fixture: Fixture,
    *,
    category: str,
    expected: Money,
    due_date: dt.date,
    settled: Money | None = None,
) -> Occurrence:
    schedule = ScheduledItem(
        workspace_id=fixture.workspace.id,
        name="Плановый платёж",
        direction="payment",
        created_by=fixture.user.id,
    )
    session.add(schedule)
    await session.flush()
    session.add(
        ScheduleVersion(
            workspace_id=fixture.workspace.id,
            schedule_id=schedule.id,
            version=1,
            effective_from=due_date,
            rule_kind="monthly",
            anchor_date=due_date,
            currency="RUB",
            expected_minor=expected.minor,
            category_id=fixture.categories[category],
            created_by=fixture.user.id,
        )
    )
    occurrence = Occurrence(
        workspace_id=fixture.workspace.id,
        schedule_id=schedule.id,
        schedule_version=1,
        occurrence_slot=0,
        original_due_date=due_date,
        due_date=due_date,
        expected_minor=expected.minor,
        settled_minor=settled.minor if settled else 0,
        state="partially_settled" if settled else "planned",
    )
    session.add(occurrence)
    await session.flush()
    return occurrence


async def test_b1_remaining_and_future_payment(owner_session: AsyncSession) -> None:
    """FORM-01, FR-35, B1: лимит 10000, покупки 6000, возврат 1000, обязательство 2000."""
    fixture = await build_fixture(owner_session, limits={"Продукты": 1_000_000})
    purchase = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(6_000), category="Продукты"),
        origin="form",
    )
    parts = await refundable_parts(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=purchase.transaction_id,
    )
    await post_refund(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        source_transaction_id=purchase.transaction_id,
        parts={parts[0].stable_line_id: rub(1_000)},
        occurred_date=DAY,
        timezone=TZ,
    )
    occurrence = await _add_commitment(
        owner_session,
        fixture,
        category="Продукты",
        expected=rub(2_000),
        due_date=dt.date(2026, 10, 5),
    )

    line = await _line(fixture, owner_session, "Продукты")
    assert line.fact_minor == 500_000, "чистый факт 5000 ₽"
    assert line.remaining_minor == 500_000, "остаток лимита 5000 ₽"
    assert line.commitments_minor == 200_000
    assert line.available_minor == 300_000, "остаток после обязательств 3000 ₽"

    # После оплаты обязательства факт растёт, остаток после обязательств тот же.
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(2_000), category="Продукты"),
        origin="form",
    )
    occurrence.settled_minor = 200_000
    occurrence.state = "settled"
    await owner_session.flush()

    line_after = await _line(fixture, owner_session, "Продукты")
    assert line_after.fact_minor == 700_000
    assert line_after.commitments_minor == 0
    assert line_after.available_minor == 300_000


async def test_b2_zero_and_unset_limit(owner_session: AsyncSession) -> None:
    """FORM-02, FR-08, B2: нулевой и незаданный лимит различаются; процент не считается."""
    fixture = await build_fixture(owner_session, limits={"Продукты": 0, "Рестораны": None})
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(500), category="Продукты"),
        origin="form",
    )
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(500), category="Рестораны"),
        origin="form",
    )
    zero_line = await _line(fixture, owner_session, "Продукты")
    assert zero_line.limit_state is LimitState.ZERO
    assert zero_line.usage_percent is None
    assert "Расход вне плана" in zero_line.status_text

    unset_line = await _line(fixture, owner_session, "Рестораны")
    assert unset_line.limit_state is LimitState.NOT_SET
    assert unset_line.status_text == "Лимит не задан"

    # Нулевой расход и нулевой план не создают предупреждения о перерасходе.
    empty_line = await _line(fixture, owner_session, "Транспорт")
    assert empty_line.fact_minor == 0
    assert "Превышен" not in empty_line.status_text


async def test_b2_negative_rollover_shows_deficit(owner_session: AsyncSession) -> None:
    """B2: назначено 1000 и перенос −1500 дают эффективный лимит −500."""
    fixture = await build_fixture(owner_session, limits={"Продукты": 100_000})
    previous = BudgetPeriod(
        workspace_id=fixture.workspace.id,
        policy_id=fixture.period.policy_id,
        policy_version=1,
        sequence=-1,
        start_date=dt.date(2026, 8, 10),
        end_exclusive=dt.date(2026, 9, 10),
        state="ended",
    )
    owner_session.add(previous)
    await owner_session.flush()

    from fintracker.application.planning.plan import current_budget_version
    from fintracker.db.models.planning import BudgetLine

    version = await current_budget_version(
        owner_session, workspace_id=fixture.workspace.id, period_id=fixture.period.id
    )
    assert version is not None
    stable = (
        await owner_session.execute(
            select(BudgetLine.stable_line_id).where(
                BudgetLine.workspace_id == fixture.workspace.id,
                BudgetLine.budget_version_id == version.id,
                BudgetLine.category_id == fixture.categories["Продукты"],
            )
        )
    ).scalar_one()
    owner_session.add(
        Rollover(
            workspace_id=fixture.workspace.id,
            source_period_id=previous.id,
            destination_period_id=fixture.period.id,
            stable_line_id=stable,
            amount_minor=-150_000,
            mode="signed",
            status="accepted",
            basis_completeness="confirmed_complete",
        )
    )
    await owner_session.flush()

    line = await _line(fixture, owner_session, "Продукты")
    assert line.effective_limit_minor == -50_000
    assert line.limit_state is LimitState.NEGATIVE_ROLLOVER
    assert line.usage_percent is None, "деления на отрицательный лимит нет"
    assert "Дефицит переноса" in line.status_text


async def test_b3_group_and_beneficiaries_counted_once(owner_session: AsyncSession) -> None:
    """B3: общий расход считается один раз и не удваивается по получателям."""
    fixture = await build_fixture(owner_session, categories=("Рестораны",))

    lines = [
        PlanLineSpec(
            category_id=fixture.categories["Рестораны"],
            beneficiary_id=fixture.beneficiaries["Общее"],
            limit_minor=600_000,
        ),
        PlanLineSpec(
            category_id=fixture.categories["Рестораны"],
            beneficiary_id=fixture.beneficiaries["Ниджат"],
            limit_minor=200_000,
        ),
        PlanLineSpec(
            category_id=fixture.categories["Рестораны"],
            beneficiary_id=fixture.beneficiaries["Софа"],
            limit_minor=200_000,
        ),
    ]
    await create_budget_version(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        kind="working",
        plan_status="approved",
        origin="manual",
        lines=lines,
        overall_limit_minor=900_000,
    )
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1_500), category="Рестораны", beneficiary="Общее"),
        origin="form",
    )
    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=DAY,
    )
    # Группа показывает 10 000 ₽; общий предел не добавляет новую статью.
    assert status.total_limit_minor == 1_000_000
    assert status.overall_limit_minor == 900_000
    assert status.total_fact_minor == 150_000
    personal = [line for line in status.lines if line.beneficiary_name in {"Ниджат", "Софа"}]
    assert all(line.fact_minor == 0 for line in personal), "личные расходы не выросли"


async def test_b4_baseline_versus_working_plan(owner_session: AsyncSession) -> None:
    """FR-37, CMD-19, B4: превышение исходного плана видно после повышения текущего лимита."""
    fixture = await build_fixture(owner_session, limits={"Рестораны": 500_000})
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(6_500), category="Рестораны"),
        origin="form",
    )
    await create_budget_version(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        kind="working",
        plan_status="approved",
        origin="manual",
        lines=[
            PlanLineSpec(category_id=fixture.categories["Рестораны"], limit_minor=700_000),
            PlanLineSpec(category_id=fixture.categories["Продукты"]),
            PlanLineSpec(category_id=fixture.categories["Транспорт"]),
        ],
    )
    working = await _line(fixture, owner_session, "Рестораны")
    assert working.effective_limit_minor == 700_000
    assert working.remaining_minor == 50_000, "остаток текущего плана 500 ₽"

    from fintracker.application.planning.plan import current_budget_version
    from fintracker.db.models.planning import BudgetLine

    baseline = await current_budget_version(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        kind="baseline",
    )
    assert baseline is not None
    baseline_limit = (
        await owner_session.execute(
            select(BudgetLine.limit_minor).where(
                BudgetLine.workspace_id == fixture.workspace.id,
                BudgetLine.budget_version_id == baseline.id,
                BudgetLine.category_id == fixture.categories["Рестораны"],
            )
        )
    ).scalar_one()
    # Историческое отклонение не исчезает после повышения лимита.
    assert baseline_limit == 500_000
    assert 650_000 - baseline_limit == 150_000


async def test_b5_positive_rollover(owner_session: AsyncSession) -> None:
    """B5: остаток 1500 переносится и не увеличивает доход."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    from fintracker.application.planning.periods import ensure_periods
    from fintracker.application.planning.plan import current_budget_version
    from fintracker.db.models.planning import BudgetLine

    version = await current_budget_version(
        owner_session, workspace_id=fixture.workspace.id, period_id=fixture.period.id
    )
    assert version is not None
    await owner_session.execute(
        BudgetLine.__table__.update()
        .where(
            BudgetLine.workspace_id == fixture.workspace.id,
            BudgetLine.budget_version_id == version.id,
            BudgetLine.category_id == fixture.categories["Продукты"],
        )
        .values(limit_minor=400_000, rollover_mode="positive_only")
    )
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(
            fixture, amount=rub(2_500), category="Продукты", occurred=dt.date(2026, 8, 15)
        ),
        origin="form",
    )
    await ensure_periods(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 9, 15)
    )
    next_period = (
        await owner_session.execute(
            select(BudgetPeriod).where(
                BudgetPeriod.workspace_id == fixture.workspace.id,
                BudgetPeriod.start_date == dt.date(2026, 9, 10),
            )
        )
    ).scalar_one()
    fixture.period.completeness = "confirmed_complete"
    await owner_session.flush()
    created = await propose_rollovers(
        owner_session,
        workspace_id=fixture.workspace.id,
        closed_period=fixture.period,
        next_period=next_period,
        currency="RUB",
        today=dt.date(2026, 9, 12),
    )
    assert len(created) == 1
    assert created[0].amount_minor == 150_000
    assert created[0].status == "accepted"

    # Повтор задачи не создаёт второй перенос.
    repeated = await propose_rollovers(
        owner_session,
        workspace_id=fixture.workspace.id,
        closed_period=fixture.period,
        next_period=next_period,
        currency="RUB",
        today=dt.date(2026, 9, 12),
    )
    assert repeated == []


async def test_b5_incomplete_period_rollover_is_proposal(owner_session: AsyncSession) -> None:
    """B5: для неполного периода перенос остаётся предложением."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    from fintracker.application.planning.periods import ensure_periods
    from fintracker.application.planning.plan import current_budget_version
    from fintracker.db.models.planning import BudgetLine

    version = await current_budget_version(
        owner_session, workspace_id=fixture.workspace.id, period_id=fixture.period.id
    )
    assert version is not None
    await owner_session.execute(
        BudgetLine.__table__.update()
        .where(
            BudgetLine.workspace_id == fixture.workspace.id,
            BudgetLine.budget_version_id == version.id,
            BudgetLine.category_id == fixture.categories["Продукты"],
        )
        .values(limit_minor=400_000, rollover_mode="positive_only")
    )
    await ensure_periods(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 9, 15)
    )
    next_period = (
        await owner_session.execute(
            select(BudgetPeriod).where(
                BudgetPeriod.workspace_id == fixture.workspace.id,
                BudgetPeriod.start_date == dt.date(2026, 9, 10),
            )
        )
    ).scalar_one()
    created = await propose_rollovers(
        owner_session,
        workspace_id=fixture.workspace.id,
        closed_period=fixture.period,
        next_period=next_period,
        currency="RUB",
        today=dt.date(2026, 9, 12),
    )
    assert created[0].status == "proposed"
    assert created[0].basis_completeness == "incomplete"


def test_b6_fund_contribution_rounding() -> None:
    """B6: до платежа 12 000 выделено 3000, осталось три взноса → 3000."""
    contribution = suggested_contribution(
        target=Money(1_200_000, "RUB"),
        already_allocated=Money(300_000, "RUB"),
        remaining_contributions=3,
    )
    assert contribution.minor == 300_000

    # Округление вверх до минимальной денежной единицы.
    uneven = suggested_contribution(
        target=Money(1_000, "RUB"),
        already_allocated=Money(0, "RUB"),
        remaining_contributions=3,
    )
    assert uneven.minor == 334


async def test_b6_fund_usage_is_not_double_reserved(owner_session: AsyncSession) -> None:
    """B6: оплата из фонда создаёт один расход и использует выделенные средства."""
    fixture = await build_fixture(owner_session)
    goal = await create_goal(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        name="Страховка",
        currency="RUB",
        target=rub(12_000),
        kind="fund",
    )
    await allocate_to_goal(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        goal_id=goal.id,
        amount=rub(3_000),
        reason="Взнос",
    )
    from fintracker.db.models.commitments import CashReservation

    reservation = (
        await owner_session.execute(
            select(CashReservation).where(
                CashReservation.workspace_id == fixture.workspace.id,
                CashReservation.is_active.is_(True),
            )
        )
    ).scalar_one()
    assert reservation.amount_minor == 300_000

    payment = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(12_000), category="Продукты"),
        origin="form",
    )
    await use_goal(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        goal_id=goal.id,
        amount=rub(3_000),
        effect_id=payment.effect_id,
        transaction_id=payment.transaction_id,
        reason="Оплата из фонда",
    )
    await owner_session.refresh(goal)
    assert goal.allocated_minor == 0
    # Резерв не удерживает те же деньги второй раз после использования.
    remaining = (
        await owner_session.execute(
            select(CashReservation).where(
                CashReservation.workspace_id == fixture.workspace.id,
                CashReservation.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    assert remaining is None

    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=DAY,
    )
    assert status.total_fact_minor == 1_200_000, "расход создан один раз"


async def test_b10_negative_net_spending(owner_session: AsyncSession) -> None:
    """B10: возврат за прошлый период даёт отрицательный чистый расход."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    purchase = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(
            fixture, amount=rub(1_200), category="Продукты", occurred=dt.date(2026, 8, 15)
        ),
        origin="form",
    )
    from fintracker.application.planning.periods import ensure_periods

    await ensure_periods(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 9, 15)
    )
    next_period = (
        await owner_session.execute(
            select(BudgetPeriod).where(
                BudgetPeriod.workspace_id == fixture.workspace.id,
                BudgetPeriod.start_date == dt.date(2026, 9, 10),
            )
        )
    ).scalar_one()
    parts = await refundable_parts(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=purchase.transaction_id,
    )
    await post_refund(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        source_transaction_id=purchase.transaction_id,
        parts={parts[0].stable_line_id: rub(1_200)},
        occurred_date=dt.date(2026, 9, 12),
        timezone=TZ,
    )
    # Возврат уменьшает статью текущего периода, прошлый не переписан.
    old_status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=dt.date(2026, 9, 12),
    )
    new_status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=next_period.id,
        currency="RUB",
        today=dt.date(2026, 9, 12),
    )
    assert old_status.total_fact_minor == 120_000
    assert new_status.total_fact_minor == -120_000
