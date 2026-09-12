"""План периода: версии, лимиты, перенос и расчёт остатков (FR-35–FR-39, FORM-01).

Формулы раздела 10.2 ТЗ исполняются детерминированным кодом и SQL; AI не
вычисляет итоговые суммы учёта.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.core.context import ActorContext
from fintracker.core.errors import ConflictError, NotFound, ValidationFailed
from fintracker.core.money import Money
from fintracker.db.models.catalog import Category
from fintracker.db.models.commitments import Occurrence, ScheduledItem, ScheduleVersion
from fintracker.db.models.ledger import Allocation, Transaction, TransactionRevision
from fintracker.db.models.planning import (
    BudgetLine,
    BudgetPeriod,
    BudgetVersion,
    Rollover,
)
from fintracker.db.uow import UnitOfWork
from fintracker.domain.ledger.model import (
    CONSUMPTION_REDUCING_ROLES,
    CONSUMPTION_ROLES,
)


class LimitState(StrEnum):
    """Состояния лимита различаются в базе (FORM-02, B2)."""

    NOT_SET = "not_set"
    ZERO = "zero"
    POSITIVE = "positive"
    NEGATIVE_ROLLOVER = "negative_rollover"


@dataclass(frozen=True, slots=True)
class PlanLineSpec:
    """Описание строки плана до записи версии."""

    category_id: uuid.UUID
    beneficiary_id: uuid.UUID | None = None
    limit_minor: int | None = None
    rollover_mode: str = "none"
    is_protected: bool = False
    stable_line_id: uuid.UUID | None = None

    def resolved_stable_id(self, workspace_id: uuid.UUID) -> uuid.UUID:
        if self.stable_line_id is not None:
            return self.stable_line_id
        return uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{workspace_id}:{self.category_id}:{self.beneficiary_id or ''}",
        )


@dataclass(frozen=True, slots=True)
class LineStatus:
    """Строка бюджета с раскладкой L, S, R, C, A (FORM-01)."""

    stable_line_id: uuid.UUID
    category_id: uuid.UUID
    category_name: str
    beneficiary_id: uuid.UUID | None
    beneficiary_name: str | None
    assigned_limit_minor: int | None
    rollover_minor: int
    effective_limit_minor: int | None
    fact_minor: int
    remaining_minor: int | None
    commitments_minor: int
    available_minor: int | None
    overdue_commitments_minor: int
    limit_state: LimitState
    usage_percent: float | None
    is_protected: bool
    currency: str

    @property
    def status_text(self) -> str:
        """Состояние словами, а не только цветом (FR-06, FR-09)."""
        if self.limit_state is LimitState.NOT_SET:
            return "Лимит не задан"
        if self.limit_state is LimitState.NEGATIVE_ROLLOVER:
            deficit = Money(-(self.effective_limit_minor or 0), self.currency)
            return f"Дефицит переноса {deficit.format()}"
        if self.limit_state is LimitState.ZERO:
            if self.fact_minor > 0:
                return f"Расход вне плана {Money(self.fact_minor, self.currency).format()}"
            return "Лимит 0"
        assert self.effective_limit_minor is not None
        if self.fact_minor > self.effective_limit_minor:
            over = Money(self.fact_minor - self.effective_limit_minor, self.currency)
            return f"Превышен на {over.format()}"
        if self.fact_minor == self.effective_limit_minor:
            return "Лимит исчерпан"
        return f"Осталось {Money(self.remaining_minor or 0, self.currency).format()}"


@dataclass(frozen=True, slots=True)
class PeriodStatus:
    period_id: uuid.UUID
    start_date: dt.date
    end_inclusive: dt.date
    currency: str
    plan_status: str
    plan_origin: str
    budget_version_id: uuid.UUID | None
    lines: tuple[LineStatus, ...]
    total_fact_minor: int
    total_limit_minor: int | None
    overall_limit_minor: int | None
    uncategorized_fact_minor: int
    pending_drafts: int
    pending_confident_minor: int
    completeness: str


def line_key(category_id: uuid.UUID, beneficiary_id: uuid.UUID | None) -> str:
    return f"{category_id}:{beneficiary_id or '-'}"


async def current_budget_version(
    session: AsyncSession, *, workspace_id: uuid.UUID, period_id: uuid.UUID, kind: str = "working"
) -> BudgetVersion | None:
    return (
        await session.execute(
            select(BudgetVersion)
            .where(
                BudgetVersion.workspace_id == workspace_id,
                BudgetVersion.period_id == period_id,
                BudgetVersion.kind == kind,
            )
            .order_by(BudgetVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def create_budget_version(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    period_id: uuid.UUID,
    kind: str,
    plan_status: str,
    origin: str,
    lines: list[PlanLineSpec],
    overall_limit_minor: int | None = None,
    template_version: int | None = None,
    approved_by: uuid.UUID | None = None,
    reason: str | None = None,
) -> BudgetVersion:
    """Создать версию плана периода. У каждого периода собственный план."""
    previous = await current_budget_version(
        session, workspace_id=workspace_id, period_id=period_id, kind=kind
    )
    version_number = (previous.version + 1) if previous else 1
    row = BudgetVersion(
        workspace_id=workspace_id,
        period_id=period_id,
        kind=kind,
        version=version_number,
        plan_status=plan_status,
        origin=origin,
        template_version=template_version,
        overall_limit_minor=overall_limit_minor,
        approved_by=approved_by,
        approved_at=func.now() if plan_status == "approved" else None,
        reason=reason,
    )
    session.add(row)
    await session.flush()
    for line in lines:
        session.add(
            BudgetLine(
                workspace_id=workspace_id,
                budget_version_id=row.id,
                stable_line_id=line.resolved_stable_id(workspace_id),
                category_id=line.category_id,
                beneficiary_id=line.beneficiary_id,
                limit_minor=line.limit_minor,
                rollover_mode=line.rollover_mode,
                is_protected=line.is_protected,
            )
        )
    await session.flush()
    return row


async def _fact_by_line(
    session: AsyncSession, *, workspace_id: uuid.UUID, period: BudgetPeriod
) -> tuple[dict[str, int], int, int]:
    """Подтверждённые расходы минус возвраты по строкам (FORM-01: S).

    Читаются только текущие проведённые ревизии; история исправлений не
    суммируется (ADR-03).
    """
    consumption = [role.value for role in CONSUMPTION_ROLES]
    reducing = [role.value for role in CONSUMPTION_REDUCING_ROLES]
    rows = (
        await session.execute(
            select(
                Allocation.category_id,
                Allocation.beneficiary_id,
                Allocation.economic_role,
                func.sum(Allocation.amount_minor).label("total"),
            )
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
                Transaction.status == "posted",
                TransactionRevision.occurred_date >= period.start_date,
                TransactionRevision.occurred_date < period.end_exclusive,
                Allocation.economic_role.in_(consumption + reducing),
            )
            .group_by(Allocation.category_id, Allocation.beneficiary_id, Allocation.economic_role)
        )
    ).all()
    totals: dict[str, int] = {}
    grand_total = 0
    uncategorized = 0
    for row in rows:
        signed = int(row.total) if row.economic_role in consumption else -int(row.total)
        grand_total += signed
        if row.category_id is None:
            uncategorized += signed
            continue
        key = line_key(row.category_id, row.beneficiary_id)
        totals[key] = totals.get(key, 0) + signed
    return totals, grand_total, uncategorized


async def _commitments_by_line(
    session: AsyncSession, *, workspace_id: uuid.UUID, period: BudgetPeriod, today: dt.date
) -> tuple[dict[str, int], dict[str, int]]:
    """Непокрытая часть обязательств до конца периода, включая просрочку (C).

    По одному обязательству учитывается только непокрытая фактом часть;
    просроченная часть прошлого периода включается один раз (FORM-01, B1).
    """
    rows = (
        await session.execute(
            select(
                Occurrence.id,
                Occurrence.due_date,
                Occurrence.expected_minor,
                Occurrence.settled_minor,
                ScheduleVersion.category_id,
                ScheduleVersion.beneficiary_id,
            )
            .join(
                ScheduledItem,
                (ScheduledItem.workspace_id == Occurrence.workspace_id)
                & (ScheduledItem.id == Occurrence.schedule_id),
            )
            .join(
                ScheduleVersion,
                (ScheduleVersion.workspace_id == Occurrence.workspace_id)
                & (ScheduleVersion.schedule_id == Occurrence.schedule_id)
                & (ScheduleVersion.version == Occurrence.schedule_version),
            )
            .where(
                Occurrence.workspace_id == workspace_id,
                Occurrence.state.in_(("planned", "partially_settled")),
                ScheduledItem.direction == "payment",
                Occurrence.due_date < period.end_exclusive,
            )
        )
    ).all()
    commitments: dict[str, int] = {}
    overdue: dict[str, int] = {}
    for row in rows:
        if row.expected_minor is None or row.category_id is None:
            continue
        remaining = max(0, int(row.expected_minor) - int(row.settled_minor))
        if remaining == 0:
            continue
        key = line_key(row.category_id, row.beneficiary_id)
        commitments[key] = commitments.get(key, 0) + remaining
        if row.due_date < max(period.start_date, today):
            overdue[key] = overdue.get(key, 0) + remaining
    return commitments, overdue


async def _accepted_rollovers(
    session: AsyncSession, *, workspace_id: uuid.UUID, period_id: uuid.UUID
) -> dict[uuid.UUID, int]:
    rows = (
        await session.execute(
            select(Rollover.stable_line_id, func.sum(Rollover.amount_minor))
            .where(
                Rollover.workspace_id == workspace_id,
                Rollover.destination_period_id == period_id,
                Rollover.status == "accepted",
            )
            .group_by(Rollover.stable_line_id)
        )
    ).all()
    return {row[0]: int(row[1]) for row in rows}


async def period_status(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    period_id: uuid.UUID,
    currency: str,
    today: dt.date,
) -> PeriodStatus:
    """Полный статус периода: план, факт, обязательства, риски (CMD-16)."""
    period = (
        await session.execute(
            select(BudgetPeriod).where(
                BudgetPeriod.workspace_id == workspace_id, BudgetPeriod.id == period_id
            )
        )
    ).scalar_one_or_none()
    if period is None:
        raise NotFound("Период недоступен")

    version = await current_budget_version(session, workspace_id=workspace_id, period_id=period_id)
    line_rows: list[tuple[BudgetLine, str, str | None]] = []
    if version is not None:
        from fintracker.db.models.access import Beneficiary

        rows = (
            await session.execute(
                select(BudgetLine, Category.name, Beneficiary.name)
                .join(
                    Category,
                    (Category.workspace_id == BudgetLine.workspace_id)
                    & (Category.id == BudgetLine.category_id),
                )
                .outerjoin(
                    Beneficiary,
                    (Beneficiary.workspace_id == BudgetLine.workspace_id)
                    & (Beneficiary.id == BudgetLine.beneficiary_id),
                )
                .where(
                    BudgetLine.workspace_id == workspace_id,
                    BudgetLine.budget_version_id == version.id,
                )
                .order_by(Category.sort_order, Category.name)
            )
        ).all()
        line_rows = [(row[0], row[1], row[2]) for row in rows]

    facts, total_fact, uncategorized = await _fact_by_line(
        session, workspace_id=workspace_id, period=period
    )
    commitments, overdue = await _commitments_by_line(
        session, workspace_id=workspace_id, period=period, today=today
    )
    rollovers = await _accepted_rollovers(session, workspace_id=workspace_id, period_id=period_id)

    statuses: list[LineStatus] = []
    total_limit: int | None = None
    seen_keys: set[str] = set()
    for line, category_name, beneficiary_name in line_rows:
        key = line_key(line.category_id, line.beneficiary_id)
        seen_keys.add(key)
        statuses.append(
            _build_line_status(
                line=line,
                category_name=category_name,
                beneficiary_name=beneficiary_name,
                fact=facts.get(key, 0),
                commitment=commitments.get(key, 0),
                overdue=overdue.get(key, 0),
                rollover=rollovers.get(line.stable_line_id, 0),
                currency=currency,
            )
        )
        if line.limit_minor is not None:
            total_limit = (total_limit or 0) + line.limit_minor

    # Расход по статьям без утверждённого лимита виден отдельно (FR-25, R03).
    for key, fact in facts.items():
        if key in seen_keys or fact == 0:
            continue
        category_id_str, beneficiary_part = key.split(":", 1)
        category_id = uuid.UUID(category_id_str)
        beneficiary_id = None if beneficiary_part == "-" else uuid.UUID(beneficiary_part)
        name = (
            await session.execute(
                select(Category.name).where(
                    Category.workspace_id == workspace_id, Category.id == category_id
                )
            )
        ).scalar_one_or_none() or "Без названия"
        statuses.append(
            LineStatus(
                stable_line_id=uuid.uuid5(
                    uuid.NAMESPACE_URL, f"{workspace_id}:{category_id}:{beneficiary_id or ''}"
                ),
                category_id=category_id,
                category_name=name,
                beneficiary_id=beneficiary_id,
                beneficiary_name=None,
                assigned_limit_minor=None,
                rollover_minor=0,
                effective_limit_minor=None,
                fact_minor=fact,
                remaining_minor=None,
                commitments_minor=commitments.get(key, 0),
                available_minor=None,
                overdue_commitments_minor=overdue.get(key, 0),
                limit_state=LimitState.NOT_SET,
                usage_percent=None,
                is_protected=False,
                currency=currency,
            )
        )

    pending_drafts, pending_minor = await _pending_summary(
        session, workspace_id=workspace_id, period=period
    )
    return PeriodStatus(
        period_id=period.id,
        start_date=period.start_date,
        end_inclusive=period.end_exclusive - dt.timedelta(days=1),
        currency=currency,
        plan_status=version.plan_status if version else "draft",
        plan_origin=version.origin if version else "manual",
        budget_version_id=version.id if version else None,
        lines=tuple(statuses),
        total_fact_minor=total_fact,
        total_limit_minor=total_limit,
        overall_limit_minor=version.overall_limit_minor if version else None,
        uncategorized_fact_minor=uncategorized,
        pending_drafts=pending_drafts,
        pending_confident_minor=pending_minor,
        completeness=period.completeness,
    )


def _build_line_status(
    *,
    line: BudgetLine,
    category_name: str,
    beneficiary_name: str | None,
    fact: int,
    commitment: int,
    overdue: int,
    rollover: int,
    currency: str,
) -> LineStatus:
    assigned = line.limit_minor
    if assigned is None and rollover == 0:
        effective: int | None = None
        state = LimitState.NOT_SET
    else:
        effective = (assigned or 0) + rollover
        if effective < 0:
            state = LimitState.NEGATIVE_ROLLOVER
        elif effective == 0:
            state = LimitState.ZERO
        else:
            state = LimitState.POSITIVE

    remaining = None if effective is None else effective - fact
    available = None if remaining is None else remaining - commitment
    # Процент считается только при положительном лимите (FORM-02, B2).
    usage = (fact / effective * 100) if effective and effective > 0 else None
    return LineStatus(
        stable_line_id=line.stable_line_id,
        category_id=line.category_id,
        category_name=category_name,
        beneficiary_id=line.beneficiary_id,
        beneficiary_name=beneficiary_name,
        assigned_limit_minor=assigned,
        rollover_minor=rollover,
        effective_limit_minor=effective,
        fact_minor=fact,
        remaining_minor=remaining,
        commitments_minor=commitment,
        available_minor=available,
        overdue_commitments_minor=overdue,
        limit_state=state,
        usage_percent=usage,
        is_protected=line.is_protected,
        currency=currency,
    )


async def _pending_summary(
    session: AsyncSession, *, workspace_id: uuid.UUID, period: BudgetPeriod
) -> tuple[int, int]:
    """Число черновиков и достоверная сумма ожидающих (FR-38, CMD-«pending-summary»).

    Черновики не входят в S, но их наличие показывается рядом.
    """
    from fintracker.db.models.platform import Candidate, Draft

    rows = (
        (
            await session.execute(
                select(Candidate.fields)
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
        )
        .scalars()
        .all()
    )
    count = len(rows)
    confident = 0
    for fields in rows:
        amount = fields.get("amount_minor") if isinstance(fields, dict) else None
        occurred = fields.get("occurred_date") if isinstance(fields, dict) else None
        if not isinstance(amount, int) or not isinstance(occurred, str):
            continue
        try:
            day = dt.date.fromisoformat(occurred)
        except ValueError:
            continue
        if period.start_date <= day < period.end_exclusive:
            confident += amount
    return count, confident


async def reallocate_limit(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    period_id: uuid.UUID,
    from_stable_line_id: uuid.UUID,
    to_stable_line_id: uuid.UUID,
    amount_minor: int,
    expected_version: int,
) -> BudgetVersion:
    """Перенести лимит между строками одной транзакцией (FR-37).

    План доходов и общий объём денег не растут.
    """
    workspace_id = actor.require_workspace()
    if amount_minor <= 0:
        raise ValidationFailed("Сумма переноса должна быть положительной")
    if from_stable_line_id == to_stable_line_id:
        raise ValidationFailed("Нельзя перенести лимит в ту же строку")
    version = await current_budget_version(session, workspace_id=workspace_id, period_id=period_id)
    if version is None:
        raise NotFound("План периода не найден")
    uow.check_expected_version(version.version, expected_version, label="План периода")

    lines = (
        (
            await session.execute(
                select(BudgetLine).where(
                    BudgetLine.workspace_id == workspace_id,
                    BudgetLine.budget_version_id == version.id,
                    BudgetLine.stable_line_id.in_((from_stable_line_id, to_stable_line_id)),
                )
            )
        )
        .scalars()
        .all()
    )
    by_stable = {line.stable_line_id: line for line in lines}
    source = by_stable.get(from_stable_line_id)
    target = by_stable.get(to_stable_line_id)
    if source is None or target is None:
        raise NotFound("Одна из строк плана не найдена")
    if source.limit_minor is None:
        raise ValidationFailed("У строки-источника не задан лимит")
    if source.limit_minor < amount_minor:
        raise ConflictError("В строке-источнике недостаточно лимита для переноса")
    if source.is_protected:
        raise ConflictError("Строка защищена: изменение требует отдельного подтверждения плана")

    new_lines = (
        (
            await session.execute(
                select(BudgetLine).where(
                    BudgetLine.workspace_id == workspace_id,
                    BudgetLine.budget_version_id == version.id,
                )
            )
        )
        .scalars()
        .all()
    )
    payload: list[PlanLineSpec] = []
    for line in new_lines:
        limit = line.limit_minor
        if line.stable_line_id == from_stable_line_id and limit is not None:
            limit = limit - amount_minor
        elif line.stable_line_id == to_stable_line_id:
            limit = (limit or 0) + amount_minor
        payload.append(
            PlanLineSpec(
                category_id=line.category_id,
                beneficiary_id=line.beneficiary_id,
                stable_line_id=line.stable_line_id,
                limit_minor=limit,
                rollover_mode=line.rollover_mode,
                is_protected=line.is_protected,
            )
        )
    created = await create_budget_version(
        session,
        workspace_id=workspace_id,
        period_id=period_id,
        kind="working",
        plan_status=version.plan_status,
        origin="manual",
        lines=payload,
        overall_limit_minor=version.overall_limit_minor,
        template_version=version.template_version,
        approved_by=actor.user_id,
        reason="Перераспределение лимита",
    )
    await uow.bump_revisions(workspace_id, plan=True)
    await uow.emit(
        workspace_id=workspace_id,
        event_type="BudgetReallocated",
        aggregate_type="budget_version",
        aggregate_id=created.id,
        aggregate_revision=created.version,
        payload={
            "period_id": str(period_id),
            "from": str(from_stable_line_id),
            "to": str(to_stable_line_id),
            "amount_minor": amount_minor,
        },
        actor_user_id=actor.user_id,
    )
    return created
