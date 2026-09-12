"""Пороговые события и уведомления A56–A60 (FR-52)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.delivery.thresholds import evaluate_thresholds
from fintracker.application.ledger.operations import post_refund, refundable_parts
from fintracker.application.ledger.service import post_transaction
from fintracker.application.planning.plan import PlanLineSpec, create_budget_version
from fintracker.db.models.platform import ThresholdEvent
from tests.conftest import requires_pg
from tests.integration.factories import TZ, build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)


async def _evaluate(fixture, session: AsyncSession):
    return await evaluate_thresholds(
        session,
        fixture.uow,
        workspace=fixture.workspace,
        period_id=fixture.period.id,
        today=DAY,
    )


async def test_a56_jump_from_70_to_105_gives_single_overspend(
    owner_session: AsyncSession,
) -> None:
    """A56: при прыжке 70% → 105% отправляется одно сообщение о превышении."""
    fixture = await build_fixture(owner_session, limits={"Продукты": 1_000_000})
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(7_000), category="Продукты"),
        origin="form",
    )
    assert await _evaluate(fixture, owner_session) == []

    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(3_500), category="Продукты"),
        origin="form",
    )
    outcomes = await _evaluate(fixture, owner_session)
    assert len(outcomes) == 1
    assert outcomes[0].threshold_type == "overspent"
    assert "Перерасход" in outcomes[0].message(fixture.workspace.name)

    events = (
        (
            await owner_session.execute(
                select(ThresholdEvent).where(ThresholdEvent.workspace_id == fixture.workspace.id)
            )
        )
        .scalars()
        .all()
    )
    # Промежуточных 80/90/100 не создаётся.
    assert {row.threshold_type for row in events} == {"overspent"}


async def test_a57_exact_limit_is_exhausted_not_overspent(
    owner_session: AsyncSession,
) -> None:
    """A57: ровно 100% лимита — «Лимит исчерпан», превышение не выдумывается."""
    fixture = await build_fixture(owner_session, limits={"Продукты": 1_000_000})
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(10_000), category="Продукты"),
        origin="form",
    )
    outcomes = await _evaluate(fixture, owner_session)
    assert len(outcomes) == 1
    assert outcomes[0].threshold_type == "exhausted_100"
    message = outcomes[0].message(fixture.workspace.name)
    assert "Лимит исчерпан" in message
    assert "Перерасход" not in message


async def test_a58_repeated_evaluation_creates_no_duplicate(
    owner_session: AsyncSession,
) -> None:
    """A58: повторная обработка того же события не создаёт второе уведомление."""
    fixture = await build_fixture(owner_session, limits={"Продукты": 1_000_000})
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(8_500), category="Продукты"),
        origin="form",
    )
    first = await _evaluate(fixture, owner_session)
    second = await _evaluate(fixture, owner_session)
    assert len(first) == 1
    assert second == []


async def test_a59_refund_then_growth_does_not_repeat_threshold(
    owner_session: AsyncSession,
) -> None:
    """A59: после возврата ниже 80% и повторного роста порог не повторяется."""
    fixture = await build_fixture(owner_session, limits={"Продукты": 1_000_000})
    purchase = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(8_500), category="Продукты"),
        origin="form",
    )
    assert len(await _evaluate(fixture, owner_session)) == 1

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
        parts={parts[0].stable_line_id: rub(3_000)},
        occurred_date=DAY,
        timezone=TZ,
    )
    assert await _evaluate(fixture, owner_session) == []

    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(3_000), category="Продукты"),
        origin="form",
    )
    # Повторное достижение того же порога не даёт второго предупреждения.
    assert await _evaluate(fixture, owner_session) == []


async def test_a60_plan_change_keeps_threshold_history(
    owner_session: AsyncSession,
) -> None:
    """A60: изменение лимита не очищает историю уведомлений."""
    fixture = await build_fixture(owner_session, limits={"Продукты": 1_000_000})
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(8_500), category="Продукты"),
        origin="form",
    )
    assert len(await _evaluate(fixture, owner_session)) == 1

    await create_budget_version(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        kind="working",
        plan_status="approved",
        origin="manual",
        lines=[
            PlanLineSpec(category_id=fixture.categories["Продукты"], limit_minor=2_000_000),
            PlanLineSpec(category_id=fixture.categories["Рестораны"]),
            PlanLineSpec(category_id=fixture.categories["Транспорт"]),
        ],
    )
    assert await _evaluate(fixture, owner_session) == []
    events = (
        (
            await owner_session.execute(
                select(ThresholdEvent).where(ThresholdEvent.workspace_id == fixture.workspace.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1, "история порогов сохранена"


async def test_thresholds_skip_unset_and_zero_limits(owner_session: AsyncSession) -> None:
    """B2: для незаданного и нулевого лимита пороговые проценты не применяются."""
    fixture = await build_fixture(owner_session, limits={"Продукты": 0})
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
    assert await _evaluate(fixture, owner_session) == []
