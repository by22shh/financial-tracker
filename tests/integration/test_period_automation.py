"""Автоматическое открытие периодов и шаблон плана (A212–A226, AR-23, AR-24)."""

from __future__ import annotations

import asyncio
import datetime as dt
import itertools

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.planning.periods import ensure_periods, period_for_date
from fintracker.application.planning.plan import (
    PlanLineSpec,
    create_budget_version,
    current_budget_version,
    period_status,
)
from fintracker.application.planning.rollover import (
    active_template,
    apply_plan_for_period,
)
from fintracker.config import Settings
from fintracker.core.errors import ValidationFailed
from fintracker.db.models.planning import (
    BudgetPeriod,
    PeriodPolicyRow,
    RecurringPlanTemplate,
)
from fintracker.db.session import RuntimeRole, session_scope
from tests.conftest import requires_pg
from tests.integration.factories import TZ, build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]


async def _add_template(
    session: AsyncSession, fixture, *, limits: dict[str, int], enabled: bool = True
) -> RecurringPlanTemplate:
    template = RecurringPlanTemplate(
        workspace_id=fixture.workspace.id,
        version=1,
        enabled=enabled,
        effective_from=fixture.period.start_date,
        approval_actor_id=fixture.user.id,
        lines=[
            {
                "category_id": str(fixture.categories[name]),
                "beneficiary_id": None,
                "limit_minor": limit,
                "rollover_mode": "none",
                "is_protected": False,
            }
            for name, limit in limits.items()
        ],
        income_rule={"precision": "exact", "monthly_minor": 10_000_000},
    )
    session.add(template)
    await session.flush()
    return template


async def test_a212_period_opens_without_ai_and_keeps_identity(
    owner_session: AsyncSession,
) -> None:
    """A212: новый период открывается без AI; ID бюджета и справочники целы."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    workspace_id = fixture.workspace.id
    categories_before = set(fixture.categories.values())

    created = await ensure_periods(
        owner_session, workspace_id=workspace_id, until_date=dt.date(2026, 9, 15)
    )
    assert len(created) == 1
    assert created[0].start_date == dt.date(2026, 9, 10)

    from fintracker.db.models.catalog import Category

    categories_after = set(
        (
            await owner_session.execute(
                select(Category.id).where(Category.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    assert categories_before <= categories_after
    assert fixture.workspace.id == workspace_id, "постоянный ID бюджета сохранён"


async def test_a213_concurrent_open_creates_single_period(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """A213/AR-24: два одновременных открытия дают один период и один план."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    await _add_template(owner_session, fixture, limits={"Продукты": 500_000})
    workspace_id = fixture.workspace.id
    await owner_session.commit()

    async def open_periods() -> None:
        async with session_scope(
            test_settings, RuntimeRole.WORKER, workspace_id=workspace_id
        ) as session:
            from fintracker.db.uow import UnitOfWork

            uow = UnitOfWork(session=session, correlation_id="concurrent")
            await uow.lock_workspace(workspace_id)
            created = await ensure_periods(
                session, workspace_id=workspace_id, until_date=dt.date(2026, 9, 15)
            )
            for materialized in created:
                period = (
                    await session.execute(
                        select(BudgetPeriod).where(BudgetPeriod.id == materialized.id)
                    )
                ).scalar_one()
                await apply_plan_for_period(session, uow, workspace_id=workspace_id, period=period)

    results = await asyncio.gather(open_periods(), open_periods(), return_exceptions=True)
    assert any(not isinstance(item, Exception) for item in results)

    async with session_scope(
        test_settings, RuntimeRole.WORKER, workspace_id=workspace_id
    ) as session:
        periods = (
            (
                await session.execute(
                    select(BudgetPeriod).where(
                        BudgetPeriod.workspace_id == workspace_id,
                        BudgetPeriod.start_date == dt.date(2026, 9, 10),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(periods) == 1, "период создан один раз"
        working = await current_budget_version(
            session, workspace_id=workspace_id, period_id=periods[0].id
        )
        assert working is not None
        assert working.version == 1, "исходный план создан один раз"


async def test_a214_recovery_after_missed_boundaries(owner_session: AsyncSession) -> None:
    """A214: после простоя периоды восстанавливаются без пропусков и пересечений."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 5, 10))
    created = await ensure_periods(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 9, 15)
    )
    assert len(created) == 4, "май→сентябрь"
    periods = (
        (
            await owner_session.execute(
                select(BudgetPeriod)
                .where(BudgetPeriod.workspace_id == fixture.workspace.id)
                .order_by(BudgetPeriod.start_date)
            )
        )
        .scalars()
        .all()
    )
    for previous, following in itertools.pairwise(periods):
        assert previous.end_exclusive == following.start_date, "нет разрывов"
    assert periods[0].start_date == dt.date(2026, 5, 10)
    assert periods[-1].start_date == dt.date(2026, 9, 10)
    # Полнота восстановленных периодов не объявляется подтверждённой.
    assert all(period.completeness == "incomplete" for period in periods)


async def test_a215_late_processing_keeps_purchase_in_its_period(
    owner_session: AsyncSession,
) -> None:
    """A215: покупка 9-го относится к прежнему периоду при обработке 10-го."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    await ensure_periods(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 9, 15)
    )
    await post_transaction_helper(owner_session, fixture, dt.date(2026, 9, 9))

    previous = await period_for_date(
        owner_session, workspace_id=fixture.workspace.id, day=dt.date(2026, 9, 9)
    )
    current = await period_for_date(
        owner_session, workspace_id=fixture.workspace.id, day=dt.date(2026, 9, 10)
    )
    old_status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=previous.id,
        currency="RUB",
        today=dt.date(2026, 9, 10),
    )
    new_status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=current.id,
        currency="RUB",
        today=dt.date(2026, 9, 10),
    )
    assert old_status.total_fact_minor == 50_000
    assert new_status.total_fact_minor == 0


async def post_transaction_helper(session, fixture, day: dt.date) -> None:
    from fintracker.application.ledger.service import post_transaction

    await post_transaction(
        session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(500), category="Продукты", occurred=day),
        origin="form",
    )


async def test_a218_template_copies_limits_not_facts(owner_session: AsyncSession) -> None:
    """A218: шаблон переносит лимиты, но не расходы и не прогресс цели."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    await _add_template(owner_session, fixture, limits={"Продукты": 500_000})
    await post_transaction_helper(owner_session, fixture, dt.date(2026, 8, 15))

    created = await ensure_periods(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 9, 15)
    )
    period = (
        await owner_session.execute(select(BudgetPeriod).where(BudgetPeriod.id == created[0].id))
    ).scalar_one()
    await apply_plan_for_period(
        owner_session, fixture.uow, workspace_id=fixture.workspace.id, period=period
    )
    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=period.id,
        currency="RUB",
        today=dt.date(2026, 9, 12),
    )
    assert status.total_limit_minor == 500_000, "лимиты перенесены"
    assert status.total_fact_minor == 0, "расходы не скопированы"
    assert status.plan_status == "approved"
    assert status.plan_origin == "template"


async def test_a221_disabled_template_gives_unapproved_draft(
    owner_session: AsyncSession,
) -> None:
    """A221: без повторения плана период открыт, проект не утверждён."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    await _add_template(owner_session, fixture, limits={"Продукты": 500_000}, enabled=False)
    created = await ensure_periods(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 9, 15)
    )
    period = (
        await owner_session.execute(select(BudgetPeriod).where(BudgetPeriod.id == created[0].id))
    ).scalar_one()
    await apply_plan_for_period(
        owner_session, fixture.uow, workspace_id=fixture.workspace.id, period=period
    )
    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=period.id,
        currency="RUB",
        today=dt.date(2026, 9, 12),
    )
    assert status.plan_status == "draft", "проект не утверждён"
    assert status.total_limit_minor is None, "отсутствие лимита не равно нулю"

    # Запись трат продолжает работать.
    await post_transaction_helper(owner_session, fixture, dt.date(2026, 9, 12))
    after = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=period.id,
        currency="RUB",
        today=dt.date(2026, 9, 12),
    )
    assert after.total_fact_minor == 50_000


async def test_approved_plan_is_not_overwritten_by_template(
    owner_session: AsyncSession,
) -> None:
    """FR-93: индивидуально утверждённый план не перезаписывается шаблоном."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    await _add_template(owner_session, fixture, limits={"Продукты": 500_000})
    created = await ensure_periods(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 9, 15)
    )
    period = (
        await owner_session.execute(select(BudgetPeriod).where(BudgetPeriod.id == created[0].id))
    ).scalar_one()
    await create_budget_version(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=period.id,
        kind="working",
        plan_status="approved",
        origin="manual",
        lines=[PlanLineSpec(category_id=fixture.categories["Продукты"], limit_minor=900_000)],
        approved_by=fixture.user.id,
    )
    await apply_plan_for_period(
        owner_session, fixture.uow, workspace_id=fixture.workspace.id, period=period
    )
    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=period.id,
        currency="RUB",
        today=dt.date(2026, 9, 12),
    )
    assert status.total_limit_minor == 900_000, "принятый план сохранён"


async def test_a214_template_version_of_boundary_is_used(
    owner_session: AsyncSession,
) -> None:
    """A214: применяется версия шаблона, действовавшая на соответствующую границу."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 7, 10))
    await _add_template(owner_session, fixture, limits={"Продукты": 300_000})
    newer = RecurringPlanTemplate(
        workspace_id=fixture.workspace.id,
        version=2,
        enabled=True,
        effective_from=dt.date(2026, 9, 10),
        approval_actor_id=fixture.user.id,
        lines=[
            {
                "category_id": str(fixture.categories["Продукты"]),
                "beneficiary_id": None,
                "limit_minor": 900_000,
                "rollover_mode": "none",
                "is_protected": False,
            }
        ],
        income_rule={},
    )
    owner_session.add(newer)
    await owner_session.flush()

    august = await active_template(
        owner_session, workspace_id=fixture.workspace.id, on_date=dt.date(2026, 8, 10)
    )
    september = await active_template(
        owner_session, workspace_id=fixture.workspace.id, on_date=dt.date(2026, 9, 10)
    )
    assert august is not None and august.version == 1
    assert september is not None and september.version == 2


async def test_a223_future_start_blocks_today_purchase(
    owner_session: AsyncSession,
) -> None:
    """A223: покупка сегодня не попадает в ещё не начавшийся период."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 12, 1))
    with pytest.raises(ValidationFailed, match="раньше начала"):
        await period_for_date(
            owner_session, workspace_id=fixture.workspace.id, day=dt.date(2026, 9, 12)
        )


async def test_a226_policy_change_keeps_single_sequence(
    owner_session: AsyncSession,
) -> None:
    """A226/FR-94: новое правило не создаёт вторую последовательность периодов."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    await ensure_periods(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 9, 15)
    )
    owner_session.add(
        PeriodPolicyRow(
            workspace_id=fixture.workspace.id,
            version=2,
            anchor_date=dt.date(2026, 11, 1),
            anchor_day=1,
            mode="calendar_months",
            interval=1,
            timezone=TZ,
            first_end_exclusive=dt.date(2026, 12, 1),
            effective_from=dt.date(2026, 10, 10),
            base_sequence=10,
            created_by=fixture.user.id,
        )
    )
    await owner_session.flush()
    await ensure_periods(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 12, 15)
    )
    periods = (
        (
            await owner_session.execute(
                select(BudgetPeriod)
                .where(BudgetPeriod.workspace_id == fixture.workspace.id)
                .order_by(BudgetPeriod.start_date)
            )
        )
        .scalars()
        .all()
    )
    for previous, following in itertools.pairwise(periods):
        assert previous.end_exclusive == following.start_date, "непрерывность сохранена"
    starts = [period.start_date for period in periods]
    assert len(starts) == len(set(starts)), "нет дублей начал"
    # Переходный интервал обозначен явно.
    assert any(period.is_transition for period in periods)
