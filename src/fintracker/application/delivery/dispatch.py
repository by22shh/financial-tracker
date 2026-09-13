"""Раскрытие outbox в персональные доставки и отправка (FR-86, TECH-05, ADR-05).

Одно финансовое событие не создаёт копий на участника: создаются независимые
NotificationDelivery с ключом (event_id, recipient, generation, channel).
Тихие часы (LIM-07) и дневной предел проактивных сообщений (LIM-06)
применяются к фоновым классам доставки.
Перед каждой попыткой отправки повторно проверяются бюджет и членство.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.platform import queue
from fintracker.application.platform.queue import LeasedJob
from fintracker.config import Settings
from fintracker.core.context import MembershipStatus, WorkspaceState
from fintracker.core.logging import get_logger
from fintracker.db.models.access import (
    Membership,
    NotificationPreference,
    User,
    Workspace,
)
from fintracker.db.models.platform import (
    ConsumerReceipt,
    NotificationDelivery,
    OutboxEvent,
    RecipientDayQuota,
)
from fintracker.db.session import RuntimeRole, session_scope

logger = get_logger("delivery.dispatch")

CONSUMER_NAME = "notifications"

# Классы доставки: ответы автору и совместные правки — отдельные потоки,
# не расходующие дневной лимит обзоров (FR-53).
IMMEDIATE_CLASSES = frozenset({"author_card", "shared_change", "terminal"})
PROACTIVE_CLASSES = frozenset({"threshold", "review", "reminder"})

EVENT_DELIVERY_CLASS: dict[str, str] = {
    "TransactionPosted": "shared_change",
    "TransactionRevised": "shared_change",
    "TransactionVoided": "shared_change",
    "CategoryCreated": "shared_change",
    "CategoryChanged": "shared_change",
    "CategoryArchived": "shared_change",
    "CategoryRestored": "shared_change",
    "BudgetAccepted": "shared_change",
    "BudgetReallocated": "shared_change",
    "MemberJoined": "shared_change",
    "MemberLeft": "shared_change",
    "MemberRemoved": "shared_change",
    "AdminTransferred": "shared_change",
    "BudgetPeriodOpened": "review",
    "BudgetPeriodEnded": "review",
    "ImportCommitted": "review",
    "PaymentReminder": "reminder",
    "ThresholdCrossed": "threshold",
    "AnalysisCompleted": "review",
    "BudgetDeletionRequested": "terminal",
}


@dataclass(frozen=True, slots=True)
class DeliveryPlan:
    event_id: uuid.UUID
    created: int
    skipped: int


def _quiet_hours_shift(
    now: dt.datetime, *, timezone: str, start_hour: int, end_hour: int
) -> dt.datetime:
    """Перенести фоновое событие на начало разрешённого времени (FR-53)."""
    local = now.astimezone(ZoneInfo(timezone))
    hour = local.hour
    in_quiet = (
        start_hour <= hour or hour < end_hour
        if start_hour > end_hour
        else start_hour <= hour < end_hour
    )
    if not in_quiet:
        return now
    target = local.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if target <= local:
        target = target + dt.timedelta(days=1)
    return target.astimezone(dt.UTC)


async def expand_event(
    session: AsyncSession, settings: Settings, event: OutboxEvent
) -> DeliveryPlan:
    """Создать персональные доставки по аудитории события."""
    workspace = (
        await session.execute(select(Workspace).where(Workspace.id == event.workspace_id))
    ).scalar_one_or_none()
    if workspace is None:
        return DeliveryPlan(event_id=event.id, created=0, skipped=0)

    delivery_class = EVENT_DELIVERY_CLASS.get(event.event_type, "shared_change")
    if event.audience == "none":
        return DeliveryPlan(event_id=event.id, created=0, skipped=0)

    # Удаление бюджета гасит обычные финансовые доставки (FR-83).
    workspace_closing = workspace.state in {
        WorkspaceState.DELETING.value,
        WorkspaceState.DELETED.value,
    }
    if workspace_closing and event.audience != "terminal":
        return DeliveryPlan(event_id=event.id, created=0, skipped=0)

    # Получатели фиксируются по активному членству на момент события (FR-86).
    statuses = (
        (MembershipStatus.ACTIVE.value, MembershipStatus.LEFT.value, MembershipStatus.REMOVED.value)
        if event.audience == "terminal"
        else (MembershipStatus.ACTIVE.value,)
    )
    members = (
        (
            await session.execute(
                select(Membership).where(
                    Membership.workspace_id == event.workspace_id,
                    Membership.status.in_(statuses),
                )
            )
        )
        .scalars()
        .all()
    )

    now = dt.datetime.now(dt.UTC)
    created = 0
    skipped = 0
    for membership in members:
        if event.audience == "admin" and membership.role != "admin":
            continue
        is_author = membership.user_id == event.actor_user_id
        if is_author and event.audience == "members":
            # Автор действия не получает дубликат своей карточки (FR-86).
            effective_class = "author_card"
        elif is_author and event.audience == "author":
            effective_class = "author_card"
        elif event.audience == "author":
            continue
        else:
            effective_class = delivery_class

        preference = (
            (
                await session.execute(
                    select(NotificationPreference).where(
                        NotificationPreference.user_id == membership.user_id,
                        NotificationPreference.workspace_id.in_((event.workspace_id, None)),
                    )
                )
            )
            .scalars()
            .first()
        )
        settings_map = dict(preference.settings) if preference else {}
        if settings_map.get(event.event_type) == "off" or (
            settings_map.get(effective_class) == "off"
        ):
            skipped += 1
            continue

        available_at = now
        if effective_class in PROACTIVE_CLASSES:
            user_tz = None
            if preference is not None and preference.timezone:
                user_tz = preference.timezone
            if user_tz is None:
                user_tz = (
                    await session.execute(
                        select(User.timezone).where(User.id == membership.user_id)
                    )
                ).scalar_one_or_none() or workspace.timezone
            available_at = _quiet_hours_shift(
                now,
                timezone=user_tz,
                start_hour=preference.quiet_hours_start
                if preference
                else settings.limits.quiet_hours_start,
                end_hour=preference.quiet_hours_end
                if preference
                else settings.limits.quiet_hours_end,
            )

        statement = (
            pg_insert(NotificationDelivery)
            .values(
                event_id=event.id,
                workspace_id=event.workspace_id,
                recipient_user_id=membership.user_id,
                membership_generation=membership.generation,
                channel="telegram",
                delivery_class=effective_class,
                state="pending",
                available_at=available_at,
                expires_at=now + dt.timedelta(hours=settings.limits.delivery_max_age_hours),
            )
            .on_conflict_do_nothing(
                index_elements=[
                    NotificationDelivery.event_id,
                    NotificationDelivery.recipient_user_id,
                    NotificationDelivery.membership_generation,
                    NotificationDelivery.channel,
                ]
            )
            .returning(NotificationDelivery.id)
        )
        inserted = (await session.execute(statement)).scalar_one_or_none()
        if inserted is not None:
            created += 1
    return DeliveryPlan(event_id=event.id, created=created, skipped=skipped)


async def handle_expand_outbox(settings: Settings, job: LeasedJob) -> None:
    """Раскрыть необработанные события в доставки (TECH-05)."""
    batch = int(job.payload.get("batch", 50))
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        events = (
            (
                await session.execute(
                    select(OutboxEvent)
                    .outerjoin(
                        ConsumerReceipt,
                        (ConsumerReceipt.event_id == OutboxEvent.id)
                        & (ConsumerReceipt.consumer_name == CONSUMER_NAME),
                    )
                    .where(ConsumerReceipt.id.is_(None))
                    .order_by(OutboxEvent.workspace_id, OutboxEvent.event_seq)
                    .limit(batch)
                )
            )
            .scalars()
            .all()
        )

    for event in events:
        async with session_scope(
            settings, RuntimeRole.WORKER, workspace_id=event.workspace_id
        ) as session:
            plan = await expand_event(session, settings, event)
            # Отметка потребителя исключает повтор эффекта (ADR-05).
            await session.execute(
                pg_insert(ConsumerReceipt)
                .values(event_id=event.id, consumer_name=CONSUMER_NAME, state="done")
                .on_conflict_do_nothing(
                    index_elements=[ConsumerReceipt.event_id, ConsumerReceipt.consumer_name]
                )
            )
            if plan.created:
                await queue.enqueue(
                    session,
                    job_type="deliver_notification",
                    logical_key=f"deliver:{event.id}",
                    queue_class="interactive",
                    workspace_id=event.workspace_id,
                    subject_id=event.id,
                    payload={"event_id": str(event.id), "schema_version": 1},
                    correlation_id=event.correlation_id,
                )


async def reserve_daily_slot(
    session: AsyncSession, *, recipient_user_id: uuid.UUID, local_day: dt.date, limit: int
) -> bool:
    """Атомарно занять слот дневного лимита проактивных сообщений (FR-53).

    Резервация выполняется отдельной короткой транзакцией без удержания
    блокировки бюджета (ADR-05).
    """
    row = (
        await session.execute(
            select(RecipientDayQuota)
            .where(
                RecipientDayQuota.recipient_user_id == recipient_user_id,
                RecipientDayQuota.local_day == local_day,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(
            RecipientDayQuota(
                recipient_user_id=recipient_user_id,
                local_day=local_day,
                used_slots=1,
                limit_slots=limit,
            )
        )
        await session.flush()
        return True
    if row.used_slots >= row.limit_slots:
        return False
    row.used_slots += 1
    await session.flush()
    return True


async def handle_deliver_notification(settings: Settings, job: LeasedJob) -> None:
    """Отправить ожидающие доставки события конкретным получателям."""
    from fintracker.infra.telegram.sender import build_sender

    event_id = uuid.UUID(str(job.payload["event_id"]))
    sender = build_sender(settings)
    workspace_id = job.workspace_id
    assert workspace_id is not None

    async with session_scope(settings, RuntimeRole.WORKER, workspace_id=workspace_id) as session:
        event = (
            await session.execute(select(OutboxEvent).where(OutboxEvent.id == event_id))
        ).scalar_one_or_none()
        if event is None:
            return
        deliveries = (
            (
                await session.execute(
                    select(NotificationDelivery).where(
                        NotificationDelivery.event_id == event_id,
                        NotificationDelivery.state.in_(("pending", "failed")),
                    )
                )
            )
            .scalars()
            .all()
        )
        pending = [
            (
                delivery.id,
                delivery.recipient_user_id,
                delivery.membership_generation,
                delivery.delivery_class,
                delivery.available_at,
            )
            for delivery in deliveries
        ]
        event_type = event.event_type
        event_payload = dict(event.payload)

    now = dt.datetime.now(dt.UTC)
    for delivery_id, recipient_id, generation, delivery_class, available_at in pending:
        if available_at > now:
            continue
        async with session_scope(
            settings, RuntimeRole.WORKER, workspace_id=workspace_id, user_id=recipient_id
        ) as session:
            # Перед отправкой повторно проверяются бюджет и то же членство.
            membership = (
                await session.execute(
                    select(Membership).where(
                        Membership.workspace_id == workspace_id,
                        Membership.user_id == recipient_id,
                        Membership.generation == generation,
                    )
                )
            ).scalar_one_or_none()
            workspace = (
                await session.execute(select(Workspace).where(Workspace.id == workspace_id))
            ).scalar_one_or_none()
            terminal = delivery_class == "terminal"
            if (
                membership is None
                or workspace is None
                or (not terminal and membership.status != MembershipStatus.ACTIVE.value)
                or (
                    not terminal
                    and workspace.state
                    in {WorkspaceState.DELETING.value, WorkspaceState.DELETED.value}
                )
            ):
                await session.execute(
                    update(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery_id)
                    .values(state="cancelled", last_error="Доступ прекращён")
                )
                continue

            if delivery_class in PROACTIVE_CLASSES:
                local_day = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
                allowed = await reserve_daily_slot(
                    session,
                    recipient_user_id=recipient_id,
                    local_day=local_day,
                    limit=settings.limits.proactive_messages_per_day,
                )
                if not allowed:
                    # Превышение объединяется в сводку позже (FR-53, A64).
                    await session.execute(
                        update(NotificationDelivery)
                        .where(NotificationDelivery.id == delivery_id)
                        .values(
                            state="pending",
                            available_at=now + dt.timedelta(hours=12),
                            last_error="Дневной лимит проактивных сообщений исчерпан",
                        )
                    )
                    continue

            telegram_user_id = (
                await session.execute(select(User.telegram_user_id).where(User.id == recipient_id))
            ).scalar_one()
            from fintracker.application.delivery.render import render_event

            text, buttons = await render_event(
                session,
                workspace=workspace,
                event_type=event_type,
                payload=event_payload,
                recipient_user_id=recipient_id,
            )
            if text is None:
                await session.execute(
                    update(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery_id)
                    .values(state="suppressed", last_error="Нечего показывать")
                )
                continue

        result = await sender.send_message(chat_id=telegram_user_id, text=text, buttons=buttons)
        async with session_scope(
            settings, RuntimeRole.WORKER, workspace_id=workspace_id
        ) as session:
            if result.ok:
                await session.execute(
                    update(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery_id)
                    .values(state="sent", telegram_message_id=result.message_id)
                )
            elif result.unknown:
                # Неоднозначный ответ: запись сохранена, доставка неопределённая
                # (A100). Повтор самой покупки не выполняется.
                await session.execute(
                    update(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery_id)
                    .values(state="unknown", last_error=result.error or "Неопределённый ответ")
                )
            elif result.blocked:
                # Блокировка бота отключает доставку только этому человеку (A66).
                await session.execute(
                    update(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery_id)
                    .values(state="cancelled", last_error="Бот заблокирован получателем")
                )
            else:
                retry_at = now + dt.timedelta(seconds=result.retry_after or 30)
                await session.execute(
                    update(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery_id)
                    .values(
                        state="failed",
                        attempts=NotificationDelivery.attempts + 1,
                        available_at=retry_at,
                        last_error=(result.error or "Ошибка отправки")[:300],
                    )
                )
