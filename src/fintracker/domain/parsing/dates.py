"""Разбор выражений даты относительно исходного события (FR-26, A50).

«Вчера» определяется относительно времени исходного сообщения и сохранённого
часового пояса, даже если обработка повторилась на следующий день.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

RELATIVE_WORDS: dict[str, int] = {
    "сегодня": 0,
    "вчера": -1,
    "позавчера": -2,
    "завтра": 1,
    "послезавтра": 2,
}

WEEKDAYS: dict[str, int] = {
    "понедельник": 0,
    "вторник": 1,
    "среда": 2,
    "среду": 2,
    "четверг": 3,
    "пятница": 4,
    "пятницу": 4,
    "суббота": 5,
    "субботу": 5,
    "воскресенье": 6,
}

MONTHS: dict[str, int] = {
    "января": 1,
    "январь": 1,
    "февраля": 2,
    "февраль": 2,
    "марта": 3,
    "март": 3,
    "апреля": 4,
    "апрель": 4,
    "мая": 5,
    "май": 5,
    "июня": 6,
    "июнь": 6,
    "июля": 7,
    "июль": 7,
    "августа": 8,
    "август": 8,
    "сентября": 9,
    "сентябрь": 9,
    "октября": 10,
    "октябрь": 10,
    "ноября": 11,
    "ноябрь": 11,
    "декабря": 12,
    "декабрь": 12,
}

_NUMERIC_DATE = re.compile(r"\b(?P<day>\d{1,2})[./](?P<month>\d{1,2})(?:[./](?P<year>\d{2,4}))?\b")
_MONTH_NAME_DATE = re.compile(
    r"\b(?P<day>\d{1,2})\s+(?P<month>" + "|".join(MONTHS) + r")(?:\s+(?P<year>\d{4}))?\b",
    re.IGNORECASE,
)
_DAYS_AGO = re.compile(r"\b(?P<count>\d{1,2})\s+дн(?:я|ей|ь)\s+назад\b", re.IGNORECASE)
_LAST_WEEKDAY = re.compile(
    r"\b(?:в\s+)?прошл(?:ый|ую|ое)\s+(?P<weekday>" + "|".join(WEEKDAYS) + r")\b", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class ParsedDate:
    value: dt.date
    expression: str
    is_future: bool
    precision: str = "day"


def resolve_date_expression(
    text: str,
    *,
    reference: dt.date,
    ignore_raw: tuple[str, ...] = (),
    prefer_future: bool = False,
) -> ParsedDate | None:
    """Разрешить дату относительно даты исходного события.

    ``ignore_raw`` содержит фрагменты, уже распознанные как суммы: «3.5» в
    «кофе 3.5 USD» является ценой, а не третьим мая (AI-05).
    Для планового платежа ``prefer_future`` выбирает ближайшую дату не раньше
    reference, если год не указан. Явный год и относительные даты не меняются.
    """
    lowered = text.lower()
    skipped = tuple(item.strip().lower() for item in ignore_raw if item.strip())

    for word, offset in RELATIVE_WORDS.items():
        if re.search(rf"(?<![а-яё]){word}(?![а-яё])", lowered):
            value = reference + dt.timedelta(days=offset)
            return ParsedDate(value=value, expression=word, is_future=offset > 0)

    days_ago = _DAYS_AGO.search(lowered)
    if days_ago:
        value = reference - dt.timedelta(days=int(days_ago.group("count")))
        return ParsedDate(value=value, expression=days_ago.group(), is_future=False)

    named = _MONTH_NAME_DATE.search(lowered)
    if named:
        day = int(named.group("day"))
        month = MONTHS[named.group("month").lower()]
        year = int(named.group("year")) if named.group("year") else reference.year
        candidate = (
            _next_date(reference, month, day)
            if prefer_future and named.group("year") is None
            else _safe_date(year, month, day)
        )
        if candidate is None:
            return None
        if (
            not prefer_future
            and named.group("year") is None
            and candidate > reference + dt.timedelta(days=1)
        ):
            # Без года ближайшая прошедшая дата вероятнее будущей.
            candidate = _safe_date(year - 1, month, day) or candidate
        return ParsedDate(
            value=candidate, expression=named.group(), is_future=candidate > reference
        )

    numeric = next(
        (
            match
            for match in _NUMERIC_DATE.finditer(lowered)
            if not any(match.group() in item for item in skipped)
        ),
        None,
    )
    if numeric:
        day = int(numeric.group("day"))
        month = int(numeric.group("month"))
        raw_year = numeric.group("year")
        if raw_year:
            year = int(raw_year)
            if year < 100:
                year += 2000
        else:
            year = reference.year
        candidate = (
            _next_date(reference, month, day)
            if prefer_future and raw_year is None
            else _safe_date(year, month, day)
        )
        if candidate is None:
            return None
        if not prefer_future and raw_year is None and candidate > reference + dt.timedelta(days=1):
            candidate = _safe_date(year - 1, month, day) or candidate
        return ParsedDate(
            value=candidate, expression=numeric.group(), is_future=candidate > reference
        )

    last_weekday = _LAST_WEEKDAY.search(lowered)
    if last_weekday:
        target = WEEKDAYS[last_weekday.group("weekday").lower()]
        delta = (reference.weekday() - target) % 7 or 7
        value = reference - dt.timedelta(days=delta)
        return ParsedDate(value=value, expression=last_weekday.group(), is_future=False)

    return None


def _safe_date(year: int, month: int, day: int) -> dt.date | None:
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _next_date(reference: dt.date, month: int, day: int) -> dt.date | None:
    # Eight years cover the leap-year gap across a non-leap century.
    for year in range(reference.year, min(reference.year + 9, 10000)):
        candidate = _safe_date(year, month, day)
        if candidate is not None and candidate >= reference:
            return candidate
    return None
