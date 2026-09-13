"""Планирование: доход, шаблон, перенос и права (A67, A145–A147, A216, A219, A220, A222, A225)."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.planning.plan import (
    PlanLineSpec,
    change_line_limit,
    create_budget_version,
    current_budget_version,
    line_key,
    period_status,
)
from fintracker.core.context import Role
from fintracker.core.errors import PermissionDenied
from fintracker.db.models.planning import (
    BudgetLine,
    BudgetPeriod,
    IncomePlan,
    IncomeSource,
    RecurringPlanTemplate,
)
from tests.conftest import requires_pg
from tests.integration.factories import build_fixture

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)


async def test_a67_plan_is_anchored_to_period_start(owner_session: AsyncSession) -> None:
    """A67: план привязан к границе периода 10-го, а не к первому числу."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 9, 10))
    period = (
        (
            await owner_session.execute(
                select(BudgetPeriod).where(BudgetPeriod.workspace_id == fixture.workspace.id)
            )
        )
        .scalars()
        .first()
    )
    assert period is not None
    assert period.start_date == dt.date(2026, 9, 10)
    assert period.end_exclusive == dt.date(2026, 10, 10)

    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=period.id,
        currency="RUB",
        today=DAY,
    )
    assert status.start_date == dt.date(2026, 9, 10)
    assert status.end_inclusive == dt.date(2026, 10, 9)


async def test_a145_income_estimate_keeps_its_precision(owner_session: AsyncSession) -> None:
    """A145: тип оценки дохода сохраняется и не превращается в точную сумму."""
    fixture = await build_fixture(owner_session)
    plan = IncomePlan(
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        precision="range",
        basis="period_total",
        min_minor=8_000_000,
        expected_minor=9_000_000,
        max_minor=10_000_000,
        period_amount_minor=8_000_000,
        created_by=fixture.user.id,
    )
    owner_session.add(plan)
    await owner_session.flush()

    stored = (
        await owner_session.execute(
            select(IncomePlan).where(IncomePlan.workspace_id == fixture.workspace.id)
        )
    ).scalar_one()
    assert stored.precision == "range"
    # Консервативная основа финансирования — нижняя граница, не ожидание.
    assert stored.period_amount_minor == stored.min_minor


async def test_a146_detailed_sources_do_not_double_income(owner_session: AsyncSession) -> None:
    """A146: детализация на источники не удваивает месячный доход."""
    fixture = await build_fixture(owner_session)
    plan = IncomePlan(
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        precision="exact",
        basis="period_total",
        period_amount_minor=10_000_000,
        detailed_by_sources=True,
        created_by=fixture.user.id,
    )
    owner_session.add(plan)
    await owner_session.flush()
    owner_session.add_all(
        [
            IncomeSource(
                workspace_id=fixture.workspace.id,
                income_plan_id=plan.id,
                name="Аванс",
                amount_minor=6_000_000,
                expected_date=dt.date(2026, 9, 15),
            ),
            IncomeSource(
                workspace_id=fixture.workspace.id,
                income_plan_id=plan.id,
                name="Зарплата",
                amount_minor=4_000_000,
                expected_date=dt.date(2026, 9, 30),
            ),
        ]
    )
    await owner_session.flush()

    sources = (
        (
            await owner_session.execute(
                select(IncomeSource).where(IncomeSource.income_plan_id == plan.id)
            )
        )
        .scalars()
        .all()
    )
    assert sum(item.amount_minor for item in sources) == plan.period_amount_minor
    assert len({item.expected_date for item in sources}) == 2


async def test_a147_limit_is_not_a_purchase(owner_session: AsyncSession) -> None:
    """A147: лимит и будущая аренда не создают фактического расхода."""
    fixture = await build_fixture(owner_session, limits={"Продукты": 2_000_000})
    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=DAY,
    )
    assert status.total_limit_minor == 2_000_000
    assert status.total_fact_minor == 0, "лимит не является покупкой"


async def test_a216_income_belongs_to_its_period(owner_session: AsyncSession) -> None:
    """A216: ожидание 60 000 ₽ попадает в этот период, 40 000 ₽ — в следующий."""
    from fintracker.application.commitments.schedules import (
        create_schedule,
        materialize_occurrences,
        upcoming_payments,
    )
    from fintracker.core.money import Money
    from fintracker.domain.schedule import ScheduleKind, ScheduleRule

    fixture = await build_fixture(owner_session, start=dt.date(2026, 9, 10))
    period = (
        (
            await owner_session.execute(
                select(BudgetPeriod).where(BudgetPeriod.workspace_id == fixture.workspace.id)
            )
        )
        .scalars()
        .first()
    )
    assert period is not None

    for day, amount in ((10, 6_000_000), (25, 4_000_000)):
        await create_schedule(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            name=f"Доход {day}",
            direction="income",
            rule=ScheduleRule(
                kind=ScheduleKind.MONTHLY,
                anchor_date=dt.date(2026, 9, day),
                interval=1,
                day_of_month=day,
            ),
            currency="RUB",
            expected=Money(amount, "RUB"),
        )
    await materialize_occurrences(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 10, 31)
    )

    short_period_end = dt.date(2026, 9, 24)
    in_period = await upcoming_payments(
        owner_session,
        workspace_id=fixture.workspace.id,
        today=dt.date(2026, 9, 10),
        horizon_days=(short_period_end - dt.date(2026, 9, 10)).days,
        currency="RUB",
        direction="income",
    )
    amounts = {item.remaining_minor for item in in_period}
    assert 6_000_000 in amounts
    assert 4_000_000 not in amounts, "доход 25-го относится к следующему интервалу"


async def test_a219_period_change_does_not_touch_template(owner_session: AsyncSession) -> None:
    """CMD-18, A219: правка лимита в периоде не меняет шаблон; правка шаблона — меняет."""
    fixture = await build_fixture(owner_session, limits={"Продукты": 1_500_000})
    template = RecurringPlanTemplate(
        workspace_id=fixture.workspace.id,
        version=1,
        enabled=True,
        effective_from=dt.date(2026, 9, 1),
        approval_actor_id=fixture.user.id,
        lines=[
            {
                "category_id": str(fixture.categories["Продукты"]),
                "beneficiary_id": None,
                "limit_minor": 1_500_000,
                "rollover_mode": "none",
            }
        ],
    )
    owner_session.add(template)
    await owner_session.flush()

    version = await current_budget_version(
        owner_session, workspace_id=fixture.workspace.id, period_id=fixture.period.id
    )
    assert version is not None
    line = (
        await owner_session.execute(
            select(BudgetLine).where(
                BudgetLine.workspace_id == fixture.workspace.id,
                BudgetLine.budget_version_id == version.id,
                BudgetLine.category_id == fixture.categories["Продукты"],
            )
        )
    ).scalar_one()

    await change_line_limit(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        period_id=fixture.period.id,
        stable_line_id=line.stable_line_id,
        new_limit_minor=1_700_000,
        expected_version=version.version,
        scope="period",
    )
    templates = (
        (
            await owner_session.execute(
                select(RecurringPlanTemplate).where(
                    RecurringPlanTemplate.workspace_id == fixture.workspace.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(templates) == 1, "правка периода не создаёт версию шаблона"
    assert templates[0].lines[0]["limit_minor"] == 1_500_000

    current = await current_budget_version(
        owner_session, workspace_id=fixture.workspace.id, period_id=fixture.period.id
    )
    assert current is not None
    await change_line_limit(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        period_id=fixture.period.id,
        stable_line_id=line.stable_line_id,
        new_limit_minor=1_800_000,
        expected_version=current.version,
        scope="template",
    )
    templates = (
        (
            await owner_session.execute(
                select(RecurringPlanTemplate)
                .where(RecurringPlanTemplate.workspace_id == fixture.workspace.id)
                .order_by(RecurringPlanTemplate.version)
            )
        )
        .scalars()
        .all()
    )
    assert len(templates) == 2, "правка шаблона создаёт новую версию"
    assert templates[1].lines[0]["limit_minor"] == 1_800_000
    assert templates[1].approval_actor_id == fixture.user.id


async def test_a225_member_cannot_change_template(owner_session: AsyncSession) -> None:
    """A225: обычный участник не меняет финансовый шаблон и календарь."""
    fixture = await build_fixture(owner_session, limits={"Продукты": 1_000_000})
    version = await current_budget_version(
        owner_session, workspace_id=fixture.workspace.id, period_id=fixture.period.id
    )
    assert version is not None
    line = (
        (
            await owner_session.execute(
                select(BudgetLine).where(
                    BudgetLine.workspace_id == fixture.workspace.id,
                    BudgetLine.budget_version_id == version.id,
                )
            )
        )
        .scalars()
        .first()
    )
    assert line is not None

    member_actor = replace(fixture.actor, role=Role.MEMBER)
    with pytest.raises(PermissionDenied):
        await change_line_limit(
            owner_session,
            fixture.uow,
            actor=member_actor,
            period_id=fixture.period.id,
            stable_line_id=line.stable_line_id,
            new_limit_minor=1_200_000,
            expected_version=version.version,
            scope="template",
        )


async def test_a220_rollover_does_not_change_template_limit(owner_session: AsyncSession) -> None:
    """A220: перенос 500 ₽ даёт эффективный лимит 15 500 ₽, шаблон остаётся 15 000 ₽."""
    from fintracker.db.models.planning import Rollover

    fixture = await build_fixture(owner_session, limits={"Продукты": 1_500_000})
    version = await current_budget_version(
        owner_session, workspace_id=fixture.workspace.id, period_id=fixture.period.id
    )
    assert version is not None
    line = (
        await owner_session.execute(
            select(BudgetLine).where(
                BudgetLine.workspace_id == fixture.workspace.id,
                BudgetLine.budget_version_id == version.id,
                BudgetLine.category_id == fixture.categories["Продукты"],
            )
        )
    ).scalar_one()

    owner_session.add(
        Rollover(
            workspace_id=fixture.workspace.id,
            stable_line_id=line.stable_line_id,
            source_period_id=fixture.period.id,
            destination_period_id=fixture.period.id,
            amount_minor=50_000,
            mode="positive_only",
            status="accepted",
            basis_completeness="confirmed_complete",
            accepted_by=fixture.user.id,
        )
    )
    await owner_session.flush()

    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=DAY,
    )
    key = line_key(fixture.categories["Продукты"], None)
    row = next(
        item for item in status.lines if line_key(item.category_id, item.beneficiary_id) == key
    )
    assert row.assigned_limit_minor == 1_500_000
    assert row.rollover_minor == 50_000
    assert row.effective_limit_minor == 1_550_000


async def test_a222_deficit_marks_plan_for_review(owner_session: AsyncSession) -> None:
    """A222: при обнаруженном дефиците план требует проверки, суммы не меняются AI."""
    fixture = await build_fixture(owner_session)
    spec = [
        PlanLineSpec(category_id=fixture.categories["Продукты"], limit_minor=9_000_000),
    ]
    version = await create_budget_version(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        kind="working",
        plan_status="needs_review",
        origin="template",
        lines=spec,
        reason="Обнаружен дефицит при повторении плана",
    )
    assert version.plan_status == "needs_review"

    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=DAY,
    )
    assert status.plan_status == "needs_review"
    assert status.total_limit_minor == 9_000_000, "суммы не изменены автоматически"
    assert uuid.UUID(str(version.id))
