"""Independent follow-up probes; production code is unchanged.

Real PostgreSQL and runtime roles. Mocks replace only external providers or
place a deterministic barrier at a real scheduling boundary.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import func, select, text, update

from fintracker.application.conversation import entry, service
from fintracker.application.conversation.types import IncomingMessage, MessageKind
from fintracker.application.identity.membership import remove_member
from fintracker.application.ingestion import process_event
from fintracker.application.intelligence import analysis, media_pipeline
from fintracker.application.intelligence.schedule import handle_run_analysis
from fintracker.application.ledger.service import post_transaction
from fintracker.application.platform import queue
from fintracker.core.fencing import execution_fence
from fintracker.core.errors import TemporarilyUnavailable
from fintracker.db.models.access import Membership, User, UserBudgetContext, Workspace
from fintracker.db.models.intelligence import AnalysisRun
from fintracker.db.models.platform import Draft, Job, OutboxEvent
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork
from fintracker.infra.ai.openai_client import ScriptedAIProvider, set_provider_override
from fintracker.infra.telegram.sender import RecordingSender, set_sender_override
from tests.integration.factories import build_fixture
from tests.integration.test_deep_audit import incoming, leased, prepared, tx_count
from tests.integration.test_money_scenarios import expense_spec, rub
from tests.integration.test_recommendations import _complete_fixture, recommendation_json


async def test_both_concurrent_executors_return_a_result(owner_session, test_settings, monkeypatch):
    f = await prepared(owner_session)
    message = IncomingMessage(
        telegram_user_id=f.user.telegram_user_id, chat_id=f.user.telegram_user_id,
        kind=MessageKind.TEXT, text="продукты 450", message_id=551,
        workspace_id=f.workspace.id, inbound_event_id=uuid.uuid4(),
    )
    original = entry.find_message_draft
    arrivals = 0
    barrier = asyncio.Event()

    async def pause_after_lookup(*args, **kwargs):
        nonlocal arrivals
        result = await original(*args, **kwargs)
        if result is None:
            arrivals += 1
            if arrivals == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), 5)
        return result

    # Force both INSERT contenders to observe the same absence. Do not mock
    # database errors or suppress exceptions returned by the two executions.
    monkeypatch.setattr(entry, "find_message_draft", pause_after_lookup)
    outcomes = await asyncio.wait_for(asyncio.gather(
        service.record_free_text(test_settings, actor=f.actor, workspace=f.workspace, message=message),
        service.record_free_text(test_settings, actor=f.actor, workspace=f.workspace, message=message),
        return_exceptions=True,
    ), 12)
    assert await tx_count(test_settings, f) == 1
    failures = [f"{type(item).__name__}: {item}" for item in outcomes if isinstance(item, BaseException)]
    assert not failures, f"One expense exists, but the second executor crashes: {failures}"
    assert all(outcomes), "Both callers must receive the existing result"


async def _member_with_history(owner_session, settings):
    f = await prepared(owner_session)
    async with session_scope(settings, RuntimeRole.OWNER) as session:
        member = User(id=uuid.uuid4(), telegram_user_id=880099881)
        session.add(member)
        await session.flush()
        session.add(Membership(workspace_id=f.workspace.id, user_id=member.id,
                               role="member", status="active", generation=uuid.uuid4()))
        session.add(UserBudgetContext(user_id=member.id, workspace_id=f.workspace.id))
        await post_transaction(session, UnitOfWork(session, "verify3-seed"), actor=f.actor,
            spec=expense_spec(f, amount=rub(98765), category="Продукты"), origin="form")
    return f, member


async def test_immediate_reply_rechecks_revocation(owner_session, test_settings, monkeypatch):
    f, member = await _member_with_history(owner_session, test_settings)
    job = await leased(test_settings, incoming(replace(f, user=member), "/history"))
    original = process_event.handle

    async def remove_after_render(settings, message):
        replies = await original(settings, message)
        assert any("765" in reply.text for reply in replies), "Fixture must reach private history"
        await remove_member(settings, workspace_id=f.workspace.id, admin_user_id=f.user.id,
                            target_user_id=member.id, correlation_id="verify3-revoke")
        return replies

    monkeypatch.setattr(process_event, "handle", remove_after_render)
    sender = RecordingSender()
    set_sender_override(sender)
    try:
        await process_event.handle_process_inbound_event(test_settings, job)
    finally:
        set_sender_override(None)
    exposed = [item for item in sender.sent if item["chat_id"] == member.telegram_user_id and "765" in item["text"]]
    assert not exposed, "Immediate path sends rendered financial history after remove_member has completed"


async def test_expired_lease_cannot_send_deferred_reply(owner_session, test_settings):
    f, member = await _member_with_history(owner_session, test_settings)
    job = await leased(test_settings, incoming(replace(f, user=member), "/history"))
    set_sender_override(RecordingSender(fail_for_chats={member.telegram_user_id}))
    try:
        await process_event.handle_process_inbound_event(test_settings, job)
        claimed = await queue.claim_jobs(test_settings, queue_classes=("interactive",), limit=20)
        reply_job = next(item for item in claimed if item.job_type == "deliver_reply")
        async with session_scope(test_settings, RuntimeRole.OWNER) as session:
            await session.execute(update(Job).where(Job.id == reply_job.id)
                                  .values(lease_until=dt.datetime.now(dt.UTC)-dt.timedelta(seconds=1)))
        sender = RecordingSender()
        set_sender_override(sender)
        async with execution_fence(queue.lease_fence(reply_job)):
            await process_event.handle_deliver_reply(test_settings, reply_job)
        assert not sender.sent, "Execution fence is installed, but deferred delivery never checks it"
    finally:
        set_sender_override(None)


async def test_analysis_resumes_after_process_interruption(owner_session, test_settings):
    f = await _complete_fixture(owner_session)
    configured = test_settings.model_copy(deep=True)
    configured.ai.enabled = True

    class InterruptedProvider(ScriptedAIProvider):
        async def structured(self, **kwargs):
            raise asyncio.CancelledError("Simulated worker interruption after persisted preparation")

    arguments = dict(workspace_id=f.workspace.id, run_kind="weekly_review",
                     logical_key="verify3:interrupted", today=dt.date(2026, 9, 25))
    set_provider_override(InterruptedProvider(responses=[]))
    try:
        with pytest.raises(asyncio.CancelledError):
            await analysis.run_analysis(configured, **arguments)
        provider = ScriptedAIProvider(responses=[recommendation_json(cards=[])])
        set_provider_override(provider)
        result = await analysis.run_analysis(configured, **arguments)
        assert result.status in {"succeeded", "fallback"}, (
            f"Retry returns {result.status!r}; persisted running analysis is never resumed"
        )
        assert provider.calls, "Retry must resume the interrupted generation"
    finally:
        set_provider_override(None)


@pytest.mark.parametrize("invalidated", ["lease", "quarantine"])
async def test_analysis_rechecks_authority_after_model(owner_session, test_settings, invalidated):
    f = await _complete_fixture(owner_session)
    configured = test_settings.model_copy(deep=True)
    configured.ai.enabled = True
    async with session_scope(test_settings, RuntimeRole.WORKER) as session:
        await queue.enqueue(session, job_type="run_analysis", logical_key="verify3:analysis-fence",
            queue_class="review", workspace_id=f.workspace.id,
            payload={"run_kind":"weekly_review", "analysis_key":"verify3:analysis-fence",
                     "analysis_date":"2026-09-25"}, correlation_id="verify3")
    job = (await queue.claim_jobs(test_settings, queue_classes=("review",), limit=1))[0]

    class InvalidateWhileWaiting(ScriptedAIProvider):
        async def structured(self, **kwargs):
            async with session_scope(test_settings, RuntimeRole.OWNER) as session:
                if invalidated == "lease":
                    await session.execute(update(Job).where(Job.id==job.id).values(lease_token=uuid.uuid4()))
                else:
                    await session.execute(update(Workspace).where(Workspace.id==f.workspace.id).values(quarantined=True))
            return await super().structured(**kwargs)

    set_provider_override(InvalidateWhileWaiting(responses=[recommendation_json(cards=[])]))
    try:
        async with execution_fence(queue.lease_fence(job)):
            with pytest.raises(TemporarilyUnavailable):
                await handle_run_analysis(configured, job)
    finally:
        set_provider_override(None)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        status = await session.scalar(select(AnalysisRun.status).where(AnalysisRun.logical_key=="verify3:analysis-fence"))
        events = await session.scalar(select(func.count()).select_from(OutboxEvent).where(
            OutboxEvent.workspace_id==f.workspace.id, OutboxEvent.event_type=="AnalysisCompleted"))
    assert status != "succeeded" and events == 0, (
        f"After {invalidated} invalidation, result was accepted: status={status}, completion events={events}"
    )


async def test_repeated_photo_event_reuses_its_draft(owner_session, test_settings, monkeypatch):
    from fintracker.application.conversation.sections import confirm_draft
    import json
    f = await prepared(owner_session)
    configured = test_settings.model_copy(deep=True)
    configured.ai.enabled = True
    receipt = json.dumps({"schema_version":"1.0", "document_kind":"receipt", "payment_confirmed":True,
        "total_decimal":"450.00", "currency":"RUB", "lines":[
            {"label":"Продукты", "amount_decimal":"450.00", "readable":True,
             "category_id":str(f.categories["Продукты"])}], "unreadable_lines":0})
    async def download_fixture(settings, *, file_id):
        return b"local receipt bytes; provider is scripted"
    monkeypatch.setattr(media_pipeline, "download_attachment", download_fixture)
    provider = ScriptedAIProvider(responses=[receipt, receipt])
    set_provider_override(provider)
    payload = incoming(f, "", message_id=993)
    payload["message"].pop("text")
    payload["message"]["photo"] = [{"file_id":"verify3-photo", "file_size":50, "width":1, "height":1}]
    job = await leased(configured, payload)
    class CrashAfterCard:
        async def send_message(self, **kwargs):
            raise RuntimeError("verify3 crash after receipt draft and reply persisted")
    try:
        set_sender_override(CrashAfterCard())
        with pytest.raises(RuntimeError, match="verify3 crash"):
            await process_event.handle_process_inbound_event(configured, job)
        set_sender_override(RecordingSender())
        await process_event.handle_process_inbound_event(configured, job)
        async with session_scope(test_settings, RuntimeRole.OWNER) as session:
            drafts = (await session.scalars(select(Draft).where(Draft.workspace_id==f.workspace.id))).all()
        # Both persisted cards are actionable. Confirming the repeated card
        # must not conduct the same source receipt a second time.
        for draft in drafts:
            await confirm_draft(configured, actor=f.actor, workspace=f.workspace,
                                draft_id=draft.id, origin="telegram_photo")
        count = await tx_count(test_settings, f)
        assert count == 1 and len(drafts) == 1, (
            f"One retried photo event made {len(drafts)} cards and {count} expenses; "
            f"source keys={[d.source_message_key for d in drafts]}"
        )
    finally:
        set_provider_override(None)
        set_sender_override(None)


async def test_downgrade_keeps_previous_purge_function_usable(owner_session, test_settings):
    f = await build_fixture(owner_session)
    f.workspace.state = "deleted"
    await owner_session.commit()
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "FINTRACKER_DB__OWNER_DSN":test_settings.db.owner_dsn}
    def migrate(target, direction):
        return subprocess.run([str(root/'.venv/bin/alembic'), direction, target], cwd=root,
                              env=env, capture_output=True, text=True)
    down = await asyncio.to_thread(migrate, "0010_input", "downgrade")
    assert down.returncode == 0, down.stderr
    failure = None
    try:
        async with session_scope(test_settings, RuntimeRole.WORKER) as session:
            await session.execute(text("SELECT purge_workspace_data(:id)"), {"id":f.workspace.id})
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        up = await asyncio.to_thread(migrate, "head", "upgrade")
        assert up.returncode == 0, up.stderr
    assert failure is None, f"Downgrade succeeds but previous application purge is broken: {failure}"


async def test_api_start_does_not_require_migration_credentials(owner_session, test_settings):
    from asgi_lifespan import LifespanManager
    from fintracker.api.app import create_app
    from fintracker.db.session import dispose_engines
    from fintracker.runtime.health import check_readiness
    await prepared(owner_session)
    configured = test_settings.model_copy(deep=True)
    configured.db.owner_dsn = "postgresql+psycopg://migration_unavailable:unused@127.0.0.1:1/unused"
    await dispose_engines()
    failure = None
    try:
        assert (await check_readiness(configured, RuntimeRole.API)).ready, "Runtime DB role must work"
        async with LifespanManager(create_app(configured)):
            pass
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        await dispose_engines()
    assert failure is None, f"API startup requires the forbidden migration connection: {failure}"


async def test_edited_message_confirmation_updates_original_transaction(owner_session, test_settings):
    from fintracker.db.models.ledger import Transaction, TransactionRevision
    f = await prepared(owner_session)
    sender = RecordingSender()
    set_sender_override(sender)
    try:
        original = await leased(test_settings, incoming(f, "продукты 450", update_id=501))
        await process_event.handle_process_inbound_event(test_settings, original)
        edited = await leased(test_settings, incoming(f, "продукты 600", update_id=502, edited=True))
        await process_event.handle_process_inbound_event(test_settings, edited)
        action = next(button["callback_data"] for item in sender.sent
                      for row in item.get("buttons") or [] for button in row
                      if button["text"] == "Обновить запись")
        await service.handle(test_settings, IncomingMessage(
            telegram_user_id=f.user.telegram_user_id, chat_id=f.user.telegram_user_id,
            kind=MessageKind.CALLBACK, callback_data=action, workspace_id=f.workspace.id))
        async with session_scope(test_settings, RuntimeRole.OWNER) as session:
            amounts = (await session.scalars(select(TransactionRevision.amount_minor)
                .join(Transaction, (Transaction.id==TransactionRevision.transaction_id) &
                      (Transaction.current_revision==TransactionRevision.revision))
                .where(Transaction.workspace_id==f.workspace.id, Transaction.status=="posted"))).all()
        assert amounts == [60000], f"Confirmation should replace 450 with 600; got {amounts}"
    finally:
        set_sender_override(None)


async def test_successful_summary_emits_one_completion_event(owner_session, test_settings):
    f = await _complete_fixture(owner_session)
    configured = test_settings.model_copy(deep=True)
    configured.ai.enabled = True
    async with session_scope(test_settings, RuntimeRole.WORKER) as session:
        await queue.enqueue(session, job_type="run_analysis", logical_key="verify3:single-summary",
            queue_class="review", workspace_id=f.workspace.id,
            payload={"run_kind":"weekly_review", "analysis_key":"verify3:single-summary",
                     "analysis_date":"2026-09-25"}, correlation_id="verify3")
    job = (await queue.claim_jobs(test_settings, queue_classes=("review",), limit=1))[0]
    set_provider_override(ScriptedAIProvider(responses=[recommendation_json(cards=[])]))
    try:
        async with execution_fence(queue.lease_fence(job)):
            await handle_run_analysis(configured, job)
    finally:
        set_provider_override(None)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        events = (await session.scalars(select(OutboxEvent).where(
            OutboxEvent.workspace_id==f.workspace.id, OutboxEvent.event_type=="AnalysisCompleted"))).all()
    assert len(events) == 1, f"A single successful model summary emitted {len(events)} completion events"
