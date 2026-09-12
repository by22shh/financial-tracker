"""Планировщик календаря и сроков (ADR-07, ADR-13).

Хранит следующую нужную границу как срок Job, а не создаёт cron-запись
на каждого участника.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import signal

from sqlalchemy import select

from fintracker.application.platform import queue
from fintracker.config import Settings
from fintracker.core.context import WorkspaceState
from fintracker.core.logging import get_logger
from fintracker.db.models.access import Workspace
from fintracker.db.session import RuntimeRole, session_scope

logger = get_logger("runtime.scheduler")

TICK_SECONDS = 30.0


async def schedule_tick(settings: Settings) -> int:
    """Поставить задачи открытия периодов и обслуживания."""
    scheduled = 0
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        workspaces = (
            await session.execute(
                select(Workspace.id, Workspace.timezone).where(
                    Workspace.state == WorkspaceState.ACTIVE.value,
                    Workspace.quarantined.is_(False),
                )
            )
        ).all()
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

    async with session_scope(settings, RuntimeRole.WORKER) as session:
        today_utc = dt.datetime.now(dt.UTC).date()
        await queue.enqueue(
            session,
            job_type="retention_sweep",
            logical_key=f"retention:{today_utc.isoformat()}",
            queue_class="maintenance",
            payload={"schema_version": 1},
            correlation_id=f"retention-{today_utc.isoformat()}",
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
