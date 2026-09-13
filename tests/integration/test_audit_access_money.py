"""Регрессии аудита по доступу, деньгам и очистке (AUD-05, 06, 09, 15, 18)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select, update

from fintracker.application.conversation.service import handle
from fintracker.application.conversation.types import IncomingMessage, MessageKind
from fintracker.application.identity.actor import resolve_actor
from fintracker.application.identity.membership import delete_workspace, remove_member
from fintracker.application.identity.security_change import resume_or_quarantine
from fintracker.application.ledger.operations import post_refund, refundable_parts
from fintracker.application.ledger.service import post_transaction, void_transaction
from fintracker.application.maintenance.retention import handle_retention_sweep
from fintracker.config import Settings
from fintracker.core.errors import DomainError
from fintracker.db.models.access import BudgetDeletionRecord, Membership, User, Workspace
from fintracker.db.session import RuntimeRole, session_scope
from tests.conftest import requires_pg
from tests.integration.factories import TZ, build_fixture
from tests.integration.test_deep_audit import prepared, tx_count
from tests.integration.test_money_scenarios import DAY, expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]


async def test_aud05_bad_delete_confirmation_does_not_fence_budget(
    owner_session, test_settings: Settings
) -> None:
    """AUD-05: опечатка в подтверждении не оставляет бюджет заблокированным."""
    fixture = await prepared(owner_session)
    with pytest.raises(DomainError):
        await delete_workspace(
            test_settings,
            workspace_id=fixture.workspace.id,
            admin_user_id=fixture.user.id,
            confirmation_name="неверное название",
            correlation_id="audit-bad-confirmation",
        )
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        fence = await session.scalar(
            select(Workspace.security_fence).where(Workspace.id == fixture.workspace.id)
        )
    assert fence is None, "fence снят: последующие изменения бюджета не заблокированы"


async def test_aud06_completed_revoke_survives_restored_old_acl(
    owner_session, test_settings: Settings
) -> None:
    """AUD-06: завершённый отзыв доступа переживает восстановление старых строк."""
    fixture = await prepared(owner_session)
    generation = uuid.uuid4()
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        user = User(id=uuid.uuid4(), telegram_user_id=880023456)
        session.add(user)
        await session.flush()
        session.add(
            Membership(
                workspace_id=fixture.workspace.id,
                user_id=user.id,
                role="member",
                status="active",
                generation=generation,
            )
        )
    await remove_member(
        test_settings,
        workspace_id=fixture.workspace.id,
        admin_user_id=fixture.user.id,
        target_user_id=user.id,
        correlation_id="audit-restore",
    )
    # Восстановление старого снимка строк доступа при сохранённом журнале.
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        await session.execute(
            update(Membership)
            .where(
                Membership.workspace_id == fixture.workspace.id,
                Membership.user_id == user.id,
            )
            .values(status="active", rejoin_blocked=False, generation=generation)
        )
        await session.execute(
            update(Workspace)
            .where(Workspace.id == fixture.workspace.id)
            .values(acl_revision=1, security_fence=None, quarantined=False)
        )

    pending = await resume_or_quarantine(test_settings, fixture.workspace.id)
    accessible = False
    async with session_scope(
        test_settings, RuntimeRole.API, user_id=user.id, workspace_id=fixture.workspace.id
    ) as session:
        try:
            actor = await resolve_actor(session, user=user, workspace_id=fixture.workspace.id)
            accessible = actor.workspace_id == fixture.workspace.id
        except DomainError:
            accessible = False
    assert not accessible, f"восстановленное членство не даёт доступ; pending={pending}"


async def test_aud09_void_refund_restores_refundable_amount(owner_session) -> None:
    """AUD-09: отменённый возврат возвращает доступную сумму возврата."""
    fixture = await build_fixture(owner_session)
    purchase = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1_000), category="Продукты"),
        origin="form",
    )
    parts = await refundable_parts(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=purchase.transaction_id,
    )
    refund = await post_refund(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        source_transaction_id=purchase.transaction_id,
        parts={parts[0].stable_line_id: rub(1_000)},
        occurred_date=DAY,
        timezone=TZ,
    )
    await void_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        transaction_id=refund.transaction_id,
        expected_version=refund.entity_version,
    )
    remaining = await refundable_parts(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=purchase.transaction_id,
    )
    assert sum(item.refundable_minor for item in remaining) == 100_000


async def test_aud15_deleted_budget_is_purged_after_deadline(
    owner_session, test_settings: Settings
) -> None:
    """AUD-15: финансовые данные удалённого бюджета удаляются в срок (ТЗ §24)."""
    fixture = await build_fixture(owner_session)
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1_000), category="Продукты"),
        origin="form",
    )
    await owner_session.commit()
    await delete_workspace(
        test_settings,
        workspace_id=fixture.workspace.id,
        admin_user_id=fixture.user.id,
        confirmation_name=fixture.workspace.name,
        correlation_id="audit-delete",
    )
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        await session.execute(
            update(Workspace)
            .where(Workspace.id == fixture.workspace.id)
            .values(deleted_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=2))
        )
        await session.execute(
            update(BudgetDeletionRecord)
            .where(BudgetDeletionRecord.workspace_id == fixture.workspace.id)
            .values(purge_after=dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
        )
    await handle_retention_sweep(test_settings, None)
    assert await tx_count(test_settings, fixture) == 0

    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        record = (
            await session.execute(
                select(BudgetDeletionRecord).where(
                    BudgetDeletionRecord.workspace_id == fixture.workspace.id
                )
            )
        ).scalar_one()
    assert record.purged_at is not None, "tombstone фиксирует завершённую очистку"


async def test_aud18_quarantined_budget_hides_financial_history(
    owner_session, test_settings: Settings
) -> None:
    """AUD-18: карантин закрывает чтение финансовой истории."""
    fixture = await build_fixture(owner_session)
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(98_765), category="Продукты", note="ЛИЧНАЯ_ЗАМЕТКА"),
        origin="form",
    )
    await owner_session.execute(
        update(Workspace).where(Workspace.id == fixture.workspace.id).values(quarantined=True)
    )
    await owner_session.commit()

    replies = await handle(
        test_settings,
        IncomingMessage(
            telegram_user_id=fixture.user.telegram_user_id,
            chat_id=fixture.user.telegram_user_id,
            kind=MessageKind.COMMAND,
            text="/history",
            workspace_id=fixture.workspace.id,
        ),
    )
    text = "\n".join(reply.text for reply in replies)
    digits = "".join(char for char in text if char.isdecimal())
    assert "98765" not in digits
    assert "Продукты" not in text
    assert "ЛИЧНАЯ_ЗАМЕТКА" not in text
