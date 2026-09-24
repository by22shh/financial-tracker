from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from fintracker.application.delivery.dispatch import expand_event
from fintracker.application.ingestion import process_event
from fintracker.application.ledger.service import post_transaction
from fintracker.core.context import MembershipStatus, Role
from fintracker.core.ids import new_generation
from fintracker.db.models.access import Membership, User
from fintracker.db.models.platform import InboundEvent, NotificationDelivery, OutboxEvent
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.infra.telegram.sender import RecordingSender, set_sender_override
from tests.conftest import requires_pg
from tests.integration import test_deep_audit as scenarios
from tests.integration.factories import build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]


async def test_callback_navigation_edits_source_card(owner_session, test_settings) -> None:
    fixture = await scenarios.prepared(owner_session)
    source_message_id = 8181
    update = {
        "update_id": 998181,
        "callback_query": {
            "id": "callback-998181",
            "from": {"id": fixture.user.telegram_user_id, "is_bot": False},
            "message": {
                "message_id": source_message_id,
                "date": 1789000000,
                "chat": {"id": fixture.user.telegram_user_id, "type": "private"},
                "text": "старое меню",
            },
            "data": "menu:budget",
        },
    }
    job = await scenarios.leased(test_settings, update)
    sender = RecordingSender()
    set_sender_override(sender)
    try:
        await process_event.handle_process_inbound_event(test_settings, job)
    finally:
        set_sender_override(None)

    assert len(sender.edited) == 1
    assert sender.edited[0]["message_id"] == source_message_id
    assert fixture.workspace.name in sender.edited[0]["text"]
    assert "Период:" in sender.edited[0]["text"]
    assert sender.sent == []


async def test_interactive_operation_skips_duplicate_for_author_but_not_member(
    owner_session, test_settings
) -> None:
    fixture = await build_fixture(owner_session, telegram_user_id=98101)
    member = User(id=uuid.uuid4(), telegram_user_id=98102)
    owner_session.add(member)
    await owner_session.flush()
    owner_session.add(
        Membership(
            workspace_id=fixture.workspace.id,
            user_id=member.id,
            role=Role.MEMBER.value,
            status=MembershipStatus.ACTIVE.value,
            generation=new_generation(),
        )
    )
    owner_session.add(
        InboundEvent(
            bot_id=1,
            update_id=99101,
            chat_id=fixture.user.telegram_user_id,
            message_id=99101,
            chat_type="private",
            edit_version=0,
            event_type="message.text",
            workspace_id=fixture.workspace.id,
            actor_user_id=fixture.user.id,
            membership_generation=fixture.actor.membership_generation,
            telegram_user_id=fixture.user.telegram_user_id,
            state="routed",
            correlation_id=fixture.actor.correlation_id,
        )
    )
    await owner_session.flush()
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(720), category="Продукты"),
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

    assert plan.created == 1
    assert plan.skipped == 1
    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        deliveries = (await session.scalars(select(NotificationDelivery))).all()
    assert [item.recipient_user_id for item in deliveries] == [member.id]
    assert deliveries[0].delivery_class == "shared_change"
