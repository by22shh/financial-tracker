"""Цели, фонды и резервы (FR-40, FR-49–FR-51, CMD-21).

Резервирование и изменение лимита не создают CashLeg или AccountEntry без
отдельного реального движения (DATA_CONTRACT §2.4).
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.core.context import ActorContext
from fintracker.core.errors import ConflictError, NotFound, ValidationFailed
from fintracker.core.money import Money
from fintracker.db.models.commitments import CashReservation, Goal, GoalMovement
from fintracker.db.uow import UnitOfWork


@dataclass(frozen=True, slots=True)
class GoalProgress:
    """Раздельные показатели прогресса (FR-50)."""

    goal_id: uuid.UUID
    name: str
    currency: str
    target_minor: int | None
    planned_contribution_minor: int | None
    allocated_minor: int
    used_minor: int
    confirmed_on_account_minor: int | None
    remaining_to_target_minor: int | None


def suggested_contribution(
    *, target: Money, already_allocated: Money, remaining_contributions: int
) -> Money:
    """Взнос в фонд = ⌈недостающая сумма / число взносов⌉ (FR-40, B6).

    Последний взнос корректируется до точного итога вызывающим кодом.
    """
    if remaining_contributions <= 0:
        raise ValidationFailed("Число оставшихся взносов должно быть положительным")
    missing = target.minor - already_allocated.minor
    if missing <= 0:
        return Money.zero(target.currency)
    per_contribution = math.ceil(missing / remaining_contributions)
    return Money(min(per_contribution, missing), target.currency)


async def create_goal(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    name: str,
    currency: str,
    target: Money | None = None,
    due_date: dt.date | None = None,
    kind: str = "goal",
    contribution: Money | None = None,
    contribution_frequency: str = "per_period",
    remaining_contributions: int | None = None,
    is_protected: bool = True,
) -> Goal:
    workspace_id = actor.require_workspace()
    if target is not None and target.currency != currency:
        raise ValidationFailed("Валюта цели не совпадает с целевой суммой")
    row = Goal(
        workspace_id=workspace_id,
        name=name.strip()[:120],
        kind=kind,
        currency=currency,
        target_minor=target.minor if target else None,
        due_date=due_date,
        contribution_minor=contribution.minor if contribution else None,
        contribution_frequency=contribution_frequency,
        remaining_contributions=remaining_contributions,
        is_protected=is_protected,
        created_by=actor.user_id,
    )
    session.add(row)
    await session.flush()
    await uow.bump_revisions(workspace_id, plan=True)
    await uow.emit(
        workspace_id=workspace_id,
        event_type="GoalChanged",
        aggregate_type="goal",
        aggregate_id=row.id,
        payload={"goal_id": str(row.id), "change": "created"},
        actor_user_id=actor.user_id,
    )
    return row


async def allocate_to_goal(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    goal_id: uuid.UUID,
    amount: Money,
    effect_id: uuid.UUID | None = None,
    transaction_id: uuid.UUID | None = None,
    period_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> GoalMovement:
    """Выделить деньги на цель (FR-50, A39).

    Выделение не является покупкой: расход равен нулю, банковский остаток
    не меняется.
    """
    workspace_id = actor.require_workspace()
    goal = await _locked_goal(session, workspace_id=workspace_id, goal_id=goal_id)
    if amount.currency != goal.currency:
        raise ValidationFailed("Валюта выделения не совпадает с валютой цели")
    if amount.minor <= 0:
        raise ValidationFailed("Сумма выделения должна быть положительной")

    if effect_id is not None:
        existing = (
            await session.execute(
                select(GoalMovement.id).where(
                    GoalMovement.workspace_id == workspace_id,
                    GoalMovement.goal_id == goal_id,
                    GoalMovement.effect_id == effect_id,
                    GoalMovement.kind == "allocate",
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Один перевод не создаёт два вклада в ту же цель (A40).
            return (
                await session.execute(select(GoalMovement).where(GoalMovement.id == existing))
            ).scalar_one()

    movement = GoalMovement(
        workspace_id=workspace_id,
        goal_id=goal_id,
        kind="allocate",
        change_minor=amount.minor,
        effect_id=effect_id,
        transaction_id=transaction_id,
        period_id=period_id,
        reason=reason,
        created_by=actor.user_id,
    )
    session.add(movement)
    goal.allocated_minor += amount.minor
    goal.version += 1
    if goal.target_minor and goal.allocated_minor >= goal.target_minor:
        goal.status = "reached"
    await _sync_reservation(session, workspace_id=workspace_id, goal=goal)
    await session.flush()
    await uow.bump_revisions(workspace_id, plan=True)
    return movement


async def use_goal(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    goal_id: uuid.UUID,
    amount: Money,
    effect_id: uuid.UUID | None = None,
    transaction_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> GoalMovement:
    """Использовать выделенные средства (FR-51, B6).

    Оплата будущего счёта создаёт расход один раз и использует фонд;
    те же деньги не резервируются второй раз.
    """
    workspace_id = actor.require_workspace()
    goal = await _locked_goal(session, workspace_id=workspace_id, goal_id=goal_id)
    if amount.minor > goal.allocated_minor:
        raise ConflictError(
            "Использование превышает выделенный остаток цели",
            details={"allocated_minor": goal.allocated_minor},
        )
    movement = GoalMovement(
        workspace_id=workspace_id,
        goal_id=goal_id,
        kind="use",
        change_minor=-amount.minor,
        effect_id=effect_id,
        transaction_id=transaction_id,
        reason=reason,
        created_by=actor.user_id,
    )
    session.add(movement)
    goal.allocated_minor -= amount.minor
    goal.version += 1
    await _sync_reservation(session, workspace_id=workspace_id, goal=goal)
    await session.flush()
    await uow.bump_revisions(workspace_id, plan=True)
    return movement


async def release_goal(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    goal_id: uuid.UUID,
    amount: Money,
    reason: str,
) -> GoalMovement:
    """Освободить резерв цели по явному действию (FR-51)."""
    workspace_id = actor.require_workspace()
    goal = await _locked_goal(session, workspace_id=workspace_id, goal_id=goal_id)
    if amount.minor > goal.allocated_minor:
        raise ConflictError("Освобождение превышает выделенный остаток")
    movement = GoalMovement(
        workspace_id=workspace_id,
        goal_id=goal_id,
        kind="release",
        change_minor=-amount.minor,
        reason=reason,
        created_by=actor.user_id,
    )
    session.add(movement)
    goal.allocated_minor -= amount.minor
    goal.version += 1
    await _sync_reservation(session, workspace_id=workspace_id, goal=goal)
    await session.flush()
    await uow.bump_revisions(workspace_id, plan=True)
    return movement


async def _locked_goal(
    session: AsyncSession, *, workspace_id: uuid.UUID, goal_id: uuid.UUID
) -> Goal:
    goal = (
        await session.execute(
            select(Goal)
            .where(Goal.workspace_id == workspace_id, Goal.id == goal_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if goal is None:
        raise NotFound("Цель недоступна")
    return goal


async def _sync_reservation(session: AsyncSession, *, workspace_id: uuid.UUID, goal: Goal) -> None:
    """Одна сумма не вычитается из доступного ресурса дважды (§2.5, B7)."""
    reservation = (
        await session.execute(
            select(CashReservation)
            .where(
                CashReservation.workspace_id == workspace_id,
                CashReservation.goal_id == goal.id,
                CashReservation.is_active.is_(True),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not goal.is_protected or goal.allocated_minor == 0:
        if reservation is not None:
            reservation.is_active = False
            reservation.released_at = dt.datetime.now(dt.UTC)
        return
    if reservation is None:
        session.add(
            CashReservation(
                workspace_id=workspace_id,
                basis_kind="goal",
                goal_id=goal.id,
                amount_minor=goal.allocated_minor,
                account_id=goal.linked_account_id,
                is_active=True,
            )
        )
    else:
        reservation.amount_minor = goal.allocated_minor


async def goal_progress(
    session: AsyncSession, *, workspace_id: uuid.UUID, goal_id: uuid.UUID
) -> GoalProgress:
    """Раздельно: запланировано, выделено, использовано (FR-50, A78)."""
    from sqlalchemy import func

    goal = (
        await session.execute(
            select(Goal).where(Goal.workspace_id == workspace_id, Goal.id == goal_id)
        )
    ).scalar_one_or_none()
    if goal is None:
        raise NotFound("Цель недоступна")
    used = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(-GoalMovement.change_minor), 0)).where(
                    GoalMovement.workspace_id == workspace_id,
                    GoalMovement.goal_id == goal_id,
                    GoalMovement.kind == "use",
                )
            )
        ).scalar_one()
    )
    confirmed: int | None = None
    if goal.linked_account_id is not None:
        from fintracker.application.ledger.service import account_balance

        confirmed = await account_balance(
            session, workspace_id=workspace_id, account_id=goal.linked_account_id
        )
    return GoalProgress(
        goal_id=goal.id,
        name=goal.name,
        currency=goal.currency,
        target_minor=goal.target_minor,
        planned_contribution_minor=goal.contribution_minor,
        allocated_minor=goal.allocated_minor,
        used_minor=used,
        confirmed_on_account_minor=confirmed,
        remaining_to_target_minor=(
            max(0, goal.target_minor - goal.allocated_minor) if goal.target_minor else None
        ),
    )
