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
    Draft,
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


async def sweep_draft_sources(session: AsyncSession, now: dt.datetime) -> int:
    """Очистить исходный текст и транскрипт завершённых черновиков.

    Структурированная подтверждённая операция и выбранная заметка остаются.
    """
    result = await session.execute(
        update(Draft)
        .where(
            Draft.delete_raw_after <= now,
            (Draft.raw_text.is_not(None)) | (Draft.transcript.is_not(None)),
        )
        .values(raw_text=None, transcript=None)
        .returning(Draft.id)
    )
    return len(result.scalars().all())


async def expire_drafts(session: AsyncSession, now: dt.datetime) -> int:
    """Срок не превращает черновик в подтверждённый расход (FR-20)."""
    result = await session.execute(
        update(Draft)
        .where(
            Draft.expires_at <= now,
            Draft.state.in_(("received", "processing", "needs_clarification", "ready")),
        )
        .values(state="expired")
        .returning(Draft.id)
    )
    return len(result.scalars().all())


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
        stats = {
            "inbound_payloads": await sweep_inbound_payloads(session, now),
            "draft_sources": await sweep_draft_sources(session, now),
            "expired_drafts": await expire_drafts(session, now),
            "staging_attachments": await sweep_staging_attachments(session, now),
            "exports": await sweep_exports(session, now),
            "stale_deliveries": await sweep_stale_deliveries(session, now),
        }
        stats["attachments"] = await sweep_attachments(session, settings, now)
    if any(stats.values()):
        logger.info("retention_sweep", **stats)
