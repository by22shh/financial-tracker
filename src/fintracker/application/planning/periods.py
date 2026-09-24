"""Материализация периодов и выбор действующей политики (ADR-07, FR-92).

Команды: CMD-16 (чтение периодов с однократной материализацией календаря),
CMD-17 (предпросмотр и принятие правила повторения).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.core.calendar import DateRange, PeriodPolicy, RepeatMode
from fintracker.core.errors import ConflictError, NotFound, ValidationFailed
from fintracker.db.models.planning import BudgetPeriod, PeriodPolicyRow

# Ограничение объёма одного восстановления последовательности (ADR-07).
MAX_PERIODS_PER_RUN = 200


@dataclass(frozen=True, slots=True)
class MaterializedPeriod:
    id: uuid.UUID
    sequence: int
    start_date: dt.date
    end_exclusive: dt.date
    state: str
    is_transition: bool
    policy_id: uuid.UUID
    policy_version: int

    @property
    def range(self) -> DateRange:
        return DateRange(self.start_date, self.end_exclusive)


def policy_from_row(row: PeriodPolicyRow) -> PeriodPolicy:
    return PeriodPolicy(
        anchor_date=row.anchor_date,
        mode=RepeatMode(row.mode),
        interval=row.interval,
        timezone=row.timezone,
    )


async def active_policy_for_date(
    session: AsyncSession, workspace_id: uuid.UUID, day: dt.date
) -> PeriodPolicyRow:
    """Версия правила, действовавшая на указанную дату (FR-93, A214).

    При восстановлении применяется версия соответствующей границы, а не
    последняя версия ко всей истории.
    """
    row = (
        await session.execute(
            select(PeriodPolicyRow)
            .where(
                PeriodPolicyRow.workspace_id == workspace_id,
                PeriodPolicyRow.effective_from <= day,
            )
            .order_by(PeriodPolicyRow.effective_from.desc(), PeriodPolicyRow.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    earliest = (
        await session.execute(
            select(PeriodPolicyRow)
            .where(PeriodPolicyRow.workspace_id == workspace_id)
            .order_by(PeriodPolicyRow.effective_from, PeriodPolicyRow.version)
            .limit(1)
        )
    ).scalar_one_or_none()
    if earliest is None:
        raise NotFound("Для бюджета не задано правило периодов")
    return earliest


async def latest_policy(session: AsyncSession, workspace_id: uuid.UUID) -> PeriodPolicyRow:
    row = (
        await session.execute(
            select(PeriodPolicyRow)
            .where(
                PeriodPolicyRow.workspace_id == workspace_id,
                PeriodPolicyRow.superseded_at.is_(None),
            )
            .order_by(PeriodPolicyRow.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Для бюджета не задано правило периодов")
    return row


async def ensure_periods(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    until_date: dt.date,
) -> list[MaterializedPeriod]:
    """Идемпотентно создать периоды до указанной даты включительно.

    Вызывается фоновым исполнителем и при чтении/записи, чтобы задержка
    scheduler не относила покупку к прежнему периоду (FR-92). Повтор
    безопасен: для одной границы создаётся ровно один период.
    """
    existing_rows = (
        (
            await session.execute(
                select(BudgetPeriod)
                .where(BudgetPeriod.workspace_id == workspace_id)
                .order_by(BudgetPeriod.start_date)
            )
        )
        .scalars()
        .all()
    )
    existing_starts = {row.start_date for row in existing_rows}
    created: list[MaterializedPeriod] = []

    if not existing_rows:
        policy_row = await latest_policy(session, workspace_id)
        cursor_start = policy_row.anchor_date
    else:
        last = existing_rows[-1]
        if last.end_exclusive > until_date:
            return []
        cursor_start = last.end_exclusive

    guard = 0
    while cursor_start <= until_date:
        guard += 1
        if guard > MAX_PERIODS_PER_RUN:
            # Дальние диапазоны восстанавливаются порциями (ADR-07).
            break
        policy_row = await active_policy_for_date(session, workspace_id, cursor_start)
        policy = policy_from_row(policy_row)
        sequence = policy.sequence_for_date(cursor_start)
        if sequence is None:
            sequence = 0
        boundary = policy.boundary(sequence)
        if boundary != cursor_start:
            # Переходный интервал между версиями правила (FR-94).
            next_boundary = policy.boundary(sequence + 1)
            period_range = DateRange(cursor_start, next_boundary)
            is_transition = True
            logical_sequence = policy_row.base_sequence + sequence
        else:
            period_range = policy.period(sequence)
            is_transition = False
            logical_sequence = policy_row.base_sequence + sequence

        if period_range.start in existing_starts:
            cursor_start = period_range.end_exclusive
            continue

        row = BudgetPeriod(
            workspace_id=workspace_id,
            policy_id=policy_row.id,
            policy_version=policy_row.version,
            sequence=logical_sequence,
            start_date=period_range.start,
            end_exclusive=period_range.end_exclusive,
            state="open",
            is_transition=is_transition,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as exc:
            # Конкурентное открытие создало тот же период — это не ошибка.
            await session.rollback()
            raise ConflictError(
                "Период уже открыт параллельным исполнителем, повторите чтение"
            ) from exc
        created.append(
            MaterializedPeriod(
                id=row.id,
                sequence=row.sequence,
                start_date=row.start_date,
                end_exclusive=row.end_exclusive,
                state=row.state,
                is_transition=row.is_transition,
                policy_id=row.policy_id,
                policy_version=row.policy_version,
            )
        )
        existing_starts.add(period_range.start)
        cursor_start = period_range.end_exclusive

    return created


async def period_for_date(
    session: AsyncSession, *, workspace_id: uuid.UUID, day: dt.date
) -> MaterializedPeriod:
    """Период конкретной даты; при необходимости материализует календарь.

    Дата покупки определяет период: сообщение, обработанное 10-го о покупке
    9-го, относится к прежнему периоду (FR-92, A215).
    """
    row = (
        await session.execute(
            select(BudgetPeriod).where(
                BudgetPeriod.workspace_id == workspace_id,
                BudgetPeriod.start_date <= day,
                BudgetPeriod.end_exclusive > day,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        await ensure_periods(session, workspace_id=workspace_id, until_date=day)
        row = (
            await session.execute(
                select(BudgetPeriod).where(
                    BudgetPeriod.workspace_id == workspace_id,
                    BudgetPeriod.start_date <= day,
                    BudgetPeriod.end_exclusive > day,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        first = (
            await session.execute(
                select(BudgetPeriod)
                .where(BudgetPeriod.workspace_id == workspace_id)
                .order_by(BudgetPeriod.start_date)
                .limit(1)
            )
        ).scalar_one_or_none()
        if first is not None and day < first.start_date:
            raise ValidationFailed(
                "Дата раньше начала первого периода бюджета: уточните дату или "
                "измените начальную настройку календаря"
            )
        raise NotFound("Период для этой даты не найден")
    return MaterializedPeriod(
        id=row.id,
        sequence=row.sequence,
        start_date=row.start_date,
        end_exclusive=row.end_exclusive,
        state=row.state,
        is_transition=row.is_transition,
        policy_id=row.policy_id,
        policy_version=row.policy_version,
    )


async def list_periods(
    session: AsyncSession, *, workspace_id: uuid.UUID, limit: int = 24
) -> list[MaterializedPeriod]:
    rows = (
        (
            await session.execute(
                select(BudgetPeriod)
                .where(BudgetPeriod.workspace_id == workspace_id)
                .order_by(BudgetPeriod.start_date.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        MaterializedPeriod(
            id=row.id,
            sequence=row.sequence,
            start_date=row.start_date,
            end_exclusive=row.end_exclusive,
            state=row.state,
            is_transition=row.is_transition,
            policy_id=row.policy_id,
            policy_version=row.policy_version,
        )
        for row in rows
    ]


async def upcoming_periods(
    session: AsyncSession, *, workspace_id: uuid.UUID, from_date: dt.date, count: int
) -> list[DateRange]:
    """Будущие периоды с учётом версий правила и переходного интервала.

    Повторяет логику ``ensure_periods`` без записи: показ совпадает с тем,
    что будет создано при наступлении дат (FR-90, FR-94).
    """
    existing = (
        (
            await session.execute(
                select(BudgetPeriod)
                .where(
                    BudgetPeriod.workspace_id == workspace_id,
                    BudgetPeriod.start_date >= from_date,
                )
                .order_by(BudgetPeriod.start_date)
            )
        )
        .scalars()
        .all()
    )
    result = [DateRange(row.start_date, row.end_exclusive) for row in existing][:count]
    cursor = result[-1].end_exclusive if result else from_date
    while len(result) < count:
        policy = policy_from_row(await active_policy_for_date(session, workspace_id, cursor))
        sequence = policy.sequence_for_date(cursor) or 0
        if policy.boundary(sequence) != cursor:
            period_range = DateRange(cursor, policy.boundary(sequence + 1))
        else:
            period_range = policy.period(sequence)
        result.append(period_range)
        cursor = period_range.end_exclusive
    return result


PERIOD_PRESETS = {
    "month": "календарный месяц, с 1-го числа",
    "10to9": "месяц с 10-го по 9-е число",
    "week": "неделя, с понедельника",
}


def _preset_anchor(preset: str, boundary: dt.date) -> tuple[dt.date, RepeatMode, int]:
    """Последняя граница нового правила не позже переданной даты."""
    if preset == "month":
        return boundary.replace(day=1), RepeatMode.CALENDAR_MONTHS, 1
    if preset == "10to9":
        if boundary.day >= 10:
            return boundary.replace(day=10), RepeatMode.CALENDAR_MONTHS, 1
        previous = boundary.replace(day=1) - dt.timedelta(days=1)
        return previous.replace(day=10), RepeatMode.CALENDAR_MONTHS, 1
    if preset == "week":
        return boundary - dt.timedelta(days=boundary.weekday()), RepeatMode.FIXED_DAYS, 7
    raise ValidationFailed("Неизвестный вариант периода")


async def next_policy_boundary(session: AsyncSession, *, workspace_id: uuid.UUID) -> dt.date:
    """С какой даты можно применить новое правило: после уже открытых периодов."""
    last = (
        await session.execute(
            select(BudgetPeriod)
            .where(BudgetPeriod.workspace_id == workspace_id)
            .order_by(BudgetPeriod.start_date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is None:
        raise NotFound("Периоды бюджета ещё не созданы")
    return last.end_exclusive


async def change_period_policy(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    preset: str,
    created_by: uuid.UUID,
) -> dt.date:
    """Новая версия правила повторения со следующей свободной границы (CMD-17).

    Прошлые и уже открытые периоды не пересчитываются. Если новое правило не
    совпадает с границей, между ними создаётся короткий переходный период.
    """
    boundary = await next_policy_boundary(session, workspace_id=workspace_id)
    current = await latest_policy(session, workspace_id)
    anchor, mode, interval = _preset_anchor(preset, boundary)
    policy = PeriodPolicy(
        anchor_date=anchor, mode=mode, interval=interval, timezone=current.timezone
    )
    last_sequence = (
        await session.execute(
            select(BudgetPeriod.sequence)
            .where(BudgetPeriod.workspace_id == workspace_id)
            .order_by(BudgetPeriod.start_date.desc())
            .limit(1)
        )
    ).scalar_one()
    session.add(
        PeriodPolicyRow(
            workspace_id=workspace_id,
            version=current.version + 1,
            anchor_date=anchor,
            anchor_day=anchor.day,
            mode=mode.value,
            interval=interval,
            timezone=current.timezone,
            first_end_exclusive=policy.period(0).end_exclusive,
            effective_from=boundary,
            base_sequence=last_sequence + 1,
            created_by=created_by,
        )
    )
    await session.flush()
    return boundary
