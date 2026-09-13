"""Напоминания о плановых платежах (FR-45, FR-53, A61).

Событие создаётся заранее, но актуальность проверяется в момент отправки:
платёж, уже отмеченный оплаченным, не напоминается.
"""

from __future__ import annotations

import datetime as dt
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.commitments.schedules import materialize_occurrences
from fintracker.config import Settings
from fintracker.core.context import WorkspaceState
from fintracker.core.logging import get_logger
from fintracker.db.models.access import Workspace
from fintracker.db.models.commitments import Occurrence, ScheduledItem, ScheduleVersion
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork

logger = get_logger("commitments.reminders")

# Состояния, в которых напоминание ещё имеет смысл.
OPEN_STATES = ("planned", "partially_settled")


async def due_reminders(
    session: AsyncSession, *, workspace_id: uuid.UUID, today: dt.date
) -> list[tuple[Occurrence, str, int]]:
    """Экземпляры, для которых наступило время напоминания (FR-45)."""
    rows = (
        await session.execute(
            select(Occurrence, ScheduledItem.name, ScheduleVersion.reminder_days_before)
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
                Occurrence.state.in_(OPEN_STATES),
                ScheduledItem.archived_at.is_(None),
            )
            .order_by(Occurrence.due_date)
        )
    ).all()
    result: list[tuple[Occurrence, str, int]] = []
    for occurrence, name, days_before in rows:
        remind_from = occurrence.due_date - dt.timedelta(days=max(0, days_before))
        if remind_from <= today:
            result.append((occurrence, name, days_before))
    return result


async def enqueue_payment_reminders(settings: Settings, *, workspace_id: uuid.UUID) -> int:
    """Создать события напоминаний на сегодня (FR-45, FR-53).

    Ключ события включает бюджет, экземпляр и локальную дату: повторный
    запуск в тот же день не рассылает второе напоминание.
    """
    created = 0
    async with session_scope(settings, RuntimeRole.WORKER, workspace_id=workspace_id) as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one_or_none()
        if workspace is None or workspace.state != WorkspaceState.ACTIVE.value:
            return 0
        today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
        await materialize_occurrences(
            session, workspace_id=workspace_id, until_date=today + dt.timedelta(days=31)
        )
        pending = await due_reminders(session, workspace_id=workspace_id, today=today)
        if not pending:
            return 0
        uow = UnitOfWork(session=session, correlation_id=f"reminder-{today.isoformat()}")
        for occurrence, _name, _days in pending:
            existing = await _already_reminded(
                session, workspace_id=workspace_id, occurrence_id=occurrence.id, day=today
            )
            if existing:
                continue
            await uow.emit(
                workspace_id=workspace_id,
                event_type="PaymentReminder",
                aggregate_type="occurrence",
                aggregate_id=occurrence.id,
                aggregate_revision=occurrence.version,
                payload={
                    "occurrence_id": str(occurrence.id),
                    "local_date": today.isoformat(),
                    "schema_version": 1,
                },
            )
            created += 1
    return created


async def _already_reminded(
    session: AsyncSession, *, workspace_id: uuid.UUID, occurrence_id: uuid.UUID, day: dt.date
) -> bool:
    from fintracker.db.models.platform import OutboxEvent

    row = (
        await session.execute(
            select(OutboxEvent.id).where(
                OutboxEvent.workspace_id == workspace_id,
                OutboxEvent.event_type == "PaymentReminder",
                OutboxEvent.aggregate_id == occurrence_id,
                OutboxEvent.payload["local_date"].astext == day.isoformat(),
            )
        )
    ).scalar_one_or_none()
    return row is not None


async def handle_payment_reminders(settings: Settings, job: object) -> None:
    """Задача планировщика: напоминания по всем активным бюджетам (FR-45)."""
    workspace_id = getattr(job, "workspace_id", None)
    if workspace_id is None:
        return
    count = await enqueue_payment_reminders(settings, workspace_id=workspace_id)
    if count:
        logger.info("payment_reminders_created", workspace_id=str(workspace_id), count=count)
