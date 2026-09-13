"""Персональные доставки и надёжность транспорта (FR-86, A95–A101, A158–A161)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.delivery.dispatch import (
    expand_event,
    handle_deliver_notification,
    handle_expand_outbox,
    reserve_daily_slot,
)
from fintracker.application.ingestion.accept_update import accept_telegram_update
from fintracker.application.ledger.service import post_transaction
from fintracker.application.platform import queue
from fintracker.config import Settings
from fintracker.core.context import MembershipStatus, Role
from fintracker.core.ids import new_generation
from fintracker.db.models.access import Membership, User
from fintracker.db.models.platform import (
    InboundEvent,
    Job,
    NotificationDelivery,
    OutboxEvent,
)
from fintracker.db.session import RuntimeRole, get_sessionmaker, session_scope
from fintracker.infra.telegram.sender import RecordingSender, set_sender_override
from tests.conftest import requires_pg
from tests.integration.factories import build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)


def telegram_update(update_id: int, *, text: str = "кофе 250", user_id: int = 5001) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1789000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Тест"},
            "text": text,
        },
    }


async def test_a95_duplicate_update_accepted_once(clean_db: None, test_settings: Settings) -> None:
    """CMD-01, A95: один update, доставленный многократно, сохраняется один раз."""
    payload = telegram_update(777001)
    results = [await accept_telegram_update(test_settings, payload) for _ in range(20)]
    assert sum(1 for item in results if not item.duplicate) == 1
    assert sum(1 for item in results if item.duplicate) == 19

    factory = get_sessionmaker(test_settings, RuntimeRole.OWNER)
    async with factory() as session, session.begin():
        events = (await session.execute(select(InboundEvent))).scalars().all()
        jobs = (await session.execute(select(Job))).scalars().all()
    assert len(events) == 1
    assert len(jobs) == 1, "повтор не создаёт вторую задачу"


async def test_invite_secret_never_stored_in_payload(
    clean_db: None, test_settings: Settings
) -> None:
    """A180/SEC-04: секрет приглашения заменяется digest и не попадает в payload."""
    payload = telegram_update(777010, text="/start join_ABCD2345EFGH")
    await accept_telegram_update(test_settings, payload)

    factory = get_sessionmaker(test_settings, RuntimeRole.OWNER)
    async with factory() as session, session.begin():
        from fintracker.db.models.platform import InboundPayload

        stored = (await session.execute(select(InboundPayload))).scalars().one()
        job = (await session.execute(select(Job))).scalars().one()
    serialized = str(stored.payload)
    assert "ABCD2345EFGH" not in serialized
    assert "<invite-code-redacted>" in serialized
    assert stored.payload["invite_present"] is True
    # Код нужен обработчику, но хранится только в защищённом payload задачи.
    assert job.payload["invite_code"] == "ABCD2345EFGH"


async def test_a158_one_event_creates_personal_deliveries(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """A158: одна операция создаёт по одной доставке участнику без копий расхода."""
    fixture = await build_fixture(owner_session, telegram_user_id=5101)
    others = []
    for telegram_id in (5102, 5103):
        user = User(id=uuid.uuid4(), telegram_user_id=telegram_id)
        owner_session.add(user)
        await owner_session.flush()
        owner_session.add(
            Membership(
                workspace_id=fixture.workspace.id,
                user_id=user.id,
                role=Role.MEMBER.value,
                status=MembershipStatus.ACTIVE.value,
                generation=new_generation(),
            )
        )
        others.append(user)
    await owner_session.flush()

    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(650), category="Рестораны"),
        origin="telegram_text",
    )
    await owner_session.commit()

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        event = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "TransactionPosted")
            )
        ).scalar_one()
        plan = await expand_event(session, test_settings, event)
    assert plan.created == 3, "автор и два участника получают свои доставки"

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        deliveries = (await session.execute(select(NotificationDelivery))).scalars().all()
        classes = {row.recipient_user_id: row.delivery_class for row in deliveries}
    assert classes[fixture.user.id] == "author_card", "автор получает карточку, не дубликат"
    assert all(classes[user.id] == "shared_change" for user in others), (
        "остальным уходит уведомление об изменении"
    )


async def test_a159_failed_delivery_does_not_block_others(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """A159: ошибка одной доставки не блокирует другую и не трогает журнал."""
    fixture = await build_fixture(owner_session, telegram_user_id=5201)
    other = User(id=uuid.uuid4(), telegram_user_id=5202)
    owner_session.add(other)
    await owner_session.flush()
    owner_session.add(
        Membership(
            workspace_id=fixture.workspace.id,
            user_id=other.id,
            role=Role.MEMBER.value,
            status=MembershipStatus.ACTIVE.value,
            generation=new_generation(),
        )
    )
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(650), category="Рестораны"),
        origin="telegram_text",
    )
    await owner_session.commit()

    sender = RecordingSender(fail_for_chats={5202})
    set_sender_override(sender)
    try:
        async with session_scope(
            test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
        ) as session:
            await queue.enqueue(
                session,
                job_type="expand_outbox",
                logical_key="expand:test",
                workspace_id=fixture.workspace.id,
                payload={"batch": 50},
                correlation_id="test",
            )
        jobs = await queue.claim_jobs(test_settings, queue_classes=("interactive",), limit=5)
        for job in jobs:
            await handle_expand_outbox(test_settings, job)
        delivery_jobs = await queue.claim_jobs(
            test_settings, queue_classes=("interactive",), limit=5
        )
        for job in delivery_jobs:
            await handle_deliver_notification(test_settings, job)
    finally:
        set_sender_override(None)

    assert any(item["chat_id"] == 5201 for item in sender.sent), "автор получил карточку"
    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        states = {
            row.recipient_user_id: row.state
            for row in ((await session.execute(select(NotificationDelivery))).scalars().all())
        }
    assert states[fixture.user.id] == "sent"
    assert states[other.id] == "failed", "повтор нужен только этому получателю"


async def test_a66_blocked_recipient_stops_only_own_delivery(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-30, A66: блокировка бота отключает доставку только этому человеку."""
    fixture = await build_fixture(owner_session, telegram_user_id=5301)
    other = User(id=uuid.uuid4(), telegram_user_id=5302)
    owner_session.add(other)
    await owner_session.flush()
    owner_session.add(
        Membership(
            workspace_id=fixture.workspace.id,
            user_id=other.id,
            role=Role.MEMBER.value,
            status=MembershipStatus.ACTIVE.value,
            generation=new_generation(),
        )
    )
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(650), category="Рестораны"),
        origin="telegram_text",
    )
    await owner_session.commit()

    sender = RecordingSender(blocked_chats={5302})
    set_sender_override(sender)
    try:
        async with session_scope(
            test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
        ) as session:
            await queue.enqueue(
                session,
                job_type="expand_outbox",
                logical_key="expand:blocked",
                workspace_id=fixture.workspace.id,
                payload={"batch": 50},
                correlation_id="test",
            )
        for job in await queue.claim_jobs(test_settings, queue_classes=("interactive",), limit=5):
            await handle_expand_outbox(test_settings, job)
        for job in await queue.claim_jobs(test_settings, queue_classes=("interactive",), limit=5):
            await handle_deliver_notification(test_settings, job)
    finally:
        set_sender_override(None)

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        states = {
            row.recipient_user_id: row.state
            for row in ((await session.execute(select(NotificationDelivery))).scalars().all())
        }
    assert states[other.id] == "cancelled", "бесконечных повторов нет"
    assert states[fixture.user.id] == "sent"


async def test_a100_unknown_send_result_keeps_record(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-30, LIM-10, A100: неоднозначный таймаут отправки не отменяет сохранённую операцию."""
    fixture = await build_fixture(owner_session, telegram_user_id=5401)
    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(650), category="Рестораны"),
        origin="telegram_text",
    )
    await owner_session.commit()

    sender = RecordingSender(unknown_chats={5401})
    set_sender_override(sender)
    try:
        async with session_scope(
            test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
        ) as session:
            await queue.enqueue(
                session,
                job_type="expand_outbox",
                logical_key="expand:unknown",
                workspace_id=fixture.workspace.id,
                payload={"batch": 50},
                correlation_id="test",
            )
        for job in await queue.claim_jobs(test_settings, queue_classes=("interactive",), limit=5):
            await handle_expand_outbox(test_settings, job)
        for job in await queue.claim_jobs(test_settings, queue_classes=("interactive",), limit=5):
            await handle_deliver_notification(test_settings, job)
    finally:
        set_sender_override(None)

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        delivery = ((await session.execute(select(NotificationDelivery))).scalars().all())[0]
        from fintracker.db.models.ledger import Transaction

        transaction = (
            await session.execute(
                select(Transaction).where(Transaction.id == posted.transaction_id)
            )
        ).scalar_one()
    assert delivery.state == "unknown", "доставка помечена неопределённой"
    assert transaction.status == "posted", "покупка не повторена и не отменена"


async def test_a64_daily_quota_limits_proactive_messages(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """LIM-06, A64: третье обычное проактивное сообщение за день переносится."""
    fixture = await build_fixture(owner_session, telegram_user_id=5501)
    await owner_session.commit()
    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        first = await reserve_daily_slot(
            session, recipient_user_id=fixture.user.id, local_day=DAY, limit=2
        )
        second = await reserve_daily_slot(
            session, recipient_user_id=fixture.user.id, local_day=DAY, limit=2
        )
        third = await reserve_daily_slot(
            session, recipient_user_id=fixture.user.id, local_day=DAY, limit=2
        )
    assert first and second
    assert not third, "предел два проактивных сообщения в день соблюдён"


async def test_ar04_expired_lease_result_is_rejected(
    clean_db: None, test_settings: Settings
) -> None:
    """AR-04: результат исполнителя с истёкшей арендой не принимается."""
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        await queue.enqueue(
            session,
            job_type="process_inbound_event",
            logical_key="lease:test",
            payload={"inbound_event_id": str(uuid.uuid4())},
            correlation_id="test",
        )
    jobs = await queue.claim_jobs(test_settings, queue_classes=("interactive",), limit=1)
    assert len(jobs) == 1
    job = jobs[0]

    # Аренда истекла и задачу перехватил другой исполнитель.
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        await session.execute(update(Job).where(Job.id == job.id).values(lease_token=uuid.uuid4()))
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        assert not await queue.lease_is_valid(session, job)
    assert not await queue.renew_lease(test_settings, job)
    assert not await queue.complete(test_settings, job)
