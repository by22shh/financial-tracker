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
from fintracker.core.errors import DomainError, NotFound, TemporarilyUnavailable
from fintracker.core.fencing import execution_fence
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
                file_name=document.get("file_name"),
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
    membership_generation: uuid.UUID | None
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
                    InboundEvent.membership_generation,
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
        membership_generation=row[2],
        chat_id=row[3],
        chat_type=row[4],
        state=row[5],
    )


async def _mark_state(settings: Settings, event_id: uuid.UUID, state: str) -> None:
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        await session.execute(
            update(InboundEvent).where(InboundEvent.id == event_id).values(state=state)
        )


async def _store_reply(
    session: AsyncSession,
    *,
    context: _EventContext,
    event_id: uuid.UUID,
    chat_id: int,
    replies: list[Reply],
) -> uuid.UUID | None:
    """Сохранить текст ответа в изолированной строке бюджета (ADR-06, R-04).

    Глобальная таблица задач технической маршрутизации не хранит суммы и
    статьи: там остаётся только идентификатор этой строки.
    """
    from fintracker.db.models.platform import AuthorReply

    if context.workspace_id is None or context.actor_user_id is None:
        return None
    row = AuthorReply(
        workspace_id=context.workspace_id,
        owner_user_id=context.actor_user_id,
        inbound_event_id=event_id,
        chat_id=chat_id,
        messages=[
            {
                "text": reply.text,
                "buttons": reply.keyboard(),
                "transaction_id": str(reply.transaction_id) if reply.transaction_id else None,
            }
            for reply in replies
            if reply.text
        ],
        state="pending",
        delete_after=dt.datetime.now(dt.UTC) + dt.timedelta(days=7),
    )
    session.add(row)
    await session.flush()
    return row.id


async def _load_reply(
    settings: Settings, *, context: _EventContext, reply_id: uuid.UUID
) -> list[dict[str, Any]] | None:
    """Прочитать сохранённый ответ под контекстом его владельца."""
    from fintracker.db.models.platform import AuthorReply

    async with session_scope(
        settings,
        RuntimeRole.WORKER,
        user_id=context.actor_user_id,
        workspace_id=context.workspace_id,
    ) as session:
        row = (
            await session.execute(select(AuthorReply).where(AuthorReply.id == reply_id))
        ).scalar_one_or_none()
        if row is None or row.state != "pending":
            return None
        return [dict(item) for item in row.messages]


async def _close_reply(
    settings: Settings,
    *,
    context: _EventContext,
    reply_id: uuid.UUID,
    state: str,
    card_links: list[dict[str, str]] | None = None,
) -> None:
    from fintracker.db.models.platform import AuthorReply

    async with session_scope(
        settings,
        RuntimeRole.WORKER,
        user_id=context.actor_user_id,
        workspace_id=context.workspace_id,
    ) as session:
        values: dict[str, Any] = {"state": state}
        if card_links:
            values["card_links"] = card_links
        await session.execute(
            update(AuthorReply)
            .where(AuthorReply.id == reply_id, AuthorReply.state == "pending")
            .values(**values)
        )


async def _enqueue_reply(
    session: AsyncSession,
    *,
    job: LeasedJob,
    context: _EventContext,
    event_id: uuid.UUID,
    chat_id: int,
    replies: list[Reply],
) -> uuid.UUID | None:
    """Поставить долговечную доставку ответа (AUD-11, R-04).

    Повтор доставки не запускает бизнес-команду заново и не может создать
    вторую финансовую запись. Полезная нагрузка задачи не содержит текста.
    """
    reply_id = await _store_reply(
        session, context=context, event_id=event_id, chat_id=chat_id, replies=replies
    )
    payload: dict[str, Any] = {
        "inbound_event_id": str(event_id),
        "chat_id": chat_id,
        "schema_version": 2,
    }
    if reply_id is not None:
        payload["author_reply_id"] = str(reply_id)
    else:
        # Ответ вне бюджета не содержит его данных: приветствие, подсказка
        # для группового чата и приглашение создать бюджет.
        payload["messages"] = [
            {"text": reply.text, "buttons": reply.keyboard()} for reply in replies if reply.text
        ]
    return await queue.enqueue(
        session,
        job_type="deliver_reply",
        logical_key=f"reply:{event_id}",
        queue_class="interactive",
        workspace_id=job.workspace_id,
        subject_id=event_id,
        payload=payload,
        correlation_id=job.correlation_id,
    )


async def _has_delivery_lease(settings: Settings, job: LeasedJob) -> bool:
    """Проверить аренду перед внешней отправкой без удержания транзакции."""
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        return await queue.lease_is_valid(session, job)


async def _cancel_revoked_reply(
    settings: Settings, context: _EventContext, event_id: uuid.UUID
) -> None:
    from fintracker.db.models.platform import AuthorReply

    async with session_scope(
        settings,
        RuntimeRole.WORKER,
        user_id=context.actor_user_id,
        workspace_id=context.workspace_id,
    ) as session:
        await session.execute(
            update(AuthorReply)
            .where(AuthorReply.inbound_event_id == event_id, AuthorReply.state == "pending")
            .values(state="cancelled")
        )
        await session.execute(
            update(InboundEvent).where(InboundEvent.id == event_id).values(state="ignored")
        )


async def _send_now(
    settings: Settings,
    *,
    chat_id: int,
    replies: list[Reply],
    context: _EventContext,
    event_id: uuid.UUID,
    job: LeasedJob,
) -> bool:
    """Немедленный ответ автору (FR-53).

    Возвращает True, когда доставка завершена: тогда событие признаётся
    обработанным, а долговечная задача доставки закрывается. False означает
    восстановимый сбой: событие остаётся недоставленным и повторяется только
    доставкой, без повторной финансовой команды (AUD-02, AUD-11).
    """
    sender = build_sender(settings)
    delivered = True
    for reply in replies:
        if not reply.text:
            continue
        if not await _has_delivery_lease(settings, job):
            return False
        if context.chat_id != chat_id or not await _delivery_allowed(settings, context):
            await _cancel_revoked_reply(settings, context, event_id)
            return True
        result = await sender.send_message(
            chat_id=chat_id, text=reply.text, buttons=reply.keyboard()
        )
        if result.ok or result.blocked:
            # Блокировка бота получателем не лечится повтором (A66).
            continue
        if result.unknown:
            # Неопределённый ответ не повторяется: сообщение могло дойти (A100).
            logger.warning("reply_delivery_unknown", chat_id=chat_id, error=result.error)
            continue
        delivered = False
        logger.warning("reply_immediate_failed", chat_id=chat_id, error=result.error)
    return delivered


async def _deliver_claimed(
    settings: Settings,
    *,
    context: _EventContext,
    event_id: uuid.UUID,
    reply_job: uuid.UUID | None,
) -> None:
    """Показать ответ сразу, если удалось захватить его задачу доставки (G-21).

    Немедленная отправка и фоновый исполнитель соревнуются за одну строку
    задачи: сообщение уходит ровно один раз. Проигравший путь ничего не
    отправляет, а незавершённая доставка остаётся к повтору.
    """
    if reply_job is None:
        await _settle_delivery(settings, event_id=event_id, reply_job_id=None)
        return
    claimed = await queue.claim_specific(settings, reply_job)
    if claimed is None:
        # Задачу уже выполняет исполнитель: второй отправки не будет.
        logger.info("reply_delivery_taken_by_worker", event_id=str(event_id))
        return
    try:
        await handle_deliver_reply(settings, claimed)
    except DomainError as exc:
        await queue.fail(settings, claimed, error=exc.message, retry_after=exc.retry_after)
        logger.warning("reply_deferred_to_delivery_job", event_id=str(event_id))
        return
    except Exception as exc:
        await queue.fail(settings, claimed, error=str(exc)[:300])
        raise
    await queue.complete(settings, claimed)


async def _settle_delivery(
    settings: Settings, *, event_id: uuid.UUID, reply_job_id: uuid.UUID | None
) -> None:
    """Признать событие обработанным после доставки ответа (AUD-11).

    Событие переходит в processed только здесь: пока ответ не доставлен, оно
    остаётся routed и остаётся видимым как незавершённое.
    """
    from fintracker.db.models.platform import Job

    context = await _read_context(settings, event_id)
    from fintracker.db.models.platform import AuthorReply

    async with session_scope(
        settings,
        RuntimeRole.WORKER,
        user_id=context.actor_user_id,
        workspace_id=context.workspace_id,
    ) as session:
        await session.execute(
            update(AuthorReply)
            .where(AuthorReply.inbound_event_id == event_id, AuthorReply.state == "pending")
            .values(state="sent")
        )
        await session.execute(
            update(InboundEvent)
            .where(InboundEvent.id == event_id, InboundEvent.state.notin_(("ignored", "failed")))
            .values(state="processed")
        )
        if reply_job_id is not None:
            await session.execute(
                update(Job)
                .where(Job.id == reply_job_id, Job.state.in_(("queued", "retry_wait", "running")))
                .values(state="succeeded", lease_token=None, lease_until=None)
            )


async def _delivery_allowed(settings: Settings, context: _EventContext) -> bool:
    """Сохраняется ли право получателя на этот приватный ответ (R-04).

    Отложенная доставка проверяет адресата заново: исключённый участник,
    сменившееся поколение членства, карантин, идущее изменение доступа и
    удаление бюджета отменяют отправку подготовленного финансового текста.
    """
    from fintracker.core.context import MembershipStatus, WorkspaceState
    from fintracker.db.models.access import Membership, Workspace

    if context.actor_user_id is None:
        return True
    if context.workspace_id is None:
        # Ответ без бюджета не содержит финансовых данных бюджета.
        return True
    async with session_scope(
        settings, RuntimeRole.WORKER, workspace_id=context.workspace_id
    ) as session:
        row = (
            await session.execute(
                select(Membership.status, Membership.generation).where(
                    Membership.workspace_id == context.workspace_id,
                    Membership.user_id == context.actor_user_id,
                )
            )
        ).one_or_none()
        workspace = (
            await session.execute(
                select(Workspace.state, Workspace.quarantined, Workspace.security_fence).where(
                    Workspace.id == context.workspace_id
                )
            )
        ).one_or_none()
    if row is None or row[0] != MembershipStatus.ACTIVE.value:
        return False
    if context.membership_generation is not None and row[1] != context.membership_generation:
        return False
    if workspace is None or workspace[0] != WorkspaceState.ACTIVE.value:
        return False
    return not (workspace[1] or workspace[2] is not None)


async def handle_deliver_reply(settings: Settings, job: LeasedJob) -> None:
    """Отправить подготовленный ответ автору с повтором при сбое (AUD-11).

    Задача не повторяет бизнес-команду: она отправляет уже сохранённый текст.
    Перед отправкой заново проверяется право получателя на эти данные (R-04).
    """
    chat_id = int(job.payload["chat_id"])
    if not await _has_delivery_lease(settings, job):
        logger.info("reply_lease_lost", job_id=str(job.id))
        return
    raw_event = job.payload.get("inbound_event_id")
    event_id = uuid.UUID(str(raw_event)) if raw_event is not None else None

    context: _EventContext | None = None
    if event_id is not None:
        context = await _read_context(settings, event_id)
        if context.state in {"processed", "ignored"}:
            return
        if context.chat_id != chat_id or not await _delivery_allowed(settings, context):
            logger.info("reply_delivery_revoked", job_id=str(job.id), event_id=str(event_id))
            await _cancel_revoked_reply(settings, context, event_id)
            return

    raw_reply_id = job.payload.get("author_reply_id")
    if raw_reply_id is not None and context is not None:
        messages = await _load_reply(
            settings, context=context, reply_id=uuid.UUID(str(raw_reply_id))
        )
        if messages is None:
            logger.info("reply_already_settled", job_id=str(job.id))
            return
    else:
        messages = [dict(item) for item in (job.payload.get("messages") or [])]

    sender = build_sender(settings)
    links: list[dict[str, str]] = []
    for item in messages:
        if not await _has_delivery_lease(settings, job):
            return
        if (
            context is not None
            and event_id is not None
            and not await _delivery_allowed(settings, context)
        ):
            await _cancel_revoked_reply(settings, context, event_id)
            return
        result = await sender.send_message(
            chat_id=chat_id, text=str(item["text"]), buttons=item.get("buttons")
        )
        if result.ok and item.get("transaction_id") and result.message_id is not None:
            # Связь карточки с операцией: ответ на неё адресует эту операцию.
            links.append(
                {
                    "message_id": str(result.message_id),
                    "transaction_id": str(item["transaction_id"]),
                }
            )
        if result.ok or result.blocked:
            continue
        if result.unknown:
            logger.warning("reply_delivery_unknown", job_id=str(job.id), error=result.error)
            continue
        raise TemporarilyUnavailable(
            f"Доставка ответа не удалась: {result.error}",
            retry_after=float(result.retry_after or 0) or None,
        )
    if raw_reply_id is not None and context is not None:
        await _close_reply(
            settings,
            context=context,
            reply_id=uuid.UUID(str(raw_reply_id)),
            state="sent",
            card_links=links,
        )
    if event_id is not None:
        await _settle_delivery(settings, event_id=event_id, reply_job_id=None)


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
        if context.chat_id is None:
            await _mark_state(settings, event_id, "processed")
            return
        hint = [Reply(text=GROUP_HINT)]
        async with session_scope(
            settings,
            RuntimeRole.WORKER,
            user_id=context.actor_user_id,
            workspace_id=context.workspace_id,
        ) as session:
            hint_job = await _enqueue_reply(
                session,
                job=job,
                context=context,
                event_id=event_id,
                chat_id=context.chat_id,
                replies=hint,
            )
        await _deliver_claimed(settings, context=context, event_id=event_id, reply_job=hint_job)
        return

    async with session_scope(settings, RuntimeRole.WORKER) as session:
        # Право на задачу проверяется до побочных эффектов: исполнитель,
        # потерявший аренду, не выполняет бизнес-команду (ADR-05, AUD-03).
        if not await queue.lease_is_valid(session, job):
            logger.warning("lease_lost_before_command", job_id=str(job.id))
            return

    try:
        # Право на результат действует всё время выполнения команды: потеря
        # аренды отменяет запись в той же транзакции (ADR-05, R-02).
        async with execution_fence(
            queue.lease_fence(job), job_id=job.id, lease_token=job.lease_token
        ):
            replies = await handle(settings, message)
    except DomainError as exc:
        # Ошибка домена превращается в понятный текст без раскрытия деталей.
        logger.info("inbound_domain_error", code=exc.code.value, event_id=str(event_id))
        replies = [Reply(text=exc.message)]

    reply_job: uuid.UUID | None = None
    async with session_scope(
        settings,
        RuntimeRole.WORKER,
        user_id=context.actor_user_id,
        workspace_id=context.workspace_id,
    ) as session:
        # Результат принимается только при действующей аренде (ADR-05, AUD-03).
        if not await queue.lease_is_valid(session, job):
            logger.warning("lease_expired_skip_result", job_id=str(job.id))
            return
        # Раскрытие outbox и долговечная доставка ответа фиксируются одной
        # транзакцией. Событие ещё не processed: признание обработанным
        # наступает только после доставки ответа (AUD-11).
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
                context=context,
                event_id=event_id,
                chat_id=context.chat_id,
                replies=replies,
            )

    if context.chat_id is None or not replies:
        await _settle_delivery(settings, event_id=event_id, reply_job_id=None)
    else:
        await _deliver_claimed(settings, context=context, event_id=event_id, reply_job=reply_job)
    logger.info(
        "inbound_processed",
        event_id=str(event_id),
        replies=len(replies),
        processed_at=dt.datetime.now(dt.UTC).isoformat(),
    )
