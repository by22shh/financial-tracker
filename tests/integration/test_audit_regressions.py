"""Регрессии по находкам аудита 13 сентября 2026 (AUD-01…AUD-18).

Проверяется полный путь без подмен: приём → очередь → штатный обработчик →
ответ и изменения базы. Изоляция AUD-01, использованная аудитом, снята.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
from sqlalchemy import func, select, update

from fintracker.application.conversation.service import record_free_text
from fintracker.application.conversation.types import IncomingMessage, MessageKind
from fintracker.application.ingestion import process_event
from fintracker.application.platform import queue
from fintracker.config import Settings
from fintracker.db.models.access import Membership, User
from fintracker.db.models.catalog import Category
from fintracker.db.models.platform import InboundEvent, Job
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.infra.ai.openai_client import ScriptedAIProvider, set_provider_override
from fintracker.infra.telegram.sender import RecordingSender, set_sender_override
from tests.conftest import requires_pg
from tests.integration import test_deep_audit as scenarios
from tests.integration.test_ai_contract import extraction_json

pytestmark = [pytest.mark.pg, requires_pg]


async def _drain_replies(settings: Settings) -> None:
    """Выполнить поставленные задачи доставки ответа."""
    jobs = await queue.claim_jobs(settings, queue_classes=("interactive",), limit=20)
    for job in jobs:
        if job.job_type == "deliver_reply":
            await process_event.handle_deliver_reply(settings, job)
            await queue.complete(settings, job)


async def test_aud01_worker_routes_accepted_start(owner_session, test_settings: Settings) -> None:
    """AUD-01: штатный worker обрабатывает принятый /start и отвечает автору."""
    fixture = await scenarios.prepared(owner_session)
    job = await scenarios.leased(test_settings, scenarios.incoming(fixture, "/start"))
    sender = RecordingSender()
    set_sender_override(sender)
    try:
        await process_event.handle_process_inbound_event(test_settings, job)
        await _drain_replies(test_settings)
        async with session_scope(test_settings, RuntimeRole.OWNER) as session:
            state = await session.scalar(
                select(InboundEvent.state).where(InboundEvent.id == job.subject_id)
            )
    finally:
        set_sender_override(None)
    assert sender.sent, "ответ автору отправлен"
    assert state == "processed"


async def test_aud01_scheduler_discovers_active_budgets(
    owner_session, test_settings: Settings
) -> None:
    """AUD-01: планировщик видит активные бюджеты и ставит календарные задания."""
    from fintracker.runtime.scheduler import schedule_tick

    fixture = await scenarios.prepared(owner_session)
    await schedule_tick(test_settings)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        jobs = (
            await session.scalars(
                select(Job.job_type).where(Job.workspace_id == fixture.workspace.id)
            )
        ).all()
    assert "open_next_period" in jobs
    assert "payment_reminders" in jobs


async def test_aud01_retention_clears_expired_private_draft(
    owner_session, test_settings: Settings
) -> None:
    """AUD-01, RET-02: истёкший приватный черновик очищается фоновой задачей."""
    from fintracker.application.maintenance.retention import handle_retention_sweep
    from fintracker.db.models.platform import Draft
    from tests.integration.factories import build_fixture

    fixture = await build_fixture(owner_session)
    before = dt.datetime.now(dt.UTC) - dt.timedelta(days=8)
    draft = Draft(
        workspace_id=fixture.workspace.id,
        owner_user_id=fixture.user.id,
        source_kind="text",
        state="ready",
        raw_text="ЛИЧНЫЙ ИСХОДНЫЙ ТЕКСТ",
        expires_at=before,
        delete_raw_after=before,
    )
    owner_session.add(draft)
    await owner_session.flush()
    draft_id = draft.id
    await owner_session.commit()

    await handle_retention_sweep(test_settings, None)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        row = await session.get(Draft, draft_id)
    assert row is not None
    assert row.state == "expired"
    assert row.raw_text is None


async def test_aud02_retry_after_commit_does_not_duplicate(
    owner_session, test_settings: Settings
) -> None:
    """AUD-02: повтор задачи после сбоя ответа не создаёт вторую трату."""
    await scenarios.test_audit_restart_after_post_does_not_duplicate(owner_session, test_settings)


async def test_aud03_stale_lease_cannot_post(owner_session, test_settings: Settings) -> None:
    """AUD-03: исполнитель с утраченной арендой не фиксирует деньги."""
    await scenarios.test_audit_stale_lease_cannot_post(owner_session, test_settings)


async def test_aud03_expired_lease_is_invalid(owner_session, test_settings: Settings) -> None:
    """AUD-03: истёкшая аренда недействительна и до перехвата другим исполнителем."""
    await scenarios.test_audit_expired_lease_is_invalid_without_reclaim(
        owner_session, test_settings
    )


async def test_aud04_removed_member_cannot_finish_inflight_write(
    owner_session, test_settings: Settings
) -> None:
    """AUD-04: отозванное членство перепроверяется под блокировкой команды."""
    from fintracker.application.conversation.category_flow import create_category_from_text
    from fintracker.application.identity.actor import resolve_actor
    from fintracker.application.identity.membership import remove_member
    from fintracker.core.errors import DomainError

    fixture = await scenarios.prepared(owner_session)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        user = User(id=uuid.uuid4(), telegram_user_id=880012345)
        session.add(user)
        await session.flush()
        session.add(
            Membership(
                workspace_id=fixture.workspace.id,
                user_id=user.id,
                role="member",
                status="active",
                generation=uuid.uuid4(),
            )
        )
        await session.flush()
        stale_actor = await resolve_actor(session, user=user, workspace_id=fixture.workspace.id)

    await remove_member(
        test_settings,
        workspace_id=fixture.workspace.id,
        admin_user_id=fixture.user.id,
        target_user_id=user.id,
        correlation_id="audit-revoke",
    )
    with pytest.raises(DomainError):
        await create_category_from_text(
            test_settings,
            actor=stale_actor,
            workspace=fixture.workspace,
            text="Создай категорию ПОСЛЕ_ИСКЛЮЧЕНИЯ",
        )
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Category)
            .where(
                Category.workspace_id == fixture.workspace.id,
                Category.name == "ПОСЛЕ_ИСКЛЮЧЕНИЯ",
            )
        )
    assert count == 0


async def test_aud07_slow_model_does_not_break_transaction(
    owner_session, test_settings: Settings
) -> None:
    """AUD-07: допустимая задержка модели не рвёт транзакцию базы."""
    fixture = await scenarios.prepared(owner_session)
    configured = test_settings.model_copy(deep=True)
    configured.ai.enabled = True

    class SlowProvider(ScriptedAIProvider):
        async def structured(self, **kwargs):  # type: ignore[no-untyped-def]
            await asyncio.sleep(11)
            return await super().structured(**kwargs)

    provider = SlowProvider(
        responses=[
            extraction_json(
                candidate={
                    "category_id": str(fixture.categories["Продукты"]),
                    "date_expression": None,
                }
            )
        ]
    )
    set_provider_override(provider)
    try:
        replies = await record_free_text(
            configured,
            actor=fixture.actor,
            workspace=fixture.workspace,
            message=IncomingMessage(
                telegram_user_id=fixture.user.telegram_user_id,
                chat_id=fixture.user.telegram_user_id,
                kind=MessageKind.TEXT,
                text="продукты 450",
                received_at=dt.datetime.now(dt.UTC),
            ),
        )
    finally:
        set_provider_override(None)
    assert replies


async def test_aud08_transfer_is_not_converted_into_expense(
    owner_session, test_settings: Settings
) -> None:
    """AUD-08: подтверждённый перевод не превращается в расход."""
    await scenarios.test_audit_transfer_is_not_converted_into_expense(owner_session, test_settings)


async def test_aud10_multiple_edits_are_accepted(owner_session, test_settings: Settings) -> None:
    """AUD-10: вторая правка одного сообщения принимается без исключения."""
    await scenarios.test_audit_multiple_edits_are_accepted(owner_session, test_settings)


async def test_aud11_failed_reply_remains_retryable(owner_session, test_settings: Settings) -> None:
    """AUD-11: недоставленный ответ повторяется отдельной задачей."""
    fixture = await scenarios.prepared(owner_session)
    job = await scenarios.leased(test_settings, scenarios.incoming(fixture, "/start"))
    failing = RecordingSender(fail_for_chats={fixture.user.telegram_user_id})
    set_sender_override(failing)
    try:
        await process_event.handle_process_inbound_event(test_settings, job)
        delivery = await _claim_reply_job(test_settings)
        from fintracker.core.errors import TemporarilyUnavailable

        with pytest.raises(TemporarilyUnavailable):
            await process_event.handle_deliver_reply(test_settings, delivery)
        state = await queue.fail(test_settings, delivery, error="сбой доставки")
        assert state == "retry_wait", "ответ остаётся к повтору"
    finally:
        set_sender_override(None)

    sender = RecordingSender()
    set_sender_override(sender)
    try:
        async with session_scope(test_settings, RuntimeRole.OWNER) as session:
            await session.execute(
                update(Job)
                .where(Job.job_type == "deliver_reply")
                .values(available_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
            )
        await _drain_replies(test_settings)
    finally:
        set_sender_override(None)
    assert sender.sent, "повтор доставил ответ без повторной финансовой команды"


async def test_aud12_group_message_does_not_disclose_budgets(
    owner_session, test_settings: Settings
) -> None:
    """AUD-12: список личных бюджетов не уходит в групповой чат."""
    await scenarios.test_audit_group_message_does_not_disclose_private_budgets(
        owner_session, test_settings
    )


@pytest.mark.parametrize("button", ["exp:xlsx", "exp:csv", "imp:start"])
async def test_aud13_import_export_buttons_are_wired(
    owner_session, test_settings: Settings, button: str
) -> None:
    """AUD-13: кнопки импорта и экспорта ведут в работающие обработчики."""
    await scenarios.test_audit_export_import_buttons_are_wired(owner_session, test_settings, button)


async def test_aud16_invite_secret_is_not_retained_in_job(
    owner_session, test_settings: Settings
) -> None:
    """AUD-16, SEC-04: открытый код приглашения не хранится в глобальной задаче."""
    from fintracker.application.ingestion.accept_update import accept_telegram_update

    fixture = await scenarios.prepared(owner_session)
    accepted = await accept_telegram_update(
        test_settings, scenarios.incoming(fixture, "/join ABCD-EFGH-IJKL")
    )
    async with session_scope(test_settings, RuntimeRole.API) as session:
        job = await session.get(Job, accepted.job_id)
    assert job is not None
    assert not job.payload.get("invite_code")
    assert "ABCD" not in str(job.payload)


async def _claim_reply_job(settings):
    """Задача доставки, ждущая повтора после неудачной немедленной отправки.

    Немедленная отправка и исполнитель делят одну строку задачи (G-21), поэтому
    после неудачи она ждёт по backoff: проверка сдвигает её срок, не меняя
    проверяемый инвариант.
    """
    import datetime as _dt

    from sqlalchemy import update as _update

    from fintracker.application.platform import queue as _queue
    from fintracker.db.models.platform import Job as _Job
    from fintracker.db.session import RuntimeRole as _Role
    from fintracker.db.session import session_scope as _scope

    async with _scope(settings, _Role.OWNER) as session:
        await session.execute(
            _update(_Job)
            .where(_Job.job_type == "deliver_reply", _Job.state.in_(("queued", "retry_wait")))
            .values(available_at=_dt.datetime.now(_dt.UTC) - _dt.timedelta(seconds=1))
        )
    claimed = await _queue.claim_jobs(settings, queue_classes=("interactive",), limit=20)
    return next(item for item in claimed if item.job_type == "deliver_reply")
