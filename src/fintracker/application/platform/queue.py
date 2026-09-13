"""Очередь задач в PostgreSQL: аренда, продление и fencing (ADR-05, TECH-06).

Исполнитель в короткой транзакции выбирает готовые строки через
FOR UPDATE SKIP LOCKED, присваивает случайный token аренды и освобождает
соединение. Работа во внешней системе идёт вне транзакции; результат
принимается только при совпадении действующего lease_token.
"""

from __future__ import annotations

import datetime as dt
import random
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.config import Settings
from fintracker.core.logging import get_logger
from fintracker.db.models.platform import Job
from fintracker.db.session import RuntimeRole, session_scope

logger = get_logger("platform.queue")

# Начальный backoff по ADR-05: 2, 10, 30, 120, 600 секунд с jitter ±20%.
RETRY_DELAYS_SECONDS: tuple[int, ...] = (2, 10, 30, 120, 600)
JITTER_RATIO = 0.2


@dataclass(frozen=True, slots=True)
class LeasedJob:
    id: uuid.UUID
    job_type: str
    queue_class: str
    workspace_id: uuid.UUID | None
    subject_id: uuid.UUID | None
    payload: dict[str, Any]
    payload_version: int
    attempts: int
    max_attempts: int
    lease_token: uuid.UUID
    lease_until: dt.datetime
    deadline_at: dt.datetime | None
    correlation_id: str
    logical_key: str


def next_delay(attempts: int, *, retry_after: float | None = None) -> float:
    """Задержка следующей попытки с учётом retry_after провайдера (A101).

    Указанная провайдером задержка является нижней границей: разброс только
    добавляется к ней, иначе повтор пришёл бы раньше разрешённого времени.
    """
    index = min(max(attempts - 1, 0), len(RETRY_DELAYS_SECONDS) - 1)
    base = float(RETRY_DELAYS_SECONDS[index])
    jitter = base * JITTER_RATIO
    if retry_after is not None:
        floor = max(base, float(retry_after))
        return floor + random.uniform(0.0, floor * JITTER_RATIO)  # noqa: S311
    return max(1.0, base + random.uniform(-jitter, jitter))  # noqa: S311


async def enqueue(
    session: AsyncSession,
    *,
    job_type: str,
    logical_key: str,
    queue_class: str = "interactive",
    workspace_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    available_at: dt.datetime | None = None,
    deadline_at: dt.datetime | None = None,
    correlation_id: str = "",
    max_attempts: int = 6,
) -> uuid.UUID | None:
    """Поставить задачу; повтор логического ключа не создаёт вторую.

    Возвращает ID новой задачи либо None, если такая уже существует.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    job_id = uuid.uuid4()
    values: dict[str, Any] = {
        "id": job_id,
        "queue_class": queue_class,
        "job_type": job_type,
        "workspace_id": workspace_id,
        "subject_id": subject_id,
        "logical_key": logical_key,
        "payload": payload or {},
        "max_attempts": max_attempts,
        "correlation_id": correlation_id,
        "deadline_at": deadline_at,
    }
    if available_at is not None:
        values["available_at"] = available_at
    statement = (
        pg_insert(Job)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[Job.logical_key])
        .returning(Job.id)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def claim_jobs(
    settings: Settings,
    *,
    queue_classes: tuple[str, ...],
    limit: int = 1,
) -> list[LeasedJob]:
    """Захватить готовые задачи короткой транзакцией (SKIP LOCKED)."""
    lease_seconds = settings.limits.job_lease_seconds
    claimed: list[LeasedJob] = []
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        now = (await session.execute(text("SELECT now()"))).scalar_one()
        # Просроченная running возвращается в доступные после проверки аренды.
        await session.execute(
            update(Job)
            .where(Job.state == "running", Job.lease_until.is_not(None), Job.lease_until < now)
            .values(state="retry_wait", lease_token=None, lease_until=None)
        )
        rows = (
            (
                await session.execute(
                    select(Job)
                    .where(
                        Job.queue_class.in_(queue_classes),
                        Job.state.in_(("queued", "retry_wait")),
                        Job.available_at <= now,
                        or_(Job.deadline_at.is_(None), Job.deadline_at > now),
                    )
                    .order_by(Job.available_at, Job.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            token = uuid.uuid4()
            lease_until = now + dt.timedelta(seconds=lease_seconds)
            row.state = "running"
            row.lease_token = token
            row.lease_until = lease_until
            row.attempts += 1
            claimed.append(
                LeasedJob(
                    id=row.id,
                    job_type=row.job_type,
                    queue_class=row.queue_class,
                    workspace_id=row.workspace_id,
                    subject_id=row.subject_id,
                    payload=dict(row.payload),
                    payload_version=row.payload_version,
                    attempts=row.attempts,
                    max_attempts=row.max_attempts,
                    lease_token=token,
                    lease_until=lease_until,
                    deadline_at=row.deadline_at,
                    correlation_id=row.correlation_id,
                    logical_key=row.logical_key,
                )
            )
    return claimed


async def renew_lease(settings: Settings, job: LeasedJob) -> bool:
    """Продлить аренду; False означает, что задача переарендована (AR-04)."""
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        now = (await session.execute(text("SELECT now()"))).scalar_one()
        renewed = (
            await session.execute(
                update(Job)
                .where(
                    Job.id == job.id,
                    Job.lease_token == job.lease_token,
                    Job.state == "running",
                )
                .values(lease_until=now + dt.timedelta(seconds=settings.limits.job_lease_seconds))
                .returning(Job.id)
            )
        ).scalar_one_or_none()
        return renewed is not None


async def lease_is_valid(session: AsyncSession, job: LeasedJob) -> bool:
    """Проверка действующего token перед сохранением результата (ADR-05)."""
    row = (
        await session.execute(
            select(Job.lease_token, Job.state).where(Job.id == job.id).with_for_update()
        )
    ).one_or_none()
    if row is None:
        return False
    return bool(row.lease_token == job.lease_token and row.state == "running")


async def complete(settings: Settings, job: LeasedJob) -> bool:
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        completed = (
            await session.execute(
                update(Job)
                .where(Job.id == job.id, Job.lease_token == job.lease_token)
                .values(state="succeeded", lease_token=None, lease_until=None, last_error=None)
                .returning(Job.id)
            )
        ).scalar_one_or_none()
        return completed is not None


async def fail(
    settings: Settings,
    job: LeasedJob,
    *,
    error: str,
    permanent: bool = False,
    retry_after: float | None = None,
) -> str:
    """Записать неуспех; постоянная ошибка не повторяется автоматически."""
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        now = (await session.execute(text("SELECT now()"))).scalar_one()
        exhausted = job.attempts >= job.max_attempts
        expired = job.deadline_at is not None and job.deadline_at <= now
        if permanent or exhausted or expired:
            state = "failed"
            values: dict[str, Any] = {
                "state": state,
                "lease_token": None,
                "lease_until": None,
                "last_error": error[:500],
            }
        else:
            state = "retry_wait"
            values = {
                "state": state,
                "lease_token": None,
                "lease_until": None,
                "last_error": error[:500],
                "available_at": now
                + dt.timedelta(seconds=next_delay(job.attempts, retry_after=retry_after)),
            }
        await session.execute(
            update(Job).where(Job.id == job.id, Job.lease_token == job.lease_token).values(**values)
        )
        return state


async def queue_depth(settings: Settings) -> dict[str, int]:
    """Глубина и возраст очереди для мониторинга (NFR-13)."""
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT state, count(*) AS total FROM jobs "
                    "WHERE state IN ('queued','retry_wait','running','failed') GROUP BY state"
                )
            )
        ).all()
        return {row.state: row.total for row in rows}


async def oldest_pending_age_seconds(settings: Settings) -> float:
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        value = (
            await session.execute(
                text(
                    "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - MIN(available_at))), 0) "
                    "FROM jobs WHERE state IN ('queued','retry_wait')"
                )
            )
        ).scalar_one()
        return float(value)
