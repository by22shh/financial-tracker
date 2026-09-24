"""New boundary checks for the claimed G-03/G-04/G-20/G-22/G-23 fixes.

Assertions express required behavior; failures are audit findings, not expected
passes. Test data stays in a unique disposable PostgreSQL database and temp dirs.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import shutil
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from fintracker.application.conversation import analytics_flow
from fintracker.application.delivery import render
from fintracker.application.identity.security_change import (
    reconcile_access_on_start,
    run_security_change,
)
from fintracker.application.intelligence import analysis, extraction, quota
from fintracker.application.ledger.service import post_transaction
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
from fintracker.db.uow import UnitOfWork
from fintracker.infra.ai.openai_client import ScriptedAIProvider, set_provider_override
from fintracker.infra.security_log import FilesystemSecurityLog, SecurityLog
from fintracker.infra.telegram.sender import RecordingSender, set_sender_override
from fintracker.runtime.worker import _run_with_lease, build_registry
from tests.integration.factories import build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub
from tests.integration.test_recommendations import TODAY, _complete_fixture, recommendation_json
from tests.readiness.test_async_readiness import prepare_delivery


@pytest.fixture(autouse=True)
def only_controlled_external_calls():
    set_sender_override(RecordingSender())
    set_provider_override(ScriptedAIProvider(responses=[]))
    yield
    set_sender_override(None)
    set_provider_override(None)


@pytest.mark.parametrize("invalidation", ["expired_lease", "security_fence", "quarantined"])
async def test_notification_rechecks_authority_after_render(
    owner_session, test_settings, monkeypatch, invalidation
):
    fixture, job, delivery_id = await prepare_delivery(test_settings, owner_session)
    original_render = render.render_event

    async def invalidate_after_render(*args, **kwargs):
        result = await original_render(*args, **kwargs)
        # The initial gate succeeded. Another process changes the execution or
        # security state while financial content is being prepared for send.
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
        return result

    monkeypatch.setattr(render, "render_event", invalidate_after_render)
    sender = RecordingSender()
    set_sender_override(sender)
    await _run_with_lease(test_settings, job, build_registry())
    assert sender.sent == [], f"Financial notification sent after {invalidation}: {sender.sent}"


async def test_paused_notification_remains_runnable_after_security_gate_clears(
    owner_session, test_settings
):
    fixture, job, delivery_id = await prepare_delivery(test_settings, owner_session)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        await session.execute(
            update(Workspace)
            .where(Workspace.id == fixture.workspace.id)
            .values(security_fence=uuid.uuid4())
        )
    await _run_with_lease(test_settings, job, build_registry())
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        await session.execute(
            update(Workspace)
            .where(Workspace.id == fixture.workspace.id)
            .values(security_fence=None)
        )
        delivery = await session.get(NotificationDelivery, delivery_id)
        jobs = (
            await session.scalars(select(Job).where(Job.job_type == "deliver_notification"))
        ).all()
        states = [(str(row.id), row.state) for row in jobs]
        assert delivery.state == "pending"
    runnable = await queue.claim_jobs(test_settings, queue_classes=("interactive",), limit=10)
    assert runnable, f"Pending notification stranded after temporary ACL fence: {states}"


async def test_older_security_journal_does_not_confirm_current_database(
    owner_session, test_settings, tmp_path
):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    journal_root = tmp_path / "journal"
    journal = SecurityLog(FilesystemSecurityLog(journal_root))

    async def unchanged_access(session, uow, workspace):
        return {"purpose": "synthetic committed access change"}

    await run_security_change(
        test_settings,
        workspace_id=fixture.workspace.id,
        kind="workspace_create",
        initiated_by=fixture.user.id,
        apply=unchanged_access,
        correlation_id="journal-first",
        security_log=journal,
    )
    shutil.copytree(journal_root, tmp_path / "old-journal")
    await run_security_change(
        test_settings,
        workspace_id=fixture.workspace.id,
        kind="member_join",
        initiated_by=fixture.user.id,
        apply=unchanged_access,
        correlation_id="journal-second",
        security_log=journal,
    )
    old_journal = SecurityLog(FilesystemSecurityLog(tmp_path / "old-journal"))
    old = await old_journal.last_committed(fixture.workspace.id)
    await reconcile_access_on_start(test_settings, security_log=old_journal)
    async with session_scope(
        test_settings, RuntimeRole.API, user_id=fixture.user.id, workspace_id=fixture.workspace.id
    ) as session:
        workspace = await session.get(Workspace, fixture.workspace.id)
        assert old.proposed_acl_revision < workspace.acl_revision
        assert workspace.quarantined, (
            f"Access open: DB revision {workspace.acl_revision}, journal revision {old.proposed_acl_revision}"
        )


async def test_published_recommendation_is_not_current_after_new_expense(
    owner_session, test_settings
):
    fixture = await _complete_fixture(owner_session)
    settings = test_settings.model_copy(deep=True)
    settings.ai.enabled = True
    preparation, _ = await analysis._prepare_analysis(
        settings,
        workspace_id=fixture.workspace.id,
        run_kind="weekly_review",
        logical_key="recheck-stale-list",
        today=TODAY,
    )
    set_provider_override(
        ScriptedAIProvider(
            responses=[
                recommendation_json(card={"metric_refs": [str(preparation.metrics["metric_id"])]})
            ]
        )
    )
    generated = await analysis._generate_cards(settings, preparation)
    outcome = await analysis._store_analysis(
        settings, preparation, generated, correlation_id="fresh-at-publication"
    )
    assert outcome.status == "succeeded"
    async with session_scope(
        settings, RuntimeRole.API, user_id=fixture.user.id, workspace_id=fixture.workspace.id
    ) as session:
        await post_transaction(
            session,
            UnitOfWork(session=session, correlation_id="after-publish"),
            actor=fixture.actor,
            spec=expense_spec(fixture, amount=rub(500), category="Рестораны", occurred=TODAY),
            origin="form",
        )
    replies = await analytics_flow.recommendation_action(
        settings, actor=fixture.actor, workspace=fixture.workspace, action="list", rest=[]
    )
    text = "\n".join(reply.text for reply in replies)
    async with session_scope(
        settings, RuntimeRole.WORKER, workspace_id=fixture.workspace.id
    ) as session:
        proposed = (
            await session.scalars(
                select(Recommendation).where(
                    Recommendation.run_id == outcome.run_id, Recommendation.status == "proposed"
                )
            )
        ).all()
    assert "Расходы на рестораны растут быстрее плана" not in text, (
        f"Old recommendation presented as current after a real posted expense: {text}; proposed={len(proposed)}"
    )


async def test_cancelled_receipt_releases_slot_and_preserves_unknown_cost(
    owner_session, test_settings
):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    settings = test_settings.model_copy(deep=True)
    settings.ai.enabled = True

    class Cancelled(ScriptedAIProvider):
        async def structured(self, **kwargs):
            raise asyncio.CancelledError()

    set_provider_override(Cancelled(responses=[]))
    with pytest.raises(asyncio.CancelledError):
        await extraction.extract_receipt(
            settings,
            actor=fixture.actor,
            draft_id=uuid.uuid4(),
            draft_version=1,
            image_data_url="data:image/png;base64,test",
            caption=None,
            catalog=extraction.WorkspaceCatalog((), (), (), "RUB", "Asia/Novosibirsk"),
            reference_date=TODAY,
            source_fingerprint="synthetic-cancelled",
        )
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        reservations = (await session.scalars(select(AICostReservation))).all()
        counters = (await session.scalars(select(AIQuotaCounter))).all()
    assert reservations and all(row.state == "unknown" for row in reservations)
    assert counters and all(row.in_flight == 0 and row.reserved_total > 0 for row in counters)


async def test_maintenance_frees_abandoned_slot_without_erasing_cost(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    from fintracker.application.maintenance.retention import _sweep_reservations

    await quota.reserve(
        test_settings,
        request_key="abandoned-receipt",
        upper_bound=Decimal("0.01"),
        workspace_id=fixture.workspace.id,
        purpose="receipt",
    )
    async with session_scope(test_settings, RuntimeRole.WORKER) as session:
        await session.execute(
            update(AICostReservation).values(
                created_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
            )
        )
    assert await _sweep_reservations(test_settings, dt.datetime.now(dt.UTC)) == 1
    async with session_scope(test_settings, RuntimeRole.WORKER) as session:
        reservations = (await session.scalars(select(AICostReservation))).all()
        counters = (await session.scalars(select(AIQuotaCounter))).all()
    assert all(row.state == "unknown" for row in reservations)
    assert all(row.in_flight == 0 and row.reserved_total == Decimal("0.01") for row in counters)
