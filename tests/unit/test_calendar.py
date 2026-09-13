"""Проверки календаря периодов (FR-90–FR-94, A201–A212, AR-23)."""

from __future__ import annotations

import datetime as dt

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fintracker.core.calendar import (
    CalendarError,
    DateRange,
    PeriodPolicy,
    RepeatMode,
    add_calendar_months,
    infer_policy_options,
    start_of_local_day,
    transition_period,
)

TZ = "Asia/Novosibirsk"


def _inclusive(policy: PeriodPolicy, index: int) -> tuple[dt.date, dt.date]:
    period = policy.period(index)
    return period.start, period.end_inclusive


def test_a201_first_period_and_preview() -> None:
    """A201: выбранные даты 10 сентября — 9 октября и календарный месяц."""
    policy = PeriodPolicy.monthly(dt.date(2026, 9, 10), TZ)
    assert policy.matches_first_end(dt.date(2026, 10, 9))
    assert policy.period(0).days == 30
    assert len(policy.preview(4)) == 4


def test_a202_monthly_cycle_10_to_9() -> None:
    """A202: месяцы не заменяются 30 днями."""
    policy = PeriodPolicy.monthly(dt.date(2026, 9, 10), TZ)
    assert _inclusive(policy, 1) == (dt.date(2026, 10, 10), dt.date(2026, 11, 9))
    assert _inclusive(policy, 2) == (dt.date(2026, 11, 10), dt.date(2026, 12, 9))
    assert _inclusive(policy, 3) == (dt.date(2026, 12, 10), dt.date(2027, 1, 9))


def test_a203_fixed_fourteen_days() -> None:
    """A203: каждый период содержит ровно 14 дней."""
    policy = PeriodPolicy.every_days(dt.date(2026, 9, 10), TZ, 14)
    assert _inclusive(policy, 0) == (dt.date(2026, 9, 10), dt.date(2026, 9, 23))
    assert _inclusive(policy, 1) == (dt.date(2026, 9, 24), dt.date(2026, 10, 7))
    assert _inclusive(policy, 2) == (dt.date(2026, 10, 8), dt.date(2026, 10, 21))
    assert all(policy.period(i).days == 14 for i in range(6))


def test_a204_month_versus_thirty_days_diverge() -> None:
    """A204: обе первые границы 10 октября, затем месячная 10 ноября, фикс. 9 ноября."""
    monthly = PeriodPolicy.monthly(dt.date(2026, 9, 10), TZ)
    fixed = PeriodPolicy.every_days(dt.date(2026, 9, 10), TZ, 30)
    assert monthly.boundary(1) == fixed.boundary(1) == dt.date(2026, 10, 10)
    assert monthly.boundary(2) == dt.date(2026, 11, 10)
    assert fixed.boundary(2) == dt.date(2026, 11, 9)


def test_a205_options_offered_without_silent_correction() -> None:
    """A205: даты 10–20 сентября дают явные варианты, конец не подменяется молча."""
    options = infer_policy_options(dt.date(2026, 9, 10), dt.date(2026, 9, 20), TZ)
    modes = {(option.mode, option.interval) for option in options}
    assert (RepeatMode.FIXED_DAYS, 11) in modes
    assert (RepeatMode.CALENDAR_MONTHS, 1) in modes
    fixed = next(o for o in options if o.mode is RepeatMode.FIXED_DAYS)
    assert fixed.period(0).end_inclusive == dt.date(2026, 9, 20)
    monthly = next(o for o in options if o.mode is RepeatMode.CALENDAR_MONTHS)
    assert monthly.period(0).end_inclusive == dt.date(2026, 10, 9)


def test_a206_invalid_intervals_rejected() -> None:
    """A206: конец раньше начала, нулевой интервал и несуществующая дата."""
    with pytest.raises(CalendarError):
        DateRange.from_inclusive(dt.date(2026, 9, 10), dt.date(2026, 9, 9))
    with pytest.raises(CalendarError):
        PeriodPolicy.every_days(dt.date(2026, 9, 10), TZ, 0)
    with pytest.raises(ValueError):
        dt.date(2026, 2, 30)


def test_a207_single_day_period() -> None:
    """A207: период 10–10 сентября длится один день, следующий начинается 11-го."""
    policy = PeriodPolicy.every_days(dt.date(2026, 9, 10), TZ, 1)
    period = policy.period(0)
    assert period.days == 1
    assert period.start == period.end_inclusive == dt.date(2026, 9, 10)
    assert policy.period(1).start == dt.date(2026, 9, 11)
    # Операция на границе учитывается один раз.
    assert policy.sequence_for_date(dt.date(2026, 9, 10)) == 0
    assert policy.sequence_for_date(dt.date(2026, 9, 11)) == 1


def test_a208_january_31_anchor_keeps_original_day() -> None:
    """A208: границы 31.01, 28.02, 31.03, 30.04 — исходный день не потерян."""
    policy = PeriodPolicy.monthly(dt.date(2027, 1, 31), TZ)
    assert [policy.boundary(i) for i in range(4)] == [
        dt.date(2027, 1, 31),
        dt.date(2027, 2, 28),
        dt.date(2027, 3, 31),
        dt.date(2027, 4, 30),
    ]


def test_a209_leap_year_february_boundary() -> None:
    """A209: в 2028 февральская граница 29 февраля, конец первого — 28 февраля."""
    policy = PeriodPolicy.monthly(dt.date(2028, 1, 31), TZ)
    assert policy.boundary(1) == dt.date(2028, 2, 29)
    assert policy.boundary(2) == dt.date(2028, 3, 31)
    assert policy.period(0).end_inclusive == dt.date(2028, 2, 28)


def test_a210_quarterly_cycle() -> None:
    """A210: каждые три календарных месяца с 10 октября 2026."""
    policy = PeriodPolicy.monthly(dt.date(2026, 10, 10), TZ, 3)
    assert policy.period(0).end_inclusive == dt.date(2027, 1, 9)
    assert _inclusive(policy, 1) == (dt.date(2027, 1, 10), dt.date(2027, 4, 9))


def test_a211_fixed_week_across_dst_shift() -> None:  # AR-23
    """A211: ровно семь локальных календарных дней, не 168 часов."""
    tz = "Europe/Berlin"  # переход на летнее время 29 марта 2026
    policy = PeriodPolicy.every_days(dt.date(2026, 3, 26), tz, 7)
    period = policy.period(0)
    assert period.days == 7
    assert period.start == dt.date(2026, 3, 26)
    assert period.end_inclusive == dt.date(2026, 4, 1)
    # Сравнение в UTC: вычитание двух дат с одинаковым tzinfo идёт по
    # «настенным» часам и скрыло бы перевод стрелок.
    start = start_of_local_day(period.start, tz).astimezone(dt.UTC)
    end = start_of_local_day(period.end_exclusive, tz).astimezone(dt.UTC)
    assert (end - start).total_seconds() == 7 * 24 * 3600 - 3600


def test_a48_a49_period_boundary_at_local_midnight() -> None:
    """A48/A49, AR-23: граница периода по локальной полуночи 10-го числа."""
    policy = PeriodPolicy.monthly(dt.date(2026, 8, 10), TZ)
    assert policy.sequence_for_date(dt.date(2026, 9, 9)) == 0
    assert policy.sequence_for_date(dt.date(2026, 9, 10)) == 1
    assert policy.period(0).contains(dt.date(2026, 9, 9))
    assert not policy.period(0).contains(dt.date(2026, 9, 10))


def test_a224_transition_period_on_calendar_change() -> None:
    """A224: после 10 сентября — 9 октября переход на 1-е даёт 10–31 октября."""
    current = DateRange.from_inclusive(dt.date(2026, 9, 10), dt.date(2026, 10, 9))
    transition = transition_period(current, dt.date(2026, 11, 1))
    assert transition is not None
    assert transition.start == dt.date(2026, 10, 10)
    assert transition.end_inclusive == dt.date(2026, 10, 31)
    new_policy = PeriodPolicy.monthly(dt.date(2026, 11, 1), TZ)
    assert _inclusive(new_policy, 0) == (dt.date(2026, 11, 1), dt.date(2026, 11, 30))


def test_transition_absent_when_boundary_matches() -> None:
    current = DateRange.from_inclusive(dt.date(2026, 9, 10), dt.date(2026, 10, 9))
    assert transition_period(current, dt.date(2026, 10, 10)) is None


def test_date_before_anchor_has_no_sequence() -> None:
    """FR-90: покупка раньше начала не попадает в ещё не начавшийся период."""
    policy = PeriodPolicy.monthly(dt.date(2026, 10, 1), TZ)
    assert policy.sequence_for_date(dt.date(2026, 9, 30)) is None


def test_a52_month_clamp_does_not_drift() -> None:
    """A52, AR-23: день 31 в феврале сокращается, а в марте снова 31-е.

    Управляемое время без дрейфа: високосность и короткие месяцы не сдвигают
    якорь навсегда.
    """
    assert add_calendar_months(dt.date(2027, 1, 31), 1) == dt.date(2027, 2, 28)
    assert add_calendar_months(dt.date(2027, 1, 31), 2) == dt.date(2027, 3, 31)
    # Сокращение не становится постоянным сдвигом на 28-е.
    assert add_calendar_months(dt.date(2027, 1, 31), 3) == dt.date(2027, 4, 30)
    assert add_calendar_months(dt.date(2027, 1, 31), 4) == dt.date(2027, 5, 31)

    policy = PeriodPolicy.monthly(dt.date(2027, 1, 31), TZ)
    boundaries = [policy.period(index).start for index in range(5)]
    assert boundaries == [
        dt.date(2027, 1, 31),
        dt.date(2027, 2, 28),
        dt.date(2027, 3, 31),
        dt.date(2027, 4, 30),
        dt.date(2027, 5, 31),
    ]
    # Ни один день не пропущен и не повторён на стыке коротких месяцев.
    for index in range(4):
        assert policy.period(index).end_exclusive == boundaries[index + 1]


def test_a51_timezone_change_keeps_confirmed_date() -> None:
    """A51: смена пояса бюджета не сдвигает уже подтверждённую локальную дату."""
    recorded = dt.date(2026, 9, 12)
    novosibirsk = PeriodPolicy.monthly(dt.date(2026, 9, 10), TZ)
    moscow = PeriodPolicy.monthly(dt.date(2026, 9, 10), "Europe/Moscow")
    # Дата операции хранится как локальная дата события и не пересчитывается.
    assert novosibirsk.sequence_for_date(recorded) == moscow.sequence_for_date(recorded)
    assert novosibirsk.period(0).start == moscow.period(0).start


@given(
    anchor=st.dates(min_value=dt.date(2020, 1, 1), max_value=dt.date(2035, 12, 31)),
    interval=st.integers(min_value=1, max_value=6),
    mode=st.sampled_from(list(RepeatMode)),
    steps=st.integers(min_value=1, max_value=40),
)
@settings(max_examples=250, deadline=None)
def test_sequence_has_no_gaps_or_overlaps(
    anchor: dt.date, interval: int, mode: RepeatMode, steps: int
) -> None:
    """QA-01: нет дрейфа, пересечений и пропусков в последовательности."""
    policy = PeriodPolicy(anchor, mode, interval, TZ)
    previous = policy.period(0)
    assert previous.start == anchor
    for index in range(1, steps):
        current = policy.period(index)
        assert current.start == previous.end_exclusive, "между периодами не должно быть разрыва"
        assert not current.overlaps(previous)
        previous = current


@given(
    anchor=st.dates(min_value=dt.date(2024, 1, 1), max_value=dt.date(2030, 12, 31)),
    interval=st.integers(min_value=1, max_value=4),
    mode=st.sampled_from(list(RepeatMode)),
    offset=st.integers(min_value=0, max_value=900),
)
@settings(max_examples=250, deadline=None)
def test_every_date_belongs_to_exactly_one_period(
    anchor: dt.date, interval: int, mode: RepeatMode, offset: int
) -> None:
    """Каждая дата после начала принадлежит ровно одному периоду."""
    policy = PeriodPolicy(anchor, mode, interval, TZ)
    day = anchor + dt.timedelta(days=offset)
    sequence = policy.sequence_for_date(day)
    assert sequence is not None
    assert policy.period(sequence).contains(day)
    if sequence > 0:
        assert not policy.period(sequence - 1).contains(day)
    assert not policy.period(sequence + 1).contains(day)
