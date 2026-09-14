"""Единственный путь проведения и исправления денег (ADR-03, ADR-10, CMD-11).

Формы, Telegram обработчики, импорт и AI-предложения используют этот сервис.
Команды: CMD-11 (создание, правка, отмена, восстановление), CMD-12 (чтение
журнала и ревизий).
Операция, её ревизия, движения, эффект, счётчики версий и outbox сохраняются
одним commit; точкой истины является commit базы (TECH-03).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.core.context import ActorContext
from fintracker.core.errors import ConflictError, NotFound, ValidationFailed
from fintracker.core.money import Money
from fintracker.db.models.access import Workspace
from fintracker.db.models.catalog import Account, Category, TransactionTag
from fintracker.db.models.commitments import Occurrence, OccurrenceSettlement
from fintracker.db.models.ledger import (
    AccountEntry,
    Allocation,
    CashLeg,
    FinancialEffect,
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
    Granularity,
    TransactionSpec,
    TransactionType,
)


@dataclass(frozen=True, slots=True)
class PostedTransaction:
    transaction_id: uuid.UUID
    revision: int
    effect_id: uuid.UUID
    occurred_date: dt.date
    amount: Money
    entity_version: int


async def _validate_references(
    session: AsyncSession, *, workspace_id: uuid.UUID, spec: TransactionSpec
) -> None:
    """Категории и счета должны принадлежать этому бюджету и быть активны."""
    category_ids = {a.category_id for a in spec.allocations if a.category_id}
    if category_ids:
        rows = (
            await session.execute(
                select(Category.id, Category.archived_at).where(
                    Category.workspace_id == workspace_id, Category.id.in_(category_ids)
                )
            )
        ).all()
        found = {row.id for row in rows}
        missing = category_ids - found
        if missing:
            raise ValidationFailed(
                "Указана категория, отсутствующая в справочнике этого бюджета",
                details={"unknown_categories": [str(i) for i in sorted(missing, key=str)]},
            )
        archived = {row.id for row in rows if row.archived_at is not None}
        if archived:
            # Архивная категория не восстанавливается автоматически (A123).
            raise ValidationFailed(
                "Категория в архиве: выберите новую или явно восстановите прежнюю",
                details={"archived_categories": [str(i) for i in sorted(archived, key=str)]},
            )

    account_ids = {leg.account_id for leg in spec.cash_legs if leg.account_id}
    if account_ids:
        account_rows = (
            await session.execute(
                select(Account.id, Account.currency, Account.mode, Account.archived_at).where(
                    Account.workspace_id == workspace_id, Account.id.in_(account_ids)
                )
            )
        ).all()
        found = {row.id for row in account_rows}
        if account_ids - found:
            raise ValidationFailed("Указан счёт, недоступный в этом бюджете")
        for row in account_rows:
            if row.currency != spec.amount.currency:
                raise ValidationFailed(
                    f"Валюта счёта {row.currency} не совпадает с валютой операции "
                    f"{spec.amount.currency}"
                )
            if row.archived_at is not None:
                raise ValidationFailed("Счёт в архиве: выберите другой способ оплаты")
        modes = {row.id: row.mode for row in account_rows}
        for leg in spec.cash_legs:
            if leg.account_id is None:
                continue
            if leg.coverage is CoverageMode.TRACKED and modes[leg.account_id] != "full_tracking":
                raise ValidationFailed(
                    "Движение с полным учётом возможно только по счёту в режиме full_tracking"
                )


async def _write_revision(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    transaction_id: uuid.UUID,
    revision: int,
    previous_revision: int | None,
    change_kind: str,
    changed_by: uuid.UUID | None,
    change_reason: str | None,
    spec: TransactionSpec,
    is_voided: bool = False,
) -> TransactionRevision:
    row = TransactionRevision(
        workspace_id=workspace_id,
        transaction_id=transaction_id,
        revision=revision,
        previous_revision=previous_revision,
        change_kind=change_kind,
        changed_by=changed_by,
        change_reason=change_reason,
        transaction_type=spec.transaction_type.value,
        amount_minor=spec.amount.minor,
        currency=spec.amount.currency,
        occurred_date=spec.occurred_date,
        occurred_end_date=spec.occurred_end_date,
        occurred_at=spec.occurred_at,
        date_precision=spec.date_precision,
        timezone=spec.timezone,
        granularity=spec.granularity.value,
        description=spec.description,
        merchant=spec.merchant,
        note=spec.note,
        spender_person_id=spec.spender_person_id,
        is_voided=is_voided,
    )
    session.add(row)
    await session.flush()
    for allocation in spec.allocations:
        session.add(
            Allocation(
                workspace_id=workspace_id,
                transaction_id=transaction_id,
                revision=revision,
                stable_line_id=allocation.stable_line_id,
                economic_role=allocation.role.value,
                category_id=allocation.category_id,
                beneficiary_id=allocation.beneficiary_id,
                amount_minor=allocation.amount.minor,
                quantity=allocation.quantity,
                unit_price=allocation.unit_price,
                related_object_kind=allocation.related_object_kind,
                related_object_id=allocation.related_object_id,
                line_label=allocation.line_label,
            )
        )
    for leg in spec.cash_legs:
        session.add(
            CashLeg(
                workspace_id=workspace_id,
                transaction_id=transaction_id,
                revision=revision,
                account_id=leg.account_id,
                signed_minor=leg.signed.minor,
                coverage_mode=leg.coverage.value,
            )
        )
    for tag_id in spec.tag_ids:
        session.add(
            TransactionTag(
                workspace_id=workspace_id,
                transaction_id=transaction_id,
                revision=revision,
                tag_id=tag_id,
            )
        )
    # Сессия работает с autoflush=False, поэтому части записываются явно:
    # последующее чтение внутри той же команды должно видеть их.
    await session.flush()
    return row


async def _create_effect(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    transaction_id: uuid.UUID,
    revision: int,
    spec: TransactionSpec,
    replaced_effect_id: uuid.UUID | None,
) -> FinancialEffect:
    """Новый действующий эффект и его движения по счетам."""
    effect = FinancialEffect(
        workspace_id=workspace_id,
        transaction_id=transaction_id,
        source_revision=revision,
        replaced_effect_id=replaced_effect_id,
        is_active=True,
    )
    session.add(effect)
    await session.flush()
    for leg in spec.cash_legs:
        if not leg.creates_account_entry or leg.account_id is None:
            continue
        session.add(
            AccountEntry(
                workspace_id=workspace_id,
                account_id=leg.account_id,
                effect_id=effect.id,
                transaction_id=transaction_id,
                revision=revision,
                signed_minor=leg.signed.minor,
                effective_date=spec.occurred_date,
            )
        )
    await session.flush()
    return effect


async def _reverse_effect(
    session: AsyncSession, *, workspace_id: uuid.UUID, effect_id: uuid.UUID
) -> None:
    """Точные обратные движения к последнему действующему эффекту (ADR-03).

    ``effective_date`` совпадает с исходной датой: это исправление, а не
    новый расход сегодня (DATA_CONTRACT §2.5).
    """
    entries = (
        (
            await session.execute(
                select(AccountEntry).where(
                    AccountEntry.workspace_id == workspace_id,
                    AccountEntry.effect_id == effect_id,
                    AccountEntry.reverses_entry_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for entry in entries:
        already = (
            await session.execute(
                select(AccountEntry.id).where(
                    AccountEntry.workspace_id == workspace_id,
                    AccountEntry.reverses_entry_id == entry.id,
                )
            )
        ).scalar_one_or_none()
        if already is not None:
            continue
        session.add(
            AccountEntry(
                workspace_id=workspace_id,
                account_id=entry.account_id,
                effect_id=entry.effect_id,
                transaction_id=entry.transaction_id,
                revision=entry.revision,
                signed_minor=-entry.signed_minor,
                effective_date=entry.effective_date,
                reverses_entry_id=entry.id,
            )
        )
    await session.flush()


async def _active_effect(
    session: AsyncSession, *, workspace_id: uuid.UUID, transaction_id: uuid.UUID
) -> FinancialEffect | None:
    return (
        await session.execute(
            select(FinancialEffect).where(
                FinancialEffect.workspace_id == workspace_id,
                FinancialEffect.transaction_id == transaction_id,
                FinancialEffect.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


async def _mark_analysis_stale(session: AsyncSession, *, workspace_id: uuid.UUID) -> None:
    """Денежная основа поменялась: предложенные рекомендации больше не текущие."""
    from fintracker.application.intelligence.analysis import mark_stale_recommendations

    workspace = (
        await session.execute(
            select(Workspace)
            .where(Workspace.id == workspace_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    await mark_stale_recommendations(session, workspace=workspace)


async def post_transaction(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    spec: TransactionSpec,
    origin: str,
    source_candidate_id: uuid.UUID | None = None,
) -> PostedTransaction:
    """Провести новую операцию (TECH-03, CMD-11)."""
    workspace_id = actor.require_workspace()
    spec.validate()
    await _validate_references(session, workspace_id=workspace_id, spec=spec)

    if source_candidate_id is not None:
        existing = (
            await session.execute(
                select(Transaction).where(
                    Transaction.workspace_id == workspace_id,
                    Transaction.source_candidate_id == source_candidate_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Поздний результат распознавания не создаёт вторую запись (AR-05).
            effect = await _active_effect(
                session, workspace_id=workspace_id, transaction_id=existing.id
            )
            assert effect is not None
            revision = (
                await session.execute(
                    select(TransactionRevision).where(
                        TransactionRevision.workspace_id == workspace_id,
                        TransactionRevision.transaction_id == existing.id,
                        TransactionRevision.revision == existing.current_revision,
                    )
                )
            ).scalar_one()
            return PostedTransaction(
                transaction_id=existing.id,
                revision=existing.current_revision,
                effect_id=effect.id,
                occurred_date=revision.occurred_date,
                amount=Money(revision.amount_minor, revision.currency),
                entity_version=existing.entity_version,
            )

    transaction = Transaction(
        workspace_id=workspace_id,
        source_candidate_id=source_candidate_id,
        created_by=actor.user_id,
        current_revision=1,
        status="posted",
        occurred_sort_date=spec.occurred_date,
        origin=origin,
    )
    session.add(transaction)
    await session.flush()

    await _write_revision(
        session,
        workspace_id=workspace_id,
        transaction_id=transaction.id,
        revision=1,
        previous_revision=None,
        change_kind="created",
        changed_by=actor.user_id,
        change_reason=None,
        spec=spec,
    )
    effect = await _create_effect(
        session,
        workspace_id=workspace_id,
        transaction_id=transaction.id,
        revision=1,
        spec=spec,
        replaced_effect_id=None,
    )
    await _invalidate_reconciliations(session, workspace_id=workspace_id, specs=(spec,))
    await uow.bump_revisions(workspace_id, data=True)
    await _mark_analysis_stale(session, workspace_id=workspace_id)
    await uow.emit(
        workspace_id=workspace_id,
        event_type="TransactionPosted",
        aggregate_type="transaction",
        aggregate_id=transaction.id,
        aggregate_revision=1,
        payload={"transaction_id": str(transaction.id), "revision": 1},
        # Исторический импорт не рассылает уведомление по каждой прошлой строке:
        # об итоге сообщает одна сводка партии (FR-53, A63).
        audience="none" if origin == "import" else "members",
        actor_user_id=actor.user_id,
    )
    return PostedTransaction(
        transaction_id=transaction.id,
        revision=1,
        effect_id=effect.id,
        occurred_date=spec.occurred_date,
        amount=spec.amount,
        entity_version=transaction.entity_version,
    )


async def load_current_spec(
    session: AsyncSession, *, workspace_id: uuid.UUID, transaction_id: uuid.UUID
) -> tuple[Transaction, TransactionRevision, TransactionSpec]:
    """Прочитать актуальную ревизию как спецификацию для изменения."""
    transaction = (
        await session.execute(
            select(Transaction).where(
                Transaction.workspace_id == workspace_id, Transaction.id == transaction_id
            )
        )
    ).scalar_one_or_none()
    if transaction is None:
        raise NotFound("Операция недоступна")
    revision = (
        await session.execute(
            select(TransactionRevision).where(
                TransactionRevision.workspace_id == workspace_id,
                TransactionRevision.transaction_id == transaction_id,
                TransactionRevision.revision == transaction.current_revision,
            )
        )
    ).scalar_one()
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
    legs = (
        (
            await session.execute(
                select(CashLeg).where(
                    CashLeg.workspace_id == workspace_id,
                    CashLeg.transaction_id == transaction_id,
                    CashLeg.revision == revision.revision,
                )
            )
        )
        .scalars()
        .all()
    )
    tags = (
        (
            await session.execute(
                select(TransactionTag.tag_id).where(
                    TransactionTag.workspace_id == workspace_id,
                    TransactionTag.transaction_id == transaction_id,
                    TransactionTag.revision == revision.revision,
                )
            )
        )
        .scalars()
        .all()
    )
    spec = TransactionSpec(
        transaction_type=TransactionType(revision.transaction_type),
        amount=Money(revision.amount_minor, revision.currency),
        occurred_date=revision.occurred_date,
        occurred_end_date=revision.occurred_end_date,
        occurred_at=revision.occurred_at,
        date_precision=revision.date_precision,
        timezone=revision.timezone,
        granularity=Granularity(revision.granularity),
        description=revision.description,
        merchant=revision.merchant,
        note=revision.note,
        spender_person_id=revision.spender_person_id,
        allocations=tuple(
            AllocationSpec(
                role=AllocationRole(a.economic_role),
                amount=Money(a.amount_minor, revision.currency),
                category_id=a.category_id,
                beneficiary_id=a.beneficiary_id,
                stable_line_id=a.stable_line_id,
                related_object_kind=a.related_object_kind,
                related_object_id=a.related_object_id,
                line_label=a.line_label,
                quantity=a.quantity,
                unit_price=a.unit_price,
            )
            for a in allocations
        ),
        cash_legs=tuple(
            CashLegSpec(
                signed=Money(leg.signed_minor, revision.currency),
                account_id=leg.account_id,
                coverage=CoverageMode(leg.coverage_mode),
            )
            for leg in legs
        ),
        tag_ids=tuple(tags),
    )
    return transaction, revision, spec


async def _guard_linked_refunds(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    transaction_id: uuid.UUID,
    new_spec: TransactionSpec | None,
) -> None:
    """Нельзя оставить возврат больше новой исходной суммы (FR-33, A45–A46)."""
    refunds = (
        (
            await session.execute(
                select(TransactionLink).where(
                    TransactionLink.workspace_id == workspace_id,
                    TransactionLink.source_transaction_id == transaction_id,
                    TransactionLink.link_type == "refund_of",
                    TransactionLink.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    if not refunds:
        return
    refunded_total = sum(link.amount_minor for link in refunds)
    new_total = 0 if new_spec is None else new_spec.amount.minor
    if refunded_total > new_total:
        raise ConflictError(
            "С этой покупкой связаны возвраты на большую сумму: сначала согласуйте "
            "изменение связанных записей",
            details={
                "refunded_minor": refunded_total,
                "proposed_amount_minor": new_total,
                "linked_refunds": [str(link.target_transaction_id) for link in refunds],
            },
        )

    # Возврат оформлен по конкретной части покупки: эту часть нельзя уменьшить
    # ниже уже возвращённой суммы, даже сохранив общий итог (FR-29, G-10).
    if new_spec is None:
        return
    per_line: dict[uuid.UUID | None, int] = {}
    for link in refunds:
        key = link.source_stable_line_id
        per_line[key] = per_line.get(key, 0) + link.amount_minor
    proposed = {
        allocation.stable_line_id: allocation.amount.minor for allocation in new_spec.allocations
    }
    for line_id, refunded in per_line.items():
        if line_id is None:
            continue
        available = proposed.get(line_id, 0)
        if refunded > available:
            raise ConflictError(
                "По части покупки уже оформлен возврат на большую сумму: "
                "сначала согласуйте изменение связанных записей",
                details={
                    "stable_line_id": str(line_id),
                    "refunded_minor": refunded,
                    "proposed_amount_minor": available,
                },
            )


async def _guard_linked_settlements(
    session: AsyncSession, *, workspace_id: uuid.UUID, transaction_id: uuid.UUID
) -> None:
    """Нельзя отменить покупку, по которой уже получено возмещение (FR-30, G-08)."""
    from fintracker.db.models.ledger import Receivable

    receivables = (
        (
            await session.execute(
                select(Receivable).where(
                    Receivable.workspace_id == workspace_id,
                    Receivable.origin_transaction_id == transaction_id,
                )
            )
        )
        .scalars()
        .all()
    )
    collected = [row for row in receivables if row.outstanding_minor < row.original_minor]
    if collected:
        raise ConflictError(
            "По этой покупке уже получено возмещение: сначала отмените возмещение "
            "или согласуйте изменение связанных записей",
            details={
                "receivables": [str(row.id) for row in collected],
                "collected_minor": sum(
                    row.original_minor - row.outstanding_minor for row in collected
                ),
            },
        )


async def _guard_receivable_origin_change(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    transaction_id: uuid.UUID,
    new_spec: TransactionSpec,
) -> None:
    """Нельзя уменьшить исходное требование ниже уже полученных возмещений."""
    from fintracker.db.models.ledger import Receivable

    rows = (
        (
            await session.execute(
                select(Receivable).where(
                    Receivable.workspace_id == workspace_id,
                    Receivable.origin_transaction_id == transaction_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return
    proposed = {
        allocation.stable_line_id: allocation.amount.minor
        for allocation in new_spec.allocations
        if allocation.role is AllocationRole.RECEIVABLE_INCREASE and allocation.stable_line_id
    }
    for receivable in rows:
        collected = receivable.original_minor - receivable.outstanding_minor
        new_original = proposed.get(receivable.origin_stable_line_id, 0)
        if collected > new_original:
            raise ConflictError(
                "По этой покупке уже получено возмещение: сначала отмените возмещение "
                "или согласуйте изменение связанных записей",
                details={
                    "receivable_id": str(receivable.id),
                    "collected_minor": collected,
                    "proposed_original_minor": new_original,
                },
            )


async def _sync_receivable_origins(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    transaction_id: uuid.UUID,
    spec: TransactionSpec,
    active: bool,
) -> None:
    """Согласовать исходные требования совместной покупки с текущей ревизией."""
    from fintracker.db.models.ledger import Receivable

    rows = (
        (
            await session.execute(
                select(Receivable)
                .where(
                    Receivable.workspace_id == workspace_id,
                    Receivable.origin_transaction_id == transaction_id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return
    proposed = {
        allocation.stable_line_id: allocation.amount.minor
        for allocation in spec.allocations
        if allocation.role is AllocationRole.RECEIVABLE_INCREASE and allocation.stable_line_id
    }
    for receivable in rows:
        collected = receivable.original_minor - receivable.outstanding_minor
        if not active:
            if collected > 0:
                raise ConflictError("По этой покупке уже получено возмещение")
            receivable.outstanding_minor = 0
            receivable.status = "settled"
            receivable.version += 1
            continue
        new_original = proposed.get(receivable.origin_stable_line_id, 0)
        if collected > new_original:
            raise ConflictError("Новая сумма требования меньше уже полученного возмещения")
        receivable.original_minor = new_original
        receivable.outstanding_minor = new_original - collected
        receivable.status = "settled" if receivable.outstanding_minor == 0 else "open"
        receivable.version += 1
    await session.flush()


async def _sync_receivable_settlement(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    transaction_id: uuid.UUID,
    effect: FinancialEffect | None,
    spec: TransactionSpec,
    active: bool,
) -> None:
    """Согласовать погашение требования с активным эффектом операции."""
    from fintracker.db.models.ledger import Receivable, ReceivableEntry

    receivable_ids = {
        allocation.related_object_id
        for allocation in spec.allocations
        if allocation.role is AllocationRole.RECEIVABLE_DECREASE and allocation.related_object_id
    }
    old_ids = (
        (
            await session.execute(
                select(ReceivableEntry.receivable_id)
                .join(
                    FinancialEffect,
                    (FinancialEffect.id == ReceivableEntry.effect_id)
                    & (FinancialEffect.workspace_id == ReceivableEntry.workspace_id),
                )
                .where(
                    ReceivableEntry.workspace_id == workspace_id,
                    FinancialEffect.transaction_id == transaction_id,
                    ReceivableEntry.kind == "settlement",
                )
            )
        )
        .scalars()
        .all()
    )
    receivable_ids.update(old_ids)
    if not receivable_ids:
        return
    if active and effect is not None:
        for allocation in spec.allocations:
            if (
                allocation.role is not AllocationRole.RECEIVABLE_DECREASE
                or allocation.related_object_id is None
            ):
                continue
            existing = (
                await session.execute(
                    select(ReceivableEntry).where(
                        ReceivableEntry.workspace_id == workspace_id,
                        ReceivableEntry.receivable_id == allocation.related_object_id,
                        ReceivableEntry.effect_id == effect.id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    ReceivableEntry(
                        workspace_id=workspace_id,
                        receivable_id=allocation.related_object_id,
                        effect_id=effect.id,
                        change_minor=-allocation.amount.minor,
                        kind="settlement",
                    )
                )
            else:
                existing.change_minor = -allocation.amount.minor
                existing.kind = "settlement"
    await session.flush()

    for receivable_id in receivable_ids:
        receivable = (
            await session.execute(
                select(Receivable)
                .where(Receivable.workspace_id == workspace_id, Receivable.id == receivable_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if receivable is None:
            continue
        settled = (
            await session.execute(
                select(ReceivableEntry.change_minor)
                .join(
                    FinancialEffect,
                    (FinancialEffect.id == ReceivableEntry.effect_id)
                    & (FinancialEffect.workspace_id == ReceivableEntry.workspace_id),
                )
                .where(
                    ReceivableEntry.workspace_id == workspace_id,
                    ReceivableEntry.receivable_id == receivable_id,
                    ReceivableEntry.kind == "settlement",
                    FinancialEffect.is_active.is_(True),
                )
            )
        ).scalars()
        settled_minor = -sum(value for value in settled if value < 0)
        if settled_minor > receivable.original_minor:
            raise ConflictError(
                "Погашения превышают сумму требования",
                details={"receivable_id": str(receivable.id)},
            )
        receivable.outstanding_minor = receivable.original_minor - settled_minor
        receivable.status = "settled" if receivable.outstanding_minor == 0 else "open"
        receivable.version += 1
    await session.flush()


async def _sync_occurrence_settlements(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    transaction_id: uuid.UUID,
    effect: FinancialEffect | None,
    spec: TransactionSpec,
    active: bool,
) -> None:
    """Согласовать оплату обязательства с активным эффектом операции."""
    rows = (
        (
            await session.execute(
                select(OccurrenceSettlement).where(
                    OccurrenceSettlement.workspace_id == workspace_id,
                    OccurrenceSettlement.transaction_id == transaction_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return
    current_amount = spec.amount.minor if active else 0
    if active and effect is not None:
        for row in rows:
            row.effect_id = effect.id
            row.amount_minor = current_amount
            row.status = "active"
    else:
        for row in rows:
            row.status = "cancelled"
    await session.flush()

    occurrence_ids = {row.occurrence_id for row in rows}
    from fintracker.domain.schedule import OccurrenceState

    for occurrence_id in occurrence_ids:
        occurrence = (
            await session.execute(
                select(Occurrence)
                .where(Occurrence.workspace_id == workspace_id, Occurrence.id == occurrence_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if occurrence is None:
            continue
        values = (
            await session.execute(
                select(OccurrenceSettlement.amount_minor).where(
                    OccurrenceSettlement.workspace_id == workspace_id,
                    OccurrenceSettlement.occurrence_id == occurrence_id,
                    OccurrenceSettlement.status == "active",
                )
            )
        ).scalars()
        settled = sum(values)
        if occurrence.expected_minor is not None and settled > occurrence.expected_minor:
            raise ConflictError(
                "Оплата превышает ожидаемую сумму платежа",
                details={"occurrence_id": str(occurrence.id)},
            )
        occurrence.settled_minor = settled
        occurrence.state = OccurrenceState(
            expected_minor=occurrence.expected_minor,
            settled_minor=settled,
            due_date=occurrence.due_date,
        ).next_state()
        occurrence.version += 1
    await session.flush()


async def _sync_money_dependents(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    transaction_id: uuid.UUID,
    effect: FinancialEffect | None,
    spec: TransactionSpec,
    active: bool,
) -> None:
    await _sync_receivable_origins(
        session,
        workspace_id=workspace_id,
        transaction_id=transaction_id,
        spec=spec,
        active=active,
    )
    await _sync_receivable_settlement(
        session,
        workspace_id=workspace_id,
        transaction_id=transaction_id,
        effect=effect,
        spec=spec,
        active=active,
    )
    await _sync_occurrence_settlements(
        session,
        workspace_id=workspace_id,
        transaction_id=transaction_id,
        effect=effect,
        spec=spec,
        active=active,
    )


async def revise_transaction(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    transaction_id: uuid.UUID,
    new_spec: TransactionSpec,
    expected_version: int | None,
    change_kind: str = "amended",
    change_reason: str | None = None,
) -> PostedTransaction:
    """Создать новую ревизию с точными обратными и новыми движениями (ADR-03)."""
    workspace_id = actor.require_workspace()
    transaction, current, current_spec = await load_current_spec(
        session, workspace_id=workspace_id, transaction_id=transaction_id
    )
    uow.check_expected_version(transaction.entity_version, expected_version, label="Операция")
    if transaction.status == "voided" and change_kind != "restored":
        raise ConflictError("Операция отменена: сначала восстановите её")

    new_spec.validate()
    await _validate_references(session, workspace_id=workspace_id, spec=new_spec)
    old_cash = tuple(
        (leg.account_id, leg.signed.minor, leg.coverage.value) for leg in current_spec.cash_legs
    )
    new_cash = tuple(
        (leg.account_id, leg.signed.minor, leg.coverage.value) for leg in new_spec.cash_legs
    )
    money_changed = (
        new_spec.amount.minor != current.amount_minor
        or new_spec.occurred_date != current.occurred_date
        or new_spec.transaction_type.value != current.transaction_type
        or new_cash != old_cash
    )
    if money_changed or change_kind == "amended":
        await _guard_linked_refunds(
            session, workspace_id=workspace_id, transaction_id=transaction_id, new_spec=new_spec
        )
        await _guard_receivable_origin_change(
            session, workspace_id=workspace_id, transaction_id=transaction_id, new_spec=new_spec
        )

    revision_number = current.revision + 1
    await _write_revision(
        session,
        workspace_id=workspace_id,
        transaction_id=transaction_id,
        revision=revision_number,
        previous_revision=current.revision,
        change_kind=change_kind,
        changed_by=actor.user_id,
        change_reason=change_reason,
        spec=new_spec,
    )

    previous_effect = await _active_effect(
        session, workspace_id=workspace_id, transaction_id=transaction_id
    )
    if previous_effect is not None:
        await _reverse_effect(session, workspace_id=workspace_id, effect_id=previous_effect.id)
        previous_effect.is_active = False
        await session.flush()
    effect = await _create_effect(
        session,
        workspace_id=workspace_id,
        transaction_id=transaction_id,
        revision=revision_number,
        spec=new_spec,
        replaced_effect_id=previous_effect.id if previous_effect else None,
    )

    transaction.current_revision = revision_number
    transaction.occurred_sort_date = new_spec.occurred_date
    transaction.entity_version += 1
    transaction.status = "posted"
    await session.flush()
    await _sync_money_dependents(
        session,
        workspace_id=workspace_id,
        transaction_id=transaction_id,
        effect=effect,
        spec=new_spec,
        active=True,
    )

    if money_changed and new_spec.transaction_type.value == "refund":
        # Изменённая сумма возврата меняет и занятую долю покупки (FR-29, R-05).
        await _resize_refund_links(
            session,
            workspace_id=workspace_id,
            transaction_id=transaction_id,
            new_amount_minor=new_spec.amount.minor,
        )

    if money_changed or change_kind == "restored":
        # Денежная правка до cutoff делает затронутую сверку требующей
        # повторной проверки; правка заметки — нет (AR-20, RV04).
        await _invalidate_reconciliations(
            session, workspace_id=workspace_id, specs=(current_spec, new_spec)
        )

    await uow.bump_revisions(workspace_id, data=True)
    await _mark_analysis_stale(session, workspace_id=workspace_id)
    await uow.emit(
        workspace_id=workspace_id,
        event_type="TransactionRevised",
        aggregate_type="transaction",
        aggregate_id=transaction_id,
        aggregate_revision=revision_number,
        payload={
            "transaction_id": str(transaction_id),
            "revision": revision_number,
            "change_kind": change_kind,
            "money_changed": money_changed,
        },
        actor_user_id=actor.user_id,
    )
    return PostedTransaction(
        transaction_id=transaction_id,
        revision=revision_number,
        effect_id=effect.id,
        occurred_date=new_spec.occurred_date,
        amount=new_spec.amount,
        entity_version=transaction.entity_version,
    )


async def _sync_dependent_state(
    session: AsyncSession, *, workspace_id: uuid.UUID, transaction_id: uuid.UUID, active: bool
) -> None:
    """Согласовать покрытие требований и обязательств с состоянием операции.

    Отмена возмещения снова открывает долг, отмена связанной оплаты возвращает
    ожидаемый платёж в план; восстановление применяет покрытие заново
    (FR-30, FR-46, G-07, G-09).
    """
    from fintracker.db.models.commitments import Occurrence, OccurrenceSettlement
    from fintracker.db.models.ledger import Receivable, ReceivableEntry

    entries = (
        (
            await session.execute(
                select(ReceivableEntry)
                .join(
                    FinancialEffect,
                    (FinancialEffect.id == ReceivableEntry.effect_id)
                    & (FinancialEffect.workspace_id == ReceivableEntry.workspace_id),
                )
                .where(
                    ReceivableEntry.workspace_id == workspace_id,
                    FinancialEffect.transaction_id == transaction_id,
                    ReceivableEntry.kind == "settlement",
                )
            )
        )
        .scalars()
        .all()
    )
    for entry in entries:
        receivable = (
            await session.execute(
                select(Receivable)
                .where(
                    Receivable.workspace_id == workspace_id,
                    Receivable.id == entry.receivable_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if receivable is None:
            continue
        # change_minor у погашения отрицателен: отмена возвращает его обратно.
        delta = -entry.change_minor if not active else entry.change_minor
        outstanding = receivable.outstanding_minor + delta
        if outstanding < 0 or outstanding > receivable.original_minor:
            raise ConflictError(
                "Восстановление возмещения не помещается в остаток требования",
                details={"receivable_id": str(receivable.id)},
            )
        receivable.outstanding_minor = outstanding
        receivable.status = "settled" if outstanding == 0 else "open"
        receivable.version += 1

    settlements = (
        (
            await session.execute(
                select(OccurrenceSettlement).where(
                    OccurrenceSettlement.workspace_id == workspace_id,
                    OccurrenceSettlement.transaction_id == transaction_id,
                    OccurrenceSettlement.status == ("active" if not active else "cancelled"),
                )
            )
        )
        .scalars()
        .all()
    )
    for settlement in settlements:
        occurrence = (
            await session.execute(
                select(Occurrence)
                .where(
                    Occurrence.workspace_id == workspace_id,
                    Occurrence.id == settlement.occurrence_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if occurrence is None:
            continue
        from fintracker.domain.schedule import OccurrenceState

        delta_minor = settlement.amount_minor if active else -settlement.amount_minor
        settled = occurrence.settled_minor + delta_minor
        if settled < 0:
            settled = 0
        if occurrence.expected_minor is not None and settled > occurrence.expected_minor:
            raise ConflictError(
                "Восстановление оплаты превышает ожидаемую сумму платежа",
                details={"occurrence_id": str(occurrence.id)},
            )
        occurrence.settled_minor = settled
        occurrence.state = OccurrenceState(
            expected_minor=occurrence.expected_minor,
            settled_minor=settled,
            due_date=occurrence.due_date,
        ).next_state()
        occurrence.version += 1
        settlement.status = "active" if active else "cancelled"
    await session.flush()


async def _invalidate_reconciliations(
    session: AsyncSession, *, workspace_id: uuid.UUID, specs: tuple[TransactionSpec, ...]
) -> None:
    """Денежное изменение делает принятую сверку требующей проверки (RV04, G-11)."""
    from fintracker.application.analytics.coverage import mark_stale_reconciliations

    seen: set[tuple[uuid.UUID, dt.date]] = set()
    for spec in specs:
        for leg in spec.cash_legs:
            if leg.account_id is None:
                continue
            key = (leg.account_id, spec.occurred_date)
            if key in seen:
                continue
            seen.add(key)
            await mark_stale_reconciliations(
                session,
                workspace_id=workspace_id,
                account_id=leg.account_id,
                changed_date=spec.occurred_date,
                money_changed=True,
            )


async def void_transaction(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    transaction_id: uuid.UUID,
    expected_version: int | None = None,
    reason: str | None = None,
) -> PostedTransaction:
    """Отменить влияние операции. Повторная отмена безопасна (FR-34, A46)."""
    workspace_id = actor.require_workspace()
    transaction, current, spec = await load_current_spec(
        session, workspace_id=workspace_id, transaction_id=transaction_id
    )
    if transaction.status == "voided":
        # Повтор не добавляет эффект и не уводит суммы ниже (DATA_CONTRACT §2.4).
        return PostedTransaction(
            transaction_id=transaction_id,
            revision=transaction.current_revision,
            effect_id=uuid.UUID(int=0),
            occurred_date=current.occurred_date,
            amount=Money(current.amount_minor, current.currency),
            entity_version=transaction.entity_version,
        )
    uow.check_expected_version(transaction.entity_version, expected_version, label="Операция")
    await _guard_linked_refunds(
        session, workspace_id=workspace_id, transaction_id=transaction_id, new_spec=None
    )
    await _guard_linked_settlements(
        session, workspace_id=workspace_id, transaction_id=transaction_id
    )

    revision_number = current.revision + 1
    await _write_revision(
        session,
        workspace_id=workspace_id,
        transaction_id=transaction_id,
        revision=revision_number,
        previous_revision=current.revision,
        change_kind="voided",
        changed_by=actor.user_id,
        change_reason=reason,
        spec=spec,
        is_voided=True,
    )
    previous_effect = await _active_effect(
        session, workspace_id=workspace_id, transaction_id=transaction_id
    )
    if previous_effect is not None:
        await _reverse_effect(session, workspace_id=workspace_id, effect_id=previous_effect.id)
        previous_effect.is_active = False
    transaction.current_revision = revision_number
    transaction.status = "voided"
    transaction.entity_version += 1
    # Связи отменённой операции перестают занимать лимит возврата (AUD-09).
    await _sync_links_with_status(
        session, workspace_id=workspace_id, transaction_id=transaction_id, active=False
    )
    # Возмещения, исходные требования и оплаты обязательств возвращаются в
    # состояние, соответствующее отсутствию активного эффекта.
    await _sync_money_dependents(
        session,
        workspace_id=workspace_id,
        transaction_id=transaction_id,
        effect=None,
        spec=spec,
        active=False,
    )
    await _invalidate_reconciliations(session, workspace_id=workspace_id, specs=(spec,))
    await session.flush()

    await uow.bump_revisions(workspace_id, data=True)
    await _mark_analysis_stale(session, workspace_id=workspace_id)
    await uow.emit(
        workspace_id=workspace_id,
        event_type="TransactionVoided",
        aggregate_type="transaction",
        aggregate_id=transaction_id,
        aggregate_revision=revision_number,
        payload={"transaction_id": str(transaction_id), "revision": revision_number},
        actor_user_id=actor.user_id,
    )
    return PostedTransaction(
        transaction_id=transaction_id,
        revision=revision_number,
        effect_id=uuid.UUID(int=0),
        occurred_date=current.occurred_date,
        amount=Money(current.amount_minor, current.currency),
        entity_version=transaction.entity_version,
    )


async def restore_transaction(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    transaction_id: uuid.UUID,
    expected_version: int | None = None,
) -> PostedTransaction:
    """Восстановить отменённую операцию новой ревизией (FR-34, A127).

    Повторно проверяются категории, счета и связанные движения.
    """
    workspace_id = actor.require_workspace()
    transaction, _, spec = await load_current_spec(
        session, workspace_id=workspace_id, transaction_id=transaction_id
    )
    if transaction.status != "voided":
        raise ConflictError("Операция не отменена, восстанавливать нечего")
    # Возврат снова займёт долю исходной покупки: свободного остатка должно
    # хватать, иначе восстановление отклоняется целиком (FR-29, R-05).
    await _guard_link_capacity(session, workspace_id=workspace_id, transaction_id=transaction_id)
    restored = await revise_transaction(
        session,
        uow,
        actor=actor,
        transaction_id=transaction_id,
        new_spec=spec,
        expected_version=expected_version,
        change_kind="restored",
    )
    await _sync_links_with_status(
        session, workspace_id=workspace_id, transaction_id=transaction_id, active=True
    )
    return restored


async def _resize_refund_links(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    transaction_id: uuid.UUID,
    new_amount_minor: int,
) -> None:
    """Согласовать занятую долю покупки с новой суммой возврата (R-05)."""
    links = (
        (
            await session.execute(
                select(TransactionLink).where(
                    TransactionLink.workspace_id == workspace_id,
                    TransactionLink.target_transaction_id == transaction_id,
                    TransactionLink.link_type == "refund_of",
                    TransactionLink.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    if not links:
        return
    if len(links) > 1:
        raise ConflictError(
            "Возврат относится к нескольким частям покупки: измените части отдельно"
        )
    link = links[0]
    await _guard_link_capacity(
        session,
        workspace_id=workspace_id,
        transaction_id=transaction_id,
        amounts={link.id: new_amount_minor},
    )
    link.amount_minor = new_amount_minor
    await session.flush()


async def _guard_link_capacity(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    transaction_id: uuid.UUID,
    amounts: dict[uuid.UUID, int] | None = None,
) -> None:
    """Проверить, что вклад этой операции умещается в остаток источника (R-05).

    Учитываются только действующие связи других операций: собственные связи
    восстанавливаемого возврата в остатке ещё не участвуют.
    """
    from fintracker.application.ledger.operations import refundable_parts

    links = (
        (
            await session.execute(
                select(TransactionLink).where(
                    TransactionLink.workspace_id == workspace_id,
                    TransactionLink.target_transaction_id == transaction_id,
                    TransactionLink.link_type == "refund_of",
                )
            )
        )
        .scalars()
        .all()
    )
    if not links:
        return
    remaining: dict[uuid.UUID, dict[uuid.UUID | None, int]] = {}
    for link in links:
        source_id = link.source_transaction_id
        if source_id not in remaining:
            parts = await refundable_parts(
                session, workspace_id=workspace_id, transaction_id=source_id
            )
            remaining[source_id] = {part.stable_line_id: part.refundable_minor for part in parts}
        wanted = link.amount_minor if amounts is None else amounts.get(link.id, link.amount_minor)
        # Собственный действующий вклад уже вычтен из остатка: при правке
        # суммы он сначала возвращается, затем проверяется новая величина.
        own_active = link.amount_minor if link.status == "active" else 0
        free = remaining[source_id].get(link.source_stable_line_id, 0) + own_active
        if wanted > free:
            raise ConflictError(
                "Возврат больше не помещается в остаток исходной покупки: "
                f"свободно {free}, требуется {wanted}"
            )
        remaining[source_id][link.source_stable_line_id] = free - wanted


async def account_balance(
    session: AsyncSession, *, workspace_id: uuid.UUID, account_id: uuid.UUID
) -> int:
    """Баланс счёта — сумма всех его подписанных записей (ADR-03)."""
    from sqlalchemy import func

    value = (
        await session.execute(
            select(func.coalesce(func.sum(AccountEntry.signed_minor), 0)).where(
                AccountEntry.workspace_id == workspace_id,
                AccountEntry.account_id == account_id,
            )
        )
    ).scalar_one()
    return int(value)


async def _sync_links_with_status(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    transaction_id: uuid.UUID,
    active: bool,
) -> None:
    """Согласовать состояние связей с состоянием операции (FR-29, AUD-09).

    Отменённый возврат не занимает возвращаемую сумму исходной покупки;
    восстановление возвращает связь в действующее состояние.
    """
    from sqlalchemy import update as sql_update

    # Операция может быть как источником связи, так и её целью: отменяется
    # вклад именно этой операции.
    await session.execute(
        sql_update(TransactionLink)
        .where(
            TransactionLink.workspace_id == workspace_id,
            TransactionLink.target_transaction_id == transaction_id,
        )
        .values(status="active" if active else "cancelled")
    )
    await session.flush()
