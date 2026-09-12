"""Долговечный приём обновления Telegram (TECH-01, TECH-02, CMD-01).

До ответа 2xx обновление и первая задача сохраняются одной транзакцией.
Распознавание внутри ожидания webhook не выполняется. Коды приглашений
обрабатываются специальным маршрутом и не сохраняются в открытом payload.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.identity.actor import ensure_user, get_active_workspace_id
from fintracker.config import Settings
from fintracker.core.context import MembershipStatus
from fintracker.core.ids import canonical_hash, normalize_invite_code
from fintracker.core.logging import get_logger
from fintracker.db.models.access import Membership
from fintracker.db.models.platform import InboundEvent, InboundPayload, Job, LogicalMessage
from fintracker.db.session import RuntimeRole, session_scope

logger = get_logger("ingestion.accept")

# Ключевые слова приглашения: содержимое не уходит в AI и в открытый payload.
_INVITE_DEEPLINK = re.compile(r"^/start\s+join_(?P<token>[A-Za-z0-9_-]{6,64})\s*$")
_INVITE_COMMAND = re.compile(r"^/join(?:\s+(?P<code>[A-Za-z0-9\s\-]{6,40}))?\s*$", re.IGNORECASE)

SUPPORTED_UPDATE_KEYS = (
    "message",
    "edited_message",
    "callback_query",
    "my_chat_member",
)


@dataclass(frozen=True, slots=True)
class AcceptedUpdate:
    inbound_event_id: uuid.UUID
    duplicate: bool
    job_id: uuid.UUID | None
    event_type: str


def classify_update(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    for key in SUPPORTED_UPDATE_KEYS:
        if key in payload and isinstance(payload[key], dict):
            return key, payload[key]
    return "unsupported", None


def _message_kind(message: dict[str, Any]) -> str:
    if "voice" in message or "audio" in message:
        return "voice"
    if "photo" in message:
        return "photo"
    if "document" in message:
        return "document"
    if "text" in message:
        return "text"
    return "other"


def sanitize_payload(update: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Убрать секрет приглашения из сохраняемого payload (FR-77, TECH-01).

    Возвращает очищенный payload и digest-пригодный код, если он найден.
    """
    kind, body = classify_update(update)
    invite_code: str | None = None
    if kind in {"message", "edited_message"} and body is not None:
        text = body.get("text")
        if isinstance(text, str):
            deeplink = _INVITE_DEEPLINK.match(text.strip())
            command = _INVITE_COMMAND.match(text.strip())
            if deeplink:
                invite_code = normalize_invite_code(deeplink.group("token"))
            elif command and command.group("code"):
                invite_code = normalize_invite_code(command.group("code"))
    if invite_code is None:
        return update, None

    sanitized = {key: value for key, value in update.items() if key != kind}
    scrubbed_body = {key: value for key, value in (body or {}).items() if key != "text"}
    scrubbed_body["text"] = "<invite-code-redacted>"
    sanitized[kind] = scrubbed_body
    return sanitized, invite_code


def _extract_identity(kind: str, body: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    """Telegram ID отправителя, chat_id и message_id."""
    if kind == "callback_query":
        user = body.get("from") or {}
        message = body.get("message") or {}
        chat = message.get("chat") or {}
        return user.get("id"), chat.get("id"), message.get("message_id")
    user = body.get("from") or {}
    chat = body.get("chat") or {}
    return user.get("id"), chat.get("id"), body.get("message_id")


async def _resolve_workspace_context(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    """Закрепить контекст бюджета на момент приёма (FR-79).

    Позднее переключение не переносит обрабатываемый чек или голос.
    """
    workspace_id = await get_active_workspace_id(session, user_id)
    if workspace_id is None:
        return None
    active = (
        await session.execute(
            select(Membership.id).where(
                Membership.workspace_id == workspace_id,
                Membership.user_id == user_id,
                Membership.status == MembershipStatus.ACTIVE.value,
            )
        )
    ).scalar_one_or_none()
    return workspace_id if active is not None else None


async def accept_telegram_update(
    settings: Settings, update: dict[str, Any], *, correlation_id: str | None = None
) -> AcceptedUpdate:
    """Сохранить входящее событие и первую задачу одной транзакцией."""
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        from fintracker.core.errors import ValidationFailed

        raise ValidationFailed("В обновлении отсутствует update_id")

    correlation = correlation_id or uuid.uuid4().hex
    bot_id = settings.telegram.bot_id
    kind, body = classify_update(update)
    sanitized, invite_code = sanitize_payload(update)

    telegram_user_id: int | None = None
    chat_id: int | None = None
    message_id: int | None = None
    edit_version = 1 if kind == "edited_message" else 0
    media_group_id: str | None = None
    if body is not None:
        telegram_user_id, chat_id, message_id = _extract_identity(kind, body)
        media_group_id = body.get("media_group_id")

    async with session_scope(settings, RuntimeRole.API) as session:
        user_id: uuid.UUID | None = None
        workspace_id: uuid.UUID | None = None
        membership_generation: uuid.UUID | None = None
        if telegram_user_id is not None:
            user = await ensure_user(session, telegram_user_id=telegram_user_id)
            user_id = user.id
            await session.flush()
            # Контекст RLS нужен для чтения собственных членств.
            from fintracker.db.session import set_rls_context

            await set_rls_context(session, user_id=user_id)
            workspace_id = await _resolve_workspace_context(session, user_id)
            if workspace_id is not None:
                membership_generation = (
                    await session.execute(
                        select(Membership.generation).where(
                            Membership.workspace_id == workspace_id,
                            Membership.user_id == user_id,
                            Membership.status == MembershipStatus.ACTIVE.value,
                        )
                    )
                ).scalar_one_or_none()
            await set_rls_context(session, user_id=user_id, workspace_id=workspace_id)

        event_id = uuid.uuid4()
        insert_event = (
            pg_insert(InboundEvent)
            .values(
                id=event_id,
                bot_id=bot_id,
                update_id=update_id,
                chat_id=chat_id,
                message_id=message_id,
                edit_version=edit_version,
                media_group_id=media_group_id,
                event_type=kind if kind != "message" else f"message.{_message_kind(body or {})}",
                workspace_id=workspace_id,
                actor_user_id=user_id,
                membership_generation=membership_generation,
                telegram_user_id=telegram_user_id,
                state="received",
                correlation_id=correlation,
            )
            .on_conflict_do_nothing(index_elements=[InboundEvent.bot_id, InboundEvent.update_id])
            .returning(InboundEvent.id)
        )
        inserted_id = (await session.execute(insert_event)).scalar_one_or_none()
        if inserted_id is None:
            # Повтор update возвращает подтверждение существующего приёма.
            existing = (
                await session.execute(
                    select(InboundEvent.id).where(
                        InboundEvent.bot_id == bot_id, InboundEvent.update_id == update_id
                    )
                )
            ).scalar_one()
            logger.info("inbound_duplicate", update_id=update_id, correlation_id=correlation)
            return AcceptedUpdate(
                inbound_event_id=existing, duplicate=True, job_id=None, event_type=kind
            )

        payload_body: dict[str, Any] = {"update": sanitized}
        if invite_code is not None:
            # Хранится проверочное значение, не открытый секрет (SEC-04).
            payload_body["invite_digest"] = canonical_hash(invite_code)
            payload_body["invite_present"] = True
        session.add(
            InboundPayload(
                inbound_event_id=inserted_id,
                workspace_id=workspace_id,
                owner_user_id=user_id,
                payload=payload_body,
                delete_after=dt.datetime.now(dt.UTC) + dt.timedelta(days=7),
            )
        )
        if chat_id is not None and message_id is not None and kind != "callback_query":
            session.add(
                LogicalMessage(
                    bot_id=bot_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    edit_version=edit_version,
                    media_group_id=media_group_id,
                    workspace_id=workspace_id,
                    owner_user_id=user_id,
                    kind=_message_kind(body or {}),
                )
            )

        job_id = uuid.uuid4()
        job = Job(
            id=job_id,
            queue_class="interactive",
            job_type="process_inbound_event",
            workspace_id=workspace_id,
            subject_id=inserted_id,
            logical_key=f"inbound:{bot_id}:{update_id}",
            payload={
                "inbound_event_id": str(inserted_id),
                "invite_code": invite_code,
                "schema_version": 1,
            },
            max_attempts=settings.limits.job_max_attempts,
            correlation_id=correlation,
        )
        session.add(job)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            existing = (
                await session.execute(
                    select(InboundEvent.id).where(
                        InboundEvent.bot_id == bot_id, InboundEvent.update_id == update_id
                    )
                )
            ).scalar_one()
            return AcceptedUpdate(
                inbound_event_id=existing, duplicate=True, job_id=None, event_type=kind
            )

    logger.info(
        "inbound_accepted",
        update_id=update_id,
        event_type=kind,
        correlation_id=correlation,
        has_workspace=workspace_id is not None,
    )
    return AcceptedUpdate(
        inbound_event_id=inserted_id, duplicate=False, job_id=job_id, event_type=kind
    )
