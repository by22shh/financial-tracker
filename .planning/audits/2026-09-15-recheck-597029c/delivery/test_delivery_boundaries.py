"""Independent delivery boundary probes against 597029c.

Real PostgreSQL/runtime roles. Barriers replace only the scheduling boundary
after rendering, never authorization, queue logic or membership operations.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select, update

from fintracker.application.delivery import render
from fintracker.application.identity.membership import delete_workspace, remove_member
from fintracker.application.identity.security_change import resume_or_quarantine, run_security_change
from fintracker.application.ledger.service import post_transaction
from fintracker.application.platform import queue
from fintracker.db.models.access import Membership, User, Workspace
from fintracker.db.models.platform import Job, NotificationDelivery, OutboxEvent
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.infra.telegram.sender import RecordingSender, set_sender_override
from fintracker.infra.security_log import FilesystemSecurityLog, SecurityLog
from fintracker.runtime.worker import _run_with_lease, build_registry
from tests.integration.factories import build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub
from tests.readiness.test_async_readiness import prepare_delivery


@pytest.fixture(autouse=True)
def external_calls_are_recorded():
    set_sender_override(RecordingSender())
    yield
    set_sender_override(None)


async def prepare_financial_delivery(settings, owner_session):
    fixture = await build_fixture(owner_session)
    user = User(id=uuid.uuid4(), telegram_user_id=888077001)
    owner_session.add(user)
    await owner_session.flush()
    generation = uuid.uuid4()
    owner_session.add(Membership(
        workspace_id=fixture.workspace.id, user_id=user.id,
        role="member", status="active", generation=generation,
    ))
    await owner_session.flush()
    await post_transaction(
        owner_session, fixture.uow, actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(450), category="Продукты"), origin="form",
    )
    await owner_session.commit()
    async with session_scope(settings, RuntimeRole.OWNER) as session:
        event = (await session.scalars(select(OutboxEvent).where(
            OutboxEvent.workspace_id == fixture.workspace.id,
            OutboxEvent.event_type == "TransactionPosted",
        ))).one()
        delivery = NotificationDelivery(
            event_id=event.id, workspace_id=fixture.workspace.id,
            recipient_user_id=user.id, membership_generation=generation,
            channel="telegram", delivery_class="shared_change", state="pending",
            available_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
        )
        session.add(delivery)
        await session.flush()
        delivery_id = delivery.id
        await queue.enqueue(
            session, job_type="deliver_notification", logical_key="financial-delivery",
            workspace_id=fixture.workspace.id, payload={"event_id": str(event.id)},
        )
    job = (await queue.claim_jobs(settings, queue_classes=("interactive",)))[0]
    return fixture, user, job, delivery_id


@pytest.mark.parametrize("access_change", ["remove_member", "delete_workspace"])
async def test_completed_access_change_during_render_prevents_send(
    owner_session, test_settings, tmp_path, monkeypatch, access_change,
):
    settings = test_settings.model_copy(deep=True)
    settings.security_log.root = tmp_path / "security-log"
    fixture, member, job, delivery_id = await prepare_financial_delivery(settings, owner_session)
    original = render.render_event
    observations = []

    async def finish_access_change_after_render(*args, **kwargs):
        result = await original(*args, **kwargs)
        assert result[0] and "450" in result[0], "Must render real financial information"
        if access_change == "remove_member":
            await remove_member(
                settings, workspace_id=fixture.workspace.id,
                admin_user_id=fixture.user.id, target_user_id=member.id,
                correlation_id="revoke-during-render",
            )
        else:
            await delete_workspace(
                settings, workspace_id=fixture.workspace.id,
                admin_user_id=fixture.user.id, confirmation_name=fixture.workspace.name,
                correlation_id="delete-during-render",
            )
        async with session_scope(settings, RuntimeRole.OWNER) as session:
            workspace = await session.get(Workspace, fixture.workspace.id)
            membership = (await session.scalars(select(Membership).where(
                Membership.workspace_id == fixture.workspace.id,
                Membership.user_id == member.id,
            ))).one()
            delivery = await session.get(NotificationDelivery, delivery_id)
            observations.append((workspace.security_fence, workspace.state,
                                 membership.status, delivery.state))
        assert observations[-1][0] is None, "Security change must have completely finished"
        assert observations[-1][3] == "cancelled", "Real domain command cancels delivery"
        return result

    monkeypatch.setattr(render, "render_event", finish_access_change_after_render)
    sender = RecordingSender()
    set_sender_override(sender)
    await _run_with_lease(settings, job, build_registry())
    async with session_scope(settings, RuntimeRole.OWNER) as session:
        after_state = (await session.get(NotificationDelivery, delivery_id)).state
    assert sender.sent == [], (
        f"Financial data sent after completed {access_change}: "
        f"before_send={observations}; after_delivery_state={after_state}; messages={sender.sent}"
    )


async def test_fence_surviving_two_attempts_keeps_delivery_runnable(owner_session, test_settings):
    fixture, job, delivery_id = await prepare_delivery(test_settings, owner_session)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        await session.execute(update(Workspace).where(
            Workspace.id == fixture.workspace.id,
        ).values(security_fence=uuid.uuid4()))
    await _run_with_lease(test_settings, job, build_registry())
    second = await queue.claim_jobs(test_settings, queue_classes=("interactive",), limit=10)
    assert second, "First retry must exist"
    for retry in second:
        await _run_with_lease(test_settings, retry, build_registry())
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        await session.execute(update(Workspace).where(
            Workspace.id == fixture.workspace.id,
        ).values(security_fence=None))
        state = (await session.get(NotificationDelivery, delivery_id)).state
        jobs = (await session.scalars(select(Job).where(
            Job.job_type == "deliver_notification",
        ))).all()
        states = [(row.logical_key, row.state) for row in jobs]
    assert state == "pending"
    runnable = await queue.claim_jobs(test_settings, queue_classes=("interactive",), limit=10)
    assert runnable, f"Unsent delivery stranded after two fenced attempts: {states}"


async def test_reconciliation_retries_after_concurrent_committed_access_change(
    owner_session, test_settings, tmp_path, monkeypatch,
):
    settings = test_settings.model_copy(deep=True)
    settings.security_log.root = tmp_path / "security-log"
    fixture, member, job, delivery_id = await prepare_financial_delivery(settings, owner_session)
    journal = SecurityLog(FilesystemSecurityLog(settings.security_log.root))

    async def baseline(session, uow, workspace):
        return {}

    await run_security_change(
        settings, workspace_id=fixture.workspace.id, kind="workspace_create",
        initiated_by=fixture.user.id, apply=baseline, correlation_id="baseline",
        security_log=journal,
    )
    original = journal.last_committed

    async def concurrent_change_after_journal_read(workspace_id):
        old = await original(workspace_id)
        assert old is not None
        await remove_member(
            settings, workspace_id=fixture.workspace.id,
            admin_user_id=fixture.user.id, target_user_id=member.id,
            correlation_id="concurrent-api-remove",
        )
        return old

    monkeypatch.setattr(journal, "last_committed", concurrent_change_after_journal_read)
    await resume_or_quarantine(settings, fixture.workspace.id, security_log=journal)
    actual_journal = await original(fixture.workspace.id)
    async with session_scope(settings, RuntimeRole.OWNER) as session:
        workspace = await session.get(Workspace, fixture.workspace.id)
        assert actual_journal.proposed_acl_revision == workspace.acl_revision
        assert workspace.security_fence is None
        assert not workspace.quarantined, (
            f"Healthy budget quarantined after live API change: "
            f"database={workspace.acl_revision}; journal={actual_journal.proposed_acl_revision}"
        )
