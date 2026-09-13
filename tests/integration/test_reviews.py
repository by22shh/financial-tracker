"""Обзоры, ранний риск, итог периода и проект плана (FR-42, FR-55, FR-56, FR-59, FR-61)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.analytics.reports import spending_report
from fintracker.application.analytics.reviews import (
    EARLY_RISK_ABSOLUTE_MINOR,
    RiskLine,
    build_next_period_draft,
    build_period_summary,
    build_weekly_review,
    early_risk_lines,
    explain_changes,
)
from fintracker.application.ledger.service import post_transaction
from fintracker.application.planning.periods import period_for_date
from fintracker.application.planning.plan import LimitState, LineStatus
from fintracker.core.money import Money
from fintracker.db.models.commitments import Goal
from fintracker.db.models.planning import IncomePlan, IncomeSource
from tests.conftest import requires_pg
from tests.integration.factories import Fixture, build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 24)


def _line(
    *,
    limit_minor: int | None,
    fact_minor: int,
    commitments_minor: int = 0,
    is_protected: bool = False,
) -> LineStatus:
    state = LimitState.NOT_SET if limit_minor is None else LimitState.POSITIVE
    if limit_minor == 0:
        state = LimitState.ZERO
    return LineStatus(
        stable_line_id=uuid.uuid4(),
        category_id=uuid.uuid4(),
        category_name="Продукты",
        beneficiary_id=None,
        beneficiary_name=None,
        assigned_limit_minor=limit_minor,
        rollover_minor=0,
        effective_limit_minor=limit_minor,
        fact_minor=fact_minor,
        remaining_minor=None if limit_minor is None else limit_minor - fact_minor,
        commitments_minor=commitments_minor,
        available_minor=None,
        overdue_commitments_minor=0,
        limit_state=state,
        usage_percent=None,
        is_protected=is_protected,
        currency="RUB",
    )


def test_early_risk_needs_enough_observed_days() -> None:
    """FORM-03, FR-41, FR-42: прогноз не строится, пока данных недостаточно."""
    line = _line(limit_minor=1_000_000, fact_minor=900_000)
    assert early_risk_lines(lines=(line,), observed_days=3, remaining_days=27) == []
    assert early_risk_lines(lines=(line,), observed_days=10, remaining_days=0) == []


def test_early_risk_triggers_above_both_thresholds() -> None:
    """FORM-11, FR-42: срабатывание при превышении абсолютного и относительного порогов."""
    # Темп 30 000 ₽ за 10 дней → прогноз 90 000 ₽ при лимите 50 000 ₽.
    line = _line(limit_minor=5_000_000, fact_minor=3_000_000)
    risky = early_risk_lines(lines=(line,), observed_days=10, remaining_days=20)
    assert len(risky) == 1
    item = risky[0]
    assert item.forecast_minor == 9_000_000
    assert item.excess_minor == 4_000_000
    assert "прогноз" in item.describe("RUB")
    assert Money(item.line.fact_minor, "RUB").format() != item.describe("RUB")

    # Небольшое отклонение ниже стартового порога не поднимает тревогу.
    small = _line(limit_minor=5_000_000, fact_minor=1_700_000)
    assert early_risk_lines(lines=(small,), observed_days=10, remaining_days=20) == []


def test_early_risk_skips_unset_zero_and_protected_limits() -> None:
    """FR-42: для незаданного и нулевого лимита действует отдельная логика."""
    unset = _line(limit_minor=None, fact_minor=3_000_000)
    zero = _line(limit_minor=0, fact_minor=3_000_000)
    protected = _line(limit_minor=5_000_000, fact_minor=3_000_000, is_protected=True)
    assert (
        early_risk_lines(lines=(unset, zero, protected), observed_days=10, remaining_days=20) == []
    )


def test_early_risk_threshold_is_configurable() -> None:
    """FR-42: порог настраивается, стартовое значение — max(500 ₽, 10% лимита)."""
    assert EARLY_RISK_ABSOLUTE_MINOR == 50_000
    line = _line(limit_minor=5_000_000, fact_minor=1_700_000)
    risky = early_risk_lines(
        lines=(line,),
        observed_days=10,
        remaining_days=20,
        absolute_threshold_minor=10_000,
        relative_threshold=0.01,
    )
    assert len(risky) == 1
    assert isinstance(risky[0], RiskLine)


async def _spend(
    session: AsyncSession,
    fixture: Fixture,
    *,
    category: str,
    amount: int,
    day: dt.date,
) -> None:
    await post_transaction(
        session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(amount), category=category, occurred=day),
        origin="form",
    )


async def test_explain_changes_separates_count_and_average(owner_session: AsyncSession) -> None:
    """FR-59: по отдельным операциям видно количество и среднюю сумму."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 9, 1))
    previous_from = dt.date(2026, 9, 3)
    for offset in range(2):
        await _spend(
            owner_session,
            fixture,
            category="Продукты",
            amount=1_000,
            day=previous_from + dt.timedelta(days=offset),
        )
    current_from = dt.date(2026, 9, 10)
    for offset in range(4):
        await _spend(
            owner_session,
            fixture,
            category="Продукты",
            amount=1_000,
            day=current_from + dt.timedelta(days=offset),
        )

    previous = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=previous_from,
        date_to_exclusive=previous_from + dt.timedelta(days=7),
    )
    current = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=current_from,
        date_to_exclusive=current_from + dt.timedelta(days=7),
    )
    changes = explain_changes(current, previous)
    assert len(changes) == 1
    item = changes[0]
    assert item.delta_minor == rub(2_000).minor
    assert item.count_previous == 2
    assert item.count_current == 4
    assert item.average_current_minor == rub(1_000).minor
    text = item.describe("RUB")
    assert "покупок 2 → 4" in text
    # Причина не утверждается: только вклад по записям.
    assert "стресс" not in text


async def test_explain_changes_without_counts_for_aggregates(owner_session: AsyncSession) -> None:
    """FR-59: по агрегированной истории разрешён анализ только суммы."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 9, 1))
    spec = expense_spec(
        fixture, amount=rub(5_000), category="Продукты", occurred=dt.date(2026, 9, 11)
    )
    from dataclasses import replace

    from fintracker.domain.ledger.model import Granularity

    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=replace(spec, granularity=Granularity.DAILY_AGGREGATE),
        origin="import",
    )
    current = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=dt.date(2026, 9, 10),
        date_to_exclusive=dt.date(2026, 9, 17),
    )
    previous = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=dt.date(2026, 9, 3),
        date_to_exclusive=dt.date(2026, 9, 10),
    )
    changes = explain_changes(current, previous)
    assert len(changes) == 1
    assert changes[0].count_available is False
    assert "агрегирована" in changes[0].describe("RUB")


async def test_weekly_review_has_required_sections(owner_session: AsyncSession) -> None:
    """FR-55: расходы, сравнение, изменения, платежи, цели, полнота и одно действие."""
    fixture = await build_fixture(
        owner_session, start=dt.date(2026, 9, 1), limits={"Продукты": 1_000_000}
    )
    for offset in range(3):
        await _spend(
            owner_session,
            fixture,
            category="Продукты",
            amount=2_000,
            day=dt.date(2026, 9, 18) + dt.timedelta(days=offset),
        )
    await _spend(
        owner_session, fixture, category="Рестораны", amount=1_000, day=dt.date(2026, 9, 12)
    )
    owner_session.add(
        Goal(
            workspace_id=fixture.workspace.id,
            name="Отпуск",
            kind="goal",
            currency="RUB",
            target_minor=10_000_000,
            allocated_minor=2_000_000,
            contribution_minor=500_000,
            created_by=fixture.user.id,
        )
    )
    await owner_session.flush()

    review = await build_weekly_review(owner_session, workspace=fixture.workspace, today=DAY)
    text = review.render()
    assert "Обзор за" in text
    assert "Учтённые расходы" in text
    assert "Изменение" in text
    assert "Главные изменения" in text
    assert "Цели:" in text
    assert "Полнота учёта" in text
    assert text.count("Предлагаемое действие:") == 1
    assert review.date_to_inclusive == DAY
    assert review.date_from == DAY - dt.timedelta(days=6)
    assert review.spent_minor == rub(6_000).minor


async def test_period_summary_does_not_call_leftovers_savings(owner_session: AsyncSession) -> None:
    """FR-56: неиспользованные лимиты не называются экономией без оснований."""
    fixture = await build_fixture(
        owner_session, start=dt.date(2026, 9, 1), limits={"Продукты": 1_000_000}
    )
    await _spend(
        owner_session, fixture, category="Продукты", amount=3_000, day=dt.date(2026, 9, 12)
    )
    period = await period_for_date(
        owner_session, workspace_id=fixture.workspace.id, day=dt.date(2026, 9, 12)
    )
    summary = await build_period_summary(
        owner_session, workspace=fixture.workspace, period_id=period.id, today=DAY
    )
    text = summary.render()
    assert "сэкономил" not in text.lower()
    assert "Неиспользованные лимиты" in text
    assert summary.consumption_minor == rub(3_000).minor
    assert summary.unspent_limits_minor == rub(7_000).minor
    assert "не является доказанной" in text


async def test_period_summary_separates_income_and_other_flows(
    owner_session: AsyncSession,
) -> None:
    """FR-56: доходы, потребление и прочие движения разделены."""
    from fintracker.domain.ledger.model import (
        AllocationRole,
        AllocationSpec,
        CashLegSpec,
        CoverageMode,
        TransactionSpec,
        TransactionType,
    )
    from tests.integration.factories import TZ

    fixture = await build_fixture(owner_session, start=dt.date(2026, 9, 1))
    salary = rub(100_000)
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=TransactionSpec(
            transaction_type=TransactionType.INCOME,
            amount=salary,
            occurred_date=dt.date(2026, 9, 10),
            timezone=TZ,
            allocations=(AllocationSpec(role=AllocationRole.INCOME, amount=salary),),
            cash_legs=(
                CashLegSpec(
                    signed=salary,
                    account_id=fixture.accounts["Карта"],
                    coverage=CoverageMode.TRACKED,
                ),
            ),
        ),
        origin="form",
    )
    await _spend(
        owner_session, fixture, category="Продукты", amount=4_000, day=dt.date(2026, 9, 11)
    )
    period = await period_for_date(
        owner_session, workspace_id=fixture.workspace.id, day=dt.date(2026, 9, 11)
    )
    summary = await build_period_summary(
        owner_session, workspace=fixture.workspace, period_id=period.id, today=DAY
    )
    assert summary.income_minor == salary.minor
    assert summary.consumption_minor == rub(4_000).minor
    text = summary.render()
    assert "Доходы:" in text
    assert "Потребительские расходы:" in text


async def test_next_period_draft_shows_balance_apart_from_income(
    owner_session: AsyncSession,
) -> None:
    """FR-61: остаток и ожидаемый доход показаны раздельно, с основанием плана."""
    fixture = await build_fixture(
        owner_session, start=dt.date(2026, 9, 1), limits={"Продукты": 2_000_000}
    )
    current = await period_for_date(
        owner_session, workspace_id=fixture.workspace.id, day=dt.date(2026, 9, 15)
    )
    following = await period_for_date(
        owner_session, workspace_id=fixture.workspace.id, day=current.end_exclusive
    )
    plan = IncomePlan(
        workspace_id=fixture.workspace.id,
        period_id=following.id,
        precision="estimate",
        basis="period_total",
        period_amount_minor=8_000_000,
        created_by=fixture.user.id,
    )
    owner_session.add(plan)
    await owner_session.flush()
    owner_session.add(
        IncomeSource(
            workspace_id=fixture.workspace.id,
            income_plan_id=plan.id,
            name="Зарплата",
            amount_minor=8_000_000,
            expected_date=following.start_date + dt.timedelta(days=4),
        )
    )
    owner_session.add(
        Goal(
            workspace_id=fixture.workspace.id,
            name="Ремонт",
            kind="fund",
            currency="RUB",
            contribution_minor=300_000,
            contribution_frequency="per_period",
            created_by=fixture.user.id,
        )
    )
    await owner_session.flush()

    draft = await build_next_period_draft(
        owner_session, workspace=fixture.workspace, period_id=following.id, today=DAY
    )
    text = draft.render("RUB")
    assert "Имеющийся остаток на счетах" in text
    assert "Ожидаемый доход" in text
    assert "Основание повторения" in text
    assert draft.fund_contributions_minor == 300_000
    assert draft.expected_income_minor == 8_000_000
    assert draft.income_dates[0][0] == "Зарплата"
    assert draft.flexible_available_minor is not None
    assert draft.deficit_minor == 0


async def test_next_period_draft_reports_deficit(owner_session: AsyncSession) -> None:
    """FR-61: потребность выше ожидаемого дохода показана как дефицит."""
    fixture = await build_fixture(
        owner_session, start=dt.date(2026, 9, 1), limits={"Продукты": 9_000_000}
    )
    current = await period_for_date(
        owner_session, workspace_id=fixture.workspace.id, day=dt.date(2026, 9, 15)
    )
    following = await period_for_date(
        owner_session, workspace_id=fixture.workspace.id, day=current.end_exclusive
    )
    owner_session.add(
        IncomePlan(
            workspace_id=fixture.workspace.id,
            period_id=following.id,
            precision="estimate",
            basis="period_total",
            period_amount_minor=5_000_000,
            created_by=fixture.user.id,
        )
    )
    await owner_session.flush()

    draft = await build_next_period_draft(
        owner_session, workspace=fixture.workspace, period_id=following.id, today=DAY
    )
    assert draft.deficit_minor == 4_000_000
    assert "Дефицит" in draft.render("RUB")


async def test_next_period_draft_without_income_basis(owner_session: AsyncSession) -> None:
    """FR-61: неизвестный доход не заменяется выдуманным числом."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 9, 1))
    current = await period_for_date(
        owner_session, workspace_id=fixture.workspace.id, day=dt.date(2026, 9, 15)
    )
    following = await period_for_date(
        owner_session, workspace_id=fixture.workspace.id, day=current.end_exclusive
    )
    draft = await build_next_period_draft(
        owner_session, workspace=fixture.workspace, period_id=following.id, today=DAY
    )
    assert draft.expected_income_minor is None
    assert draft.deficit_minor is None
    assert draft.flexible_available_minor is None
    assert "основание не задано" in draft.render("RUB")


def test_b8_fixed_expense_forecast_uses_commitments() -> None:
    """B8: прогноз складывает факт, неисполненные обязательства и гибкие траты."""
    from fintracker.application.analytics.reports import build_forecast
    from fintracker.application.planning.plan import PeriodStatus

    line = _line(limit_minor=2_000_000, fact_minor=300_000, commitments_minor=400_000)
    status = PeriodStatus(
        period_id=uuid.uuid4(),
        start_date=dt.date(2026, 9, 10),
        end_inclusive=dt.date(2026, 10, 9),
        currency="RUB",
        plan_status="approved",
        plan_origin="template",
        budget_version_id=uuid.uuid4(),
        lines=(line,),
        total_fact_minor=300_000,
        total_limit_minor=2_000_000,
        overall_limit_minor=None,
        uncategorized_fact_minor=0,
        pending_drafts=0,
        pending_confident_minor=0,
        completeness="confirmed_complete",
    )
    forecast = build_forecast(
        status=status,
        today=dt.date(2026, 9, 24),
        observed_days=15,
        flexible_fact_minor=300_000,
        coverage="confirmed_complete",
    )
    assert forecast.fact_minor == 300_000
    assert forecast.commitments_minor == 400_000
    assert forecast.total_minor is not None
    assert forecast.total_minor >= forecast.fact_minor + forecast.commitments_minor
    assert forecast.limitations, "ограничения расчёта указаны явно"
