"""Разбор денежных сумм из свободного текста (FR-10, A07, A08, A14).

Поддерживаются десятичная запятая и точка, пробелы в тысячах, «1,5к»,
«полторы тысячи», «руб», «₽». Неоднозначное «1.500» уточняется.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from fintracker.core.money import CURRENCY_EXPONENTS

# Словесные множители и числительные (русский).
MULTIPLIERS: dict[str, Decimal] = {
    "к": Decimal(1000),
    "k": Decimal(1000),
    "тыс": Decimal(1000),
    "тысяч": Decimal(1000),
    "тысячи": Decimal(1000),
    "тысяча": Decimal(1000),
    "тысячу": Decimal(1000),
    "млн": Decimal(1_000_000),
    "миллион": Decimal(1_000_000),
    "миллиона": Decimal(1_000_000),
}

WORD_NUMBERS: dict[str, Decimal] = {
    "полторы": Decimal("1.5"),
    "полтора": Decimal("1.5"),
    "пол": Decimal("0.5"),
    "半": Decimal("0.5"),
    "один": Decimal(1),
    "одна": Decimal(1),
    "два": Decimal(2),
    "две": Decimal(2),
    "три": Decimal(3),
    "четыре": Decimal(4),
    "пять": Decimal(5),
    "шесть": Decimal(6),
    "семь": Decimal(7),
    "восемь": Decimal(8),
    "девять": Decimal(9),
    "десять": Decimal(10),
}

# Символы валют ищутся как подстрока, словесные обозначения — только как
# отдельные слова: буква «р» внутри «вчера» не задаёт валюту (A14).
CURRENCY_SYMBOL_TOKENS: dict[str, str] = {
    "₽": "RUB",
    "$": "USD",
    "€": "EUR",
    "₸": "KZT",
    "₺": "TRY",
    "₼": "AZN",
    "֏": "AMD",
    "₾": "GEL",
    "£": "GBP",
}

CURRENCY_WORD_TOKENS: dict[str, str] = {
    "руб": "RUB",
    "рубль": "RUB",
    "рубля": "RUB",
    "рублей": "RUB",
    "р": "RUB",
    "usd": "USD",
    "долл": "USD",
    "доллар": "USD",
    "долларов": "USD",
    "доллара": "USD",
    "eur": "EUR",
    "евро": "EUR",
    "тенге": "KZT",
    "лир": "TRY",
    "лира": "TRY",
    "лиры": "TRY",
    "манат": "AZN",
    "маната": "AZN",
    "gel": "GEL",
    "лари": "GEL",
}

_NUMBER_RE = re.compile(
    r"(?P<number>\d{1,3}(?:[  ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"\s*(?P<suffix>к|k|тыс\.?|тысяч[аиуе]?|млн|миллион[аов]*)?",
    re.IGNORECASE,
)
_WORD_AMOUNT_RE = re.compile(
    r"\b(?P<word>полторы|полтора|одна|один|две|два|три|четыре|пять|шесть|семь|"
    r"восемь|девять|десять)\s+(?P<unit>тысяч[аиуе]?|тыс\.?|млн|миллион[аов]*)\b",
    re.IGNORECASE,
)
_COUNT_WORDS = "|".join(
    ("два", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять")
)
_PER_ITEM_RE = re.compile(
    rf"(?<![\w])(?P<count>\d{{1,3}}|{_COUNT_WORDS})\s+\D{{0,30}}?\s*по\s+"
    r"(?P<price>\d+(?:[.,]\d+)?)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ParsedAmount:
    value: Decimal
    raw: str
    currency: str | None
    ambiguous_options: tuple[Decimal, ...] = ()
    quantity: int | None = None

    @property
    def is_ambiguous(self) -> bool:
        return len(self.ambiguous_options) > 1


def detect_currency(text: str) -> str | None:
    """Определить валюту только по явному обозначению.

    Отсутствие обозначения возвращает None: валюта берётся из настройки
    бюджета либо запрашивается, но не угадывается по букве в слове (A14).
    """
    lowered = text.lower()
    for token, code in CURRENCY_SYMBOL_TOKENS.items():
        if token in lowered:
            return code
    for token, code in CURRENCY_WORD_TOKENS.items():
        if re.search(rf"(?<![а-яёa-z]){re.escape(token)}(?![а-яёa-z])", lowered):
            return code
    for code in CURRENCY_EXPONENTS:
        if re.search(rf"(?<![а-яёa-z]){code.lower()}(?![а-яёa-z])", lowered):
            return code
    return None


def _normalize_number(raw: str) -> tuple[Decimal, tuple[Decimal, ...]]:
    """Вернуть значение и варианты при неоднозначности разделителя."""
    cleaned = raw.replace(" ", " ").strip()
    if " " in cleaned:
        # Пробелы в тысячах: 1 500 -> 1500
        return Decimal(cleaned.replace(" ", "").replace(",", ".")), ()
    if "," in cleaned and "." in cleaned:
        # Последний разделитель считается десятичным.
        if cleaned.rfind(",") > cleaned.rfind("."):
            return Decimal(cleaned.replace(".", "").replace(",", ".")), ()
        return Decimal(cleaned.replace(",", "")), ()
    for separator in (",", "."):
        if separator in cleaned:
            head, _, tail = cleaned.partition(separator)
            if len(tail) == 3 and head and separator == ".":
                # «1.500» допускает две интерпретации (FR-10).
                decimal_reading = Decimal(f"{head}.{tail}")
                thousands_reading = Decimal(f"{head}{tail}")
                return thousands_reading, (thousands_reading, decimal_reading)
            return Decimal(cleaned.replace(",", ".")), ()
    return Decimal(cleaned), ()


def parse_amounts(text: str) -> list[ParsedAmount]:
    """Найти все суммы в тексте, не угадывая отсутствующие значения (AI-05)."""
    currency = detect_currency(text)
    results: list[ParsedAmount] = []

    for match in _WORD_AMOUNT_RE.finditer(text):
        word = match.group("word").lower()
        unit = match.group("unit").lower().rstrip(".")
        base = WORD_NUMBERS.get(word)
        multiplier = next(
            (value for key, value in MULTIPLIERS.items() if unit.startswith(key)), None
        )
        if base is not None and multiplier is not None:
            results.append(
                ParsedAmount(value=base * multiplier, raw=match.group(), currency=currency)
            )

    consumed_spans = [match.span() for match in _WORD_AMOUNT_RE.finditer(text)]

    per_item = _PER_ITEM_RE.search(text)
    if per_item:
        raw_count = per_item.group("count").lower()
        count = (
            int(raw_count) if raw_count.isdigit() else int(WORD_NUMBERS.get(raw_count, Decimal(0)))
        )
        price, _ = _normalize_number(per_item.group("price"))
        if 1 < count <= 100:
            # «Два кофе по 250» означает 500 (A07).
            return [
                ParsedAmount(
                    value=price * count,
                    raw=per_item.group(),
                    currency=currency,
                    quantity=count,
                )
            ]

    for match in _NUMBER_RE.finditer(text):
        span = match.span()
        if any(start <= span[0] < end for start, end in consumed_spans):
            continue
        value, options = _normalize_number(match.group("number"))
        suffix = (match.group("suffix") or "").lower().rstrip(".")
        if suffix:
            multiplier = next(
                (mult for key, mult in MULTIPLIERS.items() if suffix.startswith(key)), None
            )
            if multiplier is not None:
                value *= multiplier
                options = ()
        results.append(
            ParsedAmount(
                value=value,
                raw=match.group().strip(),
                currency=currency,
                ambiguous_options=options,
            )
        )
    return results
