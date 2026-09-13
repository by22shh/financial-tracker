"""Follow-up audit: unchanged application; deterministic failure/concurrency probes."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update, func

from fintracker.application.conversation import service as conversation
from fintracker.application.conversation.types import IncomingMessage, MessageKind
from fintracker.application.identity.membership import remove_member, delete_workspace
from fintracker.application.identity.security_change import resume_or_quarantine
from fintracker.application.ingestion import process_event
from fintracker.application.intelligence.schedule import due_analyses, handle_run_analysis
from fintracker.application.ledger.operations import post_refund, refundable_parts
from fintracker.application.ledger.service import post_transaction, void_transaction, restore_transaction
from fintracker.application.maintenance.retention import handle_retention_sweep
from fintracker.application.platform import queue
from fintracker.core.errors import DomainError
from fintracker.db.models.access import User, Membership, UserBudgetContext, Workspace, BudgetDeletionRecord
from fintracker.db.models.platform import Job, Attachment
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork
from fintracker.infra.ai.openai_client import ScriptedAIProvider, set_provider_override
from fintracker.infra.storage import build_storage
from fintracker.infra.telegram.sender import RecordingSender, set_sender_override
from tests.integration._recheck_legacy_support import prepared, incoming, leased, tx_count
from tests.integration.factories import build_fixture, TZ
from tests.integration.test_money_scenarios import expense_spec, rub, DAY
from tests.integration.test_recommendations import _complete_fixture, recommendation_json


async def test_lease_lost_during_command_cannot_commit(owner_session, test_settings, monkeypatch):
    f = await prepared(owner_session)
    job = await leased(test_settings, incoming(f, "продукты 450"))
    original = process_event.handle

    async def revoke_then_continue(settings, message):
        # Real interleaving: lease is lost AFTER the handler's initial check.
        async with session_scope(settings, RuntimeRole.OWNER) as session:
            await session.execute(update(Job).where(Job.id == job.id).values(lease_token=uuid.uuid4()))
        return await original(settings, message)

    monkeypatch.setattr(process_event, "handle", revoke_then_continue)
    sender = RecordingSender()
    set_sender_override(sender)
    try:
        await process_event.handle_process_inbound_event(test_settings, job)
    finally:
        set_sender_override(None)
    assert await tx_count(test_settings, f) == 0, "Old worker committed money after losing its lease during processing"


async def test_same_event_concurrently_creates_one_expense(owner_session, test_settings, monkeypatch):
    f = await prepared(owner_session)
    job = await leased(test_settings, incoming(f, "продукты 450"))
    message = IncomingMessage(telegram_user_id=f.user.telegram_user_id, chat_id=f.user.telegram_user_id,
        kind=MessageKind.TEXT, text="продукты 450", workspace_id=f.workspace.id, inbound_event_id=job.subject_id)
    original = conversation.create_draft_with_candidates
    arrivals = 0
    barrier = asyncio.Event()

    async def pause_before_create(*args, **kwargs):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            barrier.set()
        await asyncio.wait_for(barrier.wait(), timeout=8)
        return await original(*args, **kwargs)

    monkeypatch.setattr(conversation, "create_draft_with_candidates", pause_before_create)
    await asyncio.wait_for(asyncio.gather(
        conversation.record_free_text(test_settings, actor=f.actor, workspace=f.workspace, message=message),
        conversation.record_free_text(test_settings, actor=f.actor, workspace=f.workspace, message=message),
        return_exceptions=True,
    ), timeout=15)
    assert await tx_count(test_settings, f) == 1, "Two executions of one inbound event created two expenses"


async def test_edited_message_does_not_add_second_expense(owner_session, test_settings):
    f = await prepared(owner_session)
    sender = RecordingSender()
    set_sender_override(sender)
    try:
        original = await leased(test_settings, incoming(f, "продукты 450", update_id=501))
        await process_event.handle_process_inbound_event(test_settings, original)
        edited = await leased(test_settings, incoming(f, "продукты 600", update_id=502, edited=True))
        await process_event.handle_process_inbound_event(test_settings, edited)
    finally:
        set_sender_override(None)
    assert await tx_count(test_settings, f) == 1, "Editing one Telegram message adds a new expense instead of correcting or requesting confirmation"


async def test_restoring_old_refund_cannot_exceed_purchase(owner_session):
    f = await build_fixture(owner_session)
    purchase = await post_transaction(owner_session, f.uow, actor=f.actor,
        spec=expense_spec(f, amount=rub(1000), category="Продукты"), origin="form")
    parts = await refundable_parts(owner_session, workspace_id=f.workspace.id, transaction_id=purchase.transaction_id)
    refund = await post_refund(owner_session, f.uow, actor=f.actor, source_transaction_id=purchase.transaction_id,
        parts={parts[0].stable_line_id: rub(1000)}, occurred_date=DAY, timezone=TZ)
    voided = await void_transaction(owner_session, f.uow, actor=f.actor, transaction_id=refund.transaction_id)
    await post_refund(owner_session, f.uow, actor=f.actor, source_transaction_id=purchase.transaction_id,
        parts={parts[0].stable_line_id: rub(1000)}, occurred_date=DAY, timezone=TZ)
    try:
        await restore_transaction(owner_session, f.uow, actor=f.actor,
            transaction_id=refund.transaction_id, expected_version=voided.entity_version)
    except DomainError:
        return
    remaining = await refundable_parts(owner_session, workspace_id=f.workspace.id, transaction_id=purchase.transaction_id)
    from fintracker.db.models.ledger import Transaction, TransactionRevision
    actual_refunds = await owner_session.scalar(select(func.sum(TransactionRevision.amount_minor))
        .join(Transaction, (Transaction.id == TransactionRevision.transaction_id) &
              (Transaction.current_revision == TransactionRevision.revision))
        .where(Transaction.workspace_id == f.workspace.id, Transaction.status == "posted",
               TransactionRevision.transaction_type == "refund"))
    assert int(actual_refunds or 0) <= 100000, "Restoring an earlier refund permits 2000 RUB of actual refunds for a 1000 RUB purchase"


async def test_new_budget_has_default_weekly_analysis(owner_session):
    f = await build_fixture(owner_session)
    # Sunday 20:00, within an existing period and after the default 19:00 schedule.
    due = await due_analyses(owner_session, workspace_id=f.workspace.id,
        local_now=dt.datetime(2026, 9, 20, 20, 0, tzinfo=ZoneInfo(TZ)))
    assert any(item.run_kind == "weekly_review" for item in due), "No analysis: transient AnalysisPreference has None server defaults"


async def test_background_slow_ai_does_not_break_transaction(owner_session, test_settings):
    f = await _complete_fixture(owner_session)
    await owner_session.commit()
    configured = test_settings.model_copy(deep=True)
    configured.ai.enabled = True

    class SlowProvider(ScriptedAIProvider):
        called = False
        async def structured(self, **kwargs):
            self.called = True
            await asyncio.sleep(11)
            return await super().structured(**kwargs)

    provider = SlowProvider(responses=[recommendation_json(cards=[])])
    async with session_scope(test_settings, RuntimeRole.WORKER) as session:
        await queue.enqueue(session, job_type="run_analysis", logical_key="audit:slow-background", queue_class="review",
            workspace_id=f.workspace.id, payload={"analysis_key":"audit:slow-background", "run_kind":"weekly_review", "analysis_date":"2026-09-25"},
            correlation_id="reaudit")
    jobs = await queue.claim_jobs(test_settings, queue_classes=("review",), limit=1)
    set_provider_override(provider)
    try:
        await handle_run_analysis(configured, jobs[0])
        assert provider.called, "Probe did not reach AI; fixture needs correction"
    finally:
        set_provider_override(None)


async def test_deferred_reply_is_not_sent_after_member_removal(owner_session, test_settings):
    f = await prepared(owner_session)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        member = User(id=uuid.uuid4(), telegram_user_id=880099991)
        session.add(member)
        await session.flush()
        session.add(Membership(workspace_id=f.workspace.id, user_id=member.id, role="member", status="active", generation=uuid.uuid4()))
        session.add(UserBudgetContext(user_id=member.id, workspace_id=f.workspace.id))
        await post_transaction(session, UnitOfWork(session=session, correlation_id="seed"), actor=f.actor,
            spec=expense_spec(f, amount=rub(98765), category="Продукты"), origin="form")
    member_fixture = replace(f, user=member)
    sender = RecordingSender(fail_for_chats={member.telegram_user_id})
    set_sender_override(sender)
    try:
        job = await leased(test_settings, incoming(member_fixture, "/history"))
        await process_event.handle_process_inbound_event(test_settings, job)
        jobs = await queue.claim_jobs(test_settings, queue_classes=("interactive",), limit=20)
        reply_job = next(j for j in jobs if j.job_type == "deliver_reply")
        await remove_member(test_settings, workspace_id=f.workspace.id, admin_user_id=f.user.id,
            target_user_id=member.id, correlation_id="audit-remove")
        delivered = RecordingSender()
        set_sender_override(delivered)
        await process_event.handle_deliver_reply(test_settings, reply_job)
        assert not delivered.sent, "Deferred private financial history delivered after completed membership revocation"
    finally:
        set_sender_override(None)


async def _ready_attachment(session, f, test_settings, *, expired):
    key = "reaudit/" + uuid.uuid4().hex
    row = Attachment(workspace_id=f.workspace.id, owner_user_id=f.user.id, visibility="workspace",
        kind="photo", content_type="image/jpeg", size_bytes=6, checksum_sha256="a"*64,
        storage_key=key, state="ready",
        delete_after=dt.datetime.now(dt.UTC) + dt.timedelta(days=-1 if expired else 30))
    session.add(row)
    await session.flush()
    await build_storage(test_settings.storage).put(key, b"binary")
    return row.id, key


async def test_runtime_retention_deletes_expired_receipt(owner_session, test_settings):
    f = await build_fixture(owner_session)
    attachment_id, _ = await _ready_attachment(owner_session, f, test_settings, expired=True)
    await owner_session.commit()
    await handle_retention_sweep(test_settings, None)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        row = await session.get(Attachment, attachment_id)
        assert row is None or row.state == "deleted", "Global retention still cannot see expired receipt under RLS"


async def test_budget_purge_also_removes_receipts(owner_session, test_settings):
    f = await build_fixture(owner_session)
    attachment_id, _ = await _ready_attachment(owner_session, f, test_settings, expired=False)
    await owner_session.commit()
    await delete_workspace(test_settings, workspace_id=f.workspace.id, admin_user_id=f.user.id,
        confirmation_name=f.workspace.name, correlation_id="delete-with-receipt")
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        await session.execute(update(Workspace).where(Workspace.id==f.workspace.id)
            .values(deleted_at=dt.datetime.now(dt.UTC)-dt.timedelta(days=2)))
        await session.execute(update(BudgetDeletionRecord).where(BudgetDeletionRecord.workspace_id==f.workspace.id)
            .values(purge_after=dt.datetime.now(dt.UTC)-dt.timedelta(days=1)))
    await handle_retention_sweep(test_settings, None)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        row = await session.get(Attachment, attachment_id)
        assert row is None or row.state == "deleted", "Workspace marked purged while receipt metadata/object remain"


async def test_restore_replays_deleted_workspace_state(owner_session, test_settings):
    f = await prepared(owner_session)
    await delete_workspace(test_settings, workspace_id=f.workspace.id, admin_user_id=f.user.id,
        confirmation_name=f.workspace.name, correlation_id="restore-deletion")
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        await session.execute(update(Workspace).where(Workspace.id==f.workspace.id)
            .values(state="active", deleted_at=None, acl_revision=1, security_fence=None, quarantined=False))
    await resume_or_quarantine(test_settings, f.workspace.id)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        row = await session.get(Workspace, f.workspace.id)
        assert row.state in ("deleting", "deleted"), "Committed deletion state is omitted from ACL replay"


async def test_rejected_security_change_does_not_leave_fence(owner_session, test_settings):
    f = await prepared(owner_session)
    with pytest.raises(DomainError):
        await remove_member(test_settings, workspace_id=f.workspace.id, admin_user_id=f.user.id,
            target_user_id=uuid.uuid4(), correlation_id="reject-missing-member")
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        fence = await session.scalar(select(Workspace.security_fence).where(Workspace.id==f.workspace.id))
    assert fence is None, "Expected command rejection still leaves SecurityChange fenced outside delete precheck"


async def test_expired_lease_cannot_be_renewed(owner_session, test_settings):
    f = await prepared(owner_session)
    job = await leased(test_settings, incoming(f, "/start"))
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        await session.execute(update(Job).where(Job.id==job.id)
            .values(lease_until=dt.datetime.now(dt.UTC)-dt.timedelta(seconds=2)))
    assert not await queue.renew_lease(test_settings, job), "Expired lease is renewed without checking its deadline"

async def test_restored_refund_counts_in_remaining_limit(owner_session):
    f = await build_fixture(owner_session)
    purchase = await post_transaction(owner_session, f.uow, actor=f.actor,
        spec=expense_spec(f, amount=rub(1000), category="Продукты"), origin="form")
    parts = await refundable_parts(owner_session, workspace_id=f.workspace.id, transaction_id=purchase.transaction_id)
    refund = await post_refund(owner_session, f.uow, actor=f.actor, source_transaction_id=purchase.transaction_id,
        parts={parts[0].stable_line_id:rub(1000)}, occurred_date=DAY, timezone=TZ)
    voided = await void_transaction(owner_session, f.uow, actor=f.actor, transaction_id=refund.transaction_id)
    await restore_transaction(owner_session, f.uow, actor=f.actor,
        transaction_id=refund.transaction_id, expected_version=voided.entity_version)
    remaining = await refundable_parts(owner_session, workspace_id=f.workspace.id, transaction_id=purchase.transaction_id)
    assert sum(p.refundable_minor for p in remaining) == 0, "Restored refund remains cancelled in TransactionLink, leaving full refund limit available"


async def test_xlsx_message_reaches_import_handler(owner_session, test_settings, monkeypatch):
    from fintracker.application.conversation import io_flow
    from fintracker.application.conversation.types import Attachment as InputAttachment, Reply
    f = await prepared(owner_session)
    reached = False

    async def import_spy(settings, **kwargs):
        nonlocal reached
        reached = True
        return [Reply(text="IMPORT_REACHED")]

    monkeypatch.setattr(io_flow, "handle_table_document", import_spy)
    replies = await conversation.handle(test_settings, IncomingMessage(
        telegram_user_id=f.user.telegram_user_id, chat_id=f.user.telegram_user_id,
        workspace_id=f.workspace.id, kind=MessageKind.DOCUMENT,
        attachments=(InputAttachment(file_id="audit_xlsx", kind="document", size_bytes=100,
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),)))
    assert reached, "XLSX never reaches import handler: " + " | ".join(r.text for r in replies)


async def test_invite_expiring_after_preview_does_not_fence_budget(owner_session, test_settings, monkeypatch):
    from fintracker.application.identity import invites
    from fintracker.db.models.access import BudgetInvite
    f = await prepared(owner_session)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        guest = User(id=uuid.uuid4(), telegram_user_id=880099992)
        session.add(guest)
        await session.flush()
        issued = await invites.issue_invite(session, UnitOfWork(session=session, correlation_id="invite"),
            settings=test_settings, workspace_id=f.workspace.id, created_by=f.user.id)
    original = invites.preview_invite

    async def expire_after_preview(*args, **kwargs):
        result = await original(*args, **kwargs)
        # The invite expires while the user/worker proceeds from preview to commit.
        async with session_scope(test_settings, RuntimeRole.OWNER) as session:
            await session.execute(update(BudgetInvite).where(BudgetInvite.id==issued.invite_id)
                .values(expires_at=dt.datetime.now(dt.UTC)-dt.timedelta(seconds=1)))
        return result

    monkeypatch.setattr(invites, "preview_invite", expire_after_preview)
    with pytest.raises(DomainError):
        await invites.accept_invite(test_settings, user=guest, raw_code=issued.code, correlation_id="expired-join")
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        fence = await session.scalar(select(Workspace.security_fence).where(Workspace.id==f.workspace.id))
    assert fence is None, "Invite expiration after preview leaves the entire budget fenced"
