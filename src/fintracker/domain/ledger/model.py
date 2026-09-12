"""Доменные правила денежной операции (ADR-03, DATA_CONTRACT §2.4).

Модуль не зависит от Telegram, HTTP, ORM и AI. Здесь проверяется смысл
денег: состав распределений, направление движений и допустимые сочетания
ролей для каждого типа операции.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from fintracker.core.errors import ValidationFailed
from fintracker.core.money import Money


class TransactionType(StrEnum):
    EXPENSE = "expense"
    INCOME = "income"
    EXTERNAL_FUNDING = "external_funding"
    TRANSFER = "transfer"
    REFUND = "refund"
    MIXED_PAYMENT = "mixed_payment"
    LOAN_RECEIVED = "loan_received"
    LOAN_PRINCIPAL_PAYMENT = "loan_principal_payment"
    RECEIVABLE_SETTLEMENT = "receivable_settlement"
    ADJUSTMENT = "adjustment"
    LEGACY_UNCLASSIFIED_FLOW = "legacy_unclassified_flow"


class AllocationRole(StrEnum):
    EXPENSE = "expense"
    RECEIVABLE_INCREASE = "receivable_increase"
    RECEIVABLE_DECREASE = "receivable_decrease"
    LIABILITY_DECREASE = "liability_decrease"
    EXPENSE_REFUND = "expense_refund"
    RECEIVABLE_REVERSAL = "receivable_reversal"
    INCOME = "income"
    EXTERNAL_FUNDING = "external_funding"
    INTEREST_EXPENSE = "interest_expense"
    PRINCIPAL_REPAYMENT = "principal_repayment"
    GOAL_ALLOCATION = "goal_allocation"
    UNCLASSIFIED = "unclassified"


class CoverageMode(StrEnum):
    TRACKED = "tracked"
    REFERENCE = "reference"
    UNKNOWN = "unknown"
    INCLUDED_IN_OPENING = "included_in_opening"


class Granularity(StrEnum):
    INDIVIDUAL = "individual"
    DAILY_AGGREGATE = "daily_aggregate"
    PERIOD_AGGREGATE = "period_aggregate"


# Разрешённые сочетания ролей по типу операции (TZ §20).
ALLOWED_ROLES: dict[TransactionType, frozenset[AllocationRole]] = {
    TransactionType.EXPENSE: frozenset({AllocationRole.EXPENSE}),
    TransactionType.INCOME: frozenset({AllocationRole.INCOME}),
    TransactionType.EXTERNAL_FUNDING: frozenset({AllocationRole.EXTERNAL_FUNDING}),
    TransactionType.TRANSFER: frozenset(),
    TransactionType.REFUND: frozenset(
        {AllocationRole.EXPENSE_REFUND, AllocationRole.RECEIVABLE_REVERSAL}
    ),
    TransactionType.MIXED_PAYMENT: frozenset(
        {
            AllocationRole.EXPENSE,
            AllocationRole.RECEIVABLE_INCREASE,
            AllocationRole.LIABILITY_DECREASE,
        }
    ),
    TransactionType.LOAN_RECEIVED: frozenset(),
    TransactionType.LOAN_PRINCIPAL_PAYMENT: frozenset({AllocationRole.PRINCIPAL_REPAYMENT}),
    TransactionType.RECEIVABLE_SETTLEMENT: frozenset({AllocationRole.RECEIVABLE_DECREASE}),
    TransactionType.ADJUSTMENT: frozenset(),
    TransactionType.LEGACY_UNCLASSIFIED_FLOW: frozenset({AllocationRole.UNCLASSIFIED}),
}

# Роли, входящие в потребительский расход отчёта (FR-29, R05).
CONSUMPTION_ROLES = frozenset({AllocationRole.EXPENSE, AllocationRole.INTEREST_EXPENSE})
CONSUMPTION_REDUCING_ROLES = frozenset({AllocationRole.EXPENSE_REFUND})

# Роли, требующие категорию (иначе состояние «Без категории» осознанное).
ROLES_WITH_CATEGORY = frozenset(
    {
        AllocationRole.EXPENSE,
        AllocationRole.EXPENSE_REFUND,
        AllocationRole.INTEREST_EXPENSE,
        AllocationRole.INCOME,
        AllocationRole.UNCLASSIFIED,
    }
)


@dataclass(frozen=True, slots=True)
class AllocationSpec:
    """Часть денежного события."""

    role: AllocationRole
    amount: Money
    category_id: uuid.UUID | None = None
    beneficiary_id: uuid.UUID | None = None
    stable_line_id: uuid.UUID = field(default_factory=uuid.uuid4)
    related_object_kind: str | None = None
    related_object_id: uuid.UUID | None = None
    line_label: str | None = None
    quantity: str | None = None
    unit_price: str | None = None

    def __post_init__(self) -> None:
        if not self.amount.is_positive:
            raise ValidationFailed(
                f"Сумма части распределения должна быть положительной, получено {self.amount}"
            )


@dataclass(frozen=True, slots=True)
class CashLegSpec:
    """Направление реального внешнего потока денег."""

    signed: Money
    account_id: uuid.UUID | None = None
    coverage: CoverageMode = CoverageMode.UNKNOWN

    def __post_init__(self) -> None:
        if self.signed.is_zero:
            raise ValidationFailed("Движение денег не может быть нулевым")
        if self.coverage is CoverageMode.UNKNOWN and self.account_id is not None:
            raise ValidationFailed("Неизвестный способ оплаты не связывается со счётом")
        if self.coverage is not CoverageMode.UNKNOWN and self.account_id is None:
            raise ValidationFailed("Для известного охвата нужен счёт")

    @property
    def creates_account_entry(self) -> bool:
        """AccountEntry создаётся только для tracked части после opening cutoff."""
        return self.coverage is CoverageMode.TRACKED


@dataclass(frozen=True, slots=True)
class TransactionSpec:
    """Полное описание проводимой операции до записи в базу."""

    transaction_type: TransactionType
    amount: Money
    occurred_date: dt.date
    timezone: str
    allocations: tuple[AllocationSpec, ...]
    cash_legs: tuple[CashLegSpec, ...]
    granularity: Granularity = Granularity.INDIVIDUAL
    occurred_end_date: dt.date | None = None
    occurred_at: dt.datetime | None = None
    date_precision: str = "day"
    description: str | None = None
    merchant: str | None = None
    note: str | None = None
    spender_person_id: uuid.UUID | None = None
    tag_ids: tuple[uuid.UUID, ...] = ()

    def validate(self) -> None:
        """Проверить денежные инварианты до записи (DATA_CONTRACT §2.4)."""
        _validate_currency(self)
        _validate_roles(self)
        _validate_allocation_total(self)
        _validate_cash_direction(self)
        _validate_dates(self)
        _validate_categories(self)


def _validate_currency(spec: TransactionSpec) -> None:
    currency = spec.amount.currency
    for allocation in spec.allocations:
        if allocation.amount.currency != currency:
            raise ValidationFailed(
                f"Валюта части {allocation.amount.currency} не совпадает с валютой операции "
                f"{currency}"
            )
    for leg in spec.cash_legs:
        if leg.signed.currency != currency:
            raise ValidationFailed(
                f"Валюта движения {leg.signed.currency} не совпадает с валютой операции {currency}"
            )


def _validate_roles(spec: TransactionSpec) -> None:
    allowed = ALLOWED_ROLES[spec.transaction_type]
    for allocation in spec.allocations:
        if allocation.role not in allowed:
            raise ValidationFailed(
                f"Роль {allocation.role} недопустима для операции типа {spec.transaction_type}"
            )
    if (
        spec.transaction_type
        in {
            TransactionType.TRANSFER,
            TransactionType.ADJUSTMENT,
            TransactionType.LOAN_RECEIVED,
        }
        and spec.allocations
    ):
        raise ValidationFailed(
            f"У операции типа {spec.transaction_type} не бывает распределений расхода"
        )


def _validate_allocation_total(spec: TransactionSpec) -> None:
    if spec.transaction_type in {
        TransactionType.TRANSFER,
        TransactionType.ADJUSTMENT,
        TransactionType.LOAN_RECEIVED,
    }:
        return
    total = sum(allocation.amount.minor for allocation in spec.allocations)
    if total != spec.amount.minor:
        raise ValidationFailed(
            f"Сумма частей {total} не равна сумме операции {spec.amount.minor}",
            details={"allocations_total": total, "amount": spec.amount.minor},
        )


def _validate_cash_direction(spec: TransactionSpec) -> None:
    total = sum(leg.signed.minor for leg in spec.cash_legs)
    amount = spec.amount.minor
    outflow = {
        TransactionType.EXPENSE,
        TransactionType.MIXED_PAYMENT,
        TransactionType.LOAN_PRINCIPAL_PAYMENT,
    }
    inflow = {
        TransactionType.INCOME,
        TransactionType.EXTERNAL_FUNDING,
        TransactionType.REFUND,
        TransactionType.RECEIVABLE_SETTLEMENT,
        TransactionType.LOAN_RECEIVED,
    }
    if spec.transaction_type in outflow and total != -amount:
        raise ValidationFailed(
            f"Сумма движений {total} должна равняться -{amount} для {spec.transaction_type}"
        )
    if spec.transaction_type in inflow and total != amount:
        raise ValidationFailed(
            f"Сумма движений {total} должна равняться +{amount} для {spec.transaction_type}"
        )
    if spec.transaction_type is TransactionType.TRANSFER:
        if len(spec.cash_legs) != 2:
            raise ValidationFailed("Перевод состоит ровно из двух сторон одного движения")
        if total != 0:
            raise ValidationFailed(f"Сумма сторон перевода должна быть нулевой, получено {total}")
        outgoing = [leg for leg in spec.cash_legs if leg.signed.is_negative]
        incoming = [leg for leg in spec.cash_legs if leg.signed.is_positive]
        if len(outgoing) != 1 or len(incoming) != 1:
            raise ValidationFailed("У перевода должны быть одна сторона списания и одна зачисления")
        if abs(outgoing[0].signed.minor) != amount:
            raise ValidationFailed("Стороны перевода должны равняться сумме операции")
        if outgoing[0].account_id is not None and outgoing[0].account_id == incoming[0].account_id:
            raise ValidationFailed("Перевод между одним и тем же счётом не имеет смысла")
    if spec.transaction_type is TransactionType.LEGACY_UNCLASSIFIED_FLOW and spec.cash_legs:
        raise ValidationFailed(
            "Историческое движение неизвестного типа не проводится по реальному счёту"
        )


def _validate_dates(spec: TransactionSpec) -> None:
    if spec.occurred_end_date is not None and spec.occurred_end_date < spec.occurred_date:
        raise ValidationFailed("Конец интервала операции раньше его начала")
    if spec.granularity is Granularity.PERIOD_AGGREGATE and spec.occurred_end_date is None:
        raise ValidationFailed("Агрегат периода обязан хранить интервал дат")
    if spec.granularity is Granularity.INDIVIDUAL and spec.occurred_end_date is not None:
        raise ValidationFailed("У отдельной операции не бывает интервала дат")


def _validate_categories(spec: TransactionSpec) -> None:
    for allocation in spec.allocations:
        if allocation.role is AllocationRole.RECEIVABLE_INCREASE and allocation.category_id:
            raise ValidationFailed("Возмещаемая доля не относится к расходной категории бюджета")


def consumption_delta(allocations: tuple[AllocationSpec, ...]) -> int:
    """Вклад распределений в потребительский расход (FR-29)."""
    total = 0
    for allocation in allocations:
        if allocation.role in CONSUMPTION_ROLES:
            total += allocation.amount.minor
        elif allocation.role in CONSUMPTION_REDUCING_ROLES:
            total -= allocation.amount.minor
    return total


def reverse_legs(legs: tuple[CashLegSpec, ...]) -> tuple[CashLegSpec, ...]:
    """Точные обратные движения для исправления или отмены (ADR-03)."""
    return tuple(
        CashLegSpec(signed=-leg.signed, account_id=leg.account_id, coverage=leg.coverage)
        for leg in legs
    )
