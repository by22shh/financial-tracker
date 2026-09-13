"""Планировщик календаря и сроков (ADR-07, ADR-13).

Хранит следующую нужную границу как срок Job, а не создаёт cron-запись
на каждого участника.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import signal

from sqlalchemy import text

from fintracker.application.intelligence.schedule import enqueue_scheduled_analysis
from fintracker.application.platform import queue
from fintracker.config import Settings
from fintracker.core.context import WorkspaceState
from fintracker.core.logging import get_logger
from fintracker.db.session import RuntimeRole, session_scope

logger = get_logger("runtime.scheduler")

TICK_SECONDS = 30.0


async def schedule_tick(settings: Settings) -> int:
    """Поставить задачи открытия периодов и обслуживания."""
    scheduled = 0
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        # У фонового процесса нет пользовательского контекста RLS, поэтому
        # список обслуживаемых бюджетов даёт узкая служебная функция без
        # финансовых данных (ADR-06, AUD-01).
        rows = (
            await session.execute(
                text("SELECT id, timezone, quarantined FROM maintenance_workspaces(:states)"),
                {"states": [WorkspaceState.ACTIVE.value]},
            )
        ).all()
    workspaces = [(row[0], row[1]) for row in rows if not row[2]]
    for workspace_id, timezone in workspaces:
        from zoneinfo import ZoneInfo

        today = dt.datetime.now(ZoneInfo(timezone)).date()
        async with session_scope(
            settings, RuntimeRole.WORKER, workspace_id=workspace_id
        ) as session:
            created = await queue.enqueue(
                session,
                job_type="open_next_period",
                # Один логический запуск на бюджет и локальную дату (A128, AR-24).
                logical_key=f"open_period:{workspace_id}:{today.isoformat()}",
                queue_class="calendar",
                workspace_id=workspace_id,
                payload={"local_date": today.isoformat(), "schema_version": 1},
                correlation_id=f"sched-{today.isoformat()}",
            )
            if created is not None:
                scheduled += 1
            # Напоминания о платежах: один запуск на бюджет и локальную дату (FR-45).
            reminder = await queue.enqueue(
                session,
                job_type="payment_reminders",
                logical_key=f"reminders:{workspace_id}:{today.isoformat()}",
                queue_class="calendar",
                workspace_id=workspace_id,
                payload={"local_date": today.isoformat(), "schema_version": 1},
                correlation_id=f"reminder-{today.isoformat()}",
            )
            if reminder is not None:
                scheduled += 1
        # Периодический анализ по общему календарю бюджета (FR-73, AUD-14).
        scheduled += await enqueue_scheduled_analysis(settings, workspace_id=workspace_id)

    async with session_scope(settings, RuntimeRole.WORKER) as session:
        now_utc = dt.datetime.now(dt.UTC)
        today_utc = now_utc.date()
        await queue.enqueue(
            session,
            job_type="retention_sweep",
            logical_key=f"retention:{today_utc.isoformat()}",
            queue_class="maintenance",
            payload={"schema_version": 1},
            correlation_id=f"retention-{today_utc.isoformat()}",
        )
        # Фоновые события (напоминания, границы периода, анализ) раскрываются
        # в персональные доставки без участия входящих сообщений (TECH-05).
        minute = now_utc.strftime("%Y%m%dT%H%M")
        await queue.enqueue(
            session,
            job_type="expand_outbox",
            logical_key=f"expand:sweep:{minute}",
            queue_class="interactive",
            payload={"batch": 200, "schema_version": 1},
            correlation_id=f"outbox-{minute}",
        )
    return scheduled


async def run_scheduler(settings: Settings, *, stop_event: asyncio.Event | None = None) -> None:
    stop = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    logger.info("scheduler_started")
    while not stop.is_set():
        try:
            count = await schedule_tick(settings)
            if count:
                logger.info("scheduler_tick", scheduled=count)
        except Exception as exc:
            logger.error("scheduler_error", error=type(exc).__name__)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
    logger.info("scheduler_stopped")
