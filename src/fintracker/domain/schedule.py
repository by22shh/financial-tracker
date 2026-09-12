"""Правила повторения платежей и доходов (FR-45, FR-47).

Частота платежа независима от частоты бюджетного периода: при недельном
бюджете ежемесячная аренда возникает один раз по своему расписанию.
"""

from __future__ import annotations

import calendar as _calendar
import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from fintracker.core.errors import ValidationFailed

MAX_OCCURRENCES = 500


class ScheduleKind(StrEnum):
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"


@dataclass(frozen=True, slots=True)
class ScheduleRule:
    kind: ScheduleKind
    anchor_date: dt.date
    interval: int = 1
    day_of_month: int | None = None
    use_last_day: bool = False
    ends_on: dt.date | None = None

    def __post_init__(self) -> None:
        if self.interval < 1:
            raise ValidationFailed("Интервал расписания должен быть не меньше 1")
        if self.day_of_month is not None and not 1 <= self.day_of_month <= 31:
            raise ValidationFailed("День месяца должен быть в диапазоне 1..31")

    def occurrence_date(self, index: int) -> dt.date | None:
        """Дата экземпляра с порядковым номером ``index`` (0 — первый)."""
        if index < 0 or index > MAX_OCCURRENCES:
            raise ValidationFailed("Недопустимый номер экземпляра расписания")
        match self.kind:
            case ScheduleKind.ONCE:
                value = self.anchor_date if index == 0 else None
            case ScheduleKind.DAILY:
                value = self.anchor_date + dt.timedelta(days=index * self.interval)
            case ScheduleKind.WEEKLY:
                value = self.anchor_date + dt.timedelta(weeks=index * self.interval)
            case ScheduleKind.MONTHLY:
                value = self._monthly(index)
            case ScheduleKind.YEARLY:
                value = self._yearly(index)
        if value is None:
            return None
        if self.ends_on is not None and value > self.ends_on:
            return None
        return value

    def _monthly(self, index: int) -> dt.date:
        total = self.anchor_date.month - 1 + index * self.interval
        year = self.anchor_date.year + total // 12
        month = total % 12 + 1
        last_day = _calendar.monthrange(year, month)[1]
        if self.use_last_day:
            return dt.date(year, month, last_day)
        # Для месяца без выбранного числа берётся последний день (FR-45).
        day = self.day_of_month or self.anchor_date.day
        return dt.date(year, month, min(day, last_day))

    def _yearly(self, index: int) -> dt.date:
        year = self.anchor_date.year + index * self.interval
        month = self.anchor_date.month
        last_day = _calendar.monthrange(year, month)[1]
        return dt.date(year, month, min(self.anchor_date.day, last_day))

    def occurrences_between(
        self, start: dt.date, end_exclusive: dt.date
    ) -> list[tuple[int, dt.date]]:
        """Экземпляры, попадающие в интервал; без дрейфа и дублей."""
        results: list[tuple[int, dt.date]] = []
        for index in range(MAX_OCCURRENCES):
            value = self.occurrence_date(index)
            if value is None:
                break
            if value >= end_exclusive:
                break
            if value >= start:
                results.append((index, value))
        return results

    def describe(self) -> str:
        match self.kind:
            case ScheduleKind.ONCE:
                return f"один раз {self.anchor_date.isoformat()}"
            case ScheduleKind.DAILY:
                return "каждый день" if self.interval == 1 else f"каждые {self.interval} дней"
            case ScheduleKind.WEEKLY:
                return "каждую неделю" if self.interval == 1 else f"каждые {self.interval} недели"
            case ScheduleKind.MONTHLY:
                if self.use_last_day:
                    return "в последний день месяца"
                day = self.day_of_month or self.anchor_date.day
                return f"{day} числа каждого месяца"
            case ScheduleKind.YEARLY:
                return f"ежегодно {self.anchor_date.day}.{self.anchor_date.month:02d}"


@dataclass(frozen=True, slots=True)
class OccurrenceState:
    """Состояние ожидания платежа (R04)."""

    expected_minor: int | None
    settled_minor: int
    due_date: dt.date

    @property
    def remaining_minor(self) -> int:
        if self.expected_minor is None:
            return 0
        return max(0, self.expected_minor - self.settled_minor)

    def is_overdue(self, today: dt.date) -> bool:
        """Просрочка вычисляется по due_date и остатку, не уничтожая оплату."""
        return self.remaining_minor > 0 and self.due_date < today

    def next_state(self) -> str:
        if self.expected_minor is None:
            return "planned"
        if self.settled_minor == 0:
            return "planned"
        if self.settled_minor < self.expected_minor:
            return "partially_settled"
        return "settled"
