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

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.platform.queue import LeasedJob
from fintracker.config import Settings
from fintracker.core.logging import get_logger
from fintracker.db.models.platform import (
    Attachment,
    ExportFile,
    InboundPayload,
    NotificationDelivery,
)
from fintracker.db.session import RuntimeRole, session_scope

logger = get_logger("maintenance.retention")


async def sweep_inbound_payloads(session: AsyncSession, now: dt.datetime) -> int:
    """Сырой текст и детальный разбор удаляются по сроку (RET-08)."""
    result = await session.execute(
        delete(InboundPayload)
        .where(InboundPayload.delete_after <= now)
        .returning(InboundPayload.inbound_event_id)
    )
    return len(result.scalars().all())


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
    """Довести удаление вложений до конца; повтор безопасен (ADR-11, AR-27).

    Незавершённые попытки удаления и зависшие staging подхватываются
    следующим проходом: сбой хранилища не оставляет файл навсегда.
    """
    await session.execute(
        update(Attachment)
        .where(Attachment.delete_after <= now, Attachment.state == "ready")
        .values(state="deleting")
    )
    await session.execute(
        update(Attachment)
        .where(
            Attachment.state == "staging",
            Attachment.created_at <= now - STAGING_MAX_AGE,
        )
        .values(state="deleting")
    )
    pending = (
        await session.execute(
            select(Attachment.id, Attachment.storage_key).where(Attachment.state == "deleting")
        )
    ).all()
    if not pending:
        return 0
    from fintracker.infra.storage import build_storage

    storage = build_storage(settings.storage)
    removed = 0
    for attachment_id, storage_key in pending:
        try:
            await storage.delete(storage_key)
        except Exception as exc:  # сбой хранилища не прерывает весь проход
            logger.warning(
                "attachment_delete_failed",
                attachment_id=str(attachment_id),
                error=str(exc)[:200],
            )
            continue
        await session.execute(
            update(Attachment).where(Attachment.id == attachment_id).values(state="deleted")
        )
        removed += 1
    return removed


async def sweep_staging_attachments(session: AsyncSession, now: dt.datetime) -> int:
    """Непривязанные staging объекты очищаются через 24 часа (RET-10)."""
    cutoff = now - dt.timedelta(hours=24)
    result = await session.execute(
        update(Attachment)
        .where(Attachment.state == "staging", Attachment.created_at <= cutoff)
        .values(state="deleting")
        .returning(Attachment.id)
    )
    return len(result.scalars().all())


async def sweep_exports(session: AsyncSession, now: dt.datetime) -> int:
    """Файлы экспорта живут 24 часа в серверном хранилище (RET-07)."""
    result = await session.execute(
        update(ExportFile)
        .where(ExportFile.delete_after <= now, ExportFile.state == "ready")
        .values(state="deleted", storage_key=None)
        .returning(ExportFile.id)
    )
    return len(result.scalars().all())


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


async def handle_retention_sweep(settings: Settings, job: LeasedJob) -> None:
    """Периодическая очистка по срокам хранения."""
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        now = (await session.execute(text("SELECT now()"))).scalar_one()
        expired, cleared = await sweep_private_drafts(session, now)
        stats = {
            "inbound_payloads": await sweep_inbound_payloads(session, now),
            "draft_sources": cleared,
            "expired_drafts": expired,
            "staging_attachments": await sweep_staging_attachments(session, now),
            "exports": await sweep_exports(session, now),
            "stale_deliveries": await sweep_stale_deliveries(session, now),
        }
        stats["attachments"] = await sweep_attachments(session, settings, now)
        stats["purged_workspaces"] = await purge_deleted_workspaces(session, now)
    if any(stats.values()):
        logger.info("retention_sweep", **stats)


async def purge_deleted_workspaces(session: AsyncSession, now: dt.datetime) -> int:
    """Очистить финансовые данные удалённых бюджетов по сроку (ТЗ §24, AUD-15).

    Удаление выполняется узкой служебной функцией: обычная runtime роль не
    получает права произвольного удаления журнала (SEC-02). Запись об удалении
    сохраняется как tombstone для безопасного восстановления.
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
    for record in due:
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
        )
    return purged
