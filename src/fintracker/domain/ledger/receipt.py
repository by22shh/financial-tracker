"""Проверка сумм чека и распределение скидки (FR-15, FR-16, A22–A27).

Позиции должны складываться в итог оплаты с учётом скидок и дополнительных
начислений. Несоответствие выше одной минимальной денежной единицы блокирует
автоматическую запись и требует уточнения.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fintracker.core.money import Money, allocate_largest_remainder

# Допуск сверки — ровно одна минимальная денежная единица (FR-15).
TOLERANCE_MINOR = 1


@dataclass(frozen=True, slots=True)
class ReceiptLineInput:
    label: str
    amount: Money
    category_id: str | None = None
    quantity: str | None = None
    unit_price: str | None = None
    readable: bool = True


@dataclass(frozen=True, slots=True)
class ReceiptCheck:
    """Результат сверки чека."""

    total: Money
    lines_total: Money
    discount: Money
    tip: Money
    difference_minor: int
    within_tolerance: bool
    can_autopost: bool
    unreadable_lines: int
    reason: str | None
    distributed: tuple[ReceiptLineInput, ...]

    @property
    def detailed(self) -> bool:
        """Есть ли достоверная детализация по позициям."""
        return bool(self.distributed) and self.unreadable_lines == 0


def check_receipt(
    *,
    total: Money,
    lines: list[ReceiptLineInput],
    discount: Money | None = None,
    tip: Money | None = None,
    unreadable_lines: int = 0,
) -> ReceiptCheck:
    """Свести позиции с итогом и распределить общую скидку.

    Скидка на весь чек распределяется пропорционально подходящим позициям
    методом наибольших остатков, чтобы конечные копейки сошлись (A27).
    """
    currency = total.currency
    zero = Money.zero(currency)
    discount_value = discount or zero
    tip_value = tip or zero

    readable = [line for line in lines if line.readable]
    distributed: list[ReceiptLineInput] = list(readable)
    if discount_value.minor > 0 and readable:
        shares = allocate_largest_remainder(
            discount_value, [line.amount.minor for line in readable]
        )
        distributed = [
            ReceiptLineInput(
                label=line.label,
                amount=line.amount - share,
                category_id=line.category_id,
                quantity=line.quantity,
                unit_price=line.unit_price,
                readable=line.readable,
            )
            for line, share in zip(readable, shares, strict=True)
        ]

    lines_total = Money(sum(line.amount.minor for line in distributed), currency)
    expected = lines_total + tip_value
    difference = total.minor - expected.minor

    within = abs(difference) <= TOLERANCE_MINOR if distributed else False
    reason: str | None = None
    can_autopost = False

    if not distributed:
        reason = "Позиции не распознаны: доступна запись общей суммы без детализации"
    elif unreadable_lines > 0:
        # Допустимо предложить один расход на достоверный итог (A25).
        reason = (
            f"Не распознано позиций: {unreadable_lines}. Возможна запись общей суммы "
            "с явно неполной детализацией"
        )
    elif difference != 0 and not within:
        reason = (
            f"Позиции и итог расходятся на "
            f"{Money(abs(difference), currency).format()}: нужна проверка"
        )
    else:
        can_autopost = True

    return ReceiptCheck(
        total=total,
        lines_total=lines_total,
        discount=discount_value,
        tip=tip_value,
        difference_minor=difference,
        within_tolerance=within,
        can_autopost=can_autopost,
        unreadable_lines=unreadable_lines,
        reason=reason,
        distributed=tuple(distributed),
    )


def reconcile_to_total(
    check: ReceiptCheck,
) -> tuple[ReceiptLineInput, ...]:
    """Подогнать распределения к итогу в пределах допуска.

    Допуск не разрешает скрыто потерять копейку: разница в одну минимальную
    единицу добавляется к наибольшей позиции, большая разница не проходит.
    """
    if not check.distributed:
        return ()
    if check.difference_minor == 0:
        return check.distributed
    if abs(check.difference_minor) > TOLERANCE_MINOR:
        raise ValueError("Расхождение выше допуска не устраняется автоматически")
    ordered = sorted(
        range(len(check.distributed)),
        key=lambda index: check.distributed[index].amount.minor,
        reverse=True,
    )
    target = ordered[0]
    adjusted = list(check.distributed)
    line = adjusted[target]
    adjusted[target] = ReceiptLineInput(
        label=line.label,
        amount=line.amount + Money(check.difference_minor, line.amount.currency),
        category_id=line.category_id,
        quantity=line.quantity,
        unit_price=line.unit_price,
        readable=line.readable,
    )
    return tuple(adjusted)


def parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except (ArithmeticError, ValueError):
        return None
