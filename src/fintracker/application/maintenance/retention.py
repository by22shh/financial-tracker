"""Соблюдение сроков хранения и очистка (TZ §24, ADR-11).

Сроки: журнал и ревизии до удаления пространства (RET-01); исходное аудио
до 24 часов (RET-02); нераспознанное вложение до 7 дней (RET-03); фото чеков
30 дней (RET-04); технические логи 30 дней без финансового текста (RET-05);
резервные копии — окно 30 дней (RET-06); файлы экспорта 24 часа (RET-07);
сырой текст и разбор до 7 дней (RET-08); очистка удалённого бюджета в
течение 24 часов (RET-09); staging-объекты через 24 часа (RET-10).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import and_, delete, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.platform.queue import LeasedJob
from fintracker.config import Settings
from fintracker.core.logging import get_logger
from fintracker.db.models.platform import InboundPayload, NotificationDelivery
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.infra.storage import ObjectStorage

logger = get_logger("maintenance.retention")


async def sweep_inbound_payloads(session: AsyncSession, now: dt.datetime) -> int:
    """Сырой текст и детальный разбор удаляются по сроку (RET-08)."""
    result = await session.execute(
        delete(InboundPayload)
        .where(InboundPayload.delete_after <= now)
        .returning(InboundPayload.inbound_event_id)
    )
    return len(result.scalars().all())


async def sweep_author_replies(session: AsyncSession, now: dt.datetime) -> int:
    """Подготовленные ответы автору удаляются по сроку (RET-08, R-04).

    Строка изолирована по бюджету и владельцу, поэтому очистку выполняет узкая
    служебная функция: у фонового процесса нет пользовательского контекста.
    """
    value = (
        await session.execute(text("SELECT maintenance_purge_author_replies(:now)"), {"now": now})
    ).scalar_one()
    return int(value)


async def sweep_private_drafts(session: AsyncSession, now: dt.datetime) -> tuple[int, int]:
    """Истечение черновиков и очистка исходного текста по сроку (RET-02, RET-03).

    Черновик виден только владельцу в его бюджете, поэтому у фонового процесса
    нет подходящего контекста RLS: обслуживание выполняет узкая служебная
    функция с фиксированным поведением (ADR-06, AUD-01).
    """
    row = (
        await session.execute(
            text("SELECT expired, cleared FROM maintenance_expire_drafts(:now)"),
            {"now": now},
        )
    ).one()
    return int(row[0]), int(row[1])


# Незавершённая регистрация вложения не остаётся навсегда: файл уже загружен,
# но строка так и не стала ready (AR-27).
STAGING_MAX_AGE = dt.timedelta(hours=6)


async def sweep_attachments(session: AsyncSession, settings: Settings, now: dt.datetime) -> int:
    """Довести удаление вложений до конца; повтор безопасен (ADR-11, AR-27, R-09).

    Строки вложений защищены RLS и не видны фоновой роли без контекста, поэтому
    перечисление и фиксация состояния идут через узкие служебные функции
    (ADR-06, SEC-02). Незавершённые попытки удаления и зависшие staging
    подхватываются следующим проходом: сбой хранилища не оставляет файл навсегда.
    """
    pending = (
        await session.execute(
            text(
                "SELECT id, storage_key FROM maintenance_due_attachments"
                "(:now, :staging_cutoff, :limit)"
            ),
            {"now": now, "staging_cutoff": now - STAGING_MAX_AGE, "limit": 500},
        )
    ).all()
    if not pending:
        return 0
    from fintracker.infra.storage import build_storage

    storage = build_storage(settings.storage)
    removed = 0
    for attachment_id, storage_key in pending:
        if not await _drop_object(storage, storage_key, attachment_id=attachment_id):
            continue
        await session.execute(
            text("SELECT maintenance_finish_attachment(:id)"), {"id": attachment_id}
        )
        removed += 1
    return removed


async def _drop_object(
    storage: ObjectStorage, storage_key: str | None, *, attachment_id: object
) -> bool:
    """Удалить объект хранилища; сбой не прерывает весь проход."""
    if not storage_key:
        return True
    try:
        await storage.delete(storage_key)
    except Exception as exc:  # сбой хранилища не прерывает весь проход
        logger.warning(
            "attachment_delete_failed",
            attachment_id=str(attachment_id),
            error=str(exc)[:200],
        )
        return False
    return True


async def sweep_exports(session: AsyncSession, settings: Settings, now: dt.datetime) -> int:
    """Файлы экспорта живут 24 часа в серверном хранилище (RET-07, R-09).

    Объект удаляется до очистки ссылки: иначе ключ теряется, а файл остаётся.
    """
    due = (
        await session.execute(
            text("SELECT id, storage_key FROM maintenance_due_exports(:now, :limit)"),
            {"now": now, "limit": 500},
        )
    ).all()
    if not due:
        return 0
    from fintracker.infra.storage import build_storage

    storage = build_storage(settings.storage)
    removed = 0
    for export_id, storage_key in due:
        if not await _drop_object(storage, storage_key, attachment_id=export_id):
            continue
        await session.execute(text("SELECT maintenance_finish_export(:id)"), {"id": export_id})
        removed += 1
    return removed


async def sweep_stale_deliveries(session: AsyncSession, now: dt.datetime) -> int:
    """Просроченные доставки отменяются, а не повторяются бесконечно (ADR-05)."""
    result = await session.execute(
        update(NotificationDelivery)
        .where(
            NotificationDelivery.expires_at.is_not(None),
            NotificationDelivery.expires_at <= now,
            NotificationDelivery.state.in_(("pending", "failed")),
        )
        .values(state="cancelled", last_error="Срок доставки истёк")
        .returning(NotificationDelivery.id)
    )
    return len(result.scalars().all())


# Завершённые задачи — технический журнал без финансового текста (RET-05).
JOB_DONE_RETENTION = dt.timedelta(days=7)
JOB_FAILED_RETENTION = dt.timedelta(days=30)


async def sweep_finished_jobs(session: AsyncSession, now: dt.datetime) -> int:
    """Удалить отработавшие строки очереди по сроку (RET-05).

    Планировщик создаёт периодические задачи с ключом на интервал времени,
    поэтому завершённые строки нужно убирать, иначе таблица растёт без предела.
    """
    from fintracker.db.models.platform import Job

    result = await session.execute(
        delete(Job)
        .where(
            or_(
                and_(
                    Job.state.in_(("succeeded", "cancelled")),
                    Job.updated_at <= now - JOB_DONE_RETENTION,
                ),
                and_(Job.state == "failed", Job.updated_at <= now - JOB_FAILED_RETENTION),
            )
        )
        .returning(Job.id)
    )
    return len(result.scalars().all())


# Резервация AI, брошенная упавшим процессом, освобождается позже своего
# запроса с запасом: живой вызов не должен быть закрыт как брошенный.
RESERVATION_MAX_AGE = dt.timedelta(minutes=30)


async def _sweep_reservations(settings: Settings, now: dt.datetime) -> int:
    from fintracker.application.intelligence import quota

    return await quota.settle_abandoned(settings, now=now, older_than=RESERVATION_MAX_AGE)


async def handle_retention_sweep(settings: Settings, job: LeasedJob) -> None:
    """Периодическая очистка по срокам хранения."""
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        now = (await session.execute(text("SELECT now()"))).scalar_one()
        expired, cleared = await sweep_private_drafts(session, now)
        stats = {
            "inbound_payloads": await sweep_inbound_payloads(session, now),
            "draft_sources": cleared,
            "expired_drafts": expired,
            "author_replies": await sweep_author_replies(session, now),
            "exports": await sweep_exports(session, settings, now),
            "stale_deliveries": await sweep_stale_deliveries(session, now),
            "finished_jobs": await sweep_finished_jobs(session, now),
            "abandoned_reservations": await _sweep_reservations(settings, now),
        }
        stats["attachments"] = await sweep_attachments(session, settings, now)
        stats["purged_workspaces"] = await purge_deleted_workspaces(session, settings, now)
    if any(stats.values()):
        logger.info("retention_sweep", **stats)


async def purge_deleted_workspaces(
    session: AsyncSession, settings: Settings, now: dt.datetime
) -> int:
    """Очистить данные и файлы удалённых бюджетов по сроку (ТЗ §24, AUD-15, R-09).

    Удаление выполняется узкими служебными функциями: обычная runtime роль не
    получает права произвольного удаления журнала (SEC-02). Сначала удаляются
    объекты хранилища, затем строки: покупка файлов не переживает бюджет.
    Запись об удалении сохраняется как tombstone для безопасного восстановления.
    """
    from fintracker.db.models.access import BudgetDeletionRecord

    due = (
        (
            await session.execute(
                select(BudgetDeletionRecord).where(
                    BudgetDeletionRecord.purge_after <= now,
                    BudgetDeletionRecord.purged_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    purged = 0
    from fintracker.infra.storage import build_storage

    storage = build_storage(settings.storage)
    for record in due:
        files = (
            await session.execute(
                text("SELECT id, storage_key, file_kind FROM maintenance_workspace_files(:ws)"),
                {"ws": record.workspace_id},
            )
        ).all()
        blocked = False
        for file_id, storage_key, file_kind in files:
            if await _drop_object(storage, storage_key, attachment_id=file_id):
                if file_kind == "attachment":
                    await session.execute(
                        text("SELECT maintenance_finish_attachment(:id)"), {"id": file_id}
                    )
                continue
            blocked = True
        if blocked:
            # Очистка не подтверждается, пока объекты не удалены: следующий
            # проход повторит попытку.
            logger.warning("workspace_purge_deferred", workspace_id=str(record.workspace_id))
            continue
        removed = (
            await session.execute(
                text("SELECT purge_workspace_data(:workspace)"),
                {"workspace": record.workspace_id},
            )
        ).scalar_one()
        record.purged_at = now
        record.purged_rows = int(removed)
        purged += 1
        logger.info(
            "workspace_purged",
            workspace_id=str(record.workspace_id),
            removed_rows=int(removed),
            removed_files=len(files),
        )
    return purged
