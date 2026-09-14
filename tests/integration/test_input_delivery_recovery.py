"""Independent follow-up probes; production code is unchanged.

Real PostgreSQL and runtime roles. Mocks replace only external providers or
place a deterministic barrier at a real scheduling boundary.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import select, update

from fintracker.application.conversation import entry, service
from fintracker.application.conversation.types import IncomingMessage, MessageKind
from fintracker.application.identity.membership import remove_member
from fintracker.application.ingestion import process_event
from fintracker.application.intelligence import media_pipeline
from fintracker.application.ledger.service import post_transaction
from fintracker.application.platform import queue
from fintracker.core.fencing import execution_fence
from fintracker.db.models.access import Membership, User, UserBudgetContext
from fintracker.db.models.platform import Draft, Job
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork
from fintracker.infra.ai.openai_client import ScriptedAIProvider, set_provider_override
from fintracker.infra.telegram.sender import RecordingSender, set_sender_override
from tests.integration.test_deep_audit import incoming, leased, prepared, tx_count
from tests.integration.test_money_scenarios import expense_spec, rub


async def test_both_concurrent_executors_return_a_result(owner_session, test_settings, monkeypatch):
    f = await prepared(owner_session)
    message = IncomingMessage(
        telegram_user_id=f.user.telegram_user_id,
        chat_id=f.user.telegram_user_id,
        kind=MessageKind.TEXT,
        text="продукты 450",
        message_id=551,
        workspace_id=f.workspace.id,
        inbound_event_id=uuid.uuid4(),
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
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            service.record_free_text(
                test_settings, actor=f.actor, workspace=f.workspace, message=message
            ),
            service.record_free_text(
                test_settings, actor=f.actor, workspace=f.workspace, message=message
            ),
            return_exceptions=True,
        ),
        12,
    )
    assert await tx_count(test_settings, f) == 1
    failures = [
        f"{type(item).__name__}: {item}" for item in outcomes if isinstance(item, BaseException)
    ]
    assert not failures, f"One expense exists, but the second executor crashes: {failures}"
    assert all(outcomes), "Both callers must receive the existing result"


async def _member_with_history(owner_session, settings):
    f = await prepared(owner_session)
    async with session_scope(settings, RuntimeRole.OWNER) as session:
        member = User(id=uuid.uuid4(), telegram_user_id=880099881)
        session.add(member)
        await session.flush()
        session.add(
            Membership(
                workspace_id=f.workspace.id,
                user_id=member.id,
                role="member",
                status="active",
                generation=uuid.uuid4(),
            )
        )
        session.add(UserBudgetContext(user_id=member.id, workspace_id=f.workspace.id))
        await post_transaction(
            session,
            UnitOfWork(session, "verify3-seed"),
            actor=f.actor,
            spec=expense_spec(f, amount=rub(98765), category="Продукты"),
            origin="form",
        )
    return f, member


async def test_immediate_reply_rechecks_revocation(owner_session, test_settings, monkeypatch):
    f, member = await _member_with_history(owner_session, test_settings)
    job = await leased(test_settings, incoming(replace(f, user=member), "/history"))
    original = process_event.handle

    async def remove_after_render(settings, message):
        replies = await original(settings, message)
        assert any("765" in reply.text for reply in replies), "Fixture must reach private history"
        await remove_member(
            settings,
            workspace_id=f.workspace.id,
            admin_user_id=f.user.id,
            target_user_id=member.id,
            correlation_id="verify3-revoke",
        )
        return replies

    monkeypatch.setattr(process_event, "handle", remove_after_render)
    sender = RecordingSender()
    set_sender_override(sender)
    try:
        await process_event.handle_process_inbound_event(test_settings, job)
    finally:
        set_sender_override(None)
    exposed = [
        item
        for item in sender.sent
        if item["chat_id"] == member.telegram_user_id and "765" in item["text"]
    ]
    assert not exposed, (
        "Immediate path sends rendered financial history after remove_member has completed"
    )


async def test_expired_lease_cannot_send_deferred_reply(owner_session, test_settings):
    f, member = await _member_with_history(owner_session, test_settings)
    job = await leased(test_settings, incoming(replace(f, user=member), "/history"))
    set_sender_override(RecordingSender(fail_for_chats={member.telegram_user_id}))
    try:
        await process_event.handle_process_inbound_event(test_settings, job)
        from tests.integration.test_audit_regressions import _claim_reply_job

        reply_job = await _claim_reply_job(test_settings)
        async with session_scope(test_settings, RuntimeRole.OWNER) as session:
            await session.execute(
                update(Job)
                .where(Job.id == reply_job.id)
                .values(lease_until=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
            )
        sender = RecordingSender()
        set_sender_override(sender)
        async with execution_fence(queue.lease_fence(reply_job)):
            await process_event.handle_deliver_reply(test_settings, reply_job)
        assert not sender.sent, (
            "Execution fence is installed, but deferred delivery never checks it"
        )
    finally:
        set_sender_override(None)


@pytest.mark.parametrize("kind", ["photo", "voice"])
async def test_repeated_media_event_reuses_its_draft(
    owner_session, test_settings, monkeypatch, kind
):
    import json

    from fintracker.application.conversation.sections import confirm_draft

    f = await prepared(owner_session)
    configured = test_settings.model_copy(deep=True)
    configured.ai.enabled = True
    receipt = json.dumps(
        {
            "schema_version": "1.0",
            "document_kind": "receipt",
            "payment_confirmed": True,
            "total_decimal": "450.00",
            "currency": "RUB",
            "lines": [
                {
                    "label": "Продукты",
                    "amount_decimal": "450.00",
                    "readable": True,
                    "category_id": str(f.categories["Продукты"]),
                }
            ],
            "unreadable_lines": 0,
        }
    )

    async def download_fixture(settings, *, file_id):
        from tests.images import png_bytes

        return png_bytes(marker=file_id.encode())

    monkeypatch.setattr(media_pipeline, "download_attachment", download_fixture)
    if kind == "voice":
        from fintracker.infra.asr.provider import ScriptedAsrProvider, set_asr_override
        from tests.acceptance.test_voice_and_receipts import _voice_extraction

        receipt = _voice_extraction(
            amount_decimal="450.00", category_id=str(f.categories["Продукты"])
        )
        set_asr_override(ScriptedAsrProvider(transcripts=["продукты 450"]))
        configured.asr.provider = "stub"
        configured.asr.model = "stub-asr"
    provider = ScriptedAIProvider(responses=[receipt, receipt])
    set_provider_override(provider)
    payload = incoming(f, "", message_id=993)
    payload["message"].pop("text")
    if kind == "photo":
        payload["message"]["photo"] = [
            {"file_id": "verify3-photo", "file_size": 50, "width": 1, "height": 1}
        ]
    else:
        payload["message"]["voice"] = {
            "file_id": "verify3-voice",
            "file_size": 50,
            "duration": 5,
            "mime_type": "audio/ogg",
        }
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
            drafts = (
                await session.scalars(select(Draft).where(Draft.workspace_id == f.workspace.id))
            ).all()
        # Both persisted cards are actionable. Confirming the repeated card
        # must not conduct the same source receipt a second time.
        for draft in drafts:
            await confirm_draft(
                configured,
                actor=f.actor,
                workspace=f.workspace,
                draft_id=draft.id,
                origin="telegram_photo",
            )
        count = await tx_count(test_settings, f)
        assert count == 1 and len(drafts) == 1, (
            f"One retried photo event made {len(drafts)} cards and {count} expenses; "
            f"source keys={[d.source_message_key for d in drafts]}"
        )
    finally:
        set_provider_override(None)
        set_sender_override(None)
        if kind == "voice":
            set_asr_override(None)


@pytest.mark.parametrize("invalidated", ["version", "quarantine", "lease"])
async def test_media_late_result_is_rejected(owner_session, test_settings, invalidated):
    from fintracker.application.conversation.entry import ExtractionResult
    from fintracker.core.errors import TemporarilyUnavailable, VersionConflict
    from fintracker.db.models.access import Workspace
    from fintracker.domain.parsing.intent import Intent

    f = await prepared(owner_session)
    job = await leased(test_settings, incoming(f, "", message_id=996))
    message = IncomingMessage(
        telegram_user_id=f.user.telegram_user_id,
        chat_id=f.user.telegram_user_id,
        kind=MessageKind.PHOTO,
        message_id=996,
        workspace_id=f.workspace.id,
        inbound_event_id=uuid.uuid4(),
    )
    prepared_media = await media_pipeline._prepare_media(
        test_settings, actor=f.actor, workspace=f.workspace, message=message
    )
    assert isinstance(prepared_media, media_pipeline._MediaPreparation)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        if invalidated == "version":
            await session.execute(
                update(Draft)
                .where(Draft.id == prepared_media.draft_id)
                .values(version=Draft.version + 1)
            )
        elif invalidated == "quarantine":
            await session.execute(
                update(Workspace).where(Workspace.id == f.workspace.id).values(quarantined=True)
            )
        else:
            await session.execute(
                update(Job).where(Job.id == job.id).values(lease_token=uuid.uuid4())
            )
    async with execution_fence(queue.lease_fence(job)):
        with pytest.raises((TemporarilyUnavailable, VersionConflict)):
            await media_pipeline._finalize_extraction(
                test_settings,
                actor=f.actor,
                workspace=f.workspace,
                draft_id=prepared_media.draft_id,
                expected_version=prepared_media.version,
                extraction=ExtractionResult(intent=Intent.UNKNOWN, candidates=[]),
                source_note="late",
            )
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        draft = await session.get(Draft, prepared_media.draft_id)
        assert draft is not None and draft.state == "processing"
