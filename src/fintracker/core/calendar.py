"""Календарные правила бюджетных периодов (FR-27, FR-90–FR-94, ADR-07).

Все границы — локальные календарные даты бюджета. Период хранится как
полуинтервал ``[start_date, end_exclusive)``; в интерфейсе обе даты включены.
Календарный месяц не заменяется 30 днями, фиксированный день не равен 24 ч UTC.
"""

from __future__ import annotations

import calendar as _calendar
import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_PREVIEW_PERIODS: Final[int] = 36
# Защита от неограниченного восстановления последовательности (ADR-07).
MAX_SEQUENCE_STEPS: Final[int] = 20_000


class CalendarError(ValueError):
    """Нарушение календарного контракта."""


class RepeatMode(StrEnum):
    """Способ повторения периода (FR-91)."""

    CALENDAR_MONTHS = "calendar_months"
    FIXED_DAYS = "fixed_days"


def validate_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CalendarError(f"Неизвестный часовой пояс: {name!r}") from exc
    return name


def start_of_local_day(day: dt.date, timezone: str) -> dt.datetime:
    """Первый действительный момент локальной даты (ADR-07: DST gap).

    Для несуществующей полуночи берётся первый существующий момент этой даты
    с фиксированным правилом; для неоднозначного времени — первое вхождение.
    """
    tz = ZoneInfo(validate_timezone(timezone))
    naive = dt.datetime.combine(day, dt.time(0, 0))
    candidate = naive.replace(tzinfo=tz, fold=0)
    # Если локальная полночь не существует (DST gap), UTC-обратное
    # преобразование даст другую локальную дату/время.
    if candidate.astimezone(dt.UTC).astimezone(tz).replace(tzinfo=None) != naive:
        for minutes in range(1, 24 * 60):
            probe_naive = naive + dt.timedelta(minutes=minutes)
            probe = probe_naive.replace(tzinfo=tz, fold=0)
            if probe.astimezone(dt.UTC).astimezone(tz).replace(tzinfo=None) == probe_naive:
                return probe
        raise CalendarError(f"Дата {day} не существует в поясе {timezone}")
    return candidate


def add_calendar_months(anchor: dt.date, months: int) -> dt.date:
    """Сдвиг на календарные месяцы с ограничением до последнего дня месяца.

    Ограничение применяется только к результату: следующая граница снова
    вычисляется от исходного anchor, а не от временно сокращённой даты
    (FR-91, A208, A209).
    """
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    last_day = _calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(anchor.day, last_day))


@dataclass(frozen=True, slots=True)
class DateRange:
    """Полуоткрытый интервал локальных дат ``[start, end_exclusive)``."""

    start: dt.date
    end_exclusive: dt.date

    def __post_init__(self) -> None:
        if self.end_exclusive <= self.start:
            raise CalendarError(
                f"Конец интервала {self.end_exclusive} должен быть строго после начала {self.start}"
            )

    @classmethod
    def from_inclusive(cls, start: dt.date, end_inclusive: dt.date) -> Self:
        if end_inclusive < start:
            raise CalendarError("Дата конца раньше даты начала")
        return cls(start, end_inclusive + dt.timedelta(days=1))

    @property
    def end_inclusive(self) -> dt.date:
        return self.end_exclusive - dt.timedelta(days=1)

    @property
    def days(self) -> int:
        """Длительность в календарных днях, включительно по обеим границам."""
        return (self.end_exclusive - self.start).days

    def contains(self, day: dt.date) -> bool:
        return self.start <= day < self.end_exclusive

    def overlaps(self, other: DateRange) -> bool:
        return self.start < other.end_exclusive and other.start < self.end_exclusive

    def __str__(self) -> str:
        return f"[{self.start} .. {self.end_inclusive}]"


@dataclass(frozen=True, slots=True)
class PeriodPolicy:
    """Версионируемое правило повторения периодов (FR-90–FR-91).

    ``anchor_date`` — исходное начало первого периода; все последующие границы
    вычисляются от него, без накопления дрейфа.
    """

    anchor_date: dt.date
    mode: RepeatMode
    interval: int
    timezone: str

    def __post_init__(self) -> None:
        if self.interval < 1:
            raise CalendarError("Интервал повторения должен быть целым числом >= 1")
        validate_timezone(self.timezone)

    @classmethod
    def monthly(cls, anchor: dt.date, timezone: str, months: int = 1) -> Self:
        return cls(anchor, RepeatMode.CALENDAR_MONTHS, months, timezone)

    @classmethod
    def every_days(cls, anchor: dt.date, timezone: str, days: int) -> Self:
        return cls(anchor, RepeatMode.FIXED_DAYS, days, timezone)

    def boundary(self, index: int) -> dt.date:
        """Граница с порядковым номером ``index`` (0 — исходное начало)."""
        if index < 0:
            raise CalendarError("Порядковый номер границы не может быть отрицательным")
        if index > MAX_SEQUENCE_STEPS:
            raise CalendarError(f"Превышен предел последовательности периодов: {index}")
        if self.mode is RepeatMode.CALENDAR_MONTHS:
            return add_calendar_months(self.anchor_date, index * self.interval)
        return self.anchor_date + dt.timedelta(days=index * self.interval)

    def period(self, sequence: int) -> DateRange:
        """Период с порядковым номером ``sequence`` (0 — первый)."""
        return DateRange(self.boundary(sequence), self.boundary(sequence + 1))

    def sequence_for_date(self, day: dt.date) -> int | None:
        """Номер периода, содержащего дату; ``None`` — дата раньше начала.

        Поиск не полагается на арифметику месяцев: используется монотонность
        границ и двоичный поиск, что верно и для коротких месяцев.
        """
        if day < self.anchor_date:
            return None
        if self.mode is RepeatMode.FIXED_DAYS:
            return (day - self.anchor_date).days // self.interval

        # Календарные месяцы: оценка сверху, затем точная корректировка.
        approx_months = (day.year - self.anchor_date.year) * 12 + (
            day.month - self.anchor_date.month
        )
        guess = max(0, approx_months // self.interval)
        while guess > 0 and self.boundary(guess) > day:
            guess -= 1
        while self.boundary(guess + 1) <= day:
            guess += 1
            if guess > MAX_SEQUENCE_STEPS:
                raise CalendarError("Дата вне поддерживаемого диапазона последовательности")
        return guess

    def preview(self, count: int, *, from_sequence: int = 0) -> list[DateRange]:
        if count < 1 or count > MAX_PREVIEW_PERIODS:
            raise CalendarError(
                f"Число периодов предпросмотра должно быть 1..{MAX_PREVIEW_PERIODS}"
            )
        return [self.period(from_sequence + offset) for offset in range(count)]

    def matches_first_end(self, end_inclusive: dt.date) -> bool:
        """Согласована ли выбранная дата конца первого периода с правилом (FR-90)."""
        return self.boundary(1) == end_inclusive + dt.timedelta(days=1)

    def describe(self) -> str:
        if self.mode is RepeatMode.CALENDAR_MONTHS:
            if self.interval == 1:
                return "каждый календарный месяц"
            return f"каждые {self.interval} календарных месяца"
        if self.interval == 7:
            return "каждую неделю"
        if self.interval == 14:
            return "каждые две недели"
        if self.interval == 1:
            return "каждый день"
        return f"каждые {self.interval} дней"


def infer_policy_options(
    start: dt.date, end_inclusive: dt.date, timezone: str
) -> list[PeriodPolicy]:
    """Варианты правила повторения для выбранных дат (FR-90, A205).

    Даты не исправляются молча: возвращаются согласованные варианты, из которых
    пользователь выбирает явно.
    """
    if end_inclusive < start:
        raise CalendarError("Дата конца раньше даты начала")
    span_days = (end_inclusive - start).days + 1
    options: list[PeriodPolicy] = [PeriodPolicy.every_days(start, timezone, span_days)]
    for months in (1, 2, 3, 6, 12):
        candidate = PeriodPolicy.monthly(start, timezone, months)
        if candidate.matches_first_end(end_inclusive):
            options.insert(0, candidate)
            break
    else:
        # Календарный месяц не совпал с выбранным концом — предлагаем его
        # отдельным вариантом с собственной датой конца (без подмены).
        options.append(PeriodPolicy.monthly(start, timezone, 1))
    return options


def transition_period(current: DateRange, new_policy_first_boundary: dt.date) -> DateRange | None:
    """Переходный интервал при смене календаря (FR-94, A224).

    Возвращает ``None``, если новая привязка совпадает с концом текущего периода.
    """
    if new_policy_first_boundary <= current.end_exclusive:
        return None
    return DateRange(current.end_exclusive, new_policy_first_boundary)
