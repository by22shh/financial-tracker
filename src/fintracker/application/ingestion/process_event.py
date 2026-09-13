"""Обработка сохранённого входящего события (TECH-01, TECH-02, ADR-05).

Обработчик идемпотентен: повторная доставка одного update не создаёт вторую
финансовую запись, а результат не принимается при истёкшей аренде.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
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
    invite_digest: str | None = None,
) -> IncomingMessage | None:
    """Преобразовать сохранённый update в нормализованное сообщение."""
    update_body = payload.get("update") or {}
    for key in ("message", "edited_message"):
        body = update_body.get(key)
        if isinstance(body, dict):
            return _from_message(body, event=event, invite_digest=invite_digest)
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
            invite_digest=invite_digest,
            correlation_id=event.correlation_id,
            received_at=event.received_at,
        )
    return None


def _from_message(
    body: dict[str, Any], *, event: InboundEvent, invite_digest: str | None
) -> IncomingMessage:
    attachments: list[Attachment] = []
    kind = MessageKind.TEXT
    text = body.get("text") or body.get("caption")

    if invite_digest is not None:
        # Команда входа восстанавливается без открытого кода: проверка идёт
        # по сохранённому проверочному значению (SEC-04, AUD-16).
        text = "/join"

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
        invite_digest=invite_digest,
        correlation_id=event.correlation_id,
        received_at=event.received_at,
    )


async def _load_event(
    session: AsyncSession, event_id: uuid.UUID
) -> tuple[InboundEvent, dict[str, Any]]:
    """Событие и его защищённый payload под уже установленным контекстом.

    Контекст RLS задаётся вызывающим кодом по закреплённым при приёме
    участнику и бюджету: без него приватный payload не виден (AUD-01).
    """
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


@dataclass(frozen=True, slots=True)
class _EventContext:
    """Закреплённый при приёме контекст события (FR-79)."""

    actor_user_id: uuid.UUID | None
    workspace_id: uuid.UUID | None
    chat_id: int | None
    chat_type: str | None
    state: str

    @property
    def is_private_chat(self) -> bool:
        # Неизвестный тип чата считается непубличным только для личного id.
        if self.chat_type is None:
            return self.chat_id is not None and self.chat_id > 0
        return self.chat_type == "private"


GROUP_HINT = (
    "Финансовый бюджет доступен только в личном чате: откройте бота лично и "
    "повторите команду. В группе бот не показывает суммы и список бюджетов."
)


async def _read_context(settings: Settings, event_id: uuid.UUID) -> _EventContext:
    """Прочитать техническую часть события без приватного payload."""
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        row = (
            await session.execute(
                select(
                    InboundEvent.actor_user_id,
                    InboundEvent.workspace_id,
                    InboundEvent.chat_id,
                    InboundEvent.chat_type,
                    InboundEvent.state,
                ).where(InboundEvent.id == event_id)
            )
        ).one_or_none()
    if row is None:
        raise NotFound("Входящее событие не найдено")
    return _EventContext(
        actor_user_id=row[0],
        workspace_id=row[1],
        chat_id=row[2],
        chat_type=row[3],
        state=row[4],
    )


async def _mark_state(settings: Settings, event_id: uuid.UUID, state: str) -> None:
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        await session.execute(
            update(InboundEvent).where(InboundEvent.id == event_id).values(state=state)
        )


def _reply_payload(event_id: uuid.UUID, chat_id: int, replies: list[Reply]) -> dict[str, Any]:
    return {
        "inbound_event_id": str(event_id),
        "chat_id": chat_id,
        "messages": [
            {"text": reply.text, "buttons": reply.keyboard()} for reply in replies if reply.text
        ],
        "schema_version": 1,
    }


async def _enqueue_reply(
    session: AsyncSession,
    *,
    job: LeasedJob,
    event_id: uuid.UUID,
    chat_id: int,
    replies: list[Reply],
) -> uuid.UUID | None:
    """Поставить долговечную доставку ответа (AUD-11).

    Повтор доставки не запускает бизнес-команду заново и не может создать
    вторую финансовую запись.
    """
    return await queue.enqueue(
        session,
        job_type="deliver_reply",
        logical_key=f"reply:{event_id}",
        queue_class="interactive",
        workspace_id=job.workspace_id,
        subject_id=event_id,
        payload=_reply_payload(event_id, chat_id, replies),
        correlation_id=job.correlation_id,
    )


async def _send_now(settings: Settings, *, chat_id: int, replies: list[Reply]) -> bool:
    """Немедленный ответ автору (FR-53).

    Возвращает True, если все сообщения доставлены: тогда долговечная задача
    доставки закрывается и второго сообщения не появляется.
    """
    sender = build_sender(settings)
    delivered = True
    for reply in replies:
        if not reply.text:
            continue
        result = await sender.send_message(
            chat_id=chat_id, text=reply.text, buttons=reply.keyboard()
        )
        if not result.ok:
            if result.blocked:
                # Блокировка бота получателем не лечится повтором (A66).
                continue
            delivered = False
            logger.warning("reply_immediate_failed", chat_id=chat_id, error=result.error)
    return delivered


async def _close_reply_job(settings: Settings, job_id: uuid.UUID) -> None:
    from fintracker.db.models.platform import Job

    async with session_scope(settings, RuntimeRole.WORKER) as session:
        await session.execute(
            update(Job)
            .where(Job.id == job_id, Job.state.in_(("queued", "retry_wait")))
            .values(state="succeeded", lease_token=None, lease_until=None)
        )


async def handle_deliver_reply(settings: Settings, job: LeasedJob) -> None:
    """Отправить подготовленный ответ автору с повтором при сбое (AUD-11)."""
    from fintracker.core.errors import TemporarilyUnavailable

    chat_id = int(job.payload["chat_id"])
    messages = list(job.payload.get("messages") or [])
    sender = build_sender(settings)
    for item in messages:
        result = await sender.send_message(
            chat_id=chat_id, text=str(item["text"]), buttons=item.get("buttons")
        )
        if result.ok or result.blocked:
            # Блокировка бота получателем не лечится повтором (A66).
            continue
        if result.unknown:
            # Неопределённый ответ не повторяется автоматически: сообщение
            # могло дойти. Денежная часть уже зафиксирована отдельно (A100).
            logger.warning("reply_delivery_unknown", job_id=str(job.id), error=result.error)
            continue
        raise TemporarilyUnavailable(
            f"Доставка ответа не удалась: {result.error}",
            retry_after=float(result.retry_after or 0) or None,
        )


async def handle_process_inbound_event(settings: Settings, job: LeasedJob) -> None:
    """Разобрать событие и подготовить ответы автору.

    Порядок: чтение технической части → чтение приватного payload под
    закреплённым контекстом → бизнес-команда → фиксация результата под
    действующей арендой → отдельная доставка ответа.
    """
    event_id = uuid.UUID(str(job.payload["inbound_event_id"]))
    invite_digest = job.payload.get("invite_digest")

    context = await _read_context(settings, event_id)
    if context.state in {"processed", "ignored"}:
        # Повторная доставка не выполняет обработку второй раз (TECH-02).
        logger.info("inbound_already_processed", event_id=str(event_id))
        return

    # Приватный payload читается под контекстом автора и его бюджета (AUD-01).
    async with session_scope(
        settings,
        RuntimeRole.WORKER,
        user_id=context.actor_user_id,
        workspace_id=context.workspace_id,
    ) as session:
        event, payload = await _load_event(session, event_id)
        if event.state in {"processed", "ignored"}:
            return
        event.state = "routed"
        message = build_incoming(payload, event=event, invite_digest=invite_digest)

    if message is None:
        await _mark_state(settings, event_id, "ignored")
        return

    if not context.is_private_chat:
        # Приватные финансовые ответы не отправляются в групповой чат (AUD-12).
        logger.info("inbound_group_chat", event_id=str(event_id))
        if context.chat_id is not None:
            async with session_scope(settings, RuntimeRole.WORKER) as session:
                await session.execute(
                    update(InboundEvent)
                    .where(InboundEvent.id == event_id)
                    .values(state="processed")
                )
                hint_job = await _enqueue_reply(
                    session,
                    job=job,
                    event_id=event_id,
                    chat_id=context.chat_id,
                    replies=[Reply(text=GROUP_HINT)],
                )
            delivered = await _send_now(
                settings, chat_id=context.chat_id, replies=[Reply(text=GROUP_HINT)]
            )
            if delivered and hint_job is not None:
                await _close_reply_job(settings, hint_job)
            return
        await _mark_state(settings, event_id, "processed")
        return

    async with session_scope(settings, RuntimeRole.WORKER) as session:
        # Право на задачу проверяется до побочных эффектов: исполнитель,
        # потерявший аренду, не выполняет бизнес-команду (ADR-05, AUD-03).
        if not await queue.lease_is_valid(session, job):
            logger.warning("lease_lost_before_command", job_id=str(job.id))
            return

    try:
        replies = await handle(settings, message)
    except DomainError as exc:
        # Ошибка домена превращается в понятный текст без раскрытия деталей.
        logger.info("inbound_domain_error", code=exc.code.value, event_id=str(event_id))
        replies = [Reply(text=exc.message)]

    reply_job: uuid.UUID | None = None
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        # Результат принимается только при действующей аренде (ADR-05, AUD-03).
        if not await queue.lease_is_valid(session, job):
            logger.warning("lease_expired_skip_result", job_id=str(job.id))
            return
        # Признание события обработанным, раскрытие outbox и долговечная
        # доставка ответа фиксируются одной транзакцией: сбой отправки уже не
        # может привести к повторной финансовой команде (AUD-02, AUD-11).
        await session.execute(
            update(InboundEvent).where(InboundEvent.id == event_id).values(state="processed")
        )
        await queue.enqueue(
            session,
            job_type="expand_outbox",
            logical_key=f"expand:{event_id}",
            queue_class="interactive",
            workspace_id=job.workspace_id,
            payload={"batch": 50, "schema_version": 1},
            correlation_id=job.correlation_id,
        )
        if context.chat_id is not None and replies:
            reply_job = await _enqueue_reply(
                session,
                job=job,
                event_id=event_id,
                chat_id=context.chat_id,
                replies=replies,
            )

    if context.chat_id is not None and replies:
        # Ответ автору показывается сразу; при сбое остаётся долговечная задача.
        delivered = await _send_now(settings, chat_id=context.chat_id, replies=replies)
        if delivered and reply_job is not None:
            await _close_reply_job(settings, reply_job)
    logger.info(
        "inbound_processed",
        event_id=str(event_id),
        replies=len(replies),
        processed_at=dt.datetime.now(dt.UTC).isoformat(),
    )
