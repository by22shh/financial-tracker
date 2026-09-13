"""Second audit pass. Integration regressions against unchanged source.
isolated_inbox_context fixes ONLY inbox visibility in the test to expose
downstream defects masked by AUD-01. No production source is changed.
"""
from __future__ import annotations
import asyncio
import datetime as dt
import uuid
import pytest
from sqlalchemy import select,func,update
from fintracker.application.ingestion import process_event
from fintracker.application.platform import queue
from fintracker.db.session import session_scope,RuntimeRole,set_rls_context
from fintracker.db.models.access import User,Membership
from fintracker.db.models.catalog import Category
from fintracker.db.models.ledger import Transaction
from fintracker.db.models.platform import InboundEvent,Job
from fintracker.infra.telegram.sender import RecordingSender,set_sender_override
from fintracker.infra.ai.openai_client import ScriptedAIProvider,set_provider_override
from fintracker.application.conversation.service import record_free_text
from fintracker.application.conversation.types import IncomingMessage,MessageKind
from tests.integration import test_deep_audit as initial
from tests.integration.test_ai_contract import extraction_json

@pytest.fixture
def isolated_inbox_context(monkeypatch):
    original=process_event._load_event
    async def loader(session,event_id):
        event=await session.get(InboundEvent,event_id)
        await set_rls_context(session,user_id=event.actor_user_id,workspace_id=event.workspace_id)
        return await original(session,event_id)
    monkeypatch.setattr(process_event,"_load_event",loader)

async def test_actual_worker_routes_accepted_start(owner_session,test_settings):
    f=await initial.prepared(owner_session)
    job=await initial.leased(test_settings,initial.incoming(f,"/start"))
    sender=RecordingSender()
    set_sender_override(sender)
    try:
        await process_event.handle_process_inbound_event(test_settings,job)
        async with session_scope(test_settings,RuntimeRole.OWNER) as s:
            state=await s.scalar(select(InboundEvent.state).where(InboundEvent.id==job.subject_id))
        assert sender.sent and state=="processed",f"No reply, state={state}; supported Update lost"
    finally:
        set_sender_override(None)

async def test_actual_scheduler_discovers_active_budgets(owner_session,test_settings):
    from fintracker.runtime.scheduler import schedule_tick
    f=await initial.prepared(owner_session)
    await schedule_tick(test_settings)
    async with session_scope(test_settings,RuntimeRole.OWNER) as s:
        jobs=(await s.scalars(select(Job.job_type).where(Job.workspace_id==f.workspace.id))).all()
    assert "open_next_period" in jobs and "payment_reminders" in jobs,f"Active workspace jobs: {jobs}"

async def test_ai_latency_within_provider_timeout_does_not_kill_transaction(owner_session,test_settings):
    f=await initial.prepared(owner_session)
    configured=test_settings.model_copy(deep=True)
    configured.ai.enabled=True
    class SlowProvider(ScriptedAIProvider):
        async def structured(self,**kwargs):
            await asyncio.sleep(11)
            return await super().structured(**kwargs)
    provider=SlowProvider(responses=[extraction_json(candidate={"category_id":str(f.categories["Продукты"]),"date_expression":None})])
    set_provider_override(provider)
    try:
        replies=await record_free_text(configured,actor=f.actor,workspace=f.workspace,
            message=IncomingMessage(telegram_user_id=f.user.telegram_user_id,chat_id=f.user.telegram_user_id,
                kind=MessageKind.TEXT,text="продукты 450",received_at=dt.datetime.now(dt.UTC)))
        assert replies
    finally:
        set_provider_override(None)

async def test_downstream_retry_no_duplicate(owner_session,test_settings,isolated_inbox_context):
    await initial.test_audit_restart_after_post_does_not_duplicate(owner_session,test_settings)

async def test_downstream_stale_lease_cannot_post(owner_session,test_settings,isolated_inbox_context):
    await initial.test_audit_stale_lease_cannot_post(owner_session,test_settings)

async def test_downstream_failed_reply_is_retried(owner_session,test_settings,isolated_inbox_context):
    await initial.test_audit_failed_immediate_reply_remains_retryable(owner_session,test_settings)

async def test_downstream_group_is_private(owner_session,test_settings,isolated_inbox_context):
    await initial.test_audit_group_message_does_not_disclose_private_budgets(owner_session,test_settings)

async def test_edit_versions_accept_third_update(owner_session,test_settings):
    await initial.test_audit_multiple_edits_are_accepted(owner_session,test_settings)

async def test_transfer_semantics_survive_confirmation(owner_session,test_settings):
    await initial.test_audit_transfer_is_not_converted_into_expense(owner_session,test_settings)

@pytest.mark.parametrize("button",["exp:xlsx","exp:csv","imp:start"])
async def test_import_export_actions(owner_session,test_settings,button):
    await initial.test_audit_export_import_buttons_are_wired(owner_session,test_settings,button)

async def test_expired_lease_is_invalid(owner_session,test_settings):
    await initial.test_audit_expired_lease_is_invalid_without_reclaim(owner_session,test_settings)

async def test_removed_member_cannot_finish_inflight_write(owner_session,test_settings):
    from fintracker.application.identity.actor import resolve_actor
    from fintracker.application.identity.membership import remove_member
    from fintracker.application.conversation.category_flow import create_category_from_text
    from fintracker.core.errors import DomainError
    f=await initial.prepared(owner_session)
    async with session_scope(test_settings,RuntimeRole.OWNER) as s:
        user=User(id=uuid.uuid4(),telegram_user_id=880012345)
        s.add(user)
        await s.flush()
        membership=Membership(workspace_id=f.workspace.id,user_id=user.id,role="member",status="active",generation=uuid.uuid4())
        s.add(membership)
        await s.flush()
        old_actor=await resolve_actor(s,user=user,workspace_id=f.workspace.id)
    await remove_member(test_settings,workspace_id=f.workspace.id,admin_user_id=f.user.id,target_user_id=user.id,correlation_id="audit-revoke")
    try:
        await create_category_from_text(test_settings,actor=old_actor,workspace=f.workspace,text="Создай категорию AFTER_REMOVAL")
    except DomainError:
        pass
    async with session_scope(test_settings,RuntimeRole.OWNER) as s:
        n=await s.scalar(select(func.count()).select_from(Category).where(Category.workspace_id==f.workspace.id,Category.name=="AFTER_REMOVAL"))
    assert n==0,"Removed member writes after SecurityChange completed using stale ActorContext"

