"""Обработка сохранённого входящего события (TECH-01, TECH-02, ADR-05).

Обработчик идемпотентен: повторная доставка одного update не создаёт вторую
финансовую запись, а результат не принимается при истёкшей аренде.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.conversation.service import handle
from fintracker.application.conversation.types import (
    Attachment,
    IncomingMessage,
    MessageKind,
    Reply,
)
from fintracker.application.platform import queue
from fintracker.application.platform.queue import LeasedJob
from fintracker.config import Settings
from fintracker.core.errors import DomainError, NotFound
from fintracker.core.logging import get_logger
from fintracker.db.models.platform import InboundEvent, InboundPayload
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.infra.telegram.sender import build_sender

logger = get_logger("ingestion.process")


def build_incoming(
    payload: dict[str, Any],
    *,
    event: InboundEvent,
    invite_code: str | None,
) -> IncomingMessage | None:
    """Преобразовать сохранённый update в нормализованное сообщение."""
    update_body = payload.get("update") or {}
    for key in ("message", "edited_message"):
        body = update_body.get(key)
        if isinstance(body, dict):
            return _from_message(body, event=event, invite_code=invite_code)
    callback = update_body.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message") or {}
        return IncomingMessage(
            telegram_user_id=int((callback.get("from") or {}).get("id", 0)),
            chat_id=int((message.get("chat") or {}).get("id", 0)),
            kind=MessageKind.CALLBACK,
            callback_data=callback.get("data"),
            message_id=message.get("message_id"),
            workspace_id=event.workspace_id,
            inbound_event_id=event.id,
            correlation_id=event.correlation_id,
            received_at=event.received_at,
        )
    return None


def _from_message(
    body: dict[str, Any], *, event: InboundEvent, invite_code: str | None
) -> IncomingMessage:
    attachments: list[Attachment] = []
    kind = MessageKind.TEXT
    text = body.get("text") or body.get("caption")

    if invite_code is not None:
        # Восстанавливаем команду входа без раскрытия секрета в payload.
        text = f"/join {invite_code}"

    if "voice" in body or "audio" in body:
        media = body.get("voice") or body["audio"]
        kind = MessageKind.VOICE
        attachments.append(
            Attachment(
                file_id=str(media.get("file_id")),
                kind="voice",
                size_bytes=media.get("file_size"),
                mime_type=media.get("mime_type"),
                duration_seconds=media.get("duration"),
            )
        )
    elif body.get("photo"):
        # Telegram присылает размеры по возрастанию: берём наибольший.
        largest = max(body["photo"], key=lambda item: item.get("file_size") or 0)
        kind = MessageKind.PHOTO
        attachments.append(
            Attachment(
                file_id=str(largest.get("file_id")),
                kind="photo",
                size_bytes=largest.get("file_size"),
                mime_type="image/jpeg",
                width=largest.get("width"),
                height=largest.get("height"),
            )
        )
    elif "document" in body:
        document = body["document"]
        kind = MessageKind.DOCUMENT
        attachments.append(
            Attachment(
                file_id=str(document.get("file_id")),
                kind="document",
                size_bytes=document.get("file_size"),
                mime_type=document.get("mime_type"),
            )
        )

    reply_to = (body.get("reply_to_message") or {}).get("message_id")
    return IncomingMessage(
        telegram_user_id=int((body.get("from") or {}).get("id", 0)),
        chat_id=int((body.get("chat") or {}).get("id", 0)),
        kind=kind,
        text=text,
        message_id=body.get("message_id"),
        reply_to_message_id=reply_to,
        media_group_id=body.get("media_group_id"),
        attachments=tuple(attachments),
        workspace_id=event.workspace_id,
        inbound_event_id=event.id,
        correlation_id=event.correlation_id,
        received_at=event.received_at,
    )


async def _load_event(
    session: AsyncSession, event_id: uuid.UUID
) -> tuple[InboundEvent, dict[str, Any]]:
    event = (
        await session.execute(select(InboundEvent).where(InboundEvent.id == event_id))
    ).scalar_one_or_none()
    if event is None:
        raise NotFound("Входящее событие не найдено")
    payload_row = (
        await session.execute(
            select(InboundPayload).where(InboundPayload.inbound_event_id == event_id)
        )
    ).scalar_one_or_none()
    return event, dict(payload_row.payload) if payload_row else {}


async def handle_process_inbound_event(settings: Settings, job: LeasedJob) -> None:
    """Разобрать событие и отправить ответы автору."""
    event_id = uuid.UUID(str(job.payload["inbound_event_id"]))
    invite_code = job.payload.get("invite_code")

    async with session_scope(settings, RuntimeRole.WORKER) as session:
        event, payload = await _load_event(session, event_id)
        if event.state in {"processed", "ignored"}:
            # Повторная доставка не выполняет обработку второй раз (TECH-02).
            logger.info("inbound_already_processed", event_id=str(event_id))
            return
        event.state = "routed"
        message = build_incoming(payload, event=event, invite_code=invite_code)
        chat_id = event.chat_id

    if message is None:
        async with session_scope(settings, RuntimeRole.WORKER) as session:
            await session.execute(
                update(InboundEvent).where(InboundEvent.id == event_id).values(state="ignored")
            )
        return

    try:
        replies = await handle(settings, message)
    except DomainError as exc:
        # Ошибка домена превращается в понятный текст без раскрытия деталей.
        logger.info("inbound_domain_error", code=exc.code.value, event_id=str(event_id))
        replies = [Reply(text=exc.message)]

    sender = build_sender(settings)
    sent_any = False
    for reply in replies:
        if chat_id is None:
            break
        result = await sender.send_message(
            chat_id=chat_id, text=reply.text, buttons=reply.keyboard()
        )
        sent_any = sent_any or result.ok
        if not result.ok and not result.blocked:
            logger.warning("reply_send_failed", event_id=str(event_id), error=result.error)

    async with session_scope(settings, RuntimeRole.WORKER) as session:
        # Результат принимается только при действующей аренде (ADR-05).
        if not await queue.lease_is_valid(session, job):
            logger.warning("lease_expired_skip_result", job_id=str(job.id))
            return
        await session.execute(
            update(InboundEvent).where(InboundEvent.id == event_id).values(state="processed")
        )

    # Раскрытие outbox в персональные доставки идёт отдельной задачей.
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        await queue.enqueue(
            session,
            job_type="expand_outbox",
            logical_key=f"expand:{event_id}",
            queue_class="interactive",
            workspace_id=job.workspace_id,
            payload={"batch": 50, "schema_version": 1},
            correlation_id=job.correlation_id,
        )
    logger.info(
        "inbound_processed",
        event_id=str(event_id),
        replies=len(replies),
        delivered=sent_any,
        processed_at=dt.datetime.now(dt.UTC).isoformat(),
    )
