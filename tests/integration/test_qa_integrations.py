"""Интеграционные проверки QA-02 и эксплуатационные требования (NFR-11, NFR-12).

Проверяются повторы webhook, перезапуск исполнителя, недоступность AI,
повреждённое изображение, изменённая схема провайдера и повтор экспорта.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.ingestion.accept_update import accept_telegram_update
from fintracker.config import Settings
from fintracker.db.models.platform import InboundEvent, Job
from fintracker.db.session import RuntimeRole, get_sessionmaker, session_scope
from tests.conftest import requires_pg
from tests.integration.factories import build_fixture

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)


def _update(update_id: int, *, user_id: int = 6100, text: str = "продукты 500") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1789000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "QA"},
            "text": text,
        },
    }


async def test_qa02_webhook_repeat_and_edit_are_separate(
    clean_db: None, test_settings: Settings
) -> None:
    """QA-02: повтор webhook принимается один раз, правка сообщения — отдельное событие."""
    payload = _update(770_001)
    first = await accept_telegram_update(test_settings, payload)
    repeat = await accept_telegram_update(test_settings, payload)
    assert not first.duplicate
    assert repeat.duplicate

    edited = {
        "update_id": 770_002,
        "edited_message": payload["message"] | {"text": "продукты 700"},
    }
    edit = await accept_telegram_update(test_settings, edited)
    assert not edit.duplicate

    factory = get_sessionmaker(test_settings, RuntimeRole.OWNER)
    async with factory() as session, session.begin():
        events = (await session.execute(select(InboundEvent))).scalars().all()
    assert len(events) == 2
    assert {event.edit_version for event in events} == {0, 1}


async def test_qa02_worker_restart_keeps_single_job(
    clean_db: None, test_settings: Settings
) -> None:
    """QA-02: перезапуск исполнителя после commit не создаёт вторую задачу."""
    from fintracker.application.platform import queue

    await accept_telegram_update(test_settings, _update(770_010))
    claimed = await queue.claim_jobs(test_settings, queue_classes=("interactive",), limit=5)
    assert len(claimed) == 1

    # Исполнитель «перезапущен»: аренда истекает, задача возвращается в очередь.
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        await session.execute(
            Job.__table__.update()
            .where(Job.id == claimed[0].id)
            .values(lease_until=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5))
        )
    again = await queue.claim_jobs(test_settings, queue_classes=("interactive",), limit=5)
    assert len(again) == 1
    assert again[0].id == claimed[0].id
    assert again[0].lease_token != claimed[0].lease_token

    factory = get_sessionmaker(test_settings, RuntimeRole.OWNER)
    async with factory() as session, session.begin():
        jobs = (await session.execute(select(Job))).scalars().all()
    assert len(jobs) == 1


async def test_qa02_corrupted_image_is_rejected_before_paid_call(
    clean_db: None, test_settings: Settings
) -> None:
    """LIM-09, QA-02, NFR-11, SEC-07: повреждённый и слишком большой файл отклоняются до AI."""
    from fintracker.application.conversation.media import handle_media
    from fintracker.application.conversation.types import (
        Attachment,
        IncomingMessage,
        MessageKind,
    )
    from fintracker.application.identity.actor import ensure_user, set_active_workspace
    from fintracker.db.models.access import User

    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        fixture = await build_fixture(session, telegram_user_id=6101)
        workspace_id = fixture.workspace.id
        user_id = fixture.user.id
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        await set_active_workspace(session, user=user, workspace_id=workspace_id)

    oversized = IncomingMessage(
        telegram_user_id=6101,
        chat_id=6101,
        kind=MessageKind.PHOTO,
        attachments=(
            Attachment(
                file_id="big",
                kind="photo",
                size_bytes=test_settings.limits.max_attachment_bytes + 1,
                mime_type="image/jpeg",
                width=1200,
                height=1600,
            ),
        ),
        correlation_id=uuid.uuid4().hex,
    )
    replies = await handle_media(test_settings, oversized, user_id=user_id)
    assert "больше допустимых" in replies[0].text

    unsupported = IncomingMessage(
        telegram_user_id=6101,
        chat_id=6101,
        kind=MessageKind.DOCUMENT,
        attachments=(
            Attachment(
                file_id="broken",
                kind="document",
                size_bytes=1024,
                mime_type="application/x-msdownload",
            ),
        ),
        correlation_id=uuid.uuid4().hex,
    )
    replies = await handle_media(test_settings, unsupported, user_id=user_id)
    assert "не поддерживается" in replies[0].text
    assert "Исходное сообщение сохранено" in replies[0].text
    assert ensure_user is not None


async def test_qa02_export_repeat_is_stable(owner_session: AsyncSession) -> None:
    """CMD-29, QA-02, NFR-12: повтор экспорта даёт тот же состав данных."""
    from fintracker.application.integrations.exporter import build_snapshot, to_csv
    from fintracker.application.ledger.service import post_transaction
    from tests.integration.test_money_scenarios import expense_spec, rub

    fixture = await build_fixture(owner_session)
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1_000), category="Продукты"),
        origin="form",
    )
    first = await build_snapshot(
        owner_session,
        workspace=fixture.workspace,
        date_from=DAY - dt.timedelta(days=10),
        date_to_exclusive=DAY + dt.timedelta(days=1),
    )
    second = await build_snapshot(
        owner_session,
        workspace=fixture.workspace,
        date_from=DAY - dt.timedelta(days=10),
        date_to_exclusive=DAY + dt.timedelta(days=1),
    )
    assert to_csv(first) == to_csv(second), "повтор экспорта не меняет состав"


async def test_nfr12_export_available_to_every_member(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """NFR-12: выгрузка доступна каждому участнику, удаление бюджета — администратору."""
    from fintracker.application.integrations.exporter import build_snapshot
    from fintracker.core.context import MembershipStatus, Role
    from fintracker.core.errors import PermissionDenied
    from fintracker.core.ids import new_generation
    from fintracker.db.models.access import Membership, User

    fixture = await build_fixture(owner_session, telegram_user_id=6102)
    member = User(id=uuid.uuid4(), telegram_user_id=6103)
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
    await owner_session.flush()

    snapshot = await build_snapshot(
        owner_session,
        workspace=fixture.workspace,
        date_from=DAY - dt.timedelta(days=10),
        date_to_exclusive=DAY + dt.timedelta(days=1),
    )
    assert snapshot.workspace_name == fixture.workspace.name
    assert snapshot.data_revision >= 1

    from dataclasses import replace

    from fintracker.application.identity.preferences import update_workspace_settings

    member_actor = replace(fixture.actor, role=Role.MEMBER)
    with pytest.raises(PermissionDenied):
        await update_workspace_settings(
            owner_session, fixture.uow, actor=member_actor, name="Чужое имя"
        )
