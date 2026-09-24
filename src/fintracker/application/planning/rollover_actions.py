"""Explicit, revalidated acceptance of a category remainder (FR-39)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.planning.plan import current_budget_version, period_status
from fintracker.application.planning.rollover import apply_plan_for_period
from fintracker.core.context import ActorContext
from fintracker.core.errors import ConflictError, NotFound, ValidationFailed
from fintracker.db.models.planning import BudgetLine, BudgetPeriod, Rollover
from fintracker.db.uow import UnitOfWork


async def accept_rollover(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    rollover_id: uuid.UUID,
    today: dt.date,
    expected_amount_minor: int,
) -> Rollover:
    workspace_id = actor.require_workspace()
    workspace = await uow.lock_workspace(workspace_id, actor=actor)
    row = await session.scalar(
        select(Rollover).where(Rollover.workspace_id == workspace_id, Rollover.id == rollover_id)
    )
    if row is None:
        raise NotFound("Предложение переноса недоступно")
    if row.amount_minor != expected_amount_minor:
        raise ConflictError("Сумма предложения изменилась. Откройте перенос заново.")
    if row.status == "accepted":
        return row
    if row.status != "proposed":
        raise ConflictError("Предложение устарело. Откройте перенос заново.")
    source = await session.scalar(
        select(BudgetPeriod).where(
            BudgetPeriod.workspace_id == workspace_id, BudgetPeriod.id == row.source_period_id
        )
    )
    destination = await session.scalar(
        select(BudgetPeriod).where(
            BudgetPeriod.workspace_id == workspace_id, BudgetPeriod.id == row.destination_period_id
        )
    )
    if source is None or destination is None or source.end_exclusive != destination.start_date:
        raise ValidationFailed("Периоды переноса изменились. Откройте перенос заново.")
    if today < source.end_exclusive:
        raise ValidationFailed("Остаток можно принять после окончания исходного периода.")
    status = await period_status(
        session,
        workspace_id=workspace_id,
        period_id=source.id,
        currency=workspace.currency,
        today=today,
    )
    line = next((line for line in status.lines if line.stable_line_id == row.stable_line_id), None)
    if line is None or line.remaining_minor != row.amount_minor:
        raise ConflictError("Остаток изменился после предпросмотра. Откройте перенос заново.")
    await apply_plan_for_period(session, uow, workspace_id=workspace_id, period=destination)
    version = await current_budget_version(
        session, workspace_id=workspace_id, period_id=destination.id
    )
    if version is None:
        raise ValidationFailed("Сначала настройте план следующего периода.")
    destination_line = await session.scalar(
        select(BudgetLine).where(
            BudgetLine.workspace_id == workspace_id,
            BudgetLine.budget_version_id == version.id,
            BudgetLine.stable_line_id == row.stable_line_id,
        )
    )
    if destination_line is None:
        raise ValidationFailed("Сначала добавьте эту категорию в план следующего периода.")
    row.status = "accepted"
    row.accepted_by = actor.user_id
    row.accepted_at = dt.datetime.now(dt.UTC)
    await session.flush()
    await uow.bump_revisions(workspace_id, plan=True)
    await uow.emit(
        workspace_id=workspace_id,
        event_type="RolloverAccepted",
        aggregate_type="rollover",
        aggregate_id=row.id,
        actor_user_id=actor.user_id,
        payload={
            "amount_minor": row.amount_minor,
            "source_period_id": str(source.id),
            "destination_period_id": str(destination.id),
        },
    )
    return row
