"""Проверки денежного ядра (FR-26, DATA_CONTRACT §1)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fintracker.core.money import (
    CurrencyMismatch,
    Money,
    MoneyError,
    allocate_largest_remainder,
    money_sum,
)


def test_rub_minor_units() -> None:
    """650 ₽ хранится как 65000 минимальных единиц (FR-26)."""
    assert Money.from_decimal("650.00", "RUB").minor == 65000
    assert Money.from_decimal(Decimal("1.5"), "RUB").minor == 150
    assert Money(65000, "RUB").to_decimal() == Decimal("650.00")


def test_zero_exponent_currency() -> None:
    assert Money.from_decimal("1500", "JPY").minor == 1500
    assert Money.from_decimal("1500", "KWD").minor == 1_500_000


def test_float_is_rejected() -> None:
    """Двоичный float в финансовом пути запрещён."""
    with pytest.raises(MoneyError):
        Money.from_decimal(650.10, "RUB")  # type: ignore[arg-type]


def test_currency_mismatch_blocked() -> None:
    with pytest.raises(CurrencyMismatch):
        Money(100, "RUB") + Money(100, "USD")


def test_unknown_currency_rejected() -> None:
    with pytest.raises(MoneyError):
        Money(100, "XXX")


def test_amount_limit() -> None:
    """DATA_CONTRACT §1: сумма не превышает 10^15 minor units."""
    Money(10**15, "RUB")
    with pytest.raises(MoneyError):
        Money(10**15 + 1, "RUB")


def test_format_groups_digits() -> None:
    """FR-09: числа с разделением разрядов и понятной валютой."""
    assert Money(4820000, "RUB").format() == "48\xa0200\xa0₽"
    assert Money(-120000, "RUB").format() == "-1\xa0200\xa0₽"
    # Дробная часть остаётся, когда она есть, и в выгрузках без символа.
    assert Money(125050, "RUB").format() == "1\xa0250,50\xa0₽"
    assert Money(4820000, "RUB").format(with_symbol=False) == "48\xa0200,00"


def test_money_sum_of_empty_is_explicit_zero() -> None:
    assert money_sum([], "RUB") == Money(0, "RUB")


def test_allocate_one_kopeck_over_three_equal_items() -> None:
    """FORM-09, A27: сумма частей после распределения равна итогу."""
    parts = allocate_largest_remainder(Money(1, "RUB"), [1, 1, 1])
    assert sum(part.minor for part in parts) == 1
    assert [part.minor for part in parts] == [1, 0, 0]


def test_allocate_proportional() -> None:
    parts = allocate_largest_remainder(Money(140000, "RUB"), [100000, 40000])
    assert [part.minor for part in parts] == [100000, 40000]


def test_allocate_negative_total_preserves_sum() -> None:
    parts = allocate_largest_remainder(Money(-100, "RUB"), [1, 1, 1])
    assert sum(part.minor for part in parts) == -100


@given(
    total=st.integers(min_value=-(10**9), max_value=10**9),
    weights=st.lists(st.integers(min_value=0, max_value=10**6), min_size=1, max_size=12),
)
@settings(max_examples=300)
def test_allocation_preserves_total(total: int, weights: list[int]) -> None:
    """Инвариант: распределение никогда не теряет и не создаёт копейку."""
    parts = allocate_largest_remainder(Money(total, "RUB"), weights)
    assert sum(part.minor for part in parts) == total
    assert len(parts) == len(weights)


@given(values=st.lists(st.integers(min_value=-(10**9), max_value=10**9), min_size=0, max_size=40))
def test_sum_is_exact(values: list[int]) -> None:
    total = money_sum([Money(value, "RUB") for value in values], "RUB")
    assert total.minor == sum(values)
