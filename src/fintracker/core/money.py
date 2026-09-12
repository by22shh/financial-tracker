"""Денежные величины в минимальных единицах (FR-26, DATA_CONTRACT §1).

Двоичный float в финансовом пути запрещён. Все суммы — целые minor units
плюс код валюты. Преобразование из десятичной строки выполняется точно.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final, Self

# Справочник валют: код -> число десятичных знаков.
# Расширяется миграцией/конфигурацией, а не догадкой парсера.
CURRENCY_EXPONENTS: Final[dict[str, int]] = {
    "RUB": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "KZT": 2,
    "AZN": 2,
    "TRY": 2,
    "GEL": 2,
    "AMD": 2,
    "UAH": 2,
    "BYN": 2,
    "CNY": 2,
    "AED": 2,
    "RSD": 2,
    "JPY": 0,
    "KRW": 0,
    "CLP": 0,
    "ISK": 0,
    "BHD": 3,
    "KWD": 3,
    "OMR": 3,
    "TND": 3,
    "JOD": 3,
}

CURRENCY_SYMBOLS: Final[dict[str, str]] = {
    "RUB": "₽",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "KZT": "₸",
    "TRY": "₺",
    "UAH": "₴",
    "AMD": "֏",
    "GEL": "₾",
    "CNY": "¥",
}

# DATA_CONTRACT §1: одна обычная сумма > 0 и <= 10^15 minor units.
MAX_MINOR_UNITS: Final[int] = 10**15

_CURRENCY_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z]{3}$")


class MoneyError(ValueError):
    """Нарушение денежного контракта."""


class CurrencyMismatch(MoneyError):
    """Операция над суммами в разных валютах."""


def normalize_currency(code: str) -> str:
    upper = code.strip().upper()
    if not _CURRENCY_RE.match(upper):
        raise MoneyError(f"Некорректный код валюты: {code!r}")
    if upper not in CURRENCY_EXPONENTS:
        raise MoneyError(f"Валюта {upper} отсутствует в справочнике")
    return upper


def currency_exponent(code: str) -> int:
    return CURRENCY_EXPONENTS[normalize_currency(code)]


@dataclass(frozen=True, slots=True, order=False)
class Money:
    """Точная денежная сумма: целые minor units + код валюты."""

    minor: int
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.minor, int) or isinstance(self.minor, bool):
            raise MoneyError(f"minor должен быть int, получено {type(self.minor).__name__}")
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        if abs(self.minor) > MAX_MINOR_UNITS:
            raise MoneyError(f"Сумма {self.minor} превышает предел {MAX_MINOR_UNITS}")

    # --- конструкторы -----------------------------------------------------

    @classmethod
    def zero(cls, currency: str) -> Self:
        return cls(0, currency)

    @classmethod
    def from_decimal(cls, value: Decimal | str | int, currency: str) -> Self:
        """Точное преобразование десятичного значения в minor units.

        Округление выполняется только здесь — на границе преобразования,
        по правилу ROUND_HALF_UP (DATA_CONTRACT §1). float не принимается.
        """
        if isinstance(value, float):  # pragma: no cover - защита контракта
            raise MoneyError("float запрещён в денежном пути (FR-26)")
        cur = normalize_currency(currency)
        try:
            dec = Decimal(value) if not isinstance(value, Decimal) else value
        except (InvalidOperation, ArithmeticError) as exc:
            raise MoneyError(f"Не удалось разобрать сумму {value!r}") from exc
        if not dec.is_finite():
            raise MoneyError(f"Нечисловая сумма: {value!r}")
        exponent = CURRENCY_EXPONENTS[cur]
        scaled = (dec * (Decimal(10) ** exponent)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return cls(int(scaled), cur)

    # --- представление ----------------------------------------------------

    def to_decimal(self) -> Decimal:
        exponent = CURRENCY_EXPONENTS[self.currency]
        return Decimal(self.minor).scaleb(-exponent)

    def format(self, *, with_symbol: bool = True, group: bool = True) -> str:
        """Человекочитаемый вид с разделением разрядов (FR-09)."""
        exponent = CURRENCY_EXPONENTS[self.currency]
        sign = "-" if self.minor < 0 else ""
        digits = str(abs(self.minor)).rjust(exponent + 1, "0")
        whole, frac = (digits[:-exponent], digits[-exponent:]) if exponent else (digits, "")
        if group:
            whole = f"{int(whole):,}".replace(",", " ")
        body = f"{whole},{frac}" if frac else whole
        if with_symbol:
            symbol = CURRENCY_SYMBOLS.get(self.currency, self.currency)
            return f"{sign}{body} {symbol}"
        return f"{sign}{body}"

    def __str__(self) -> str:
        return self.format()

    def __repr__(self) -> str:
        return f"Money({self.minor}, {self.currency!r})"

    # --- арифметика -------------------------------------------------------

    def _check(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatch(f"{self.currency} != {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor - other.minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.minor, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.minor), self.currency)

    def __mul__(self, factor: int) -> Money:
        if not isinstance(factor, int) or isinstance(factor, bool):
            raise MoneyError("Умножение денег допускается только на целое число")
        return Money(self.minor * factor, self.currency)

    __rmul__ = __mul__

    def __lt__(self, other: Money) -> bool:
        self._check(other)
        return self.minor < other.minor

    def __le__(self, other: Money) -> bool:
        self._check(other)
        return self.minor <= other.minor

    def __gt__(self, other: Money) -> bool:
        self._check(other)
        return self.minor > other.minor

    def __ge__(self, other: Money) -> bool:
        self._check(other)
        return self.minor >= other.minor

    @property
    def is_zero(self) -> bool:
        return self.minor == 0

    @property
    def is_positive(self) -> bool:
        return self.minor > 0

    @property
    def is_negative(self) -> bool:
        return self.minor < 0


def money_sum(items: list[Money], currency: str) -> Money:
    """Сумма списка; пустой список даёт явный ноль указанной валюты."""
    total = Money.zero(currency)
    for item in items:
        total = total + item
    return total


def allocate_largest_remainder(total: Money, weights: list[int]) -> list[Money]:
    """Пропорциональное распределение методом наибольших остатков (FORM-09, A27).

    Сумма результата всегда точно равна `total` — копейки не теряются.
    Веса неотрицательные; при нулевой сумме весов распределение равномерное.
    """
    if not weights:
        raise MoneyError("Нужен хотя бы один вес распределения")
    if any(w < 0 for w in weights):
        raise MoneyError("Отрицательный вес распределения недопустим")
    count = len(weights)
    weight_total = sum(weights)
    if weight_total == 0:
        weights = [1] * count
        weight_total = count

    sign = -1 if total.minor < 0 else 1
    amount = abs(total.minor)

    base: list[int] = []
    remainders: list[tuple[int, int]] = []  # (остаток, индекс)
    for index, weight in enumerate(weights):
        product = amount * weight
        share, rest = divmod(product, weight_total)
        base.append(share)
        remainders.append((rest, index))

    leftover = amount - sum(base)
    # Больший остаток получает лишнюю единицу; при равенстве — меньший индекс.
    remainders.sort(key=lambda pair: (-pair[0], pair[1]))
    for position in range(leftover):
        base[remainders[position][1]] += 1

    return [Money(sign * value, total.currency) for value in base]
