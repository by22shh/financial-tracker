"""Календарь периодического анализа бюджета (FR-73, CMD-25, AUD-14).

Расписание принадлежит бюджету, а не участнику: на бюджет и период создаётся
один логический запуск анализа. Персональная адресация, тихие часы и
отключённые уведомления применяются позже, при раскрытии outbox.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.planning.periods import period_for_date
from fintracker.application.platform.queue import LeasedJob
from fintracker.config import Settings
from fintracker.core.context import WorkspaceState
from fintracker.core.errors import NotFound
from fintracker.core.logging import get_logger
from fintracker.db.models.access import Workspace
from fintracker.db.models.intelligence import AnalysisPreference, AnalysisRun
from fintracker.db.session import RuntimeRole, session_scope

logger = get_logger("intelligence.schedule")

# Обзор подготовки плана запускается за несколько дней до конца периода.
PLAN_LEAD_DAYS = 5
# Обзор закрытия запускается в первые дни нового периода по прошлому периоду.
CLOSING_GRACE_DAYS = 3


@dataclass(frozen=True, slots=True)
class ScheduledAnalysis:
    """Наступивший по календарю запуск анализа."""

    run_kind: str
    logical_key: str
    analysis_date: dt.date


def weekly_key(workspace_id: uuid.UUID, day: dt.date) -> str:
    """Ключ недельного обзора: одна неделя бюджета — один запуск.

    Ключ привязан к ISO-неделе, поэтому перенос дня или часа внутри недели
    не создаёт второй обзор, а со следующей недели действует новое расписание.
    """
    iso = day.isocalendar()
    return f"analysis:weekly:{workspace_id}:{iso.year}-W{iso.week:02d}"


# Доменные значения по умолчанию: воскресный обзор в 19:00 по времени бюджета.
DEFAULT_WEEKLY_ENABLED = True
DEFAULT_WEEKLY_WEEKDAY = 6
DEFAULT_WEEKLY_HOUR = 19
DEFAULT_PLAN_PREPARATION_ENABLED = True
DEFAULT_CLOSING_ENABLED = True


@dataclass(frozen=True, slots=True)
class AnalysisSchedule:
    """Действующее расписание анализа бюджета (FR-73, CMD-25, R-06).

    Значения по умолчанию заданы в домене: отсутствие строки настроек не
    отключает анализ, а несохранённый ORM-объект не приносит None вместо
    серверных значений.
    """

    weekly_enabled: bool = DEFAULT_WEEKLY_ENABLED
    weekly_weekday: int = DEFAULT_WEEKLY_WEEKDAY
    weekly_hour: int = DEFAULT_WEEKLY_HOUR
    plan_preparation_enabled: bool = DEFAULT_PLAN_PREPARATION_ENABLED
    closing_enabled: bool = DEFAULT_CLOSING_ENABLED

    @classmethod
    def from_row(cls, row: AnalysisPreference | None) -> AnalysisSchedule:
        if row is None:
            return cls()
        return cls(
            weekly_enabled=cls.weekly_enabled if row.weekly_enabled is None else row.weekly_enabled,
            weekly_weekday=DEFAULT_WEEKLY_WEEKDAY
            if row.weekly_weekday is None
            else int(row.weekly_weekday),
            weekly_hour=DEFAULT_WEEKLY_HOUR if row.weekly_hour is None else int(row.weekly_hour),
            plan_preparation_enabled=DEFAULT_PLAN_PREPARATION_ENABLED
            if row.plan_preparation_enabled is None
            else row.plan_preparation_enabled,
            closing_enabled=DEFAULT_CLOSING_ENABLED
            if row.closing_enabled is None
            else row.closing_enabled,
        )

    def weekly_due(self, local_now: dt.datetime) -> bool:
        if not self.weekly_enabled:
            return False
        if local_now.weekday() != self.weekly_weekday:
            return False
        return local_now.hour >= self.weekly_hour


async def ensure_analysis_preference(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> AnalysisPreference:
    """Сохранить расписание анализа бюджета при его создании (FR-73, R-06)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    await session.execute(
        pg_insert(AnalysisPreference)
        .values(
            workspace_id=workspace_id,
            weekly_enabled=DEFAULT_WEEKLY_ENABLED,
            weekly_weekday=DEFAULT_WEEKLY_WEEKDAY,
            weekly_hour=DEFAULT_WEEKLY_HOUR,
            plan_preparation_enabled=DEFAULT_PLAN_PREPARATION_ENABLED,
            closing_enabled=DEFAULT_CLOSING_ENABLED,
            muted_directions=[],
        )
        .on_conflict_do_nothing(index_elements=[AnalysisPreference.workspace_id])
    )
    await session.flush()
    return (
        await session.execute(
            select(AnalysisPreference).where(AnalysisPreference.workspace_id == workspace_id)
        )
    ).scalar_one()


async def due_analyses(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    local_now: dt.datetime,
) -> list[ScheduledAnalysis]:
    """Что наступило к этому моменту по календарю бюджета (FR-73)."""
    row = (
        await session.execute(
            select(AnalysisPreference).where(AnalysisPreference.workspace_id == workspace_id)
        )
    ).scalar_one_or_none()
    schedule = AnalysisSchedule.from_row(row)

    today = local_now.date()
    period = await period_for_date(session, workspace_id=workspace_id, day=today)
    due: list[ScheduledAnalysis] = []

    if schedule.weekly_due(local_now):
        due.append(
            ScheduledAnalysis(
                run_kind="weekly_review",
                logical_key=weekly_key(workspace_id, today),
                analysis_date=today,
            )
        )

    if schedule.plan_preparation_enabled and 0 <= (period.end_exclusive - today).days <= (
        PLAN_LEAD_DAYS
    ):
        due.append(
            ScheduledAnalysis(
                run_kind="plan_preparation",
                logical_key=f"analysis:plan:{workspace_id}:{period.id}",
                analysis_date=today,
            )
        )

    if schedule.closing_enabled and 0 <= (today - period.start_date).days < CLOSING_GRACE_DAYS:
        # Закрытие относится к прошлому периоду: анализируется его дата.
        closed_day = period.start_date - dt.timedelta(days=1)
        closed = await period_for_date(session, workspace_id=workspace_id, day=closed_day)
        due.append(
            ScheduledAnalysis(
                run_kind="period_closing",
                logical_key=f"analysis:closing:{workspace_id}:{closed.id}",
                analysis_date=closed_day,
            )
        )
    return due


async def enqueue_scheduled_analysis(settings: Settings, *, workspace_id: uuid.UUID) -> int:
    """Поставить задачи анализа, наступившие по календарю бюджета (AUD-14).

    Уже выполненный логический запуск повторно не ставится: одинаковый
    AI-запрос не отправляется второй раз.
    """
    created = 0
    async with session_scope(settings, RuntimeRole.WORKER, workspace_id=workspace_id) as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one_or_none()
        if workspace is None or workspace.state != WorkspaceState.ACTIVE.value:
            return 0
        if workspace.quarantined:
            # Бюджет на карантине не отправляет фоновых финансовых сообщений.
            return 0
        local_now = dt.datetime.now(ZoneInfo(workspace.timezone))
        pending = await due_analyses(session, workspace_id=workspace_id, local_now=local_now)
        if not pending:
            return 0
        done = set(
            (
                await session.scalars(
                    select(AnalysisRun.logical_key).where(
                        AnalysisRun.workspace_id == workspace_id,
                        AnalysisRun.logical_key.in_([item.logical_key for item in pending]),
                    )
                )
            ).all()
        )
        from fintracker.application.platform import queue

        for item in pending:
            if item.logical_key in done:
                continue
            job_id = await queue.enqueue(
                session,
                job_type="run_analysis",
                logical_key=item.logical_key,
                queue_class="review",
                workspace_id=workspace_id,
                payload={
                    "run_kind": item.run_kind,
                    "analysis_key": item.logical_key,
                    "analysis_date": item.analysis_date.isoformat(),
                    "schema_version": 1,
                },
                correlation_id=f"analysis-{item.analysis_date.isoformat()}",
            )
            if job_id is not None:
                created += 1
    return created


async def handle_run_analysis(settings: Settings, job: LeasedJob) -> None:
    """Задача периодического анализа: один запуск на бюджет и период (AUD-14).

    Модель вызывается вне транзакции базы: короткие транзакции подготовки и
    сохранения разделены ожиданием провайдера (ADR-04, R-07). Доставка
    участникам идёт обычным путём outbox → персональные доставки, где
    применяются тихие часы и личные отключения.
    """
    from fintracker.application.intelligence.analysis import run_analysis
    from fintracker.application.platform import queue
    from fintracker.db.uow import UnitOfWork

    workspace_id = job.workspace_id
    if workspace_id is None:
        raise NotFound("У задачи анализа нет бюджета")
    run_kind = str(job.payload.get("run_kind") or "weekly_review")
    logical_key = str(job.payload["analysis_key"])
    analysis_date = dt.date.fromisoformat(str(job.payload["analysis_date"]))

    async with session_scope(settings, RuntimeRole.WORKER, workspace_id=workspace_id) as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one_or_none()
        if workspace is None or workspace.state != WorkspaceState.ACTIVE.value:
            return
        if workspace.quarantined:
            # Незавершённое изменение доступа не рассылает финансовые обзоры.
            return

    outcome = await run_analysis(
        settings,
        workspace_id=workspace_id,
        run_kind=run_kind,
        logical_key=logical_key,
        today=analysis_date,
        correlation_id=job.correlation_id,
    )
    if outcome.status not in {"succeeded", "fallback"}:
        # Без новых подходящих данных повторный обзор не рассылается (A129).
        logger.info("analysis_not_delivered", workspace_id=str(workspace_id), status=outcome.status)
        return

    async with session_scope(settings, RuntimeRole.WORKER, workspace_id=workspace_id) as session:
        if not outcome.recommendations and outcome.summary:
            # Числовая сводка без карточек тоже доходит до участников: сбой
            # генерации не отменяет отчёт за период (AI-08, A138).
            uow = UnitOfWork(session=session, correlation_id=job.correlation_id)
            await uow.emit(
                workspace_id=workspace_id,
                event_type="AnalysisCompleted",
                aggregate_type="analysis_run",
                aggregate_id=outcome.run_id,
                payload={"text": outcome.summary, "run_id": str(outcome.run_id), "cards": 0},
            )
        await queue.enqueue(
            session,
            job_type="expand_outbox",
            logical_key=f"expand:analysis:{outcome.run_id}",
            queue_class="interactive",
            workspace_id=workspace_id,
            payload={"batch": 50, "schema_version": 1},
            correlation_id=job.correlation_id,
        )
    logger.info(
        "analysis_completed",
        workspace_id=str(workspace_id),
        run_kind=run_kind,
        status=outcome.status,
        cards=len(outcome.recommendations),
    )
