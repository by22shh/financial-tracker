"""Регрессии очередей, доступа и AI по аудиту готовности 14 сентября 2026.

Диагностики аудита фиксировали наличие дефекта; здесь те же сценарии записаны
как требуемое поведение (G-03, G-04, G-20…G-24, G-27).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, update

from fintracker.application.intelligence import analysis, extraction
from fintracker.application.platform import queue
from fintracker.db.models.access import Workspace
from fintracker.db.models.intelligence import Recommendation
from fintracker.db.models.platform import (
    AICostReservation,
    AIQuotaCounter,
    Draft,
    Job,
    NotificationDelivery,
    OutboxEvent,
)
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.infra.ai.openai_client import ScriptedAIProvider, set_provider_override
from fintracker.infra.telegram.sender import RecordingSender, set_sender_override
from fintracker.runtime.worker import _run_with_lease, build_registry
from tests.integration.factories import build_fixture
from tests.integration.test_recommendations import TODAY, _complete_fixture, recommendation_json


@pytest.fixture(autouse=True)
def no_external_calls():
    set_sender_override(RecordingSender())
    set_provider_override(ScriptedAIProvider(responses=[]))
    yield
    set_sender_override(None)
    set_provider_override(None)


async def prepare_delivery(settings, owner_session):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with session_scope(settings, RuntimeRole.OWNER) as session:
        event = (
            await session.scalars(
                select(OutboxEvent).where(OutboxEvent.event_type == "CategoryCreated").limit(1)
            )
        ).one()
        event_id = event.id
        delivery = NotificationDelivery(
            event_id=event.id,
            workspace_id=fixture.workspace.id,
            recipient_user_id=fixture.user.id,
            membership_generation=fixture.actor.membership_generation,
            channel="telegram",
            delivery_class="shared_change",
            state="pending",
            available_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
        )
        session.add(delivery)
        await session.flush()
        delivery_id = delivery.id
        await queue.enqueue(
            session,
            job_type="deliver_notification",
            logical_key="probe-deliver",
            workspace_id=fixture.workspace.id,
            payload={"event_id": str(event_id)},
        )
    job = (await queue.claim_jobs(settings, queue_classes=("interactive",)))[0]
    return fixture, job, delivery_id


# --- G-03 -------------------------------------------------------------------
@pytest.mark.parametrize("invalidation", ["expired_lease", "security_fence", "quarantined"])
async def test_notification_requires_execution_authority(
    owner_session, test_settings, invalidation
) -> None:
    """G-03: уведомление не уходит без действующего права исполнителя и доступа."""
    fixture, job, delivery_id = await prepare_delivery(test_settings, owner_session)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        if invalidation == "expired_lease":
            await session.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(lease_until=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
            )
        else:
            value = uuid.uuid4() if invalidation == "security_fence" else True
            await session.execute(
                update(Workspace)
                .where(Workspace.id == fixture.workspace.id)
                .values(**{invalidation: value})
            )
    sender = RecordingSender()
    set_sender_override(sender)
    await _run_with_lease(test_settings, job, build_registry())
    assert sender.sent == [], f"уведомление отправлено при {invalidation}: {sender.sent}"
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        state = (await session.get(NotificationDelivery, delivery_id)).state
    assert state != "sent", f"доставка помечена отправленной при {invalidation}"


# --- G-20 -------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["future_delivery", "failed_transport"])
async def test_pending_delivery_keeps_runnable_job(owner_session, test_settings, mode) -> None:
    """G-20: незавершённая доставка всегда имеет задачу, которая её повторит."""
    fixture, job, delivery_id = await prepare_delivery(test_settings, owner_session)
    sender = RecordingSender(
        fail_for_chats={fixture.user.telegram_user_id} if mode == "failed_transport" else set()
    )
    set_sender_override(sender)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        if mode == "future_delivery":
            await session.execute(
                update(NotificationDelivery)
                .where(NotificationDelivery.id == delivery_id)
                .values(available_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1))
            )
    await _run_with_lease(test_settings, job, build_registry())
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        delivery = await session.get(NotificationDelivery, delivery_id)
        assert delivery.state in {"pending", "failed"}
        await session.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.id == delivery_id)
            .values(available_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
        )
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        pending = (
            await session.scalars(
                select(Job).where(
                    Job.job_type == "deliver_notification",
                    Job.state.in_(("queued", "retry_wait", "running")),
                )
            )
        ).all()
    assert pending, f"незавершённая доставка ({mode}) осталась без задачи повтора"


# --- G-27 -------------------------------------------------------------------
async def test_expired_running_job_respects_max_attempts(owner_session, test_settings) -> None:
    """G-27: перехват после сбоя процесса не обходит предел попыток."""
    async with session_scope(test_settings, RuntimeRole.WORKER) as session:
        await queue.enqueue(session, job_type="probe", logical_key="probe-retries", max_attempts=1)
    first = (await queue.claim_jobs(test_settings, queue_classes=("interactive",)))[0]
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        await session.execute(
            update(Job)
            .where(Job.id == first.id)
            .values(lease_until=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
        )
    again = await queue.claim_jobs(test_settings, queue_classes=("interactive",))
    assert not again, f"исчерпавшая попытки задача выдана снова: {again}"
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        state = (await session.get(Job, first.id)).state
    assert state == "failed", f"состояние задачи после исчерпания попыток: {state}"


# --- G-22 -------------------------------------------------------------------
async def test_cancelled_extraction_releases_quota(owner_session, test_settings) -> None:
    """G-22: отменённый разбор освобождает резерв и слот одновременности."""
    fixture = await build_fixture(owner_session)
    draft = Draft(
        workspace_id=fixture.workspace.id,
        owner_user_id=fixture.user.id,
        owner_membership_generation=fixture.actor.membership_generation,
        source_kind="voice",
        state="processing",
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=1),
        delete_raw_after=dt.datetime.now(dt.UTC) + dt.timedelta(days=1),
    )
    owner_session.add(draft)
    await owner_session.flush()
    draft_id = draft.id
    await owner_session.commit()
    settings = test_settings.model_copy(deep=True)
    settings.ai.enabled = True
    settings.ai.max_concurrent_per_workspace = 1

    class Cancelled(ScriptedAIProvider):
        async def structured(self, **kwargs):
            raise asyncio.CancelledError()

    set_provider_override(Cancelled(responses=[]))
    catalog = extraction.WorkspaceCatalog((), (), (), "RUB", "Asia/Novosibirsk")
    with pytest.raises(asyncio.CancelledError):
        await extraction.extract_with_model(
            settings,
            actor=fixture.actor,
            draft_id=draft_id,
            draft_version=1,
            text="кофе 200",
            catalog=catalog,
            reference_date=TODAY,
        )
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        reservations = (await session.scalars(select(AICostReservation))).all()
        counters = (await session.scalars(select(AIQuotaCounter))).all()
    assert all(row.state != "reserved" for row in reservations), "резерв стоимости не закрыт"
    assert all(row.in_flight == 0 for row in counters), "слот одновременности не освобождён"

    set_provider_override(
        ScriptedAIProvider(
            responses=[
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "intent": "record_transaction",
                        "question": None,
                        "candidates": [],
                    }
                )
            ]
        )
    )
    # Следующая попытка не блокируется квотой после отменённой.
    await extraction.extract_with_model(
        settings,
        actor=fixture.actor,
        draft_id=draft_id,
        draft_version=2,
        text="кофе 200",
        catalog=catalog,
        reference_date=TODAY,
    )


# --- G-23 -------------------------------------------------------------------
async def test_stale_analysis_is_not_published_as_current(owner_session, test_settings) -> None:
    """G-23: устаревший за время анализа результат не выдаётся как текущий."""
    fixture = await _complete_fixture(owner_session)
    settings = test_settings.model_copy(deep=True)
    settings.ai.enabled = True
    args = {
        "workspace_id": fixture.workspace.id,
        "run_kind": "weekly_review",
        "logical_key": "probe-stale-analysis",
        "today": TODAY,
    }
    preparation, _ = await analysis._prepare_analysis(settings, **args)
    metric_id = str(preparation.metrics["metric_id"])
    set_provider_override(
        ScriptedAIProvider(responses=[recommendation_json(card={"metric_refs": [metric_id]})])
    )
    result = await analysis._generate_cards(settings, preparation)
    async with session_scope(
        settings, RuntimeRole.API, user_id=fixture.user.id, workspace_id=fixture.workspace.id
    ) as session:
        await session.execute(
            update(Workspace)
            .where(Workspace.id == fixture.workspace.id)
            .values(data_revision=Workspace.data_revision + 1)
        )
    outcome = await analysis._store_analysis(
        settings, preparation, result, correlation_id="probe-stale"
    )
    async with session_scope(
        settings, RuntimeRole.WORKER, workspace_id=fixture.workspace.id
    ) as session:
        proposed = (
            await session.scalars(
                select(Recommendation).where(
                    Recommendation.run_id == outcome.run_id,
                    Recommendation.status == "proposed",
                )
            )
        ).all()
        events = (
            await session.scalars(
                select(OutboxEvent).where(OutboxEvent.event_type == "AnalysisCompleted")
            )
        ).all()
    assert not proposed, "устаревшая рекомендация предложена как действующая"
    assert not events, "устаревший обзор отправлен участникам"


# --- G-24 -------------------------------------------------------------------
def test_recommendation_effect_is_verified_by_server() -> None:
    """G-24: заявленный моделью эффект проверяется сервером по формуле."""
    from fintracker.infra.ai.schemas import RecommendationResponse

    response = RecommendationResponse.model_validate_json(
        recommendation_json(
            card={
                "metric_refs": ["known"],
                "estimated_effect_decimal": "999999999.00",
                "effect_formula": "1 - 1",
            }
        )
    )
    accepted, rejected = analysis.validate_cards(
        response,
        snapshot={"metric_id": "known", "total_fact_minor": 100, "lines": []},
        protected_lines=set(),
        blocked_directions=set(),
        muted=set(),
        currency="RUB",
    )
    assert not accepted, f"неподтверждённый эффект принят: {accepted}"
    assert rejected, "карточка с невоспроизводимым эффектом должна быть отклонена"


# --- G-04 -------------------------------------------------------------------
async def test_missing_security_journal_blocks_workspace(owner_session, test_settings) -> None:
    """G-04: пустой журнал доступа не подтверждает доступ к существующему бюджету."""
    from fintracker.application.identity.actor import resolve_actor
    from fintracker.application.identity.security_change import (
        reconcile_access_on_start,
        run_security_change,
    )
    from fintracker.core.errors import DomainError
    from fintracker.infra.security_log import FilesystemSecurityLog, SecurityLog

    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    root = Path(__file__).parent / ("security-log-" + uuid.uuid4().hex)
    journal = SecurityLog(FilesystemSecurityLog(root / "original"))

    async def apply(session, uow, workspace):
        return {"test": "committed access baseline"}

    await run_security_change(
        test_settings,
        workspace_id=fixture.workspace.id,
        kind="workspace_create",
        initiated_by=fixture.user.id,
        apply=apply,
        correlation_id="missing-journal",
        security_log=journal,
    )
    assert await journal.last_committed(fixture.workspace.id) is not None
    missing_journal = SecurityLog(FilesystemSecurityLog(root / "replacement"))
    await reconcile_access_on_start(test_settings, security_log=missing_journal)
    async with session_scope(
        test_settings, RuntimeRole.API, user_id=fixture.user.id, workspace_id=fixture.workspace.id
    ) as session:
        workspace = await session.get(Workspace, fixture.workspace.id)
        assert workspace.quarantined, "бюджет доступен без доказанного состояния доступа"
        with pytest.raises(DomainError):
            await resolve_actor(session, user=fixture.user, workspace_id=fixture.workspace.id)


# --- G-21 -------------------------------------------------------------------
async def test_reply_is_delivered_once(owner_session, test_settings, monkeypatch) -> None:
    """G-21: немедленный ответ и отложенная копия не дублируют сообщение."""
    from fintracker.application.conversation.types import Reply
    from fintracker.application.identity.actor import set_active_workspace
    from fintracker.application.ingestion import process_event
    from fintracker.application.ingestion.accept_update import accept_telegram_update
    from tests.integration.test_delivery import telegram_update

    fixture = await build_fixture(owner_session, telegram_user_id=590001)
    await set_active_workspace(owner_session, user=fixture.user, workspace_id=fixture.workspace.id)
    await owner_session.commit()
    await accept_telegram_update(
        test_settings,
        telegram_update(900001, text="/history", user_id=fixture.user.telegram_user_id),
    )
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingSender(RecordingSender):
        async def send_message(self, **kwargs):
            result = await super().send_message(**kwargs)
            if len(self.sent) == 1:
                entered.set()
                await release.wait()
            return result

    sender = WaitingSender()
    set_sender_override(sender)

    async def answer(settings, message):
        return [Reply(text="Saved financial answer 250 RUB")]

    monkeypatch.setattr(process_event, "handle", answer)
    job = (await queue.claim_jobs(test_settings, queue_classes=("interactive",)))[0]
    first = asyncio.create_task(_run_with_lease(test_settings, job, build_registry()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        jobs = await queue.claim_jobs(test_settings, queue_classes=("interactive",), limit=10)
        reply_job = next((item for item in jobs if item.job_type == "deliver_reply"), None)
        if reply_job is not None:
            await _run_with_lease(test_settings, reply_job, build_registry())
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
    texts = [item["text"] for item in sender.sent]
    assert len(texts) == len(set(texts)), f"один ответ отправлен дважды: {texts}"
