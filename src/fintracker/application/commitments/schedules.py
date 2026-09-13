"""Расписания платежей и доходов, экземпляры и сопоставление фактов (FR-45–FR-47).

Команды CMD-20: расписания, экземпляры, связь факта, пропуск и отмена.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.core.context import ActorContext
from fintracker.core.errors import ConflictError, NotFound, ValidationFailed
from fintracker.core.money import Money
from fintracker.db.models.commitments import (
    Occurrence,
    OccurrenceSettlement,
    ScheduledItem,
    ScheduleVersion,
)
from fintracker.db.uow import UnitOfWork
from fintracker.domain.schedule import OccurrenceState, ScheduleKind, ScheduleRule

# Горизонт материализации ожиданий вперёд.
HORIZON_DAYS = 120


@dataclass(frozen=True, slots=True)
class UpcomingPayment:
    occurrence_id: uuid.UUID
    schedule_name: str
    due_date: dt.date
    expected_minor: int | None
    settled_minor: int
    remaining_minor: int
    state: str
    is_overdue: bool
    category_id: uuid.UUID | None
    currency: str


async def create_schedule(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    name: str,
    direction: str,
    rule: ScheduleRule,
    currency: str,
    expected: Money | None = None,
    expected_range: tuple[Money, Money] | None = None,
    category_id: uuid.UUID | None = None,
    beneficiary_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    fund_goal_id: uuid.UUID | None = None,
    reminder_days_before: int = 1,
) -> ScheduledItem:
    """Создать расписание с первой версией правила."""
    workspace_id = actor.require_workspace()
    if direction not in {"payment", "income"}:
        raise ValidationFailed("Направление должно быть payment или income")
    item = ScheduledItem(
        workspace_id=workspace_id,
        name=name.strip()[:120],
        direction=direction,
        current_version=1,
        created_by=actor.user_id,
    )
    session.add(item)
    await session.flush()
    session.add(
        ScheduleVersion(
            workspace_id=workspace_id,
            schedule_id=item.id,
            version=1,
            effective_from=rule.anchor_date,
            rule_kind=rule.kind.value,
            rule_interval=rule.interval,
            anchor_date=rule.anchor_date,
            day_of_month=rule.day_of_month,
            use_last_day=rule.use_last_day,
            ends_on=rule.ends_on,
            expected_minor=expected.minor if expected else None,
            expected_min_minor=expected_range[0].minor if expected_range else None,
            expected_max_minor=expected_range[1].minor if expected_range else None,
            currency=currency,
            category_id=category_id,
            beneficiary_id=beneficiary_id,
            account_id=account_id,
            fund_goal_id=fund_goal_id,
            reminder_days_before=reminder_days_before,
            created_by=actor.user_id,
        )
    )
    await session.flush()
    await uow.bump_revisions(workspace_id, plan=True)
    return item


def rule_from_version(version: ScheduleVersion) -> ScheduleRule:
    return ScheduleRule(
        kind=ScheduleKind(version.rule_kind),
        anchor_date=version.anchor_date,
        interval=version.rule_interval,
        day_of_month=version.day_of_month,
        use_last_day=version.use_last_day,
        ends_on=version.ends_on,
    )


async def materialize_occurrences(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    until_date: dt.date,
) -> list[Occurrence]:
    """Создать недостающие экземпляры до указанной даты.

    Смена бюджетного периода не создаёт ещё один экземпляр: ключ включает
    исходную дату и номер слота (FR-45, A227).
    """
    versions = (
        await session.execute(
            select(ScheduleVersion, ScheduledItem)
            .join(
                ScheduledItem,
                (ScheduledItem.workspace_id == ScheduleVersion.workspace_id)
                & (ScheduledItem.id == ScheduleVersion.schedule_id),
            )
            .where(
                ScheduleVersion.workspace_id == workspace_id,
                ScheduledItem.archived_at.is_(None),
                ScheduleVersion.version == ScheduledItem.current_version,
            )
        )
    ).all()
    created: list[Occurrence] = []
    for version, item in versions:
        rule = rule_from_version(version)
        for index, due in rule.occurrences_between(
            rule.anchor_date, until_date + dt.timedelta(days=1)
        ):
            statement = (
                pg_insert(Occurrence)
                .values(
                    workspace_id=workspace_id,
                    schedule_id=item.id,
                    schedule_version=version.version,
                    occurrence_slot=index,
                    original_due_date=due,
                    due_date=due,
                    expected_minor=version.expected_minor,
                    settled_minor=0,
                    state="planned",
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        Occurrence.workspace_id,
                        Occurrence.schedule_id,
                        Occurrence.original_due_date,
                        Occurrence.occurrence_slot,
                    ]
                )
                .returning(Occurrence.id)
            )
            inserted = (await session.execute(statement)).scalar_one_or_none()
            if inserted is not None:
                created.append(
                    (
                        await session.execute(select(Occurrence).where(Occurrence.id == inserted))
                    ).scalar_one()
                )
    return created


async def upcoming_payments(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    today: dt.date,
    horizon_days: int = 30,
    currency: str = "RUB",
    direction: str = "payment",
) -> list[UpcomingPayment]:
    """Ближайшие и просроченные экземпляры расписания (FR-45, FR-47, B1).

    ``direction`` разделяет обязательства и ожидаемые доходы: зарплата имеет
    собственные экземпляры ожиданий и не смешивается с платежами (FR-47).
    """
    rows = (
        await session.execute(
            select(Occurrence, ScheduledItem.name, ScheduleVersion.category_id)
            .join(
                ScheduledItem,
                (ScheduledItem.workspace_id == Occurrence.workspace_id)
                & (ScheduledItem.id == Occurrence.schedule_id),
            )
            .join(
                ScheduleVersion,
                (ScheduleVersion.workspace_id == Occurrence.workspace_id)
                & (ScheduleVersion.schedule_id == Occurrence.schedule_id)
                & (ScheduleVersion.version == Occurrence.schedule_version),
            )
            .where(
                Occurrence.workspace_id == workspace_id,
                Occurrence.state.in_(("planned", "partially_settled")),
                Occurrence.due_date <= today + dt.timedelta(days=horizon_days),
                ScheduledItem.direction == direction,
            )
            .order_by(Occurrence.due_date, Occurrence.id)
        )
    ).all()
    result: list[UpcomingPayment] = []
    for occurrence, name, category_id in rows:
        state = OccurrenceState(
            expected_minor=occurrence.expected_minor,
            settled_minor=occurrence.settled_minor,
            due_date=occurrence.due_date,
        )
        result.append(
            UpcomingPayment(
                occurrence_id=occurrence.id,
                schedule_name=name,
                due_date=occurrence.due_date,
                expected_minor=occurrence.expected_minor,
                settled_minor=occurrence.settled_minor,
                remaining_minor=state.remaining_minor,
                state=occurrence.state,
                is_overdue=state.is_overdue(today),
                category_id=category_id,
                currency=currency,
            )
        )
    return result


async def settle_occurrence(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    occurrence_id: uuid.UUID,
    effect_id: uuid.UUID,
    transaction_id: uuid.UUID,
    amount: Money,
    stable_line_id: uuid.UUID | None = None,
) -> Occurrence:
    """Связать факт с конкретным экземпляром (FR-46, R04).

    Связь не создаёт дополнительной покупки; сумма связей не превышает
    ожидаемую, переплата не переносится молча.
    """
    workspace_id = actor.require_workspace()
    occurrence = (
        await session.execute(
            select(Occurrence)
            .where(Occurrence.workspace_id == workspace_id, Occurrence.id == occurrence_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if occurrence is None:
        raise NotFound("Ожидаемый платёж недоступен")
    if occurrence.state in {"cancelled", "skipped", "superseded"}:
        raise ConflictError("Этот экземпляр платежа уже закрыт")

    # Проверка повтора идёт до проверки суммы: иначе повторная доставка того
    # же события выглядела бы переплатой (TECH-05).
    existing = (
        await session.execute(
            select(OccurrenceSettlement).where(
                OccurrenceSettlement.workspace_id == workspace_id,
                OccurrenceSettlement.occurrence_id == occurrence_id,
                OccurrenceSettlement.effect_id == effect_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return occurrence

    if occurrence.expected_minor is not None:
        remaining = occurrence.expected_minor - occurrence.settled_minor
        if amount.minor > remaining:
            raise ConflictError(
                "Оплата больше ожидаемой суммы: измените ожидание с версией или "
                "оставьте излишек отдельной несвязанной частью факта",
                details={"remaining_minor": remaining},
            )

    session.add(
        OccurrenceSettlement(
            workspace_id=workspace_id,
            occurrence_id=occurrence_id,
            effect_id=effect_id,
            transaction_id=transaction_id,
            stable_line_id=stable_line_id,
            amount_minor=amount.minor,
            status="active",
        )
    )
    occurrence.settled_minor += amount.minor
    occurrence.state = OccurrenceState(
        expected_minor=occurrence.expected_minor,
        settled_minor=occurrence.settled_minor,
        due_date=occurrence.due_date,
    ).next_state()
    occurrence.version += 1
    await session.flush()
    await uow.bump_revisions(workspace_id, plan=True)
    await uow.emit(
        workspace_id=workspace_id,
        event_type="OccurrenceSettled",
        aggregate_type="occurrence",
        aggregate_id=occurrence_id,
        payload={"occurrence_id": str(occurrence_id), "amount_minor": amount.minor},
        actor_user_id=actor.user_id,
    )
    return occurrence


async def change_occurrence(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    occurrence_id: uuid.UUID,
    action: str,
    new_due_date: dt.date | None = None,
    new_expected: Money | None = None,
    reason: str | None = None,
) -> Occurrence:
    """Перенести, пропустить или отменить экземпляр (FR-46, R04).

    Проведённые прошлые экземпляры не переписываются; пропуск не равен
    удалению всего расписания и не создаёт дохода.
    """
    workspace_id = actor.require_workspace()
    occurrence = (
        await session.execute(
            select(Occurrence)
            .where(Occurrence.workspace_id == workspace_id, Occurrence.id == occurrence_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if occurrence is None:
        raise NotFound("Ожидаемый платёж недоступен")
    if occurrence.state == "settled":
        raise ConflictError("Исполненный экземпляр не изменяется этим действием")

    match action:
        case "postpone":
            if new_due_date is None:
                raise ValidationFailed("Для переноса нужна новая дата")
            occurrence.due_date = new_due_date
        case "change_amount":
            if new_expected is None:
                raise ValidationFailed("Для изменения нужна новая сумма")
            if new_expected.minor < occurrence.settled_minor:
                raise ConflictError("Новая сумма меньше уже оплаченной части")
            occurrence.expected_minor = new_expected.minor
        case "skip":
            occurrence.state = "skipped"
        case "cancel":
            occurrence.state = "cancelled"
        case _:
            raise ValidationFailed(f"Неизвестное действие {action}")

    occurrence.change_reason = reason
    occurrence.version += 1
    await session.flush()
    await uow.bump_revisions(workspace_id, plan=True)
    return occurrence
