"""Распознавание формы короткого ввода до разбора траты.

Здесь собраны проверки, которые решают, куда направить сообщение: код
приглашения, приветствие, одиночная сумма или дата, новая трата посреди
незавершённого действия. Проверки детерминированы и не зависят от AI.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from fintracker.core.ids import INVITE_ALPHABET, INVITE_LENGTH, normalize_invite_code
from fintracker.domain.parsing.amounts import ParsedAmount, detect_currency, parse_amounts

# Код приглашения: 12 символов, по желанию группами по четыре через пробел
# или дефис. Хотя бы одна латинская буква отличает код от длинного числа.
_INVITE_SHAPE = re.compile(r"^[A-Za-z0-9]{4}[\s\-]?[A-Za-z0-9]{4}[\s\-]?[A-Za-z0-9]{4}$")

_GREETINGS = re.compile(
    r"^\s*(привет\w*|здравствуй\w*|добр(ое|ый|ого)\s+\w+|хай|hi|hello|hey|"
    r"спасибо\w*|благодарю|ок|окей|ok|меню|начать|старт|помощь|help)\s*[!.)]*\s*$",
    re.IGNORECASE,
)

# Одна сумма без других слов: «500», «1 200,50», «300 ₽», «250 руб».
_BARE_AMOUNT = re.compile(
    r"^\s*\d[\d\s]*(?:[.,]\d{1,2})?\s*(?:₽|р\.?|руб\.?|рублей|рубля|рубль|[$€₸₺₼֏₾£]|"
    r"[A-Za-z]{3})?\s*$",
    re.IGNORECASE,
)

# Дата без суммы: «25.09», «25.09.2026».
_BARE_DATE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?\s*$")

_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]{2,}")


def invite_code_in_text(text: str) -> str | None:
    """Нормализованный код приглашения, если сообщение состоит только из него."""
    stripped = text.strip()
    if not _INVITE_SHAPE.match(stripped) or not re.search(r"[A-Za-z]", stripped):
        return None
    code = normalize_invite_code(stripped)
    if len(code) != INVITE_LENGTH or any(ch not in INVITE_ALPHABET for ch in code):
        return None
    return code


def is_greeting(text: str) -> bool:
    return bool(_GREETINGS.match(text))


def single_amount(text: str) -> ParsedAmount | None:
    """Ровно одна сумма без описания покупки, иначе None."""
    if not _BARE_AMOUNT.match(text):
        return None
    amounts = parse_amounts(text)
    if len(amounts) != 1:
        return None
    amount = amounts[0]
    if amount.value <= Decimal(0) or amount.is_ambiguous:
        return None
    suffix = re.sub(r"[\d\s.,]", "", text)
    if (
        suffix
        and detect_currency(suffix) is None
        and suffix.lower().strip(".")
        not in {
            "р",
            "руб",
            "рублей",
            "рубля",
            "рубль",
        }
    ):
        return None
    return amount


def bare_date(text: str) -> dt.date | None:
    """Сообщение из одной календарной даты «ДД.ММ» без суммы.

    «12.50» — цена (месяца 50 нет), а «25.09» — дата.
    """
    match = _BARE_DATE.match(text)
    if match is None:
        return None
    day, month = int(match.group(1)), int(match.group(2))
    year_raw = match.group(3)
    year = 2000 + int(year_raw) if year_raw and len(year_raw) == 2 else int(year_raw or 2000)
    if len(match.group(2)) != 2:
        return None
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def looks_like_new_entry(text: str) -> bool:
    """Похоже на самостоятельную запись траты: слово и сумма вместе."""
    from fintracker.domain.parsing.intent import Intent, classify_intent

    if not _WORD.search(text) or not re.search(r"\d", text):
        return False
    return classify_intent(text).intent is Intent.RECORD_TRANSACTION


__all__ = [
    "bare_date",
    "invite_code_in_text",
    "is_greeting",
    "looks_like_new_entry",
    "single_amount",
]
