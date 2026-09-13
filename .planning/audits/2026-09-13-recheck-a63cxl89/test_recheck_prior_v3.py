"""Additional money, access and retention repros; unchanged application source."""
from __future__ import annotations
import datetime as dt
import uuid
from dataclasses import replace
import pytest
from sqlalchemy import select,update,func
from fintracker.application.conversation.service import handle
from fintracker.application.conversation.types import IncomingMessage,MessageKind
from fintracker.application.identity.membership import delete_workspace,remove_member
from fintracker.application.identity.security_change import resume_or_quarantine
from fintracker.application.identity.actor import resolve_actor
from fintracker.application.ledger.service import post_transaction,void_transaction
from fintracker.application.ledger.operations import post_refund,refundable_parts
from fintracker.application.maintenance.retention import handle_retention_sweep
from fintracker.db.session import session_scope,RuntimeRole
from fintracker.db.models.access import Workspace,Membership,User,BudgetDeletionRecord
from fintracker.db.models.platform import Draft
from fintracker.core.errors import DomainError
from tests.integration.factories import build_fixture,TZ
from tests.integration.test_money_scenarios import expense_spec,rub,DAY
from tests.integration._recheck_legacy_support import prepared,tx_count

async def test_bad_delete_confirmation_does_not_fence_budget(owner_session,test_settings):
    f=await prepared(owner_session)
    with pytest.raises(DomainError):
        await delete_workspace(test_settings,workspace_id=f.workspace.id,admin_user_id=f.user.id,
            confirmation_name="incorrect name",correlation_id="audit-bad-confirmation")
    async with session_scope(test_settings,RuntimeRole.OWNER) as s:
        fence=await s.scalar(select(Workspace.security_fence).where(Workspace.id==f.workspace.id))
    assert fence is None,"User typo leaves committed security_fence and permanently blocks writes"

async def test_void_refund_restores_refundable_amount(owner_session,test_settings):
    f=await build_fixture(owner_session)
    purchase=await post_transaction(owner_session,f.uow,actor=f.actor,
        spec=expense_spec(f,amount=rub(1000),category="Продукты"),origin="form")
    parts=await refundable_parts(owner_session,workspace_id=f.workspace.id,transaction_id=purchase.transaction_id)
    refund=await post_refund(owner_session,f.uow,actor=f.actor,source_transaction_id=purchase.transaction_id,
        parts={parts[0].stable_line_id:rub(1000)},occurred_date=DAY,timezone=TZ)
    await void_transaction(owner_session,f.uow,actor=f.actor,transaction_id=refund.transaction_id,expected_version=refund.entity_version)
    remaining=await refundable_parts(owner_session,workspace_id=f.workspace.id,transaction_id=purchase.transaction_id)
    assert sum(p.refundable_minor for p in remaining)==100000,"Voided refund still consumes refund allowance via active TransactionLink"

async def test_runtime_retention_clears_expired_private_draft(owner_session,test_settings):
    f=await build_fixture(owner_session)
    before=dt.datetime.now(dt.UTC)-dt.timedelta(days=8)
    draft=Draft(workspace_id=f.workspace.id,owner_user_id=f.user.id,source_kind="text",state="ready",
        raw_text="AUDIT PRIVATE SOURCE",expires_at=before,delete_raw_after=before)
    owner_session.add(draft)
    await owner_session.flush()
    draft_id=draft.id
    await owner_session.commit()
    await handle_retention_sweep(test_settings,None)
    async with session_scope(test_settings,RuntimeRole.OWNER) as s:
        row=await s.get(Draft,draft_id)
        assert row.raw_text is None and row.state=="expired",f"Expired draft survives: {row.state}, raw retained={bool(row.raw_text)}"

async def test_quarantined_budget_does_not_show_financial_history(owner_session,test_settings):
    f=await build_fixture(owner_session)
    await post_transaction(owner_session,f.uow,actor=f.actor,
        spec=expense_spec(f,amount=rub(98765),category="Продукты",note="AUDIT_PRIVATE_NOTE"),origin="form")
    await owner_session.execute(update(Workspace).where(Workspace.id==f.workspace.id).values(quarantined=True))
    await owner_session.commit()
    replies=await handle(test_settings,IncomingMessage(telegram_user_id=f.user.telegram_user_id,chat_id=f.user.telegram_user_id,
        kind=MessageKind.COMMAND,text="/history",workspace_id=f.workspace.id))
    text="\n".join(r.text for r in replies)
    digits = "".join(char for char in text if char.isdecimal())
    assert "98765" not in digits and "Продукты" not in text and "AUDIT_PRIVATE_NOTE" not in text,f"Quarantine does not protect financial reads: {text}"

async def test_completed_revoke_survives_restored_old_acl(owner_session,test_settings):
    f=await prepared(owner_session)
    generation=uuid.uuid4()
    async with session_scope(test_settings,RuntimeRole.OWNER) as s:
        user=User(id=uuid.uuid4(),telegram_user_id=880023456)
        s.add(user)
        await s.flush()
        s.add(Membership(workspace_id=f.workspace.id,user_id=user.id,role="member",status="active",generation=generation))
    await remove_member(test_settings,workspace_id=f.workspace.id,admin_user_id=f.user.id,target_user_id=user.id,correlation_id="audit-restore")
    # Restore only the access-related rows to their pre-revocation snapshot.
    # Monetary data is irrelevant to this access replay invariant.
    async with session_scope(test_settings,RuntimeRole.OWNER) as s:
        await s.execute(update(Membership).where(Membership.workspace_id==f.workspace.id,Membership.user_id==user.id)
            .values(status="active",rejoin_blocked=False,generation=generation))
        await s.execute(update(Workspace).where(Workspace.id==f.workspace.id).values(acl_revision=1,security_fence=None,quarantined=False))
    pending=await resume_or_quarantine(test_settings,f.workspace.id)
    accessible=False
    async with session_scope(test_settings,RuntimeRole.API,user_id=user.id,workspace_id=f.workspace.id) as s:
        try:
            actor=await resolve_actor(s,user=user,workspace_id=f.workspace.id)
            accessible=actor.workspace_id==f.workspace.id
        except DomainError:
            pass
    assert not accessible,f"Restored old membership is active despite committed revoke; pending={pending}"

async def test_deleted_budget_financial_data_is_purged_after_deadline(owner_session,test_settings):
    f=await build_fixture(owner_session)
    await post_transaction(owner_session,f.uow,actor=f.actor,
        spec=expense_spec(f,amount=rub(1000),category="Продукты"),origin="form")
    await owner_session.commit()
    await delete_workspace(test_settings,workspace_id=f.workspace.id,admin_user_id=f.user.id,
        confirmation_name=f.workspace.name,correlation_id="audit-delete")
    async with session_scope(test_settings,RuntimeRole.OWNER) as s:
        await s.execute(update(Workspace).where(Workspace.id==f.workspace.id).values(deleted_at=dt.datetime.now(dt.UTC)-dt.timedelta(days=2)))
        await s.execute(update(BudgetDeletionRecord).where(BudgetDeletionRecord.workspace_id==f.workspace.id)
            .values(purge_after=dt.datetime.now(dt.UTC)-dt.timedelta(days=1)))
    await handle_retention_sweep(test_settings,None)
    assert await tx_count(test_settings,f)==0,"Deleted workspace retains financial data beyond 24h; no purge handler"

async def test_invite_secret_is_not_retained_in_global_job(owner_session,test_settings):
    from fintracker.application.ingestion.accept_update import accept_telegram_update
    from fintracker.db.models.platform import Job
    from tests.integration.test_deep_audit import incoming
    f=await prepared(owner_session)
    accepted=await accept_telegram_update(test_settings,incoming(f,"/join ABCD-EFGH-IJKL"))
    async with session_scope(test_settings,RuntimeRole.API) as s:
        job=await s.get(Job,accepted.job_id)
        assert not job.payload.get("invite_code"),"Raw invitation secret is stored in a global table readable without workspace context"
