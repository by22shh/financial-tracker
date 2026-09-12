"""Обязательства: расписания, частичные оплаты, просрочка (FR-45–FR-47, R04, A227)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.commitments.schedules import (
    change_occurrence,
    create_schedule,
    materialize_occurrences,
    settle_occurrence,
    upcoming_payments,
)
from fintracker.application.ledger.service import post_transaction
from fintracker.application.planning.plan import line_key, period_status
from fintracker.core.errors import ConflictError
from fintracker.db.models.commitments import Occurrence
from fintracker.domain.schedule import ScheduleKind, ScheduleRule
from tests.conftest import requires_pg
from tests.integration.factories import build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]


def test_monthly_rule_uses_last_existing_day() -> None:
    """FR-45: для месяца без выбранного числа берётся последний день."""
    rule = ScheduleRule(kind=ScheduleKind.MONTHLY, anchor_date=dt.date(2027, 1, 31))
    assert rule.occurrence_date(0) == dt.date(2027, 1, 31)
    assert rule.occurrence_date(1) == dt.date(2027, 2, 28)
    assert rule.occurrence_date(2) == dt.date(2027, 3, 31), "исходный день не потерян"


def test_weekly_and_yearly_rules() -> None:
    weekly = ScheduleRule(kind=ScheduleKind.WEEKLY, anchor_date=dt.date(2026, 9, 10))
    assert weekly.occurrence_date(2) == dt.date(2026, 9, 24)
    yearly = ScheduleRule(kind=ScheduleKind.YEARLY, anchor_date=dt.date(2024, 2, 29))
    assert yearly.occurrence_date(1) == dt.date(2025, 2, 28)


async def test_a227_monthly_payment_not_duplicated_by_weekly_budget(
    owner_session: AsyncSession,
) -> None:
    """A227: ежемесячная аренда возникает один раз, а не каждую неделю."""
    fixture = await build_fixture(owner_session, categories=("Жильё",))
    await create_schedule(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        name="Аренда",
        direction="payment",
        rule=ScheduleRule(kind=ScheduleKind.MONTHLY, anchor_date=dt.date(2026, 9, 10)),
        currency="RUB",
        expected=rub(40_000),
        category_id=fixture.categories["Жильё"],
    )
    created = await materialize_occurrences(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 10, 20)
    )
    assert len(created) == 2, "сентябрь и октябрь"

    # Повтор материализации не создаёт дублей.
    again = await materialize_occurrences(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 10, 20)
    )
    assert again == []
    total = (
        (
            await owner_session.execute(
                select(Occurrence).where(Occurrence.workspace_id == fixture.workspace.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(total) == 2


async def test_r04_partial_payment_keeps_overdue_remainder_once(
    owner_session: AsyncSession,
) -> None:
    """R04/A227: счёт 1000 на 9-е, оплачено 600 — на 10-е видны 400 просрочки."""
    fixture = await build_fixture(owner_session, categories=("Связь",))
    await create_schedule(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        name="Интернет",
        direction="payment",
        rule=ScheduleRule(
            kind=ScheduleKind.MONTHLY,
            anchor_date=dt.date(2026, 9, 9),
            ends_on=dt.date(2026, 9, 30),
        ),
        currency="RUB",
        expected=rub(1_000),
        category_id=fixture.categories["Связь"],
    )
    created = await materialize_occurrences(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 9, 30)
    )
    occurrence = created[0]

    payment = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(600), category="Связь", occurred=dt.date(2026, 9, 9)),
        origin="form",
    )
    await settle_occurrence(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        occurrence_id=occurrence.id,
        effect_id=payment.effect_id,
        transaction_id=payment.transaction_id,
        amount=rub(600),
    )
    await owner_session.refresh(occurrence)
    assert occurrence.state == "partially_settled"
    assert occurrence.settled_minor == 60_000

    payments = await upcoming_payments(
        owner_session,
        workspace_id=fixture.workspace.id,
        today=dt.date(2026, 9, 10),
        horizon_days=30,
    )
    assert len(payments) == 1
    assert payments[0].remaining_minor == 40_000
    assert payments[0].is_overdue, "остаток остаётся просроченным"

    # Просроченная часть учитывается один раз в расчёте обязательств.
    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=dt.date(2026, 9, 10),
    )
    key = line_key(fixture.categories["Связь"], None)
    line = next(
        item for item in status.lines if line_key(item.category_id, item.beneficiary_id) == key
    )
    assert line.commitments_minor == 40_000
    assert line.overdue_commitments_minor == 40_000

    # Доплата закрывает обязательство один раз.
    rest = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(
            fixture, amount=rub(400), category="Связь", occurred=dt.date(2026, 9, 12)
        ),
        origin="form",
    )
    await settle_occurrence(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        occurrence_id=occurrence.id,
        effect_id=rest.effect_id,
        transaction_id=rest.transaction_id,
        amount=rub(400),
    )
    await owner_session.refresh(occurrence)
    assert occurrence.state == "settled"
    remaining = await upcoming_payments(
        owner_session,
        workspace_id=fixture.workspace.id,
        today=dt.date(2026, 9, 15),
        horizon_days=30,
    )
    assert remaining == []


async def test_overpayment_requires_explicit_decision(owner_session: AsyncSession) -> None:
    """R04: переплата не переносится молча и требует решения."""
    fixture = await build_fixture(owner_session, categories=("Связь",))
    await create_schedule(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        name="Интернет",
        direction="payment",
        rule=ScheduleRule(kind=ScheduleKind.ONCE, anchor_date=dt.date(2026, 9, 15)),
        currency="RUB",
        expected=rub(1_000),
        category_id=fixture.categories["Связь"],
    )
    created = await materialize_occurrences(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 9, 30)
    )
    payment = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1_200), category="Связь"),
        origin="form",
    )
    with pytest.raises(ConflictError, match="больше ожидаемой"):
        await settle_occurrence(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            occurrence_id=created[0].id,
            effect_id=payment.effect_id,
            transaction_id=payment.transaction_id,
            amount=rub(1_200),
        )


async def test_repeated_settlement_event_is_idempotent(owner_session: AsyncSession) -> None:
    """TECH-05: повтор события не создаёт вторую связь исполнения."""
    fixture = await build_fixture(owner_session, categories=("Связь",))
    await create_schedule(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        name="Интернет",
        direction="payment",
        rule=ScheduleRule(kind=ScheduleKind.ONCE, anchor_date=dt.date(2026, 9, 15)),
        currency="RUB",
        expected=rub(1_000),
        category_id=fixture.categories["Связь"],
    )
    created = await materialize_occurrences(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 9, 30)
    )
    payment = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1_000), category="Связь"),
        origin="form",
    )
    for _ in range(3):
        await settle_occurrence(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            occurrence_id=created[0].id,
            effect_id=payment.effect_id,
            transaction_id=payment.transaction_id,
            amount=rub(1_000),
        )
    await owner_session.refresh(created[0])
    assert created[0].settled_minor == 100_000


async def test_skip_does_not_create_income(owner_session: AsyncSession) -> None:
    """R04: пропуск экземпляра не создаёт дохода и не удаляет расписание."""
    fixture = await build_fixture(owner_session, categories=("Связь",))
    await create_schedule(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        name="Интернет",
        direction="payment",
        rule=ScheduleRule(kind=ScheduleKind.MONTHLY, anchor_date=dt.date(2026, 9, 15)),
        currency="RUB",
        expected=rub(1_000),
        category_id=fixture.categories["Связь"],
    )
    created = await materialize_occurrences(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 10, 20)
    )
    await change_occurrence(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        occurrence_id=created[0].id,
        action="skip",
        reason="Оплачено вне бюджета",
    )
    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=dt.date(2026, 9, 20),
    )
    assert status.total_fact_minor == 0, "пропуск не создаёт дохода"
    remaining = (
        (
            await owner_session.execute(
                select(Occurrence).where(
                    Occurrence.workspace_id == fixture.workspace.id,
                    Occurrence.state == "planned",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(remaining) == 1, "следующий экземпляр расписания сохранён"
