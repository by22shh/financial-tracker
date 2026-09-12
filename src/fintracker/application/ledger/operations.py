"""Переводы, возвраты, смешанные платежи и требования (FR-28–FR-30, FR-34).

Все пути проходят через единственный сервис проведения: формы, Telegram,
импорт и AI-предложения используют одни доменные команды (ADR-10).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.ledger.service import (
    PostedTransaction,
    load_current_spec,
    post_transaction,
)
from fintracker.core.context import ActorContext
from fintracker.core.errors import ConflictError, NotFound, ValidationFailed
from fintracker.core.money import Money
from fintracker.db.models.catalog import Account
from fintracker.db.models.ledger import (
    Allocation,
    Receivable,
    ReceivableEntry,
    Transaction,
    TransactionLink,
    TransactionRevision,
)
from fintracker.db.uow import UnitOfWork
from fintracker.domain.ledger.model import (
    AllocationRole,
    AllocationSpec,
    CashLegSpec,
    CoverageMode,
    TransactionSpec,
    TransactionType,
)


def _coverage_for(account_mode: str | None) -> CoverageMode:
    if account_mode == "full_tracking":
        return CoverageMode.TRACKED
    if account_mode == "reference":
        return CoverageMode.REFERENCE
    return CoverageMode.UNKNOWN


async def _account_modes(
    session: AsyncSession, *, workspace_id: uuid.UUID, account_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not account_ids:
        return {}
    rows = (
        await session.execute(
            select(Account.id, Account.mode).where(
                Account.workspace_id == workspace_id, Account.id.in_(account_ids)
            )
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def post_transfer(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    amount: Money,
    from_account_id: uuid.UUID,
    to_account_id: uuid.UUID,
    occurred_date: dt.date,
    timezone: str,
    description: str | None = None,
    note: str | None = None,
    goal_id: uuid.UUID | None = None,
    origin: str = "form",
) -> PostedTransaction:
    """Перевод между своими счетами (FR-28, A34, A35).

    Один перевод создаёт согласованные изменения двух счетов в одной
    транзакции базы; расход и доход равны нулю.
    """
    workspace_id = actor.require_workspace()
    if from_account_id == to_account_id:
        raise ValidationFailed("Счета перевода должны различаться")
    modes = await _account_modes(
        session, workspace_id=workspace_id, account_ids=[from_account_id, to_account_id]
    )
    if len(modes) != 2:
        raise NotFound("Один из счетов недоступен в этом бюджете")

    spec = TransactionSpec(
        transaction_type=TransactionType.TRANSFER,
        amount=amount,
        occurred_date=occurred_date,
        timezone=timezone,
        description=description,
        note=note,
        allocations=(),
        cash_legs=(
            CashLegSpec(
                signed=-amount,
                account_id=from_account_id,
                coverage=_coverage_for(modes.get(from_account_id)),
            ),
            CashLegSpec(
                signed=amount,
                account_id=to_account_id,
                coverage=_coverage_for(modes.get(to_account_id)),
            ),
        ),
    )
    result = await post_transaction(session, uow, actor=actor, spec=spec, origin=origin)
    if goal_id is not None:
        # Указание цели на переводе не создаёт второй вклад в ту же цель (A40).
        from fintracker.application.commitments.goals import allocate_to_goal

        await allocate_to_goal(
            session,
            uow,
            actor=actor,
            goal_id=goal_id,
            amount=amount,
            effect_id=result.effect_id,
            transaction_id=result.transaction_id,
            reason="Перевод на накопительный счёт",
        )
    return result


async def post_mixed_payment(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    total: Money,
    own_share: Money,
    counterparty_label: str,
    counterparty_person_id: uuid.UUID | None,
    category_id: uuid.UUID | None,
    beneficiary_id: uuid.UUID | None,
    occurred_date: dt.date,
    timezone: str,
    account_id: uuid.UUID | None = None,
    description: str | None = None,
    note: str | None = None,
    origin: str = "form",
) -> PostedTransaction:
    """Оплата за двоих с возмещаемой долей (FR-30, A41).

    Со счёта ушло `total`, собственное потребление — `own_share`, остальное
    остаётся ожидаемым возмещением. В бюджет расхода входит только `expense`.
    """
    workspace_id = actor.require_workspace()
    if own_share.minor <= 0 or own_share.minor >= total.minor:
        raise ValidationFailed("Собственная доля должна быть больше нуля и меньше общей суммы")
    receivable_amount = total - own_share
    expense_line = uuid.uuid4()
    receivable_line = uuid.uuid4()
    modes = await _account_modes(
        session,
        workspace_id=workspace_id,
        account_ids=[account_id] if account_id else [],
    )
    spec = TransactionSpec(
        transaction_type=TransactionType.MIXED_PAYMENT,
        amount=total,
        occurred_date=occurred_date,
        timezone=timezone,
        description=description,
        note=note,
        allocations=(
            AllocationSpec(
                role=AllocationRole.EXPENSE,
                amount=own_share,
                category_id=category_id,
                beneficiary_id=beneficiary_id,
                stable_line_id=expense_line,
            ),
            AllocationSpec(
                role=AllocationRole.RECEIVABLE_INCREASE,
                amount=receivable_amount,
                stable_line_id=receivable_line,
                line_label=counterparty_label,
            ),
        ),
        cash_legs=(
            CashLegSpec(
                signed=-total,
                account_id=account_id,
                coverage=_coverage_for(modes.get(account_id) if account_id else None),
            ),
        ),
    )
    result = await post_transaction(session, uow, actor=actor, spec=spec, origin=origin)

    receivable = Receivable(
        workspace_id=workspace_id,
        counterparty_person_id=counterparty_person_id,
        counterparty_label=counterparty_label,
        currency=total.currency,
        original_minor=receivable_amount.minor,
        outstanding_minor=receivable_amount.minor,
        origin_transaction_id=result.transaction_id,
        origin_stable_line_id=receivable_line,
        status="open",
    )
    session.add(receivable)
    await session.flush()
    session.add(
        ReceivableEntry(
            workspace_id=workspace_id,
            receivable_id=receivable.id,
            effect_id=result.effect_id,
            change_minor=receivable_amount.minor,
            kind="increase",
        )
    )
    return result


async def settle_receivable(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    receivable_id: uuid.UUID,
    amount: Money,
    occurred_date: dt.date,
    timezone: str,
    account_id: uuid.UUID | None = None,
    origin: str = "form",
) -> PostedTransaction:
    """Возмещение долга по совместной покупке (FR-30, A42).

    Погашает требование и не создаёт нового дохода.
    """
    workspace_id = actor.require_workspace()
    receivable = (
        await session.execute(
            select(Receivable)
            .where(Receivable.workspace_id == workspace_id, Receivable.id == receivable_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if receivable is None:
        raise NotFound("Требование недоступно")
    if amount.currency != receivable.currency:
        raise ValidationFailed("Валюта возмещения не совпадает с валютой требования")
    if amount.minor > receivable.outstanding_minor:
        # Частичное возмещение не превышает остаток без отдельного разбора.
        raise ConflictError(
            "Сумма больше непогашенного остатка требования",
            details={"outstanding_minor": receivable.outstanding_minor},
        )

    modes = await _account_modes(
        session, workspace_id=workspace_id, account_ids=[account_id] if account_id else []
    )
    spec = TransactionSpec(
        transaction_type=TransactionType.RECEIVABLE_SETTLEMENT,
        amount=amount,
        occurred_date=occurred_date,
        timezone=timezone,
        description=f"Возмещение: {receivable.counterparty_label or 'контрагент'}",
        allocations=(
            AllocationSpec(
                role=AllocationRole.RECEIVABLE_DECREASE,
                amount=amount,
                related_object_kind="receivable",
                related_object_id=receivable_id,
            ),
        ),
        cash_legs=(
            CashLegSpec(
                signed=amount,
                account_id=account_id,
                coverage=_coverage_for(modes.get(account_id) if account_id else None),
            ),
        ),
    )
    result = await post_transaction(session, uow, actor=actor, spec=spec, origin=origin)
    receivable.outstanding_minor -= amount.minor
    receivable.status = "settled" if receivable.outstanding_minor == 0 else "open"
    receivable.version += 1
    session.add(
        ReceivableEntry(
            workspace_id=workspace_id,
            receivable_id=receivable_id,
            effect_id=result.effect_id,
            change_minor=-amount.minor,
            kind="settlement",
        )
    )
    session.add(
        TransactionLink(
            workspace_id=workspace_id,
            source_transaction_id=receivable.origin_transaction_id,
            target_transaction_id=result.transaction_id,
            link_type="settles_receivable",
            source_stable_line_id=receivable.origin_stable_line_id,
            amount_minor=amount.minor,
            created_by=actor.user_id,
        )
    )
    await session.flush()
    return result


@dataclass(frozen=True, slots=True)
class RefundablePart:
    stable_line_id: uuid.UUID
    role: AllocationRole
    category_id: uuid.UUID | None
    beneficiary_id: uuid.UUID | None
    original_minor: int
    already_refunded_minor: int
    currency: str

    @property
    def refundable_minor(self) -> int:
        return max(0, self.original_minor - self.already_refunded_minor)


async def refundable_parts(
    session: AsyncSession, *, workspace_id: uuid.UUID, transaction_id: uuid.UUID
) -> list[RefundablePart]:
    """Возвращаемые части покупки с учётом уже оформленных возвратов (FR-29)."""
    transaction, revision, _ = await load_current_spec(
        session, workspace_id=workspace_id, transaction_id=transaction_id
    )
    if transaction.status != "posted":
        raise ConflictError("Операция отменена: возврат недоступен")
    allocations = (
        (
            await session.execute(
                select(Allocation).where(
                    Allocation.workspace_id == workspace_id,
                    Allocation.transaction_id == transaction_id,
                    Allocation.revision == revision.revision,
                )
            )
        )
        .scalars()
        .all()
    )
    refunded_rows = (
        await session.execute(
            select(
                TransactionLink.source_stable_line_id,
                func.coalesce(func.sum(TransactionLink.amount_minor), 0),
            )
            .where(
                TransactionLink.workspace_id == workspace_id,
                TransactionLink.source_transaction_id == transaction_id,
                TransactionLink.link_type == "refund_of",
                TransactionLink.status == "active",
            )
            .group_by(TransactionLink.source_stable_line_id)
        )
    ).all()
    refunded: dict[uuid.UUID | None, int] = {row[0]: int(row[1]) for row in refunded_rows}
    return [
        RefundablePart(
            stable_line_id=allocation.stable_line_id,
            role=AllocationRole(allocation.economic_role),
            category_id=allocation.category_id,
            beneficiary_id=allocation.beneficiary_id,
            original_minor=allocation.amount_minor,
            already_refunded_minor=refunded.get(allocation.stable_line_id, 0),
            currency=revision.currency,
        )
        for allocation in allocations
    ]


async def post_refund(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    source_transaction_id: uuid.UUID,
    parts: dict[uuid.UUID, Money],
    occurred_date: dt.date,
    timezone: str,
    account_id: uuid.UUID | None = None,
    note: str | None = None,
    origin: str = "form",
) -> PostedTransaction:
    """Возврат части покупки (FR-29, A36–A38, AR-17).

    По умолчанию уменьшает чистый расход категории в периоде фактического
    возврата; прошлый период не переписывается. Возвращаемые части
    проверяются под блокировкой бюджета: два параллельных возврата не
    получают один и тот же остаток.
    """
    workspace_id = actor.require_workspace()
    available = {
        part.stable_line_id: part
        for part in await refundable_parts(
            session, workspace_id=workspace_id, transaction_id=source_transaction_id
        )
    }
    if not parts:
        raise ValidationFailed("Не выбрана ни одна возвращаемая часть")

    allocations: list[AllocationSpec] = []
    currency: str | None = None
    total = 0
    for stable_line_id, amount in parts.items():
        part = available.get(stable_line_id)
        if part is None:
            raise NotFound("Указанная часть покупки не найдена")
        if amount.minor <= 0:
            raise ValidationFailed("Сумма возврата должна быть положительной")
        if amount.minor > part.refundable_minor:
            raise ConflictError(
                "Возврат превышает ещё не возвращённую сумму этой части",
                details={
                    "refundable_minor": part.refundable_minor,
                    "requested_minor": amount.minor,
                },
            )
        currency = currency or amount.currency
        if amount.currency != currency:
            raise ValidationFailed("Все части возврата должны быть в одной валюте")
        total += amount.minor
        role = (
            AllocationRole.RECEIVABLE_REVERSAL
            if part.role is AllocationRole.RECEIVABLE_INCREASE
            else AllocationRole.EXPENSE_REFUND
        )
        allocations.append(
            AllocationSpec(
                role=role,
                amount=amount,
                # Получатель и категория следуют возвращаемой части (§2.5).
                category_id=part.category_id if role is AllocationRole.EXPENSE_REFUND else None,
                beneficiary_id=part.beneficiary_id
                if role is AllocationRole.EXPENSE_REFUND
                else None,
                related_object_kind="refund_of",
                related_object_id=source_transaction_id,
            )
        )

    assert currency is not None
    amount_total = Money(total, currency)
    modes = await _account_modes(
        session, workspace_id=workspace_id, account_ids=[account_id] if account_id else []
    )
    spec = TransactionSpec(
        transaction_type=TransactionType.REFUND,
        amount=amount_total,
        occurred_date=occurred_date,
        timezone=timezone,
        note=note,
        allocations=tuple(allocations),
        cash_legs=(
            CashLegSpec(
                signed=amount_total,
                account_id=account_id,
                coverage=_coverage_for(modes.get(account_id) if account_id else None),
            ),
        ),
    )
    result = await post_transaction(session, uow, actor=actor, spec=spec, origin=origin)

    for stable_line_id, amount in parts.items():
        session.add(
            TransactionLink(
                workspace_id=workspace_id,
                source_transaction_id=source_transaction_id,
                target_transaction_id=result.transaction_id,
                link_type="refund_of",
                source_stable_line_id=stable_line_id,
                amount_minor=amount.minor,
                created_by=actor.user_id,
            )
        )
    await session.flush()

    # Возврат возмещаемой доли уменьшает ещё открытое требование (§2.5).
    for stable_line_id, amount in parts.items():
        part = available[stable_line_id]
        if part.role is not AllocationRole.RECEIVABLE_INCREASE:
            continue
        receivable = (
            await session.execute(
                select(Receivable)
                .where(
                    Receivable.workspace_id == workspace_id,
                    Receivable.origin_stable_line_id == stable_line_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if receivable is None:
            continue
        if amount.minor > receivable.outstanding_minor:
            # P0 оставляет конфликт на уточнении и не создаёт отрицательное
            # требование или скрытый новый долг.
            raise ConflictError(
                "Доля уже возмещена контрагентом: нужен согласованный разбор расчётов"
            )
        receivable.outstanding_minor -= amount.minor
        receivable.version += 1
        session.add(
            ReceivableEntry(
                workspace_id=workspace_id,
                receivable_id=receivable.id,
                effect_id=result.effect_id,
                change_minor=-amount.minor,
                kind="reversal",
            )
        )
    return result


async def post_cash_withdrawal(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    amount: Money,
    card_account_id: uuid.UUID,
    cash_account_id: uuid.UUID,
    occurred_date: dt.date,
    timezone: str,
    origin: str = "form",
) -> PostedTransaction:
    """Снятие наличных — перевод, расход возникает при покупке (A35)."""
    return await post_transfer(
        session,
        uow,
        actor=actor,
        amount=amount,
        from_account_id=card_account_id,
        to_account_id=cash_account_id,
        occurred_date=occurred_date,
        timezone=timezone,
        description="Снятие наличных",
        origin=origin,
    )


async def linked_refund_total(
    session: AsyncSession, *, workspace_id: uuid.UUID, transaction_id: uuid.UUID
) -> int:
    value = (
        await session.execute(
            select(func.coalesce(func.sum(TransactionLink.amount_minor), 0)).where(
                TransactionLink.workspace_id == workspace_id,
                TransactionLink.source_transaction_id == transaction_id,
                TransactionLink.link_type == "refund_of",
                TransactionLink.status == "active",
            )
        )
    ).scalar_one()
    return int(value)


async def net_spending_for_line(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    category_id: uuid.UUID,
    beneficiary_id: uuid.UUID | None,
    date_from: dt.date,
    date_to_exclusive: dt.date,
) -> int:
    """Чистый расход статьи: расходы минус возвраты (FORM-01: S)."""
    rows = (
        await session.execute(
            select(Allocation.economic_role, func.sum(Allocation.amount_minor))
            .join(
                Transaction,
                (Transaction.workspace_id == Allocation.workspace_id)
                & (Transaction.id == Allocation.transaction_id)
                & (Transaction.current_revision == Allocation.revision),
            )
            .join(
                TransactionRevision,
                (TransactionRevision.workspace_id == Allocation.workspace_id)
                & (TransactionRevision.transaction_id == Allocation.transaction_id)
                & (TransactionRevision.revision == Allocation.revision),
            )
            .where(
                Allocation.workspace_id == workspace_id,
                Allocation.category_id == category_id,
                Allocation.beneficiary_id == beneficiary_id
                if beneficiary_id is not None
                else Allocation.beneficiary_id.is_(None),
                Transaction.status == "posted",
                TransactionRevision.occurred_date >= date_from,
                TransactionRevision.occurred_date < date_to_exclusive,
            )
            .group_by(Allocation.economic_role)
        )
    ).all()
    total = 0
    for role, amount in rows:
        if role == AllocationRole.EXPENSE.value:
            total += int(amount)
        elif role == AllocationRole.EXPENSE_REFUND.value:
            total -= int(amount)
    return total
