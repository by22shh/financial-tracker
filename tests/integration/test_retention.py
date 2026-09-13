"""Сроки хранения и очистка (TZ §24, RET-01…RET-10, A109)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.ingestion.accept_update import accept_telegram_update
from fintracker.application.ledger.service import post_transaction
from fintracker.application.maintenance.retention import (
    expire_drafts,
    sweep_attachments,
    sweep_draft_sources,
    sweep_exports,
    sweep_inbound_payloads,
    sweep_staging_attachments,
    sweep_stale_deliveries,
)
from fintracker.config import Settings
from fintracker.db.models.ledger import Transaction, TransactionRevision
from fintracker.db.models.platform import (
    Attachment,
    Draft,
    ExportFile,
    InboundPayload,
)
from fintracker.db.session import RuntimeRole, get_sessionmaker
from tests.conftest import requires_pg
from tests.integration.factories import build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]

NOW = dt.datetime(2026, 9, 12, 12, 0, tzinfo=dt.UTC)


async def test_ret08_raw_payload_is_deleted_after_term(
    clean_db: None, test_settings: Settings
) -> None:
    """RET-05, RET-08: сырой payload входящего события удаляется по сроку."""
    await accept_telegram_update(
        test_settings,
        {
            "update_id": 990001,
            "message": {
                "message_id": 1,
                "chat": {"id": 6001, "type": "private"},
                "from": {"id": 6001, "is_bot": False},
                "text": "кофе 250",
            },
        },
    )
    factory = get_sessionmaker(test_settings, RuntimeRole.OWNER)
    async with factory() as session, session.begin():
        payloads = (await session.execute(select(InboundPayload))).scalars().all()
        assert len(payloads) == 1
        payloads[0].delete_after = NOW - dt.timedelta(days=1)

    async with factory() as session, session.begin():
        removed = await sweep_inbound_payloads(session, NOW)
    assert removed == 1

    async with factory() as session, session.begin():
        remaining = (await session.execute(select(InboundPayload))).scalars().all()
    assert remaining == []


async def test_ret08_draft_sources_cleared_but_transaction_remains(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """RET-01, RET-02, RET-03, RET-08: сырой текст и транскрипт удаляются.

    Проведённая операция при этом остаётся в общей истории.
    """
    fixture = await build_fixture(owner_session)
    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(250), category="Продукты", note="Нужная заметка"),
        origin="telegram_text",
    )
    draft = Draft(
        workspace_id=fixture.workspace.id,
        owner_user_id=fixture.user.id,
        source_kind="voice",
        state="posted",
        raw_text="исходный текст",
        transcript="распознанная речь",
        expires_at=NOW + dt.timedelta(days=7),
        delete_raw_after=NOW - dt.timedelta(days=1),
    )
    owner_session.add(draft)
    await owner_session.flush()

    cleared = await sweep_draft_sources(owner_session, NOW)
    assert cleared == 1
    await owner_session.refresh(draft)
    assert draft.raw_text is None
    assert draft.transcript is None

    revision = (
        await owner_session.execute(
            select(TransactionRevision).where(
                TransactionRevision.transaction_id == posted.transaction_id
            )
        )
    ).scalar_one()
    # Структурированная операция и выбранная заметка остаются (RET-08).
    assert revision.note == "Нужная заметка"
    assert revision.amount_minor == 25_000


async def test_expired_draft_never_becomes_posted(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """LIM-05, FR-20: срок не превращает черновик в подтверждённый расход."""
    fixture = await build_fixture(owner_session)
    draft = Draft(
        workspace_id=fixture.workspace.id,
        owner_user_id=fixture.user.id,
        source_kind="text",
        state="needs_clarification",
        expires_at=NOW - dt.timedelta(days=1),
        delete_raw_after=NOW + dt.timedelta(days=7),
    )
    owner_session.add(draft)
    await owner_session.flush()

    expired = await expire_drafts(owner_session, NOW)
    assert expired == 1
    await owner_session.refresh(draft)
    assert draft.state == "expired"

    posted = (
        (
            await owner_session.execute(
                select(Transaction).where(Transaction.workspace_id == fixture.workspace.id)
            )
        )
        .scalars()
        .all()
    )
    assert posted == [], "истёкший черновик не создал расход"


async def test_a109_attachment_removed_but_operation_remains(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """ADR-11, RET-04, SEC-05, A109: серверная копия удалена по сроку.

    Сама операция при этом остаётся.
    """
    fixture = await build_fixture(owner_session)
    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(250), category="Продукты"),
        origin="telegram_photo",
    )
    attachment = Attachment(
        workspace_id=fixture.workspace.id,
        owner_user_id=fixture.user.id,
        visibility="workspace",
        kind="photo",
        content_type="image/jpeg",
        size_bytes=1024,
        checksum_sha256="a" * 64,
        storage_key="receipts/test-object",
        state="ready",
        transaction_id=posted.transaction_id,
        delete_after=NOW - dt.timedelta(days=1),
    )
    owner_session.add(attachment)
    await owner_session.flush()

    from fintracker.infra.storage import build_storage

    storage = build_storage(test_settings.storage)
    await storage.put("receipts/test-object", b"binary")

    removed = await sweep_attachments(owner_session, test_settings, NOW)
    assert removed == 1
    await owner_session.refresh(attachment)
    assert attachment.state == "deleted"
    assert await storage.get("receipts/test-object") is None

    transaction = (
        await owner_session.execute(
            select(Transaction).where(Transaction.id == posted.transaction_id)
        )
    ).scalar_one()
    assert transaction.status == "posted", "операция и её след сохранены"


async def test_ret10_staging_objects_cleaned_after_day(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """RET-09, RET-10: непривязанные staging объекты очищаются через 24 часа."""
    fixture = await build_fixture(owner_session)
    attachment = Attachment(
        workspace_id=fixture.workspace.id,
        owner_user_id=fixture.user.id,
        visibility="owner",
        kind="photo",
        content_type="image/jpeg",
        size_bytes=1024,
        checksum_sha256="b" * 64,
        storage_key="staging/object",
        state="staging",
        delete_after=NOW + dt.timedelta(days=7),
    )
    owner_session.add(attachment)
    await owner_session.flush()
    await owner_session.execute(
        Attachment.__table__.update()
        .where(Attachment.id == attachment.id)
        .values(created_at=NOW - dt.timedelta(hours=30))
    )
    marked = await sweep_staging_attachments(owner_session, NOW)
    assert marked == 1


async def test_ret07_export_file_expires(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """RET-07: файл экспорта живёт 24 часа в серверном хранилище."""
    fixture = await build_fixture(owner_session)
    export = ExportFile(
        workspace_id=fixture.workspace.id,
        requested_by=fixture.user.id,
        fmt="csv",
        data_revision=1,
        storage_key="exports/file.csv",
        state="ready",
        delete_after=NOW - dt.timedelta(hours=1),
    )
    owner_session.add(export)
    await owner_session.flush()
    removed = await sweep_exports(owner_session, NOW)
    assert removed == 1
    await owner_session.refresh(export)
    assert export.state == "deleted"
    assert export.storage_key is None


async def test_stale_delivery_is_cancelled_not_retried_forever(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """ADR-05: просроченная доставка отменяется, а не повторяется бесконечно."""
    from fintracker.db.models.platform import NotificationDelivery, OutboxEvent

    fixture = await build_fixture(owner_session)
    event = OutboxEvent(
        workspace_id=fixture.workspace.id,
        # Номер события берётся после уже созданных мастером записей.
        event_seq=fixture.workspace.event_seq + 100,
        aggregate_type="transaction",
        event_type="TransactionPosted",
        payload={},
        correlation_id="test",
    )
    owner_session.add(event)
    await owner_session.flush()
    delivery = NotificationDelivery(
        event_id=event.id,
        workspace_id=fixture.workspace.id,
        recipient_user_id=fixture.user.id,
        membership_generation=uuid.uuid4(),
        delivery_class="shared_change",
        state="failed",
        expires_at=NOW - dt.timedelta(hours=1),
    )
    owner_session.add(delivery)
    await owner_session.flush()

    cancelled = await sweep_stale_deliveries(owner_session, NOW)
    assert cancelled == 1
    await owner_session.refresh(delivery)
    assert delivery.state == "cancelled"
