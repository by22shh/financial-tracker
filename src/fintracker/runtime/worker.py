"""Фоновый исполнитель задач (ADR-05, ADR-12).

Отдельные классы задач и справедливый выбор бюджетов не позволяют одному
большому импорту занять всю обработку.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from fintracker.application.platform import queue
from fintracker.config import Settings
from fintracker.core.errors import DomainError
from fintracker.core.fencing import execution_fence
from fintracker.core.logging import get_logger

logger = get_logger("runtime.worker")

# Классы задач и предельная одновременность на процесс (ADR-12).
QUEUE_PLAN: tuple[tuple[tuple[str, ...], int], ...] = (
    (("interactive",), 4),
    (("calendar",), 2),
    (("integration",), 1),
    (("review",), 1),
    (("maintenance",), 1),
)

IDLE_SLEEP_SECONDS = 1.0


class JobHandlerRegistry:
    """Сопоставление типа задачи и обработчика."""

    def __init__(self) -> None:
        self._handlers: dict[str, Any] = {}

    def register(self, job_type: str, handler: Any) -> None:
        self._handlers[job_type] = handler

    def get(self, job_type: str) -> Any | None:
        return self._handlers.get(job_type)


def build_registry() -> JobHandlerRegistry:
    from fintracker.application.commitments.reminders import handle_payment_reminders
    from fintracker.application.delivery.dispatch import (
        handle_deliver_notification,
        handle_expand_outbox,
    )
    from fintracker.application.ingestion.process_event import (
        handle_deliver_reply,
        handle_process_inbound_event,
    )
    from fintracker.application.intelligence.schedule import handle_run_analysis
    from fintracker.application.maintenance.retention import handle_retention_sweep
    from fintracker.application.planning.rollover import (
        handle_open_next_period,
        handle_plan_review,
    )

    registry = JobHandlerRegistry()
    registry.register("process_inbound_event", handle_process_inbound_event)
    registry.register("expand_outbox", handle_expand_outbox)
    registry.register("deliver_notification", handle_deliver_notification)
    registry.register("open_next_period", handle_open_next_period)
    registry.register("retention_sweep", handle_retention_sweep)
    registry.register("payment_reminders", handle_payment_reminders)
    registry.register("plan_review", handle_plan_review)
    registry.register("deliver_reply", handle_deliver_reply)
    registry.register("run_analysis", handle_run_analysis)
    return registry


async def _run_with_lease(
    settings: Settings, job: queue.LeasedJob, registry: JobHandlerRegistry
) -> None:
    handler = registry.get(job.job_type)
    if handler is None:
        await queue.fail(
            settings, job, error=f"Неизвестный тип задачи {job.job_type}", permanent=True
        )
        return

    renew_task = asyncio.create_task(_renew_periodically(settings, job))
    try:
        # Право на результат действует на всё выполнение обработчика: команда
        # с потерянной арендой не фиксирует запись (ADR-05, R-02).
        async with execution_fence(queue.lease_fence(job)):
            await handler(settings, job)
    except DomainError as exc:
        state = await queue.fail(
            settings,
            job,
            error=f"{exc.code.value}: {exc.message}",
            permanent=not exc.retryable,
            retry_after=exc.retry_after,
        )
        logger.warning("job_failed", job_type=job.job_type, state=state, code=exc.code.value)
        return
    except Exception as exc:
        state = await queue.fail(settings, job, error=f"{type(exc).__name__}")
        logger.error("job_error", job_type=job.job_type, state=state, error=type(exc).__name__)
        return
    finally:
        renew_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renew_task
    await queue.complete(settings, job)


async def _renew_periodically(settings: Settings, job: queue.LeasedJob) -> None:
    interval = settings.limits.job_lease_renew_seconds
    while True:
        await asyncio.sleep(interval)
        if not await queue.renew_lease(settings, job):
            logger.warning("lease_lost", job_id=str(job.id), job_type=job.job_type)
            return


async def run_worker(settings: Settings, *, stop_event: asyncio.Event | None = None) -> None:
    registry = build_registry()
    stop = stop_event or asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    from fintracker.application.identity.security_change import reconcile_access_on_start

    # Очереди не обрабатываются, пока доступ не сверен с журналом (R-10).
    await reconcile_access_on_start(settings)
    logger.info("worker_started", queues=[classes for classes, _ in QUEUE_PLAN])
    while not stop.is_set():
        did_work = False
        for classes, concurrency in QUEUE_PLAN:
            jobs = await queue.claim_jobs(settings, queue_classes=classes, limit=concurrency)
            if jobs:
                did_work = True
                await asyncio.gather(*(_run_with_lease(settings, job, registry) for job in jobs))
        if not did_work:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=IDLE_SLEEP_SECONDS)
    # При завершении процесс перестаёт брать задания; остаток вернётся по
    # истечении аренды (ADR-13).
    logger.info("worker_stopped")
