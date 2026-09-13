"""Audit repros: assertions describe required behavior. No production code changes."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import func, select, update

from fintracker.application.conversation.service import handle, record_free_text
from fintracker.application.conversation.types import IncomingMessage, MessageKind
from fintracker.application.ingestion.accept_update import accept_telegram_update
from fintracker.application.ingestion.process_event import handle_process_inbound_event
from fintracker.application.platform import queue
from fintracker.db.models.access import Membership, UserBudgetContext
from fintracker.db.models.ledger import Transaction, TransactionRevision
from fintracker.db.models.platform import Job
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.infra.ai.openai_client import ScriptedAIProvider, set_provider_override
from fintracker.infra.telegram.sender import RecordingSender, set_sender_override
from tests.integration.factories import build_fixture
from tests.integration.test_ai_contract import extraction_json


async def prepared(owner_session):
    f = await build_fixture(owner_session)
    owner_session.add(UserBudgetContext(user_id=f.user.id, workspace_id=f.workspace.id))
    await owner_session.execute(
        update(Membership)
        .where(Membership.id == f.actor.membership_id)
        .values(autopost_enabled=True)
    )
    await owner_session.commit()
    return f


def incoming(f, text, *, chat_id=None, update_id=101, message_id=11, edited=False):
    body = {
        "message_id": message_id,
        "date": int(dt.datetime.now(dt.UTC).timestamp()),
        "from": {"id": f.user.telegram_user_id, "is_bot": False, "first_name": "Audit"},
        "chat": {
            "id": chat_id or f.user.telegram_user_id,
            "type": "supergroup" if chat_id and chat_id < 0 else "private",
        },
        "text": text,
    }
    return {"update_id": update_id, "edited_message" if edited else "message": body}


async def leased(settings, payload):
    accepted = await accept_telegram_update(settings, payload)
    jobs = await queue.claim_jobs(settings, queue_classes=("interactive",), limit=20)
    return next(j for j in jobs if j.subject_id == accepted.inbound_event_id)


async def tx_count(settings, f):
    async with session_scope(settings, RuntimeRole.OWNER) as s:
        return await s.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.workspace_id == f.workspace.id)
        )


async def test_audit_ai_enabled_text_can_finish(owner_session, test_settings):
    f = await prepared(owner_session)
    configured = test_settings.model_copy(deep=True)
    configured.ai.enabled = True
    provider = ScriptedAIProvider(
        responses=[
            extraction_json(
                candidate={"category_id": str(f.categories["Продукты"]), "date_expression": None}
            )
        ]
    )
    set_provider_override(provider)
    try:
        result = await record_free_text(
            configured,
            actor=f.actor,
            workspace=f.workspace,
            message=IncomingMessage(
                telegram_user_id=f.user.telegram_user_id,
                chat_id=f.user.telegram_user_id,
                kind=MessageKind.TEXT,
                text="продукты 450",
                received_at=dt.datetime.now(dt.UTC),
            ),
        )
        assert result and provider.calls
    finally:
        set_provider_override(None)


async def test_audit_restart_after_post_does_not_duplicate(owner_session, test_settings):
    f = await prepared(owner_session)
    job = await leased(test_settings, incoming(f, "продукты 500"))

    class CrashSender:
        async def send_message(self, **kwargs):
            raise RuntimeError("AUDIT simulated crash after financial commit")

    set_sender_override(CrashSender())
    try:
        with pytest.raises(RuntimeError, match="AUDIT"):
            await handle_process_inbound_event(test_settings, job)
        assert await tx_count(test_settings, f) == 1
        set_sender_override(RecordingSender())
        await handle_process_inbound_event(test_settings, job)
        assert await tx_count(test_settings, f) == 1, (
            "Same inbound event created two financial transactions"
        )
    finally:
        set_sender_override(None)


async def test_audit_stale_lease_cannot_post(owner_session, test_settings):
    f = await prepared(owner_session)
    job = await leased(test_settings, incoming(f, "продукты 500"))
    async with session_scope(test_settings, RuntimeRole.OWNER) as s:
        await s.execute(update(Job).where(Job.id == job.id).values(lease_token=uuid.uuid4()))
    set_sender_override(RecordingSender())
    try:
        await handle_process_inbound_event(test_settings, job)
        assert await tx_count(test_settings, f) == 0, (
            "Worker posted money before checking its lost lease"
        )
    finally:
        set_sender_override(None)


async def test_audit_failed_immediate_reply_remains_retryable(owner_session, test_settings):
    f = await prepared(owner_session)
    job = await leased(test_settings, incoming(f, "/start"))
    sender = RecordingSender(fail_for_chats={f.user.telegram_user_id})
    set_sender_override(sender)
    try:
        # Инвариант AUD-11: недоставленный ответ не теряется. Повтор идёт
        # отдельной долговечной задачей и не запускает бизнес-команду заново,
        # поэтому событие остаётся processed (рекомендация самого аудита).
        await handle_process_inbound_event(test_settings, job)
        async with session_scope(test_settings, RuntimeRole.OWNER) as s:
            pending = (
                await s.scalars(select(Job.state).where(Job.job_type == "deliver_reply"))
            ).all()
        assert pending and all(state in {"queued", "retry_wait"} for state in pending), (
            "Failed reply must stay retryable as an independent delivery job"
        )
    finally:
        set_sender_override(None)


async def test_audit_group_message_does_not_disclose_private_budgets(owner_session, test_settings):
    f = await prepared(owner_session)
    group = -100987654321
    job = await leased(test_settings, incoming(f, "/start", chat_id=group))
    sender = RecordingSender()
    set_sender_override(sender)
    try:
        await handle_process_inbound_event(test_settings, job)
        exposed = [
            m for m in sender.sent if m["chat_id"] == group and f.workspace.name in m["text"]
        ]
        assert not exposed, (
            "Private budget name was sent to all members of an unrelated Telegram group"
        )
    finally:
        set_sender_override(None)


async def test_audit_multiple_edits_are_accepted(owner_session, test_settings):
    f = await prepared(owner_session)
    await accept_telegram_update(
        test_settings, incoming(f, "продукты 500", update_id=201, message_id=99)
    )
    await accept_telegram_update(
        test_settings, incoming(f, "продукты 600", update_id=202, message_id=99, edited=True)
    )
    accepted = await accept_telegram_update(
        test_settings, incoming(f, "продукты 700", update_id=203, message_id=99, edited=True)
    )
    assert not accepted.duplicate


async def test_audit_transfer_is_not_converted_into_expense(owner_session, test_settings):
    from fintracker.application.conversation.entry import (
        CandidateFields,
        ExtractionResult,
        create_draft_with_candidates,
    )
    from fintracker.application.conversation.sections import confirm_draft
    from fintracker.domain.parsing.intent import Intent

    f = await prepared(owner_session)
    async with session_scope(
        test_settings, RuntimeRole.API, user_id=f.user.id, workspace_id=f.workspace.id
    ) as s:
        draft, _ = await create_draft_with_candidates(
            s,
            settings=test_settings,
            actor=f.actor,
            source_kind="text",
            raw_text="перевёл 500 с карты на кошелёк",
            extraction=ExtractionResult(
                intent=Intent.RECORD_TRANSACTION,
                candidates=[
                    CandidateFields(
                        kind="transfer",
                        amount_minor=50000,
                        currency="RUB",
                        occurred_date=dt.date.today(),
                        category_id=f.categories["Продукты"],
                    )
                ],
            ),
        )
        draft_id = draft.id
    await confirm_draft(
        test_settings,
        actor=f.actor,
        workspace=f.workspace,
        draft_id=draft_id,
        origin="telegram_text",
    )
    async with session_scope(test_settings, RuntimeRole.OWNER) as s:
        kinds = (
            await s.scalars(
                select(TransactionRevision.transaction_type).where(
                    TransactionRevision.workspace_id == f.workspace.id
                )
            )
        ).all()
    assert "expense" not in kinds, "Confirmed transfer is stored as an expense"


@pytest.mark.parametrize("button", ["exp:xlsx", "exp:csv", "imp:start"])
async def test_audit_export_import_buttons_are_wired(owner_session, test_settings, button):
    f = await prepared(owner_session)
    replies = await handle(
        test_settings,
        IncomingMessage(
            telegram_user_id=f.user.telegram_user_id,
            chat_id=f.user.telegram_user_id,
            kind=MessageKind.CALLBACK,
            callback_data=button,
            workspace_id=f.workspace.id,
        ),
    )
    assert not any("Кнопка устарела" in r.text for r in replies), (
        f"{button} displayed by menu but has no handler"
    )


async def test_audit_expired_lease_is_invalid_without_reclaim(owner_session, test_settings):
    f = await prepared(owner_session)
    job = await leased(test_settings, incoming(f, "/start"))
    async with session_scope(test_settings, RuntimeRole.OWNER) as s:
        await s.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(lease_until=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5))
        )
    async with session_scope(test_settings, RuntimeRole.WORKER) as s:
        valid = await queue.lease_is_valid(s, job)
    assert not valid, "Expired lease accepted before any other worker reclaims it"
