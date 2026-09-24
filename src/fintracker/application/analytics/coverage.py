"""Полнота учёта, сверка счёта и замещение агрегатов (FR-69–FR-72, CMD-15).

Статус полноты — основание, а не абсолютная гарантия. Совпадение остатков
и полнота расходов остаются разными показателями: взаимно компенсирующие
пропуски не обнаруживаются сверкой баланса.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.core.context import ActorContext
from fintracker.core.errors import ConflictError, NotFound, ValidationFailed
from fintracker.core.money import Money
from fintracker.db.models.catalog import Account
from fintracker.db.models.commitments import CoverageRecord, Reconciliation
from fintracker.db.models.integrations import ImportedAggregateLink
from fintracker.db.models.ledger import (
    AccountEntry,
    Allocation,
    OpeningAdjustment,
    Transaction,
    TransactionLink,
)
from fintracker.db.models.planning import BudgetPeriod
from fintracker.db.uow import UnitOfWork

COVERAGE_STATUSES = ("incomplete", "reconciled_source", "confirmed_complete")


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    reconciliation_id: uuid.UUID
    account_id: uuid.UUID
    cutoff_date: dt.date
    observed_minor: int
    computed_minor: int
    difference_minor: int
    balance_kind: str
    status: str

    @property
    def matches(self) -> bool:
        return self.difference_minor == 0

    def explanation(self, currency: str) -> str:
        """Пояснение с явной границей доказательности (FR-71, RV05)."""
        lines = [
            f"🔎 Сверка счёта на {self.cutoff_date.isoformat()}",
            "",
            f"Указанный остаток: {Money(self.observed_minor, currency).format()}",
            f"По известным движениям: {Money(self.computed_minor, currency).format()}",
        ]
        if self.matches:
            lines.append(
                "\n✅ Расхождения нет\n\nСовпадение остатка не доказывает полноту расходов: "
                "пропущенные доход и расход одной суммы компенсируют друг друга."
            )
        else:
            lines.append(
                f"\n⚠️ Расхождение: {Money(self.difference_minor, currency).format()}\n\n"
                "Проверьте пропущенные и повторные записи. Пока причина не найдена, "
                "оценки на основе этого остатка показаны с ограничениями."
            )
        return "\n".join(lines)


async def computed_balance(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
    cutoff_date: dt.date,
) -> int:
    """Известный расчёт остатка на дату по неизменяемым движениям (ADR-03)."""
    value = (
        await session.execute(
            select(func.coalesce(func.sum(AccountEntry.signed_minor), 0)).where(
                AccountEntry.workspace_id == workspace_id,
                AccountEntry.account_id == account_id,
                AccountEntry.effective_date <= cutoff_date,
            )
        )
    ).scalar_one()
    return int(value)


async def record_reconciliation(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    account_id: uuid.UUID,
    cutoff_date: dt.date,
    observed: Money,
    balance_kind: str = "posted",
) -> ReconciliationResult:
    """Зафиксировать сверку остатка (FR-71, CMD-15)."""
    workspace_id = actor.require_workspace()
    account = (
        await session.execute(
            select(Account).where(Account.workspace_id == workspace_id, Account.id == account_id)
        )
    ).scalar_one_or_none()
    if account is None:
        raise NotFound("Счёт недоступен")
    if account.mode != "full_tracking":
        # Reference и неизвестный счёт не дают достоверного остатка (R01).
        raise ConflictError(
            "Сверка доступна только для счёта с полным учётом: у reference-счёта "
            "нет обещания фактического остатка"
        )
    if observed.currency != account.currency:
        raise ValidationFailed("Валюта остатка не совпадает с валютой счёта")

    computed = await computed_balance(
        session,
        workspace_id=workspace_id,
        account_id=account_id,
        cutoff_date=cutoff_date,
    )
    row = Reconciliation(
        workspace_id=workspace_id,
        account_id=account_id,
        cutoff_date=cutoff_date,
        balance_kind=balance_kind,
        observed_minor=observed.minor,
        computed_minor=computed,
        difference_minor=observed.minor - computed,
        status="open",
        basis_revision=0,
        author_id=actor.user_id,
    )
    session.add(row)
    await session.flush()
    await uow.bump_revisions(workspace_id, coverage=True)
    return ReconciliationResult(
        reconciliation_id=row.id,
        account_id=account_id,
        cutoff_date=cutoff_date,
        observed_minor=observed.minor,
        computed_minor=computed,
        difference_minor=row.difference_minor,
        balance_kind=balance_kind,
        status=row.status,
    )


async def accept_reconciliation(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    reconciliation_id: uuid.UUID,
    adjust: bool,
    reason: str | None = None,
) -> Reconciliation:
    """Принять сверку; техническая корректировка проходит через ledger.

    Корректировка не попадает в доходы и расходы (FR-71, DATA_CONTRACT §2.4).
    """
    workspace_id = actor.require_workspace()
    row = (
        await session.execute(
            select(Reconciliation)
            .where(
                Reconciliation.workspace_id == workspace_id,
                Reconciliation.id == reconciliation_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Сверка недоступна")
    if row.status == "accepted":
        return row
    if row.is_stale:
        raise ConflictError(
            "Основа сверки изменилась: повторите сверку остатка",
            details={"reconciliation_id": str(reconciliation_id)},
        )
    # Основа могла измениться между предпросмотром и подтверждением: остаток
    # пересчитывается под блокировкой строки (FR-71, RV04, G-11).
    current = await computed_balance(
        session,
        workspace_id=workspace_id,
        account_id=row.account_id,
        cutoff_date=row.cutoff_date,
    )
    if current != row.computed_minor:
        row.is_stale = True
        row.computed_minor = current
        row.difference_minor = row.observed_minor - current
        await session.flush()
        raise ConflictError(
            "Остаток счёта изменился после предпросмотра сверки: подтвердите заново",
            details={
                "reconciliation_id": str(reconciliation_id),
                "computed_minor": current,
            },
        )
    if adjust and row.difference_minor != 0:
        if not reason:
            raise ValidationFailed("Для корректировки нужна причина")
        adjustment = OpeningAdjustment(
            workspace_id=workspace_id,
            account_id=row.account_id,
            amount_minor=row.difference_minor,
            effective_date=row.cutoff_date,
            kind="reconciliation",
            reason=reason,
            created_by=actor.user_id,
        )
        session.add(adjustment)
        await session.flush()
        session.add(
            AccountEntry(
                workspace_id=workspace_id,
                account_id=row.account_id,
                opening_adjustment_id=adjustment.id,
                signed_minor=row.difference_minor,
                effective_date=row.cutoff_date,
            )
        )
    row.status = "accepted"
    row.accepted_at = func.now()
    await session.flush()
    await uow.bump_revisions(workspace_id, coverage=True, data=adjust)
    return row


async def mark_stale_reconciliations(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
    changed_date: dt.date,
    money_changed: bool,
) -> int:
    """Денежная правка до cutoff делает сверку требующей проверки (RV04, AR-20).

    Правка только комментария не сбрасывает сверенный баланс.
    """
    if not money_changed:
        return 0
    rows = (
        (
            await session.execute(
                select(Reconciliation).where(
                    Reconciliation.workspace_id == workspace_id,
                    Reconciliation.account_id == account_id,
                    Reconciliation.cutoff_date >= changed_date,
                    Reconciliation.status == "accepted",
                    Reconciliation.is_stale.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.is_stale = True
    await session.flush()
    return len(rows)


async def set_period_completeness(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    period_id: uuid.UUID,
    status: str,
    basis: str,
    scope_person_id: uuid.UUID | None = None,
) -> CoverageRecord:
    """Отметить полноту периода с автором и областью (FR-69).

    Подтверждение одним человеком только своих расходов не закрывает пропуски
    всего бюджета: область сверки сохраняется явно.
    """
    workspace_id = actor.require_workspace()
    if status not in COVERAGE_STATUSES:
        raise ValidationFailed(f"Недопустимый статус полноты: {status}")
    period = (
        await session.execute(
            select(BudgetPeriod)
            .where(BudgetPeriod.workspace_id == workspace_id, BudgetPeriod.id == period_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if period is None:
        raise NotFound("Период недоступен")

    record = CoverageRecord(
        workspace_id=workspace_id,
        scope_kind="person" if scope_person_id else "period",
        period_id=period_id,
        person_id=scope_person_id,
        date_from=period.start_date,
        date_to=period.end_exclusive - dt.timedelta(days=1),
        status=status,
        basis=basis[:200],
        basis_revision=0,
        declared_by=actor.user_id,
    )
    session.add(record)

    # Общая полнота периода повышается только при подтверждении всей области.
    if scope_person_id is None:
        period.completeness = status
    await session.flush()
    await uow.bump_revisions(workspace_id, coverage=True)
    await uow.emit(
        workspace_id=workspace_id,
        event_type="CoverageUpdated",
        aggregate_type="budget_period",
        aggregate_id=period_id,
        payload={"period_id": str(period_id), "status": status},
        actor_user_id=actor.user_id,
    )
    return record


@dataclass(frozen=True, slots=True)
class QualityCheck:
    """Экран «Проверить учёт» (R02)."""

    pending_drafts: int
    uncategorized_count: int
    uncategorized_minor: int
    unexplained_reconciliations: int
    stale_reconciliations: int
    periods_incomplete: int
    possible_duplicates: int


async def quality_check(
    session: AsyncSession, *, workspace_id: uuid.UUID, currency: str
) -> QualityCheck:
    """Сводка незакрытых мест учёта (FR-69–FR-71)."""
    from fintracker.db.models.platform import Candidate, Draft

    pending = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Candidate)
                .join(
                    Draft,
                    (Draft.workspace_id == Candidate.workspace_id)
                    & (Draft.id == Candidate.draft_id),
                )
                .where(
                    Candidate.workspace_id == workspace_id,
                    Candidate.state.in_(("draft", "ready", "needs_clarification")),
                    Draft.state.in_(("received", "processing", "needs_clarification", "ready")),
                )
            )
        ).scalar_one()
    )
    uncategorized = (
        await session.execute(
            select(
                func.count(func.distinct(Allocation.transaction_id)),
                func.coalesce(func.sum(Allocation.amount_minor), 0),
            )
            .join(
                Transaction,
                (Transaction.workspace_id == Allocation.workspace_id)
                & (Transaction.id == Allocation.transaction_id)
                & (Transaction.current_revision == Allocation.revision),
            )
            .where(
                Allocation.workspace_id == workspace_id,
                Allocation.category_id.is_(None),
                Allocation.economic_role == "expense",
                Transaction.status == "posted",
            )
        )
    ).one()
    unexplained = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Reconciliation)
                .where(
                    Reconciliation.workspace_id == workspace_id,
                    Reconciliation.status == "open",
                    Reconciliation.difference_minor != 0,
                )
            )
        ).scalar_one()
    )
    stale = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Reconciliation)
                .where(
                    Reconciliation.workspace_id == workspace_id,
                    Reconciliation.is_stale.is_(True),
                )
            )
        ).scalar_one()
    )
    incomplete = int(
        (
            await session.execute(
                select(func.count())
                .select_from(BudgetPeriod)
                .where(
                    BudgetPeriod.workspace_id == workspace_id,
                    BudgetPeriod.completeness == "incomplete",
                )
            )
        ).scalar_one()
    )
    duplicates = int(
        (
            await session.execute(
                select(func.count())
                .select_from(TransactionLink)
                .where(
                    TransactionLink.workspace_id == workspace_id,
                    TransactionLink.link_type == "duplicate_of",
                    TransactionLink.status == "active",
                )
            )
        ).scalar_one()
    )
    return QualityCheck(
        pending_drafts=pending,
        uncategorized_count=int(uncategorized[0]),
        uncategorized_minor=int(uncategorized[1]),
        unexplained_reconciliations=unexplained,
        stale_reconciliations=stale,
        periods_incomplete=incomplete,
        possible_duplicates=duplicates,
    )


async def replace_aggregate(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    aggregate_transaction_id: uuid.UUID,
    replacement_transaction_ids: list[uuid.UUID],
    accept_difference: bool = False,
) -> int:
    """Замещение дневного агрегата подробными операциями (FR-72, A83, A84).

    Одновременно включать в итог агрегат и заменяющие его покупки запрещено;
    при расхождении сумм требуется решение участника.
    """
    from fintracker.application.ledger.service import load_current_spec, void_transaction

    workspace_id = actor.require_workspace()
    _, aggregate_revision, _ = await load_current_spec(
        session, workspace_id=workspace_id, transaction_id=aggregate_transaction_id
    )
    if aggregate_revision.granularity != "daily_aggregate":
        raise ConflictError("Замещать можно только дневной агрегат импорта")

    detail_total = 0
    for transaction_id in replacement_transaction_ids:
        _, revision, _ = await load_current_spec(
            session, workspace_id=workspace_id, transaction_id=transaction_id
        )
        detail_total += revision.amount_minor

    if detail_total != aggregate_revision.amount_minor and not accept_difference:
        # «Другое» не используется для скрытого выравнивания (A84).
        raise ConflictError(
            "Сумма подробных операций отличается от агрегата: нужно решение участника",
            details={
                "aggregate_minor": aggregate_revision.amount_minor,
                "detail_minor": detail_total,
            },
        )

    for transaction_id in replacement_transaction_ids:
        session.add(
            ImportedAggregateLink(
                workspace_id=workspace_id,
                aggregate_transaction_id=aggregate_transaction_id,
                replacement_transaction_id=transaction_id,
                state="active",
                approved_by=actor.user_id,
            )
        )
    # Агрегат перестаёт участвовать в итогах: суммы не складываются.
    await void_transaction(
        session,
        uow,
        actor=actor,
        transaction_id=aggregate_transaction_id,
        reason="Замещён подробными операциями",
    )
    await session.flush()
    return len(replacement_transaction_ids)
